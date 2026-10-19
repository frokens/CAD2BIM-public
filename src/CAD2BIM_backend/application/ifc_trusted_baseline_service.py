"""Internal trusted test preparation through the existing durable IFC writer."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from time import monotonic

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.ifc_model_service import IfcInvocationSupervisor
from CAD2BIM_backend.application.ifc_revision_writer import IfcRevisionWriter
from CAD2BIM_backend.application.ifc_write_coordinator import IfcWriteCoordinator
from CAD2BIM_backend.application.ifc_write_support import require_ifc_idempotency_key
from CAD2BIM_backend.domain.control_plane import ArtifactRecord
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef,
    IFC_TRUSTED_BASELINE_LOAD_INTENT_SUMMARY,
    IfcTrustedBaselineLoadOperationDetail,
)
from CAD2BIM_backend.domain.ifc_model import (
    IfcCommittedVerificationError,
    IfcModelIntegrityError,
    IfcModelValidationError,
    IfcRunIdentity,
    IfcSettledOperationError,
)
from CAD2BIM_backend.domain.runtime_execution import IFC_REVISION_INVOCATION_EXECUTION
from CAD2BIM_backend.ports.ifc_revision_runtime import IfcRevisionRuntimeError


_TOOL_NAME = "ifc_trusted_baseline_load"


@dataclass(frozen=True, slots=True)
class _ArtifactRevisionSource:
    """Borrow repeatable bytes from the sole registered immutable source."""

    artifacts: ArtifactService
    artifact: ArtifactRecord

    @property
    def size_bytes(self) -> int:
        return self.artifact.size_bytes

    @property
    def sha256(self) -> str:
        return self.artifact.sha256

    async def iter_chunks(self, *, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        # ArtifactService performs its existing exact metadata/storage checks.
        del chunk_size
        stream = await self.artifacts.stream_version(
            self.artifact.owner_id, self.artifact.artifact_id, self.artifact.version_id,
        )
        async for chunk in stream.chunks:
            yield chunk


class IfcTrustedBaselineService:
    """Load a registered test baseline; never exposed as an authoring tool."""

    def __init__(
        self,
        writer: IfcRevisionWriter,
        artifacts: ArtifactService,
        coordinator: IfcWriteCoordinator,
        *,
        supervisor: IfcInvocationSupervisor | None = None,
    ) -> None:
        self._writer = writer
        self._artifacts = artifacts
        self._coordinator = coordinator
        self._supervisor = supervisor or IfcInvocationSupervisor()

    async def load(
        self, scope: IfcRunIdentity, source_asset: ArtifactVersionRef, *, idempotency_key: str,
    ) -> str:
        """Return the original terminal Operation alias for this exact source."""
        key = require_ifc_idempotency_key(idempotency_key)
        detail = IfcTrustedBaselineLoadOperationDetail(source_asset=source_asset)
        payload = detail.persisted_payload()
        digest = hashlib.sha256(detail.model_dump_json(exclude_none=True).encode()).hexdigest()
        request = dict(tool_name=_TOOL_NAME, idempotency_key=key, request_digest=digest,
                       intent_summary=IFC_TRUSTED_BASELINE_LOAD_INTENT_SUMMARY,
                       operation_detail=payload)

        async def recovered_alias() -> str | None:
            original = await self._writer.recover_revision(scope, **request)
            return original.operation_alias if original is not None else None

        async def supervised() -> str:
            original = await recovered_alias()
            if original is not None:
                return original
            prepared = await self._writer.prepare(scope, **request)
            claim = await self._writer.claim_execution(scope, prepared)

            async def attempt() -> str:
                resumed = await self._writer.resume_selected_revision(scope, prepared, claim)
                if resumed is not None:
                    return resumed[0].operation_alias
                try:
                    artifact = await self._artifacts.get_version_metadata(
                        scope.owner_id, source_asset.artifact_id, source_asset.version_id,
                        run_id=scope.run_id,
                    )
                    committed = await self._writer.commit_trusted_baseline(
                        scope, prepared, _ArtifactRevisionSource(self._artifacts, artifact), claim=claim,
                    )
                    return committed.operation_alias
                except IfcCommittedVerificationError:
                    # This terminal write committed but failed its independent
                    # readback. Preserve the frozen state and precise diagnostic.
                    raise
                except (IfcModelIntegrityError, IfcModelValidationError, IfcRevisionRuntimeError) as exc:
                    if isinstance(exc, IfcRevisionRuntimeError) and exc.retryable:
                        raise
                    # Reconcile first: a lost response or selected candidate must
                    # never be rewritten as a failed source-validation attempt.
                    original = await recovered_alias()
                    if original is not None:
                        return original
                    resumed = await self._writer.resume_selected_revision(scope, prepared, claim)
                    if resumed is not None:
                        return resumed[0].operation_alias
                    alias = await self._writer.fail_prepared(
                        scope, prepared, claim=claim,
                        error_code="IFC_TRUSTED_BASELINE_INVALID", error_message=str(exc),
                    )
                    raise IfcSettledOperationError(
                        code="IFC_TRUSTED_BASELINE_INVALID", message=str(exc),
                        phase="ifc_validation", operation_alias=alias,
                    ) from exc

            async def pause(last_error: Exception) -> None:
                del last_error
                await self._writer.freeze_indeterminate(
                    scope, prepared, claim=claim, reason_code="IFC_INVOCATION_RETRIES_EXHAUSTED",
                )

            async with self._writer.execution_invocation(scope, claim, IFC_REVISION_INVOCATION_EXECUTION):
                return await self._supervisor.invoke(
                    attempt, on_exhausted=pause,
                    non_retryable_errors=(IfcSettledOperationError, IfcCommittedVerificationError),
                )

        return await self._coordinator.invoke(
            scope, logical_operation_identity=f"{_TOOL_NAME}:{key}:{digest}",
            operation=supervised,
            deadline=monotonic() + IFC_REVISION_INVOCATION_EXECUTION.total_seconds,
        )


__all__ = ["IfcTrustedBaselineService"]
