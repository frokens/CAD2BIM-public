"""Ports for the Run-bound durable IFC model kernel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Mapping
from typing import Protocol
from pydantic import Field, model_validator

from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationErrorPhase,
    CriticalOperationStatus,
    ProductReportPublication,
)
from CAD2BIM_backend.domain.product_apply import ProductApplySummary
from CAD2BIM_backend.domain.product_edit import ProductsEditSummary
from CAD2BIM_backend.domain.ifc_model import (
    EncodedIfcRevision,
    ExactIfcRevision,
    IfcExecutionClaim,
    IfcModelContract,
    IfcModelInitializeResult,
    IfcRevisionInspection,
    IfcRunIdentity,
    PreparedIfcInitialize,
    PreparedIfcRevision,
    VerifiedIfcPublication,
)
from CAD2BIM_backend.domain.ifc_project import IfcProjectSummary
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    IfcSpatialAppliedItem,
    IfcSpatialBlockerSummary,
    IfcSpatialNodeSummary,
    IfcSpatialStructureApplyResult,
)


@dataclass(frozen=True, slots=True)
class IfcInitializeOperationState:
    """Server-only initialization state used by the recovery reconciler."""

    prepared: PreparedIfcInitialize
    status: CriticalOperationStatus
    error_code: str | None = None
    error_message: str | None = None
    error_phase: CriticalOperationErrorPhase | None = None
    operation_alias: str | None = None


@dataclass(frozen=True, slots=True)
class IfcRevisionOperationState:
    """Server-only non-initial revision state used by the shared writer."""

    prepared: PreparedIfcRevision
    status: CriticalOperationStatus
    error_code: str | None = None
    error_message: str | None = None
    error_phase: CriticalOperationErrorPhase | None = None
    operation_alias: str | None = None
    project_result: IfcProjectSummary | None = None
    spatial_result: IfcSpatialStructureApplyResult | None = None
    failure_blockers: IfcSpatialBlockerSummary | None = None
    product_summary: ProductApplySummary | ProductsEditSummary | None = None
    report_package_id: str | None = None


@dataclass(frozen=True, slots=True)
class CommittedIfcRevision:
    """Server-only committed revision plus its repository-assigned Catalog alias."""

    operation_alias: str
    exact: ExactIfcRevision


class IfcSelectedCandidate(IfcModelContract):
    """One immutable, private IFC payload selected on the original Operation."""

    origin_generation: int = Field(gt=0)
    upload_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inspection: IfcRevisionInspection
    project_result: IfcProjectSummary | None = None
    spatial_applied: tuple[IfcSpatialAppliedItem, ...] | None = None
    spatial_summary: tuple[IfcSpatialNodeSummary, ...] | None = None
    product_report: ProductReportPublication | None = None
    product_failure_report: ProductReportPublication | None = None

    @model_validator(mode="after")
    def match_stored_bytes(self) -> "IfcSelectedCandidate":
        if (self.size_bytes != self.inspection.size_bytes
                or self.sha256 != self.inspection.sha256):
            raise ValueError("selected IFC candidate metadata differs from inspection")
        if (self.spatial_applied is None) != (self.spatial_summary is None):
            raise ValueError("selected spatial result must include applied and summary")
        return self


class PrivateIfcModel(Protocol):
    """Opaque process-local private copy; never a durable business identity."""

    def to_bytes(self) -> bytes: ...


class IfcModelCodecPort(Protocol):
    def stable_project_global_id(self, identity_seed: str) -> str: ...

    def create_revision1(
        self,
        *,
        project_global_id: str,
        project_name: str,
        header_timestamp: datetime,
    ) -> EncodedIfcRevision: ...

    def inspect(self, content: bytes) -> IfcRevisionInspection: ...

    def inspect_revision1(
        self,
        content: bytes,
        *,
        expected_project_global_id: str,
        expected_project_name: str,
    ) -> IfcRevisionInspection: ...

    def private_copy(self, content: bytes) -> PrivateIfcModel: ...


class IfcModelRepository(Protocol):
    """Atomic persistence boundary for model identity, revisions, and Operation state."""

    async def claim_execution(
        self, scope: IfcRunIdentity, *, operation_id: str, request_digest: str,
    ) -> IfcExecutionClaim: ...

    async def associate_execution_guard(
        self, scope: IfcRunIdentity, claim: IfcExecutionClaim,
        *, guardian_pid: int, guardian_birth_us: int,
    ) -> None: ...

    async def retire_execution(
        self, scope: IfcRunIdentity, claim: IfcExecutionClaim,
    ) -> None: ...

    async def get_selected_candidate(
        self, scope: IfcRunIdentity,
        prepared: PreparedIfcInitialize | PreparedIfcRevision,
        claim: IfcExecutionClaim,
    ) -> IfcSelectedCandidate | None: ...

    async def select_candidate(
        self, scope: IfcRunIdentity,
        prepared: PreparedIfcInitialize | PreparedIfcRevision,
        claim: IfcExecutionClaim,
        candidate: IfcSelectedCandidate,
    ) -> IfcSelectedCandidate: ...

    async def prepare_initialize(
        self,
        scope: IfcRunIdentity,
        *,
        request_digest: str,
        project_global_id: str,
        project_name: str,
    ) -> PreparedIfcInitialize: ...

    async def commit_initialize(
        self,
        scope: IfcRunIdentity,
        *,
        prepared: PreparedIfcInitialize,
        publication: VerifiedIfcPublication,
        claim: IfcExecutionClaim,
    ) -> IfcModelInitializeResult: ...

    async def get_current(
        self,
        scope: IfcRunIdentity,
    ) -> ExactIfcRevision | None: ...

    async def get_exact(
        self,
        scope: IfcRunIdentity,
        *,
        revision_id: str,
    ) -> ExactIfcRevision | None: ...

    async def get_revision_inspection(self, exact: ExactIfcRevision) -> IfcRevisionInspection | None:
        """Read the facts published with this exact immutable ArtifactVersion."""
        ...

    async def require_recovery_allowed(self, scope: IfcRunIdentity) -> None: ...

    async def require_invocation_allowed(
        self,
        scope: IfcRunIdentity,
    ) -> None: ...

    async def get_initialize_operation_state(
        self,
        scope: IfcRunIdentity,
        *,
        request_digest: str,
        project_global_id: str,
        project_name: str,
    ) -> IfcInitializeOperationState | None: ...

    async def reopen_indeterminate_initialize(
        self,
        scope: IfcRunIdentity,
        *,
        operation_id: str,
    ) -> None: ...

    async def fail_indeterminate_initialize(
        self,
        scope: IfcRunIdentity,
        *,
        operation_id: str,
        error_code: str,
        error_message: str,
    ) -> None: ...

    async def unfreeze_reconciled_model(
        self,
        scope: IfcRunIdentity,
        *,
        model_id: str,
    ) -> None: ...

    async def prepare_revision(
        self,
        scope: IfcRunIdentity,
        *,
        tool_name: str,
        idempotency_key: str,
        request_digest: str,
        intent_summary: str,
        operation_detail: Mapping[str, object],
    ) -> PreparedIfcRevision: ...

    async def get_revision_operation_state(
        self,
        scope: IfcRunIdentity,
        *,
        tool_name: str,
        idempotency_key: str,
        request_digest: str,
        intent_summary: str,
        operation_detail: Mapping[str, object],
    ) -> IfcRevisionOperationState | None: ...

    async def unfreeze_succeeded_revision(
        self,
        scope: IfcRunIdentity,
        *,
        operation_id: str,
        revision_id: str,
    ) -> None: ...

    async def reopen_indeterminate_revision(
        self,
        scope: IfcRunIdentity,
        *,
        operation_id: str,
    ) -> None: ...

    async def commit_revision(
        self,
        scope: IfcRunIdentity,
        *,
        prepared: PreparedIfcRevision,
        publication: VerifiedIfcPublication,
        claim: IfcExecutionClaim,
        project_result: IfcProjectSummary | None = None,
        spatial_applied: tuple[IfcSpatialAppliedItem, ...] | None = None,
        spatial_summary: tuple[IfcSpatialNodeSummary, ...] | None = None,
    ) -> CommittedIfcRevision: ...

    async def record_failed_ifc_operation(
        self,
        scope: IfcRunIdentity,
        *,
        operation_id: str,
        error_code: str,
        error_message: str,
        error_phase: CriticalOperationErrorPhase,
        claim: IfcExecutionClaim,
        spatial_blockers: IfcSpatialBlockerSummary | None = None,
        product_report: ProductReportPublication | None = None,
    ) -> str: ...

    async def freeze_indeterminate(
        self,
        scope: IfcRunIdentity,
        *,
        model_id: str | None,
        operation_id: str | None,
        reason_code: str,
        claim: IfcExecutionClaim | None = None,
    ) -> None: ...


__all__ = [
    "CommittedIfcRevision",
    "IfcInitializeOperationState",
    "IfcRevisionOperationState",
    "IfcSelectedCandidate",
    "IfcModelCodecPort",
    "IfcModelRepository",
    "PrivateIfcModel",
]
