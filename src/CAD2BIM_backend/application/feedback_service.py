"""Authenticated feedback inputs and durable Gate commands."""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any

from langchain_core.messages import HumanMessage

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.identity_binding import resolve_user_task_identity
from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.control_plane import (
    FeedbackCommand,
    FeedbackCommandReceipt,
    FrozenFeedbackBatch,
    GateResumePlan,
    OpenInterruptRequest,
    ResourceNotFoundError,
    RespondGateCommand,
    RunRecord,
    SubmitFeedbackCommand,
    TerminalGateReentryPlan,
    UserTextContent,
)
from CAD2BIM_backend.domain.feedback_target import (
    ActiveFeedbackSubject,
    FeedbackSubjectActivation,
    FeedbackTargetConflictError,
    feedback_claim_cursor,
)
from CAD2BIM_backend.domain.identity import (
    AuthenticatedSubject,
    TenantActorKind,
    TenantExecutionContext,
)
from CAD2BIM_backend.domain.role_memory import parse_role_messages
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.domain.system_interrupts import (
    InterruptResumeEnvelope,
    SystemInterrupt,
    SystemInterruptEnvelope,
    SystemInterruptResponseReceipt,
)
from CAD2BIM_backend.ports.feedback import FeedbackRepository
from CAD2BIM_backend.ports.identity import IdentityAccessDenied, IdentityDirectory


def delivery_update(
    state: Mapping[str, Any], frozen: tuple[FrozenFeedbackBatch, ...],
    *, projected_content: Mapping[str, list[dict[str, Any]]] | None = None,
    initial_feedback_id: str | None = None,
) -> dict[str, Any]:
    """Append ordered user feedback with its existing batch and message identities."""

    messages = parse_role_messages(state.get("messages", ()))
    existing_ids = {message.id for message in messages}
    ordered = sorted(
        ((member, message_id, batch.batch.batch_id)
         for batch in frozen
         for member, message_id in zip(batch.members, batch.message_ids, strict=True)),
        key=lambda item: item[0].accepted_sequence,
    )
    for member, message_id, batch_id in ordered:
        if message_id not in existing_ids:
            content = projected_content.get(member.feedback_id) if projected_content else None
            if content is None:
                if any(not isinstance(block, UserTextContent) for block in member.content):
                    raise ValueError("attachment input requires an exact content projection")
                content = [block.model_dump(mode="json") for block in member.content]
            messages.append(HumanMessage(
                id=message_id,
                content=content,
                additional_kwargs={
                    "source": "user_feedback",
                    "feedback_id": member.feedback_id,
                    "feedback_batch_id": batch_id,
                    "input_content": [block.model_dump(mode="json") for block in member.content],
                    "input_kind": (
                        "initial_input" if member.feedback_id == initial_feedback_id else
                        "role_feedback" if member.resolved_scope.role_execution_id else "gate_input"
                    ),
                },
            ))
            existing_ids.add(message_id)
    return {"messages": messages, "feedback_batch_ids": tuple(batch.batch.batch_id for batch in frozen)}


