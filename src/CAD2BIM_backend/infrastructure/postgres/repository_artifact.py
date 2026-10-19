"""PostgreSQL artifact metadata and savepoint operations."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePath
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from CAD2BIM_backend.domain.control_plane import (
    ArtifactRecord,
    ConflictError,
    CreateSavepointRequest,
    ExecutionProtocolStatus,
    ResourceNotFoundError,
    SavepointRecord,
)
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactModel,
    ArtifactVersionModel,
    CheckpointLinkModel,
    McpSessionModel,
    McpSnapshotModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository_base import (
    PostgresRepositoryBase,
    artifact_storage_key,
    as_utc,
    fingerprint,
    utcnow,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    set_run_execution_context,
)


class PostgresArtifactRepositoryMixin(PostgresRepositoryBase):
    async def register_artifact(
        self,
        owner_id: str,
        task_id: str,
        *,
        artifact_id: str,
        version_no: int,
        filename: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        idempotency_key: str,
        kind: str = "input",
        run_id: str | None = None,
        version_metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        self._validate_artifact_metadata(
            artifact_id=artifact_id,
            version_no=version_no,
            filename=filename,
            size_bytes=size_bytes,
            sha256=sha256,
            kind=kind,
        )
        request_value = {
            "artifact_id": artifact_id,
            "version_no": version_no,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "idempotency_key": idempotency_key,
            "kind": kind,
            "run_id": run_id,
            "version_metadata": dict(version_metadata or {}),
        }
        try:
            async with self._sessions() as session, session.begin():
                await self._require_task(session, owner_id, task_id, for_update=True)
                if run_id is not None:
                    run = await self._require_run(session, owner_id, run_id)
                    if run.task_id != task_id:
                        raise ResourceNotFoundError("artifact run not found")
                    await set_run_execution_context(session, run_id)
                replay = await session.scalar(
                    select(ArtifactModel).where(
                        ArtifactModel.task_id == task_id,
                        ArtifactModel.idempotency_key == idempotency_key,
                    )
                )
                if replay is not None:
                    self._assert_replay(replay, request_value)
                    return await self._artifact_record(session, replay, version_no)
                if await session.get(ArtifactModel, artifact_id) is not None:
                    raise ConflictError("artifact_id already exists")
                now = utcnow()
                record = ArtifactRecord(
                    artifact_id=artifact_id, version_id=f"artifact_version_{uuid4().hex}",
                    owner_id=owner_id, task_id=task_id, run_id=run_id, kind=kind,
                    filename=filename, content_type=content_type, version_no=version_no,
                    size_bytes=size_bytes, sha256=sha256, idempotency_key=idempotency_key,
                    created_at=now, version_metadata=dict(version_metadata or {}),
                )
                await register_artifact_in_transaction(session, record, request_value=request_value)
                return record
        except IntegrityError as exc:
            async with self._sessions() as session:
                if run_id is not None:
                    await set_run_execution_context(session, run_id)
                replay = await session.scalar(
                    select(ArtifactModel).where(
                        ArtifactModel.task_id == task_id,
                        ArtifactModel.idempotency_key == idempotency_key,
                    )
                )
                if replay is not None:
                    self._assert_replay(replay, request_value)
                    return await self._artifact_record(session, replay, version_no)
            raise ConflictError("artifact metadata could not be registered") from exc

    async def get_artifact(
        self,
        owner_id: str,
        artifact_id: str,
        *,
        run_id: str | None = None,
    ) -> ArtifactRecord | None:
        async with self._sessions() as session:
            if run_id is not None:
                await set_run_execution_context(session, run_id)
            filters = [
                ArtifactModel.owner_id == owner_id,
                ArtifactModel.artifact_id == artifact_id,
            ]
            if run_id is not None:
                filters.append(ArtifactModel.run_id == run_id)
            artifact = await session.scalar(select(ArtifactModel).where(*filters))
            if artifact is None:
                return None
            return await self._artifact_record(session, artifact)

    async def get_artifact_version(
        self,
        owner_id: str,
        artifact_id: str,
        version_id: str,
        *,
        run_id: str | None = None,
    ) -> ArtifactRecord | None:
        async with self._sessions() as session:
            if run_id is not None:
                await set_run_execution_context(session, run_id)
            filters = [
                ArtifactModel.owner_id == owner_id,
                ArtifactModel.artifact_id == artifact_id,
                ArtifactVersionModel.owner_id == owner_id,
                ArtifactVersionModel.version_id == version_id,
            ]
            if run_id is not None:
                filters.append(ArtifactModel.run_id == run_id)
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
                    .where(*filters)
                )
            ).one_or_none()
            if row is None:
                return None
            artifact, version = row
            return self._artifact_record_from_models(artifact, version)

    async def get_artifact_version_number(
        self,
        owner_id: str,
        artifact_id: str,
        version_no: int,
    ) -> ArtifactRecord | None:
        async with self._sessions() as session:
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
                        ArtifactModel.owner_id == owner_id,
                        ArtifactModel.artifact_id == artifact_id,
                        ArtifactVersionModel.owner_id == owner_id,
                        ArtifactVersionModel.version_no == version_no,
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            artifact, version = row
            return self._artifact_record_from_models(artifact, version)

    async def create_savepoint(
        self, owner_id: str, run_id: str, request: CreateSavepointRequest
    ) -> SavepointRecord:
        try:
            async with self._sessions() as session, session.begin():
                run = await self._require_run(session, owner_id, run_id, for_update=True)
                run_record = self._run_record(run)
                binding = run_record.execution_protocol
                if (
                    run_record.execution_protocol_status is not ExecutionProtocolStatus.PINNED
                    or binding is None
                ):
                    raise ConflictError("savepoint Run has no executable pinned protocol")
                replay = await session.scalar(
                    select(CheckpointLinkModel).where(
                        CheckpointLinkModel.run_id == run_id,
                        CheckpointLinkModel.idempotency_key == request.idempotency_key,
                    )
                )
                if replay is not None:
                    self._assert_replay(replay, request)
                    return self._savepoint_record(replay)
                if request.mcp_session_id is not None and (
                    run.status != "running"
                    or run.writer_generation < 1
                    or run.writer_generation != request.expected_active_writer_generation
                    or request.writer_generation > run.writer_generation
                ):
                    raise ConflictError("savepoint writer generation is stale")
                for artifact_id in request.artifact_ids:
                    artifact = await session.scalar(
                        select(ArtifactModel.artifact_id).where(
                            ArtifactModel.artifact_id == artifact_id,
                            ArtifactModel.owner_id == owner_id,
                            ArtifactModel.task_id == run.task_id,
                            or_(
                                ArtifactModel.run_id == run_id,
                                ArtifactModel.run_id.is_(None),
                            ),
                        )
                    )
                    if artifact is None:
                        raise ResourceNotFoundError("savepoint artifact not found")
                manifest_row = (
                    await session.execute(
                        select(ArtifactModel, ArtifactVersionModel)
                        .join(
                            ArtifactVersionModel,
                            (ArtifactVersionModel.owner_id == ArtifactModel.owner_id)
                            & (ArtifactVersionModel.task_id == ArtifactModel.task_id)
                            & (ArtifactVersionModel.artifact_id == ArtifactModel.artifact_id),
                        )
                        .where(
                            ArtifactModel.owner_id == owner_id,
                            ArtifactModel.task_id == run.task_id,
                            ArtifactModel.run_id == run_id,
                            ArtifactModel.artifact_id == request.manifest_artifact_id,
                            ArtifactModel.kind == "savepoint_manifest",
                            ArtifactVersionModel.version_no == request.manifest_version_no,
                            ArtifactVersionModel.sha256 == request.manifest_sha256,
                        )
                    )
                ).one_or_none()
                if manifest_row is None:
                    raise ResourceNotFoundError("savepoint manifest Artifact version not found")
                if request.mcp_snapshot_id is not None:
                    snapshot = await session.scalar(
                        select(McpSnapshotModel)
                        .join(
                            McpSessionModel,
                            and_(
                                McpSessionModel.owner_id == McpSnapshotModel.owner_id,
                                McpSessionModel.task_id == McpSnapshotModel.task_id,
                                McpSessionModel.session_id == McpSnapshotModel.session_id,
                            ),
                        )
                        .where(
                            McpSnapshotModel.owner_id == owner_id,
                            McpSnapshotModel.task_id == run.task_id,
                            McpSnapshotModel.session_id == request.mcp_session_id,
                            McpSnapshotModel.snapshot_id == request.mcp_snapshot_id,
                            McpSessionModel.run_id == run_id,
                        )
                    )
                    if snapshot is None:
                        raise ResourceNotFoundError("savepoint MCP snapshot not found")
                    if snapshot.writer_generation != request.writer_generation:
                        raise ConflictError("savepoint snapshot writer generation changed")
                model = CheckpointLinkModel(
                    savepoint_id=f"savepoint_{uuid4().hex}",
                    owner_id=owner_id,
                    task_id=run.task_id,
                    run_id=run_id,
                    thread_id=run.thread_id,
                    safe_point_key=request.safe_point_key,
                    checkpoint_id=request.checkpoint_id,
                    checkpoint_ns=request.checkpoint_ns,
                    stage_key=request.stage_key,
                    checkpoint_next=list(request.checkpoint_next),
                    safe_point_context=request.safe_point_context,
                    default_route=request.default_route,
                    mcp_session_id=request.mcp_session_id,
                    mcp_snapshot_id=request.mcp_snapshot_id,
                    writer_generation=request.writer_generation,
                    artifact_ids=list(request.artifact_ids),
                    manifest_artifact_id=request.manifest_artifact_id,
                    manifest_version_no=request.manifest_version_no,
                    manifest_sha256=request.manifest_sha256,
                    is_user_visible=request.is_user_visible,
                    idempotency_key=request.idempotency_key,
                    request_fingerprint=fingerprint(request),
                    created_at=utcnow(),
                )
                session.add(model)
                await session.flush()
                return self._savepoint_record(model)
        except IntegrityError as exc:
            async with self._sessions() as session:
                run = await self._require_run(session, owner_id, run_id)
                replay = await session.scalar(
                    select(CheckpointLinkModel).where(
                        CheckpointLinkModel.run_id == run_id,
                        CheckpointLinkModel.idempotency_key == request.idempotency_key,
                    )
                )
                if replay is not None:
                    self._assert_replay(replay, request)
                    return self._savepoint_record(replay)
            raise ConflictError("savepoint could not be created") from exc

    async def get_savepoint(
        self, owner_id: str, task_id: str, savepoint_id: str
    ) -> SavepointRecord | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(CheckpointLinkModel).where(
                    CheckpointLinkModel.owner_id == owner_id,
                    CheckpointLinkModel.task_id == task_id,
                    CheckpointLinkModel.savepoint_id == savepoint_id,
                )
            )
            return self._savepoint_record(model) if model is not None else None

    async def get_savepoint_by_idempotency(
        self, owner_id: str, run_id: str, idempotency_key: str
    ) -> SavepointRecord | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(CheckpointLinkModel).where(
                    CheckpointLinkModel.owner_id == owner_id,
                    CheckpointLinkModel.run_id == run_id,
                    CheckpointLinkModel.idempotency_key == idempotency_key,
                )
            )
            return self._savepoint_record(model) if model is not None else None

    async def list_savepoints(self, owner_id: str, task_id: str) -> tuple[SavepointRecord, ...]:
        async with self._sessions() as session:
            await self._require_task(session, owner_id, task_id)
            models = (
                await session.scalars(
                    select(CheckpointLinkModel)
                    .where(
                        CheckpointLinkModel.owner_id == owner_id,
                        CheckpointLinkModel.task_id == task_id,
                    )
                    .order_by(
                        CheckpointLinkModel.created_at,
                        CheckpointLinkModel.savepoint_id,
                    )
                )
            ).all()
            return tuple(self._savepoint_record(model) for model in models)

    async def _artifact_record(
        self,
        session: AsyncSession,
        artifact: ArtifactModel,
        version_no: int | None = None,
    ) -> ArtifactRecord:
        statement = select(ArtifactVersionModel).where(
            ArtifactVersionModel.owner_id == artifact.owner_id,
            ArtifactVersionModel.artifact_id == artifact.artifact_id,
        )
        if version_no is None:
            statement = statement.where(
                ArtifactVersionModel.version_id == artifact.current_version_id
            )
        else:
            statement = statement.where(ArtifactVersionModel.version_no == version_no)
        version = await session.scalar(statement)
        if version is None:
            raise ConflictError("artifact current version metadata is missing")
        return self._artifact_record_from_models(artifact, version)

    @staticmethod
    def _artifact_record_from_models(
        artifact: ArtifactModel, version: ArtifactVersionModel
    ) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=artifact.artifact_id,
            version_id=version.version_id,
            owner_id=artifact.owner_id,
            task_id=artifact.task_id,
            kind=artifact.kind,
            filename=version.filename,
            content_type=version.content_type,
            version_no=version.version_no,
            size_bytes=version.size_bytes,
            sha256=version.sha256,
            idempotency_key=artifact.idempotency_key,
            created_at=as_utc(artifact.created_at),
            run_id=artifact.run_id,
            version_metadata=dict(version.metadata_json or {}),
        )

    @staticmethod
    def _savepoint_record(model: CheckpointLinkModel) -> SavepointRecord:
        return SavepointRecord(
            savepoint_id=model.savepoint_id,
            owner_id=model.owner_id,
            task_id=model.task_id,
            run_id=model.run_id,
            thread_id=model.thread_id,
            safe_point_key=model.safe_point_key,
            checkpoint_id=model.checkpoint_id,
            checkpoint_ns=model.checkpoint_ns,
            stage_key=model.stage_key,
            checkpoint_next=tuple(model.checkpoint_next or ()),
            safe_point_context=model.safe_point_context,
            default_route=model.default_route,
            mcp_session_id=model.mcp_session_id,
            mcp_snapshot_id=model.mcp_snapshot_id,
            writer_generation=model.writer_generation,
            artifact_ids=tuple(model.artifact_ids or ()),
            manifest_artifact_id=model.manifest_artifact_id,
            manifest_version_no=model.manifest_version_no,
            manifest_sha256=model.manifest_sha256,
            is_user_visible=model.is_user_visible,
            idempotency_key=model.idempotency_key,
            created_at=as_utc(model.created_at),
        )

    @staticmethod
    def _validate_artifact_metadata(
        *,
        artifact_id: str,
        version_no: int,
        filename: str,
        size_bytes: int,
        sha256: str,
        kind: str,
    ) -> None:
        if not artifact_id:
            raise ValueError("artifact_id must not be empty")
        if version_no < 1:
            raise ValueError("version_no must be positive")
        if (
            not filename
            or PurePath(filename).name != filename
            or "/" in filename
            or "\\" in filename
        ):
            raise ValueError("filename must not contain a path")
        if "\x00" in filename:
            raise ValueError("filename contains NUL")
        if size_bytes < 0:
            raise ValueError("size_bytes must not be negative")
        if not kind or len(kind) > 64 or any(ord(character) < 32 for character in kind):
            raise ValueError("kind must be 1..64 printable characters")
        if len(sha256) != 64 or any(
            character not in "0123456789abcdefABCDEF" for character in sha256
        ):
            raise ValueError("sha256 must be a 64-character hexadecimal digest")


async def register_artifact_in_transaction(
    session, record: ArtifactRecord, *, request_value: Mapping[str, Any],
    tenant_id: str | None = None, lineage: Mapping[str, Any] | None = None,
) -> None:
    """Register exact staged bytes in the caller's existing publication transaction."""
    scope_fields = {"tenant_id": tenant_id} if tenant_id is not None else {}
    artifact = ArtifactModel(
        artifact_id=record.artifact_id, owner_id=record.owner_id, task_id=record.task_id,
        run_id=record.run_id, kind=record.kind, filename=record.filename,
        content_type=record.content_type, status="available", current_version_id=None,
        idempotency_key=record.idempotency_key, request_fingerprint=fingerprint(request_value),
        created_at=record.created_at, updated_at=record.created_at, **scope_fields,
    )
    session.add(artifact)
    await session.flush()
    session.add(ArtifactVersionModel(
        version_id=record.version_id, owner_id=record.owner_id, task_id=record.task_id,
        artifact_id=record.artifact_id, version_no=record.version_no,
        storage_key=artifact_storage_key(record.owner_id, record.task_id, record.artifact_id, record.version_no),
        filename=record.filename, content_type=record.content_type, size_bytes=record.size_bytes,
        sha256=record.sha256, metadata_json=record.version_metadata,
        lineage_json=dict(lineage or {}), created_at=record.created_at, **scope_fields,
    ))
    await session.flush()
    artifact.current_version_id = record.version_id
    await session.flush()


__all__ = ["PostgresArtifactRepositoryMixin", "register_artifact_in_transaction"]
