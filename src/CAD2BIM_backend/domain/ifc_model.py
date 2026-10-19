"""Contracts for the Run-bound durable IFC model kernel.

Provider schemas are intentionally outside this module.  The public-safe
summaries below may be projected by a later transport adapter; exact revision,
Artifact, Operation, and writer identities remain server-only.
"""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from CAD2BIM_backend.domain.ifc_geometry_basis import IfcGeometryBasisSummary, IfcProductBodyContextFacts


IFC_TARGET_SCHEMA = "IFC4X3_ADD2"
IFC_TARGET_SCHEMA_FAMILY = "IFC4X3"
IFC_REVISION_CONTENT_TYPE = "application/x-ifc"
IFC_REVISION_FILENAME = "model.ifc"

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IFC_GLOBAL_ID_PATTERN = r"^[0-9A-Za-z_$]{22}$"
_OPERATION_ALIAS_PATTERN = r"^op_[0-9]{3,}$"


def _normalize_project_name(value: object) -> object:
    if not isinstance(value, str):
        return value
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("project name must not contain control characters")
    return value.strip()


class IfcModelError(Exception):
    """Base class for expected durable IFC kernel failures."""


class IfcModelNotInitializedError(IfcModelError, LookupError):
    """The selected Run has no successfully initialized IfcModel."""


class IfcModelAlreadyInitializedError(IfcModelError, ValueError):
    """A create-only initialization targeted a Run that already has a model."""


class IfcModelConflictError(IfcModelError, ValueError):
    """CURRENT or the IFC-local write epoch changed at the commit fence."""


class IfcInvocationIdentityConflictError(IfcModelConflictError):
    """A server-only logical invocation identity was reused inconsistently."""


class IfcModelFrozenError(IfcModelError, RuntimeError):
    """The model is fail-closed pending server-side reconciliation."""


class IfcModelIntegrityError(IfcModelError, RuntimeError):
    """Committed exact bytes or their durable binding failed integrity checks."""


class IfcCommittedVerificationError(IfcModelIntegrityError):
    """The commit exists, but its actual exact readback was not verified."""

    code = "IFC_COMMITTED_READBACK_FAILED"

    def __init__(self, message: str, *, operation_alias: str, revision_id: str) -> None:
        super().__init__(message)
        self.operation_alias = operation_alias
        self.revision_id = revision_id


class IfcModelValidationError(IfcModelError, ValueError):
    """Candidate IFC bytes do not satisfy the required kernel invariants."""


