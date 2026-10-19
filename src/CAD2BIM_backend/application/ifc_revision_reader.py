"""Verified exact reads for the Run-bound durable IFC model."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from CAD2BIM_backend.domain.ifc_model import (
    ExactIfcRevision,
    IfcModelFrozenError,
    IfcModelIntegrityError,
    IfcModelNotInitializedError,
    IfcModelState,
    IfcRunIdentity,
)
from CAD2BIM_backend.ports.ifc_model import IfcModelRepository
from CAD2BIM_backend.ports.ifc_revision_reader import VerifiedExactIfcRevision
from CAD2BIM_backend.ports.ifc_revision_runtime import (
    IfcRevisionChunkSource, IfcRevisionRuntimePort, InspectExactIfcRevisionRequest, InspectedIfcRevision,
)
from CAD2BIM_backend.ports.storage import StorageKey, StoragePort, StoredObject


@dataclass(frozen=True, slots=True)
class _StoredIfcRevisionSource:
    storage: StoragePort
    key: StorageKey
    expected: StoredObject
    size_bytes: int
    sha256: str

    def iter_chunks(self, *, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        return self.storage.iter_chunks(
            self.key,
            chunk_size=chunk_size,
            expected=self.expected,
        )


class IfcRevisionReader:
    """Pin revision identities and independently verify their immutable bytes."""

    def __init__(
        self,
        repository: IfcModelRepository,
        storage: StoragePort,
        runtime: IfcRevisionRuntimePort,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._runtime = runtime

    async def pin_current(self, scope: IfcRunIdentity) -> ExactIfcRevision:
        """Pin CURRENT before any fallible Storage or IFC inspection work."""

        await self._repository.require_invocation_allowed(scope)
        exact = await self._repository.get_current(scope)
        if exact is None:
            raise IfcModelNotInitializedError("the selected Run has no IfcModel")
        self._validate_visible_exact(scope, exact, current=True)
        return exact

    async def pin_exact(
        self,
        scope: IfcRunIdentity,
        *,
        revision_id: str,
    ) -> ExactIfcRevision:
        """Resolve one server-owned immutable revision identity."""

        await self._repository.require_invocation_allowed(scope)
        exact = await self._repository.get_exact(scope, revision_id=revision_id)
        if exact is None:
            raise IfcModelNotInitializedError("the requested IFC revision is not visible")
        self._validate_visible_exact(scope, exact, current=False)
        return exact

    async def read_exact(
        self,
        exact: ExactIfcRevision,
    ) -> VerifiedExactIfcRevision:
        """Verify one previously pinned immutable revision."""

        key = _storage_key(exact)
        stored = await self._storage.stat(key)
        if stored.key != key:
            raise IfcModelIntegrityError("Storage returned a different logical IFC key")
        if any(
            (
                stored.filename != exact.artifact.filename,
                stored.content_type != exact.artifact.content_type,
                stored.size_bytes != exact.artifact.size_bytes,
                stored.sha256 != exact.artifact.sha256,
            )
        ):
            raise IfcModelIntegrityError("Storage metadata does not match exact ArtifactVersion")
        result = await self._runtime.execute(
            InspectExactIfcRevisionRequest(),
            source=_StoredIfcRevisionSource(self._storage, key, stored, stored.size_bytes, stored.sha256),
        )
        if not isinstance(result, InspectedIfcRevision):
            raise IfcModelIntegrityError("IFC runtime returned an invalid exact inspection")
        inspection = result.inspection
        if any(
            (
                inspection.project_global_id != exact.project_global_id,
                inspection.size_bytes != exact.artifact.size_bytes,
                inspection.sha256 != exact.artifact.sha256,
            )
        ):
            raise IfcModelIntegrityError("exact IFC bytes do not match committed metadata")
        verified = await self._storage.read_verified(key)
        if verified.stored.size_bytes != inspection.size_bytes or verified.verified_sha256 != inspection.sha256:
            raise IfcModelIntegrityError("exact IFC bytes changed after supervised inspection")
        return VerifiedExactIfcRevision(
            exact=exact,
            content=verified.content,
            inspection=inspection,
        )

    async def open_exact(
        self,
        exact: ExactIfcRevision,
    ) -> IfcRevisionChunkSource:
        """Open a repeatable verified stream without loading the IFC into Backend memory."""

        key = _storage_key(exact)
        stored = await self._storage.stat(key)
        if stored.key != key:
            raise IfcModelIntegrityError("Storage returned a different logical IFC key")
        if any(
            (
                stored.filename != exact.artifact.filename,
                stored.content_type != exact.artifact.content_type,
                stored.size_bytes != exact.artifact.size_bytes,
                stored.sha256 != exact.artifact.sha256,
            )
        ):
            raise IfcModelIntegrityError("Storage metadata does not match exact ArtifactVersion")
        return _StoredIfcRevisionSource(
            storage=self._storage,
            key=key,
            expected=stored,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
        )

    @staticmethod
    def _validate_visible_exact(
        scope: IfcRunIdentity,
        exact: ExactIfcRevision,
        *,
        current: bool,
    ) -> None:
        if exact.scope != scope:
            kind = "current" if current else "exact"
            raise IfcModelIntegrityError(f"{kind} IFC revision does not match the requested Run")
        if exact.model_state is IfcModelState.FROZEN:
            raise IfcModelFrozenError("IFC model is frozen pending reconciliation")


def _storage_key(exact: ExactIfcRevision) -> StorageKey:
    return StorageKey(
        owner_id=exact.scope.owner_id,
        task_id=exact.scope.task_id,
        object_id=exact.artifact.artifact_id,
        version=1,
    )


__all__ = ["IfcRevisionReader"]
