from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from CAD2BIM_backend.application.task_run_service import TaskRunService
from CAD2BIM_backend.domain.control_plane import (
    CreateTaskRequest,
    ResourceNotFoundError,
    RunRecord,
    RunStatus,
    StartRunRequest,
    TaskStatus,
    ConflictError,
    SubmitFeedbackCommand,
    FeedbackDeliveryMode,
    FeedbackScope,
)
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest
from CAD2BIM_backend.domain.state_read_binding import (
    StateReadBindingError,
    make_completion_checkpoint_event,
)
from CAD2BIM_backend.domain.execution_protocol import (
    ExecutionProtocolBinding,
    ExecutionProtocolMismatchError,
)
from CAD2BIM_backend.domain.identity import AuthenticatedSubject
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.infrastructure.memory.control_plane_repository import (
    InMemoryControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.memory.identity_directory import InMemoryIdentityDirectory
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    bind_tenant_execution_context,
    current_tenant_execution_context,
)
from CAD2BIM_backend.ports.graph_runtime import GraphCheckpointDescriptor, GraphCheckpointState
from CAD2BIM_backend.domain.data_runtime import CriticalOperationRecord, CriticalOperationStatus


def _subject(name: str) -> AuthenticatedSubject:
    now = datetime.now(timezone.utc)
    return AuthenticatedSubject(
        issuer="https://identity.example.test",
        subject=name,
        audience="cad2bim-api",
        authenticated_at=now,
        token_expires_at=now + timedelta(minutes=5),
    )


def _binding(module: str, stage_key: str) -> ExecutionProtocolBinding:
    return ExecutionProtocolBinding(bundle_id="unit_bundle", module=module, stage_key=stage_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True])
async def test_task_deletion_retires_all_entries_and_cancels_inactive_runs_without_erasing_assets(paused) -> None:
    repository = InMemoryControlPlaneRepository()
    service = TaskRunService(repository=repository, identity_directory=InMemoryIdentityDirectory(),
        execution_protocol_factory=_binding, bind_identity=bind_tenant_execution_context)
    subject = _subject("delete-owner")
    request = CreateTaskRequest(title="Temporary task", idempotency_key="temporary-delete")
    task = await service.create_task(subject, request)
    run = await service.start_run(subject, task.task_id, StartRunRequest(
        module="component", stage_key="component", idempotency_key="temporary-run",
        content=({"type": "text", "text": "Temporary deletion test"},)))
    artifact = await repository.register_artifact(task.owner_id, task.task_id,
        artifact_id="temporary-asset", version_no=1, filename="evidence.txt", content_type="text/plain",
        size_bytes=4, sha256="a" * 64, idempotency_key="temporary-asset")
    if paused:
        await service.pause_run(subject, task.task_id, run.run_id, "pause-temporary")
    await service.delete_task(subject, task.task_id)
    await service.delete_task(subject, task.task_id)
    assert repository._tasks[task.task_id].status is TaskStatus.ARCHIVED
    assert repository._runs[run.run_id].status is RunStatus.CANCELLED
    assert repository._runs[run.run_id].cancel_requested
    assert not repository._runs[run.run_id].feedback_pending
    assert repository._feedback[run.initial_feedback.feedback_id].status.value == "superseded"
    assert await repository.get_artifact(task.owner_id, artifact.artifact_id) == artifact
    assert await service.list_tasks(subject) == ()
    assert await repository.claim_next_run("deleted-task-runner") is None
    for entry in (
        service.get_task(subject, task.task_id),
        service.get_run(subject, task.task_id, run.run_id),
        service.authorized_task_context(subject, task.task_id),
        service.authorized_run_context(subject, task.task_id, run.run_id),
        service.chat_snapshot_basis(subject, task.task_id, run.run_id),
        service.continue_run(subject, task.task_id, run.run_id, "continue-deleted"),
        service.start_run(subject, task.task_id, StartRunRequest(idempotency_key="new-deleted-run")),
    ):
        with pytest.raises(ResourceNotFoundError):
            await entry
    with pytest.raises(ConflictError, match="deleted"):
        await service.create_task(subject, request)


