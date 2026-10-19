"""Entry-neutral contracts for bounded exploratory DuckDB SQL queries.

The Direct adapter supplies ``direct_session_alias`` separately and converts it
to an authoritative :class:`InvocationScope`.  These models therefore contain
only business query fields and provider-safe scalar response values.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.data_query import PageInfo
from CAD2BIM_backend.domain.data_runtime import validate_arrow_type_string
from CAD2BIM_backend.domain.data_transform import (
    DuckDbSqlExecution,
    TransformInputBinding,
    TransformParameter,
)


_CURSOR_PATTERN = r"^cur\..+$"
_MAX_PARAMETER_BYTES = 32 * 1024
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

SignedInt64: TypeAlias = Annotated[
    StrictInt,
    Field(ge=_INT64_MIN, le=_INT64_MAX),
]
QuerySqlScalar: TypeAlias = None | StrictBool | SignedInt64 | StrictFloat | str


class DataQuerySqlModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class DataQuerySqlQuery(DataQuerySqlModel):
    inputs: tuple[TransformInputBinding, ...] = Field(
        min_length=1,
        max_length=16,
        description="Exact read-only Package Payload bindings exposed as DuckDB relations.",
    )
    parameters: tuple[TransformParameter, ...] = Field(
        default=(),
        max_length=32,
        description="Small typed values bound as DuckDB named parameters.",
    )
    execution: DuckDbSqlExecution = Field(
        description="Multi-statement private DuckDB script whose final statement returns rows."
    )
    page_size: Annotated[StrictInt, Field(ge=1, le=200)] = Field(
        default=100,
        description="Requested rows per page; default 100 and maximum 200.",
    )

    @model_validator(mode="after")
    def validate_bindings_and_parameters(self) -> "DataQuerySqlQuery":
        # SQL syntax is shared with Transform; exploratory Query keeps its
        # independent source budget when the Transform capacity changes.
        if len(self.execution.code.encode("utf-8")) > 65_536:
            raise ValueError("query code exceeds 65536 UTF-8 bytes")
        input_names = [item.input_name for item in self.inputs]
        if len(input_names) != len(set(input_names)):
            raise ValueError("input_name values must be unique")
        exact_inputs = [(item.package_alias, item.payload_name) for item in self.inputs]
        if len(exact_inputs) != len(set(exact_inputs)):
            raise ValueError("the same exact Package Payload cannot be bound twice")
        parameter_names = [item.name for item in self.parameters]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("parameter names must be unique")
        encoded = canonical_json([item.model_dump(mode="json") for item in self.parameters]).encode(
            "utf-8"
        )
        if len(encoded) > _MAX_PARAMETER_BYTES:
            raise ValueError("typed parameters exceed 32 KiB canonical JSON")
        return self


class DataQuerySqlRequest(DataQuerySqlModel):
    query: DataQuerySqlQuery | None = None
    cursor: str | None = Field(default=None, pattern=_CURSOR_PATTERN, max_length=16_384)

    @model_validator(mode="after")
    def require_query_xor_cursor(self) -> "DataQuerySqlRequest":
        if (self.query is None) == (self.cursor is None):
            raise ValueError("exactly one of query or cursor is required")
        return self


class QuerySqlResponseEncoding(StrEnum):
    JSON_BOOLEAN = "json_boolean"
    JSON_INTEGER = "json_integer"
    JSON_NUMBER = "json_number"
    DECIMAL_STRING = "decimal_string"
    STRING = "string"
    ISO_STRING = "iso_string"
    INTERVAL_STRING = "interval_string"
    BASE64 = "base64"
    JSON_TEXT = "json_text"
    SPECIAL_FLOAT_STRING = "special_float_string"


class QuerySqlColumn(DataQuerySqlModel):
    name: str = Field(min_length=1, max_length=255)
    logical_type: str = Field(min_length=1, max_length=65_536)
    nullable: bool
    response_encoding: QuerySqlResponseEncoding

    @field_validator("logical_type")
    @classmethod
    def validate_logical_type(cls, value: str) -> str:
        return validate_arrow_type_string(value)


class QuerySqlRowSchema(DataQuerySqlModel):
    columns: tuple[QuerySqlColumn, ...] = Field(min_length=1, max_length=512)

    @field_validator("columns")
    @classmethod
    def validate_unique_columns(
        cls,
        value: tuple[QuerySqlColumn, ...],
    ) -> tuple[QuerySqlColumn, ...]:
        names = [column.name for column in value]
        if len(names) != len(set(names)):
            raise ValueError("query result column names must be unique")
        return value


class QuerySqlRow(DataQuerySqlModel):
    values: dict[str, QuerySqlScalar] = Field(min_length=1, max_length=512)

    @field_validator("values")
    @classmethod
    def validate_names(cls, value: dict[str, QuerySqlScalar]) -> dict[str, QuerySqlScalar]:
        if any(not name for name in value):
            raise ValueError("query row column names must be non-empty")
        return value


class QuerySqlStatistics(DataQuerySqlModel):
    bound_input_payload_count: int = Field(strict=True, ge=0, le=16)
    bound_input_row_count: int = Field(strict=True, ge=0)
    bound_input_stored_bytes: int = Field(strict=True, ge=0)
    execution_ms: int = Field(strict=True, ge=0)


class DataQuerySqlResult(DataQuerySqlModel):
    row_schema: QuerySqlRowSchema
    rows: tuple[QuerySqlRow, ...] = Field(max_length=200)
    page: PageInfo
    query_statistics: QuerySqlStatistics

    @model_validator(mode="after")
    def validate_rows(self) -> "DataQuerySqlResult":
        expected = tuple(column.name for column in self.row_schema.columns)
        for row in self.rows:
            if tuple(row.values) != expected:
                raise ValueError("query row fields must exactly match row_schema order")
        if len(self.rows) != self.page.returned_count:
            raise ValueError("returned_count must equal the number of rows")
        return self


__all__ = [
    "DataQuerySqlQuery",
    "DataQuerySqlRequest",
    "DataQuerySqlResult",
    "QuerySqlColumn",
    "QuerySqlResponseEncoding",
    "QuerySqlRow",
    "QuerySqlRowSchema",
    "QuerySqlScalar",
    "QuerySqlStatistics",
]
