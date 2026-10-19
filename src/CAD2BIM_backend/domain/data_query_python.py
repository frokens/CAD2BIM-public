"""Entry-neutral contracts for bounded exploratory Python/Arrow queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.data_query import PageInfo
from CAD2BIM_backend.domain.data_query_sql import QuerySqlRow, QuerySqlRowSchema
from CAD2BIM_backend.domain.data_transform import (
    DataTransformWorkerParameter,
    InlineTable,
    TransformInputBinding,
    TransformParameter,
)


_CURSOR_PATTERN = r"^cur\..+$"
_MAX_CODE_BYTES = 65_536
_MAX_PARAMETER_BYTES = 32 * 1024
_MAX_INLINE_TABLE_BYTES = 1_048_576


class DataQueryPythonModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _validate_query_source(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("code must not be blank or contain NUL")
    if len(value.encode("utf-8")) > _MAX_CODE_BYTES:
        raise ValueError("code exceeds 65536 UTF-8 bytes")
    return value


class DataQueryPythonQuery(DataQueryPythonModel):
    inputs: tuple[TransformInputBinding, ...] = Field(min_length=1, max_length=16)
    inline_tables: tuple[InlineTable, ...] = Field(default=(), max_length=8)
    parameters: tuple[TransformParameter, ...] = Field(default=(), max_length=32)
    code: str = Field(min_length=1, max_length=_MAX_CODE_BYTES)
    page_size: Annotated[StrictInt, Field(ge=1, le=200)] = 100

    _validate_code = field_validator("code")(_validate_query_source)

    @model_validator(mode="after")
    def validate_names_and_budgets(self) -> "DataQueryPythonQuery":
        package_names = [item.input_name for item in self.inputs]
        inline_names = [item.input_name for item in self.inline_tables]
        if len(package_names) != len(set(package_names)):
            raise ValueError("Package input_name values must be unique")
        if len(inline_names) != len(set(inline_names)):
            raise ValueError("inline table input_name values must be unique")
        if set(package_names).intersection(inline_names):
            raise ValueError("Package and inline input_name values must not collide")
        exact_inputs = [(item.package_alias, item.payload_name) for item in self.inputs]
        if len(exact_inputs) != len(set(exact_inputs)):
            raise ValueError("the same exact Package Payload cannot be bound twice")
        parameter_names = [item.name for item in self.parameters]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("parameter names must be unique")
        parameter_bytes = canonical_json(
            [item.model_dump(mode="json") for item in self.parameters]
        ).encode("utf-8")
        if len(parameter_bytes) > _MAX_PARAMETER_BYTES:
            raise ValueError("typed parameters exceed 32 KiB canonical JSON")
        inline_bytes = canonical_json(
            [item.model_dump(mode="json") for item in self.inline_tables]
        ).encode("utf-8")
        if len(inline_bytes) > _MAX_INLINE_TABLE_BYTES:
            raise ValueError("inline tables exceed 1 MiB canonical JSON")
        return self


class DataQueryPythonRequest(DataQueryPythonModel):
    query: DataQueryPythonQuery | None = None
    cursor: str | None = Field(default=None, pattern=_CURSOR_PATTERN, max_length=16_384)

    @model_validator(mode="after")
    def require_query_xor_cursor(self) -> "DataQueryPythonRequest":
        if (self.query is None) == (self.cursor is None):
            raise ValueError("exactly one of query or cursor is required")
        return self


class QueryPythonStatistics(DataQueryPythonModel):
    bound_input_payload_count: int = Field(strict=True, ge=1, le=16)
    bound_input_row_count: int = Field(strict=True, ge=0)
    bound_input_stored_bytes: int = Field(strict=True, ge=0)
    bound_inline_table_count: int = Field(strict=True, ge=0, le=8)
    bound_inline_row_count: int = Field(strict=True, ge=0, le=4_000)
    execution_ms: int = Field(strict=True, ge=0)


class DataQueryPythonResult(DataQueryPythonModel):
    row_schema: QuerySqlRowSchema
    rows: tuple[QuerySqlRow, ...] = Field(max_length=200)
    page: PageInfo
    query_statistics: QueryPythonStatistics

    @model_validator(mode="after")
    def validate_rows(self) -> "DataQueryPythonResult":
        expected = tuple(column.name for column in self.row_schema.columns)
        if any(tuple(row.values) != expected for row in self.rows):
            raise ValueError("query row fields must exactly match row_schema order")
        if len(self.rows) != self.page.returned_count:
            raise ValueError("returned_count must equal the number of rows")
        return self


@dataclass(frozen=True, slots=True)
class PythonArrowQueryWorkerRequest:
    code: str
    parameters: tuple[DataTransformWorkerParameter, ...] = ()

    def __post_init__(self) -> None:
        _validate_query_source(self.code)
        names = [item.name for item in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("Python query worker parameter names must be unique")


__all__ = [
    "DataQueryPythonQuery",
    "DataQueryPythonRequest",
    "DataQueryPythonResult",
    "PythonArrowQueryWorkerRequest",
    "QueryPythonStatistics",
]