@pytest.mark.asyncio
async def test_running_task_deletion_requires_committed_pause_and_never_requests_it_implicitly() -> None:
    repository = InMemoryControlPlaneRepository()
    service = TaskRunService(repository=repository, identity_directory=InMemoryIdentityDirectory(),
        execution_protocol_factory=_binding, bind_identity=bind_tenant_execution_context)
    subject = _subject("running-delete-owner")
    task = await service.create_task(subject, CreateTaskRequest(title="Temporary running task"))
    run = await service.start_run(subject, task.task_id, StartRunRequest(idempotency_key="running-delete"))
    claimed = await repository.claim_next_run("running-delete-runner", lease_seconds=30)
    with pytest.raises(ConflictError, match="Pause"):
        await service.delete_task(subject, task.task_id)
    assert repository._runs[run.run_id] == claimed
    await service.pause_run(subject, task.task_id, run.run_id, "explicit-pause")
    with pytest.raises(ConflictError, match="Pause"):
        await service.delete_task(subject, task.task_id)
    await repository.pause_claimed(run.run_id, "running-delete-runner", claimed.lease_token)
    await service.delete_task(subject, task.task_id)
    assert repository._runs[run.run_id].status is RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_task_deletion_rejects_foreign_scope_and_unresolved_write_without_mutation() -> None:
    repository = InMemoryControlPlaneRepository()
    service = TaskRunService(repository=repository, identity_directory=InMemoryIdentityDirectory(),
        execution_protocol_factory=_binding, bind_identity=bind_tenant_execution_context)
    subject = _subject("write-delete-owner")
    task = await service.create_task(subject, CreateTaskRequest(title="Temporary unresolved task"))
    run = await service.start_run(subject, task.task_id, StartRunRequest(idempotency_key="unresolved-delete"))
    operation = CriticalOperationRecord(operation_id="temporary-unresolved-write", owner_id=task.owner_id,
        tenant_id=task.owner_id, task_id=task.task_id, run_id=run.run_id, tool_name="cad_import_source",
        source_filename="temporary-input.dxf",
        idempotency_key="temporary-unresolved", request_fingerprint="b" * 64,
        status=CriticalOperationStatus.PREPARED, started_at=run.created_at)
    repository._state.critical_operations[operation.operation_id] = operation
    before = repository._tasks[task.task_id], repository._runs[run.run_id]
    with pytest.raises(ResourceNotFoundError):
        await service.delete_task(_subject("foreign-delete-owner"), task.task_id)
    with pytest.raises(ConflictError, match="unresolved write"):
        await service.delete_task(subject, task.task_id)
    assert (repository._tasks[task.task_id], repository._runs[run.run_id]) == before
    assert repository._state.critical_operations[operation.operation_id] == operation


@pytest.mark.asyncio
async def test_public_task_metadata_cannot_forge_generated_title_state():
    from types import SimpleNamespace
    import httpx
    from CAD2BIM_backend.api import create_task_api

    service = TaskRunService(repository=InMemoryControlPlaneRepository(),
        identity_directory=InMemoryIdentityDirectory(), execution_protocol_factory=_binding,
        bind_identity=bind_tenant_execution_context)
    subject = _subject("title-marker-owner")
    app = create_task_api(service=service,
        authenticator=SimpleNamespace(authenticate=lambda _bearer: subject))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test",
        headers={"authorization": "Bearer unit"}) as client:
        forged = await client.post("/tasks", json={"title": "伪造已生成状态",
            "metadata": {"task_type": "component", "component_title_generated": True}})
        assert forged.status_code == 403 and forged.json() == {"detail": "access denied"}
        assert await service.list_tasks(subject) == ()
        normal = await client.post("/tasks", json={"title": "新建 Component",
            "metadata": {"task_type": "component"}})
        assert normal.status_code == 201
        saved = await service.get_task(subject, normal.json()["task_id"])
        assert "component_title_generated" not in saved.metadata


