from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from CAD2BIM_backend.application.feedback_service import (
    FeedbackService,
    delivery_update,
)
from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.control_plane import (
    ExecutionMode,
    FeedbackCommand,
    FeedbackCommandStatus,
    FeedbackDeliveryBatch,
    FeedbackDeliveryMode,
    FeedbackScope,
    FeedbackWakeResumePlan,
    FrozenFeedbackBatch,
    SubmitFeedbackCommand,
    TerminalGateReentryPlan,
    feedback_message_id,
)
from CAD2BIM_backend.domain.identity import (
    AuthenticatedSubject,
    TenantActorKind,
    TenantExecutionContext,
)
from CAD2BIM_backend.domain.feedback_target import FeedbackTargetConflictError
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant, RunnerGraphContext
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest
from CAD2BIM_backend.infrastructure.memory.identity_directory import InMemoryIdentityDirectory
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    bind_tenant_execution_context,
    current_tenant_execution_context,
)


NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def _grant(generation: int = 1) -> RunnerCheckpointGrant:
    identity = TenantExecutionContext(
        tenant_id="tenant-1",
        principal_id="runner-1",
        task_id="task-1",
        actor_kind=TenantActorKind.RUNNER,
        command_id=f"run:run-1:generation:{generation}",
    )
    scope = InvocationScope(
        identity=identity,
        owner_id="tenant-1",
        run_id="run-1",
        execution_mode=ExecutionMode.GRAPH,
        catalog_frontier=None,
    )
    return RunnerCheckpointGrant(
        graph_context=RunnerGraphContext(scope=scope),
        thread_id="thread-run-1",
        runner_id="runner-1",
        lease_token=f"lease-{generation}",
        writer_generation=generation,
    )


def _cursor(namespace: str = "") -> CheckpointCursor:
    checkpoint_id = "child-input" if namespace else "root-input"
    mapping = (("", "root-input"), (namespace, checkpoint_id)) if namespace else (("", "root-input"),)
    return CheckpointCursor(
        thread_id="thread-run-1",
        checkpoint_id=checkpoint_id,
        checkpoint_ns=namespace,
        root_checkpoint_id="root-input",
        checkpoint_map=mapping,
        checkpoint_digest=checkpoint_cursor_digest(
            checkpoint_id=checkpoint_id, checkpoint_ns=namespace, checkpoint_map=mapping
        ),
    )


def _frozen(namespace: str = "") -> FrozenFeedbackBatch:
    scope = FeedbackScope(run_id="run-1", stage_key="stage-1")
    member = FeedbackCommand(
        feedback_id="feedback-1",
        accepted_sequence=1,
        owner_id="tenant-1",
        author_principal_id="user-1",
        task_id="task-1",
        run_id="run-1",
        client_request_id="request-1",
        request_fingerprint="a" * 64,
        delivery_mode=FeedbackDeliveryMode.STEER,
        content=({"type": "text", "text": "Complete original user feedback, without truncation."},),
        resolved_scope=scope,
        status=FeedbackCommandStatus.CLAIMED,
        claimed_batch_id="batch-1",
        created_at=NOW,
    )
    batch = FeedbackDeliveryBatch(
        batch_id="batch-1",
        owner_id="tenant-1",
        run_id="run-1",
        target_fingerprint="b" * 64,
        claim_high_watermark=1,
        member_feedback_ids=(member.feedback_id,),
        contains_interrupt=False,
        claimed_writer_generation=1,
        claimed_checkpoint_id="child-input" if namespace else "root-input",
        claimed_checkpoint_cursor=_cursor(namespace),
        created_at=NOW,
    )
    return FrozenFeedbackBatch(
        batch=batch,
        members=(member,),
        message_ids=(feedback_message_id(batch.batch_id, member.feedback_id),),
    )


def test_frozen_batch_rejects_member_above_claim_high_watermark() -> None:
    raw = _frozen().model_dump(mode="json")
    raw["members"][0]["accepted_sequence"] = 2
    with pytest.raises(ValidationError, match="frozen batch"):
        FrozenFeedbackBatch.model_validate(raw)


def _terminal_reentry_plan() -> TerminalGateReentryPlan:
    gate_plan = FeedbackWakeResumePlan(
        interrupt_id="interrupt-1",
        source_checkpoint_cursor=_cursor(),
        operation_id="gate-operation-1",
        business_entry_id="entry-1",
        accepted_root_checkpoint_id="root-input",
        frozen_batch=_frozen(),
    )
    return TerminalGateReentryPlan(
        source_checkpoint_id="terminal-root-1",
        operation_id="terminal-reentry-1",
        gate_plan=gate_plan,
    )


