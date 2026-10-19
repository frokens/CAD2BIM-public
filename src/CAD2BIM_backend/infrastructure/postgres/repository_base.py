"""Shared transaction and serialization helpers for PostgreSQL repositories."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from CAD2BIM_backend.domain.control_plane import (
    ExecutionMode,
    ExecutionProtocolStatus,
    IdempotencyConflictError,
    LeaseConflictError,
    ResourceNotFoundError,
    RunRecord,
    RunStatus,
    TaskRecord,
    TaskStatus,
)
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.feedback_target import ActiveFeedbackSubject
from CAD2BIM_backend.domain.artifact_refs import FinalIFCArtifactRef
from CAD2BIM_backend.domain.run_input_manifest import RunInputManifest
from CAD2BIM_backend.infrastructure.postgres.models import RunModel, TaskModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def database_now(session: AsyncSession) -> datetime:
    """Use the database clock for lease fencing across multiple hosts."""

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        value = await session.scalar(select(func.clock_timestamp()))
        if value is not None:
            return as_utc(value)
    return utcnow()


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def fingerprint(value: Any) -> str:
    payload = json.dumps(
        json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def event_fingerprint(value: Any) -> str:
    """Fingerprint producer-owned event content, excluding sink-owned sequence."""

    return fingerprint(value.model_dump(mode="json", exclude={"sequence"}))


def artifact_storage_key(
    owner_id: str,
    task_id: str,
    artifact_id: str,
    version_no: int,
) -> str:
    """Encode the logical StorageKey without exposing a physical path."""

    return json.dumps(
        [owner_id, task_id, artifact_id, version_no],
        ensure_ascii=False,
        separators=(",", ":"),
    )


class PostgresRepositoryBase:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def _delete_library_resource(self, context, *, root_kind: str, root_id: str) -> bool:
        from CAD2BIM_backend.domain.identity import TenantActorKind
        from CAD2BIM_backend.domain.resource_scope import ResourceDeletionError
        from CAD2BIM_backend.ports.identity import IdentityAccessDenied
        from CAD2BIM_backend.infrastructure.postgres.tenant_context import set_tenant_execution_context

        if context.actor_kind not in {TenantActorKind.USER, TenantActorKind.RUNNER}:
            raise IdentityAccessDenied("library deletion requires a tenant user context")
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, context)
            result = await session.scalar(select(func.cad2bim_delete_library_resource(root_kind, root_id)))
            errors = {
                "system_resource": ("LIBRARY_SYSTEM_RESOURCE", "SYSTEM resources cannot be deleted"),
                "geometry_in_use": ("LIBRARY_GEOMETRY_IN_USE", "Geometry is used by a product family or another geometry resource"),
                "publication_pending": ("LIBRARY_PUBLICATION_PENDING", "An unresolved publication is using this resource; reconcile it before deletion"),
            }
            if result in errors:
                raise ResourceDeletionError(*errors[result])
            if result not in {"deleted", "not_found"}:
                raise RuntimeError("unexpected library deletion result")
            return result == "deleted"

    @staticmethod
    def _assert_replay(model: Any, request: Any) -> None:
        if model.request_fingerprint != fingerprint(request):
            raise IdempotencyConflictError("idempotency key was reused with different input")

    @staticmethod
    def _task_record(model: TaskModel) -> TaskRecord:
        return TaskRecord(
            task_id=model.task_id,
            owner_id=model.owner_id,
            tenant_id=model.tenant_id,
            title=model.title,
            instruction=model.instruction,
            metadata=dict(model.metadata_json or {}),
            status=TaskStatus(model.status),
            active_module=model.active_module,
            workspace_uri=model.workspace_uri,
            idempotency_key=model.idempotency_key,
            created_at=as_utc(model.created_at),
            updated_at=as_utc(model.updated_at),
        )

    @staticmethod
    def _run_record(model: RunModel) -> RunRecord:
        protocol_status = ExecutionProtocolStatus(model.execution_protocol_status)
        execution_protocol = (
            ExecutionProtocolBinding.model_validate(model.execution_protocol)
            if model.execution_protocol is not None
            else None
        )
        input_manifest = (
            RunInputManifest.model_validate(model.input_manifest)
            if model.input_manifest is not None
            else None
        )
        if (input_manifest is None) != (model.input_manifest_digest is None):
            raise ValueError("stored Run input manifest fields are incomplete")
        if input_manifest is not None and model.input_manifest_digest != input_manifest.digest:
            raise ValueError("stored Run input manifest digest is inconsistent")
        return RunRecord(
            run_id=model.run_id,
            task_id=model.task_id,
            owner_id=model.owner_id,
            tenant_id=model.tenant_id,
            module=model.module,
            stage_key=model.stage_key,
            input_payload=dict(model.input_payload or {}),
            input_manifest=input_manifest,
            initial_feedback=model.initial_feedback,
            pause_request_id=model.pause_request_id,
            continue_request_id=model.continue_request_id,
            idempotency_key=model.idempotency_key,
            thread_id=model.thread_id,
            execution_mode=ExecutionMode(model.execution_mode),
            execution_protocol_status=protocol_status,
            execution_protocol=execution_protocol,
            execution_protocol_quarantine_reason=(
                model.execution_protocol_quarantine_reason
            ),
            status=RunStatus(model.status),
            claimed_by=model.claimed_by,
            lease_token=model.lease_token,
            lease_expires_at=as_utc(model.lease_expires_at),
            heartbeat_at=as_utc(model.heartbeat_at),
            claim_count=model.claim_count,
            writer_generation=model.writer_generation,
            reclaimed_lease=model.reclaimed_lease,
            claim_not_before=as_utc(model.claim_not_before),
            recovery_generation=model.recovery_generation or 0,
            restore_defer_count=model.restore_defer_count or 0,
            restore_defer_reason_code=model.restore_defer_reason_code,
            pause_requested=model.pause_requested,
            cancel_requested=model.cancel_requested,
            feedback_pending=model.feedback_pending,
            feedback_sealed=model.feedback_sealed,
            feedback_transition_subject_id=model.feedback_transition_subject_id,
            active_feedback_subject=(
                ActiveFeedbackSubject.model_validate(model.active_feedback_subject)
                if model.active_feedback_subject is not None
                else None
            ),
            resume_from_savepoint_id=model.resume_from_savepoint_id,
            error=model.error,
            final_artifact_ref=(
                FinalIFCArtifactRef.model_validate(model.final_artifact)
                if model.final_artifact is not None
                else None
            ),
            created_at=as_utc(model.created_at),
            updated_at=as_utc(model.updated_at),
            started_at=as_utc(model.started_at),
            ended_at=as_utc(model.ended_at),
        )

    async def _require_task(
        self,
        session: AsyncSession,
        owner_id: str,
        task_id: str,
        *,
        for_update: bool = False,
        include_archived: bool = False,
    ) -> TaskModel:
        statement = select(TaskModel).where(
            TaskModel.owner_id == owner_id,
            TaskModel.task_id == task_id,
            True if include_archived else TaskModel.status != TaskStatus.ARCHIVED.value,
        )
        if for_update:
            statement = statement.with_for_update()
        model = await session.scalar(statement)
        if model is None:
            raise ResourceNotFoundError("task not found")
        return model

    async def _require_run(
        self,
        session: AsyncSession,
        owner_id: str,
        run_id: str,
        *,
        for_update: bool = False,
    ) -> RunModel:
        statement = select(RunModel).where(
            RunModel.owner_id == owner_id,
            RunModel.run_id == run_id,
            select(TaskModel.task_id).where(
                TaskModel.task_id == RunModel.task_id,
                TaskModel.status != TaskStatus.ARCHIVED.value,
            ).exists(),
        )
        if for_update:
            statement = statement.with_for_update()
        model = await session.scalar(statement)
        if model is None:
            raise ResourceNotFoundError("run not found")
        return model

    async def _require_lease(
        self,
        session: AsyncSession,
        run_id: str,
        runner_id: str,
        lease_token: str,
    ) -> RunModel:
        model = await session.scalar(
            select(RunModel).where(RunModel.run_id == run_id).with_for_update()
        )
        now = await database_now(session)
        if model is None:
            raise ResourceNotFoundError("run not found")
        if model.execution_protocol_status != ExecutionProtocolStatus.PINNED.value:
            raise LeaseConflictError("run execution protocol is quarantined")
        if (
            model.status != RunStatus.RUNNING.value
            or model.claimed_by != runner_id
            or model.lease_token != lease_token
            or model.lease_expires_at is None
            or as_utc(model.lease_expires_at) <= now
        ):
            raise LeaseConflictError("runner does not own an active lease")
        return model

    async def _set_task_status(
        self,
        session: AsyncSession,
        task_id: str,
        status: TaskStatus,
        now: datetime,
    ) -> None:
        task = await session.scalar(
            select(TaskModel).where(TaskModel.task_id == task_id).with_for_update()
        )
        if task is None:
            raise ResourceNotFoundError("task not found")
        task.status = status.value
        task.updated_at = now


__all__ = [
    "PostgresRepositoryBase",
    "as_utc",
    "artifact_storage_key",
    "database_now",
    "event_fingerprint",
    "fingerprint",
    "json_value",
    "utcnow",
]
