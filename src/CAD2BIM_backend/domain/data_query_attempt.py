"""Durable Run-scoped identity for temporary SQL and Python data queries."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from CAD2BIM_backend.domain.canonical import canonical_json


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_SAFE_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("data query timestamps must be timezone-aware")
    return value


class DataQueryAttemptContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DataQueryEngine(StrEnum):
    DUCKDB_SQL = "duckdb_sql"
    PYTHON_ARROW = "python_arrow"


class DataQueryAttemptStatus(StrEnum):
    PREPARED = "PREPARED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class DataQueryArtifactRef(DataQueryAttemptContract):
    artifact_id: str = Field(min_length=1, max_length=80)
    version_id: str = Field(min_length=1, max_length=80)
    sha256: str = Field(pattern=_SHA256_PATTERN)


class DataQueryExactInputRef(DataQueryAttemptContract):
    ordinal: int = Field(ge=1, le=16)
    input_name: str = Field(pattern=_SAFE_NAME_PATTERN, max_length=63)
    package_id: str = Field(min_length=1, max_length=80)
    payload_member_id: str = Field(min_length=1, max_length=80)
    schema_member_id: str | None = Field(default=None, min_length=1, max_length=80)


class DataQueryDiagnostic(DataQueryAttemptContract):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=2000)
    phase: Literal["read", "preflight", "code_validation", "execution", "output_validation"]
    retryable: bool = False

    @field_validator("message")
    @classmethod
    def trimmed(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("data query diagnostic message must be trimmed")
        return value


class DataQueryResultSummary(DataQueryAttemptContract):
    exact_input_digest: str = Field(pattern=_SHA256_PATTERN)
    query_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    row_schema_digest: str = Field(pattern=_SHA256_PATTERN)
    snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    row_count: int = Field(ge=0, le=2_000_000)
    snapshot_size_bytes: int = Field(ge=1, le=128 * 1024 * 1024)


class DataQueryAttemptPreparation(DataQueryAttemptContract):
    attempt_id: str = Field(pattern=_ID_PATTERN, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    engine: DataQueryEngine
    source_artifact: DataQueryArtifactRef
    inputs: tuple[DataQueryExactInputRef, ...] = Field(min_length=1, max_length=16)
    parameters: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=32)
    inline_tables: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=8)
    page_size: int = Field(ge=1, le=200)
    repair_parent_attempt_id: str | None = Field(default=None, min_length=1, max_length=80)
    request_identity: str = Field(pattern=_ID_PATTERN, max_length=128)
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def aware_created_at(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.repair_parent_attempt_id == self.attempt_id:
            raise ValueError("data query repair parent cannot be the attempt itself")
        ordinals = [item.ordinal for item in self.inputs]
        if ordinals != list(range(1, len(self.inputs) + 1)):
            raise ValueError("data query input ordinals must be contiguous from one")
        if len({item.input_name for item in self.inputs}) != len(self.inputs):
            raise ValueError("data query input names must be unique")
        exact = [(item.package_id, item.payload_member_id) for item in self.inputs]
        if len(set(exact)) != len(exact):
            raise ValueError("the same exact Payload cannot be bound twice")
        if self.engine is DataQueryEngine.DUCKDB_SQL and self.inline_tables:
            raise ValueError("DuckDB SQL attempts do not accept inline tables")
        for value, label, budget in (
            (self.parameters, "parameters", 32 * 1024),
            (self.inline_tables, "inline tables", 1024 * 1024),
        ):
            if len(canonical_json(value).encode("utf-8")) > budget:
                raise ValueError(f"data query {label} exceed the persisted budget")
        return self


class DataQueryAttemptTerminal(DataQueryAttemptContract):
    status: DataQueryAttemptStatus
    diagnostic: DataQueryDiagnostic | None = None
    result_summary: DataQueryResultSummary | None = None
    finished_at: datetime

    @field_validator("finished_at")
    @classmethod
    def aware_finished_at(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_terminal(self) -> Self:
        if self.status is DataQueryAttemptStatus.PREPARED:
            raise ValueError("data query completion must be terminal")
        if self.status is DataQueryAttemptStatus.FAILED:
            if self.diagnostic is None or self.result_summary is not None:
                raise ValueError("failed data query attempt requires only a diagnostic")
        elif self.diagnostic is not None or self.result_summary is None:
            raise ValueError("succeeded data query attempt requires only a result summary")
        return self


class DataQueryAttemptRecord(DataQueryAttemptContract):
    attempt_id: str = Field(pattern=_ID_PATTERN, max_length=80)
    sequence: int = Field(ge=1)
    owner_id: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(pattern=_ID_PATTERN, max_length=80)
    task_id: str = Field(pattern=_ID_PATTERN, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    engine: DataQueryEngine
    source_artifact: DataQueryArtifactRef
    inputs: tuple[DataQueryExactInputRef, ...] = Field(min_length=1, max_length=16)
    parameters: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=32)
    inline_tables: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=8)
    page_size: int = Field(ge=1, le=200)
    repair_parent_attempt_id: str | None = Field(default=None, min_length=1, max_length=80)
    request_identity: str = Field(pattern=_ID_PATTERN, max_length=128)
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    status: DataQueryAttemptStatus
    diagnostic: DataQueryDiagnostic | None = None
    result_summary: DataQueryResultSummary | None = None
    created_at: datetime
    finished_at: datetime | None = None

    @field_validator("created_at", "finished_at")
    @classmethod
    def aware_times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        DataQueryAttemptPreparation(
            attempt_id=self.attempt_id,
            run_id=self.run_id,
            engine=self.engine,
            source_artifact=self.source_artifact,
            inputs=self.inputs,
            parameters=self.parameters,
            inline_tables=self.inline_tables,
            page_size=self.page_size,
            repair_parent_attempt_id=self.repair_parent_attempt_id,
            request_identity=self.request_identity,
            request_digest=self.request_digest,
            created_at=self.created_at,
        )
        if self.status is DataQueryAttemptStatus.PREPARED:
            if self.finished_at is not None or self.diagnostic is not None or self.result_summary is not None:
                raise ValueError("prepared data query attempt cannot contain terminal facts")
        else:
            if self.finished_at is None or self.finished_at < self.created_at:
                raise ValueError("terminal data query attempt requires an ordered finished_at")
            DataQueryAttemptTerminal(
                status=self.status,
                diagnostic=self.diagnostic,
                result_summary=self.result_summary,
                finished_at=self.finished_at,
            )
        return self


class DataQueryPrepared(DataQueryAttemptContract):
    attempt: DataQueryAttemptRecord
    created: bool


__all__ = [
    "DataQueryArtifactRef",
    "DataQueryAttemptPreparation",
    "DataQueryAttemptRecord",
    "DataQueryAttemptStatus",
    "DataQueryAttemptTerminal",
    "DataQueryDiagnostic",
    "DataQueryEngine",
    "DataQueryExactInputRef",
    "DataQueryPrepared",
    "DataQueryResultSummary",
]
