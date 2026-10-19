"""In-memory artifact and savepoint operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from CAD2BIM_backend.domain.control_plane import (
    ArtifactRecord,
    ConflictError,
    CreateSavepointRequest,
    ExecutionProtocolStatus,
    ResourceNotFoundError,
    RunStatus,
    SavepointRecord,
)
from CAD2BIM_backend.infrastructure.memory.state import MemoryRepositoryBase, _now


class ArtifactRepositoryMixin(MemoryRepositoryBase):
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
        async with self._lock:
            self._require_task(owner_id, task_id)
            if run_id is not None:
                run = self._require_run(owner_id, run_id)
                if run.task_id != task_id:
                    raise ResourceNotFoundError("artifact run not found")
            request_value = {
                "filename": filename,
                "content_type": content_type,
                "version_no": version_no,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "kind": kind,
                "run_id": run_id,
                "version_metadata": dict(version_metadata or {}),
            }
            scope = f"{owner_id}:{task_id}"
            replay = self._idempotent_replay(
                "artifact", scope, idempotency_key, request_value, self._artifacts
            )
            if replay is not None:
                return replay
            record = ArtifactRecord(
                artifact_id=artifact_id,
                version_id=f"artifact_version_{uuid4().hex}",
                owner_id=owner_id,
                task_id=task_id,
                kind=kind,
                filename=filename,
                content_type=content_type,
                version_no=version_no,
                size_bytes=size_bytes,
                sha256=sha256,
                idempotency_key=idempotency_key,
                created_at=_now(),
                run_id=run_id,
                version_metadata=dict(version_metadata or {}),
            )
            self._artifacts[record.artifact_id] = record
            self._remember("artifact", scope, idempotency_key, request_value, record.artifact_id)
            return record

    async def get_artifact(
        self,
        owner_id: str,
        artifact_id: str,
        *,
        run_id: str | None = None,
    ) -> ArtifactRecord | None:
        async with self._lock:
            record = self._artifacts.get(artifact_id)
            return (
                record
                if record is not None
                and record.owner_id == owner_id
                and (run_id is None or record.run_id == run_id)
                else None
            )

    async def get_artifact_version(
        self,
        owner_id: str,
        artifact_id: str,
        version_id: str,
        *,
        run_id: str | None = None,
    ) -> ArtifactRecord | None:
        async with self._lock:
            record = self._artifacts.get(artifact_id)
            if (
                record is None
                or record.owner_id != owner_id
                or record.version_id != version_id
                or (run_id is not None and record.run_id != run_id)
            ):
                return None
            return record

    async def get_artifact_version_number(
        self,
        owner_id: str,
        artifact_id: str,
        version_no: int,
    ) -> ArtifactRecord | None:
        async with self._lock:
            record = self._artifacts.get(artifact_id)
            if record is None or record.owner_id != owner_id or record.version_no != version_no:
                return None
            return record

    async def create_savepoint(
        self, owner_id: str, run_id: str, request: CreateSavepointRequest
    ) -> SavepointRecord:
        async with self._lock:
            run = self._require_run(owner_id, run_id)
            replay = self._idempotent_replay(
                "savepoint", run_id, request.idempotency_key, request, self._savepoints
            )
            if replay is not None:
                return replay
            if request.mcp_session_id is not None and (
                run.status is not RunStatus.RUNNING
                or run.writer_generation < 1
                or run.writer_generation != request.expected_active_writer_generation
                or request.writer_generation > run.writer_generation
            ):
                raise ConflictError("savepoint writer generation is stale")
            binding = run.execution_protocol
            if (
                run.execution_protocol_status is not ExecutionProtocolStatus.PINNED
                or binding is None
            ):
                raise ConflictError("savepoint Run has no executable pinned protocol")
            for artifact_id in request.artifact_ids:
                artifact = self._artifacts.get(artifact_id)
                if (
                    artifact is None
                    or artifact.owner_id != owner_id
                    or artifact.task_id != run.task_id
                ):
                    raise ResourceNotFoundError("savepoint artifact not found")
            manifest = self._artifacts.get(request.manifest_artifact_id)
            if (
                manifest is None
                or manifest.owner_id != owner_id
                or manifest.task_id != run.task_id
                or manifest.run_id != run_id
                or manifest.kind != "savepoint_manifest"
                or manifest.version_no != request.manifest_version_no
                or manifest.sha256 != request.manifest_sha256
            ):
                raise ResourceNotFoundError("savepoint manifest Artifact version not found")
            record = SavepointRecord(
                savepoint_id=f"savepoint_{uuid4().hex}",
                owner_id=owner_id,
                task_id=run.task_id,
                run_id=run_id,
                thread_id=run.thread_id,
                **request.model_dump(exclude={"active_writer_generation"}),
                created_at=_now(),
            )
            self._savepoints[record.savepoint_id] = record
            self._remember(
                "savepoint", run_id, request.idempotency_key, request, record.savepoint_id
            )
            return record

    async def get_savepoint(
        self, owner_id: str, task_id: str, savepoint_id: str
    ) -> SavepointRecord | None:
        async with self._lock:
            item = self._savepoints.get(savepoint_id)
            if item is None or item.owner_id != owner_id or item.task_id != task_id:
                return None
            return item

    async def get_savepoint_by_idempotency(
        self, owner_id: str, run_id: str, idempotency_key: str
    ) -> SavepointRecord | None:
        async with self._lock:
            run = self._require_run(owner_id, run_id)
            return next(
                (
                    item
                    for item in self._savepoints.values()
                    if item.owner_id == owner_id
                    and item.task_id == run.task_id
                    and item.run_id == run_id
                    and item.idempotency_key == idempotency_key
                ),
                None,
            )

    async def list_savepoints(self, owner_id: str, task_id: str) -> tuple[SavepointRecord, ...]:
        async with self._lock:
            self._require_task(owner_id, task_id)
            return tuple(
                sorted(
                    (
                        item
                        for item in self._savepoints.values()
                        if item.owner_id == owner_id and item.task_id == task_id
                    ),
                    key=lambda item: (item.created_at, item.savepoint_id),
                )
            )


__all__ = ["ArtifactRepositoryMixin"]
