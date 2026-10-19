"""Durable contracts for source-first, read-only IFC query attempts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from CAD2BIM_backend.domain.canonical import canonical_json


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_SAFE_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
_PACKAGE_ALIAS_PATTERN = r"^data_[0-9]{3,}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_DIAGNOSTIC_CODE_PATTERN = r"^[A-Z][A-Z0-9_]{0,63}$"


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("IFC query timestamps must be timezone-aware")
    return value


class IfcQueryContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IfcQueryAttemptStatus(StrEnum):
    PREPARED = "PREPARED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class IfcQueryInputBinding(IfcQueryContract):
    """Entry-neutral request for one visible exact Package Payload."""

    input_name: str = Field(pattern=_SAFE_NAME_PATTERN, max_length=63)
    package_alias: str = Field(pattern=_PACKAGE_ALIAS_PATTERN, max_length=32)
    payload_name: str = Field(min_length=1, max_length=120)

    @field_validator("payload_name")
    @classmethod
    def validate_payload_name(cls, value: str) -> str:
        if not value.strip() or value != value.strip() or "\x00" in value:
            raise ValueError("IFC query payload_name must be trimmed and non-blank")
        return value


class IfcQueryArtifactRef(IfcQueryContract):
    """Exact immutable ArtifactVersion used by or produced by an attempt."""

    artifact_id: str = Field(min_length=1, max_length=80)
    version_id: str = Field(min_length=1, max_length=80)
    sha256: str = Field(pattern=_SHA256_PATTERN)


class IfcQueryExactInputRef(IfcQueryContract):
    """Server-only exact Package member binding persisted with an attempt."""

    ordinal: int = Field(ge=1, le=16)
    input_name: str = Field(pattern=_SAFE_NAME_PATTERN, max_length=63)
    package_id: str = Field(min_length=1, max_length=80)
    payload_member_id: str = Field(min_length=1, max_length=80)
    schema_member_id: str | None = Field(default=None, min_length=1, max_length=80)


class IfcQueryBudgetSummary(IfcQueryContract):
    """Server-owned execution budget retained for exact replay diagnostics."""

    max_result_bytes: int = Field(ge=1, le=1024 * 1024)


class IfcQueryDiagnostic(IfcQueryContract):
    """Small persisted diagnostic safe to expose at an entry boundary."""

    code: str = Field(pattern=_DIAGNOSTIC_CODE_PATTERN)
    message: str = Field(min_length=1, max_length=2000)
    phase: Literal["read", "preflight", "execution", "output_validation"]
    execution_started: bool
    retryable: bool = False
    detail: dict[str, JsonValue] | None = None

    @field_validator("message")
    @classmethod
    def canonical_message(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("IFC query diagnostic message must be trimmed")
        return value

    @field_validator("detail")
    @classmethod
    def bounded_detail(
        cls,
        value: dict[str, JsonValue] | None,
    ) -> dict[str, JsonValue] | None:
        if value is not None and len(canonical_json(value).encode("utf-8")) > 2000:
            raise ValueError("IFC query diagnostic detail exceeds 2000 UTF-8 bytes")
        return value


class IfcQueryAttemptPreparation(IfcQueryContract):
    """Server-authored immutable identity and exact inputs for fresh or repair."""

    attempt_id: str = Field(pattern=_ID_PATTERN, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    ifc_model_id: str = Field(min_length=1, max_length=80)
    ifc_revision_id: str = Field(min_length=1, max_length=80)
    source_artifact: IfcQueryArtifactRef
    inputs: tuple[IfcQueryExactInputRef, ...] = Field(default=(), max_length=16)
    repair_parent_attempt_id: str | None = Field(default=None, min_length=1, max_length=80)
    request_identity: str = Field(pattern=_ID_PATTERN, max_length=128)
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    budget: IfcQueryBudgetSummary
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_aware_created_at(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def validate_exact_inputs(self) -> Self:
        if self.repair_parent_attempt_id == self.attempt_id:
            raise ValueError("IFC query repair parent cannot be the attempt itself")
        ordinals = [item.ordinal for item in self.inputs]
        if ordinals != list(range(1, len(self.inputs) + 1)):
            raise ValueError("IFC query input ordinals must be contiguous from one")
        if len({item.input_name for item in self.inputs}) != len(self.inputs):
            raise ValueError("IFC query input names must be unique")
        exact_members = [(item.package_id, item.payload_member_id) for item in self.inputs]
        if len(set(exact_members)) != len(exact_members):
            raise ValueError("the same exact Package Payload cannot be bound twice")
        return self


class IfcQueryAttemptTerminal(IfcQueryContract):
    """The only allowed terminal transition for one prepared attempt."""

    status: IfcQueryAttemptStatus
    result_artifact: IfcQueryArtifactRef | None = None
    diagnostics: tuple[IfcQueryDiagnostic, ...] = Field(default=(), max_length=32)
    finished_at: datetime

    @field_validator("finished_at")
    @classmethod
    def require_aware_finished_at(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def require_terminal_shape(self) -> Self:
        if self.status is IfcQueryAttemptStatus.PREPARED:
            raise ValueError("IFC query completion must be terminal")
        if self.status is IfcQueryAttemptStatus.FAILED:
            if self.result_artifact is not None:
                raise ValueError("failed IFC query attempt cannot publish a result artifact")
            if not self.diagnostics:
                raise ValueError("failed IFC query attempt requires a diagnostic")
        elif self.result_artifact is None:
            raise ValueError("succeeded IFC query attempt requires a result artifact")
        return self


class IfcQueryAttemptRecord(IfcQueryContract):
    """Run-visible durable record for one exact IFC investigation."""

    attempt_id: str = Field(pattern=_ID_PATTERN, max_length=80)
    owner_id: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(pattern=_ID_PATTERN, max_length=80)
    task_id: str = Field(pattern=_ID_PATTERN, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    ifc_model_id: str = Field(min_length=1, max_length=80)
    ifc_revision_id: str = Field(min_length=1, max_length=80)
    source_artifact: IfcQueryArtifactRef
    result_artifact: IfcQueryArtifactRef | None = None
    inputs: tuple[IfcQueryExactInputRef, ...] = Field(default=(), max_length=16)
    repair_parent_attempt_id: str | None = Field(default=None, min_length=1, max_length=80)
    request_identity: str = Field(pattern=_ID_PATTERN, max_length=128)
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    budget: IfcQueryBudgetSummary
    status: IfcQueryAttemptStatus
    diagnostics: tuple[IfcQueryDiagnostic, ...] = Field(default=(), max_length=32)
    created_at: datetime
    finished_at: datetime | None = None

    @field_validator("created_at", "finished_at")
    @classmethod
    def require_aware_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        IfcQueryAttemptPreparation(
            attempt_id=self.attempt_id,
            run_id=self.run_id,
            ifc_model_id=self.ifc_model_id,
            ifc_revision_id=self.ifc_revision_id,
            source_artifact=self.source_artifact,
            inputs=self.inputs,
            repair_parent_attempt_id=self.repair_parent_attempt_id,
            request_identity=self.request_identity,
            request_digest=self.request_digest,
            budget=self.budget,
            created_at=self.created_at,
        )
        if self.status is IfcQueryAttemptStatus.PREPARED:
            if self.finished_at is not None or self.result_artifact is not None:
                raise ValueError("prepared IFC query attempt cannot contain terminal output")
            if self.diagnostics:
                raise ValueError("prepared IFC query attempt cannot contain diagnostics")
        else:
            if self.finished_at is None:
                raise ValueError("terminal IFC query attempt requires finished_at")
            if self.finished_at < self.created_at:
                raise ValueError("IFC query attempt cannot finish before it was created")
            IfcQueryAttemptTerminal(
                status=self.status,
                result_artifact=self.result_artifact,
                diagnostics=self.diagnostics,
                finished_at=self.finished_at,
            )
        return self


class IfcQueryPrepared(IfcQueryContract):
    """Preparation result distinguishing a new insert from an exact replay."""

    attempt: IfcQueryAttemptRecord
    created: bool


class IfcQueryPage(IfcQueryContract):
    """One bounded provider-safe page projected from an exact persisted result."""

    attempt_alias: str = Field(min_length=1, max_length=16_384)
    parent_attempt_alias: str | None = Field(default=None, min_length=1, max_length=16_384)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_bytes: int = Field(ge=0, le=1024 * 1024)
    total_count: int = Field(ge=0)
    returned_count: int = Field(ge=0, le=200)
    results: tuple[JsonValue, ...] = Field(max_length=200)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=16_384)

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        if self.returned_count != len(self.results):
            raise ValueError("returned_count must equal IFC query results length")
        if self.returned_count > self.total_count:
            raise ValueError("returned_count cannot exceed total_count")
        return self


__all__ = [
    "IfcQueryArtifactRef",
    "IfcQueryAttemptPreparation",
    "IfcQueryAttemptRecord",
    "IfcQueryAttemptStatus",
    "IfcQueryAttemptTerminal",
    "IfcQueryBudgetSummary",
    "IfcQueryContract",
    "IfcQueryDiagnostic",
    "IfcQueryExactInputRef",
    "IfcQueryInputBinding",
    "IfcQueryPage",
    "IfcQueryPrepared",
]