@pytest.mark.asyncio
async def test_generated_component_title_is_task_owned_atomic_and_replayable_with_current_claim():
    from dataclasses import replace
    from CAD2BIM_backend.domain.control_plane import LeaseConflictError

    repository = InMemoryControlPlaneRepository()
    service = TaskRunService(repository=repository, identity_directory=InMemoryIdentityDirectory(),
        execution_protocol_factory=_binding, bind_identity=bind_tenant_execution_context)
    subject = _subject("title-owner")
    task = await service.create_task(subject, CreateTaskRequest(title="Component", active_module="component",
        metadata={"task_type": "component", "keep": "original metadata"}))
    run = await service.start_run(subject, task.task_id, StartRunRequest(module="component", stage_key="component",
        idempotency_key="title-first", content=({"type": "text", "text": "检查圆环尺寸"},)))
    claimed = await repository.claim_next_run("title-runner", lease_seconds=30)
    grant = RunnerCheckpointGrant.from_claim(claimed, runner_id="title-runner")
    assert await service.generated_task_title(grant) is None
    with pytest.raises(LeaseConflictError):
        await service.set_generated_task_title(replace(grant, writer_generation=grant.writer_generation + 1), "未获授权的名称")
    before = await service.get_task(subject, task.task_id)
    assert before.title == "Component" and "component_title_generated" not in before.metadata
    saved = await service.set_generated_task_title(grant, "圆环构件尺寸核验")
    assert saved.title == "圆环构件尺寸核验"
    assert saved.metadata == {**task.metadata, "component_title_generated": True}
    assert await service.generated_task_title(grant) == saved.title
    replay = await service.set_generated_task_title(grant, "重试不能覆盖已生成名称")
    assert replay == saved
    events = await service.list_events(subject, task.task_id, run.run_id)
    title_events = [event for event in events if event.event_type == "task.title.updated"]
    assert len(title_events) == 1 and title_events[0].item_id == f"task-title:{task.task_id}"
    assert (await service.get_task(subject, task.task_id)).title == saved.title
    assert (await service.list_tasks(subject))[0][0].title == saved.title
    with pytest.raises(ResourceNotFoundError):
        await service.get_task(_subject("another-owner"), task.task_id)


@pytest.mark.asyncio
async def test_component_start_atomically_accepts_first_input_and_replays_original_receipt() -> None:
    repository = InMemoryControlPlaneRepository()
    service = TaskRunService(repository=repository, identity_directory=InMemoryIdentityDirectory(),
        execution_protocol_factory=_binding, bind_identity=bind_tenant_execution_context)
    subject = _subject("alice")
    task = await service.create_task(subject, CreateTaskRequest(title="Component", active_module="component"))
    with pytest.raises(ResourceNotFoundError):
        await service.start_run(subject, task.task_id, StartRunRequest(module="component", stage_key="component",
            idempotency_key="bad-image", content=({"type": "image", "artifact_id": "missing", "version_id": "missing"},)))
    assert next(entry for entry in await service.list_tasks(subject) if entry[0].task_id == task.task_id)[1] is None
    text = "真实用户要求" * 1500
    request = StartRunRequest(module="component", stage_key="component", idempotency_key="first",
                              content=({"type": "text", "text": text},))
    run = await service.start_run(subject, task.task_id, request)
    assert run.initial_feedback is not None
    replay = await service.start_run(subject, task.task_id, request)
    assert replay.run_id == run.run_id
    assert replay.initial_feedback == run.initial_feedback
    saved = await repository.get_feedback(run.owner_id, run.run_id, run.initial_feedback.feedback_id)
    assert saved.content[0].text == text
    assert not run.input_payload and task.instruction is None
    snapshot = await service.chat_snapshot_basis(subject, task.task_id, run.run_id)
    assert snapshot[2] == 1 and snapshot[1][0].event_type == "feedback.accepted"
    with pytest.raises(ConflictError):
        await service.start_run(subject, task.task_id, request.model_copy(update={
            "content": StartRunRequest(content=({"type": "text", "text": "changed"},), idempotency_key="other").content}))
    assert len(await repository.list_feedback(run.owner_id, run.run_id)) == 1


