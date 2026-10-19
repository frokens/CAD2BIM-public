"""Closed Direct Provider contracts for TASK geometry authoring."""

from __future__ import annotations

import json
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter, field_validator, model_validator

from CAD2BIM_backend.domain.geometry_authoring import (
    GeometryDraftWriteCommand, GeometryParameterSchemaSet, GeometryParameterSchemaChange,
)
from CAD2BIM_backend.domain.geometry_interface import GeometryModelInterfaces
from CAD2BIM_backend.domain.geometry_runtime import (
    GEOMETRY_INPUT_KEY_PATTERN,
    MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS, MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES,
    MAX_GEOMETRY_BUILD_INPUTS,
)
from CAD2BIM_backend.domain.text_source import (
    SourceBundleChange, SourceBundleAddMember, SourceBundleDeleteMember, SourceBundleLiteralEdit,
    SourceBundleReplaceMember, SourceEdit,
    TextChange, TextSet,
)
from CAD2BIM_mcp.direct.contract_base import DirectContract
from CAD2BIM_mcp.direct.ifc.geometry.contracts import (
    _DirectGeometryInput,
    _safe_source_path,
    _utf8_bounded,
)
from CAD2BIM_mcp.tool_docs import ifc_geometry as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


_DRAFT_COMMAND = TypeAdapter(GeometryDraftWriteCommand)
_SOURCE_CHANGE = TypeAdapter(SourceBundleChange)

_SAFE_REFERENCE_PATTERN = r"^cur\..+$"
_SOURCE_PATH_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,254}$"
_OPERATION_ALIAS_PATTERN = r"^op_[0-9]{3,}$"
_MAX_EDIT_BYTES = 16_384
# The 16 KiB final schema budget belongs to the Domain validator; action/key
# overhead must not reduce the capacity of a valid complete schema.
_MAX_PARAMETER_CHANGE_JSON_BYTES = 32_768


class IfcGeometryAuthoringSourceFile(DirectContract):
    source_path: str = Field(
        pattern=_SOURCE_PATH_PATTERN,
        max_length=255,
        description=(_tool_docs.IFC_GEOMETRY_AUTHORING_SOURCE_FILE_SOURCE_PATH_DESCRIPTION),
    )
    source: str = Field(
        min_length=1,
        max_length=MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES,
        description=_tool_docs.IFC_GEOMETRY_AUTHORING_SOURCE_FILE_SOURCE_DESCRIPTION,
    )

    _source_path = field_validator("source_path")(_safe_source_path)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return _utf8_bounded(value, maximum=MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES, label="authoring source")


class IfcGeometryAuthoringInput(DirectContract):
    input_key: str = Field(
        pattern=GEOMETRY_INPUT_KEY_PATTERN,
        description=_tool_docs.GEOMETRY_AUTHORING_INPUT_KEY_DESCRIPTION,
    )
    resource_version_alias: str = Field(
        pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384,
        description=_tool_docs.GEOMETRY_AUTHORING_INPUT_VERSION_DESCRIPTION,
    )


class IfcGeometryLiteralReplacement(DirectContract):
    old_text: str = Field(min_length=1, max_length=_MAX_EDIT_BYTES)
    new_text: str = Field(default="", max_length=_MAX_EDIT_BYTES)

    @field_validator("old_text", "new_text")
    @classmethod
    def validate_text(cls, value: str, info) -> str:
        return _utf8_bounded(value, maximum=_MAX_EDIT_BYTES, label=info.field_name)


class IfcGeometryLiteralEditChange(DirectContract):
    change_kind: Literal["literal_edit"]
    source_path: str = Field(pattern=_SOURCE_PATH_PATTERN, max_length=255)
    replacements: tuple[IfcGeometryLiteralReplacement, ...] = Field(min_length=1, max_length=16)
    _source_path = field_validator("source_path")(_safe_source_path)

    def to_domain(self) -> SourceBundleLiteralEdit:
        return SourceBundleLiteralEdit(path=self.source_path, edits=tuple(
            SourceEdit(old_text=v.old_text, new_text=v.new_text) for v in self.replacements))


class IfcGeometryAddMemberChange(IfcGeometryAuthoringSourceFile):
    change_kind: Literal["add_member"]

    def to_domain(self) -> SourceBundleAddMember:
        return SourceBundleAddMember(path=self.source_path, source=self.source)


