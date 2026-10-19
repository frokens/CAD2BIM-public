"""In-memory task, run, lease, and event operations."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import timedelta
from uuid import uuid4

from CAD2BIM_backend.domain.control_plane import (
    ACTIVE_RUN_STATUSES,
    ConflictError,
    CreateTaskRequest,
    ExecutionMode,
    ExecutionProtocolStatus,
    FeedbackCommandStatus,
    FeedbackCommand,
    FeedbackCommandReceipt,
    FeedbackDeliveryMode,
    FeedbackScope,
    SubmitFeedbackCommand,
    IdempotencyConflictError,
    InterruptStatus,
    LeaseConflictError,
    ResourceNotFoundError,
    RunRecord,
    RunStatus,
    SavepointRecord,
    StartRunRequest,
    TaskRecord,
    TaskStatus,
    TERMINAL_RUN_STATUSES,
    require_run_transition,
)
from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.domain.data_runtime import CriticalOperationStatus
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.artifact_refs import FinalIFCArtifactRef
from CAD2BIM_backend.domain.identity import stable_internal_identity_id
from CAD2BIM_backend.domain.feedback_target import resolve_feedback_scope
from CAD2BIM_backend.infrastructure.postgres.tenant_context import current_tenant_execution_context
from CAD2BIM_backend.domain.run_events import (
    RunEvent,
    RunEventScope,
    RunEventType,
    make_run_status_event,
    make_task_title_event,
    reclaimed_runtime_terminal_events,
)
from CAD2BIM_backend.domain.resume_operations import (
    ResumeObservedOutcome,
    ResumeOperationPhase,
    ResumeOperationRecord,
    require_command_fingerprint,
)
from CAD2BIM_backend.domain.run_request_identity import (
    new_run_request_identity,
    require_exact_resume_request,
    require_exact_resume_target,
)
from CAD2BIM_backend.domain.run_input_manifest import (
    RunInputManifest,
    rebind_run_input_manifest,
)
from CAD2BIM_backend.infrastructure.memory.state import MemoryRepositoryBase, _now, _fingerprint


class TaskRunRepositoryMixin(MemoryRepositoryBase):
    async def set_generated_task_title(self, grant: RunnerCheckpointGrant, title: str) -> TaskRecord:
        async with self._lock:
            run = self._require_lease(grant.scope.run_id, grant.runner_id, grant.lease_token)
            identity = grant.scope.identity
            if (run.owner_id != identity.tenant_id or run.owner_id != grant.scope.owner_id
                or run.principal_id != identity.principal_id or run.task_id != identity.task_id
                or run.thread_id != grant.thread_id or run.writer_generation != grant.writer_generation
                or run.execution_mode is not grant.scope.execution_mode
                or run.module != "component"):
                raise LeaseConflictError("Task naming does not belong to the current Component claim")
            task = self._require_task(identity.tenant_id, run.task_id)
            if task.metadata.get("component_title_generated") is True:
                return task
            now = _now()
            updated = task.model_copy(update={"title": title,
                "metadata": {**task.metadata, "component_title_generated": True}, "updated_at": now})
            self._append_events_locked(run.owner_id, run, (make_task_title_event(run, occurred_at=now),))
            self._tasks[task.task_id] = updated
            return updated

    async def create_task(self, owner_id: str, request: CreateTaskRequest) -> TaskRecord:
        async with self._lock:
            replay = self._idempotent_replay(
                "task", owner_id, request.idempotency_key, request, self._tasks
            )
            if replay is not None:
                if replay.status is TaskStatus.ARCHIVED:
                    raise ConflictError("task was deleted; use a new request identity")
                return replay
            now = _now()
            task = TaskRecord(
                task_id=f"task_{uuid4().hex}",
                owner_id=owner_id,
                title=request.title,
                instruction=request.instruction,
                metadata=request.metadata,
                status=TaskStatus.DRAFT,
                active_module=request.active_module,
                workspace_uri=request.workspace_uri,
                idempotency_key=request.idempotency_key,
                created_at=now,
                updated_at=now,
            )
            self._tasks[task.task_id] = task
            self._remember("task", owner_id, request.idempotency_key, request, task.task_id)
            return task

    async def get_task(self, owner_id: str, task_id: str) -> TaskRecord | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            return task if task is not None and task.owner_id == owner_id and task.status is not TaskStatus.ARCHIVED else None

    async def delete_task(self, owner_id: str, task_id: str) -> None:
        async with self._lock:
            task = self._require_task(owner_id, task_id, include_archived=True)
            if task.status is TaskStatus.ARCHIVED:
                return
            runs = [run for run in self._runs.values() if run.owner_id == owner_id and run.task_id == task_id]
            if any(run.status is RunStatus.RUNNING for run in runs):
                raise ConflictError("Pause the running task and wait for it to stop before deleting it.")
            if any(operation.owner_id == owner_id and operation.task_id == task_id
                and operation.status is CriticalOperationStatus.PREPARED
                for operation in self._state.critical_operations.values()):
                raise ConflictError("This task has an unresolved write. Reconcile its original Operation before deleting it.")
            now = _now()
            for run in runs:
                if run.status not in TERMINAL_RUN_STATUSES:
                    self._cancel_run_locked(run, now)
            self._set_task_status(task_id, TaskStatus.ARCHIVED, now)

    async def get_run_start_replay(
        self,
        owner_id: str,
        task_id: str,
        request: StartRunRequest,
    ) -> RunRecord | None:
        async with self._lock:
            self._require_task(owner_id, task_id)
            return self._idempotent_replay(
                "run",
                f"{owner_id}:{task_id}",
                request.idempotency_key,
                new_run_request_identity(request),
                self._runs,
            )

    async def get_resume_run_replay(
        self,
        owner_id: str,
        task_id: str,
        savepoint_id: str,
        request: StartRunRequest,
    ) -> RunRecord | None:
        async with self._lock:
            self._require_task(owner_id, task_id)
            savepoint = self._savepoints.get(savepoint_id)
            if savepoint is None or savepoint.owner_id != owner_id or savepoint.task_id != task_id:
                raise ResourceNotFoundError("savepoint not found")
            source_run = self._runs.get(savepoint.run_id)
            if source_run is None:
                raise ResourceNotFoundError("source run not found")
            require_exact_resume_request(
                source_run,
                savepoint_thread_id=savepoint.thread_id,
                request=request,
            )

            entry = self._idempotency.get(
                ("run", f"{owner_id}:{task_id}", request.idempotency_key)
            )
            if entry is None:
                return None
            _, run_id = entry
            replay = self._runs[run_id]
            require_exact_resume_target(
                source_run,
                savepoint_thread_id=savepoint.thread_id,
                target_run=replay,
            )
            existing_request = StartRunRequest(
                module=replay.module,
                stage_key=replay.stage_key,
                input_payload=replay.input_payload,
                idempotency_key=replay.idempotency_key,
            )
            if replay.resume_from_savepoint_id != savepoint_id or new_run_request_identity(
                existing_request,
                resume_from_savepoint_id=replay.resume_from_savepoint_id,
            ) != new_run_request_identity(
                request,
                resume_from_savepoint_id=savepoint_id,
            ):
                raise IdempotencyConflictError(
                    "idempotency key was reused with different input"
                )
            if replay.run_id not in self._resume_operations:
                raise ConflictError("resumed run is missing its resume operation")
            return replay

    async def create_run(
        self,
        owner_id: str,
        task_id: str,
        request: StartRunRequest,
        *,
        execution_protocol_factory: Callable[[], ExecutionProtocolBinding],
        resume_from_savepoint_id: str | None = None,
        run_id: str | None = None,
        input_manifest: RunInputManifest | None = None,
        input_manifest_factory: Callable[
            [ExecutionProtocolBinding], RunInputManifest
        ]
        | None = None,
    ) -> RunRecord:
        if input_manifest is not None and input_manifest_factory is not None:
            raise ValueError("Run input manifest has two fact sources")
        async with self._lock:
            task = self._require_task(owner_id, task_id)
            scope = f"{owner_id}:{task_id}"
            request_identity = new_run_request_identity(
                request,
                resume_from_savepoint_id=resume_from_savepoint_id,
            )
            replay = self._idempotent_replay(
                "run", scope, request.idempotency_key, request_identity, self._runs
            )
            if replay is not None:
                return replay
            self._require_no_active_run_locked(task_id)
            execution_protocol = execution_protocol_factory()
            resolved_manifest = (
                input_manifest_factory(execution_protocol)
                if input_manifest_factory is not None
                else input_manifest
            )
            run = self._new_run_locked(
                owner_id,
                task_id,
                request,
                run_id=run_id or f"run_{uuid4().hex}",
                input_manifest=resolved_manifest,
                execution_protocol=execution_protocol,
                resume_from_savepoint_id=resume_from_savepoint_id,
            )
            identity = current_tenant_execution_context()
            if request.content and (identity is None or identity.task_id != task_id):
                raise ConflictError("initial input requires an authenticated Task identity")
            if identity is not None:
                run = run.model_copy(update={"tenant_id": identity.tenant_id, "principal_id": identity.principal_id})
            if request.content:
                receipt = self._accept_feedback_locked(identity, run, SubmitFeedbackCommand(
                    client_request_id=stable_internal_identity_id("initial_", "initial-input", run.run_id, request.idempotency_key),
                    delivery_mode=FeedbackDeliveryMode.STEER, content=request.content,
                    scope=FeedbackScope(run_id=run.run_id)))
                run = self._runs[run.run_id].model_copy(update={"initial_feedback": receipt})
            self._persist_new_run_locked(task, run, request_identity)
            return run

    async def create_resume_run(
        self,
        owner_id: str,
        task_id: str,
        savepoint_id: str,
        request: StartRunRequest,
        *,
        operation_id: str,
        command_fingerprint: str,
        source_checkpoint_cursor: CheckpointCursor,
    ) -> tuple[RunRecord, ResumeOperationRecord]:
        if not operation_id:
            raise ValueError("operation_id must not be empty")
        require_command_fingerprint(command_fingerprint)
        async with self._lock:
            task = self._require_task(owner_id, task_id)
            savepoint = self._savepoints.get(savepoint_id)
            if savepoint is None or savepoint.owner_id != owner_id or savepoint.task_id != task_id:
                raise ResourceNotFoundError("savepoint not found")
            self._require_source_cursor(savepoint, source_checkpoint_cursor)
            source_run = self._runs.get(savepoint.run_id)
            if source_run is None or source_run.status not in TERMINAL_RUN_STATUSES:
                raise ConflictError("source run must be terminal before savepoint resume")
            if (
                source_run.execution_protocol_status
                is not ExecutionProtocolStatus.PINNED
                or source_run.execution_protocol is None
            ):
                raise ConflictError("source run execution protocol is quarantined")
            require_exact_resume_request(
                source_run,
                savepoint_thread_id=savepoint.thread_id,
                request=request,
            )

            scope = f"{owner_id}:{task_id}"
            request_identity = {
                **new_run_request_identity(
                    request,
                    resume_from_savepoint_id=savepoint_id,
                ),
                "command_fingerprint": command_fingerprint,
                "source_checkpoint_cursor": source_checkpoint_cursor.model_dump(mode="json"),
            }
            replay = self._idempotent_replay(
                "run", scope, request.idempotency_key, request_identity, self._runs
            )
            if replay is not None:
                operation = self._resume_operations.get(replay.run_id)
                if operation is None:
                    raise ConflictError("resumed run is missing its resume operation")
                return replay, operation
            if any(item.operation_id == operation_id for item in self._resume_operations.values()):
                raise ConflictError("resume operation already exists")

            self._require_no_active_run_locked(task_id)
            now = _now()
            run_id = f"run_{uuid4().hex}"
            run = self._new_run_locked(
                owner_id,
                task_id,
                request,
                run_id=run_id,
                input_manifest=(
                    rebind_run_input_manifest(
                        source_run.input_manifest,
                        run_id=run_id,
                        created_at=now,
                    )
                    if source_run.input_manifest is not None
                    else None
                ),
                execution_protocol=source_run.execution_protocol,
                resume_from_savepoint_id=savepoint_id,
                now=now,
            )
            require_exact_resume_target(
                source_run,
                savepoint_thread_id=savepoint.thread_id,
                target_run=run,
            )
            operation = ResumeOperationRecord(
                operation_id=operation_id,
                owner_id=owner_id,
                task_id=task_id,
                run_id=run.run_id,
                savepoint_id=savepoint_id,
                command_fingerprint=command_fingerprint,
                source_checkpoint_cursor=source_checkpoint_cursor,
                created_at=run.created_at,
                updated_at=run.updated_at,
            )
            self._persist_new_run_locked(task, run, request_identity)
            self._resume_operations[run.run_id] = operation
            return run, operation

    async def get_resume_operation(
        self, owner_id: str, run_id: str
    ) -> ResumeOperationRecord | None:
        async with self._lock:
            operation = self._resume_operations.get(run_id)
            return operation if operation is not None and operation.owner_id == owner_id else None

    async def bind_resume_session(
        self,
        run_id: str,
        runner_id: str,
        lease_token: str,
        session_id: str,
    ) -> ResumeOperationRecord:
        if not session_id:
            raise ValueError("session_id must not be empty")
        async with self._lock:
            self._require_lease(run_id, runner_id, lease_token)
            operation = self._require_resume_operation(run_id)
            if operation.target_mcp_session_id == session_id:
                return operation
            if operation.target_mcp_session_id is not None:
                raise ConflictError("resume operation is already bound to another MCP session")
            if operation.phase not in {
                ResumeOperationPhase.INTENT,
                ResumeOperationPhase.GRAPH_INFLIGHT,
            }:
                raise ConflictError("resume session cannot be bound after graph observation")
            updated = operation.model_copy(
                update={"target_mcp_session_id": session_id, "updated_at": _now()}
            )
            self._resume_operations[run_id] = updated
            return updated

    async def mark_resume_graph_inflight(
        self,
        run_id: str,
        runner_id: str,
        lease_token: str,
        operation_id: str,
    ) -> ResumeOperationRecord:
        async with self._lock:
            self._require_lease(run_id, runner_id, lease_token)
            operation = self._require_resume_operation(run_id)
            self._require_operation_id(operation, operation_id)
            if operation.phase == ResumeOperationPhase.GRAPH_INFLIGHT:
                return operation
            if operation.phase != ResumeOperationPhase.INTENT:
                raise ConflictError("resume operation cannot re-enter graph execution")
            if operation.target_mcp_session_id is None:
                raise ConflictError("resume operation has no bound target MCP session")
            updated = operation.model_copy(
                update={
                    "phase": ResumeOperationPhase.GRAPH_INFLIGHT,
                    "attempt_count": operation.attempt_count + 1,
                    "updated_at": _now(),
                }
            )
            self._resume_operations[run_id] = updated
            return updated

    async def record_resume_graph_observation(
        self,
        run_id: str,
        runner_id: str,
        lease_token: str,
        operation_id: str,
        checkpoint_cursor: CheckpointCursor,
        outcome: ResumeObservedOutcome,
    ) -> ResumeOperationRecord:
        observed_outcome = ResumeObservedOutcome(outcome)
        async with self._lock:
            run = self._require_lease(run_id, runner_id, lease_token)
            if checkpoint_cursor.thread_id != run.thread_id:
                raise ConflictError("observed checkpoint belongs to another Run thread")
            operation = self._require_resume_operation(run_id)
            self._require_operation_id(operation, operation_id)
            if operation.phase in {
                ResumeOperationPhase.GRAPH_OBSERVED,
                ResumeOperationPhase.FINALIZED,
            }:
                if (
                    operation.observed_checkpoint_cursor == checkpoint_cursor
                    and operation.observed_outcome == observed_outcome
                ):
                    return operation
                raise ConflictError("resume operation already has another graph observation")
            if operation.phase != ResumeOperationPhase.GRAPH_INFLIGHT:
                raise ConflictError("resume operation is not executing in the graph")
            updated = operation.model_copy(
                update={
                    "phase": ResumeOperationPhase.GRAPH_OBSERVED,
                    "observed_checkpoint_cursor": checkpoint_cursor,
                    "observed_outcome": observed_outcome,
                    "updated_at": _now(),
                }
            )
            self._resume_operations[run_id] = updated
            return updated

    @staticmethod
    def _require_source_cursor(
        savepoint: SavepointRecord,
        cursor: CheckpointCursor,
    ) -> None:
        if (
            cursor.thread_id != savepoint.thread_id
            or cursor.checkpoint_id != savepoint.checkpoint_id
            or cursor.checkpoint_ns != savepoint.checkpoint_ns
        ):
            raise ConflictError("source checkpoint cursor does not match the savepoint")

    async def finalize_resume_operation(
        self,
        run_id: str,
        runner_id: str,
        lease_token: str,
        operation_id: str,
    ) -> ResumeOperationRecord:
        async with self._lock:
            self._require_lease(run_id, runner_id, lease_token)
            operation = self._require_resume_operation(run_id)
            self._require_operation_id(operation, operation_id)
            finalized = self._finalize_resume_operation_locked(operation)
            self._resume_operations[run_id] = finalized
            return finalized

    @staticmethod
    def _finalize_resume_operation_locked(
        operation: ResumeOperationRecord,
    ) -> ResumeOperationRecord:
        if operation.phase == ResumeOperationPhase.FINALIZED:
            return operation
        if operation.phase != ResumeOperationPhase.GRAPH_OBSERVED:
            raise ConflictError("resume operation has no durable graph observation")
        return operation.model_copy(
            update={"phase": ResumeOperationPhase.FINALIZED, "updated_at": _now()}
        )

    def _new_run_locked(
        self,
        owner_id: str,
        task_id: str,
        request: StartRunRequest,
        *,
        run_id: str,
        input_manifest: RunInputManifest | None,
        execution_protocol: ExecutionProtocolBinding,
        resume_from_savepoint_id: str | None,
        now=None,
    ) -> RunRecord:
        now = now or _now()
        return RunRecord(
            run_id=run_id,
            task_id=task_id,
            owner_id=owner_id,
            module=request.module,
            stage_key=request.stage_key,
            input_payload=request.input_payload,
            input_manifest=input_manifest,
            idempotency_key=request.idempotency_key,
            thread_id=request.thread_id or f"thread_{run_id}",
            execution_protocol_status=ExecutionProtocolStatus.PINNED,
            execution_protocol=execution_protocol,
            execution_protocol_quarantine_reason=None,
            resume_from_savepoint_id=resume_from_savepoint_id,
            created_at=now,
            updated_at=now,
        )

    def _require_no_active_run_locked(self, task_id: str) -> None:
        if any(
            run.task_id == task_id and run.status in ACTIVE_RUN_STATUSES
            for run in self._runs.values()
        ):
            raise ConflictError("task already has an active run")

    def _persist_new_run_locked(
        self,
        task: TaskRecord,
        run: RunRecord,
        request_identity: object,
    ) -> None:
        self._runs[run.run_id] = run
        self._events.setdefault(run.run_id, [])
        self._tasks[run.task_id] = task.model_copy(
            update={"status": TaskStatus.RUNNING, "updated_at": run.updated_at}
        )
        self._remember(
            "run",
            f"{run.owner_id}:{run.task_id}",
            run.idempotency_key,
            request_identity,
            run.run_id,
        )

    def _require_resume_operation(self, run_id: str) -> ResumeOperationRecord:
        operation = self._resume_operations.get(run_id)
        if operation is None:
            raise ResourceNotFoundError("resume operation not found")
        return operation

    @staticmethod
    def _require_operation_id(operation: ResumeOperationRecord, operation_id: str) -> None:
        if operation.operation_id != operation_id:
            raise ConflictError("resume operation id does not match the Run")

    async def get_run(self, owner_id: str, run_id: str) -> RunRecord | None:
        async with self._lock:
            try:
                return self._require_run(owner_id, run_id)
            except ResourceNotFoundError:
                return None

    async def get_observed_run(self, run_id: str, thread_id: str):
        from CAD2BIM_backend.domain.identity import TenantExecutionContext, TenantActorKind
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.thread_id != thread_id:
                raise ResourceNotFoundError("observed Run not found")
            self._require_task(run.owner_id, run.task_id)
            tenant_id, principal_id = run.require_execution_identity()
            identity = current_tenant_execution_context()
            if identity is not None:
                if identity.tenant_id != tenant_id or identity.task_id != run.task_id:
                    raise ResourceNotFoundError("observed Run is outside the active identity")
            else:
                identity = TenantExecutionContext(tenant_id=tenant_id, principal_id=principal_id,
                    task_id=run.task_id, actor_kind=TenantActorKind.RUNNER, command_id=f"observe:{run_id}")
            return identity, run

    async def pause_run(self, owner_id: str, run_id: str, *, client_request_id: str) -> RunRecord:
        async with self._lock:
            run = self._require_run(owner_id, run_id)
            if run.pause_request_id == client_request_id or run.status == RunStatus.PAUSED or run.pause_requested:
                return run
            now = _now()
            if run.status == RunStatus.RUNNING:
                updated = run.model_copy(update={"pause_requested": True, "updated_at": now})
            else:
                require_run_transition(run.status, RunStatus.PAUSED)
                now = _now()
                updated = run.model_copy(
                    update={
                        "status": RunStatus.PAUSED,
                        "pause_requested": False,
                        "updated_at": now,
                    }
                )
                self._set_task_status(run.task_id, TaskStatus.PAUSED, now)
            updated = updated.model_copy(update={"pause_request_id": client_request_id})
            self._runs[run_id] = updated
            self._append_events_locked(owner_id, updated, (make_run_status_event(
                source_event_key=f"pause:{client_request_id}", task_id=run.task_id, run_id=run_id,
                module=run.module, stage_key=run.stage_key, run_status=updated.status,
                pause_requested=updated.pause_requested,
                reason="pause_requested" if updated.pause_requested else "paused", occurred_at=now,
            ),))
            return updated

    async def continue_run(self, owner_id: str, run_id: str, *, client_request_id: str) -> RunRecord:
        async with self._lock:
            run = self._require_run(owner_id, run_id)
            if run.continue_request_id == client_request_id:
                return run
            if run.status is not RunStatus.PAUSED:
                raise ConflictError("only a paused Run can continue")
            gate = any(item.run_id == run_id and item.status.value == "pending" for item in self._interrupts.values())
            target = RunStatus.WAITING_USER if gate and not run.feedback_pending else RunStatus.QUEUED
            now = _now()
            run = run.model_copy(update={"status": target, "pause_requested": False,
                                         "continue_request_id": client_request_id, "updated_at": now,
                                         "error": None})
            self._runs[run_id] = run
            self._set_task_status(run.task_id, TaskStatus.WAITING_USER if target is RunStatus.WAITING_USER else TaskStatus.RUNNING, now)
            self._append_events_locked(owner_id, run, (make_run_status_event(
                source_event_key=f"continue:{client_request_id}", task_id=run.task_id, run_id=run_id,
                module=run.module, stage_key=run.stage_key, run_status=target,
                pause_requested=False, reason="continued", occurred_at=now,
            ),))
            return run

    def _accept_feedback_locked(self, identity, run: RunRecord, command: SubmitFeedbackCommand):
        if run.task_id != identity.task_id or run.owner_id != identity.tenant_id:
            raise ResourceNotFoundError("run not found")
        digest = _fingerprint({"run_id": run.run_id, "command": command.model_dump(mode="json")})
        for item in self._feedback.values():
            if item.owner_id == identity.tenant_id and item.author_principal_id == identity.principal_id and item.client_request_id == command.client_request_id:
                if item.run_id != run.run_id or item.request_fingerprint != digest:
                    raise ConflictError("feedback idempotency key was reused with different input")
                return FeedbackCommandReceipt(**item.model_dump(include={"feedback_id", "accepted_sequence", "delivery_mode", "resolved_scope", "status"}))
        if run.status not in ACTIVE_RUN_STATUSES or run.cancel_requested:
            raise ConflictError("Run cannot accept new feedback")
        resolved = resolve_feedback_scope(task_id=run.task_id, run_id=run.run_id, run_stage_key=run.stage_key,
            requested_scope=command.scope, active_subject=run.active_feedback_subject if not run.feedback_sealed else None)
        for block in command.content:
            if block.type == "text":
                continue
            artifact = self._artifacts.get(block.artifact_id)
            if (artifact is None or artifact.owner_id != run.owner_id or artifact.task_id != run.task_id
                or artifact.version_id != block.version_id):
                raise ResourceNotFoundError("input ArtifactVersion not found")
            if block.type == "image" and not artifact.content_type.startswith("image/"):
                raise ConflictError("image input must reference an image ArtifactVersion")
        sequence = max((item.accepted_sequence for item in self._feedback.values() if item.run_id == run.run_id), default=0) + 1
        item = FeedbackCommand(feedback_id=f"feedback_{uuid4().hex}", accepted_sequence=sequence,
            owner_id=run.owner_id, author_principal_id=identity.principal_id, task_id=run.task_id, run_id=run.run_id,
            client_request_id=command.client_request_id, request_fingerprint=digest, delivery_mode=command.delivery_mode,
            content=command.content, resolved_scope=resolved, created_at=_now())
        self._feedback[item.feedback_id] = item
        target = RunStatus.QUEUED if run.status is RunStatus.WAITING_USER else run.status
        self._runs[run.run_id] = run.model_copy(update={"feedback_pending": True, "status": target, "updated_at": _now()})
        self._append_events_locked(run.owner_id, self._runs[run.run_id], (RunEvent(
            event_id=stable_internal_identity_id("event_", "feedback-accepted", item.feedback_id),
            sequence=1, task_id=run.task_id, run_id=run.run_id, event_type="feedback.accepted", occurred_at=item.created_at,
            scope=RunEventScope(module=run.module, stage_key=run.stage_key), item_id=item.feedback_id,
            status=RunEventType.SUCCEEDED, payload_ref=f"feedback://{item.feedback_id}",
            attributes={"feedback_id": item.feedback_id, "client_request_id": item.client_request_id}),))
        if run.status is RunStatus.WAITING_USER:
            self._append_events_locked(run.owner_id, self._runs[run.run_id], (make_run_status_event(
                source_event_key=f"feedback-wake:{item.feedback_id}", task_id=run.task_id, run_id=run.run_id,
                module=run.module, stage_key=run.stage_key, run_status=RunStatus.QUEUED,
                pause_requested=run.pause_requested, reason="feedback_wake", occurred_at=item.created_at,
            ),))
        return FeedbackCommandReceipt(**item.model_dump(include={"feedback_id", "accepted_sequence", "delivery_mode", "resolved_scope", "status"}))

    async def accept_feedback(self, identity, run_id: str, command: SubmitFeedbackCommand):
        async with self._lock:
            return self._accept_feedback_locked(identity, self._require_run(identity.tenant_id, run_id), command)

    async def list_feedback(self, owner_id: str, run_id: str, *, after_sequence: int = 0, limit: int = 100):
        async with self._lock:
            self._require_run(owner_id, run_id)
            if after_sequence < 0 or not 1 <= limit <= 500:
                raise ValueError("invalid feedback page")
            return tuple(sorted((item for item in self._feedback.values() if item.run_id == run_id and item.accepted_sequence > after_sequence),
                                key=lambda item: item.accepted_sequence))[:limit]

    async def get_feedback(self, owner_id: str, run_id: str, feedback_id: str):
        async with self._lock:
            self._require_run(owner_id, run_id)
            item = self._feedback.get(feedback_id)
            return item if item is not None and item.run_id == run_id else None

    async def cancel_run(self, owner_id: str, run_id: str) -> RunRecord:
        async with self._lock:
            run = self._require_run(owner_id, run_id)
            if run.status == RunStatus.CANCELLED or run.cancel_requested:
                return run
            return self._cancel_run_locked(run, _now())

    def _cancel_run_locked(self, run: RunRecord, now) -> RunRecord:
        if run.status is RunStatus.RUNNING:
            updated = run.model_copy(update={"cancel_requested": True, "updated_at": now})
        else:
            require_run_transition(run.status, RunStatus.CANCELLED)
            for gate_id, gate in tuple(self._interrupts.items()):
                if gate.run_id == run.run_id and gate.status is InterruptStatus.PENDING:
                    self._interrupts[gate_id] = gate.model_copy(update={
                        "status": InterruptStatus.RESPONDED, "response": {"origin": "run_cancelled"},
                        "responded_at": now,
                    })
            for feedback_id, command in tuple(self._feedback.items()):
                if command.run_id == run.run_id and command.status in {
                    FeedbackCommandStatus.ACCEPTED, FeedbackCommandStatus.CLAIMED,
                }:
                    self._feedback[feedback_id] = command.model_copy(update={
                        "status": FeedbackCommandStatus.SUPERSEDED, "claimed_batch_id": None,
                    })
            updated = run.model_copy(update={"status": RunStatus.CANCELLED,
                "cancel_requested": True, "feedback_pending": False,
                "updated_at": now, "ended_at": now})
            self._set_task_status(run.task_id, TaskStatus.CANCELLED, now)
        self._runs[run.run_id] = updated
        return updated

    async def claim_next_run(self, runner_id: str, *, lease_seconds: int = 30) -> RunRecord | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        async with self._lock:
            now = _now()
            candidates = sorted(
                self._runs.values(), key=lambda item: (item.created_at, item.run_id)
            )
            for run in candidates:
                if run.execution_mode is not ExecutionMode.GRAPH:
                    continue
                if run.execution_protocol_status is not ExecutionProtocolStatus.PINNED:
                    continue
                expired = (
                    run.status == RunStatus.RUNNING
                    and run.lease_expires_at is not None
                    and run.lease_expires_at <= now
                )
                queued_ready = (
                    run.status == RunStatus.QUEUED
                    and (
                        run.claim_not_before is None
                        or run.claim_not_before <= now
                    )
                )
                if not queued_ready and not expired:
                    continue
                deferred_restore = (
                    run.status == RunStatus.QUEUED
                    and run.restore_defer_reason_code == "mcp_restore_transient"
                )
                # A deferred queued re-claim remains in the same recovery cycle.
                # Any newly expired running lease starts a new cycle, including
                # a prior recovery worker that itself lost the lease.
                new_recovery_cycle = expired
                updated = run.model_copy(
                    update={
                        "status": RunStatus.RUNNING,
                        "claimed_by": runner_id,
                        "lease_token": f"lease_{uuid4().hex}",
                        "lease_expires_at": now + timedelta(seconds=lease_seconds),
                        "heartbeat_at": now,
                        "claim_count": run.claim_count + 1,
                        "writer_generation": run.writer_generation + 1,
                        "reclaimed_lease": expired or deferred_restore,
                        "claim_not_before": None,
                        "recovery_generation": (
                            run.recovery_generation + 1
                            if new_recovery_cycle
                            else run.recovery_generation
                        ),
                        "restore_defer_count": (
                            0 if new_recovery_cycle else run.restore_defer_count
                        ),
                        "restore_defer_reason_code": None,
                        "started_at": run.started_at or now,
                        "updated_at": now,
                    }
                )
                self._runs[run.run_id] = updated
                if not expired:
                    self._append_events_locked(updated.owner_id, updated, (make_run_status_event(
                        source_event_key=f"claim:{updated.writer_generation}",
                        task_id=updated.task_id, run_id=updated.run_id, module=updated.module,
                        stage_key=updated.stage_key, run_status=RunStatus.RUNNING,
                        pause_requested=updated.pause_requested, reason="claimed", occurred_at=now,
                    ),))
                return updated
            return None

    async def defer_claimed(
        self,
        run_id: str,
        runner_id: str,
        lease_token: str,
        *,
        expected_writer_generation: int,
        expected_recovery_generation: int,
        reason_code: str,
        delay_seconds: float,
    ) -> RunRecord:
        if not 0 < delay_seconds <= 300:
            raise ValueError("delay_seconds must be between 0 and 300")
        if reason_code != "mcp_restore_transient":
            raise ValueError("unsupported durable deferral reason")
        async with self._lock:
            run = self._require_lease(run_id, runner_id, lease_token)
            if run.writer_generation != expected_writer_generation:
                raise LeaseConflictError("writer generation changed")
            if run.recovery_generation != expected_recovery_generation:
                raise LeaseConflictError("recovery generation changed")
            if not run.reclaimed_lease or run.recovery_generation < 1:
                raise ConflictError("Run is not in exact reclaim recovery")
            require_run_transition(run.status, RunStatus.QUEUED)
            now = _now()
            updated = run.model_copy(
                update={
                    "status": RunStatus.QUEUED,
                    "claimed_by": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "reclaimed_lease": True,
                    "claim_not_before": now + timedelta(seconds=delay_seconds),
                    "restore_defer_count": run.restore_defer_count + 1,
                    "restore_defer_reason_code": reason_code,
                    "error": None,
                    "ended_at": None,
                    "updated_at": now,
                }
            )
            self._runs[run_id] = updated
            return updated

    async def heartbeat(
        self,
        run_id: str,
        runner_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 30,
    ) -> RunRecord:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        async with self._lock:
            run = self._require_lease(run_id, runner_id, lease_token)
            now = _now()
            updated = run.model_copy(
                update={
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "heartbeat_at": now,
                    "updated_at": now,
                }
            )
            self._runs[run_id] = updated
            return updated

    async def pause_claimed(
        self, run_id: str, runner_id: str, lease_token: str, *, error: str | None = None,
    ) -> RunRecord:
        async with self._lock:
            run = self._require_lease(run_id, runner_id, lease_token)
            now = _now()
            require_run_transition(run.status, RunStatus.PAUSED)
            updated = run.model_copy(
                update={
                    "status": RunStatus.PAUSED,
                    "pause_requested": False,
                    "claimed_by": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "error": error,
                    "updated_at": now,
                }
            )
            self._runs[run_id] = updated
            self._set_task_status(run.task_id, TaskStatus.PAUSED, now)
            self._append_events_locked(updated.owner_id, updated, (make_run_status_event(
                source_event_key=f"paused-claim:{updated.writer_generation}",
                task_id=updated.task_id, run_id=run_id, module=updated.module, stage_key=updated.stage_key,
                run_status=RunStatus.PAUSED, pause_requested=False, reason=error or "paused", occurred_at=now,
            ),))
            return updated

    async def complete(
        self,
        run_id: str,
        runner_id: str,
        lease_token: str,
        *,
        events: Sequence[RunEvent] = (),
        final_artifact_ref: FinalIFCArtifactRef | None = None,
    ) -> RunRecord:
        async with self._lock:
            run = self._require_lease(run_id, runner_id, lease_token)
            target = RunStatus.CANCELLED if run.cancel_requested else RunStatus.COMPLETED
            require_run_transition(run.status, target)
            if target is RunStatus.COMPLETED and any(
                item.run_id == run_id
                and item.status
                in {FeedbackCommandStatus.ACCEPTED, FeedbackCommandStatus.CLAIMED}
                for item in self._feedback.values()
            ):
                raise ConflictError("Run cannot complete with unacknowledged feedback")
            if final_artifact_ref is not None:
                if target is not RunStatus.COMPLETED:
                    raise ConflictError("cancelled Run cannot publish a final IFC Artifact")
                self._require_final_artifact(run, final_artifact_ref)
            finalized_operation: ResumeOperationRecord | None = None
            if run.resume_from_savepoint_id is not None:
                operation = self._require_resume_operation(run_id)
                finalized_operation = self._finalize_resume_operation_locked(operation)
            self._append_events_locked(run.owner_id, run, events)
            now = _now()
            updated = run.model_copy(
                update={
                    "status": target,
                    "final_artifact_ref": final_artifact_ref or run.final_artifact_ref,
                    "claimed_by": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "updated_at": now,
                    "ended_at": now,
                }
            )
            if finalized_operation is not None:
                self._resume_operations[run_id] = finalized_operation
            self._runs[run_id] = updated
            task_status = (
                TaskStatus.CANCELLED if target == RunStatus.CANCELLED else TaskStatus.COMPLETED
            )
            self._set_task_status(run.task_id, task_status, now)
            return updated

    def _require_final_artifact(
        self,
        run: RunRecord,
        reference: FinalIFCArtifactRef,
    ) -> None:
        artifact = self._artifacts.get(reference.artifact_id)
        if (
            artifact is None
            or reference.source_run_id != run.run_id
            or artifact.owner_id != run.owner_id
            or artifact.task_id != run.task_id
            or artifact.run_id != run.run_id
            or artifact.kind != "final_ifc"
            or artifact.version_id != reference.version_id
        ):
            raise ResourceNotFoundError("final IFC Artifact version not found")
        if (
            artifact.version_no != reference.version
            or artifact.sha256 != reference.sha256
            or artifact.size_bytes != reference.size_bytes
            or artifact.filename != reference.filename
        ):
            raise ConflictError("final IFC Artifact metadata does not match the registry")

    async def fail(
        self,
        run_id: str,
        runner_id: str,
        lease_token: str,
        *,
        error: str,
        events: Sequence[RunEvent] = (),
    ) -> RunRecord:
        async with self._lock:
            run = self._require_lease(run_id, runner_id, lease_token)
            self._append_events_locked(run.owner_id, run, events)
            now = _now()
            target = RunStatus.CANCELLED if run.cancel_requested else RunStatus.FAILED
            require_run_transition(run.status, target)
            updated = run.model_copy(
                update={
                    "status": target,
                    "error": error,
                    "claimed_by": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "updated_at": now,
                    "ended_at": now,
                }
            )
            self._runs[run_id] = updated
            self._set_task_status(
                run.task_id,
                TaskStatus.CANCELLED if target == RunStatus.CANCELLED else TaskStatus.FAILED,
                now,
            )
            return updated

    async def append_events(
        self, owner_id: str, run_id: str, events: Sequence[RunEvent]
    ) -> tuple[RunEvent, ...]:
        async with self._lock:
            run = self._require_run(owner_id, run_id)
            return self._append_events_locked(owner_id, run, events)

    async def reconcile_reclaimed_runtime_events(
        self,
        run_id: str,
        runner_id: str,
        lease_token: str,
    ) -> tuple[RunEvent, ...]:
        async with self._lock:
            run = self._require_lease(run_id, runner_id, lease_token)
            if not run.reclaimed_lease:
                return ()
            events = reclaimed_runtime_terminal_events(
                tuple(self._events.get(run_id, ())),
                task_id=run.task_id,
                run_id=run.run_id,
                claim_count=run.claim_count,
                occurred_at=_now(),
            )
            return self._append_events_locked(run.owner_id, run, events)

    async def list_events(
        self,
        owner_id: str,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        through_sequence: int | None = None,
        audience=None,
    ) -> tuple[RunEvent, ...]:
        async with self._lock:
            self._require_run(owner_id, run_id)
            if after_sequence < 0 or limit < 1:
                raise ValueError("invalid event cursor")
            return tuple(
                event for event in self._events.get(run_id, ()) if event.sequence > after_sequence
                and (through_sequence is None or event.sequence <= through_sequence)
                and (audience is None or event.audience == audience)
            )[:limit]

    async def list_tasks(self, owner_id: str, *, after_task_id: str | None = None, limit: int = 50):
        async with self._lock:
            if not 1 <= limit <= 200:
                raise ValueError("invalid task page size")
            tasks = sorted((task for task in self._tasks.values() if task.owner_id == owner_id
                and task.status is not TaskStatus.ARCHIVED
                and (after_task_id is None or task.task_id > after_task_id)), key=lambda item: item.task_id)[:limit]
            return tuple((task, max((run for run in self._runs.values() if run.task_id == task.task_id),
                         key=lambda run: (run.created_at, run.run_id), default=None)) for task in tasks)

    async def read_chat_snapshot(self, owner_id: str, run_id: str, *, before_sequence: int | None = None, limit: int = 100):
        from CAD2BIM_backend.domain.run_events import RunEventAudience
        async with self._lock:
            run = self._require_run(owner_id, run_id)
            if not 1 <= limit <= 500 or (before_sequence is not None and before_sequence < 1):
                raise ValueError("invalid chat snapshot page")
            events = self._events.get(run_id, ())
            rows = [event for event in reversed(events) if event.audience is RunEventAudience.PUBLIC
                    and (before_sequence is None or event.sequence < before_sequence)]
            page = rows[:limit]
            boundary = max((event.sequence for event in events), default=0)
            item_ids = {event.item_id for event in page if (
                event.event_type.startswith(("chat.", "tool.call."))
                or event.event_type == "feedback.accepted" or event.attributes.get("source_kind") == "skill"
            )}
            call_ids = {event.attributes["call_id"] for event in page if event.attributes.get("call_id")}
            public = tuple(event for event in events if event.audience is RunEventAudience.PUBLIC)
            cycle_start = max((event.sequence for event in public if event.event_type == "run.status.changed"
                and event.attributes.get("run_status") != RunStatus.RUNNING.value), default=0)
            histories = {event.sequence: event for event in page}
            activities = []
            for event in public:
                if event.item_id in item_ids:
                    histories[event.sequence] = event
                if ((event.event_type == "runtime.activity.changed" and (event.sequence > cycle_start
                        or event.attributes.get("call_id") in call_ids)) or event.sequence == cycle_start
                    or (event.event_type.startswith("tool.call.") and event.sequence > max(0, boundary - 100))):
                    activities.append(event)
            return (run, tuple(histories[sequence] for sequence in sorted(histories)), boundary,
                    page[-1].sequence if len(rows) > limit else None, tuple(activities))

    async def get_item_events(self, owner_id: str, run_id: str, item_id: str):
        from CAD2BIM_backend.domain.run_events import RunEventAudience
        async with self._lock:
            self._require_run(owner_id, run_id)
            return tuple(event for event in self._events.get(run_id, ()) if event.item_id == item_id
                         and event.audience is RunEventAudience.PUBLIC)

    async def append_chat_events(self, owner_id: str, run_id: str, events: Sequence[RunEvent]):
        async with self._lock:
            run = self._require_run(owner_id, run_id)
            by_id = {event.event_id: event for event in self._events.get(run_id, ())}
            canonical = []
            for event in events:
                original = by_id.get(event.event_id)
                if original is None:
                    canonical.append(event)
                else:
                    attributes = dict(event.attributes)
                    for location in ("checkpoint_ns", "message_index", "memory_slot"):
                        if location in original.attributes:
                            attributes[location] = original.attributes[location]
                        else:
                            attributes.pop(location, None)
                    canonical.append(event.model_copy(update={"payload_ref": original.payload_ref,
                        "occurred_at": original.occurred_at, "attributes": attributes}))
            return self._append_events_locked(owner_id, run, canonical)


__all__ = ["TaskRunRepositoryMixin"]