@pytest.mark.asyncio
async def test_paused_input_is_saved_until_explicit_continue_on_the_original_run() -> None:
    repository = InMemoryControlPlaneRepository()
    service = TaskRunService(repository=repository, identity_directory=InMemoryIdentityDirectory(),
        execution_protocol_factory=_binding, bind_identity=bind_tenant_execution_context)
    subject = _subject("alice")
    task = await service.create_task(subject, CreateTaskRequest(title="Paused"))
    run = await service.start_run(subject, task.task_id, StartRunRequest(idempotency_key="run"))
    paused = await service.pause_run(subject, task.task_id, run.run_id, "pause")
    assert paused.status is RunStatus.PAUSED
    pause_events = await service.list_events(subject, task.task_id, run.run_id)
    assert [event.attributes for event in pause_events] == [{
        "run_status": "paused", "pause_requested": "false", "reason": "paused",
    }]
    identity, _ = await service.authorized_run_context(subject, task.task_id, run.run_id)
    with bind_tenant_execution_context(identity):
        receipt = await repository.accept_feedback(identity, run.run_id, SubmitFeedbackCommand(
            client_request_id="during-pause", delivery_mode=FeedbackDeliveryMode.STEER,
            content=({"type": "text", "text": "继续时纳入"},), scope=FeedbackScope(run_id=run.run_id)))
    assert (await service.get_run(subject, task.task_id, run.run_id)).status is RunStatus.PAUSED
    continued = await service.continue_run(subject, task.task_id, run.run_id, "continue")
    assert continued.run_id == run.run_id and continued.status is RunStatus.QUEUED
    assert (await service.pause_run(subject, task.task_id, run.run_id, "pause")).status is RunStatus.QUEUED
    assert (await repository.get_feedback(run.owner_id, run.run_id, receipt.feedback_id)).accepted_sequence == 1
    events = await service.list_events(subject, task.task_id, run.run_id)
    assert [event.event_type for event in events] == [
        "run.status.changed", "feedback.accepted", "run.status.changed",
    ]
    assert events[-1].attributes == {
        "run_status": "queued", "pause_requested": "false", "reason": "continued",
    }
    assert await service.continue_run(subject, task.task_id, run.run_id, "continue") == continued
    assert await service.list_events(subject, task.task_id, run.run_id) == events


@pytest.mark.asyncio
async def test_model_failure_pause_releases_claim_and_resume_keeps_original_run_and_feedback():
    repository = InMemoryControlPlaneRepository()
    service = TaskRunService(repository=repository, identity_directory=InMemoryIdentityDirectory(),
        execution_protocol_factory=_binding, bind_identity=bind_tenant_execution_context)
    subject = _subject("model-retry")
    task = await service.create_task(subject, CreateTaskRequest(title="Retry"))
    run = await service.start_run(subject, task.task_id, StartRunRequest(
        module="component", stage_key="component", idempotency_key="original-run",
        content=({"type": "text", "text": "Keep the original task"},)))
    claimed = await repository.claim_next_run("retry-runner")
    paused = await repository.pause_claimed(run.run_id, "retry-runner", claimed.lease_token,
        error="model_network_interrupted")
    assert paused.status is RunStatus.PAUSED and paused.claimed_by is None
    events = await service.list_events(subject, task.task_id, run.run_id)
    assert events[-1].attributes["reason"] == "model_network_interrupted"
    feedback_before = await repository.list_feedback(run.owner_id, run.run_id)
    continued = await service.continue_run(subject, task.task_id, run.run_id, "continue-original")
    assert continued.status is RunStatus.QUEUED and continued.error is None
    assert (continued.run_id, continued.thread_id) == (run.run_id, run.thread_id)
    assert await repository.list_feedback(run.owner_id, run.run_id) == feedback_before
    assert await service.continue_run(subject, task.task_id, run.run_id, "continue-original") == continued