class IfcGeometryReplaceMemberChange(IfcGeometryAuthoringSourceFile):
    change_kind: Literal["replace_member"]

    def to_domain(self) -> SourceBundleReplaceMember:
        return SourceBundleReplaceMember(path=self.source_path, source=self.source)


class IfcGeometryDeleteMemberChange(DirectContract):
    change_kind: Literal["delete_member"]
    source_path: str = Field(pattern=_SOURCE_PATH_PATTERN, max_length=255)
    _source_path = field_validator("source_path")(_safe_source_path)

    def to_domain(self) -> SourceBundleDeleteMember:
        return SourceBundleDeleteMember(path=self.source_path)


IfcGeometrySourceChange: TypeAlias = Annotated[
    IfcGeometryLiteralEditChange | IfcGeometryAddMemberChange
    | IfcGeometryReplaceMemberChange | IfcGeometryDeleteMemberChange,
    Field(discriminator="change_kind"),
]


class IfcGeometryMappedDraftSource(DirectContract):
    source_kind: Literal["mapped_model"]
    source_files: tuple[IfcGeometryAuthoringSourceFile, ...] = Field(min_length=1, max_length=MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS, description=_tool_docs.IFC_GEOMETRY_SOURCE_FILES_DESCRIPTION)
    geometry_inputs: tuple[IfcGeometryAuthoringInput, ...] = Field(default=(), max_length=MAX_GEOMETRY_BUILD_INPUTS, description=_tool_docs.GEOMETRY_AUTHORING_INPUTS_DESCRIPTION)
    interfaces: GeometryModelInterfaces = ()


class IfcGeometryFunctionDraftSource(DirectContract):
    source_kind: Literal["modeling_function"]
    source_files: tuple[IfcGeometryAuthoringSourceFile, ...] = Field(min_length=1, max_length=MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS, description=_tool_docs.IFC_GEOMETRY_SOURCE_FILES_DESCRIPTION)
    geometry_inputs: tuple[IfcGeometryAuthoringInput, ...] = Field(default=(), max_length=MAX_GEOMETRY_BUILD_INPUTS, description=_tool_docs.GEOMETRY_AUTHORING_INPUTS_DESCRIPTION)
    parameter_schema_json: str = Field(min_length=2, max_length=_MAX_PARAMETER_CHANGE_JSON_BYTES, description=_tool_docs.GEOMETRY_DRAFT_PARAMETER_SCHEMA_DESCRIPTION)

    @field_validator("parameter_schema_json")
    @classmethod
    def validate_parameter_set(cls, value):
        _utf8_bounded(value, maximum=_MAX_PARAMETER_CHANGE_JSON_BYTES, label="parameter_schema_json action")
        TypeAdapter(GeometryParameterSchemaSet).validate_python(json.loads(value))
        return value


class IfcGeometryPublishedDraftSource(DirectContract):
    source_kind: Literal["published"]
    resource_version_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)


IfcGeometryDraftCreateSource = Annotated[
    IfcGeometryMappedDraftSource | IfcGeometryFunctionDraftSource,
    Field(discriminator="source_kind"),
]


class IfcGeometryDraftCreateInput(DirectContract):
    mode: Literal["create"]
    display_name: str = Field(min_length=1, max_length=255)
    usage_instructions: TextSet = Field(description=_common_tool_docs.AUTHORING_USAGE_INSTRUCTIONS_DESCRIPTION)
    source: IfcGeometryDraftCreateSource

    def to_domain_declaration(self):
        value = self.model_dump(mode="json", exclude={"direct_session_alias"}, exclude_unset=True)
        source = value["source"]
        if "source_files" in source:
            source["source_files"] = [{"path": f["source_path"], "source": f["source"]} for f in source["source_files"]]
        if "parameter_schema_json" in source:
            source["parameter_schema"] = json.loads(source.pop("parameter_schema_json"))
        return _DRAFT_COMMAND.validate_python(value).model_dump(mode="json", exclude_unset=True, by_alias=True)


