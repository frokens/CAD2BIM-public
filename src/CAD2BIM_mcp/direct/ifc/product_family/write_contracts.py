"""Closed Direct Provider contracts for TASK ProductFamily authoring."""

from __future__ import annotations

import unicodedata
from typing import Annotated, ClassVar, Literal, TypeAlias

from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from CAD2BIM_backend.domain.product_realization import (
    MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES,
    MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS,
)
from CAD2BIM_backend.domain.text_source import (
    SourceBundleChange,
    SourceBundleAddMember,
    SourceBundleDeleteMember,
    SourceBundleLiteralEdit,
    SourceBundleReplaceMember,
    SourceEdit,
    TextChange,
    TextSet,
)
from CAD2BIM_backend.domain.product_family_authoring import MAX_FAMILY_DRAFT_DEFINITION_JSON_BYTES, FamilyDraftDefinitionSet, FamilyDraftDefinitionChange, FamilyDraftWriteCommand
from CAD2BIM_mcp.direct.contract_base import DirectContract
from CAD2BIM_mcp.direct.provider_schema import provider_reference_schema
from CAD2BIM_mcp.direct.tool_support import (
    ProviderJsonDocumentError,
    decode_provider_json_object,
    provider_validation_message,
)
from CAD2BIM_mcp.tool_docs import ifc_product_family as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


_DIRECT_ALIAS_PATTERN = r"^direct_[0-9]{3,}$"
_SAFE_REFERENCE_PATTERN = r"^cur\..+$"
_OPERATION_ALIAS_PATTERN = r"^op_[0-9]{3,}$"
_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_SOURCE_PATH_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,254}$"
_MAX_EDIT_BYTES = 16_384
_MAX_DEFINITION_DOCUMENT_BYTES = MAX_FAMILY_DRAFT_DEFINITION_JSON_BYTES + 128 * 1024
_MAX_SOURCE_CHANGES_DOCUMENT_BYTES = 64 * 1024 * 1024

IfcProductFamilyGeometryResourceKind: TypeAlias = Literal["mapped_model", "modeling_function"]
IfcProductFamilyGeometryPurpose: TypeAlias = Literal[
    "product_component", "shape_fragment"
]


def _safe_source_path(value: str) -> str:
    if (
        not value.endswith(".py")
        or "\\" in value
        or value.startswith("/")
        or "//" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("source_path must be a normalized relative POSIX .py path")
    return value


def _utf8_bounded(value: str, *, maximum: int, label: str, allow_empty: bool = False) -> str:
    if (not allow_empty and not value) or "\x00" in value:
        raise ValueError(f"{label} must be valid non-NUL UTF-8 text")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds {maximum} UTF-8 bytes")
    return value


def _normalized_text(value: str, *, label: str) -> str:
    if value != value.strip() or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ValueError(f"{label} must be trimmed and contain no control characters")
    return value


class _DirectProductFamilyWriteInput(DirectContract):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_TASK_SCOPE_ALIAS_DESCRIPTION,
    )


class IfcProductFamilyAuthoringSourceFile(DirectContract):
    source_path: str = Field(
        pattern=_SOURCE_PATH_PATTERN,
        max_length=255,
        description=(_tool_docs.IFC_PRODUCT_FAMILY_AUTHORING_SOURCE_FILE_SOURCE_PATH_DESCRIPTION),
    )
    source: str = Field(
        min_length=1,
        max_length=MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES,
        description=_tool_docs.IFC_PRODUCT_FAMILY_AUTHORING_SOURCE_FILE_SOURCE_DESCRIPTION,
    )

    _validate_path = field_validator("source_path")(_safe_source_path)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return _utf8_bounded(
            value,
            maximum=MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES,
            label="source",
        )


class IfcProductFamilyNumberParameterValue(DirectContract):
    parameter_kind: Literal["number"]
    parameter_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    value: float = Field(strict=True)


class IfcProductFamilyIntegerParameterValue(DirectContract):
    parameter_kind: Literal["integer"]
    parameter_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    value: StrictInt


class IfcProductFamilyBooleanParameterValue(DirectContract):
    parameter_kind: Literal["boolean"]
    parameter_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    value: StrictBool


class IfcProductFamilyChoiceParameterValue(DirectContract):
    parameter_kind: Literal["choice"]
    parameter_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    value: str = Field(min_length=1, max_length=64)