class IfcSettledOperationError(IfcModelError):
    """The original IFC Operation failed and cannot execute again under its identity."""

    def __init__(
        self, *, code: str, message: str, phase: str, operation_alias: str,
        diagnostic: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.operation_alias = operation_alias
        self.retryable = False
        self.diagnostic = diagnostic


class IfcModelContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InitializeIfcModelCommand(IfcModelContract):
    """Internal create-only command for the required business project name."""

    project_name: str = Field(min_length=1, max_length=255)

    @field_validator("project_name", mode="before")
    @classmethod
    def normalize_project_name(cls, value: object) -> object:
        return _normalize_project_name(value)


class IfcModelState(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"


class IfcProjectSummary(IfcModelContract):
    global_id: str = Field(pattern=_IFC_GLOBAL_ID_PATTERN)
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        return _normalize_project_name(value)


class IfcModelInfo(IfcModelContract):
    """Bounded initialized-model summary with no durable internal identities."""

    ifc_schema: Literal["IFC4X3_ADD2"] = IFC_TARGET_SCHEMA
    project: IfcProjectSummary
    size_bytes: int = Field(
        gt=0, description="Byte count of the exact persisted IFC file represented by this summary."
    )
    geometry_basis_configured: bool = False
    geometry_basis: IfcGeometryBasisSummary | None = None
    site_count: int = Field(default=0, ge=0)
    element_count: int = Field(default=0, ge=0)
    source_exact: ExactIfcRevision | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_geometry_basis(self) -> "IfcModelInfo":
        if self.geometry_basis_configured != (self.geometry_basis is not None):
            raise ValueError(
                "geometry_basis_configured must match the exact geometry_basis summary"
            )
        return self


class IfcModelInitializeResult(IfcModelContract):
    """Public-safe success summary returned by the internal initialize use case."""

    operation_alias: str = Field(pattern=_OPERATION_ALIAS_PATTERN, max_length=32)
    model: IfcModelInfo


class IfcGeometryBasisConfigureResult(IfcModelContract):
    """Public-safe result of one committed geometry-basis revision."""

    operation_alias: str = Field(pattern=_OPERATION_ALIAS_PATTERN, max_length=32)
    model: IfcModelInfo


class IfcRunIdentity(IfcModelContract):
    """Server-only tenant and Run binding."""

    owner_id: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(min_length=1, max_length=80)
    task_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)


class IfcExecutionClaim(IfcModelContract):
    """Private proof for one execution of an original prepared IFC Operation."""

    operation_id: str = Field(min_length=1, max_length=80)
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    owner: str = Field(min_length=1, max_length=160)
    generation: int = Field(gt=0)
    token: str = Field(min_length=1)
    deadline_at: datetime
    remaining_seconds: float = Field(gt=0)

    @field_validator("deadline_at")
    @classmethod
    def require_aware_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline_at must be timezone-aware")
        return value


class PreparedIfcInitialize(IfcModelContract):
    """Server-only stable identities persisted before initialization side effects."""

    scope: IfcRunIdentity
    operation_id: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=255)
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    model_id: str = Field(min_length=1, max_length=80)
    revision_id: str = Field(min_length=1, max_length=80)
    artifact_id: str = Field(min_length=1, max_length=80)
    artifact_version_id: str = Field(min_length=1, max_length=80)
    project_global_id: str = Field(pattern=_IFC_GLOBAL_ID_PATTERN)
    prepared_at: datetime

    @field_validator("prepared_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("prepared_at must be timezone-aware")
        return value


class ExactIfcArtifactVersion(IfcModelContract):
    """Server-only immutable ArtifactVersion identity for one IFC revision."""

    artifact_id: str = Field(min_length=1, max_length=80)
    version_id: str = Field(min_length=1, max_length=80)
    version_no: Literal[1] = 1
    filename: Literal["model.ifc"] = IFC_REVISION_FILENAME
    content_type: Literal["application/x-ifc"] = IFC_REVISION_CONTENT_TYPE
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=_SHA256_PATTERN)


class ExactIfcRevision(IfcModelContract):
    """Server-only pinned revision binding used for exact reads and recovery."""

    scope: IfcRunIdentity
    model_id: str = Field(min_length=1, max_length=80)
    model_state: IfcModelState
    revision_id: str = Field(min_length=1, max_length=80)
    revision_no: int = Field(ge=1)
    write_epoch: int = Field(ge=1)
    artifact: ExactIfcArtifactVersion
    project_global_id: str = Field(pattern=_IFC_GLOBAL_ID_PATTERN)


