"""PostgreSQL task, run, lease, and append-only event operations."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from CAD2BIM_backend.domain.control_plane import (
    ACTIVE_RUN_STATUSES,
    ConflictError,
    CreateTaskRequest,
    ExecutionMode,
    ExecutionProtocolStatus,
    FeedbackCommandStatus,
    FeedbackDeliveryMode,
    FeedbackScope,
    SubmitFeedbackCommand,
    IdempotencyConflictError,
    LeaseConflictError,
    ResourceNotFoundError,
    RunRecord,
    RunStatus,
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
from CAD2BIM_backend.domain.identity import stable_internal_identity_id
from CAD2BIM_backend.domain.artifact_refs import FinalIFCArtifactRef
from CAD2BIM_backend.domain.run_events import (
    RunEvent,
    RunEventAudience,
    RunEventName,
    RunEventScope,
    RunEventType,
    make_run_status_event,
    make_task_title_event,
    reclaimed_runtime_terminal_events,
    validate_run_event_stream,
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
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactModel,
    ArtifactVersionModel,
    CheckpointLinkModel,
    CriticalOperationModel,
    FeedbackCommandModel,
    InterruptModel,
    McpSessionModel,
    RunEventModel,
    RunModel,
    RunResumeOperationModel,
    TaskModel,
    TaskTenantBindingModel,
    UserModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository_base import (
    PostgresRepositoryBase,
    as_utc,
    database_now,
    event_fingerprint,
    fingerprint,
    json_value,
    utcnow,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.postgres.runtime_roles import require_system_curator
from CAD2BIM_backend.ports.identity import IdentityAccessDenied
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantContextMismatch,
    bind_tenant_execution_context,
    current_tenant_execution_context,
    set_tenant_execution_context,
)
from CAD2BIM_backend.domain.identity import (
    TenantActorKind,
    TenantExecutionContext,
)


class PostgresTaskRunRepositoryMixin(PostgresRepositoryBase):
    async def set_generated_task_title(self, grant: RunnerCheckpointGrant, title: str) -> TaskRecord:
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, grant.scope.identity)
            run = await self._require_feedback_claim(session, grant)
            if run.module != "component":
                raise LeaseConflictError("Task naming requires the current Component claim")
            task = await self._require_task(session, run.owner_id, run.task_id, for_update=True)
            await self._require_task_identity(session, task, grant.scope.identity)
            if task.metadata_json.get("component_title_generated") is True:
                return self._task_record(task)
            now = await database_now(session)
            task.title = title
            task.metadata_json = {**task.metadata_json, "component_title_generated": True}
            task.updated_at = now
            await self._append_events(session, run, (
                make_task_title_event(self._run_record(run), occurred_at=now),
            ))
            await session.flush()
            return self._task_record(task)

    async def open_system_family_publication_scope(
        self, context: TenantExecutionContext, publication_name: str,
    ) -> InvocationScope:
        """Open a durable management Task/Run without a Direct Session binding."""
        if context.actor_kind is not TenantActorKind.SYSTEM_CURATOR or context.task_id is not None:
            raise IdentityAccessDenied("management scope requires a resolved curator identity")
        if not publication_name.strip() or len(publication_name) > 200:
            raise ValueError("publication_name requires 1..200 nonblank characters")
        request_key = f"system-library:{publication_name}"
        with bind_tenant_execution_context(context):
            async with self._sessions() as session:
                await require_system_curator(session)
            task = await self.create_task(context.tenant_id, CreateTaskRequest(
                title=f"SYSTEM library publication: {publication_name}",
                active_module="system_library", idempotency_key=request_key,
            ))
        identity = context.model_copy(update={"task_id": task.task_id})
        with bind_tenant_execution_context(identity):
            async with self._sessions() as session, session.begin():
                await set_tenant_execution_context(session, identity)
                await require_system_curator(session)
                task_model = await self._require_task(
                    session, context.tenant_id, task.task_id, for_update=True,
                )
                await self._require_task_identity(session, task_model, identity)
                run = await session.scalar(select(RunModel).where(
                    RunModel.owner_id == context.tenant_id,
                    RunModel.task_id == task.task_id,
                    RunModel.idempotency_key == request_key,
                ))
                if run is None:
                    now = await database_now(session)
                    run_id = f"run_{uuid4().hex}"
                    run = RunModel(
                        run_id=run_id, owner_id=context.tenant_id,
                        tenant_id=context.tenant_id, task_id=task.task_id,
                        module="system_library", stage_key="publication", input_payload={},
                        idempotency_key=request_key,
                        request_fingerprint=fingerprint({"publication_name": publication_name}),
                        thread_id=f"thread_{run_id}", execution_mode=ExecutionMode.DIRECT_MCP.value,
                        execution_protocol_status="not_applicable", execution_protocol=None,
                        status=RunStatus.RUNNING.value,
                        created_at=now, updated_at=now, started_at=now,
                    )
                    session.add(run)
                    await session.flush()
                elif (run.module != "system_library" or run.stage_key != "publication"
                      or run.execution_mode != ExecutionMode.DIRECT_MCP.value):
                    raise ConflictError("management Run changed its execution responsibility")
                return InvocationScope(identity=identity, owner_id=context.tenant_id,
                    run_id=run.run_id, execution_mode=ExecutionMode.DIRECT_MCP,
                    catalog_frontier=None)

    async def create_task(self, owner_id: str, request: CreateTaskRequest) -> TaskRecord:
        context = current_tenant_execution_context()
        if context is not None and owner_id != context.tenant_id:
            raise TenantContextMismatch("task owner does not match the active tenant")
        try:
            async with self._sessions() as session, session.begin():
                await self._ensure_owner(session, owner_id)
                if request.idempotency_key:
                    replay = await session.scalar(
                        select(TaskModel).where(
                            TaskModel.owner_id == owner_id,
                            TaskModel.idempotency_key == request.idempotency_key,
                        )
                    )
                    if replay is not None:
                        self._assert_replay(replay, request)
                        await self._require_task_identity(session, replay, context)
                        if replay.status == TaskStatus.ARCHIVED.value:
                            raise ConflictError("task was deleted; use a new request identity")
                        return self._task_record(replay)
                now = utcnow()
                model = TaskModel(
                    task_id=f"task_{uuid4().hex}",
                    owner_id=owner_id,
                    title=request.title,
                    instruction=request.instruction,
                    metadata_json=json_value(request.metadata),
                    status=TaskStatus.DRAFT.value,
                    active_module=request.active_module,
                    workspace_uri=request.workspace_uri,
                    idempotency_key=request.idempotency_key,
                    request_fingerprint=(fingerprint(request) if request.idempotency_key else None),
                    created_at=now,
                    updated_at=now,
                )
                session.add(model)
                await session.flush()
                if context is not None:
                    session.add(
                        TaskTenantBindingModel(
                            tenant_id=context.tenant_id,
                            task_id=model.task_id,
                            principal_id=context.principal_id,
                            legacy_owner_id=owner_id,
                            created_at=now,
                        )
                    )
                    await session.flush()
                return self._task_record(model)
        except IntegrityError as exc:
            if request.idempotency_key:
                async with self._sessions() as session:
                    replay = await session.scalar(
                        select(TaskModel).where(
                            TaskModel.owner_id == owner_id,
                            TaskModel.idempotency_key == request.idempotency_key,
                        )
                    )
                    if replay is not None:
                        self._assert_replay(replay, request)
                        await self._require_task_identity(session, replay, context)
                        if replay.status == TaskStatus.ARCHIVED.value:
                            raise ConflictError("task was deleted; use a new request identity")
                        return self._task_record(replay)
            raise ConflictError("task could not be created") from exc

    @staticmethod
    async def _require_task_identity(
        session: AsyncSession,
        task: TaskModel,
        context: TenantExecutionContext | None,
    ) -> None:
        if context is None:
            return
        binding = await session.scalar(
            select(TaskTenantBindingModel).where(
                TaskTenantBindingModel.tenant_id == context.tenant_id,
                TaskTenantBindingModel.task_id == task.task_id,
                TaskTenantBindingModel.principal_id == context.principal_id,
            )
        )
        if binding is None:
            raise TenantContextMismatch("task identity binding does not match the caller")

    @staticmethod
    async def _ensure_owner(session: AsyncSession, owner_id: str) -> None:
        """Preserve the compatibility owner alias after identity resolution."""

        context = current_tenant_execution_context()
        if context is not None and context.actor_kind is TenantActorKind.SYSTEM_CURATOR:
            await require_system_curator(session)
            owner = await session.get(UserModel, owner_id)
            if owner is None or owner.tenant_id != context.tenant_id:
                raise TenantContextMismatch("resolved curator owner does not match the active tenant")
            return
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = postgres_insert(UserModel).values(
                owner_id=owner_id,
                tenant_id=(context.tenant_id if context is not None else None),
            )
            await session.execute(statement.on_conflict_do_nothing(index_elements=["owner_id"]))
            owner = await session.get(UserModel, owner_id)
            if context is not None and (
                owner is None or owner.tenant_id != context.tenant_id
            ):
                raise TenantContextMismatch("owner alias does not match the active tenant")
            return
        if await session.get(UserModel, owner_id) is None:
            session.add(UserModel(owner_id=owner_id))

    async def get_task(self, owner_id: str, task_id: str) -> TaskRecord | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(TaskModel).where(
                    TaskModel.owner_id == owner_id,
                    TaskModel.task_id == task_id,
                    TaskModel.status != TaskStatus.ARCHIVED.value,
                )
            )
            return self._task_record(model) if model is not None else None

    async def delete_task(self, owner_id: str, task_id: str) -> None:
        """Retire task access without deleting immutable or shared library assets."""
        try:
            async with self._sessions() as session, session.begin():
                task = await self._require_task(
                    session, owner_id, task_id, for_update=True, include_archived=True,
                )
                await self._require_task_identity(session, task, current_tenant_execution_context())
                if task.status == TaskStatus.ARCHIVED.value:
                    return
                # Lifecycle commands lock Run before Task. Do not wait on their
                # Run lock while holding Task; a busy deletion can safely retry.
                runs = (await session.scalars(select(RunModel).where(
                    RunModel.owner_id == owner_id, RunModel.task_id == task_id,
                ).order_by(RunModel.run_id).with_for_update(nowait=True))).all()
                if any(run.status == RunStatus.RUNNING.value for run in runs):
                    raise ConflictError("Pause the running task and wait for it to stop before deleting it.")
                unresolved = await session.scalar(select(CriticalOperationModel.operation_id).where(
                    CriticalOperationModel.owner_id == owner_id,
                    CriticalOperationModel.task_id == task_id,
                    CriticalOperationModel.status == CriticalOperationStatus.PREPARED.value,
                ).limit(1))
                if unresolved is not None:
                    raise ConflictError("This task has an unresolved write. Reconcile its original Operation before deleting it.")
                now = await database_now(session)
                for run in runs:
                    if RunStatus(run.status) not in TERMINAL_RUN_STATUSES:
                        await self._cancel_run_locked(session, run, now)
                task.status = TaskStatus.ARCHIVED.value
                task.updated_at = now
                await session.flush()
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == "55P03":
                raise ConflictError("This task is changing. Wait for the current action to finish and try deleting it again.") from None
            raise

    async def get_run_start_replay(
        self,
        owner_id: str,
        task_id: str,
        request: StartRunRequest,
    ) -> RunRecord | None:
        async with self._sessions() as session:
            await self._require_task(session, owner_id, task_id)
            replay = await session.scalar(
                select(RunModel).where(
                    RunModel.task_id == task_id,
                    RunModel.idempotency_key == request.idempotency_key,
                )
            )
            if replay is None:
                return None
            self._assert_run_start_replay(replay, request)
            return self._run_record(replay)

    async def get_resume_run_replay(
        self,
        owner_id: str,
        task_id: str,
        savepoint_id: str,
        request: StartRunRequest,
    ) -> RunRecord | None:
        async with self._sessions() as session:
            await self._require_task(session, owner_id, task_id)
            replay = await session.scalar(
                select(RunModel).where(
                    RunModel.task_id == task_id,
                    RunModel.idempotency_key == request.idempotency_key,
                )
            )
            if replay is None:
                return None
            await self._require_exact_resume_replay(
                session,
                replay=replay,
                owner_id=owner_id,
                task_id=task_id,
                savepoint_id=savepoint_id,
                request=request,
            )
            return self._run_record(replay)

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
        try:
            async with self._sessions() as session, session.begin():
                task = await self._require_task(session, owner_id, task_id, for_update=True)
                request_identity = new_run_request_identity(
                    request,
                    resume_from_savepoint_id=resume_from_savepoint_id,
                )
                replay = await session.scalar(
                    select(RunModel).where(
                        RunModel.task_id == task_id,
                        RunModel.idempotency_key == request.idempotency_key,
                    )
                )
                if replay is not None:
                    self._assert_run_start_replay(
                        replay,
                        request,
                        resume_from_savepoint_id=resume_from_savepoint_id,
                    )
                    return self._run_record(replay)
                active = await session.scalar(
                    select(RunModel.run_id).where(
                        RunModel.task_id == task_id,
                        RunModel.status.in_(status.value for status in ACTIVE_RUN_STATUSES),
                    )
                )
                if active is not None:
                    raise ConflictError("task already has an active run")
                execution_protocol = execution_protocol_factory()
                now = utcnow()
                resolved_manifest = (
                    input_manifest_factory(execution_protocol)
                    if input_manifest_factory is not None
                    else input_manifest
                )
                model = self._new_run_model(
                    owner_id=owner_id,
                    task_id=task_id,
                    request=request,
                    run_id=run_id or f"run_{uuid4().hex}",
                    input_manifest=resolved_manifest,
                    execution_protocol=execution_protocol,
                    request_identity=request_identity,
                    resume_from_savepoint_id=resume_from_savepoint_id,
                    now=now,
                )
                session.add(model)
                task.status = TaskStatus.RUNNING.value
                task.updated_at = now
                await session.flush()
                if request.content:
                    context = current_tenant_execution_context()
                    if context is None or context.task_id != task_id:
                        raise ConflictError("initial input requires an authenticated Task identity")
                    receipt = await self._accept_feedback_locked(
                        session, model, context,
                        SubmitFeedbackCommand(client_request_id=stable_internal_identity_id(
                            "initial_", "initial-input", model.run_id, request.idempotency_key),
                            delivery_mode=FeedbackDeliveryMode.STEER, content=request.content,
                            scope=FeedbackScope(run_id=model.run_id)))
                    model.initial_feedback = receipt.model_dump(mode="json")
                    await session.flush()
                    await session.refresh(model)
                return self._run_record(model)
        except IntegrityError as exc:
            async with self._sessions() as session:
                replay = await session.scalar(
                    select(RunModel).where(
                        RunModel.task_id == task_id,
                        RunModel.idempotency_key == request.idempotency_key,
                    )
                )
                if replay is not None:
                    self._assert_run_start_replay(
                        replay,
                        request,
                        resume_from_savepoint_id=resume_from_savepoint_id,
                    )
                    return self._run_record(replay)
            raise ConflictError("run could not be created") from exc

    @classmethod
    def _assert_run_start_replay(
        cls,
        model: RunModel,
        request: StartRunRequest,
        *,
        resume_from_savepoint_id: str | None = None,
    ) -> None:
        identity = new_run_request_identity(
            request,
            resume_from_savepoint_id=resume_from_savepoint_id,
        )
        cls._assert_replay(model, identity)

    @staticmethod
    def _assert_resume_client_replay(
        model: RunModel,
        request: StartRunRequest,
        *,
        savepoint_id: str,
    ) -> None:
        requested = new_run_request_identity(
            request,
            resume_from_savepoint_id=savepoint_id,
        )
        persisted_request = StartRunRequest(
            module=model.module,
            stage_key=model.stage_key,
            input_payload=dict(model.input_payload or {}),
            idempotency_key=model.idempotency_key,
        )
        persisted = new_run_request_identity(
            persisted_request,
            resume_from_savepoint_id=model.resume_from_savepoint_id,
        )
        if fingerprint(requested) != fingerprint(persisted):
            raise IdempotencyConflictError(
                "idempotency key was reused with different input"
            )

    async def _require_exact_resume_replay(
        self,
        session: AsyncSession,
        *,
        replay: RunModel,
        owner_id: str,
        task_id: str,
        savepoint_id: str,
        request: StartRunRequest,
    ) -> RunResumeOperationModel:
        self._assert_resume_client_replay(replay, request, savepoint_id=savepoint_id)
        try:
            operation = await self._require_resume_operation_model(session, replay.run_id)
        except ResourceNotFoundError as exc:
            raise ConflictError("resumed run is missing its resume operation") from exc
        if (
            operation.owner_id != owner_id
            or operation.task_id != task_id
            or operation.savepoint_id != savepoint_id
            or replay.owner_id != owner_id
            or replay.task_id != task_id
            or replay.resume_from_savepoint_id != savepoint_id
        ):
            raise ConflictError("resumed run identity does not match its resume operation")

        savepoint = await session.scalar(
            select(CheckpointLinkModel).where(
                CheckpointLinkModel.owner_id == owner_id,
                CheckpointLinkModel.task_id == task_id,
                CheckpointLinkModel.savepoint_id == savepoint_id,
            )
        )
        if savepoint is None:
            raise ConflictError("resumed run source savepoint is unavailable")
        source = await session.scalar(
            select(RunModel).where(
                RunModel.owner_id == owner_id,
                RunModel.task_id == task_id,
                RunModel.run_id == savepoint.run_id,
            )
        )
        if source is None:
            raise ConflictError("resumed run source Run is unavailable")

        source_run = self._run_record(source)
        target_run = self._run_record(replay)
        require_exact_resume_request(
            source_run,
            savepoint_thread_id=savepoint.thread_id,
            request=request,
        )
        require_exact_resume_target(
            source_run,
            savepoint_thread_id=savepoint.thread_id,
            target_run=target_run,
        )
        if (
            source_run.execution_protocol_status is not ExecutionProtocolStatus.PINNED
            or target_run.execution_protocol_status is not ExecutionProtocolStatus.PINNED
            or source_run.execution_protocol is None
            or target_run.execution_protocol != source_run.execution_protocol
        ):
            raise ConflictError("resumed run execution protocol does not match the source Run")
        return operation

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
        request_identity = {
            **new_run_request_identity(
                request,
                resume_from_savepoint_id=savepoint_id,
            ),
            "command_fingerprint": command_fingerprint,
            "source_checkpoint_cursor": source_checkpoint_cursor.model_dump(mode="json"),
        }
        try:
            async with self._sessions() as session, session.begin():
                task = await self._require_task(session, owner_id, task_id, for_update=True)
                replay = await session.scalar(
                    select(RunModel).where(
                        RunModel.task_id == task_id,
                        RunModel.idempotency_key == request.idempotency_key,
                    )
                )
                if replay is not None:
                    self._assert_replay(replay, request_identity)
                    operation = await self._require_exact_resume_replay(
                        session,
                        replay=replay,
                        owner_id=owner_id,
                        task_id=task_id,
                        savepoint_id=savepoint_id,
                        request=request,
                    )
                    return self._run_record(replay), self._resume_operation_record(operation)

                savepoint = await session.scalar(
                    select(CheckpointLinkModel).where(
                        CheckpointLinkModel.owner_id == owner_id,
                        CheckpointLinkModel.task_id == task_id,
                        CheckpointLinkModel.savepoint_id == savepoint_id,
                    )
                )
                if savepoint is None:
                    raise ResourceNotFoundError("savepoint not found")
                self._require_source_cursor(savepoint, source_checkpoint_cursor)
                source_model = await session.scalar(
                    select(RunModel).where(
                        RunModel.owner_id == owner_id,
                        RunModel.task_id == task_id,
                        RunModel.run_id == savepoint.run_id,
                    ).with_for_update()
                )
                if (
                    source_model is None
                    or RunStatus(source_model.status) not in TERMINAL_RUN_STATUSES
                ):
                    raise ConflictError("source run must be terminal before savepoint resume")
                source_run = self._run_record(source_model)
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
                active = await session.scalar(
                    select(RunModel.run_id).where(
                        RunModel.task_id == task_id,
                        RunModel.status.in_(status.value for status in ACTIVE_RUN_STATUSES),
                    )
                )
                if active is not None:
                    raise ConflictError("task already has an active run")
                now = utcnow()
                run_id = f"run_{uuid4().hex}"
                run = self._new_run_model(
                    owner_id=owner_id,
                    task_id=task_id,
                    request=request,
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
                    request_identity=request_identity,
                    resume_from_savepoint_id=savepoint_id,
                    now=now,
                )
                operation = RunResumeOperationModel(
                    run_id=run.run_id,
                    operation_id=operation_id,
                    owner_id=owner_id,
                    task_id=task_id,
                    savepoint_id=savepoint_id,
                    target_mcp_session_id=None,
                    command_fingerprint=command_fingerprint,
                    source_checkpoint_cursor=json_value(source_checkpoint_cursor),
                    phase=ResumeOperationPhase.INTENT.value,
                    observed_checkpoint_cursor=None,
                    observed_outcome=None,
                    attempt_count=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add_all((run, operation))
                task.status = TaskStatus.RUNNING.value
                task.updated_at = now
                await session.flush()
                target_run = self._run_record(run)
                require_exact_resume_target(
                    source_run,
                    savepoint_thread_id=savepoint.thread_id,
                    target_run=target_run,
                )
                return target_run, self._resume_operation_record(operation)
        except IntegrityError as exc:
            async with self._sessions() as session:
                replay = await session.scalar(
                    select(RunModel).where(
                        RunModel.task_id == task_id,
                        RunModel.idempotency_key == request.idempotency_key,
                    )
                )
                if replay is not None:
                    self._assert_replay(replay, request_identity)
                    operation = await self._require_exact_resume_replay(
                        session,
                        replay=replay,
                        owner_id=owner_id,
                        task_id=task_id,
                        savepoint_id=savepoint_id,
                        request=request,
                    )
                    return self._run_record(replay), self._resume_operation_record(operation)
            raise ConflictError("resumed run could not be created") from exc

    async def get_resume_operation(
        self, owner_id: str, run_id: str
    ) -> ResumeOperationRecord | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(RunResumeOperationModel).where(
                    RunResumeOperationModel.owner_id == owner_id,
                    RunResumeOperationModel.run_id == run_id,
                )
            )
            return self._resume_operation_record(model) if model is not None else None

    async def bind_resume_session(
        self,
        run_id: str,
        runner_id: str,
        lease_token: str,
        session_id: str,
    ) -> ResumeOperationRecord:
        if not session_id:
            raise ValueError("session_id must not be empty")
        async with self._sessions() as session, session.begin():
            run = await self._require_lease(session, run_id, runner_id, lease_token)
            operation = await self._require_resume_operation_model(session, run_id, for_update=True)
            if operation.target_mcp_session_id == session_id:
                return self._resume_operation_record(operation)
            if operation.target_mcp_session_id is not None:
                raise ConflictError("resume operation is already bound to another MCP session")
            if ResumeOperationPhase(operation.phase) not in {
                ResumeOperationPhase.INTENT,
                ResumeOperationPhase.GRAPH_INFLIGHT,
            }:
                raise ConflictError("resume session cannot be bound after graph observation")
            target = await session.scalar(
                select(McpSessionModel.session_id).where(
                    McpSessionModel.owner_id == run.owner_id,
                    McpSessionModel.task_id == run.task_id,
                    McpSessionModel.run_id == run_id,
                    McpSessionModel.session_id == session_id,
                )
            )
            if target is None:
                raise ResourceNotFoundError("resume target MCP session not found")
            operation.target_mcp_session_id = session_id
            operation.updated_at = await database_now(session)
            await session.flush()
            return self._resume_operation_record(operation)

    async def mark_resume_graph_inflight(
        self,
        run_id: str,
        runner_id: str,
        lease_token: str,
        operation_id: str,
    ) -> ResumeOperationRecord:
        async with self._sessions() as session, session.begin():
            await self._require_lease(session, run_id, runner_id, lease_token)
            operation = await self._require_resume_operation_model(session, run_id, for_update=True)
            self._require_operation_id(operation, operation_id)
            phase = ResumeOperationPhase(operation.phase)
            if phase == ResumeOperationPhase.GRAPH_INFLIGHT:
                return self._resume_operation_record(operation)
            if phase != ResumeOperationPhase.INTENT:
                raise ConflictError("resume operation cannot re-enter graph execution")
            if operation.target_mcp_session_id is None:
                raise ConflictError("resume operation has no bound target MCP session")
            operation.phase = ResumeOperationPhase.GRAPH_INFLIGHT.value
            operation.attempt_count += 1
            operation.updated_at = await database_now(session)
            await session.flush()
            return self._resume_operation_record(operation)

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
        async with self._sessions() as session, session.begin():
            run = await self._require_lease(session, run_id, runner_id, lease_token)
            if checkpoint_cursor.thread_id != run.thread_id:
                raise ConflictError("observed checkpoint belongs to another Run thread")
            operation = await self._require_resume_operation_model(session, run_id, for_update=True)
            self._require_operation_id(operation, operation_id)
            phase = ResumeOperationPhase(operation.phase)
            if phase in {
                ResumeOperationPhase.GRAPH_OBSERVED,
                ResumeOperationPhase.FINALIZED,
            }:
                if (
                    self._operation_observed_cursor(operation) == checkpoint_cursor
                    and operation.observed_outcome == observed_outcome.value
                ):
                    return self._resume_operation_record(operation)
                raise ConflictError("resume operation already has another graph observation")
            if phase != ResumeOperationPhase.GRAPH_INFLIGHT:
                raise ConflictError("resume operation is not executing in the graph")
            operation.phase = ResumeOperationPhase.GRAPH_OBSERVED.value
            operation.observed_checkpoint_cursor = json_value(checkpoint_cursor)
            operation.observed_outcome = observed_outcome.value
            operation.updated_at = await database_now(session)
            await session.flush()
            return self._resume_operation_record(operation)

    async def finalize_resume_operation(
        self,
        run_id: str,
        runner_id: str,
        lease_token: str,
        operation_id: str,
    ) -> ResumeOperationRecord:
        async with self._sessions() as session, session.begin():
            await self._require_lease(session, run_id, runner_id, lease_token)
            operation = await self._require_resume_operation_model(session, run_id, for_update=True)
            self._require_operation_id(operation, operation_id)
            self._finalize_resume_operation_model(operation, await database_now(session))
            await session.flush()
            return self._resume_operation_record(operation)

    async def get_run(self, owner_id: str, run_id: str) -> RunRecord | None:
        async with self._sessions() as session:
            try:
                model = await self._require_run(session, owner_id, run_id)
            except ResourceNotFoundError:
                return None
            return self._run_record(model)

    async def get_run_with_execution_identity(
        self, owner_id: str, run_id: str
    ) -> RunRecord | None:
        async with self._sessions() as session:
            try:
                model = await self._require_run(session, owner_id, run_id)
            except ResourceNotFoundError:
                return None
            return await self._run_record_with_execution_identity(session, model)

    async def get_observed_run(self, run_id: str, thread_id: str):
        async with self._sessions() as session, session.begin():
            active = current_tenant_execution_context()
            if active is not None:
                await set_tenant_execution_context(session, active)
                run = await self._require_run(session, active.tenant_id, run_id)
                if run.task_id != active.task_id or run.tenant_id != active.tenant_id:
                    raise ResourceNotFoundError("observed Run is outside the active identity")
                identity = active
            elif session.bind is not None and session.bind.dialect.name == "postgresql":
                row = (await session.execute(text(
                    "SELECT tenant_id, principal_id, task_id FROM cad2bim_observed_run_identity(:run_id, :thread_id)"
                ), {"run_id": run_id, "thread_id": thread_id})).mappings().one_or_none()
                if row is None:
                    raise ResourceNotFoundError("observed Run not found")
                identity = TenantExecutionContext(tenant_id=row["tenant_id"], principal_id=row["principal_id"],
                    task_id=row["task_id"], actor_kind=TenantActorKind.RUNNER,
                    command_id=f"observe:{run_id}")
                await set_tenant_execution_context(session, identity)
                run = await self._require_run(session, identity.tenant_id, run_id)
            else:
                run = await session.scalar(select(RunModel).where(RunModel.run_id == run_id, RunModel.thread_id == thread_id))
                if run is None:
                    raise ResourceNotFoundError("observed Run not found")
                await self._require_task(session, run.owner_id, run.task_id)
                record = await self._run_record_with_execution_identity(session, run)
                tenant_id, principal_id = record.require_execution_identity()
                identity = TenantExecutionContext(tenant_id=tenant_id, principal_id=principal_id,
                    task_id=run.task_id, actor_kind=TenantActorKind.RUNNER, command_id=f"observe:{run_id}")
            if run.thread_id != thread_id:
                raise ConflictError("observed checkpoint belongs to another Run thread")
            return identity, await self._run_record_with_execution_identity(session, run)

    async def pause_run(self, owner_id: str, run_id: str, *, client_request_id: str) -> RunRecord:
        async with self._sessions() as session, session.begin():
            run = await self._require_run(session, owner_id, run_id, for_update=True)
            if run.pause_request_id == client_request_id or run.status == RunStatus.PAUSED or run.pause_requested:
                return self._run_record(run)
            now = await database_now(session)
            if run.status == RunStatus.RUNNING:
                run.pause_requested = True
            else:
                require_run_transition(RunStatus(run.status), RunStatus.PAUSED)
                run.status = RunStatus.PAUSED.value
                run.pause_requested = False
                await self._set_task_status(session, run.task_id, TaskStatus.PAUSED, now)
            run.updated_at = now
            run.pause_request_id = client_request_id
            await self._append_events(session, run, (make_run_status_event(
                source_event_key=f"pause:{client_request_id}", task_id=run.task_id, run_id=run.run_id,
                module=run.module, stage_key=run.stage_key, run_status=RunStatus(run.status),
                pause_requested=run.pause_requested,
                reason="pause_requested" if run.pause_requested else "paused", occurred_at=now,
            ),))
            await session.flush()
            return self._run_record(run)

    async def continue_run(self, owner_id: str, run_id: str, *, client_request_id: str) -> RunRecord:
        async with self._sessions() as session, session.begin():
            run = await self._require_run(session, owner_id, run_id, for_update=True)
            if run.continue_request_id == client_request_id:
                return self._run_record(run)
            if run.status != RunStatus.PAUSED.value:
                raise ConflictError("only a paused Run can continue")
            now = await database_now(session)
            gate = await session.scalar(select(InterruptModel).where(
                InterruptModel.run_id == run.run_id,
                InterruptModel.status == "pending"
            ).order_by(InterruptModel.created_at.desc()).limit(1))
            target = RunStatus.QUEUED
            if gate is not None:
                from CAD2BIM_backend.domain.system_interrupts import (
                    SystemInterruptEnvelope, SystemInterruptResponseReceipt)
                envelope = SystemInterruptEnvelope.model_validate(gate.payload)
                if envelope.system_interrupt.kind.value == "run_paused":
                    high = await session.scalar(select(func.max(FeedbackCommandModel.accepted_sequence))
                                                .where(FeedbackCommandModel.run_id == run.run_id))
                    receipt = SystemInterruptResponseReceipt(
                        interrupt_id=gate.interrupt_id,
                        action="continue", response_high_watermark=high or 0,
                        responded_at=now)
                    operation_id = stable_internal_identity_id(
                        "continue_operation_", "run-continue", run.run_id, gate.interrupt_id, client_request_id)
                    gate.status = "responded"
                    gate.response = {"origin": "run_continue", "receipt": receipt.model_dump(mode="json"),
                                     "resume_selection": {"operation_id": operation_id}}
                    gate.response_idempotency_key = client_request_id
                    gate.responded_at = now
                elif not run.feedback_pending:
                    target = RunStatus.WAITING_USER
            require_run_transition(RunStatus.PAUSED, target)
            run.status = target.value
            run.pause_requested = False
            run.continue_request_id = client_request_id
            run.error = None
            run.updated_at = now
            await self._set_task_status(session, run.task_id,
                TaskStatus.WAITING_USER if target is RunStatus.WAITING_USER else TaskStatus.RUNNING, now)
            await self._append_events(session, run, (make_run_status_event(
                source_event_key=f"continue:{client_request_id}", task_id=run.task_id, run_id=run.run_id,
                module=run.module, stage_key=run.stage_key, run_status=target,
                pause_requested=False, reason="continued", occurred_at=now,
            ),))
            await session.flush()
            return self._run_record(run)

    async def list_tasks(self, owner_id: str, *, after_task_id: str | None = None, limit: int = 50):
        if not 1 <= limit <= 200:
            raise ValueError("invalid task page size")
        async with self._sessions() as session:
            tasks = (await session.scalars(select(TaskModel).where(
                TaskModel.owner_id == owner_id,
                TaskModel.status != TaskStatus.ARCHIVED.value,
                TaskModel.task_id > after_task_id if after_task_id is not None else True
            ).order_by(TaskModel.task_id).limit(limit))).all()
            result = []
            for task in tasks:
                latest = await session.scalar(select(RunModel).where(RunModel.task_id == task.task_id)
                                              .order_by(RunModel.created_at.desc(), RunModel.run_id.desc()).limit(1))
                result.append((self._task_record(task), self._run_record(latest) if latest else None))
            return tuple(result)

    async def read_chat_snapshot(self, owner_id: str, run_id: str, *,
                                 before_sequence: int | None = None, limit: int = 100):
        if not 1 <= limit <= 500 or (before_sequence is not None and before_sequence < 1):
            raise ValueError("invalid chat snapshot page")
        async with self._sessions() as session, session.begin():
            run = await self._require_run(session, owner_id, run_id, for_update=True)
            high = await session.scalar(select(func.max(RunEventModel.sequence)).where(RunEventModel.run_id == run_id))
            rows = (await session.scalars(select(RunEventModel).where(
                RunEventModel.run_id == run_id, RunEventModel.audience == RunEventAudience.PUBLIC.value,
                RunEventModel.sequence < before_sequence if before_sequence is not None else True
            ).order_by(RunEventModel.sequence.desc()).limit(limit + 1))).all()
            selected = rows[:limit]
            next_before = selected[-1].sequence if len(rows) > limit else None
            boundary = int(high or 0)
            item_ids = {row.item_id for row in selected if (
                row.event_type.startswith(("chat.", "tool.call."))
                or row.event_type == "feedback.accepted" or row.attributes.get("source_kind") == "skill"
            )}
            call_ids = {row.attributes["call_id"] for row in selected if row.attributes.get("call_id")}
            stopped_at = await session.scalar(select(func.max(RunEventModel.sequence)).where(
                RunEventModel.run_id == run_id, RunEventModel.audience == RunEventAudience.PUBLIC.value,
                RunEventModel.sequence <= boundary, RunEventModel.event_type == "run.status.changed",
                RunEventModel.attributes["run_status"].as_string() != RunStatus.RUNNING.value,
            ))
            cycle_start = int(stopped_at or 0)
            recent_start = max(0, boundary - 100)
            activity = and_(RunEventModel.event_type == "runtime.activity.changed", or_(
                RunEventModel.sequence > cycle_start,
                RunEventModel.attributes["call_id"].as_string().in_(tuple(call_ids)),
            ))
            recent_tools = and_(RunEventModel.event_type.like("tool.call.%"),
                                RunEventModel.sequence > recent_start)
            related = (await session.scalars(select(RunEventModel).where(
                RunEventModel.run_id == run_id, RunEventModel.audience == RunEventAudience.PUBLIC.value,
                RunEventModel.sequence <= boundary,
                or_(RunEventModel.item_id.in_(tuple(item_ids)), activity, recent_tools,
                    RunEventModel.sequence == cycle_start),
            ).order_by(RunEventModel.sequence))).all()
            histories = {row.sequence: row for row in selected}
            activity_events = []
            for row in related:
                if row.item_id in item_ids:
                    histories[row.sequence] = row
                if (row.event_type == "runtime.activity.changed" or row.sequence == cycle_start
                    or (row.event_type.startswith("tool.call.") and row.sequence > recent_start)):
                    activity_events.append(self._event_record(row))
            return (self._run_record(run), tuple(self._event_record(histories[sequence])
                    for sequence in sorted(histories)), boundary, next_before, tuple(activity_events))

    async def get_item_events(self, owner_id: str, run_id: str, item_id: str) -> tuple[RunEvent, ...]:
        async with self._sessions() as session:
            await self._require_run(session, owner_id, run_id)
            rows = (await session.scalars(select(RunEventModel).where(
                RunEventModel.run_id == run_id, RunEventModel.item_id == item_id,
                RunEventModel.audience == RunEventAudience.PUBLIC.value
            ).order_by(RunEventModel.sequence))).all()
            return tuple(self._event_record(row) for row in rows)

    async def append_chat_events(self, owner_id: str, run_id: str, events: Sequence[RunEvent]) -> tuple[RunEvent, ...]:
        async with self._sessions() as session, session.begin():
            run = await self._require_run(session, owner_id, run_id, for_update=True)
            existing = (await session.scalars(select(RunEventModel).where(
                RunEventModel.run_id == run_id,
                RunEventModel.source_event_id.in_(tuple(event.event_id for event in events))
            ))).all() if events else []
            by_id = {row.source_event_id: self._event_record(row) for row in existing}
            # Parent and child observations may point to different saved copies.
            # The first event's payload reference remains the published source.
            canonical = []
            for event in events:
                original = by_id.get(event.event_id)
                if original is None:
                    canonical.append(event)
                    continue
                attributes = dict(event.attributes)
                for location in ("checkpoint_ns", "message_index", "memory_slot"):
                    if location in original.attributes:
                        attributes[location] = original.attributes[location]
                    else:
                        attributes.pop(location, None)
                canonical.append(event.model_copy(update={"payload_ref": original.payload_ref,
                    "occurred_at": original.occurred_at, "attributes": attributes}))
            return await self._append_events(session, run, canonical)

    async def cancel_run(self, owner_id: str, run_id: str) -> RunRecord:
        async with self._sessions() as session, session.begin():
            run = await self._require_run(session, owner_id, run_id, for_update=True)
            status = RunStatus(run.status)
            if status == RunStatus.CANCELLED or run.cancel_requested:
                return self._run_record(run)
            now = await database_now(session)
            await self._cancel_run_locked(session, run, now)
            await session.flush()
            return self._run_record(run)

    async def _cancel_run_locked(self, session: AsyncSession, run: RunModel, now: datetime) -> None:
        status = RunStatus(run.status)
        if status == RunStatus.RUNNING:
            run.cancel_requested = True
        else:
            require_run_transition(status, RunStatus.CANCELLED)
            run.status = RunStatus.CANCELLED.value
            run.cancel_requested = True
            run.ended_at = now
            pending_gates = (await session.scalars(select(InterruptModel).where(
                InterruptModel.run_id == run.run_id, InterruptModel.status == "pending",
            ))).all()
            for gate in pending_gates:
                gate.status = "responded"
                gate.response = {"origin": "run_cancelled"}
                gate.responded_at = now
            await self._supersede_undelivered_feedback(session, run, now)
            await self._set_task_status(session, run.task_id, TaskStatus.CANCELLED, now)
        run.updated_at = now

    async def claim_next_run(self, runner_id: str, *, lease_seconds: int = 30) -> RunRecord | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        async with self._sessions() as session, session.begin():
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                claimed = await self._claim_next_tenant_run(
                    session,
                    runner_id,
                    lease_seconds=lease_seconds,
                )
                if claimed is not None:
                    return claimed
                is_superuser = await session.scalar(
                    text(
                        "SELECT rolsuper FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
                if not is_superuser:
                    return None
            now = await database_now(session)
            run = await session.scalar(self._claim_statement(now))
            if run is None:
                return None
            expired = RunStatus(run.status) is RunStatus.RUNNING
            deferred_restore = (
                RunStatus(run.status) is RunStatus.QUEUED
                and run.restore_defer_reason_code == "mcp_restore_transient"
            )
            # A deferred queued re-claim remains in the same recovery cycle.
            # Any newly expired running lease starts a new cycle, including
            # a prior recovery worker that itself lost the lease.
            new_recovery_cycle = expired
            run.status = RunStatus.RUNNING.value
            run.claimed_by = runner_id
            run.lease_token = f"lease_{uuid4().hex}"
            run.lease_expires_at = now + timedelta(seconds=lease_seconds)
            run.heartbeat_at = now
            run.claim_count += 1
            run.writer_generation += 1
            run.reclaimed_lease = expired or deferred_restore
            run.claim_not_before = None
            if new_recovery_cycle:
                run.recovery_generation += 1
                run.restore_defer_count = 0
            run.restore_defer_reason_code = None
            run.started_at = run.started_at or now
            run.updated_at = now
            if not expired:
                await self._append_events(session, run, (make_run_status_event(
                    source_event_key=f"claim:{run.writer_generation}", task_id=run.task_id, run_id=run.run_id,
                    module=run.module, stage_key=run.stage_key, run_status=RunStatus.RUNNING,
                    pause_requested=run.pause_requested, reason="claimed", occurred_at=now,
                ),))
            await session.flush()
            return await self._run_record_with_execution_identity(session, run)

    async def _claim_next_tenant_run(
        self,
        session: AsyncSession,
        runner_id: str,
        *,
        lease_seconds: int,
    ) -> RunRecord | None:
        result = await session.execute(
            text(
                "SELECT run_id, task_id, owner_id, tenant_id, principal_id, "
                "lease_token, reclaimed_lease, writer_generation "
                "FROM cad2bim_claim_next_run(:runner_id, :lease_seconds)"
            ),
            {"runner_id": runner_id, "lease_seconds": lease_seconds},
        )
        claimed = result.mappings().one_or_none()
        if claimed is None:
            return None
        context = TenantExecutionContext(
            tenant_id=claimed["tenant_id"],
            principal_id=claimed["principal_id"],
            task_id=claimed["task_id"],
            actor_kind=TenantActorKind.RUNNER,
            command_id=f"claim:{claimed['run_id']}:{claimed['lease_token']}",
        )
        await set_tenant_execution_context(session, context)
        run = await session.scalar(
            select(RunModel).where(
                RunModel.run_id == claimed["run_id"],
                RunModel.task_id == claimed["task_id"],
                RunModel.owner_id == claimed["owner_id"],
                RunModel.tenant_id == claimed["tenant_id"],
            )
        )
        if run is None or run.lease_token != claimed["lease_token"]:
            raise ConflictError("claimed Run could not be restored under tenant RLS")
        # The original claim function clears the defer count only for an expired
        # running lease; a deferred queued claim retains its positive count.
        if not run.reclaimed_lease or run.restore_defer_count > 0:
            await self._append_events(session, run, (make_run_status_event(
                source_event_key=f"claim:{run.writer_generation}", task_id=run.task_id, run_id=run.run_id,
                module=run.module, stage_key=run.stage_key, run_status=RunStatus.RUNNING,
                pause_requested=run.pause_requested, reason="claimed", occurred_at=run.updated_at,
            ),))
        return self._run_record(run).model_copy(
            update={"principal_id": claimed["principal_id"]}
        )

    @staticmethod
    def _claim_statement(now: datetime):
        return (
            select(RunModel)
            .where(
                RunModel.execution_mode == ExecutionMode.GRAPH.value,
                RunModel.execution_protocol_status
                == ExecutionProtocolStatus.PINNED.value,
                or_(
                    and_(
                        RunModel.status == RunStatus.QUEUED.value,
                        or_(
                            RunModel.claim_not_before.is_(None),
                            RunModel.claim_not_before <= now,
                        ),
                    ),
                    and_(
                        RunModel.status == RunStatus.RUNNING.value,
                        RunModel.lease_expires_at.is_not(None),
                        RunModel.lease_expires_at <= now,
                    ),
                )
            )
            .order_by(RunModel.created_at, RunModel.run_id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )

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
        async with self._sessions() as session, session.begin():
            run = await self._require_lease(session, run_id, runner_id, lease_token)
            if run.writer_generation != expected_writer_generation:
                raise LeaseConflictError("writer generation changed")
            if run.recovery_generation != expected_recovery_generation:
                raise LeaseConflictError("recovery generation changed")
            if not run.reclaimed_lease or run.recovery_generation < 1:
                raise ConflictError("Run is not in exact reclaim recovery")
            require_run_transition(RunStatus(run.status), RunStatus.QUEUED)
            now = await database_now(session)
            run.status = RunStatus.QUEUED.value
            run.claimed_by = None
            run.lease_token = None
            run.lease_expires_at = None
            run.heartbeat_at = None
            run.reclaimed_lease = True
            run.claim_not_before = now + timedelta(seconds=delay_seconds)
            run.restore_defer_count += 1
            run.restore_defer_reason_code = reason_code
            run.error = None
            run.ended_at = None
            run.updated_at = now
            await session.flush()
            return self._run_record(run)

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
        async with self._sessions() as session, session.begin():
            run = await self._require_lease(session, run_id, runner_id, lease_token)
            now = await database_now(session)
            run.lease_expires_at = now + timedelta(seconds=lease_seconds)
            run.heartbeat_at = now
            run.updated_at = now
            await session.flush()
            return await self._run_record_with_execution_identity(session, run)

    async def _run_record_with_execution_identity(
        self,
        session: AsyncSession,
        run: RunModel,
    ) -> RunRecord:
        record = self._run_record(run)
        if run.tenant_id is None:
            return record
        principal_id = await session.scalar(
            select(TaskTenantBindingModel.principal_id).where(
                TaskTenantBindingModel.tenant_id == run.tenant_id,
                TaskTenantBindingModel.task_id == run.task_id,
            )
        )
        if principal_id is None:
            raise ConflictError("claimed Run has no persistent tenant identity")
        return record.model_copy(update={"principal_id": principal_id})

    async def pause_claimed(
        self, run_id: str, runner_id: str, lease_token: str, *, error: str | None = None,
    ) -> RunRecord:
        async with self._sessions() as session, session.begin():
            run = await self._require_lease(session, run_id, runner_id, lease_token)
            now = utcnow()
            require_run_transition(RunStatus(run.status), RunStatus.PAUSED)
            run.status = RunStatus.PAUSED.value
            run.pause_requested = False
            run.claimed_by = None
            run.lease_token = None
            run.lease_expires_at = None
            run.error = error
            run.updated_at = now
            await self._set_task_status(session, run.task_id, TaskStatus.PAUSED, now)
            await self._append_events(session, run, (make_run_status_event(
                source_event_key=f"paused-claim:{run.writer_generation}",
                task_id=run.task_id, run_id=run.run_id, module=run.module, stage_key=run.stage_key,
                run_status=RunStatus.PAUSED, pause_requested=False, reason=error or "paused", occurred_at=now,
            ),))
            await session.flush()
            return self._run_record(run)

    async def complete(
        self,
        run_id: str,
        runner_id: str,
        lease_token: str,
        *,
        events: Sequence[RunEvent] = (),
        final_artifact_ref: FinalIFCArtifactRef | None = None,
    ) -> RunRecord:
        async with self._sessions() as session, session.begin():
            run = await self._require_lease(session, run_id, runner_id, lease_token)
            now = utcnow()
            target = RunStatus.CANCELLED if run.cancel_requested else RunStatus.COMPLETED
            require_run_transition(RunStatus(run.status), target)
            effective_events = (
                tuple(
                    event
                    for event in events
                    if not (
                        event.event_type == RunEventName.RUN_STATUS_CHANGED.value
                        and event.audience is RunEventAudience.INTERNAL
                        and event.status is RunEventType.SUCCEEDED
                        and "terminal_root_checkpoint_id" in event.attributes
                    )
                )
                if target is RunStatus.CANCELLED
                else events
            )
            if target is RunStatus.COMPLETED:
                pending_gate = await session.scalar(
                    select(InterruptModel.interrupt_id).where(
                        InterruptModel.run_id == run_id,
                        InterruptModel.status == "pending",
                    ).limit(1)
                )
                if pending_gate is not None:
                    raise ConflictError("Run cannot complete with a pending Gate")
                unacknowledged_count = await session.scalar(
                    select(func.count())
                    .select_from(FeedbackCommandModel)
                    .where(
                        FeedbackCommandModel.run_id == run_id,
                        FeedbackCommandModel.status.in_(
                            (
                                FeedbackCommandStatus.ACCEPTED.value,
                                FeedbackCommandStatus.CLAIMED.value,
                            )
                        ),
                    )
                )
                if unacknowledged_count:
                    raise ConflictError("Run cannot complete with unacknowledged feedback")
            else:
                await self._supersede_undelivered_feedback(session, run, now)
            if final_artifact_ref is not None:
                if target is not RunStatus.COMPLETED:
                    raise ConflictError("cancelled Run cannot publish a final IFC Artifact")
                await self._require_final_artifact(session, run, final_artifact_ref)
            if run.resume_from_savepoint_id is not None:
                operation = await self._require_resume_operation_model(
                    session, run_id, for_update=True
                )
                self._finalize_resume_operation_model(operation, now)
            await self._append_events(session, run, effective_events)
            run.status = target.value
            if final_artifact_ref is not None:
                run.final_artifact_id = final_artifact_ref.artifact_id
                run.final_artifact_version_id = final_artifact_ref.version_id
                run.final_artifact = json_value(final_artifact_ref)
            run.claimed_by = None
            run.lease_token = None
            run.lease_expires_at = None
            run.updated_at = now
            run.ended_at = now
            task_status = (
                TaskStatus.CANCELLED if target == RunStatus.CANCELLED else TaskStatus.COMPLETED
            )
            await self._set_task_status(session, run.task_id, task_status, now)
            await session.flush()
            return self._run_record(run)

    async def _supersede_undelivered_feedback(self, session, run: RunModel, now: datetime) -> None:
        commands = (await session.scalars(
            select(FeedbackCommandModel).where(
                FeedbackCommandModel.run_id == run.run_id,
                FeedbackCommandModel.status.in_((
                    FeedbackCommandStatus.ACCEPTED.value,
                    FeedbackCommandStatus.CLAIMED.value,
                )),
            )
        )).all()
        for command in commands:
            command.status = FeedbackCommandStatus.SUPERSEDED.value
            # The membership table keeps the original batch/feedback linkage.
            command.claimed_batch_id = None
        run.feedback_pending = False

    async def fail(
        self,
        run_id: str,
        runner_id: str,
        lease_token: str,
        *,
        error: str,
        events: Sequence[RunEvent] = (),
    ) -> RunRecord:
        async with self._sessions() as session, session.begin():
            run = await self._require_lease(session, run_id, runner_id, lease_token)
            await self._append_events(session, run, events)
            now = utcnow()
            target = RunStatus.CANCELLED if run.cancel_requested else RunStatus.FAILED
            require_run_transition(RunStatus(run.status), target)
            if target is RunStatus.CANCELLED:
                await self._supersede_undelivered_feedback(session, run, now)
            run.status = target.value
            run.error = error
            run.claimed_by = None
            run.lease_token = None
            run.lease_expires_at = None
            run.updated_at = now
            run.ended_at = now
            task_status = (
                TaskStatus.CANCELLED if target == RunStatus.CANCELLED else TaskStatus.FAILED
            )
            await self._set_task_status(session, run.task_id, task_status, now)
            await session.flush()
            return self._run_record(run)

    async def append_events(
        self, owner_id: str, run_id: str, events: Sequence[RunEvent]
    ) -> tuple[RunEvent, ...]:
        async with self._sessions() as session, session.begin():
            run = await self._require_run(session, owner_id, run_id, for_update=True)
            return await self._append_events(session, run, events)

    async def reconcile_reclaimed_runtime_events(
        self,
        run_id: str,
        runner_id: str,
        lease_token: str,
    ) -> tuple[RunEvent, ...]:
        async with self._sessions() as session, session.begin():
            run = await self._require_lease(session, run_id, runner_id, lease_token)
            if not run.reclaimed_lease:
                return ()
            existing = (
                await session.scalars(
                    select(RunEventModel)
                    .where(RunEventModel.run_id == run_id)
                    .order_by(RunEventModel.sequence)
                )
            ).all()
            events = reclaimed_runtime_terminal_events(
                tuple(self._event_record(model) for model in existing),
                task_id=run.task_id,
                run_id=run.run_id,
                claim_count=run.claim_count,
                occurred_at=await database_now(session),
            )
            return await self._append_events(session, run, events)

    async def list_events(
        self,
        owner_id: str,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        through_sequence: int | None = None,
        audience: RunEventAudience | None = None,
    ) -> tuple[RunEvent, ...]:
        if after_sequence < 0 or limit < 1:
            raise ValueError("invalid event cursor")
        async with self._sessions() as session:
            await self._require_run(session, owner_id, run_id)
            models = (
                await session.scalars(
                    select(RunEventModel)
                    .where(
                        RunEventModel.owner_id == owner_id,
                        RunEventModel.run_id == run_id,
                        RunEventModel.sequence > after_sequence,
                        RunEventModel.sequence <= through_sequence if through_sequence is not None else True,
                        RunEventModel.audience == audience.value if audience is not None else True,
                    )
                    .order_by(RunEventModel.sequence)
                    .limit(limit)
                )
            ).all()
            return tuple(self._event_record(model) for model in models)

    async def get_completion_checkpoint_events(
        self, owner_id: str, run_id: str
    ) -> tuple[RunEvent, ...]:
        """Read all candidates for the one internal completed Run reference."""

        async with self._sessions() as session:
            await self._require_run(session, owner_id, run_id)
            models = (
                await session.scalars(
                    select(RunEventModel)
                    .where(
                        RunEventModel.owner_id == owner_id,
                        RunEventModel.run_id == run_id,
                        RunEventModel.event_type == RunEventName.RUN_STATUS_CHANGED.value,
                        RunEventModel.audience == RunEventAudience.INTERNAL.value,
                        RunEventModel.event == RunEventType.SUCCEEDED.value,
                    )
                    .order_by(RunEventModel.sequence)
                    .limit(2)
                )
            ).all()
            return tuple(self._event_record(model) for model in models)

    async def _append_events(
        self,
        session: AsyncSession,
        run: RunModel,
        events: Sequence[RunEvent],
    ) -> tuple[RunEvent, ...]:
        if not events:
            return ()
        source_ids = {event.event_id for event in events}
        item_ids = {event.item_id for event in events}
        existing_models = (
            await session.scalars(
                select(RunEventModel)
                .where(
                    RunEventModel.run_id == run.run_id,
                    or_(
                        RunEventModel.source_event_id.in_(source_ids),
                        RunEventModel.item_id.in_(item_ids),
                    ),
                )
                .order_by(RunEventModel.sequence)
            )
        ).all()
        existing_events = [self._event_record(model) for model in existing_models]
        by_source_id = {model.source_event_id: model for model in existing_models}
        last_sequence = await session.scalar(
            select(func.max(RunEventModel.sequence)).where(
                RunEventModel.run_id == run.run_id
            )
        )
        next_sequence = int(last_sequence or 0) + 1
        appended: list[RunEvent] = []
        new_events: list[RunEvent] = []
        planned_by_source: dict[str, RunEvent] = {}
        new_fingerprints: dict[str, str] = {}
        for source in events:
            if source.run_id != run.run_id or source.task_id != run.task_id:
                raise ValueError("event does not belong to the run")
            source_fingerprint = event_fingerprint(source)
            replay = by_source_id.get(source.event_id)
            if replay is not None:
                if replay.source_fingerprint != source_fingerprint:
                    raise ConflictError("event_id was reused with different content")
                appended.append(self._event_record(replay))
                continue
            planned = planned_by_source.get(source.event_id)
            if planned is not None:
                if new_fingerprints[source.event_id] != source_fingerprint:
                    raise ConflictError("event_id was reused with different content")
                appended.append(planned)
                continue
            if RunStatus(run.status) in TERMINAL_RUN_STATUSES:
                raise ConflictError("events cannot be appended to a terminal run")
            persisted = source.model_copy(update={"sequence": next_sequence})
            new_events.append(persisted)
            planned_by_source[source.event_id] = persisted
            new_fingerprints[source.event_id] = source_fingerprint
            appended.append(persisted)
            next_sequence += 1

        combined = (*existing_events, *new_events)
        try:
            validate_run_event_stream(tuple(combined))
        except ValueError as exc:
            raise ConflictError(f"invalid run event transition: {exc}") from exc

        for persisted in new_events:
            model = RunEventModel(
                event_id=persisted.event_id,
                source_event_id=persisted.event_id,
                source_fingerprint=new_fingerprints[persisted.event_id],
                sequence=persisted.sequence,
                tenant_id=run.tenant_id,
                owner_id=run.owner_id,
                task_id=run.task_id,
                run_id=run.run_id,
                occurred_at=persisted.occurred_at,
                scope=json_value(persisted.scope),
                event=persisted.status.value,
                event_type=persisted.event_type,
                audience=persisted.audience.value,
                item_id=persisted.item_id,
                parent_item_id=persisted.parent_item_id,
                summary=persisted.summary,
                subject_refs=list(persisted.subject_refs),
                metrics=dict(persisted.metrics),
                attributes=dict(persisted.attributes),
                payload_ref=persisted.payload_ref,
                created_at=utcnow(),
            )
            session.add(model)
        if new_events:
            await session.flush()
        return tuple(appended)

    @staticmethod
    def _new_run_model(
        *,
        owner_id: str,
        task_id: str,
        request: StartRunRequest,
        run_id: str,
        input_manifest: RunInputManifest | None,
        execution_protocol: ExecutionProtocolBinding,
        request_identity: object,
        resume_from_savepoint_id: str | None,
        now: datetime,
    ) -> RunModel:
        return RunModel(
            run_id=run_id,
            owner_id=owner_id,
            task_id=task_id,
            module=request.module,
            stage_key=request.stage_key,
            input_payload=json_value(request.input_payload),
            input_manifest=(json_value(input_manifest) if input_manifest is not None else None),
            input_manifest_digest=(
                input_manifest.digest if input_manifest is not None else None
            ),
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint(request_identity),
            thread_id=request.thread_id or f"thread_{run_id}",
            execution_protocol_status=ExecutionProtocolStatus.PINNED.value,
            execution_protocol=json_value(execution_protocol),
            execution_protocol_quarantine_reason=None,
            status=RunStatus.QUEUED.value,
            claimed_by=None,
            lease_token=None,
            lease_expires_at=None,
            heartbeat_at=None,
            claim_count=0,
            writer_generation=0,
            reclaimed_lease=False,
            claim_not_before=None,
            recovery_generation=0,
            restore_defer_count=0,
            restore_defer_reason_code=None,
            pause_requested=False,
            cancel_requested=False,
            feedback_pending=False,
            resume_from_savepoint_id=resume_from_savepoint_id,
            error=None,
            started_at=None,
            ended_at=None,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    async def _require_resume_operation_model(
        session: AsyncSession,
        run_id: str,
        *,
        for_update: bool = False,
    ) -> RunResumeOperationModel:
        statement = select(RunResumeOperationModel).where(RunResumeOperationModel.run_id == run_id)
        if for_update:
            statement = statement.with_for_update()
        operation = await session.scalar(statement)
        if operation is None:
            raise ResourceNotFoundError("resume operation not found")
        return operation

    @staticmethod
    def _require_operation_id(
        operation: RunResumeOperationModel,
        operation_id: str,
    ) -> None:
        if operation.operation_id != operation_id:
            raise ConflictError("resume operation id does not match the Run")

    @staticmethod
    def _require_source_cursor(
        savepoint: CheckpointLinkModel,
        cursor: CheckpointCursor,
    ) -> None:
        if (
            cursor.thread_id != savepoint.thread_id
            or cursor.checkpoint_id != savepoint.checkpoint_id
            or cursor.checkpoint_ns != savepoint.checkpoint_ns
        ):
            raise ConflictError("source checkpoint cursor does not match the savepoint")

    @staticmethod
    def _operation_observed_cursor(
        operation: RunResumeOperationModel,
    ) -> CheckpointCursor | None:
        if operation.observed_checkpoint_cursor is None:
            return None
        return CheckpointCursor.model_validate(operation.observed_checkpoint_cursor)

    @staticmethod
    def _finalize_resume_operation_model(
        operation: RunResumeOperationModel,
        now: datetime,
    ) -> None:
        phase = ResumeOperationPhase(operation.phase)
        if phase == ResumeOperationPhase.FINALIZED:
            return
        if phase != ResumeOperationPhase.GRAPH_OBSERVED:
            raise ConflictError("resume operation has no durable graph observation")
        operation.phase = ResumeOperationPhase.FINALIZED.value
        operation.updated_at = now

    @staticmethod
    def _resume_operation_record(model: RunResumeOperationModel) -> ResumeOperationRecord:
        return ResumeOperationRecord(
            operation_id=model.operation_id,
            owner_id=model.owner_id,
            task_id=model.task_id,
            run_id=model.run_id,
            savepoint_id=model.savepoint_id,
            target_mcp_session_id=model.target_mcp_session_id,
            command_fingerprint=model.command_fingerprint,
            source_checkpoint_cursor=CheckpointCursor.model_validate(
                model.source_checkpoint_cursor
            ),
            phase=ResumeOperationPhase(model.phase),
            observed_checkpoint_cursor=(
                CheckpointCursor.model_validate(model.observed_checkpoint_cursor)
                if model.observed_checkpoint_cursor is not None
                else None
            ),
            observed_outcome=(
                ResumeObservedOutcome(model.observed_outcome)
                if model.observed_outcome is not None
                else None
            ),
            attempt_count=model.attempt_count,
            created_at=as_utc(model.created_at),
            updated_at=as_utc(model.updated_at),
        )

    @staticmethod
    async def _require_final_artifact(
        session: AsyncSession,
        run: RunModel,
        reference: FinalIFCArtifactRef,
    ) -> None:
        if reference.source_run_id != run.run_id:
            raise ConflictError("final IFC Artifact belongs to another Run")
        row = (
            await session.execute(
                select(ArtifactModel, ArtifactVersionModel)
                .join(
                    ArtifactVersionModel,
                    and_(
                        ArtifactVersionModel.owner_id == ArtifactModel.owner_id,
                        ArtifactVersionModel.task_id == ArtifactModel.task_id,
                        ArtifactVersionModel.artifact_id == ArtifactModel.artifact_id,
                    ),
                )
                .where(
                    ArtifactModel.owner_id == run.owner_id,
                    ArtifactModel.task_id == run.task_id,
                    ArtifactModel.run_id == run.run_id,
                    ArtifactModel.artifact_id == reference.artifact_id,
                    ArtifactModel.kind == "final_ifc",
                    ArtifactModel.status == "available",
                    ArtifactModel.current_version_id == reference.version_id,
                    ArtifactVersionModel.version_id == reference.version_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise ResourceNotFoundError("final IFC Artifact version not found")
        _, version = row
        if (
            version.version_no != reference.version
            or version.sha256 != reference.sha256
            or version.size_bytes != reference.size_bytes
            or version.filename != reference.filename
        ):
            raise ConflictError("final IFC Artifact metadata does not match the registry")

    @staticmethod
    def _event_record(model: RunEventModel) -> RunEvent:
        return RunEvent(
            event_id=model.event_id,
            sequence=model.sequence,
            task_id=model.task_id,
            run_id=model.run_id,
            event_type=model.event_type,
            audience=RunEventAudience(model.audience),
            occurred_at=as_utc(model.occurred_at),
            scope=RunEventScope.model_validate(model.scope),
            item_id=model.item_id,
            parent_item_id=model.parent_item_id,
            subject_refs=tuple(model.subject_refs or ()),
            status=RunEventType(model.event),
            summary=model.summary,
            metrics=dict(model.metrics or {}),
            attributes=dict(model.attributes or {}),
            payload_ref=model.payload_ref,
        )


__all__ = ["PostgresTaskRunRepositoryMixin"]