def test_terminal_reentry_plan_round_trips_the_original_gate_selection() -> None:
    plan = _terminal_reentry_plan()
    restored = TerminalGateReentryPlan.model_validate(plan.model_dump(mode="json"))
    assert restored == plan
    assert restored.gate_plan.frozen_batch.batch.batch_id == "batch-1"
    assert restored.gate_plan.source_checkpoint_cursor.checkpoint_id == "root-input"
    with pytest.raises(ValidationError):
        TerminalGateReentryPlan.model_validate(
            {**plan.model_dump(mode="json"), "source_checkpoint_id": ""}
        )


@pytest.mark.asyncio
async def test_explicit_other_run_feedback_target_is_a_conflict() -> None:
    service = FeedbackService(
        repository=object(),
        identity_directory=InMemoryIdentityDirectory(),
        bind_identity=bind_tenant_execution_context,
    )
    subject = AuthenticatedSubject(
        issuer="https://identity.example.test",
        subject="alice",
        audience="cad2bim-api",
        authenticated_at=NOW,
        token_expires_at=NOW + timedelta(hours=1),
    )
    command = SubmitFeedbackCommand(
        client_request_id="request-other-run",
        delivery_mode=FeedbackDeliveryMode.STEER,
        content=({"type": "text", "text": "Do not redirect this text"},),
        scope=FeedbackScope(run_id="run-other"),
    )
    with pytest.raises(FeedbackTargetConflictError, match="another Run"):
        await service.submit_feedback(subject, "task-1", "run-1", command)


@pytest.mark.asyncio
async def test_runner_gate_methods_forward_the_private_grant_under_identity_binding() -> None:
    calls = []
    marker = object()

    class _GateRepo:
        async def open_gate(self, grant, request):
            assert current_tenant_execution_context() == grant.scope.identity
            calls.append(("open", grant, request))
            return marker

        async def park_waiting_user(self, grant, interrupt_id):
            assert current_tenant_execution_context() == grant.scope.identity
            calls.append(("park", grant, interrupt_id))
            return marker

        async def load_gate_resume(self, grant):
            assert current_tenant_execution_context() == grant.scope.identity
            calls.append(("load", grant))
            return None

        async def load_terminal_reentry(self, grant):
            assert current_tenant_execution_context() == grant.scope.identity
            calls.append(("load_terminal", grant))
            return marker

        async def select_terminal_reentry(self, grant, *, source_checkpoint_id):
            assert current_tenant_execution_context() == grant.scope.identity
            calls.append(("select_terminal", grant, source_checkpoint_id))
            return marker

    service = FeedbackService(
        repository=_GateRepo(),
        identity_directory=InMemoryIdentityDirectory(),
        bind_identity=bind_tenant_execution_context,
    )
    grant = _grant()
    request = object()
    assert await service.open_gate(grant, request) is marker
    assert await service.park_waiting_user(grant, "interrupt-1") is marker
    assert await service.load_gate_resume(grant) is None
    assert await service.load_terminal_reentry(grant) is marker
    assert await service.select_terminal_reentry(
        grant, source_checkpoint_id="terminal-root-1"
    ) is marker
    assert calls == [
        ("open", grant, request),
        ("park", grant, "interrupt-1"),
        ("load", grant),
        ("load_terminal", grant),
        ("select_terminal", grant, "terminal-root-1"),
    ]
    assert current_tenant_execution_context() is None


@pytest.mark.asyncio
async def test_current_gate_read_uses_the_authenticated_task_identity() -> None:
    class _GateRepo:
        async def get_pending_gate(self, identity, run_id):
            assert identity.actor_kind is TenantActorKind.USER
            assert identity.task_id == "task-1"
            assert current_tenant_execution_context() == identity
            assert run_id == "run-1"
            return None

    service = FeedbackService(
        repository=_GateRepo(),
        identity_directory=InMemoryIdentityDirectory(),
        bind_identity=bind_tenant_execution_context,
    )
    subject = AuthenticatedSubject(
        issuer="https://identity.example.test",
        subject="alice",
        audience="cad2bim-api",
        authenticated_at=NOW,
        token_expires_at=NOW + timedelta(hours=1),
    )
    assert await service.current_gate(subject, "task-1", "run-1") is None
    assert current_tenant_execution_context() is None


def test_public_scope_and_gate_command_reject_internal_write_authority() -> None:
    from pydantic import ValidationError
    from CAD2BIM_backend.domain.control_plane import RespondGateCommand

    with pytest.raises(ValidationError):
        FeedbackScope.model_validate({"run_id": "run-1", "expected_writer_generation": 1})
    with pytest.raises(ValidationError):
        RespondGateCommand.model_validate({
            "interrupt_id": "interrupt-1", "action": "approve",
            "idempotency_key": "once", "checkpoint_cursor": _cursor().model_dump(),
        })