class PreparedIfcRevision(IfcModelContract):
    """Server-only private-copy frontier fixed before a mutation is dispatched."""

    operation_id: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=255)
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    before: ExactIfcRevision
    after_revision_id: str = Field(min_length=1, max_length=80)
    artifact_id: str = Field(min_length=1, max_length=80)
    artifact_version_id: str = Field(min_length=1, max_length=80)
    prepared_at: datetime

    @field_validator("prepared_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("prepared_at must be timezone-aware")
        return value


class IfcRevisionInspection(IfcModelContract):
    """Server-only facts independently derived from exact IFC-SPF bytes."""

    schema_identifier: Literal["IFC4X3_ADD2"]
    project_global_id: str = Field(pattern=_IFC_GLOBAL_ID_PATTERN)
    project_name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    site_count: int = Field(ge=0)
    element_count: int = Field(ge=0)
    unit_assignment_count: int = Field(ge=0)
    geometric_context_count: int = Field(ge=0)
    # None means these optional derived facts were not recorded; () is an
    # actual inspection that found no Body subcontext.
    product_body_contexts: tuple[IfcProductBodyContextFacts, ...] | None = None
    project_length_unit_m: float | None = Field(default=None, gt=0)
    geometry_basis_configured: bool
    geometry_basis: IfcGeometryBasisSummary | None = None

    @field_validator("project_name", mode="before")
    @classmethod
    def normalize_project_name(cls, value: object) -> object:
        return _normalize_project_name(value)

    @model_validator(mode="after")
    def validate_geometry_basis(self) -> "IfcRevisionInspection":
        if self.geometry_basis_configured != (self.geometry_basis is not None):
            raise ValueError(
                "geometry_basis_configured must match the exact geometry_basis inspection"
            )
        return self

    def matches_recorded_facts(self, other: "IfcRevisionInspection") -> bool:
        """Compare every recorded fact without treating absent derived facts as a mismatch."""
        exclude = ({"product_body_contexts", "project_length_unit_m"}
                   if self.product_body_contexts is None or other.product_body_contexts is None else set())
        return self.model_dump(exclude=exclude) == other.model_dump(exclude=exclude)

    def to_safe_info(self) -> IfcModelInfo:
        return IfcModelInfo(
            ifc_schema=IFC_TARGET_SCHEMA,
            project=IfcProjectSummary(
                global_id=self.project_global_id,
                name=self.project_name,
            ),
            size_bytes=self.size_bytes,
            geometry_basis_configured=self.geometry_basis_configured,
            geometry_basis=self.geometry_basis,
            site_count=self.site_count,
            element_count=self.element_count,
        )


class EncodedIfcRevision(IfcModelContract):
    """Short-lived server-only candidate bytes and their codec inspection."""

    content: bytes
    inspection: IfcRevisionInspection

    @model_validator(mode="after")
    def validate_content_digest(self) -> "EncodedIfcRevision":
        if not self.content:
            raise ValueError("encoded IFC revision content must not be empty")
        if len(self.content) != self.inspection.size_bytes:
            raise ValueError("encoded IFC revision size does not match inspection")
        if hashlib.sha256(self.content).hexdigest() != self.inspection.sha256:
            raise ValueError("encoded IFC revision digest does not match inspection")
        return self


class VerifiedIfcPublication(IfcModelContract):
    """Server-only Storage publication verified by rereading exact bytes."""

    scope: IfcRunIdentity
    artifact: ExactIfcArtifactVersion
    inspection: IfcRevisionInspection
    storage_created_at: datetime

    @field_validator("storage_created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("storage_created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_exact_metadata(self) -> "VerifiedIfcPublication":
        if (
            self.artifact.size_bytes != self.inspection.size_bytes
            or self.artifact.sha256 != self.inspection.sha256
        ):
            raise ValueError("verified Storage metadata does not match IFC inspection")
        return self


__all__ = [
    "EncodedIfcRevision",
    "ExactIfcArtifactVersion",
    "ExactIfcRevision",
    "IFC_REVISION_CONTENT_TYPE",
    "IFC_REVISION_FILENAME",
    "IFC_TARGET_SCHEMA",
    "IFC_TARGET_SCHEMA_FAMILY",
    "IfcModelAlreadyInitializedError",
    "IfcModelConflictError",
    "IfcModelContract",
    "IfcModelError",
    "IfcModelFrozenError",
    "IfcGeometryBasisConfigureResult",
    "IfcExecutionClaim",
    "IfcInvocationIdentityConflictError",
    "IfcModelInfo",
    "IfcModelInitializeResult",
    "IfcModelIntegrityError",
    "IfcModelNotInitializedError",
    "IfcModelState",
    "IfcModelValidationError",
    "IfcSettledOperationError",
    "IfcProjectSummary",
    "IfcRevisionInspection",
    "IfcRunIdentity",
    "InitializeIfcModelCommand",
    "PreparedIfcInitialize",
    "PreparedIfcRevision",
    "VerifiedIfcPublication",
]
