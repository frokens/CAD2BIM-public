"""Application service coordinating immutable storage and artifact metadata."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from CAD2BIM_backend.application.errors import ConflictError, NotFoundError, ValidationError
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.control_plane import ArtifactRecord
from CAD2BIM_backend.ports.artifacts import ArtifactRepository
from CAD2BIM_backend.ports.storage import (
    StorageKey,
    StorageConflictError,
    StorageObjectNotFoundError,
    StoragePort,
    StorageValidationError,
    StoredObject,
)

_MAX_VERSION_METADATA_BYTES = 16_384
_MAX_VERSION_METADATA_DEPTH = 16
_SENSITIVE_METADATA_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "bearer",
        "code_verifier",
        "cookie",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class ArtifactDownload:
    artifact: ArtifactRecord
    content: bytes


@dataclass(frozen=True, slots=True)
class ArtifactStream:
    artifact: ArtifactRecord
    chunks: AsyncIterable[bytes]


class ArtifactService:
    """Preflight metadata, publish bytes, then register immutable identity."""

    def __init__(self, repository: ArtifactRepository, storage: StoragePort) -> None:
        self._repository = repository
        self._storage = storage

    async def upload(
        self,
        *,
        owner_id: str,
        task_id: str,
        filename: str,
        content_type: str,
        chunks: AsyncIterable[bytes],
        idempotency_key: str | None = None,
        kind: str = "input",
        run_id: str | None = None,
        version_metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        if await self._repository.get_task(owner_id, task_id) is None:
            raise NotFoundError("task not found")
        immutable_metadata = _normalize_version_metadata(version_metadata)
        effective_key = idempotency_key or f"upload_{uuid4().hex}"
        artifact_id = _artifact_id_for_idempotency(
            owner_id,
            task_id,
            effective_key,
        )
        version_no = 1
        storage_key = StorageKey(owner_id, task_id, artifact_id, version_no)
        try:
            stored = await self._storage.write(
                storage_key,
                filename=filename,
                content_type=content_type,
                chunks=chunks,
            )
        except StorageValidationError as exc:
            raise ValidationError(str(exc)) from exc
        except StorageConflictError as exc:
            raise ConflictError(str(exc)) from exc
        registration = {
            "artifact_id": artifact_id,
            "version_no": version_no,
            "filename": stored.filename,
            "content_type": stored.content_type,
            "size_bytes": stored.size_bytes,
            "sha256": stored.sha256,
            "idempotency_key": effective_key,
            "kind": kind,
            "run_id": run_id,
        }
        if immutable_metadata:
            registration["version_metadata"] = immutable_metadata
        try:
            return await self._repository.register_artifact(
                owner_id,
                task_id,
                **registration,
            )
        except Exception:
            recovered = await self._recover_after_registration_error(
                owner_id,
                artifact_id,
                stored,
                run_id=run_id,
                version_metadata=immutable_metadata,
            )
            if recovered is not None:
                return recovered
            # A concurrent registrar may commit after the recovery read. Deleting the
            # shared immutable object here can create metadata that points to no bytes.
            # Unreferenced publications are reclaimed only by a durable, delayed GC.
            raise

    async def upload_bytes(
        self,
        *,
        owner_id: str,
        task_id: str,
        filename: str,
        content_type: str,
        content: bytes,
        idempotency_key: str | None = None,
        kind: str = "input",
        run_id: str | None = None,
        version_metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        async def one_chunk():
            yield content

        return await self.upload(
            owner_id=owner_id,
            task_id=task_id,
            filename=filename,
            content_type=content_type,
            chunks=one_chunk(),
            idempotency_key=idempotency_key,
            kind=kind,
            run_id=run_id,
            version_metadata=version_metadata,
        )

    async def get_metadata(self, owner_id: str, artifact_id: str) -> ArtifactRecord:
        artifact = await self._repository.get_artifact(owner_id, artifact_id)
        if artifact is None:
            raise NotFoundError("artifact not found")
        return artifact

    async def find_by_idempotency_key(
        self,
        owner_id: str,
        task_id: str,
        idempotency_key: str,
    ) -> ArtifactRecord | None:
        """Resolve the immutable identity used by ``upload`` without a second index."""

        artifact_id = _artifact_id_for_idempotency(
            owner_id,
            task_id,
            idempotency_key,
        )
        artifact = await self._repository.get_artifact(owner_id, artifact_id)
        if artifact is None:
            return None
        if artifact.task_id != task_id or artifact.idempotency_key != idempotency_key:
            raise ConflictError("artifact idempotency identity changed")
        return artifact

    async def get_version_metadata(
        self,
        owner_id: str,
        artifact_id: str,
        version_id: str,
        *,
        run_id: str | None = None,
    ) -> ArtifactRecord:
        if run_id is None:
            artifact = await self._repository.get_artifact_version(
                owner_id,
                artifact_id,
                version_id,
            )
        else:
            artifact = await self._repository.get_artifact_version(
                owner_id,
                artifact_id,
                version_id,
                run_id=run_id,
            )
        if artifact is None:
            raise NotFoundError("artifact version not found")
        return artifact

    async def get_version_metadata_by_number(
        self,
        owner_id: str,
        artifact_id: str,
        version_no: int,
    ) -> ArtifactRecord:
        artifact = await self._repository.get_artifact_version_number(
            owner_id,
            artifact_id,
            version_no,
        )
        if artifact is None:
            raise NotFoundError("artifact version not found")
        return artifact

    async def download(self, owner_id: str, artifact_id: str) -> ArtifactDownload:
        artifact = await self.get_metadata(owner_id, artifact_id)
        return await self._download_record(artifact)

    async def download_version(
        self,
        owner_id: str,
        artifact_id: str,
        version_id: str,
        *,
        run_id: str | None = None,
    ) -> ArtifactDownload:
        """Download one exact immutable ArtifactVersion without consulting current."""

        artifact = await self.get_version_metadata(
            owner_id,
            artifact_id,
            version_id,
            run_id=run_id,
        )
        return await self._download_record(artifact)

    async def download_version_by_number(
        self,
        owner_id: str,
        artifact_id: str,
        version_no: int,
    ) -> ArtifactDownload:
        artifact = await self.get_version_metadata_by_number(
            owner_id,
            artifact_id,
            version_no,
        )
        return await self._download_record(artifact)

    async def _download_record(self, artifact: ArtifactRecord) -> ArtifactDownload:
        key = StorageKey(
            artifact.owner_id,
            artifact.task_id,
            artifact.artifact_id,
            artifact.version_no,
        )
        try:
            verified = await self._storage.read_verified(key)
        except StorageObjectNotFoundError as exc:
            raise ConflictError("artifact payload is missing") from exc
        except (StorageConflictError, StorageValidationError) as exc:
            raise ConflictError("artifact payload failed integrity validation") from exc
        stored = verified.stored
        if (
            stored.key != key
            or stored.filename != artifact.filename
            or stored.content_type != artifact.content_type
            or stored.size_bytes != artifact.size_bytes
            or stored.sha256 != artifact.sha256
            or verified.verified_sha256 != artifact.sha256
            or len(verified.content) != artifact.size_bytes
        ):
            raise ConflictError("artifact metadata does not match its stored payload")
        return ArtifactDownload(artifact=artifact, content=verified.content)

    async def stream(self, owner_id: str, artifact_id: str) -> ArtifactStream:
        artifact = await self.get_metadata(owner_id, artifact_id)
        return await self._stream_record(artifact)

    async def stream_version(
        self,
        owner_id: str,
        artifact_id: str,
        version_id: str,
    ) -> ArtifactStream:
        artifact = await self.get_version_metadata(owner_id, artifact_id, version_id)
        return await self._stream_record(artifact)

    async def _stream_record(self, artifact: ArtifactRecord) -> ArtifactStream:
        key = StorageKey(
            artifact.owner_id,
            artifact.task_id,
            artifact.artifact_id,
            artifact.version_no,
        )
        try:
            stored = await self._storage.stat(key)
        except StorageObjectNotFoundError as exc:
            raise ConflictError("artifact payload is missing") from exc
        except (StorageConflictError, StorageValidationError) as exc:
            raise ConflictError("artifact payload failed integrity validation") from exc
        if (
            stored.filename != artifact.filename
            or stored.content_type != artifact.content_type
            or stored.size_bytes != artifact.size_bytes
            or stored.sha256 != artifact.sha256
        ):
            raise ConflictError("artifact metadata does not match its stored payload")
        return ArtifactStream(
            artifact=artifact,
            chunks=self._storage.iter_chunks(key, expected=stored),
        )

    async def _recover_after_registration_error(
        self,
        owner_id: str,
        artifact_id: str,
        stored: StoredObject,
        *,
        run_id: str | None,
        version_metadata: Mapping[str, Any],
    ) -> ArtifactRecord | None:
        """Resolve an ambiguous DB commit without deleting a committed payload."""

        if run_id is None:
            artifact = await self._repository.get_artifact(owner_id, artifact_id)
        else:
            artifact = await self._repository.get_artifact(
                owner_id,
                artifact_id,
                run_id=run_id,
            )
        if artifact is None:
            return None
        if (
            artifact.filename == stored.filename
            and artifact.content_type == stored.content_type
            and artifact.size_bytes == stored.size_bytes
            and artifact.sha256 == stored.sha256
            and artifact.version_metadata == dict(version_metadata)
        ):
            return artifact
        raise ConflictError("artifact metadata does not match its stored payload")


def _normalize_version_metadata(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not value:
        return {}
    _reject_sensitive_metadata(value)
    try:
        encoded = canonical_json(value)
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValidationError("artifact version metadata is outside canonical JSON") from exc
    if len(encoded.encode("utf-8")) > _MAX_VERSION_METADATA_BYTES:
        raise ValidationError("artifact version metadata exceeds its size limit")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ValidationError("artifact version metadata must be an object")
    return decoded


def _artifact_id_for_idempotency(
    owner_id: str,
    task_id: str,
    idempotency_key: str,
) -> str:
    return f"artifact_{uuid5(NAMESPACE_URL, f'cad2bim:{owner_id}:{task_id}:{idempotency_key}').hex}"


def _reject_sensitive_metadata(value: Any, *, depth: int = 0) -> None:
    if depth > _MAX_VERSION_METADATA_DEPTH:
        raise ValidationError("artifact version metadata exceeds its depth limit")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            normalized = key.strip().lower().replace("-", "_")
            if (
                normalized in _SENSITIVE_METADATA_KEYS
                or normalized.endswith("_api_key")
                or normalized.endswith("_password")
                or normalized.endswith("_secret")
                or normalized.endswith("_token")
            ):
                raise ValidationError(f"artifact version metadata contains sensitive field: {key}")
            _reject_sensitive_metadata(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_sensitive_metadata(item, depth=depth + 1)


__all__ = ["ArtifactDownload", "ArtifactService", "ArtifactStream"]