class IfcProductFamilyLiteralReplacement(DirectContract):
    old_text: str = Field(min_length=1, max_length=_MAX_EDIT_BYTES)
    new_text: str = Field(default="", max_length=_MAX_EDIT_BYTES)

    @field_validator("old_text")
    @classmethod
    def validate_old_text(cls, value: str) -> str:
        return _utf8_bounded(value, maximum=_MAX_EDIT_BYTES, label="old_text")

    @field_validator("new_text")
    @classmethod
    def validate_new_text(cls, value: str) -> str:
        return _utf8_bounded(value, maximum=_MAX_EDIT_BYTES, label="new_text", allow_empty=True)


class IfcProductFamilyLiteralEditChange(DirectContract):
    change_kind: Literal["literal_edit"]
    source_path: str = Field(pattern=_SOURCE_PATH_PATTERN, max_length=255)
    replacements: tuple[IfcProductFamilyLiteralReplacement, ...] = Field(
        min_length=1, max_length=16
    )

    _validate_path = field_validator("source_path")(_safe_source_path)

    @field_validator("replacements")
    @classmethod
    def require_unique_literals(
        cls,
        value: tuple[IfcProductFamilyLiteralReplacement, ...],
    ) -> tuple[IfcProductFamilyLiteralReplacement, ...]:
        literals = tuple(item.old_text for item in value)
        if len(literals) != len(set(literals)):
            raise ValueError("literal_edit old_text values must be unique per member")
        return value

    def to_domain(self) -> SourceBundleLiteralEdit:
        return SourceBundleLiteralEdit(
            path=self.source_path,
            edits=tuple(
                SourceEdit(old_text=item.old_text, new_text=item.new_text)
                for item in self.replacements
            ),
        )


class IfcProductFamilyAddMemberChange(DirectContract):
    change_kind: Literal["add_member"]
    source_path: str = Field(pattern=_SOURCE_PATH_PATTERN, max_length=255)
    source: str = Field(min_length=1, max_length=MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES)

    _validate_path = field_validator("source_path")(_safe_source_path)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return _utf8_bounded(
            value,
            maximum=MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES,
            label="source",
        )

    def to_domain(self) -> SourceBundleAddMember:
        return SourceBundleAddMember(path=self.source_path, source=self.source)


class IfcProductFamilyReplaceMemberChange(IfcProductFamilyAddMemberChange):
    change_kind: Literal["replace_member"]

    def to_domain(self) -> SourceBundleReplaceMember:
        return SourceBundleReplaceMember(
            path=self.source_path,
            source=self.source,
        )


class IfcProductFamilyDeleteMemberChange(DirectContract):
    change_kind: Literal["delete_member"]
    source_path: str = Field(pattern=_SOURCE_PATH_PATTERN, max_length=255)

    _validate_path = field_validator("source_path")(_safe_source_path)

    @model_validator(mode="after")
    def keep_entry_member(self) -> "IfcProductFamilyDeleteMemberChange":
        if self.source_path == "main.py":
            raise ValueError("source changes cannot delete main.py")
        return self

    def to_domain(self) -> SourceBundleDeleteMember:
        return SourceBundleDeleteMember(
            path=self.source_path,
        )


IfcProductFamilySourceChange: TypeAlias = Annotated[
    IfcProductFamilyLiteralEditChange
    | IfcProductFamilyAddMemberChange
    | IfcProductFamilyReplaceMemberChange
    | IfcProductFamilyDeleteMemberChange,
    Field(discriminator="change_kind"),
]


class _ProductFamilySourceChangesDocument(DirectContract):
    changes: tuple[IfcProductFamilySourceChange, ...] = Field(
        min_length=1, max_length=MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS,
    )


class IfcProductFamilyInlineDraftSource(DirectContract):
    source_kind: Literal["inline"]
    source_files: tuple[IfcProductFamilyAuthoringSourceFile, ...] = Field(
        min_length=1, max_length=MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS,
        description=_tool_docs.PRODUCT_FAMILY_DRAFT_SOURCE_FILES_DESCRIPTION,
    )
    definition_json: str = Field(min_length=2, max_length=_MAX_DEFINITION_DOCUMENT_BYTES, description=_tool_docs.PRODUCT_FAMILY_DRAFT_DEFINITION_DESCRIPTION)


class IfcProductFamilyPublishedDraftSource(DirectContract):
    source_kind: Literal["published"]
    family_version_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)


