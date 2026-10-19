"""Activity lifecycle pairing through the real RunEvent repository and runtime RLS."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from alembic import command
import pytest
from langchain_core.tools import StructuredTool
from sqlalchemy import text

from CAD2BIM_backend.application.task_run_service import TaskRunService
from CAD2BIM_backend.domain.control_plane import ConflictError, CreateTaskRequest, StartRunRequest
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.identity import AuthenticatedSubject
from CAD2BIM_backend.domain.run_events import RunEvent, RunEventScope, RunEventType, runtime_activity_item_id
from CAD2BIM_backend.infrastructure.chat_stream import create_component_presentation
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from tests.integration.backend.test_geometry_resource_migrations import _isolated_database
from tests.integration.backend.test_geometry_resource_postgres import (
    _create_runtime, _create_test_runtime_logins, _drop_test_runtime_logins, _runtime_database_url,
)

pytestmark = [pytest.mark.integration, pytest.mark.network]


@pytest.mark.asyncio
async def test_runtime_stream_persists_model_stage_changes_and_five_parallel_tool_lifecycles():
    with _isolated_database("runtime_activity") as (config, scoped_engine):
        command.upgrade(config, "head")
        admin_url = scoped_engine.url.render_as_string(hide_password=False)
        logins = _create_test_runtime_logins(admin_url)
        api = _create_runtime(_runtime_database_url(admin_url, logins["api"]))
        runner = _create_runtime(_runtime_database_url(admin_url, logins["runner"]))
        restarted = None
        try:
            tasks = TaskRunService(repository=api.repository, observation_repository=runner.repository,
                identity_directory=api.identity_directory,
                execution_protocol_factory=lambda _module, _stage: ExecutionProtocolBinding(
                    bundle_id="component", module="component", stage_key="component"),
                bind_identity=bind_tenant_execution_context)
            now = datetime.now(UTC)
            subject = AuthenticatedSubject(issuer="https://activity.example.test", subject=uuid4().hex,
                audience="component", authenticated_at=now, token_expires_at=now + timedelta(minutes=5))
            task = await tasks.create_task(subject, CreateTaskRequest(title="Isolated activity witness", active_module="component"))
            run = await tasks.start_run(subject, task.task_id, StartRunRequest(module="component", stage_key="component",
                idempotency_key=uuid4().hex, content=({"type": "text", "text": "Do not invoke a model."},)))
            # The caller supplies a separately created test database; this is
            # its only queued Run, never a claim against the live demo database.
            claimed = await runner.repository.claim_next_run("activity-witness", lease_seconds=120)
            assert claimed.run_id == run.run_id
            identity, _ = await tasks.authorized_run_context(subject, task.task_id, run.run_id)
            projection = create_component_presentation(task_runs=tasks, repository=runner.repository,
                graph_runtime=SimpleNamespace(), bind_identity=bind_tenant_execution_context,
                feedback_repository=api.repository, artifact_service=None)
            callback = projection.callback_factory()
            metadata = {"run_id": run.run_id, "thread_id": run.thread_id, "main_message_id": "main-witness"}
            model_call = uuid4()
            await callback.on_chat_model_start({}, [], run_id=model_call, tags=["role_main"], metadata=metadata)
            await callback.on_llm_new_token("", run_id=model_call, activity_stage="tool_arguments")
            await callback.on_llm_end(None, run_id=model_call)
            ready, release = asyncio.Event(), asyncio.Event()
            entered = 0

            async def read_actual_run() -> str:
                """Read the exact test Run while all five actual tools are open."""
                nonlocal entered
                with bind_tenant_execution_context(identity):
                    persisted = await runner.repository.get_run(identity.tenant_id, run.run_id)
                entered += 1
                if entered == 5:
                    ready.set()
                await release.wait()
                return persisted.status.value

            tool = StructuredTool.from_function(coroutine=read_actual_run)
            execution = asyncio.gather(*(tool.ainvoke({"name": tool.name, "id": f"call-{index}",
                "args": {}, "type": "tool_call"}, config={"callbacks": [callback], "metadata": metadata}) for index in range(5)))
            try:
                await asyncio.wait_for(ready.wait(), timeout=10)
                active = await projection.snapshot(subject, task.task_id, run.run_id)
                assert active["activity"]["stage"] == "tools" and active["activity"]["phase"] == "start"
            finally:
                release.set()
                await execution
            with bind_tenant_execution_context(identity):
                events = await runner.repository.get_item_events(identity.tenant_id, run.run_id, runtime_activity_item_id(run.run_id))
            assert len(events) == 13
            assert len({event.item_id for event in events}) == 1
            assert [event.attributes["stage"] for event in events[:3]] == ["model", "tool_arguments", "tool_arguments"]
            assert len([event for event in events if event.attributes["stage"] == "tool_results" and event.attributes["phase"] == "end"]) == 5
            assert len({event.sequence for event in events}) == 13
            # Real repository transition checks still reject a second terminal
            # for one activity; they allow another independent invocation.
            with pytest.raises(ConflictError, match="more than one terminal"):
                await projection.checkpoint_observer.activity_changed(str(model_call), "model", "end", metadata)
            await projection.checkpoint_observer.activity_changed(str(uuid4()), "model", "start", metadata)
            restarted = _create_runtime(_runtime_database_url(admin_url, logins["runner"]))
            restored = create_component_presentation(task_runs=tasks, repository=restarted.repository,
                graph_runtime=SimpleNamespace(), bind_identity=bind_tenant_execution_context,
                feedback_repository=api.repository, artifact_service=None)
            snapshot = await restored.snapshot(subject, task.task_id, run.run_id)
            assert snapshot["activity"]["stage"] == "model" and snapshot["activity"]["phase"] == "start"
            assert snapshot["activity"]["event_sequence"] > events[-1].sequence
            observed_identity, _ = await tasks.checkpoint_observation_context(run.run_id, run.thread_id)
            proposal = RunEvent(event_id="pending-proposal", sequence=1, task_id=task.task_id, run_id=run.run_id,
                event_type="tool.call.started", item_id="pending-tool", scope=RunEventScope(module="component", stage_key="component"),
                occurred_at=now, status=RunEventType.RUNNING,
                attributes={"source_kind": "tool_request", "call_id": "pending-recovery", "message_id": "checkpoint-main"})
            with bind_tenant_execution_context(observed_identity):
                await runner.repository.append_chat_events(identity.tenant_id, run.run_id, (proposal,))
            with scoped_engine.begin() as connection:
                connection.execute(text("UPDATE runs SET lease_expires_at=now()-interval '1 minute' WHERE run_id=:run"), {"run": run.run_id})
            recovered = await runner.repository.claim_next_run("activity-witness-recovery", lease_seconds=120)
            assert recovered.run_id == run.run_id and recovered.claim_count == 2
            with bind_tenant_execution_context(observed_identity):
                await runner.repository.reconcile_reclaimed_runtime_events(
                    run.run_id, "activity-witness-recovery", recovered.lease_token
                )
                recovered_events = await runner.repository.get_item_events(identity.tenant_id, run.run_id, runtime_activity_item_id(run.run_id))
                pending = await runner.repository.get_item_events(identity.tenant_id, run.run_id, proposal.item_id)
                assert pending == (pending[0],) and pending[0].event_type == "tool.call.started"
                assert recovered_events[-1].attributes["phase"] == "error"
                assert recovered_events[-1].attributes["reason_code"] == "runner_lease_reclaimed"
                final = proposal.model_copy(update={"event_id": "pending-result", "event_type": "tool.call.completed",
                    "status": RunEventType.SUCCEEDED, "attributes": {"source_kind": "tool_result", "call_id": "pending-recovery"}})
                await runner.repository.append_chat_events(identity.tenant_id, run.run_id, (final,))
                settled = await runner.repository.get_item_events(identity.tenant_id, run.run_id, proposal.item_id)
                assert [event.event_type for event in settled] == ["tool.call.started", "tool.call.completed"]
        finally:
            if restarted is not None:
                await restarted.close()
            await api.close()
            await runner.close()
            _drop_test_runtime_logins(admin_url, logins)
