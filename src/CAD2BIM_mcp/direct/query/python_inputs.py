"""Provider-safe inputs for the Direct Python read-only query tool."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import Field, TypeAdapter, field_validator, model_validator

from CAD2BIM_backend.domain.data_query_python import DataQueryPythonQuery
from CAD2BIM_mcp.direct.transform.inputs import (
    DirectDataInput,
    InlineTable,
    TransformInputBinding,
    TransformParameter,
)
from CAD2BIM_mcp.tool_docs import data_query as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


_DIRECT_ALIAS_PATTERN = r"^direct_[0-9]{3,}$"
_CURSOR_PATTERN = r"^cur\..+$"
_MAX_CODE_BYTES = 65_536


class QueryPythonQuery(DirectDataInput):
    __doc__ = _tool_docs.QUERY_PYTHON_QUERY_DESCRIPTION

    inputs: tuple[TransformInputBinding, ...] = Field(
        min_length=1,
        max_length=16,
        description=(_tool_docs.QUERY_PYTHON_QUERY_INPUTS_DESCRIPTION),
    )
    inline_tables: tuple[InlineTable, ...] = Field(
        default=(),
        max_length=8,
        description=(_tool_docs.QUERY_PYTHON_QUERY_INLINE_TABLES_DESCRIPTION),
    )
    parameters: tuple[TransformParameter, ...] = Field(
        default=(),
        max_length=32,
        description=(_tool_docs.QUERY_PYTHON_QUERY_PARAMETERS_DESCRIPTION),
    )
    code: str = Field(
        min_length=1,
        max_length=_MAX_CODE_BYTES,
        description=(_tool_docs.QUERY_PYTHON_QUERY_CODE_DESCRIPTION),
    )
    page_size: int = Field(
        default=100,
        strict=True,
        ge=1,
        le=200,
        description=_tool_docs.QUERY_PYTHON_QUERY_PAGE_SIZE_DESCRIPTION,
    )

    @field_validator("code")
    @classmethod
    def validate_code_utf8_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > _MAX_CODE_BYTES:
            raise ValueError("code exceeds 65,536 UTF-8 bytes")
        return value

    @model_validator(mode="after")
    def validate_domain_contract(self) -> "QueryPythonQuery":
        # Reuse the entry-neutral validator so the Provider and backend cannot drift.
        _DOMAIN_QUERY.validate_python(self.model_dump(mode="python"))
        return self


class QueryPythonFirstPageInput(DirectDataInput):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )
    query: QueryPythonQuery = Field(
        description=_tool_docs.QUERY_PYTHON_FIRST_PAGE_INPUT_QUERY_DESCRIPTION
    )
    mode: Literal["fresh"] = Field(
        default="fresh",
        description=_tool_docs.QUERY_FRESH_MODE_DESCRIPTION,
    )


class QueryPythonSourceEdit(DirectDataInput):
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


class QueryPythonRepairInput(DirectDataInput):
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
    edits: tuple[QueryPythonSourceEdit, ...] = Field(
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


class QueryPythonContinuationInput(DirectDataInput):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )
    cursor: str = Field(
        pattern=_CURSOR_PATTERN,
        max_length=16_384,
        description=(_tool_docs.QUERY_PYTHON_CONTINUATION_INPUT_CURSOR_DESCRIPTION),
    )
    mode: Literal["cursor"] = Field(
        default="cursor",
        description=_tool_docs.QUERY_CURSOR_MODE_DESCRIPTION,
    )


DirectDataQueryPythonInput: TypeAlias = (
    QueryPythonFirstPageInput | QueryPythonRepairInput | QueryPythonContinuationInput
)

_DIRECT_QUERY_INPUT = TypeAdapter(DirectDataQueryPythonInput)
_DOMAIN_QUERY = TypeAdapter(DataQueryPythonQuery)


def validate_direct_data_query_python_input(
    value: object,
) -> QueryPythonFirstPageInput | QueryPythonRepairInput | QueryPythonContinuationInput:
    """Validate exactly one complete Direct first-page or continuation branch."""

    return _DIRECT_QUERY_INPUT.validate_python(value)


def to_domain_data_query_python_query(value: QueryPythonQuery) -> DataQueryPythonQuery:
    """Remove Direct scope and return the entry-neutral query contract."""

    return _DOMAIN_QUERY.validate_python(value.model_dump(mode="python"))


__all__ = [
    "DirectDataQueryPythonInput",
    "QueryPythonContinuationInput",
    "QueryPythonFirstPageInput",
    "QueryPythonQuery",
    "QueryPythonRepairInput",
    "QueryPythonSourceEdit",
    "to_domain_data_query_python_query",
    "validate_direct_data_query_python_input",
]
