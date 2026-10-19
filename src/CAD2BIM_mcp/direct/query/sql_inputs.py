"""Provider-visible inputs for the Direct SQL query tool."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import Field, TypeAdapter, model_validator

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.data_query_sql import DataQuerySqlQuery
from CAD2BIM_mcp.direct.transform.common_inputs import (
    DirectM2aInput,
    DuckDbSqlExecution,
    TransformInputBinding,
    TransformParameter,
)
from CAD2BIM_mcp.tool_docs import data_query as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


_DIRECT_ALIAS_PATTERN = r"^direct_[0-9]{3,}$"
_CURSOR_PATTERN = r"^cur\..+$"


class QuerySqlExecution(DuckDbSqlExecution):
    engine: Literal["duckdb_sql"] = Field(
        description=_tool_docs.QUERY_SQL_EXECUTION_ENGINE_DESCRIPTION,
    )
    code: str = Field(
        min_length=1,
        max_length=65_536,
        description=(_tool_docs.QUERY_SQL_EXECUTION_CODE_DESCRIPTION),
    )


class QuerySqlQuery(DirectM2aInput):
    inputs: tuple[TransformInputBinding, ...] = Field(
        min_length=1,
        max_length=16,
        description=(_tool_docs.QUERY_PYTHON_QUERY_INPUTS_DESCRIPTION),
    )
    parameters: tuple[TransformParameter, ...] = Field(
        default=(),
        max_length=32,
        description=(_tool_docs.QUERY_SQL_QUERY_PARAMETERS_DESCRIPTION),
    )
    execution: QuerySqlExecution = Field(
        description=(_tool_docs.QUERY_SQL_QUERY_EXECUTION_DESCRIPTION)
    )
    page_size: int = Field(
        default=100,
        strict=True,
        ge=1,
        le=200,
        description=_tool_docs.QUERY_PYTHON_QUERY_PAGE_SIZE_DESCRIPTION,
    )

    @model_validator(mode="after")
    def validate_bindings_and_parameters(self) -> QuerySqlQuery:
        input_names = [item.input_name for item in self.inputs]
        if len(set(input_names)) != len(input_names):
            raise ValueError("input_name values must be unique")
        bindings = [(item.package_alias, item.payload_name) for item in self.inputs]
        if len(set(bindings)) != len(bindings):
            raise ValueError("the same exact Package Payload must not be bound twice")
        parameter_names = [item.name for item in self.parameters]
        if len(set(parameter_names)) != len(parameter_names):
            raise ValueError("parameter names must be unique")
        encoded = canonical_json([item.model_dump(mode="json") for item in self.parameters]).encode(
            "utf-8"
        )
        if len(encoded) > 32 * 1024:
            raise ValueError("typed parameters exceed 32 KiB canonical JSON")
        return self


class QuerySqlFirstPageInput(DirectM2aInput):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )
    query: QuerySqlQuery = Field(
        description=_tool_docs.QUERY_SQL_FIRST_PAGE_INPUT_QUERY_DESCRIPTION
    )
    mode: Literal["fresh"] = Field(
        default="fresh",
        description=_tool_docs.QUERY_FRESH_MODE_DESCRIPTION,
    )


class QuerySqlSourceEdit(DirectM2aInput):
    old_text: str = Field(
        min_length=1,
        max_length=16_384,
        description=_tool_docs.QUERY_REPAIR_OLD_TEXT_DESCRIPTION,
    )
    new_text: str = Field(
        default="",
        max_length=16_384,
        description=_tool_docs.QUERY_REPAIR_NEW_TEXT_DESCRIPTION,
    )


class QuerySqlRepairInput(DirectM2aInput):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )
    mode: Literal["repair"] = Field(
        description=_tool_docs.QUERY_REPAIR_MODE_DESCRIPTION,
    )
    parent_attempt_alias: str = Field(
        pattern=r"^cur\..+$",
        max_length=16_384,
        description=_tool_docs.QUERY_REPAIR_PARENT_ATTEMPT_ALIAS_DESCRIPTION,
    )
    edits: tuple[QuerySqlSourceEdit, ...] = Field(
        min_length=1,
        max_length=16,
        description=_tool_docs.QUERY_REPAIR_EDITS_DESCRIPTION,
    )
    page_size: int = Field(
        default=100,
        strict=True,
        ge=1,
        le=200,
        description=_tool_docs.QUERY_REPAIR_PAGE_SIZE_DESCRIPTION,
    )


class QuerySqlContinuationInput(DirectM2aInput):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )
    cursor: str = Field(
        pattern=_CURSOR_PATTERN,
        max_length=16_384,
        description=(_tool_docs.QUERY_SQL_CONTINUATION_INPUT_CURSOR_DESCRIPTION),
    )
    mode: Literal["cursor"] = Field(
        default="cursor",
        description=_tool_docs.QUERY_CURSOR_MODE_DESCRIPTION,
    )


DirectDataQuerySqlInput: TypeAlias = (
    QuerySqlFirstPageInput | QuerySqlRepairInput | QuerySqlContinuationInput
)

_DIRECT_QUERY_SQL_INPUT = TypeAdapter(DirectDataQuerySqlInput)
_DOMAIN_QUERY_SQL_QUERY = TypeAdapter(DataQuerySqlQuery)


def validate_direct_data_query_sql_input(
    value: object,
) -> QuerySqlFirstPageInput | QuerySqlRepairInput | QuerySqlContinuationInput:
    """Validate exactly one complete Direct first-page or continuation branch."""

    return _DIRECT_QUERY_SQL_INPUT.validate_python(value)


def to_domain_data_query_sql_query(value: QuerySqlQuery) -> DataQuerySqlQuery:
    """Convert the Provider query without carrying Direct scope into the core."""

    return _DOMAIN_QUERY_SQL_QUERY.validate_python(value.model_dump(mode="python"))


__all__ = [
    "DirectDataQuerySqlInput",
    "QuerySqlContinuationInput",
    "QuerySqlExecution",
    "QuerySqlFirstPageInput",
    "QuerySqlQuery",
    "QuerySqlRepairInput",
    "QuerySqlSourceEdit",
    "to_domain_data_query_sql_query",
    "validate_direct_data_query_sql_input",
]