class DirectIfcGeometryDraftCreateInput(IfcGeometryDraftCreateInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


class IfcGeometryDraftPublishedCreateInput(IfcGeometryDraftCreateInput):
    source: IfcGeometryPublishedDraftSource
    display_name: str = Field(default_factory=lambda: None, min_length=1, max_length=255)
    usage_instructions: TextSet = Field(default_factory=lambda: None, description=_common_tool_docs.AUTHORING_USAGE_INSTRUCTIONS_DESCRIPTION)


class DirectIfcGeometryDraftPublishedCreateInput(IfcGeometryDraftPublishedCreateInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


class IfcGeometryDraftUpdateInput(DirectContract):
    mode: Literal["update"]
    draft_ref: str = Field(min_length=1, max_length=16_384, description=_common_tool_docs.AUTHORING_DRAFT_REF_DESCRIPTION)
    expected_edit_token: int = Field(ge=1, strict=True, description=_common_tool_docs.AUTHORING_EXPECTED_EDIT_TOKEN_DESCRIPTION)
    # None is only an omitted-field sentinel; explicit null remains invalid.
    display_name: str = Field(default_factory=lambda: None, min_length=1, max_length=255)
    usage_instructions: TextChange = Field(default_factory=lambda: None, description=_common_tool_docs.AUTHORING_USAGE_INSTRUCTIONS_DESCRIPTION)
    source_changes: tuple[IfcGeometrySourceChange, ...] = Field(default_factory=lambda: None, max_length=64, description=_tool_docs.GEOMETRY_DRAFT_SOURCE_CHANGES_DESCRIPTION)
    geometry_inputs: tuple[IfcGeometryAuthoringInput, ...] = Field(default_factory=lambda: None, max_length=MAX_GEOMETRY_BUILD_INPUTS, description=_tool_docs.GEOMETRY_DRAFT_INPUTS_DESCRIPTION)
    parameter_schema_json: str = Field(default_factory=lambda: None, min_length=2, max_length=_MAX_PARAMETER_CHANGE_JSON_BYTES, description=_tool_docs.GEOMETRY_DRAFT_PARAMETER_SCHEMA_DESCRIPTION)
    interfaces: GeometryModelInterfaces = Field(default_factory=lambda: None)

    @field_validator("parameter_schema_json")
    @classmethod
    def validate_parameter_change(cls, value):
        _utf8_bounded(value, maximum=_MAX_PARAMETER_CHANGE_JSON_BYTES, label="parameter_schema_json action")
        TypeAdapter(GeometryParameterSchemaChange).validate_python(json.loads(value))
        return value

    @model_validator(mode="after")
    def require_nonnull_changes(self):
        changes = self.model_fields_set - {"direct_session_alias", "mode", "draft_ref", "expected_edit_token"}
        if not changes or any(getattr(self, key) is None for key in changes):
            raise ValueError("update requires at least one non-null change; omit inherited fields")
        return self

    def to_domain_declaration(self):
        value = self.model_dump(mode="json", exclude={"direct_session_alias"}, exclude_unset=True)
        if self.source_changes is not None:
            value["source_changes"] = [_SOURCE_CHANGE.dump_python(item.to_domain(), mode="json") for item in self.source_changes]
        if "parameter_schema_json" in value:
            value["parameter_schema"] = json.loads(value.pop("parameter_schema_json"))
        return _DRAFT_COMMAND.validate_python(value).model_dump(mode="json", exclude_unset=True, by_alias=True)


class DirectIfcGeometryDraftUpdateInput(IfcGeometryDraftUpdateInput, _DirectGeometryInput):
    __doc__ = _tool_docs.DIRECT_GEOMETRY_ENTRY_SCOPE_DESCRIPTION


DirectIfcGeometryResourceWriteInput = (
    DirectIfcGeometryDraftCreateInput | DirectIfcGeometryDraftPublishedCreateInput | DirectIfcGeometryDraftUpdateInput
)


class IfcGeometryResourceWriteResult(DirectContract):
    draft_ref: str = Field(min_length=1, max_length=16_384, description=_common_tool_docs.AUTHORING_DRAFT_REF_DESCRIPTION)
    edit_token: int = Field(ge=1, strict=True, description=_common_tool_docs.AUTHORING_EDIT_TOKEN_DESCRIPTION)
    operation_alias: str = Field(pattern=_OPERATION_ALIAS_PATTERN, max_length=32)
    saved: Literal[True] = True
    runtime_checked: Literal[False] = False


_WRITE_ADAPTER = TypeAdapter(DirectIfcGeometryResourceWriteInput)


def validate_geometry_resource_write_input(value: object) -> DirectIfcGeometryResourceWriteInput:
    request = _WRITE_ADAPTER.validate_python(value)
    request.to_domain_declaration()
    return request
