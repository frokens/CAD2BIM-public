"""Current input/control transactions on existing nonprivileged repositories.

These are database owner tests. Synthetic cursors exercise the transaction
contract; the real checkpoint/Runner witness belongs to the Component slice.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url

from CAD2BIM_backend.application.feedback_service import FeedbackService
from CAD2BIM_backend.application.task_run_service import TaskRunService
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.control_plane import (
    CreateTaskRequest, FeedbackCommandStatus, FeedbackDeliveryMode, FeedbackScope,
    ResourceNotFoundError, RunStatus, StartRunRequest, SubmitFeedbackCommand,
)
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.identity import AuthenticatedSubject
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest
from CAD2BIM_backend.infrastructure.langgraph.user_gate import build_user_gate_open_request, build_run_pause_open_request
from CAD2BIM_backend.infrastructure.postgres.database import create_database
from CAD2BIM_backend.infrastructure.postgres.identity_directory import PostgresIdentityDirectory
from CAD2BIM_backend.infrastructure.postgres.repository import PostgresControlPlaneRepository
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from CAD2BIM_backend.ports.graph_runtime import GraphCheckpointDescriptor, GraphExecutionResult

pytestmark = [pytest.mark.integration, pytest.mark.network]


def _urls():
    raw = os.environ.get("CAD2BIM_COMPONENT_DATABASE_URL")
    if not raw:
        pytest.skip("coordinated isolated Component PostgreSQL is required")
    url = make_url(raw)
    if url.host not in {"127.0.0.1", "localhost"} or url.database != "component_demo":
        raise RuntimeError("Component input witness requires the coordinated local component_demo database")
    return tuple(url.set(username=name).render_as_string(hide_password=False)
                 for name in ("cad2bim_api_login", "cad2bim_runner_login"))


def _binding(module="component", stage_key="component"):
    return ExecutionProtocolBinding(bundle_id="component", module=module, stage_key=stage_key)


def _cursor(run, label):
    mapping = (("", label),)
    return CheckpointCursor(thread_id=run.thread_id, checkpoint_id=label,
        root_checkpoint_id=label, checkpoint_map=mapping,
        checkpoint_digest=checkpoint_cursor_digest(checkpoint_id=label, checkpoint_ns="", checkpoint_map=mapping))


def _gate(run, label, watermark):
    cursor = _cursor(run, label)
    return build_user_gate_open_request(run, GraphExecutionResult(
        values={"business_entry_id": label, "gate_target_role": "component", "input_high_watermark": watermark},
        checkpoint=GraphCheckpointDescriptor(thread_id=run.thread_id, checkpoint_id=label,
            root_checkpoint_id=label, checkpoint_map=cursor.checkpoint_map,
            checkpoint_map_digest=cursor.checkpoint_digest, next_nodes=("user_gate",),
            created_at=datetime.now(timezone.utc).isoformat()),
        interrupts=({"id": f"graph-{label}", "value": {"kind": "user_gate", "business_entry_id": label,
            "target_role": "component", "input_high_watermark": watermark}},)))


async def _claim(repository, run_id, runner_id):
    run = await repository.claim_next_run(runner_id, lease_seconds=120)
    if run is not None and run.run_id != run_id:
        raise AssertionError("shared Runner must be idle during the isolated owner witness")
    return run


@pytest.mark.asyncio
async def test_original_input_all_pending_boundary_and_gate_pause_controls():
    api_url, runner_url = _urls()
    api_db = create_database(Settings(database_url=api_url))
    runner_db = create_database(Settings(database_url=runner_url))
    api = PostgresControlPlaneRepository(api_db.session_factory)
    runner = PostgresControlPlaneRepository(runner_db.session_factory)
    identities = PostgresIdentityDirectory(api_db.session_factory)
    tasks = TaskRunService(repository=api, observation_repository=runner,
        identity_directory=identities, execution_protocol_factory=_binding,
        bind_identity=bind_tenant_execution_context)
    feedback = FeedbackService(repository=runner, identity_directory=identities,
                               bind_identity=bind_tenant_execution_context)
    now = datetime.now(timezone.utc)
    subject = AuthenticatedSubject(issuer="https://component-input.example.test", subject=f"user-{uuid4().hex}",
        audience="component", authenticated_at=now, token_expires_at=now + timedelta(hours=1))
    try:
        task = await tasks.create_task(subject, CreateTaskRequest(title="Ordered Component input", active_module="component"))
        invalid = StartRunRequest(module="component", stage_key="component", idempotency_key=f"bad-{uuid4().hex}",
            content=({"type": "image", "artifact_id": "artifact-missing", "version_id": "version-missing"},))
        with pytest.raises(ResourceNotFoundError):
            await tasks.start_run(subject, task.task_id, invalid)
        assert next(entry for entry in await tasks.list_tasks(subject) if entry[0].task_id == task.task_id)[1] is None
        request = StartRunRequest(module="component", stage_key="component", idempotency_key=f"first-{uuid4().hex}",
                                  content=({"type": "text", "text": "首条真实要求" * 1000},))
        run = await tasks.start_run(subject, task.task_id, request)
        replay = await tasks.start_run(subject, task.task_id, request)
        assert replay.run_id == run.run_id and replay.initial_feedback == run.initial_feedback
        assert run.initial_feedback is not None and not run.input_payload and task.instruction is None
        claimed = await _claim(runner, run.run_id, "component-input-first")
        assert claimed is not None
        grant = RunnerCheckpointGrant.from_claim(claimed, runner_id="component-input-first")
        active = await feedback.activate_feedback_subject(grant, business_entry_id="entry-1", stage_key="component",
            agent="component", attempt=101, checkpoint_id="input-1")
        first = await feedback.freeze_for_main(grant, stage_key="component", role_execution_id=active.subject_id,
                                              input_checkpoint_cursor=_cursor(run, "input-1"))
        initial_messages = (await feedback.delivery_update(grant, {}, first))["messages"]
        assert initial_messages[0].additional_kwargs["input_kind"] == "initial_input"
        assert not await feedback.freeze_for_main(grant, stage_key="component", role_execution_id=active.subject_id,
            input_checkpoint_cursor=_cursor(run, "accepted-1"), delivered_batch_ids=tuple(batch.batch.batch_id for batch in first))
        assert await feedback.decide_role_exit(grant, active.subject_id, 1,
            input_checkpoint_cursor=_cursor(run, "exit-1"), delivered_batch_ids=())
        gate = _gate(claimed, "gate-1", 1)
        await feedback.open_gate(grant, gate)
        assert (await feedback.park_waiting_user(grant, gate.requested_interrupt_id)).status is RunStatus.WAITING_USER
        assert (await tasks.pause_run(subject, task.task_id, run.run_id, "pause-at-gate")).status is RunStatus.PAUSED
        assert (await tasks.continue_run(subject, task.task_id, run.run_id, "continue-empty-gate")).status is RunStatus.WAITING_USER
        first_later = await feedback.submit_feedback(subject, task.task_id, run.run_id, SubmitFeedbackCommand(
            client_request_id="preselected", delivery_mode=FeedbackDeliveryMode.STEER,
            content=({"type": "text", "text": "A"},), scope=FeedbackScope(run_id=run.run_id)))
        claimed = await _claim(runner, run.run_id, "component-input-second")
        assert claimed is not None
        grant = RunnerCheckpointGrant.from_claim(claimed, runner_id="component-input-second")
        plan = await feedback.load_gate_resume(grant)
        assert plan is not None and plan.frozen_batch is not None
        original_batch = plan.frozen_batch.batch.batch_id
        for index in range(105):
            await feedback.submit_feedback(subject, task.task_id, run.run_id, SubmitFeedbackCommand(
                client_request_id=f"after-selection-{index}", delivery_mode=FeedbackDeliveryMode.STEER,
                content=({"type": "text", "text": f"B{index}"},), scope=FeedbackScope(run_id=run.run_id)))
        active = await feedback.activate_feedback_subject(grant, business_entry_id="entry-2", stage_key="component",
            agent="component", attempt=102, checkpoint_id="input-2")
        batches = await feedback.freeze_for_main(grant, stage_key="component", role_execution_id=active.subject_id,
                                                input_checkpoint_cursor=_cursor(run, "input-2"))
        assert original_batch in {batch.batch.batch_id for batch in batches}
        assert len(batches) == 2 and sum(len(batch.members) for batch in batches) == 106
        update = await feedback.delivery_update(grant, {"messages": initial_messages}, batches)
        assert len(update["messages"]) == 107
        assert [message.additional_kwargs["feedback_id"] for message in update["messages"]][1] == first_later.feedback_id
        assert not await feedback.freeze_for_main(grant, stage_key="component", role_execution_id=active.subject_id,
            input_checkpoint_cursor=_cursor(run, "accepted-2"), delivered_batch_ids=update["feedback_batch_ids"])
        identity, _ = await tasks.authorized_run_context(subject, task.task_id, run.run_id)
        with bind_tenant_execution_context(identity):
            accepted = await api.list_feedback(run.owner_id, run.run_id, limit=500)
            observed_identity, observed_run = await tasks.checkpoint_observation_context(run.run_id, run.thread_id)
            assert observed_identity == identity and observed_run.run_id == run.run_id
        assert len(accepted) == 107 and all(item.status is FeedbackCommandStatus.DELIVERED for item in accepted)
        assert accepted[1].delivered_checkpoint_id == "accepted-2"
        assert await feedback.decide_role_exit(grant, active.subject_id, 107,
            input_checkpoint_cursor=_cursor(run, "exit-2"), delivered_batch_ids=())
        gate = _gate(claimed, "gate-2", 107)
        await feedback.open_gate(grant, gate)
        assert (await tasks.pause_run(subject, task.task_id, run.run_id, "pause-before-park")).pause_requested
        await feedback.submit_feedback(subject, task.task_id, run.run_id, SubmitFeedbackCommand(
            client_request_id="while-finishing", delivery_mode=FeedbackDeliveryMode.STEER,
            content=({"type": "text", "text": "C"},), scope=FeedbackScope(run_id=run.run_id)))
        assert (await feedback.park_waiting_user(grant, gate.requested_interrupt_id)).status is RunStatus.PAUSED
        assert (await tasks.continue_run(subject, task.task_id, run.run_id, "continue-gate-input")).status is RunStatus.QUEUED
        snapshot = await tasks.chat_snapshot_basis(subject, task.task_id, run.run_id, limit=2)
        assert snapshot[0].run_id == run.run_id and len(snapshot[1]) == 2
        assert snapshot[2] == 108 and snapshot[3] is not None
        with bind_tenant_execution_context(identity):
            await api.cancel_run(run.owner_id, run.run_id)
    finally:
        await api_db.dispose()
        await runner_db.dispose()


@pytest.mark.asyncio
async def test_saved_leaf_pause_continues_original_selection_without_feedback_injection():
    api_url, runner_url = _urls()
    api_db = create_database(Settings(database_url=api_url))
    runner_db = create_database(Settings(database_url=runner_url))
    api = PostgresControlPlaneRepository(api_db.session_factory)
    runner = PostgresControlPlaneRepository(runner_db.session_factory)
    identities = PostgresIdentityDirectory(api_db.session_factory)
    tasks = TaskRunService(repository=api, observation_repository=runner, identity_directory=identities,
        execution_protocol_factory=_binding, bind_identity=bind_tenant_execution_context)
    feedback = FeedbackService(repository=runner, identity_directory=identities,
                               bind_identity=bind_tenant_execution_context)
    now = datetime.now(timezone.utc)
    subject = AuthenticatedSubject(issuer="https://component-pause.example.test", subject=f"user-{uuid4().hex}",
        audience="component", authenticated_at=now, token_expires_at=now + timedelta(hours=1))
    try:
        task = await tasks.create_task(subject, CreateTaskRequest(title="Original pause control", active_module="component"))
        run = await tasks.start_run(subject, task.task_id, StartRunRequest(module="component", stage_key="component",
            idempotency_key=f"run-{uuid4().hex}", content=({"type": "text", "text": "initial"},)))
        claimed = await _claim(runner, run.run_id, "component-pause-first")
        grant = RunnerCheckpointGrant.from_claim(claimed, runner_id="component-pause-first")
        assert (await tasks.pause_run(subject, task.task_id, run.run_id, "pause-leaf")).pause_requested
        namespace = "component_role:saved-child"
        mapping = (("", "root-pause"), (namespace, "leaf-pause"))
        result = GraphExecutionResult(values={"business_entry_id": "entry-original"},
            checkpoint=GraphCheckpointDescriptor(thread_id=run.thread_id, checkpoint_id="leaf-pause",
                checkpoint_ns=namespace, root_checkpoint_id="root-pause", checkpoint_map=mapping,
                checkpoint_map_digest=checkpoint_cursor_digest(checkpoint_id="leaf-pause", checkpoint_ns=namespace, checkpoint_map=mapping),
                next_nodes=("pause_before_llm",), created_at=now.isoformat()),
            interrupts=({"id": "graph-original-pause", "value": {"kind": "run_pause", "business_entry_id": "entry-original"}},))
        request = build_run_pause_open_request(claimed, result)
        paused = await feedback.open_run_pause(grant, request)
        assert paused.status is RunStatus.PAUSED and paused.claimed_by is None and paused.lease_token is None
        await feedback.submit_feedback(subject, task.task_id, run.run_id, SubmitFeedbackCommand(
            client_request_id="saved-during-pause", delivery_mode=FeedbackDeliveryMode.STEER,
            content=({"type": "text", "text": "later"},), scope=FeedbackScope(run_id=run.run_id)))
        assert (await tasks.get_run(subject, task.task_id, run.run_id)).status is RunStatus.PAUSED
        continued = await tasks.continue_run(subject, task.task_id, run.run_id, "continue-original")
        assert continued.run_id == run.run_id and continued.status is RunStatus.QUEUED
        claimed = await _claim(runner, run.run_id, "component-pause-second")
        grant = RunnerCheckpointGrant.from_claim(claimed, runner_id="component-pause-second")
        selection = await feedback.load_run_continue(grant)
        operation_id, graph_id, resume = selection
        assert graph_id == "graph-original-pause" and resume.checkpoint_cursor == request.runtime_envelope.checkpoint_cursor
        assert resume.decision.action == "continue" and resume.decision.message is None
        assert resume.feedback_batch_id is None and resume.selected_feedback_ids == ()
        assert (await tasks.continue_run(subject, task.task_id, run.run_id, "continue-original")).run_id == run.run_id
        assert (await feedback.load_run_continue(grant))[0] == operation_id
        identity, observed = await tasks.checkpoint_observation_context(run.run_id, run.thread_id)
        assert identity.task_id == task.task_id and observed.run_id == run.run_id
        with bind_tenant_execution_context(grant.scope.identity):
            await runner.pause_claimed(run.run_id, "component-pause-second", grant.lease_token)
        user_identity, _ = await tasks.authorized_run_context(subject, task.task_id, run.run_id)
        with bind_tenant_execution_context(user_identity):
            await api.cancel_run(run.owner_id, run.run_id)
    finally:
        await api_db.dispose()
        await runner_db.dispose()