@pytest.mark.asyncio
async def test_running_pause_request_and_claim_publish_only_committed_public_changes() -> None:
    repository = InMemoryControlPlaneRepository()
    service = TaskRunService(repository=repository, identity_directory=InMemoryIdentityDirectory(),
        execution_protocol_factory=_binding, bind_identity=bind_tenant_execution_context)
    subject = _subject("alice")
    task = await service.create_task(subject, CreateTaskRequest(title="Run lifecycle"))
    run = await service.start_run(subject, task.task_id, StartRunRequest(idempotency_key="lifecycle"))
    claimed = await repository.claim_next_run("runner", lease_seconds=30)
    assert claimed.run_id == run.run_id and claimed.status is RunStatus.RUNNING
    requested = await service.pause_run(subject, task.task_id, run.run_id, "pause-running")
    assert requested.status is RunStatus.RUNNING and requested.pause_requested
    before_replay = await service.list_events(subject, task.task_id, run.run_id)
    await service.pause_run(subject, task.task_id, run.run_id, "pause-running")
    assert await service.list_events(subject, task.task_id, run.run_id) == before_replay
    await repository.pause_claimed(run.run_id, "runner", claimed.lease_token)
    await service.continue_run(subject, task.task_id, run.run_id, "continue-running")
    resumed = await repository.claim_next_run("runner", lease_seconds=30)
    events = await service.list_events(subject, task.task_id, run.run_id)
    assert [event.attributes for event in events] == [
        {"run_status": "running", "pause_requested": "false", "reason": "claimed"},
        {"run_status": "running", "pause_requested": "true", "reason": "pause_requested"},
        {"run_status": "paused", "pause_requested": "false", "reason": "paused"},
        {"run_status": "queued", "pause_requested": "false", "reason": "continued"},
        {"run_status": "running", "pause_requested": "false", "reason": "claimed"},
    ]
    assert all(event.event_type == "run.status.changed" and event.payload_ref is None for event in events)
    assert len({event.event_id for event in events}) == len(events)
    # An expired running lease changes its claim, without inventing a status transition.
    repository._runs[run.run_id] = resumed.model_copy(update={"lease_expires_at": resumed.updated_at - timedelta(seconds=1)})
    reclaimed = await repository.claim_next_run("runner-recovery", lease_seconds=30)
    assert reclaimed.reclaimed_lease and reclaimed.writer_generation == resumed.writer_generation + 1
    assert await service.list_events(subject, task.task_id, run.run_id) == events
    deferred = await repository.defer_claimed(
        run.run_id, "runner-recovery", reclaimed.lease_token,
        expected_writer_generation=reclaimed.writer_generation,
        expected_recovery_generation=reclaimed.recovery_generation,
        reason_code="mcp_restore_transient", delay_seconds=0.01,
    )
    repository._runs[run.run_id] = deferred.model_copy(update={"claim_not_before": deferred.updated_at})
    deferred_claim = await repository.claim_next_run("runner-restored", lease_seconds=30)
    assert deferred_claim.reclaimed_lease and deferred_claim.restore_defer_count > 0
    after_defer = await service.list_events(subject, task.task_id, run.run_id)
    assert len(after_defer) == len(events) + 1
    assert after_defer[-1].attributes == {
        "run_status": "running", "pause_requested": "false", "reason": "claimed",
    }


@pytest.mark.asyncio
async def test_feedback_wake_publishes_queued_once_after_saved_waiting_run() -> None:
    repository = InMemoryControlPlaneRepository()
    service = TaskRunService(repository=repository, identity_directory=InMemoryIdentityDirectory(),
        execution_protocol_factory=_binding, bind_identity=bind_tenant_execution_context)
    subject = _subject("alice")
    task = await service.create_task(subject, CreateTaskRequest(title="Gate wake"))
    run = await service.start_run(subject, task.task_id, StartRunRequest(idempotency_key="waiting"))
    repository._runs[run.run_id] = run.model_copy(update={"status": RunStatus.WAITING_USER})
    identity, _ = await service.authorized_run_context(subject, task.task_id, run.run_id)
    command = SubmitFeedbackCommand(client_request_id="wake", delivery_mode=FeedbackDeliveryMode.STEER,
        content=({"type": "text", "text": "继续调查"},), scope=FeedbackScope(run_id=run.run_id))
    with bind_tenant_execution_context(identity):
        first = await repository.accept_feedback(identity, run.run_id, command)
        assert await repository.accept_feedback(identity, run.run_id, command) == first
    events = await service.list_events(subject, task.task_id, run.run_id)
    assert [event.event_type for event in events] == ["feedback.accepted", "run.status.changed"]
    assert events[-1].attributes == {
        "run_status": "queued", "pause_requested": "false", "reason": "feedback_wake",
    }


