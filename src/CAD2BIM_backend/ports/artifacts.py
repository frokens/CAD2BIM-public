"""Artifact Registry persistence port."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from CAD2BIM_backend.domain.control_plane import ArtifactRecord, TaskRecord


class ArtifactRepository(Protocol):
    async def get_task(self, owner_id: str, task_id: str) -> TaskRecord | None: ...

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
    ) -> ArtifactRecord: ...

    async def get_artifact(
        self,
        owner_id: str,
        artifact_id: str,
        *,
        run_id: str | None = None,
    ) -> ArtifactRecord | None: ...

    async def get_artifact_version(
        self,
        owner_id: str,
        artifact_id: str,
        version_id: str,
        *,
        run_id: str | None = None,
    ) -> ArtifactRecord | None: ...

    async def get_artifact_version_number(
        self,
        owner_id: str,
        artifact_id: str,
        version_no: int,
    ) -> ArtifactRecord | None: ...


__all__ = ["ArtifactRepository"]
