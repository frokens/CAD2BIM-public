"""Provider-safe Direct inputs for transformation source reads and repairs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from pydantic import Field, TypeAdapter, field_validator, model_validator

from CAD2BIM_backend.domain.data_transform import (
    DataRepairTransformCommand,
    OutputPackageUpdate,
    TransformOutputsChange,
)
from CAD2BIM_backend.domain.text_source import SourceEdit, TextEdit
from CAD2BIM_mcp.direct.transform.common_inputs import (
    DirectM2aInput,
    OperationTransformationCodeReference,
    PackageTransformationCodeReference,
)
from CAD2BIM_mcp.tool_docs import data_transform as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


_DIRECT_ALIAS_PATTERN = r"^direct_[0-9]{3,}$"
_OPERATION_ALIAS_PATTERN = r"^op_[0-9]{3,}$"
_MAX_SOURCE_BYTES = 65_536


def _utf8_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


class _DirectCodeReadBase(DirectM2aInput):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )


class _FullCodeRead(DirectM2aInput):
    view_mode: Literal["full"] = Field(
        default="full",
        description=(_tool_docs.FULL_CODE_READ_VIEW_MODE_DESCRIPTION),
    )


class _LinesCodeRead(DirectM2aInput):
    view_mode: Literal["lines"] = Field(
        description=_tool_docs.LINES_CODE_READ_VIEW_MODE_DESCRIPTION
    )
    start_line: int = Field(
        strict=True,
        ge=1,
        le=_MAX_SOURCE_BYTES,
        description=_tool_docs.LINES_CODE_READ_START_LINE_DESCRIPTION,
    )
    line_count: int = Field(
        strict=True,
        ge=1,
        le=200,
        description=_tool_docs.LINES_CODE_READ_LINE_COUNT_DESCRIPTION,
    )


class _SearchCodeRead(DirectM2aInput):
    view_mode: Literal["search"] = Field(
        description=_tool_docs.SEARCH_CODE_READ_VIEW_MODE_DESCRIPTION
    )
    text: str = Field(
        min_length=1,
        max_length=_MAX_SOURCE_BYTES,
        description=(_tool_docs.SEARCH_CODE_READ_TEXT_DESCRIPTION),
    )
    context_lines: int = Field(
        default=3,
        strict=True,
        ge=0,
        le=20,
        description=_tool_docs.SEARCH_CODE_READ_CONTEXT_LINES_DESCRIPTION,
    )
    max_matches: int = Field(
        default=20,
        strict=True,
        ge=1,
        le=50,
        description=_tool_docs.SEARCH_CODE_READ_MAX_MATCHES_DESCRIPTION,
    )

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("search text must not contain NUL")
        if _utf8_bytes(value) > _MAX_SOURCE_BYTES:
            raise ValueError("search text exceeds 65,536 UTF-8 bytes")
        return value


class DirectPackageCodeFullInput(_DirectCodeReadBase, _FullCodeRead):
    reference: PackageTransformationCodeReference = Field(
        description=_tool_docs.DIRECT_PACKAGE_CODE_FULL_INPUT_REFERENCE_DESCRIPTION
    )


class DirectOperationCodeFullInput(_DirectCodeReadBase, _FullCodeRead):
    reference: OperationTransformationCodeReference = Field(
        description=_tool_docs.DIRECT_OPERATION_CODE_FULL_INPUT_REFERENCE_DESCRIPTION
    )


class DirectPackageCodeLinesInput(_DirectCodeReadBase, _LinesCodeRead):
    reference: PackageTransformationCodeReference = Field(
        description=_tool_docs.DIRECT_PACKAGE_CODE_FULL_INPUT_REFERENCE_DESCRIPTION
    )


class DirectOperationCodeLinesInput(_DirectCodeReadBase, _LinesCodeRead):
    reference: OperationTransformationCodeReference = Field(
        description=_tool_docs.DIRECT_OPERATION_CODE_FULL_INPUT_REFERENCE_DESCRIPTION
    )


class DirectPackageCodeSearchInput(_DirectCodeReadBase, _SearchCodeRead):
    reference: PackageTransformationCodeReference = Field(
        description=_tool_docs.DIRECT_PACKAGE_CODE_FULL_INPUT_REFERENCE_DESCRIPTION
    )


class DirectOperationCodeSearchInput(_DirectCodeReadBase, _SearchCodeRead):
    reference: OperationTransformationCodeReference = Field(
        description=_tool_docs.DIRECT_OPERATION_CODE_FULL_INPUT_REFERENCE_DESCRIPTION
    )


DirectTransformationCodeReadInput: TypeAlias = (
    DirectPackageCodeFullInput
    | DirectOperationCodeFullInput
    | DirectPackageCodeLinesInput
    | DirectOperationCodeLinesInput
    | DirectPackageCodeSearchInput
    | DirectOperationCodeSearchInput
)


@dataclass(frozen=True, slots=True)
class TransformationCodeView:
    view_mode: Literal["full", "lines", "search"]
    start_line: int | None = None
    line_count: int | None = None
    text: str | None = None
    context_lines: int = 3
    max_matches: int = 20


DirectTransformSourceEdit = SourceEdit


class FailedOperationRepairSource(DirectM2aInput):
    source_kind: Literal["failed_operation"] = Field(
        description=_tool_docs.FAILED_OPERATION_REPAIR_SOURCE_SOURCE_KIND_DESCRIPTION
    )
    operation_alias: str = Field(
        pattern=_OPERATION_ALIAS_PATTERN,
        max_length=32,
        description=(_tool_docs.FAILED_OPERATION_REPAIR_SOURCE_OPERATION_ALIAS_DESCRIPTION),
    )


class SucceededPackageRepairSource(DirectM2aInput):
    source_kind: Literal["succeeded_package"] = Field(
        description=_tool_docs.SUCCEEDED_PACKAGE_REPAIR_SOURCE_SOURCE_KIND_DESCRIPTION
    )
    package_alias: str = Field(
        pattern=r"^data_[0-9]{3,}$",
        max_length=32,
        description=(_tool_docs.SUCCEEDED_PACKAGE_REPAIR_SOURCE_PACKAGE_ALIAS_DESCRIPTION),
    )


class _DirectRepairTransformBase(DirectM2aInput):
    mode: Literal["repair"] = Field(
        description=_tool_docs.DIRECT_REPAIR_TRANSFORM_BASE_MODE_DESCRIPTION
    )
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )
    edits: tuple[DirectTransformSourceEdit, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=16,
        description=(_tool_docs.DIRECT_REPAIR_TRANSFORM_BASE_EDITS_DESCRIPTION),
    )
    outputs: TransformOutputsChange | None = None
    output_package: OutputPackageUpdate | None = None

    @model_validator(mode="after")
    def validate_edit_budget(self) -> "_DirectRepairTransformBase":
        if self.edits is None and self.outputs is None and self.output_package is None:
            raise ValueError("repair requires edits, outputs or output_package")
        if self.edits is not None:
            TextEdit(action="edit", edits=self.edits)
        return self


class DirectFailedOperationRepairTransformInput(_DirectRepairTransformBase):
    source: FailedOperationRepairSource = Field(
        description=_tool_docs.DIRECT_FAILED_OPERATION_REPAIR_TRANSFORM_INPUT_SOURCE_DESCRIPTION
    )


class DirectSucceededPackageRepairTransformInput(_DirectRepairTransformBase):
    source: SucceededPackageRepairSource = Field(
        description=_tool_docs.DIRECT_SUCCEEDED_PACKAGE_REPAIR_TRANSFORM_INPUT_SOURCE_DESCRIPTION
    )


DirectRepairTransformInput: TypeAlias = (
    DirectFailedOperationRepairTransformInput | DirectSucceededPackageRepairTransformInput
)


_CODE_READ_ADAPTER = TypeAdapter(DirectTransformationCodeReadInput)
_REPAIR_TRANSFORM_ADAPTER = TypeAdapter(DirectRepairTransformInput)


def validate_direct_transformation_code_read_input(
    value: object,
) -> DirectTransformationCodeReadInput:
    return _CODE_READ_ADAPTER.validate_python(value)


def validate_direct_repair_transform_input(value: object) -> DirectRepairTransformInput:
    return _REPAIR_TRANSFORM_ADAPTER.validate_python(value)


def transformation_code_view(value: DirectTransformationCodeReadInput) -> TransformationCodeView:
    if isinstance(value, (DirectPackageCodeLinesInput, DirectOperationCodeLinesInput)):
        return TransformationCodeView(
            view_mode="lines",
            start_line=value.start_line,
            line_count=value.line_count,
        )
    if isinstance(value, (DirectPackageCodeSearchInput, DirectOperationCodeSearchInput)):
        return TransformationCodeView(
            view_mode="search",
            text=value.text,
            context_lines=value.context_lines,
            max_matches=value.max_matches,
        )
    return TransformationCodeView(view_mode="full")


def to_domain_repair_transform_command(
    value: DirectRepairTransformInput,
    *,
    parent_operation_id: str,
) -> DataRepairTransformCommand:
    return DataRepairTransformCommand.model_validate({
        "parent_operation_id": parent_operation_id,
        **value.model_dump(mode="python", exclude={"direct_session_alias", "mode", "source"}, exclude_none=True),
    })


__all__ = [
    "DirectOperationCodeFullInput",
    "DirectOperationCodeLinesInput",
    "DirectOperationCodeSearchInput",
    "DirectFailedOperationRepairTransformInput",
    "DirectPackageCodeFullInput",
    "DirectPackageCodeLinesInput",
    "DirectPackageCodeSearchInput",
    "DirectRepairTransformInput",
    "DirectSucceededPackageRepairTransformInput",
    "DirectTransformSourceEdit",
    "DirectTransformationCodeReadInput",
    "TransformationCodeView",
    "FailedOperationRepairSource",
    "SucceededPackageRepairSource",
    "to_domain_repair_transform_command",
    "transformation_code_view",
    "validate_direct_transformation_code_read_input",
    "validate_direct_repair_transform_input",
]