@pytest.mark.asyncio
async def test_public_snapshot_keeps_first_checkpoint_source_and_global_event_boundary() -> None:
    from CAD2BIM_backend.domain.run_events import RunEvent, RunEventAudience, RunEventScope, RunEventType
    repository = InMemoryControlPlaneRepository()
    service = TaskRunService(repository=repository, identity_directory=InMemoryIdentityDirectory(),
        execution_protocol_factory=_binding, bind_identity=bind_tenant_execution_context)
    subject = _subject("alice")
    task = await service.create_task(subject, CreateTaskRequest(title="Saved public history"))
    run = await service.start_run(subject, task.task_id, StartRunRequest(idempotency_key="history"))
    event = RunEvent(event_id="event-chat-message", sequence=1, task_id=task.task_id, run_id=run.run_id,
        event_type="chat.message.saved", occurred_at=run.created_at,
        scope=RunEventScope(module="workflow"), item_id="message-original", status=RunEventType.SUCCEEDED,
        payload_ref="chat-checkpoint:original-child", attributes={"checkpoint_ns": "child:original",
            "message_id": "native-message", "message_index": "4", "source_kind": "message"})
    first = await repository.append_chat_events(run.owner_id, run.run_id, (event,))
    parent_copy = event.model_copy(update={"payload_ref": "chat-checkpoint:parent-copy",
        "occurred_at": run.created_at + timedelta(seconds=1), "attributes": {
            "checkpoint_ns": "", "message_id": "native-message", "message_index": "2",
            "memory_slot": "component:", "source_kind": "message"}})
    replay = await repository.append_chat_events(run.owner_id, run.run_id, (parent_copy,))
    assert replay == first
    internal = event.model_copy(update={"event_id": "event-internal", "item_id": "internal-detail",
        "audience": RunEventAudience.INTERNAL, "event_type": "runtime.observed"})
    await repository.append_events(run.owner_id, run.run_id, (internal,))
    snapshot = await service.chat_snapshot_basis(subject, task.task_id, run.run_id)
    assert snapshot[1] == first and snapshot[2] == 2
    assert await service.list_events(subject, task.task_id, run.run_id) == first
    with pytest.raises(ConflictError, match="different content"):
        await repository.append_chat_events(run.owner_id, run.run_id, (event.model_copy(update={"summary": "conflicting message"}),))
    with pytest.raises(ConflictError, match="different content"):
        await repository.append_chat_events(run.owner_id, run.run_id, (parent_copy.model_copy(update={
            "attributes": {**parent_copy.attributes, "message_id": "different-message"}}),))


class _ObservingRepository(InMemoryControlPlaneRepository):
    def __init__(self) -> None:
        super().__init__()
        self.contexts = []

    async def create_task(self, owner_id, request):
        self.contexts.append(current_tenant_execution_context())
        return await super().create_task(owner_id, request)

    async def create_run(self, owner_id, task_id, request, **kwargs):
        self.contexts.append(current_tenant_execution_context())
        return await super().create_run(owner_id, task_id, request, **kwargs)


