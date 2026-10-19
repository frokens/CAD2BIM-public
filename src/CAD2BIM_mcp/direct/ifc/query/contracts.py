"""Closed Provider contracts for read-only investigation of CURRENT IFC."""

from __future__ import annotations

import json
from typing import Literal, TypeAlias

from pydantic import Field, TypeAdapter, field_validator, model_validator

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_mcp.direct.contract_base import DirectContract
from CAD2BIM_mcp.tool_docs import ifc_query as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


_DIRECT_ALIAS_PATTERN = r"^direct_[0-9]{3,}$"
_ATTEMPT_ALIAS_PATTERN = r"^cur\..+$"
_CURSOR_PATTERN = r"^cur\..+$"
_DATA_ALIAS_PATTERN = r"^data_[0-9]{3,}$"
_SAFE_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_SOURCE_BYTES = 65_536
_MAX_EDIT_BYTES = 16_384
_MAX_SEARCH_BYTES = 4_096


def _utf8_bounded(value: str, *, maximum: int, label: str) -> str:
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds {maximum} UTF-8 bytes")
    return value


def _non_blank_utf8(value: str, *, maximum: int, label: str) -> str:
    if not value.strip():
        raise ValueError(f"{label} must not be blank")
    return _utf8_bounded(value, maximum=maximum, label=label)


class _DirectIfcQueryInput(DirectContract):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )


class IfcQueryExactInput(DirectContract):
    input_name: str = Field(
        pattern=_SAFE_NAME_PATTERN,
        max_length=63,
        description=(_tool_docs.IFC_QUERY_EXACT_INPUT_INPUT_NAME_DESCRIPTION),
    )
    package_alias: str = Field(
        pattern=_DATA_ALIAS_PATTERN,
        max_length=32,
        description=_tool_docs.IFC_QUERY_EXACT_INPUT_PACKAGE_ALIAS_DESCRIPTION,
    )
    payload_name: str = Field(
        min_length=1,
        max_length=120,
        description=_tool_docs.IFC_QUERY_EXACT_INPUT_PAYLOAD_NAME_DESCRIPTION,
    )

    @field_validator("payload_name")
    @classmethod
    def validate_payload_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("payload_name must be trimmed")
        return _non_blank_utf8(value, maximum=480, label="payload_name")


class IfcQuerySourceEdit(DirectContract):
    old_text: str = Field(
        min_length=1,
        max_length=_MAX_EDIT_BYTES,
        description=_tool_docs.IFC_QUERY_SOURCE_EDIT_OLD_TEXT_DESCRIPTION,
    )
    new_text: str = Field(
        default="",
        max_length=_MAX_EDIT_BYTES,
        description=_tool_docs.IFC_QUERY_SOURCE_EDIT_NEW_TEXT_DESCRIPTION,
    )

    @field_validator("old_text", "new_text")
    @classmethod
    def validate_edit_text(cls, value: str, info) -> str:
        return _utf8_bounded(value, maximum=_MAX_EDIT_BYTES, label=info.field_name)


class DirectIfcQueryFreshInput(_DirectIfcQueryInput):
    mode: Literal["fresh"] = Field(
        description=(_tool_docs.DIRECT_IFC_QUERY_FRESH_INPUT_MODE_DESCRIPTION)
    )
    inputs: tuple[IfcQueryExactInput, ...] = Field(
        default=(),
        max_length=16,
        description=(_tool_docs.DIRECT_IFC_QUERY_FRESH_INPUT_INPUTS_DESCRIPTION),
    )
    code: str = Field(
        min_length=1,
        max_length=_MAX_SOURCE_BYTES,
        description=(_tool_docs.DIRECT_IFC_QUERY_FRESH_INPUT_CODE_DESCRIPTION),
    )
    page_size: int = Field(
        default=100,
        strict=True,
        ge=1,
        le=200,
        description=_tool_docs.DIRECT_IFC_QUERY_FRESH_INPUT_PAGE_SIZE_DESCRIPTION,
    )

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        return _non_blank_utf8(value, maximum=_MAX_SOURCE_BYTES, label="query code")

    @model_validator(mode="after")
    def validate_exact_inputs(self) -> "DirectIfcQueryFreshInput":
        names = [item.input_name for item in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError("input_name values must be unique")
        exact_inputs = [(item.package_alias, item.payload_name) for item in self.inputs]
        if len(exact_inputs) != len(set(exact_inputs)):
            raise ValueError("the same exact Package Payload cannot be bound twice")
        return self


class DirectIfcQueryRepairInput(_DirectIfcQueryInput):
    mode: Literal["repair"] = Field(
        description=(_tool_docs.DIRECT_IFC_QUERY_REPAIR_INPUT_MODE_DESCRIPTION)
    )
    parent_attempt_alias: str = Field(
        pattern=_ATTEMPT_ALIAS_PATTERN,
        max_length=16_384,
        description=_tool_docs.DIRECT_IFC_QUERY_REPAIR_INPUT_PARENT_ATTEMPT_ALIAS_DESCRIPTION,
    )
    edits: tuple[IfcQuerySourceEdit, ...] = Field(
        min_length=1,
        max_length=16,
        description=_tool_docs.DIRECT_IFC_QUERY_REPAIR_INPUT_EDITS_DESCRIPTION,
    )
    page_size: int = Field(
        default=100,
        strict=True,
        ge=1,
        le=200,
        description=_tool_docs.DIRECT_IFC_QUERY_REPAIR_INPUT_PAGE_SIZE_DESCRIPTION,
    )


class DirectIfcQueryCursorInput(_DirectIfcQueryInput):
    mode: Literal["cursor"] = Field(
        description=_tool_docs.DIRECT_IFC_QUERY_CURSOR_INPUT_MODE_DESCRIPTION
    )
    cursor: str = Field(
        pattern=_CURSOR_PATTERN,
        max_length=16_384,
        description=_tool_docs.DIRECT_IFC_QUERY_CURSOR_INPUT_CURSOR_DESCRIPTION,
    )


DirectIfcQueryPythonInput: TypeAlias = (
    DirectIfcQueryFreshInput | DirectIfcQueryRepairInput | DirectIfcQueryCursorInput
)


class IfcQueryPythonResult(DirectContract):
    attempt_alias: str = Field(pattern=_ATTEMPT_ALIAS_PATTERN, max_length=16_384)
    parent_attempt_alias: str | None = Field(
        default=None,
        pattern=_ATTEMPT_ALIAS_PATTERN,
        max_length=16_384,
    )
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    status: Literal["succeeded"]
    summary: str = Field(min_length=1, max_length=4096)
    result_json: str = Field(
        max_length=65_536,
        description=_tool_docs.IFC_QUERY_PYTHON_RESULT_RESULT_JSON_DESCRIPTION,
    )
    next_cursor: str | None = Field(default=None, pattern=_CURSOR_PATTERN, max_length=16_384)

    @field_validator("result_json")
    @classmethod
    def validate_result_json(cls, value: str) -> str:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("result_json must contain valid JSON") from exc
        if canonical_json(decoded) != value:
            raise ValueError("result_json must be RFC 8785 canonical JSON")
        return value


IFC_QUERY_INPUT_ADAPTER = TypeAdapter(DirectIfcQueryPythonInput)


def validate_ifc_query_input(value: object) -> DirectIfcQueryPythonInput:
    return IFC_QUERY_INPUT_ADAPTER.validate_python(value)


__all__ = [name for name in globals() if name.startswith(("DirectIfc", "IfcQuery"))] + [
    "IFC_QUERY_INPUT_ADAPTER",
    "validate_ifc_query_input",
]
