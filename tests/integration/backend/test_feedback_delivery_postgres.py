"""Original frozen feedback survives a nonprivileged Runner claim takeover."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from sqlalchemy import select
from sqlalchemy.engine import make_url

from CAD2BIM_backend.application.feedback_service import FeedbackService
from CAD2BIM_backend.application.runner_execution_service import RunnerExecutionService
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.control_plane import (
    CreateTaskRequest,
    FeedbackDeliveryMode,
    FeedbackScope,
    FeedbackWakeResumePlan,
    UserGateResumePlan,
    ConflictError,
    LeaseConflictError,
    RunStatus,
    RunRecord,
    StartRunRequest,
    SubmitFeedbackCommand,
    feedback_message_id,
    RespondGateCommand,
)
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.identity import AuthenticatedSubject
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.domain.runner_context import RunnerGraphContext
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest
from CAD2BIM_backend.infrastructure.postgres.database import create_database
from CAD2BIM_backend.infrastructure.postgres.identity_directory import PostgresIdentityDirectory
from CAD2BIM_backend.infrastructure.postgres.models import (
    FeedbackCommandModel,
    FeedbackDeliveryBatchMemberModel,
    FeedbackDeliveryBatchModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository import PostgresControlPlaneRepository
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context, set_tenant_execution_context
from CAD2BIM_backend.infrastructure.langgraph.feedback_nodes import FeedbackNodes
from CAD2BIM_backend.infrastructure.langgraph.runtime import AsyncPostgresGraphRuntime
from CAD2BIM_backend.infrastructure.langgraph.user_gate import (
    build_user_gate_open_request,
    reentry_route_update,
)
from CAD2BIM_backend.ports.graph_runtime import GraphCheckpointDescriptor, GraphExecutionResult


pytestmark = [pytest.mark.integration, pytest.mark.network]


def _urls() -> tuple[str, str]:
    url = make_url(os.environ["CAD2BIM_BATCH3_END_DATABASE_URL"])
    if not (url.database or "").startswith("cad2bim_batch3_end_"):
        raise RuntimeError("feedback delivery requires the isolated batch-3 database")
    if url.host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("feedback delivery requires local PostgreSQL")
    return (
        url.set(username="cad2bim_api_login").render_as_string(hide_password=False),
        url.set(username="cad2bim_runner_login").render_as_string(hide_password=False),
    )


def _binding() -> ExecutionProtocolBinding:
    return ExecutionProtocolBinding(
        bundle_id="feedback_delivery", module="workflow", stage_key="cad2bim_workflow",
    )


async def _claim_target(
    repository: PostgresControlPlaneRepository,
    run_id: str,
    runner_id: str,
    *,
    lease_seconds: int = 30,
):
    # The dedicated database may retain queued Runs from an earlier failed assertion.
    for _ in range(20):
        claimed = await repository.claim_next_run(runner_id, lease_seconds=lease_seconds)
        if claimed is None or claimed.run_id == run_id:
            return claimed
    raise AssertionError("too many unrelated queued Runs in the isolated database")


class _RecoveryState(TypedDict, total=False):
    role: str
    stage_key: str
    business_entry_id: str
    messages: list[Any]
    feedback_batch_id: str | None
    feedback_subject_id: str
    input_high_watermark: int
    main_count: int


def _gate_request(claimed: RunRecord, now: datetime):
    checkpoint_id = f"gate-checkpoint-{uuid4().hex}"
    mapping = (("", checkpoint_id),)
    binding = _binding()
    request = build_user_gate_open_request(
        claimed,
        GraphExecutionResult(
            values={
                "business_entry_id": "entry-before-park",
                "gate_target_role": "requirement",
                "input_high_watermark": 0,
            },
            checkpoint=GraphCheckpointDescriptor(
                thread_id=claimed.thread_id,
                checkpoint_id=checkpoint_id,
                root_checkpoint_id=checkpoint_id,
                checkpoint_map=mapping,
                checkpoint_map_digest=checkpoint_cursor_digest(
                    checkpoint_id=checkpoint_id, checkpoint_ns="",
                    checkpoint_map=mapping,
                ),
                next_nodes=("user_gate",),
                created_at=now.isoformat(),
            ),
            interrupts=({
                "id": f"graph-interrupt-{uuid4().hex}",
                "value": {
                    "kind": "user_gate",
                    "business_entry_id": "entry-before-park",
                    "target_role": "requirement",
                    "input_high_watermark": 0,
                },
            },),
        ),
    )
    return request, checkpoint_id


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["feedback", "response"])
async def test_signal_arriving_before_park_keeps_run_claimable_without_notification(
    signal: str,
) -> None:
    api_url, runner_url = _urls()
    api_db = create_database(Settings(database_url=api_url, environment="test"))
    runner_db = create_database(Settings(database_url=runner_url, environment="test"))
    api = PostgresControlPlaneRepository(api_db.session_factory)
    runner = PostgresControlPlaneRepository(runner_db.session_factory)
    identities = PostgresIdentityDirectory(api_db.session_factory)
    service = FeedbackService(
        repository=runner, identity_directory=identities,
        bind_identity=bind_tenant_execution_context,
    )
    try:
        now = datetime.now(timezone.utc)
        subject = AuthenticatedSubject(
            issuer="https://batch3-feedback.example",
            subject=f"person-{uuid4().hex}",
            audience="cad2bim-batch3",
            authenticated_at=now,
            token_expires_at=now + timedelta(hours=1),
        )
        identity = await identities.resolve_authenticated_subject(
            subject, command_id=f"feedback_{uuid4().hex}"
        )
        with bind_tenant_execution_context(identity):
            task = await api.create_task(
                identity.tenant_id, CreateTaskRequest(title=f"{signal} before Gate park")
            )
            run = await api.create_run(
                identity.tenant_id, task.task_id,
                StartRunRequest(idempotency_key=f"run-{uuid4().hex}"),
                execution_protocol_factory=_binding,
            )
        claimed = await _claim_target(runner, run.run_id, "batch3-park-first")
        assert claimed is not None and claimed.run_id == run.run_id
        grant = RunnerCheckpointGrant.from_claim(claimed, runner_id="batch3-park-first")
        request, checkpoint_id = _gate_request(claimed, now)
        await service.open_gate(grant, request)
        if signal == "feedback":
            accepted = await service.submit_feedback(
                subject, task.task_id, run.run_id,
                SubmitFeedbackCommand(
                    client_request_id=f"before-park-{uuid4().hex}",
                    delivery_mode=FeedbackDeliveryMode.STEER,
                    body="Accepted after Gate save and before Runner park.",
                    scope=FeedbackScope(run_id=run.run_id, stage_key=run.stage_key),
                ),
            )
        else:
            original_command = RespondGateCommand(
                interrupt_id=request.requested_interrupt_id,
                action="approve", idempotency_key=f"before-park-{uuid4().hex}",
            )
            original_receipt = await service.respond_gate(
                subject, task.task_id, run.run_id, original_command
            )
        parked = await service.park_waiting_user(grant, request.requested_interrupt_id)
        assert parked.status is RunStatus.QUEUED
        assert parked.claimed_by is None and parked.lease_token is None
        replacement = await _claim_target(runner, run.run_id, "batch3-park-replacement")
        assert replacement is not None and replacement.run_id == run.run_id
        next_grant = RunnerCheckpointGrant.from_claim(
            replacement, runner_id="batch3-park-replacement"
        )
        plan = await service.load_gate_resume(next_grant)
        assert plan.source_checkpoint_cursor.checkpoint_id == checkpoint_id
        task_identity = identity.model_copy(update={"task_id": task.task_id})
        assert await api.get_pending_gate(task_identity, run.run_id) is None
        if signal == "feedback":
            assert isinstance(plan, FeedbackWakeResumePlan)
            assert tuple(item.feedback_id for item in plan.frozen_batch.members) == (
                accepted.feedback_id,
            )
        else:
            assert isinstance(plan, UserGateResumePlan)
            assert plan.response.decision.action == "approve"
            assert await service.respond_gate(
                subject, task.task_id, run.run_id, original_command
            ) == original_receipt
            later = await service.submit_feedback(
                subject, task.task_id, run.run_id,
                SubmitFeedbackCommand(
                    client_request_id=f"after-resume-selection-{uuid4().hex}",
                    delivery_mode=FeedbackDeliveryMode.STEER,
                    body="Accepted after the saved Gate route was selected.",
                    scope=FeedbackScope(run_id=run.run_id, stage_key=run.stage_key),
                ),
            )
            selected_again = await service.load_gate_resume(next_grant)
            assert isinstance(selected_again, UserGateResumePlan)
            assert selected_again.operation_id == plan.operation_id
            assert selected_again.source_checkpoint_cursor == plan.source_checkpoint_cursor
            assert selected_again.frozen_batch is None
            terminal_source = f"terminal-{uuid4().hex}"
            selected_terminal = await service.select_terminal_reentry(
                next_grant, source_checkpoint_id=terminal_source
            )
            assert selected_terminal is not None
            assert selected_terminal.source_checkpoint_id == terminal_source
            assert tuple(
                item.feedback_id
                for item in selected_terminal.gate_plan.frozen_batch.members
            ) == (later.feedback_id,)
            newer = await service.submit_feedback(
                subject, task.task_id, run.run_id,
                SubmitFeedbackCommand(
                    client_request_id=f"after-terminal-selection-{uuid4().hex}",
                    delivery_mode=FeedbackDeliveryMode.STEER,
                    body="Accepted after terminal source and operation were saved.",
                    scope=FeedbackScope(run_id=run.run_id, stage_key=run.stage_key),
                ),
            )
            assert await service.load_terminal_reentry(next_grant) == selected_terminal
            assert await service.select_terminal_reentry(
                next_grant, source_checkpoint_id=terminal_source
            ) == selected_terminal
            async with api_db.session_factory() as session, session.begin():
                await set_tenant_execution_context(session, identity)
                original = await session.get(FeedbackCommandModel, later.feedback_id)
                subsequent = await session.get(FeedbackCommandModel, newer.feedback_id)
                assert original is not None and original.status == "claimed"
                assert subsequent is not None and subsequent.status == "accepted"
        with pytest.raises(ConflictError, match="already consumed"):
            await runner.respond_gate(
                task_identity, run.run_id, request.requested_interrupt_id,
                RespondGateCommand(
                    interrupt_id=request.requested_interrupt_id,
                    action="approve", idempotency_key=f"late-{uuid4().hex}",
                ),
            )
    finally:
        await runner_db.dispose()
        await api_db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_timing", ["before_open", "after_open"])
async def test_cancel_during_gate_open_and_park_finishes_without_user_input(
    cancel_timing: str,
) -> None:
    api_url, runner_url = _urls()
    api_db = create_database(Settings(database_url=api_url, environment="test"))
    runner_db = create_database(Settings(database_url=runner_url, environment="test"))
    api = PostgresControlPlaneRepository(api_db.session_factory)
    runner = PostgresControlPlaneRepository(runner_db.session_factory)
    identities = PostgresIdentityDirectory(api_db.session_factory)
    service = FeedbackService(
        repository=runner, identity_directory=identities,
        bind_identity=bind_tenant_execution_context,
    )
    try:
        now = datetime.now(timezone.utc)
        subject = AuthenticatedSubject(
            issuer="https://batch3-feedback.example",
            subject=f"person-{uuid4().hex}",
            audience="cad2bim-batch3",
            authenticated_at=now,
            token_expires_at=now + timedelta(hours=1),
        )
        identity = await identities.resolve_authenticated_subject(
            subject, command_id=f"feedback_{uuid4().hex}"
        )
        with bind_tenant_execution_context(identity):
            task = await api.create_task(
                identity.tenant_id, CreateTaskRequest(title=f"Cancel {cancel_timing} Gate")
            )
            run = await api.create_run(
                identity.tenant_id, task.task_id,
                StartRunRequest(idempotency_key=f"run-{uuid4().hex}"),
                execution_protocol_factory=_binding,
            )
        claimed = await _claim_target(runner, run.run_id, f"batch3-cancel-{cancel_timing}")
        assert claimed is not None and claimed.run_id == run.run_id
        grant = RunnerCheckpointGrant.from_claim(
            claimed, runner_id=f"batch3-cancel-{cancel_timing}"
        )
        request, checkpoint_id = _gate_request(claimed, now)
        mapping = (("", checkpoint_id),)
        source = CheckpointCursor(
            thread_id=run.thread_id, checkpoint_id=checkpoint_id, checkpoint_ns="",
            root_checkpoint_id=checkpoint_id, checkpoint_map=mapping,
            checkpoint_digest=checkpoint_cursor_digest(
                checkpoint_id=checkpoint_id, checkpoint_ns="", checkpoint_map=mapping,
            ),
        )
        active = await service.activate_feedback_subject(
            grant, business_entry_id="entry-before-park", stage_key=run.stage_key,
            agent="requirement", attempt=1, checkpoint_id=checkpoint_id,
        )
        first = await service.submit_feedback(
            subject, task.task_id, run.run_id,
            SubmitFeedbackCommand(
                client_request_id=f"delivered-{uuid4().hex}",
                delivery_mode=FeedbackDeliveryMode.STEER,
                body="Delivered body must remain delivered.",
                scope=FeedbackScope(
                    run_id=run.run_id, stage_key=run.stage_key,
                    role_execution_id=active.subject_id,
                ),
            ),
        )
        delivered_batch = await service.freeze_for_main(
            grant, stage_key=run.stage_key, role_execution_id=active.subject_id,
            input_checkpoint_cursor=source,
        )
        assert delivered_batch is not None
        assert await service.freeze_for_main(
            grant, stage_key=run.stage_key, role_execution_id=active.subject_id,
            input_checkpoint_cursor=source, delivered_batch_id=delivered_batch.batch.batch_id,
        ) is None
        second = await service.submit_feedback(
            subject, task.task_id, run.run_id,
            SubmitFeedbackCommand(
                client_request_id=f"claimed-{uuid4().hex}",
                delivery_mode=FeedbackDeliveryMode.STEER,
                body="Unconfirmed body remains in batch audit.",
                scope=FeedbackScope(
                    run_id=run.run_id, stage_key=run.stage_key,
                    role_execution_id=active.subject_id,
                ),
            ),
        )
        claimed_batch = await service.freeze_for_main(
            grant, stage_key=run.stage_key, role_execution_id=active.subject_id,
            input_checkpoint_cursor=source,
        )
        assert claimed_batch is not None
        if cancel_timing == "after_open":
            await service.open_gate(grant, request)
        with bind_tenant_execution_context(identity):
            requested = await api.cancel_run(identity.tenant_id, run.run_id)
        assert requested.cancel_requested and requested.status is RunStatus.RUNNING
        if cancel_timing == "before_open":
            await service.open_gate(grant, request)
        finished = await service.park_waiting_user(grant, request.requested_interrupt_id)
        assert finished.status is RunStatus.CANCELLED
        assert finished.claimed_by is None and finished.lease_token is None
        assert await api.get_pending_gate(
            identity.model_copy(update={"task_id": task.task_id}), run.run_id
        ) is None
        async with api_db.session_factory() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            commands = (await session.scalars(
                select(FeedbackCommandModel).where(FeedbackCommandModel.run_id == run.run_id)
            )).all()
            by_id = {item.feedback_id: item for item in commands}
            assert by_id[first.feedback_id].status == "delivered"
            assert by_id[first.feedback_id].delivered_checkpoint_id == "saved-before-cancel"
            assert by_id[second.feedback_id].status == "superseded"
            assert by_id[second.feedback_id].claimed_batch_id is None
            members = (await session.scalars(
                select(FeedbackDeliveryBatchMemberModel).where(
                    FeedbackDeliveryBatchMemberModel.batch_id == claimed_batch.batch.batch_id
                )
            )).all()
            assert tuple(item.feedback_id for item in members) == (second.feedback_id,)
    finally:
        await runner_db.dispose()
        await api_db.dispose()


@pytest.mark.asyncio
async def test_fail_after_cancel_request_supersedes_undelivered_feedback() -> None:
    api_url, runner_url = _urls()
    api_db = create_database(Settings(database_url=api_url, environment="test"))
    runner_db = create_database(Settings(database_url=runner_url, environment="test"))
    api = PostgresControlPlaneRepository(api_db.session_factory)
    runner = PostgresControlPlaneRepository(runner_db.session_factory)
    identities = PostgresIdentityDirectory(api_db.session_factory)
    service = FeedbackService(
        repository=runner, identity_directory=identities,
        bind_identity=bind_tenant_execution_context,
    )
    try:
        now = datetime.now(timezone.utc)
        subject = AuthenticatedSubject(
            issuer="https://batch3-feedback.example",
            subject=f"person-{uuid4().hex}",
            audience="cad2bim-batch3",
            authenticated_at=now,
            token_expires_at=now + timedelta(hours=1),
        )
        identity = await identities.resolve_authenticated_subject(
            subject, command_id=f"feedback_{uuid4().hex}"
        )
        with bind_tenant_execution_context(identity):
            task = await api.create_task(
                identity.tenant_id, CreateTaskRequest(title="Cancel before fail")
            )
            run = await api.create_run(
                identity.tenant_id, task.task_id,
                StartRunRequest(idempotency_key=f"run-{uuid4().hex}"),
                execution_protocol_factory=_binding,
            )
        claimed = await _claim_target(runner, run.run_id, "batch3-fail-after-cancel")
        assert claimed is not None and claimed.run_id == run.run_id
        accepted = await service.submit_feedback(
            subject, task.task_id, run.run_id,
            SubmitFeedbackCommand(
                client_request_id=f"pending-{uuid4().hex}",
                delivery_mode=FeedbackDeliveryMode.STEER,
                body="Accepted but not yet delivered.",
                scope=FeedbackScope(run_id=run.run_id, stage_key=run.stage_key),
            ),
        )
        with bind_tenant_execution_context(identity):
            requested = await api.cancel_run(identity.tenant_id, run.run_id)
        assert requested.cancel_requested
        with bind_tenant_execution_context(
            RunnerCheckpointGrant.from_claim(
                claimed, runner_id="batch3-fail-after-cancel"
            ).scope.identity
        ):
            finished = await runner.fail(
                run.run_id, "batch3-fail-after-cancel", claimed.lease_token,
                error="process stopped after cancellation",
            )
        assert finished.status is RunStatus.CANCELLED
        async with api_db.session_factory() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            command = await session.get(FeedbackCommandModel, accepted.feedback_id)
            assert command is not None
            assert command.status == "superseded"
            assert command.body == "Accepted but not yet delivered."
    finally:
        await runner_db.dispose()
        await api_db.dispose()


@pytest.mark.asyncio
async def test_feedback_checkpoint_fault_windows_resume_original_batch_once() -> None:
    api_url, runner_url = _urls()
    api_db = create_database(Settings(database_url=api_url, environment="test"))
    runner_db = create_database(Settings(database_url=runner_url, environment="test"))
    api = PostgresControlPlaneRepository(api_db.session_factory)
    runner = PostgresControlPlaneRepository(runner_db.session_factory)
    identities = PostgresIdentityDirectory(api_db.session_factory)
    runtime: AsyncPostgresGraphRuntime | None = None
    prepare_batches: list[str] = []
    main_calls: list[str] = []
    body = "Saved full feedback survives both crash windows."
    try:
        source = {
            "role": "requirement", "stage_key": "cad2bim_workflow",
            "business_entry_id": "entry-recovery", "messages": [],
            "input_high_watermark": 0, "main_count": 0,
        }
        now = datetime.now(timezone.utc)
        subject = AuthenticatedSubject(
            issuer="https://batch3-feedback.example",
            subject=f"person-{uuid4().hex}",
            audience="cad2bim-batch3",
            authenticated_at=now,
            token_expires_at=now + timedelta(hours=1),
        )
        identity = await identities.resolve_authenticated_subject(
            subject, command_id=f"feedback_{uuid4().hex}"
        )
        with bind_tenant_execution_context(identity):
            task = await api.create_task(
                identity.tenant_id, CreateTaskRequest(title="Feedback checkpoint fault windows")
            )
            run = await api.create_run(
                identity.tenant_id, task.task_id,
                StartRunRequest(
                    idempotency_key=f"run-{uuid4().hex}", input_payload=source
                ),
                execution_protocol_factory=_binding,
            )
        receipt = await FeedbackService(
            repository=runner, identity_directory=identities,
            bind_identity=bind_tenant_execution_context,
        ).submit_feedback(
            subject, task.task_id, run.run_id,
            SubmitFeedbackCommand(
                client_request_id=f"feedback-{uuid4().hex}",
                delivery_mode=FeedbackDeliveryMode.STEER,
                body=body,
                scope=FeedbackScope(run_id=run.run_id, stage_key=run.stage_key),
            ),
        )
        operation_id = f"run:{run.run_id}:start"

        for generation, fault in (
            (1, "before_save"),
            (2, "before_main"),
            (3, "after_settle"),
            (4, "before_complete"),
            (5, "none"),
        ):
            runner_id = f"batch3-recovery-{generation}"
            lease_seconds = 1 if generation < 5 else 30

            class TargetRunRepository:
                last_claim: RunRecord | None = None

                async def claim_next_run(self, runner_id: str, *, lease_seconds: int = 30):
                    self.last_claim = await _claim_target(
                        runner, run.run_id, runner_id, lease_seconds=lease_seconds
                    )
                    return self.last_claim

                async def heartbeat(self, *args, **kwargs):
                    return await runner.heartbeat(*args, **kwargs)

                async def complete(self, *args, **kwargs):
                    if fault == "before_complete":
                        raise RuntimeError("fault after Graph terminal before Run complete")
                    return await runner.complete(*args, **kwargs)

            target_runs = TargetRunRepository()
            service = FeedbackService(
                repository=runner, identity_directory=identities,
                bind_identity=bind_tenant_execution_context,
            )
            nodes = FeedbackNodes(service)

            async def prepare(state: _RecoveryState, config, runtime: Runtime[RunnerGraphContext]):
                update = await nodes.prepare_main(state, config, runtime)
                batch_id = update.get("feedback_batch_id")
                assert batch_id is not None
                prepare_batches.append(batch_id)
                if fault == "before_save":
                    raise RuntimeError("fault before feedback checkpoint save")
                return update

            async def main(state: _RecoveryState) -> dict[str, int]:
                if fault == "before_main":
                    raise RuntimeError("fault after checkpoint before Main")
                main_calls.append(state["business_entry_id"])
                return {"main_count": state.get("main_count", 0) + 1}

            async def role_exit(state: _RecoveryState, config, runtime: Runtime[RunnerGraphContext]):
                update = await nodes.decide_role_exit(state, config, runtime)
                if fault == "after_settle":
                    raise RuntimeError("fault after batch settlement before exit State save")
                return update

            def graph_builder(saver):
                graph = StateGraph(_RecoveryState, context_schema=RunnerGraphContext)
                graph.add_node("prepare_main", prepare)
                graph.add_node("main", main)
                graph.add_edge(START, "prepare_main")
                graph.add_node("role_exit", role_exit)
                graph.add_edge("prepare_main", "main")
                graph.add_edge("main", "role_exit")
                graph.add_edge("role_exit", END)
                return graph.compile(checkpointer=saver)

            runtime = await AsyncPostgresGraphRuntime.open(
                database_url=runner_url, graph_builder=graph_builder,
                execution_protocol=_binding(),
            )
            nodes.bind_grant_provider(runtime.require_current_execution_grant)
            execution = RunnerExecutionService(
                repository=target_runs,
                graph_runtime=runtime,
                feedback_service=service,
                build_gate_open_request=build_user_gate_open_request,
                build_gate_reentry_update=lambda plan, clarifications: reentry_route_update(
                    plan, clarifications=clarifications
                ),
                bind_execution_context=bind_tenant_execution_context,
                runner_id=runner_id,
                lease_seconds=lease_seconds,
                heartbeat_interval_seconds=0.2 if generation < 5 else 10,
            )
            try:
                if generation < 5:
                    with pytest.raises(RuntimeError, match="fault"):
                        await execution.execute_next()
                else:
                    completed = await execution.execute_next()
                    assert completed is not None
                    assert completed.status is RunStatus.COMPLETED
                claimed = target_runs.last_claim
                assert claimed is not None and claimed.run_id == run.run_id
                grant = RunnerCheckpointGrant.from_claim(claimed, runner_id=runner_id)
                if generation == 2:
                    snapshot = await runtime._graph.aget_state(
                        {"configurable": {"thread_id": run.thread_id}}
                    )
                    assert snapshot.next == ("main",)
                    assert snapshot.values["feedback_batch_id"] == prepare_batches[0]
                    assert len(snapshot.values["messages"]) == 1
                    assert snapshot.values["messages"][0].content == body
                    assert snapshot.values["messages"][0].id == feedback_message_id(
                        prepare_batches[0], receipt.feedback_id
                    )
                    saved_id = snapshot.config["configurable"]["checkpoint_id"]
                    exact = await runtime.get_checkpoint_state(
                        thread_id=run.thread_id, checkpoint_id=saved_id,
                    )
                    assert exact is not None
                    assert exact.graph_operation_id == operation_id
                    assert exact.target_run_id == run.run_id
                    assert exact.checkpoint.next_nodes == ("main",)
                    unconfirmed = await runner.get_frozen_feedback_batch(grant, prepare_batches[0])
                    assert unconfirmed.batch.delivered_checkpoint_id is None
                    assert unconfirmed.members[0].status.value == "claimed"
                if generation == 3:
                    snapshot = await runtime._graph.aget_state(
                        {"configurable": {"thread_id": run.thread_id}}
                    )
                    assert snapshot.next == ("role_exit",)
                    assert snapshot.values["feedback_batch_id"] == prepare_batches[0]
                    assert snapshot.values["main_count"] == 1
                    settled = await runner.get_frozen_feedback_batch(grant, prepare_batches[0])
                    assert settled.members[0].status.value == "delivered"
                if generation in (4, 5):
                    final_snapshot = await runtime._graph.aget_state(
                        {"configurable": {"thread_id": run.thread_id}}
                    )
                    assert final_snapshot.next == ()
                    assert final_snapshot.values["main_count"] == 1
                    assert len(final_snapshot.values["messages"]) == 1
                    assert final_snapshot.values["messages"][0].content == body
                    assert final_snapshot.values["messages"][0].id == feedback_message_id(
                        prepare_batches[0], receipt.feedback_id
                    )
                    if generation == 4:
                        terminal_id = final_snapshot.config["configurable"]["checkpoint_id"]
                        saved_terminal = await runtime.get_checkpoint_state(
                            thread_id=run.thread_id, checkpoint_id=terminal_id,
                        )
                        assert saved_terminal is not None
                        assert saved_terminal.graph_operation_id == operation_id
                        assert saved_terminal.checkpoint.next_nodes == ()
                        with bind_tenant_execution_context(identity):
                            unfinished = await api.get_run(identity.tenant_id, run.run_id)
                        assert unfinished is not None
                        assert unfinished.status is RunStatus.RUNNING
                        async with api_db.session_factory() as session, session.begin():
                            await set_tenant_execution_context(session, identity)
                            delivered_before_complete = await session.get(
                                FeedbackCommandModel, receipt.feedback_id
                            )
                            assert delivered_before_complete is not None
                            assert delivered_before_complete.status == "delivered"
                            assert delivered_before_complete.delivered_checkpoint_id is not None
            finally:
                await runtime.close()
                runtime = None
            if generation < 5:
                await asyncio.sleep(1.2)

        assert prepare_batches == [prepare_batches[0], prepare_batches[0]]
        assert runtime is None
        assert main_calls == ["entry-recovery"]
        with bind_tenant_execution_context(identity):
            completed_run = await api.get_run(identity.tenant_id, run.run_id)
        assert completed_run is not None
        assert completed_run.status is RunStatus.COMPLETED
        assert completed_run.thread_id == run.thread_id
        assert completed_run.writer_generation == 5
        async with api_db.session_factory() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            command = await session.get(FeedbackCommandModel, receipt.feedback_id)
            batch = await session.get(FeedbackDeliveryBatchModel, prepare_batches[0])
            assert command is not None
            assert batch is not None
            assert command.status == "delivered"
            assert command.body == body
            assert command.delivered_checkpoint_id is not None
            assert batch.delivered_checkpoint_id == command.delivered_checkpoint_id
    finally:
        if runtime is not None:
            await runtime.close()
        await runner_db.dispose()
        await api_db.dispose()


@pytest.mark.asyncio
async def test_original_frozen_batch_survives_claim_takeover() -> None:
    api_url, runner_url = _urls()
    api_db = create_database(Settings(database_url=api_url, environment="test"))
    runner_db = create_database(Settings(database_url=runner_url, environment="test"))
    api = PostgresControlPlaneRepository(api_db.session_factory)
    runner = PostgresControlPlaneRepository(runner_db.session_factory)
    identities = PostgresIdentityDirectory(api_db.session_factory)
    service = FeedbackService(
        repository=runner, identity_directory=identities,
        bind_identity=bind_tenant_execution_context,
    )
    try:
        now = datetime.now(timezone.utc)
        subject = AuthenticatedSubject(
            issuer="https://batch3-feedback.example",
            subject=f"person-{uuid4().hex}",
            audience="cad2bim-batch3",
            authenticated_at=now,
            token_expires_at=now + timedelta(hours=1),
        )
        identity = await identities.resolve_authenticated_subject(
            subject, command_id=f"feedback_{uuid4().hex}"
        )
        with bind_tenant_execution_context(identity):
            task = await api.create_task(
                identity.tenant_id, CreateTaskRequest(title="Original feedback batch")
            )
            run = await api.create_run(
                identity.tenant_id, task.task_id,
                StartRunRequest(idempotency_key=f"run-{uuid4().hex}"),
                execution_protocol_factory=_binding,
            )
        first = await _claim_target(
            runner, run.run_id, "batch3-runner-one", lease_seconds=1,
        )
        assert first is not None and first.run_id == run.run_id
        g1 = RunnerCheckpointGrant.from_claim(first, runner_id="batch3-runner-one")
        active = await service.activate_feedback_subject(
            g1, business_entry_id="entry-one", stage_key=first.stage_key,
            agent="requirement", attempt=1, checkpoint_id="root-input",
        )
        bodies = ("First complete feedback body.", "Second complete feedback body.")
        for position, body in enumerate(bodies):
            await service.submit_feedback(
                subject, task.task_id, run.run_id,
                SubmitFeedbackCommand(
                    client_request_id=f"feedback-{position}-{uuid4().hex}",
                    delivery_mode=FeedbackDeliveryMode.STEER,
                    body=body,
                    scope=FeedbackScope(
                        run_id=run.run_id, stage_key=first.stage_key,
                        role_execution_id=active.subject_id,
                    ),
                ),
            )
        mapping = (("", "root-input"),)
        source = CheckpointCursor(
            thread_id=run.thread_id, checkpoint_id="root-input", checkpoint_ns="",
            root_checkpoint_id="root-input", checkpoint_map=mapping,
            checkpoint_digest=checkpoint_cursor_digest(
                checkpoint_id="root-input", checkpoint_ns="", checkpoint_map=mapping,
            ),
        )
        frozen = await service.freeze_for_main(
            g1, stage_key=first.stage_key, role_execution_id=active.subject_id,
            input_checkpoint_cursor=source,
        )
        assert frozen is not None
        assert tuple(item.body for item in frozen.members) == bodies
        assert frozen.batch.claimed_checkpoint_cursor == source
        await asyncio.sleep(1.2)
        with pytest.raises(LeaseConflictError):
            await service.freeze_for_main(
                g1, stage_key=first.stage_key, role_execution_id=active.subject_id,
                input_checkpoint_cursor=source,
            )
        second = await _claim_target(runner, run.run_id, "batch3-runner-two")
        assert second is not None and second.run_id == run.run_id
        g2 = RunnerCheckpointGrant.from_claim(second, runner_id="batch3-runner-two")
        with pytest.raises(LeaseConflictError):
            await service.freeze_for_main(
                g1, stage_key=first.stage_key, role_execution_id=active.subject_id,
                input_checkpoint_cursor=source, delivered_batch_id=frozen.batch.batch_id,
            )
        replay = await service.freeze_for_main(
            g2, stage_key=second.stage_key, role_execution_id=active.subject_id,
            input_checkpoint_cursor=source,
        )
        assert replay is not None
        assert replay.batch == frozen.batch
        assert replay.members == frozen.members
        assert replay.message_ids == frozen.message_ids
        assert await service.freeze_for_main(
            g2, stage_key=second.stage_key, role_execution_id=active.subject_id,
            input_checkpoint_cursor=source, delivered_batch_id=replay.batch.batch_id,
        ) is None
        delivered = await runner.get_frozen_feedback_batch(g2, replay.batch.batch_id)
        assert delivered.batch.delivered_checkpoint_id == source.checkpoint_id
        # Repeated settlement at a later boundary keeps the first audit position.
        later_map = (("", "later-input"),)
        later = CheckpointCursor(
            thread_id=run.thread_id, checkpoint_id="later-input", checkpoint_ns="",
            root_checkpoint_id="later-input", checkpoint_map=later_map,
            checkpoint_digest=checkpoint_cursor_digest(
                checkpoint_id="later-input", checkpoint_ns="", checkpoint_map=later_map,
            ),
        )
        assert await service.freeze_for_main(
            g2, stage_key=second.stage_key, role_execution_id=active.subject_id,
            input_checkpoint_cursor=later, delivered_batch_id=replay.batch.batch_id,
        ) is None
        assert (await runner.get_frozen_feedback_batch(g2, replay.batch.batch_id)) == delivered
        accepted = []
        for position in range(101):
            accepted.append(await service.submit_feedback(
                subject, task.task_id, run.run_id,
                SubmitFeedbackCommand(
                    client_request_id=f"overflow-{position}-{uuid4().hex}",
                    delivery_mode=FeedbackDeliveryMode.STEER,
                    body=f"Same-role feedback {position}.",
                    scope=FeedbackScope(
                        run_id=run.run_id, stage_key=first.stage_key,
                        role_execution_id=active.subject_id,
                    ),
                ),
            ))
        high_watermark = accepted[-1].accepted_sequence
        first_page = await service.freeze_for_main(
            g2, stage_key=second.stage_key, role_execution_id=active.subject_id,
            input_checkpoint_cursor=source,
        )
        assert first_page is not None and len(first_page.members) == 100
        # A saved input boundary can cover more than one bounded batch.
        assert not await service.decide_role_exit(
            g2, active.subject_id, high_watermark, input_checkpoint_cursor=source,
            delivered_batch_id=first_page.batch.batch_id,
        )
        last_page = await service.freeze_for_main(
            g2, stage_key=second.stage_key, role_execution_id=active.subject_id,
            input_checkpoint_cursor=source,
        )
        assert last_page is not None
        assert tuple(item.feedback_id for item in last_page.members) == (accepted[-1].feedback_id,)
        assert await service.decide_role_exit(
            g2, active.subject_id, high_watermark, input_checkpoint_cursor=source,
            delivered_batch_id=last_page.batch.batch_id,
        )
        late = await service.submit_feedback(
            subject, task.task_id, run.run_id,
            SubmitFeedbackCommand(
                client_request_id=f"late-{uuid4().hex}",
                delivery_mode=FeedbackDeliveryMode.STEER,
                body="Accepted before terminal arbitration.",
                scope=FeedbackScope(run_id=run.run_id, stage_key=first.stage_key),
            ),
        )
        with pytest.raises(ConflictError, match="unacknowledged feedback"):
            with bind_tenant_execution_context(g2.scope.identity):
                await runner.complete(run.run_id, g2.runner_id, g2.lease_token)
        with bind_tenant_execution_context(identity):
            requested = await api.cancel_run(identity.tenant_id, run.run_id)
        assert requested.cancel_requested
        with bind_tenant_execution_context(g2.scope.identity):
            terminal = await runner.complete(run.run_id, g2.runner_id, g2.lease_token)
        assert terminal.status is RunStatus.CANCELLED
        async with api_db.session_factory() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            rows = (await session.scalars(
                select(FeedbackCommandModel).where(FeedbackCommandModel.run_id == run.run_id)
            )).all()
            by_id = {item.feedback_id: item for item in rows}
            assert all(by_id[item.feedback_id].status == "delivered" for item in frozen.members)
            assert by_id[late.feedback_id].status == "superseded"
            assert by_id[late.feedback_id].claimed_batch_id is None
            membership = (await session.scalars(
                select(FeedbackDeliveryBatchMemberModel).where(
                    FeedbackDeliveryBatchMemberModel.batch_id == frozen.batch.batch_id
                )
            )).all()
            assert tuple(item.feedback_id for item in membership) == frozen.batch.member_feedback_ids
        with pytest.raises(ConflictError, match="cannot accept"):
            await service.submit_feedback(
                subject, task.task_id, run.run_id,
                SubmitFeedbackCommand(
                    client_request_id=f"after-terminal-{uuid4().hex}",
                    delivery_mode=FeedbackDeliveryMode.STEER,
                    body="Must be rejected after cancellation.",
                    scope=FeedbackScope(run_id=run.run_id, stage_key=first.stage_key),
                ),
            )
    finally:
        await runner_db.dispose()
        await api_db.dispose()
