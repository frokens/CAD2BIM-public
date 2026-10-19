"""Durable Task-scoped geometry query attempt contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from CAD2BIM_backend.domain.geometry_inspection import (
    GeometryInspectionCoordinates,
    GeometryInspectionSelection,
    require_inspection_coordinate_selection,
)
from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometryFormat


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_DIAGNOSTIC_CODE_PATTERN = r"^[A-Z][A-Z0-9_]{0,63}$"


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("geometry query timestamps must be timezone-aware")
    return value


class GeometryQueryContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GeometryQueryAttemptStatus(StrEnum):
    PREPARED = "PREPARED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class GeometryQueryTargetKind(StrEnum):
    """Current exact targets plus retired values needed to decode old records."""

    GEOMETRY_RESOURCE_VERSION = "geometry_resource_version"
    GEOMETRY_INSPECTION = "geometry_inspection"
    FAMILY_STANDARD_MODEL = "family_standard_model"
    PRODUCT_REALIZATION_PREVIEW = "product_realization_preview"
    GEOMETRY_PREVIEW = "geometry_preview"


class GeometryQueryArtifactRef(GeometryQueryContract):
    """Exact immutable ArtifactVersion used by or produced by a query attempt."""

    artifact_id: str = Field(min_length=1, max_length=80)
    version_id: str = Field(min_length=1, max_length=80)
    sha256: str = Field(pattern=_SHA256_PATTERN)


class GeometryQueryDiagnostic(GeometryQueryContract):
    """Small persisted diagnostic safe to return at the Provider boundary."""

    code: str = Field(pattern=_DIAGNOSTIC_CODE_PATTERN)
    message: str = Field(min_length=1, max_length=2000)
    phase: Literal["read", "target", "preflight", "execution", "output_validation"] = "execution"
    retryable: bool = False

    @field_validator("message")
    @classmethod
    def canonical_message(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("geometry query diagnostic message must be trimmed")
        return value


class GeometryQueryAttemptPreparation(GeometryQueryContract):
    """Server-authored immutable identity and inputs for one fresh or repair attempt."""

    attempt_id: str = Field(pattern=_ID_PATTERN, max_length=80)
    origin_run_id: str | None = Field(default=None, min_length=1, max_length=80)
    target_kind: GeometryQueryTargetKind = GeometryQueryTargetKind.GEOMETRY_RESOURCE_VERSION
    resource_version_id: str | None = Field(default=None, min_length=1, max_length=80)
    family_version_id: str | None = Field(default=None, min_length=1, max_length=80)
    target_artifact: GeometryQueryArtifactRef | None = None
    target_geometry_format: CanonicalGeometryFormat | None = None
    selection: GeometryInspectionSelection | None = None
    coordinate_space: GeometryInspectionCoordinates | None = None
    source_artifact: GeometryQueryArtifactRef
    repair_parent_attempt_id: str | None = Field(default=None, min_length=1, max_length=80)
    request_identity: str = Field(pattern=_ID_PATTERN, max_length=128)
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_aware_created_at(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def reject_self_parent(self) -> Self:
        if self.repair_parent_attempt_id == self.attempt_id:
            raise ValueError("geometry query repair parent cannot be the attempt itself")
        if self.target_kind not in {
            GeometryQueryTargetKind.GEOMETRY_RESOURCE_VERSION,
            GeometryQueryTargetKind.GEOMETRY_INSPECTION,
            GeometryQueryTargetKind.FAMILY_STANDARD_MODEL,
        }:
            raise ValueError("new geometry queries require an exact mapped resource version or saved inspection; retired preview targets are unavailable")
        _validate_target_shape(self)
        return self


class GeometryQueryAttemptTerminal(GeometryQueryContract):
    """The only mutable projection of an already prepared attempt."""

    status: GeometryQueryAttemptStatus
    result_artifact: GeometryQueryArtifactRef | None = None
    diagnostics: tuple[GeometryQueryDiagnostic, ...] = Field(default=(), max_length=32)
    finished_at: datetime

    @field_validator("finished_at")
    @classmethod
    def require_aware_finished_at(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def require_terminal_shape(self) -> Self:
        if self.status is GeometryQueryAttemptStatus.PREPARED:
            raise ValueError("geometry query completion must be terminal")
        if self.status is GeometryQueryAttemptStatus.FAILED:
            if self.result_artifact is not None:
                raise ValueError("failed geometry query attempt cannot publish a result artifact")
            if not self.diagnostics:
                raise ValueError("failed geometry query attempt requires a diagnostic")
        elif self.result_artifact is None:
            raise ValueError("succeeded geometry query attempt requires a result artifact")
        return self


class GeometryQueryAttemptRecord(GeometryQueryContract):
    """Task-visible durable state for one exact geometry investigation."""

    attempt_id: str = Field(pattern=_ID_PATTERN, max_length=80)
    tenant_id: str = Field(pattern=_ID_PATTERN, max_length=80)
    task_id: str = Field(pattern=_ID_PATTERN, max_length=80)
    origin_run_id: str | None = Field(default=None, min_length=1, max_length=80)
    target_kind: GeometryQueryTargetKind = GeometryQueryTargetKind.GEOMETRY_RESOURCE_VERSION
    resource_version_id: str | None = Field(default=None, min_length=1, max_length=80)
    family_version_id: str | None = Field(default=None, min_length=1, max_length=80)
    target_artifact: GeometryQueryArtifactRef | None = None
    target_geometry_format: CanonicalGeometryFormat | None = None
    selection: GeometryInspectionSelection | None = None
    coordinate_space: GeometryInspectionCoordinates | None = None
    source_artifact: GeometryQueryArtifactRef
    result_artifact: GeometryQueryArtifactRef | None = None
    repair_parent_attempt_id: str | None = Field(default=None, min_length=1, max_length=80)
    request_identity: str = Field(pattern=_ID_PATTERN, max_length=128)
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    status: GeometryQueryAttemptStatus
    diagnostics: tuple[GeometryQueryDiagnostic, ...] = Field(default=(), max_length=32)
    created_at: datetime
    finished_at: datetime | None = None

    @field_validator("created_at", "finished_at")
    @classmethod
    def require_aware_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        _validate_target_shape(self)
        if self.repair_parent_attempt_id == self.attempt_id:
            raise ValueError("geometry query repair parent cannot be the attempt itself")
        if self.status is GeometryQueryAttemptStatus.PREPARED:
            if self.finished_at is not None or self.result_artifact is not None:
                raise ValueError("prepared geometry query attempt cannot contain terminal output")
            if self.diagnostics:
                raise ValueError("prepared geometry query attempt cannot contain diagnostics")
        else:
            if self.finished_at is None:
                raise ValueError("terminal geometry query attempt requires finished_at")
            if self.finished_at < self.created_at:
                raise ValueError("geometry query attempt cannot finish before it was created")
            if self.status is GeometryQueryAttemptStatus.FAILED:
                if self.result_artifact is not None:
                    raise ValueError("failed geometry query attempt cannot contain a result")
                if not self.diagnostics:
                    raise ValueError("failed geometry query attempt requires a diagnostic")
            elif self.result_artifact is None:
                raise ValueError("succeeded geometry query attempt requires a result")
        return self


class GeometryQueryPage(GeometryQueryContract):
    """One bounded page projected from an exact persisted query result."""

    attempt_alias: str = Field(min_length=1, max_length=16_384)
    target_kind: GeometryQueryTargetKind = GeometryQueryTargetKind.GEOMETRY_RESOURCE_VERSION
    resource_version_id: str | None = Field(default=None, min_length=1, max_length=80)
    family_version_id: str | None = Field(default=None, min_length=1, max_length=80)
    inspection_ref: str | None = Field(default=None, min_length=1, max_length=16_384)
    selection: GeometryInspectionSelection | None = None
    coordinate_space: GeometryInspectionCoordinates | None = None
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    geometry_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_bytes: int = Field(ge=0, le=1024 * 1024)
    total_count: int = Field(ge=0)
    returned_count: int = Field(ge=0, le=200)
    results: tuple[JsonValue, ...] = Field(max_length=200)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=16_384)

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        if (self.target_kind is GeometryQueryTargetKind.FAMILY_STANDARD_MODEL) != (
            self.family_version_id is not None
        ):
            raise ValueError("geometry query page target kind and Family version disagree")
        if (self.target_kind is GeometryQueryTargetKind.GEOMETRY_RESOURCE_VERSION) != (
            self.resource_version_id is not None
        ):
            raise ValueError("geometry query page target kind and resource version disagree")
        if self.target_kind is GeometryQueryTargetKind.GEOMETRY_INSPECTION:
            if self.inspection_ref is None or self.selection is None or self.coordinate_space is None:
                raise ValueError("inspection query page requires its exact reference, selection and coordinates")
            require_inspection_coordinate_selection(self.selection, self.coordinate_space)
        elif self.target_kind in {GeometryQueryTargetKind.GEOMETRY_RESOURCE_VERSION, GeometryQueryTargetKind.FAMILY_STANDARD_MODEL}:
            if any(value is not None for value in (self.inspection_ref, self.selection, self.coordinate_space)):
                raise ValueError("mapped resource query page cannot carry inspection fields")
        else:
            raise ValueError("geometry query pages cannot use retired preview targets")
        if self.returned_count != len(self.results):
            raise ValueError("returned_count must equal geometry query results length")
        if self.returned_count > self.total_count:
            raise ValueError("returned_count cannot exceed total_count")
        return self


def _validate_target_shape(
    value: GeometryQueryAttemptPreparation | GeometryQueryAttemptRecord,
) -> None:
    if (value.target_kind is GeometryQueryTargetKind.FAMILY_STANDARD_MODEL) != (value.family_version_id is not None):
        raise ValueError("geometry query target kind and Family version disagree")
    if value.target_kind is GeometryQueryTargetKind.FAMILY_STANDARD_MODEL:
        if any(item is not None for item in (value.resource_version_id, value.target_artifact,
                value.target_geometry_format, value.selection, value.coordinate_space)):
            raise ValueError("Family standard model query uses only its exact Family version")
        return
    if value.target_kind is GeometryQueryTargetKind.GEOMETRY_RESOURCE_VERSION:
        if value.resource_version_id is None:
            raise ValueError("geometry resource query target requires resource_version_id")
        if value.target_artifact is not None or value.target_geometry_format is not None:
            raise ValueError("geometry resource query target cannot carry preview Artifact fields")
        if value.selection is not None or value.coordinate_space is not None:
            raise ValueError("geometry resource query target cannot carry inspection choices")
        return
    if value.target_kind is GeometryQueryTargetKind.GEOMETRY_INSPECTION:
        if value.resource_version_id is not None or value.target_geometry_format is not None:
            raise ValueError("inspection query target requires a bundle rather than a resource or BREP target")
        if value.target_artifact is None:
            raise ValueError("inspection query target requires an exact ArtifactVersion")
        if value.selection is None or value.coordinate_space is None:
            raise ValueError("inspection query target requires persistent selection and coordinates")
        require_inspection_coordinate_selection(value.selection, value.coordinate_space)
        return
    if value.selection is not None or value.coordinate_space is not None:
        raise ValueError("retired preview target cannot carry inspection choices")
    if value.resource_version_id is not None:
        raise ValueError("preview geometry target cannot carry resource_version_id")
    if value.target_artifact is None:
        raise ValueError("preview geometry target requires an exact ArtifactVersion")
    if value.target_geometry_format is not CanonicalGeometryFormat.OCCT_BREP_V1:
        raise ValueError("preview geometry target requires canonical OCCT BREP")


__all__ = [
    "GeometryQueryArtifactRef",
    "GeometryQueryAttemptPreparation",
    "GeometryQueryAttemptRecord",
    "GeometryQueryAttemptStatus",
    "GeometryQueryAttemptTerminal",
    "GeometryQueryContract",
    "GeometryQueryDiagnostic",
    "GeometryQueryPage",
    "GeometryQueryTargetKind",
]