@pytest.mark.asyncio
async def test_verified_subject_uses_identity_directory_and_server_protocol() -> None:
    repository = _ObservingRepository()
    service = TaskRunService(
        repository=repository,
        identity_directory=InMemoryIdentityDirectory(),
        execution_protocol_factory=_binding,
        bind_identity=bind_tenant_execution_context,
    )
    alice = _subject("alice")

    task = await service.create_task(
        alice, CreateTaskRequest(title="Requirements", idempotency_key="task-one")
    )
    run = await service.start_run(alice, task.task_id, StartRunRequest(idempotency_key="run-one"))

    assert repository.contexts[0].tenant_id == task.owner_id
    assert repository.contexts[0].task_id is None
    assert repository.contexts[1].tenant_id == task.owner_id
    assert repository.contexts[1].task_id == task.task_id
    assert repository.contexts[1].principal_id != repository.contexts[1].tenant_id
    assert current_tenant_execution_context() is None
    assert run.task_id == task.task_id
    assert run.thread_id == f"thread_{run.run_id}"
    assert run.execution_protocol == _binding("workflow", "cad2bim_workflow")
    assert (await service.get_run(alice, task.task_id, run.run_id)).run_id == run.run_id


@pytest.mark.asyncio
async def test_other_user_and_other_task_cannot_read_or_start_run() -> None:
    service = TaskRunService(
        repository=InMemoryControlPlaneRepository(),
        identity_directory=InMemoryIdentityDirectory(),
        execution_protocol_factory=_binding,
        bind_identity=bind_tenant_execution_context,
    )
    alice, bob = _subject("alice"), _subject("bob")
    first = await service.create_task(alice, CreateTaskRequest(title="First"))
    second = await service.create_task(alice, CreateTaskRequest(title="Second"))
    run = await service.start_run(
        alice, first.task_id, StartRunRequest(idempotency_key="first-run")
    )

    with pytest.raises(ResourceNotFoundError):
        await service.get_task(bob, first.task_id)
    with pytest.raises(ResourceNotFoundError):
        await service.start_run(bob, first.task_id, StartRunRequest(idempotency_key="forged"))
    with pytest.raises(ResourceNotFoundError):
        await service.get_run(bob, first.task_id, run.run_id)
    with pytest.raises(ResourceNotFoundError):
        await service.get_run(alice, second.task_id, run.run_id)


@pytest.mark.asyncio
async def test_server_rejects_supplied_thread_and_protocol_scope_mismatch() -> None:
    service = TaskRunService(
        repository=InMemoryControlPlaneRepository(),
        identity_directory=InMemoryIdentityDirectory(),
        execution_protocol_factory=lambda _module, _stage: _binding("workflow", "cad2bim_workflow"),
        bind_identity=bind_tenant_execution_context,
    )
    alice = _subject("alice")
    task = await service.create_task(alice, CreateTaskRequest(title="First"))

    with pytest.raises(ValueError, match="server-owned"):
        await service.start_run(
            alice, task.task_id, StartRunRequest(idempotency_key="one", thread_id="forged")
        )
    with pytest.raises(ExecutionProtocolMismatchError, match="route"):
        await service.start_run(
            alice,
            task.task_id,
            StartRunRequest(module="other", stage_key="other", idempotency_key="two"),
        )


@pytest.mark.asyncio
async def test_invalid_task_id_never_enters_trusted_context() -> None:
    service = TaskRunService(
        repository=InMemoryControlPlaneRepository(),
        identity_directory=InMemoryIdentityDirectory(),
        execution_protocol_factory=_binding,
        bind_identity=bind_tenant_execution_context,
    )

    with pytest.raises(ValueError, match="invalid task ID"):
        await service.get_task(_subject("alice"), "bad!")
    assert current_tenant_execution_context() is None


class _CompletedRepository:
    def __init__(self, run: RunRecord) -> None:
        self.run = run
        self.events = (
            make_completion_checkpoint_event(
                run, root_checkpoint_id="root-one", graph_operation_id="operation-one"
            ),
        )

    async def get_run(self, owner_id, run_id):
        return self.run if (owner_id, run_id) == (self.run.owner_id, self.run.run_id) else None

    async def get_completion_checkpoint_events(self, owner_id, run_id):
        assert (owner_id, run_id) == (self.run.owner_id, self.run.run_id)
        return self.events


