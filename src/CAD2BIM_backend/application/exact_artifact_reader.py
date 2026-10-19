"""Match a domain-resolved reference to an authorized, verified ArtifactVersion."""

from __future__ import annotations

from typing import Protocol

from CAD2BIM_backend.application.artifact_service import ArtifactDownload, ArtifactService
from CAD2BIM_backend.domain.invocation import InvocationScope


class ExactArtifactVersionReference(Protocol):
    artifact_id: str
    version_id: str
    sha256: str


class ExactArtifactReadError(RuntimeError):
    """The already-resolved exact ArtifactVersion is unavailable or inconsistent."""


class ExactArtifactReader:
    """Check domain scope and reference; ArtifactService owns byte integrity."""

    def __init__(self, artifacts: ArtifactService) -> None:
        self._artifacts = artifacts

    async def read(
        self,
        scope: InvocationScope,
        reference: ExactArtifactVersionReference,
        *,
        expected_content_type: str,
    ) -> ArtifactDownload:
        try:
            downloaded = await self._artifacts.download_version(
                scope.owner_id,
                reference.artifact_id,
                reference.version_id,
                run_id=scope.run_id,
            )
        except Exception as exc:
            raise ExactArtifactReadError("exact ArtifactVersion is unavailable") from exc

        record = downloaded.artifact
        if (
            record.owner_id != scope.owner_id
            or record.task_id != scope.identity.task_id
            or record.run_id != scope.run_id
            or record.content_type != expected_content_type
            or record.artifact_id != reference.artifact_id
            or record.version_id != reference.version_id
            or record.sha256 != reference.sha256
        ):
            raise ExactArtifactReadError("exact ArtifactVersion failed integrity validation")
        return downloaded


__all__ = [
    "ExactArtifactReadError",
    "ExactArtifactReader",
    "ExactArtifactVersionReference",
]