IfcProductFamilyDraftCreateSource: TypeAlias = Annotated[
    IfcProductFamilyInlineDraftSource | IfcProductFamilyPublishedDraftSource,
    Field(discriminator="source_kind"),
]
_SOURCE_CHANGE_ADAPTER = TypeAdapter(SourceBundleChange)
_DRAFT_COMMAND_ADAPTER = TypeAdapter(FamilyDraftWriteCommand)


def _parse_definition_document(value: str, *, create: bool = False):
    decoded = decode_provider_json_object(
        value, label="definition_json", maximum_utf8_bytes=_MAX_DEFINITION_DOCUMENT_BYTES,
    )
    try:
        return TypeAdapter(FamilyDraftDefinitionSet if create else FamilyDraftDefinitionChange).validate_python(decoded)
    except ValidationError as exc:
        raise ProviderJsonDocumentError(provider_validation_message(
            "definition_json is invalid", exc, include_value_error_reason=True,
        )) from exc


def _parse_source_changes_document(value: str, *, document_type=_ProductFamilySourceChangesDocument):
    decoded = decode_provider_json_object(
        value, label="source_changes_json", maximum_utf8_bytes=_MAX_SOURCE_CHANGES_DOCUMENT_BYTES,
    )
    try:
        return document_type.model_validate(decoded)
    except ValidationError as exc:
        raise ProviderJsonDocumentError(provider_validation_message(
            "source_changes_json is invalid", exc, include_value_error_reason=True,
        )) from exc


class _IfcProductFamilyDraftCreateInput(DirectContract):
    mode: Literal["create"]

    def to_domain_declaration(self) -> dict:
        value = self.model_dump(mode="json", exclude={"direct_session_alias"}, exclude_unset=True)
        if isinstance(self.source, IfcProductFamilyInlineDraftSource):
            value["source"]["definition"] = _parse_definition_document(
                value["source"].pop("definition_json"), create=True,
            ).model_dump(mode="json", exclude_unset=True)
            value["source"]["source_files"] = [
                {"path": item.source_path, "source": item.source} for item in self.source.source_files
            ]
        return _DRAFT_COMMAND_ADAPTER.validate_python(value).model_dump(mode="json", exclude_unset=True)


class IfcProductFamilyInlineDraftCreateInput(_IfcProductFamilyDraftCreateInput):
    display_name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2_048)
    usage_instructions: TextSet = Field(description=_common_tool_docs.AUTHORING_USAGE_INSTRUCTIONS_DESCRIPTION)
    source: IfcProductFamilyInlineDraftSource


class DirectIfcProductFamilyInlineDraftCreateInput(IfcProductFamilyInlineDraftCreateInput, _DirectProductFamilyWriteInput):
    __doc__ = _tool_docs.DIRECT_IFC_PRODUCT_FAMILY_SCOPED_INPUT_DESCRIPTION


class IfcProductFamilyPublishedDraftCreateInput(_IfcProductFamilyDraftCreateInput):
    display_name: str = Field(default_factory=lambda: None, min_length=1, max_length=255)
    purpose: str = Field(default_factory=lambda: None, min_length=1, max_length=2_048)
    usage_instructions: TextSet = Field(default_factory=lambda: None, description=_common_tool_docs.AUTHORING_USAGE_INSTRUCTIONS_DESCRIPTION)
    source: IfcProductFamilyPublishedDraftSource


class DirectIfcProductFamilyPublishedDraftCreateInput(IfcProductFamilyPublishedDraftCreateInput, _DirectProductFamilyWriteInput):
    __doc__ = _tool_docs.DIRECT_IFC_PRODUCT_FAMILY_SCOPED_INPUT_DESCRIPTION


DirectIfcProductFamilyDraftCreateInput: TypeAlias = DirectIfcProductFamilyInlineDraftCreateInput | DirectIfcProductFamilyPublishedDraftCreateInput