class _CompletedGraph:
    def __init__(self, run: RunRecord) -> None:
        binding = run.execution_protocol
        assert binding is not None
        self.calls = []
        checkpoint_map = (("", "root-one"),)
        self.state = GraphCheckpointState(
            values={"requirements": []},
            checkpoint=GraphCheckpointDescriptor(
                thread_id=run.thread_id,
                checkpoint_id="root-one",
                checkpoint_ns="",
                root_checkpoint_id="root-one",
                checkpoint_map=checkpoint_map,
                checkpoint_map_digest=checkpoint_cursor_digest(
                    checkpoint_id="root-one", checkpoint_ns="", checkpoint_map=checkpoint_map
                ),
            ),
            graph_operation_id="operation-one",
            target_run_id=run.run_id,
        )

    async def get_checkpoint_state(self, *, thread_id, checkpoint_id, checkpoint_ns=""):
        self.calls.append((thread_id, checkpoint_id, checkpoint_ns))
        return self.state


async def _completed_service():
    subject = _subject("completed-user")
    directory = InMemoryIdentityDirectory()
    identity = await directory.resolve_authenticated_subject(subject, command_id="test")
    run = RunRecord.model_construct(
        run_id="run-one",
        task_id="task-one",
        owner_id=identity.tenant_id,
        module="workflow",
        stage_key="cad2bim_workflow",
        thread_id="thread-one",
        execution_protocol=_binding("workflow", "cad2bim_workflow"),
        status=RunStatus.COMPLETED,
    )
    repository = _CompletedRepository(run)
    graph = _CompletedGraph(run)
    service = TaskRunService(
        repository=repository,
        identity_directory=directory,
        execution_protocol_factory=_binding,
        bind_identity=bind_tenant_execution_context,
        graph_runtime=graph,
    )
    return service, repository, graph, run, subject


@pytest.mark.asyncio
async def test_completed_read_uses_unique_event_and_saved_terminal_root() -> None:
    service, _repository, graph, run, subject = await _completed_service()
    binding = await service.completed_state_read_binding(subject, run.task_id, run.run_id)

    assert binding.owner_id == run.owner_id
    assert binding.checkpoint_cursor.checkpoint_id == "root-one"
    assert {source.checkpoint_ns for source in binding.field_sources} == {""}
    assert {source.checkpoint_id for source in binding.field_sources} == {"root-one"}
    assert graph.calls == [("thread-one", "root-one", "")]


@pytest.mark.asyncio
async def test_completed_read_rejects_duplicate_or_missing_event_before_graph() -> None:
    service, repository, graph, run, subject = await _completed_service()
    repository.events = repository.events * 2
    with pytest.raises(StateReadBindingError, match="unique"):
        await service.completed_state_read_binding(subject, run.task_id, run.run_id)
    repository.events = ()
    with pytest.raises(StateReadBindingError, match="unique"):
        await service.completed_state_read_binding(subject, run.task_id, run.run_id)
    assert graph.calls == []


@pytest.mark.asyncio
async def test_completed_read_rejects_contradictory_operation_and_unfinished_root() -> None:
    service, _repository, graph, run, subject = await _completed_service()
    graph.state = graph.state.model_copy(update={"graph_operation_id": "other-operation"})
    with pytest.raises(StateReadBindingError, match="contradicts"):
        await service.completed_state_read_binding(subject, run.task_id, run.run_id)
    graph.state = graph.state.model_copy(
        update={"graph_operation_id": "operation-one", "interrupts": ({"id": "hold"},)}
    )
    with pytest.raises(StateReadBindingError, match="contradicts"):
        await service.completed_state_read_binding(subject, run.task_id, run.run_id)
    graph.state = graph.state.model_copy(
        update={
            "interrupts": (),
            "checkpoint": graph.state.checkpoint.model_copy(update={"next_nodes": ("next",)}),
        }
    )
    with pytest.raises(StateReadBindingError, match="contradicts"):
        await service.completed_state_read_binding(subject, run.task_id, run.run_id)