def test_delivery_update_preserves_existing_memory_and_deduplicates_replay() -> None:
    frozen = _frozen()
    existing = HumanMessage(id="earlier", content="Earlier role memory")
    update = delivery_update({"messages": [existing]}, (frozen,))
    assert [message.id for message in update["messages"]] == ["earlier", *frozen.message_ids]
    replay = delivery_update(update, (frozen,))
    assert [message.id for message in replay["messages"]] == ["earlier", *frozen.message_ids]


def test_delivery_contains_only_messages_and_batch_identity() -> None:
    frozen = _frozen()
    update = delivery_update({}, (frozen,))
    assert set(update) == {"messages", "feedback_batch_ids"}
    assert update["feedback_batch_ids"] == (frozen.batch.batch_id,)
    assert all(isinstance(message, HumanMessage) for message in update["messages"])
    assert [message.content for message in update["messages"]] == [
        [block.model_dump(mode="json") for block in member.content] for member in frozen.members
    ]


def test_feedback_command_dto_retains_claimed_batch_in_superseded_status() -> None:
    member = _frozen().members[0]
    with pytest.raises(ValidationError):
        FeedbackCommand.model_validate(
            {**member.model_dump(), "status": "applied"}
        )
    superseded = member.model_copy(update={"status": FeedbackCommandStatus.SUPERSEDED})
    assert FeedbackCommand.model_validate(superseded.model_dump()).claimed_batch_id == "batch-1"
    with pytest.raises(ValidationError, match="requires batch and checkpoint"):
        FeedbackCommand.model_validate(
            {**member.model_dump(), "status": "delivered"}
        )


def test_delivery_combines_old_batches_and_new_input_in_acceptance_order_without_100_cap() -> None:
    original = _frozen()
    members = tuple(original.members[0].model_copy(update={
        "feedback_id": f"feedback-{index}", "accepted_sequence": index,
        "claimed_batch_id": "later-batch", "content": original.members[0].content,
    }) for index in range(2, 107))
    batch = original.batch.model_copy(update={"batch_id": "later-batch", "claim_high_watermark": 106,
        "member_feedback_ids": tuple(member.feedback_id for member in members)})
    later = FrozenFeedbackBatch(batch=batch, members=members,
        message_ids=tuple(feedback_message_id(batch.batch_id, member.feedback_id) for member in members))
    update = delivery_update({}, (later, original), initial_feedback_id=original.members[0].feedback_id)
    assert len(update["messages"]) == 106
    assert [message.additional_kwargs["feedback_id"] for message in update["messages"]] == [f"feedback-{index}" for index in range(1, 107)]
    assert update["messages"][0].additional_kwargs["input_kind"] == "initial_input"
    assert all(message.additional_kwargs["input_kind"] == "gate_input" for message in update["messages"][1:])
    assert len(delivery_update(update, (original, later))["messages"]) == 106


@pytest.mark.asyncio
async def test_image_only_input_uses_exact_artifact_bytes_and_atomic_initial_marker() -> None:
    from types import SimpleNamespace
    from CAD2BIM_backend.domain.control_plane import FeedbackCommandReceipt, UserArtifactContent
    frozen = _frozen()
    member = frozen.members[0].model_copy(update={"content": (UserArtifactContent(type="image", artifact_id="artifact-1", version_id="version-2"),)})
    frozen = FrozenFeedbackBatch(batch=frozen.batch, members=(member,), message_ids=frozen.message_ids)
    class Repository:
        async def get_run(self, owner_id, run_id):
            return SimpleNamespace(task_id="task-1", initial_feedback=FeedbackCommandReceipt(
                feedback_id=member.feedback_id, accepted_sequence=1, delivery_mode=member.delivery_mode,
                resolved_scope=member.resolved_scope, status=member.status))
    class Artifacts:
        async def download_version(self, owner_id, artifact_id, version_id):
            assert (owner_id, artifact_id, version_id) == ("tenant-1", "artifact-1", "version-2")
            return SimpleNamespace(content=b"exact-image", artifact=SimpleNamespace(
                task_id="task-1", content_type="image/png", filename="input.png"))
    service = FeedbackService(repository=Repository(), identity_directory=InMemoryIdentityDirectory(),
                              bind_identity=bind_tenant_execution_context, artifacts=Artifacts())
    update = await service.delivery_update(_grant(), {}, (frozen,))
    message = update["messages"][0]
    assert message.content == [{"type": "image_url", "image_url": {"url": "data:image/png;base64,ZXhhY3QtaW1hZ2U="}}]
    assert message.additional_kwargs["input_kind"] == "initial_input"
    assert message.additional_kwargs["input_content"] == [{"type": "image", "artifact_id": "artifact-1", "version_id": "version-2"}]