class IfcProductFamilyDraftUpdateInput(DirectContract):
    _source_changes_document_type: ClassVar[type] = _ProductFamilySourceChangesDocument
    mode: Literal["update"]
    draft_ref: str = Field(min_length=1, max_length=16_384, description=_common_tool_docs.AUTHORING_DRAFT_REF_DESCRIPTION)
    expected_edit_token: int = Field(strict=True, ge=1, description=_common_tool_docs.AUTHORING_EXPECTED_EDIT_TOKEN_DESCRIPTION)
    # None is only an omitted-field sentinel; explicit null remains invalid.
    display_name: str = Field(default_factory=lambda: None, min_length=1, max_length=255)
    purpose: str = Field(default_factory=lambda: None, min_length=1, max_length=2_048)
    usage_instructions: TextChange = Field(default_factory=lambda: None, description=_common_tool_docs.AUTHORING_USAGE_INSTRUCTIONS_DESCRIPTION)
    definition_json: str = Field(default_factory=lambda: None, min_length=2, max_length=_MAX_DEFINITION_DOCUMENT_BYTES, description=_tool_docs.PRODUCT_FAMILY_DRAFT_DEFINITION_DESCRIPTION)
    source_changes_json: str = Field(default_factory=lambda: None, min_length=2, max_length=_MAX_SOURCE_CHANGES_DOCUMENT_BYTES, description=_tool_docs.PRODUCT_FAMILY_DRAFT_SOURCE_CHANGES_DESCRIPTION)

    @model_validator(mode="after")
    def require_nonnull_changes(self):
        changes = self.model_fields_set - {"direct_session_alias", "mode", "draft_ref", "expected_edit_token"}
        if not changes or any(getattr(self, key) is None for key in changes):
            raise ValueError("update requires at least one non-null change; omit inherited fields")
        return self

    def to_domain_declaration(self) -> dict:
        value = self.model_dump(mode="json", exclude={"direct_session_alias"}, exclude_unset=True)
        if "definition_json" in value:
            value["definition"] = _parse_definition_document(value.pop("definition_json")).model_dump(mode="json", exclude_unset=True)
        if "source_changes_json" in value:
            document = _parse_source_changes_document(value.pop("source_changes_json"),
                document_type=self._source_changes_document_type)
            value["source_changes"] = [
                _SOURCE_CHANGE_ADAPTER.dump_python(item.to_domain(), mode="json") for item in document.changes
            ]
        return _DRAFT_COMMAND_ADAPTER.validate_python(value).model_dump(mode="json", exclude_unset=True)


class DirectIfcProductFamilyDraftUpdateInput(IfcProductFamilyDraftUpdateInput, _DirectProductFamilyWriteInput):
    __doc__ = _tool_docs.DIRECT_IFC_PRODUCT_FAMILY_SCOPED_INPUT_DESCRIPTION


DirectIfcProductFamilyWriteInput: TypeAlias = DirectIfcProductFamilyInlineDraftCreateInput | DirectIfcProductFamilyPublishedDraftCreateInput | DirectIfcProductFamilyDraftUpdateInput
DirectIfcProductFamilyWriteProviderInput = DirectIfcProductFamilyWriteInput
PRODUCT_FAMILY_WRITE_INPUT_ADAPTER = TypeAdapter(DirectIfcProductFamilyWriteInput)


class IfcProductFamilyWriteResult(DirectContract):
    draft_ref: str = Field(min_length=1, max_length=16_384, description=_common_tool_docs.AUTHORING_DRAFT_REF_DESCRIPTION)
    edit_token: int = Field(strict=True, ge=1, description=_common_tool_docs.AUTHORING_EDIT_TOKEN_DESCRIPTION)
    operation_alias: str = Field(pattern=_OPERATION_ALIAS_PATTERN, max_length=32)
    saved: Literal[True] = True
    runtime_checked: Literal[False] = False


def validate_product_family_write_input(value: object) -> DirectIfcProductFamilyWriteInput:
    result = PRODUCT_FAMILY_WRITE_INPUT_ADAPTER.validate_python(value)
    result.to_domain_declaration()
    return result


def validate_product_family_write_provider_input(value: object) -> DirectIfcProductFamilyWriteInput:
    return validate_product_family_write_input(value)


def product_family_definition_reference_document(*, create: bool = False) -> tuple[dict[str, object], int]:
    return provider_reference_schema(FamilyDraftDefinitionSet if create else FamilyDraftDefinitionChange), _MAX_DEFINITION_DOCUMENT_BYTES


def product_family_definition_resource_document() -> dict[str, object]:
    return {
        "create_definition_input_schema": product_family_definition_reference_document(create=True)[0],
        "update_definition_input_schema": product_family_definition_reference_document()[0],
    }


def product_family_source_changes_schema(document_type=_ProductFamilySourceChangesDocument) -> dict[str, object]:
    return provider_reference_schema(document_type)