class FeedbackService:
    def __init__(
        self,
        *,
        repository: FeedbackRepository,
        identity_directory: IdentityDirectory,
        bind_identity: Callable[[TenantExecutionContext], AbstractContextManager[None]],
        artifacts: ArtifactService | None = None,
    ) -> None:
        self._repository = repository
        self._identities = identity_directory
        self._bind_identity = bind_identity
        self._artifacts = artifacts

    async def list_feedback(self, subject: AuthenticatedSubject, task_id: str, run_id: str,
                            *, after_sequence: int = 0, limit: int = 100) -> tuple[FeedbackCommand, ...]:
        identity = await self._public_identity(subject, task_id)
        with self._bind_identity(identity):
            return await self._repository.list_feedback(identity.tenant_id, run_id,
                                                        after_sequence=after_sequence, limit=limit)

    async def pause_requested(self, grant: RunnerCheckpointGrant) -> bool:
        with self._bind_identity(grant.scope.identity):
            return await self._repository.pause_requested(grant)

    async def open_run_pause(self, grant: RunnerCheckpointGrant, request: OpenInterruptRequest) -> RunRecord:
        with self._bind_identity(grant.scope.identity):
            return await self._repository.open_run_pause(grant, request)

    async def load_run_continue(self, grant: RunnerCheckpointGrant) -> tuple[str, str, InterruptResumeEnvelope] | None:
        with self._bind_identity(grant.scope.identity):
            return await self._repository.load_run_continue(grant)

    async def delivery_update(self, grant: RunnerCheckpointGrant, state: Mapping[str, Any],
                              batches: tuple[FrozenFeedbackBatch, ...]) -> dict[str, Any]:
        identity = grant.scope.identity
        with self._bind_identity(identity):
            run = await self._repository.get_run(identity.tenant_id, grant.scope.run_id)
            if run is None or run.task_id != identity.task_id:
                raise ResourceNotFoundError("run not found")
            projected: dict[str, list[dict[str, Any]]] = {}
            existing = {message.id for message in parse_role_messages(state.get("messages", ()))}
            for batch in batches:
                for member, message_id in zip(batch.members, batch.message_ids, strict=True):
                    if message_id in existing:
                        continue
                    blocks: list[dict[str, Any]] = []
                    for block in member.content:
                        if isinstance(block, UserTextContent):
                            blocks.append(block.model_dump(mode="json"))
                            continue
                        if self._artifacts is None:
                            raise RuntimeError("input artifact reader is not configured")
                        download = await self._artifacts.download_version(
                            identity.tenant_id, block.artifact_id, block.version_id)
                        artifact = download.artifact
                        if artifact.task_id != identity.task_id:
                            raise ResourceNotFoundError("input artifact belongs to another Task")
                        encoded = base64.b64encode(download.content).decode("ascii")
                        if block.type == "image":
                            if not artifact.content_type.startswith("image/"):
                                raise ValueError("image input must reference an image ArtifactVersion")
                            blocks.append({"type": "image_url", "image_url": {
                                "url": f"data:{artifact.content_type};base64,{encoded}"}})
                        else:
                            blocks.append({"type": "file", "file": {
                                "filename": artifact.filename,
                                "file_data": f"data:{artifact.content_type};base64,{encoded}"}})
                    projected[member.feedback_id] = blocks
            return delivery_update(state, batches, projected_content=projected,
                                   initial_feedback_id=run.initial_feedback.feedback_id if run.initial_feedback else None)

    async def _public_identity(
        self, subject: AuthenticatedSubject, task_id: str
    ) -> TenantExecutionContext:
        return await resolve_user_task_identity(self._identities, subject, task_id=task_id)

    async def submit_feedback(
        self,
        subject: AuthenticatedSubject,
        task_id: str,
        run_id: str,
        command: SubmitFeedbackCommand,
    ) -> FeedbackCommandReceipt:
        if command.scope.run_id != run_id:
            raise FeedbackTargetConflictError("feedback scope belongs to another Run")
        identity = await self._public_identity(subject, task_id)
        with self._bind_identity(identity):
            return await self._repository.accept_feedback(identity, run_id, command)

    async def respond_gate(
        self,
        subject: AuthenticatedSubject,
        task_id: str,
        run_id: str,
        command: RespondGateCommand,
    ) -> SystemInterruptResponseReceipt:
        identity = await self._public_identity(subject, task_id)
        with self._bind_identity(identity):
            return await self._repository.respond_gate(
                identity, run_id, command.interrupt_id, command
            )

    async def current_gate(
        self,
        subject: AuthenticatedSubject,
        task_id: str,
        run_id: str,
    ) -> SystemInterrupt | None:
        identity = await self._public_identity(subject, task_id)
        with self._bind_identity(identity):
            return await self._repository.get_pending_gate(identity, run_id)

    async def open_gate(
        self,
        grant: RunnerCheckpointGrant,
        request: OpenInterruptRequest,
    ) -> SystemInterruptEnvelope:
        identity = grant.scope.identity
        if identity.actor_kind is not TenantActorKind.RUNNER:
            raise IdentityAccessDenied("Gate open requires a Runner identity")
        with self._bind_identity(identity):
            return await self._repository.open_gate(grant, request)

    async def park_waiting_user(
        self,
        grant: RunnerCheckpointGrant,
        interrupt_id: str,
    ) -> RunRecord:
        identity = grant.scope.identity
        if identity.actor_kind is not TenantActorKind.RUNNER:
            raise IdentityAccessDenied("Gate park requires a Runner identity")
        with self._bind_identity(identity):
            return await self._repository.park_waiting_user(grant, interrupt_id)

    async def load_gate_resume(
        self,
        grant: RunnerCheckpointGrant,
    ) -> GateResumePlan | None:
        identity = grant.scope.identity
        if identity.actor_kind is not TenantActorKind.RUNNER:
            raise IdentityAccessDenied("Gate resume requires a Runner identity")
        with self._bind_identity(identity):
            return await self._repository.load_gate_resume(grant)

    async def load_terminal_reentry(
        self,
        grant: RunnerCheckpointGrant,
    ) -> TerminalGateReentryPlan | None:
        identity = grant.scope.identity
        if identity.actor_kind is not TenantActorKind.RUNNER:
            raise IdentityAccessDenied("terminal Gate reentry requires a Runner identity")
        with self._bind_identity(identity):
            return await self._repository.load_terminal_reentry(grant)

    async def select_terminal_reentry(
        self,
        grant: RunnerCheckpointGrant,
        *,
        source_checkpoint_id: str,
    ) -> TerminalGateReentryPlan | None:
        identity = grant.scope.identity
        if identity.actor_kind is not TenantActorKind.RUNNER:
            raise IdentityAccessDenied("terminal Gate reentry requires a Runner identity")
        with self._bind_identity(identity):
            return await self._repository.select_terminal_reentry(
                grant, source_checkpoint_id=source_checkpoint_id
            )

    async def freeze_for_main(
        self,
        grant: RunnerCheckpointGrant,
        *,
        stage_key: str | None,
        role_execution_id: str,
        task_ids: tuple[str, ...] = (),
        check_ids: tuple[str, ...] = (),
        input_checkpoint_cursor: CheckpointCursor,
        delivered_batch_ids: tuple[str, ...] = (),
    ) -> tuple[FrozenFeedbackBatch, ...]:
        identity, run_id = grant.scope.identity, grant.scope.run_id
        if identity.actor_kind is not TenantActorKind.RUNNER:
            raise IdentityAccessDenied("feedback freeze requires a Runner identity")
        with self._bind_identity(identity):
            run = await self._repository.get_run(identity.tenant_id, run_id)
            if run is None or run.task_id != identity.task_id:
                raise ResourceNotFoundError("run not found")
            if stage_key is not None and stage_key != run.stage_key:
                raise ValueError("feedback target stage differs from Run scope")
            if input_checkpoint_cursor.thread_id != grant.thread_id or run.thread_id != grant.thread_id:
                raise ValueError("feedback input cursor belongs to another thread")
            cursor = feedback_claim_cursor(
                run_id=run_id,
                stage_key=run.stage_key,
                role_execution_id=role_execution_id,
                writer_generation=grant.writer_generation,
                checkpoint_id=input_checkpoint_cursor.checkpoint_id,
                task_ids=task_ids,
                check_ids=check_ids,
            )
            return await self._repository.freeze_feedback(
                grant,
                cursor,
                input_checkpoint_cursor=input_checkpoint_cursor,
                delivered_batch_ids=delivered_batch_ids,
            )

    async def activate_feedback_subject(
        self,
        grant: RunnerCheckpointGrant,
        *,
        business_entry_id: str,
        stage_key: str | None,
        agent: str,
        attempt: int,
        checkpoint_id: str | None,
        stage_epoch: int = 0,
        plan_id: str | None = None,
        plan_version: int | None = None,
        task_ids: tuple[str, ...] = (),
        check_ids: tuple[str, ...] = (),
    ) -> ActiveFeedbackSubject:
        identity, run_id = grant.scope.identity, grant.scope.run_id
        if identity.actor_kind is not TenantActorKind.RUNNER or identity.task_id is None:
            raise IdentityAccessDenied("subject activation requires a Runner identity")
        with self._bind_identity(identity):
            run = await self._repository.get_run(identity.tenant_id, run_id)
            if run is None or run.task_id != identity.task_id:
                raise ResourceNotFoundError("run not found")
            if stage_key is not None and stage_key != run.stage_key:
                raise ValueError("feedback subject stage differs from Run scope")
            if run.thread_id != grant.thread_id:
                raise ValueError("feedback subject belongs to another Graph thread")
            activation = FeedbackSubjectActivation(
                task_id=identity.task_id,
                run_id=run_id,
                business_entry_id=business_entry_id,
                stage_key=run.stage_key,
                agent=agent,
                attempt=attempt,
                writer_generation=grant.writer_generation,
                checkpoint_id=checkpoint_id,
                stage_epoch=stage_epoch,
                plan_id=plan_id,
                plan_version=plan_version,
                task_ids=task_ids,
                check_ids=check_ids,
            )
            return await self._repository.activate_feedback_subject(
                grant, activation
            )

    async def decide_role_exit(
        self,
        grant: RunnerCheckpointGrant,
        role_execution_id: str,
        input_high_watermark: int,
        *,
        input_checkpoint_cursor: CheckpointCursor,
        delivered_batch_ids: tuple[str, ...],
    ) -> bool:
        identity = grant.scope.identity
        if identity.actor_kind is not TenantActorKind.RUNNER:
            raise IdentityAccessDenied("role exit requires a Runner identity")
        with self._bind_identity(identity):
            return await self._repository.seal_role_exit(
                grant, role_execution_id, input_high_watermark,
                input_checkpoint_cursor=input_checkpoint_cursor,
                delivered_batch_ids=delivered_batch_ids,
            )


__all__ = [
    "FeedbackService",
    "delivery_update",
]
