"""Provider-visible complete branches for Direct ``data_read_json`` inputs."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import Field, TypeAdapter, field_validator

from CAD2BIM_backend.domain.data_json import (
    JsonChildrenQuery,
    JsonReadQuery,
    JsonRecordsQuery,
    JsonValueQuery,
)
from CAD2BIM_mcp.direct.transform.common_inputs import DirectM2aInput
from CAD2BIM_mcp.tool_docs import data_query as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


_DIRECT_ALIAS_PATTERN = r"^direct_[0-9]{3,}$"
_DATA_ALIAS_PATTERN = r"^data_[0-9]{3,}$"
_PAYLOAD_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,119}$"
_CURSOR_PATTERN = r"^cur\..+$"


class _JsonPayloadToolQuery(DirectM2aInput):
    package_alias: str = Field(
        pattern=_DATA_ALIAS_PATTERN,
        max_length=32,
        description=(_tool_docs.JSON_PAYLOAD_TOOL_QUERY_PACKAGE_ALIAS_DESCRIPTION),
    )
    payload_name: str = Field(
        pattern=_PAYLOAD_NAME_PATTERN,
        max_length=120,
        description=_tool_docs.JSON_PAYLOAD_TOOL_QUERY_PAYLOAD_NAME_DESCRIPTION,
    )


class JsonValueToolQuery(_JsonPayloadToolQuery):
    mode: Literal["value"] = Field(description=_tool_docs.JSON_VALUE_TOOL_QUERY_MODE_DESCRIPTION)
    pointer: str = Field(
        max_length=4_096,
        description=(_tool_docs.JSON_VALUE_TOOL_QUERY_POINTER_DESCRIPTION),
    )

    @field_validator("pointer")
    @classmethod
    def validate_pointer_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 4_096:
            raise ValueError("pointer exceeds 4,096 UTF-8 bytes")
        return value


class JsonChildrenToolQuery(_JsonPayloadToolQuery):
    mode: Literal["children"] = Field(
        description=_tool_docs.JSON_CHILDREN_TOOL_QUERY_MODE_DESCRIPTION
    )
    pointer: str = Field(
        max_length=4_096,
        description=(_tool_docs.JSON_CHILDREN_TOOL_QUERY_POINTER_DESCRIPTION),
    )
    page_size: int = Field(
        default=50,
        strict=True,
        ge=1,
        le=100,
        description=_tool_docs.JSON_CHILDREN_TOOL_QUERY_PAGE_SIZE_DESCRIPTION,
    )

    @field_validator("pointer")
    @classmethod
    def validate_pointer_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 4_096:
            raise ValueError("pointer exceeds 4,096 UTF-8 bytes")
        return value


class JsonRecordsToolQuery(_JsonPayloadToolQuery):
    mode: Literal["records"] = Field(
        description=_tool_docs.JSON_RECORDS_TOOL_QUERY_MODE_DESCRIPTION
    )
    page_size: int = Field(
        default=100,
        strict=True,
        ge=1,
        le=200,
        description=_tool_docs.JSON_RECORDS_TOOL_QUERY_PAGE_SIZE_DESCRIPTION,
    )


class _DirectJsonFirstPage(DirectM2aInput):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )


class DirectJsonValueInput(_DirectJsonFirstPage):
    query: JsonValueToolQuery = Field(
        description=_tool_docs.DIRECT_JSON_VALUE_INPUT_QUERY_DESCRIPTION
    )


class DirectJsonChildrenInput(_DirectJsonFirstPage):
    query: JsonChildrenToolQuery = Field(
        description=_tool_docs.DIRECT_JSON_CHILDREN_INPUT_QUERY_DESCRIPTION
    )


class DirectJsonRecordsInput(_DirectJsonFirstPage):
    query: JsonRecordsToolQuery = Field(
        description=_tool_docs.DIRECT_JSON_RECORDS_INPUT_QUERY_DESCRIPTION
    )


class DirectJsonContinuationInput(DirectM2aInput):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )
    cursor: str = Field(
        pattern=_CURSOR_PATTERN,
        max_length=16_384,
        description=(_tool_docs.DIRECT_JSON_CONTINUATION_INPUT_CURSOR_DESCRIPTION),
    )


DirectDataReadJsonInput: TypeAlias = (
    DirectJsonValueInput
    | DirectJsonChildrenInput
    | DirectJsonRecordsInput
    | DirectJsonContinuationInput
)

_DIRECT_DATA_READ_JSON_INPUT = TypeAdapter(DirectDataReadJsonInput)
_DOMAIN_JSON_QUERY = TypeAdapter(JsonReadQuery)


def validate_direct_data_read_json_input(
    value: object,
) -> (
    DirectJsonValueInput
    | DirectJsonChildrenInput
    | DirectJsonRecordsInput
    | DirectJsonContinuationInput
):
    """Validate exactly one complete first-page or continuation branch."""

    return _DIRECT_DATA_READ_JSON_INPUT.validate_python(value)


def to_domain_json_query(
    value: JsonValueToolQuery | JsonChildrenToolQuery | JsonRecordsToolQuery,
) -> JsonValueQuery | JsonChildrenQuery | JsonRecordsQuery:
    """Drop Direct authority and deterministically construct the domain query."""

    return _DOMAIN_JSON_QUERY.validate_python(value.model_dump(mode="python"))


__all__ = [
    "DirectDataReadJsonInput",
    "DirectJsonChildrenInput",
    "DirectJsonContinuationInput",
    "DirectJsonRecordsInput",
    "DirectJsonValueInput",
    "JsonChildrenToolQuery",
    "JsonRecordsToolQuery",
    "JsonValueToolQuery",
    "to_domain_json_query",
    "validate_direct_data_read_json_input",
]
