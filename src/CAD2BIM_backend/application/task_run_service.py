"""Authenticated Task/Run commands over the existing repository and identity directory."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from CAD2BIM_backend.application.identity_binding import resolve_user_task_identity

from CAD2BIM_backend.domain.control_plane import (
    CreateTaskRequest,
    ResourceNotFoundError,
    RunRecord,
    RunStatus,
    StartRunRequest,
    TaskRecord,
)
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.state_read_binding import (
    ExactStateReadBinding,
    StateReadBindingError,
    completion_ref_from_event,
    root_field_sources,
)
from CAD2BIM_backend.domain.identity import (
    AuthenticatedSubject,
    TenantActorKind,
    TenantExecutionContext,
)
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.domain.run_events import RunEventAudience
from CAD2BIM_backend.ports.identity import IdentityAccessDenied, IdentityDirectory
from CAD2BIM_backend.ports.task_runs import TaskRunRepository
from CAD2BIM_backend.ports.graph_runtime import GraphRuntimePort


class TaskRunService:
    def __init__(
        self,
        *,
        repository: TaskRunRepository,
        identity_directory: IdentityDirectory,
        execution_protocol_factory: Callable[[str, str], ExecutionProtocolBinding],
        bind_identity: Callable[[TenantExecutionContext], AbstractContextManager[None]],
        graph_runtime: GraphRuntimePort | None = None,
        observation_repository: TaskRunRepository | None = None,
    ) -> None:
        self._repository = repository
        self._identities = identity_directory
        self._execution_protocol_factory = execution_protocol_factory
        self._bind_identity = bind_identity
        self._graph_runtime = graph_runtime
        self._observation_repository = observation_repository or repository

    async def _identity(
        self, subject: AuthenticatedSubject, *, task_id: str | None = None
    ) -> TenantExecutionContext:
        return await resolve_user_task_identity(self._identities, subject, task_id=task_id)

    async def generated_task_title(self, grant: RunnerCheckpointGrant) -> str | None:
        identity = grant.scope.identity
        if identity.actor_kind is not TenantActorKind.RUNNER or identity.task_id is None:
            raise IdentityAccessDenied("Task naming requires a claimed Runner scope")
        with self._bind_identity(identity):
            task = await self._observation_repository.get_task(identity.tenant_id, identity.task_id)
        if task is None:
            raise ResourceNotFoundError("task not found")
        return task.title if task.metadata.get("component_title_generated") is True else None

    async def set_generated_task_title(self, grant: RunnerCheckpointGrant, title: str) -> TaskRecord:
        identity = grant.scope.identity
        if identity.actor_kind is not TenantActorKind.RUNNER or identity.task_id is None:
            raise IdentityAccessDenied("Task naming requires a claimed Runner scope")
        if not title.strip() or len(title) > 80 or any(character in title for character in "\r\n"):
            raise ValueError("Task title must be one nonblank line of at most 80 characters")
        with self._bind_identity(identity):
            return await self._observation_repository.set_generated_task_title(grant, title.strip())

    async def create_task(
        self, subject: AuthenticatedSubject, request: CreateTaskRequest
    ) -> TaskRecord:
        if "component_title_generated" in request.metadata:
            raise IdentityAccessDenied("Generated Task title state is server-owned")
        context = await self._identity(subject)
        with self._bind_identity(context):
            return await self._repository.create_task(context.tenant_id, request)

    async def delete_task(self, subject: AuthenticatedSubject, task_id: str) -> None:
        context = await self._identity(subject, task_id=task_id)
        with self._bind_identity(context):
            await self._repository.delete_task(context.tenant_id, task_id)

    async def get_task(self, subject: AuthenticatedSubject, task_id: str) -> TaskRecord:
        context = await self._identity(subject, task_id=task_id)
        with self._bind_identity(context):
            task = await self._repository.get_task(context.tenant_id, task_id)
        if task is None:
            raise ResourceNotFoundError("task not found")
        return task

    async def authorized_task_context(self, subject: AuthenticatedSubject, task_id: str):
        context = await self._identity(subject, task_id=task_id)
        with self._bind_identity(context):
            task = await self._repository.get_task(context.tenant_id, task_id)
        if task is None:
            raise ResourceNotFoundError("task not found")
        return context, task

    async def authorized_run_context(self, subject: AuthenticatedSubject, task_id: str, run_id: str):
        context = await self._identity(subject, task_id=task_id)
        with self._bind_identity(context):
            run = await self._repository.get_run(context.tenant_id, run_id)
        if run is None or run.task_id != task_id:
            raise ResourceNotFoundError("run not found")
        return context, run

    async def checkpoint_observation_context(self, run_id: str, thread_id: str):
        """Trusted observer: resolve the persisted execution identity, then bind RLS."""
        return await self._observation_repository.get_observed_run(run_id, thread_id)

    async def list_tasks(self, subject: AuthenticatedSubject, *, after_task_id: str | None = None, limit: int = 50):
        context = await self._identity(subject)
        with self._bind_identity(context):
            return await self._repository.list_tasks(context.tenant_id, after_task_id=after_task_id, limit=limit)

    async def chat_snapshot_basis(self, subject: AuthenticatedSubject, task_id: str, run_id: str,
                                  *, before_sequence: int | None = None, limit: int = 100):
        context, _ = await self.authorized_run_context(subject, task_id, run_id)
        with self._bind_identity(context):
            return await self._repository.read_chat_snapshot(context.tenant_id, run_id,
                                                            before_sequence=before_sequence, limit=limit)

    async def list_events(self, subject: AuthenticatedSubject, task_id: str, run_id: str,
                          *, after_sequence: int = 0, through_sequence: int | None = None, limit: int = 100):
        context, _ = await self.authorized_run_context(subject, task_id, run_id)
        with self._bind_identity(context):
            return await self._repository.list_events(context.tenant_id, run_id,
                after_sequence=after_sequence, through_sequence=through_sequence,
                audience=RunEventAudience.PUBLIC, limit=limit)

    async def pause_run(self, subject: AuthenticatedSubject, task_id: str, run_id: str, client_request_id: str):
        context, _ = await self.authorized_run_context(subject, task_id, run_id)
        if not client_request_id or len(client_request_id) > 200:
            raise ValueError("invalid pause request identity")
        with self._bind_identity(context):
            return await self._repository.pause_run(context.tenant_id, run_id, client_request_id=client_request_id)

    async def continue_run(self, subject: AuthenticatedSubject, task_id: str, run_id: str, client_request_id: str):
        context, _ = await self.authorized_run_context(subject, task_id, run_id)
        if not client_request_id or len(client_request_id) > 200:
            raise ValueError("invalid continue request identity")
        with self._bind_identity(context):
            return await self._repository.continue_run(context.tenant_id, run_id, client_request_id=client_request_id)

    async def start_run(
        self, subject: AuthenticatedSubject, task_id: str, request: StartRunRequest
    ) -> RunRecord:
        if request.thread_id is not None:
            raise ValueError("thread identity is server-owned")
        if request.module == "component" and not request.content:
            raise ValueError("Component start requires its first user content")
        context = await self._identity(subject, task_id=task_id)

        def current_protocol() -> ExecutionProtocolBinding:
            binding = self._execution_protocol_factory(request.module, request.stage_key)
            binding.require_scope(module=request.module, stage_key=request.stage_key)
            return binding

        with self._bind_identity(context):
            return await self._repository.create_run(
                context.tenant_id,
                task_id,
                request,
                execution_protocol_factory=current_protocol,
            )

    async def get_run(self, subject: AuthenticatedSubject, task_id: str, run_id: str) -> RunRecord:
        context = await self._identity(subject, task_id=task_id)
        with self._bind_identity(context):
            run = await self._repository.get_run(context.tenant_id, run_id)
        if run is None or run.task_id != task_id:
            raise ResourceNotFoundError("run not found")
        return run

    async def completed_state_read_binding(
        self, subject: AuthenticatedSubject, task_id: str, run_id: str
    ) -> ExactStateReadBinding:
        """Authorize one completed Run and bind its saved terminal root exactly."""

        if self._graph_runtime is None:
            raise RuntimeError("completed state reader is not configured")
        context = await self._identity(subject, task_id=task_id)
        with self._bind_identity(context):
            run = await self._repository.get_run(context.tenant_id, run_id)
            if run is None or run.task_id != task_id:
                raise ResourceNotFoundError("run not found")
            if run.status is not RunStatus.COMPLETED or run.execution_protocol is None:
                raise StateReadBindingError("Run has no completed state")
            events = await self._repository.get_completion_checkpoint_events(
                context.tenant_id, run_id
            )
            if len(events) != 1:
                raise StateReadBindingError("Run has no unique terminal checkpoint reference")
            reference = completion_ref_from_event(run, events[0])
            state = await self._graph_runtime.get_checkpoint_state(
                thread_id=run.thread_id,
                checkpoint_id=reference.root_checkpoint_id,
                checkpoint_ns="",
            )
        if state is None:
            raise StateReadBindingError("Run terminal root checkpoint is missing")
        checkpoint = state.checkpoint
        protocol = run.execution_protocol
        if (
            checkpoint.thread_id != run.thread_id
            or checkpoint.checkpoint_id != reference.root_checkpoint_id
            or checkpoint.checkpoint_ns
            or checkpoint.root_checkpoint_id != reference.root_checkpoint_id
            or checkpoint.next_nodes
            or state.interrupts
            or state.graph_operation_id != reference.graph_operation_id
            or state.target_run_id != run.run_id
            or checkpoint.checkpoint_map_digest is None
        ):
            raise StateReadBindingError("Run terminal root checkpoint contradicts its reference")
        try:
            cursor = CheckpointCursor(
                thread_id=run.thread_id,
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_ns="",
                root_checkpoint_id=reference.root_checkpoint_id,
                checkpoint_map=checkpoint.checkpoint_map,
                checkpoint_digest=checkpoint.checkpoint_map_digest,
            )
        except ValueError as exc:
            raise StateReadBindingError("Run terminal checkpoint lineage is malformed") from exc
        return ExactStateReadBinding(
            owner_id=run.owner_id,
            task_id=run.task_id,
            run_id=run.run_id,
            thread_id=run.thread_id,
            checkpoint_cursor=cursor,
            field_sources=root_field_sources(reference.root_checkpoint_id),
        )


__all__ = ["TaskRunService"]
