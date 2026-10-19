"""Closed Provider contracts for Direct ProductFamily Catalog reads."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import Field, TypeAdapter, field_validator

from CAD2BIM_backend.domain.product_family import (
    MAX_PRODUCT_FAMILY_CONTRACT_JSON_BYTES,
    MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS,
    MAX_PRODUCT_FAMILY_VERSION_ASSETS,
)
from CAD2BIM_mcp.direct.contract_base import DirectContract
from CAD2BIM_mcp.tool_docs import ifc_product_family as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


_DIRECT_ALIAS_PATTERN = r"^direct_[0-9]{3,}$"
_SAFE_REFERENCE_PATTERN = r"^cur\..+$"
_CURSOR_PATTERN = r"^cur\..+$"
_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

IfcProductFamilyOwnerScope: TypeAlias = Literal["system", "tenant", "task"]
IfcProductStructureKind: TypeAlias = Literal["single_product", "semantic_assembly"]
IfcProductFamilyAssetRole: TypeAlias = Literal[
    "realization_source_bundle",
    "standard_model_geometry",
    "standard_model_manifest",
]
IfcProductFamilyGeometryPurpose: TypeAlias = Literal[
    "product_component", "shape_fragment"
]
IfcProductFamilyGeometryResourceKind: TypeAlias = Literal["mapped_model", "modeling_function"]


class _DirectProductFamilyInput(DirectContract):
    direct_session_alias: str = Field(
        pattern=_DIRECT_ALIAS_PATTERN,
        max_length=32,
        description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
    )


class IfcProductFamilyListFirstInput(DirectContract):
    owner_scopes: tuple[IfcProductFamilyOwnerScope, ...] = Field(
        default=(),
        max_length=3,
        description=_tool_docs.DIRECT_IFC_PRODUCT_FAMILY_LIST_FIRST_INPUT_OWNER_SCOPES_DESCRIPTION,
    )
    product_structure_kinds: tuple[IfcProductStructureKind, ...] = Field(
        default=(),
        max_length=2,
        description=_tool_docs.DIRECT_IFC_PRODUCT_FAMILY_LIST_FIRST_INPUT_PRODUCT_STRUCTURE_KINDS_DESCRIPTION,
    )
    purpose_contains: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description=_tool_docs.DIRECT_IFC_PRODUCT_FAMILY_LIST_FIRST_INPUT_PURPOSE_CONTAINS_DESCRIPTION,
    )
    page_size: int = Field(
        default=50,
        strict=True,
        ge=1,
        le=100,
        description=_tool_docs.DIRECT_IFC_PRODUCT_FAMILY_LIST_FIRST_INPUT_PAGE_SIZE_DESCRIPTION,
    )

    @field_validator("owner_scopes", "product_structure_kinds")
    @classmethod
    def require_unique_filters(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("product family filters must not contain duplicates")
        return value


class DirectIfcProductFamilyListFirstInput(IfcProductFamilyListFirstInput, _DirectProductFamilyInput):
    __doc__ = _tool_docs.DIRECT_IFC_PRODUCT_FAMILY_SCOPED_INPUT_DESCRIPTION


class IfcProductFamilyListCursorInput(DirectContract):
    cursor: str = Field(
        pattern=_CURSOR_PATTERN,
        max_length=16_384,
        description=(_tool_docs.DIRECT_IFC_PRODUCT_FAMILY_LIST_CURSOR_INPUT_CURSOR_DESCRIPTION),
    )


class DirectIfcProductFamilyListCursorInput(IfcProductFamilyListCursorInput, _DirectProductFamilyInput):
    __doc__ = _tool_docs.DIRECT_IFC_PRODUCT_FAMILY_SCOPED_INPUT_DESCRIPTION


DirectIfcProductFamilyListInput: TypeAlias = (
    DirectIfcProductFamilyListFirstInput | DirectIfcProductFamilyListCursorInput
)


class IfcProductFamilyRootTarget(DirectContract):
    target_kind: Literal["family_root"] = Field(
        description=_tool_docs.IFC_PRODUCT_FAMILY_ROOT_TARGET_TARGET_KIND_DESCRIPTION
    )
    family_alias: str = Field(
        pattern=_SAFE_REFERENCE_PATTERN,
        max_length=16_384,
        description=_tool_docs.IFC_PRODUCT_FAMILY_ROOT_TARGET_FAMILY_ALIAS_DESCRIPTION,
    )
    before_version_no: int | None = Field(
        default=None,
        strict=True,
        ge=1,
        description=(_tool_docs.IFC_PRODUCT_FAMILY_ROOT_TARGET_BEFORE_VERSION_NO_DESCRIPTION),
    )
    page_size: int = Field(
        default=20,
        strict=True,
        ge=1,
        le=100,
        description=_tool_docs.IFC_PRODUCT_FAMILY_ROOT_TARGET_PAGE_SIZE_DESCRIPTION,
    )


class IfcProductFamilyVersionTarget(DirectContract):
    target_kind: Literal["family_version"] = Field(
        description=_tool_docs.IFC_PRODUCT_FAMILY_VERSION_TARGET_TARGET_KIND_DESCRIPTION
    )
    family_version_alias: str = Field(
        pattern=_SAFE_REFERENCE_PATTERN,
        max_length=16_384,
        description=_tool_docs.IFC_PRODUCT_FAMILY_VERSION_TARGET_FAMILY_VERSION_ALIAS_DESCRIPTION,
    )


from CAD2BIM_backend.domain.authoring_draft import AuthoringDraftInfoTarget
from CAD2BIM_backend.domain.authoring_draft_content import FamilyDraftInfo

IfcProductFamilyInfoTarget: TypeAlias = IfcProductFamilyRootTarget | IfcProductFamilyVersionTarget | AuthoringDraftInfoTarget


class IfcProductFamilyInfoInput(DirectContract):
    target: IfcProductFamilyInfoTarget = Field(
        description=_tool_docs.DIRECT_IFC_PRODUCT_FAMILY_INFO_INPUT_TARGET_DESCRIPTION
    )


class DirectIfcProductFamilyInfoInput(IfcProductFamilyInfoInput, _DirectProductFamilyInput):
    __doc__ = _tool_docs.DIRECT_IFC_PRODUCT_FAMILY_SCOPED_INPUT_DESCRIPTION


class IfcProductFamilyVersionCard(DirectContract):
    family_version_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    version_no: int = Field(strict=True, ge=1)
    display_name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2048)
    product_structure_kind: IfcProductStructureKind
    created_at: datetime


class IfcProductFamilyCard(DirectContract):
    family_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    owner_scope: IfcProductFamilyOwnerScope
    display_name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2048)
    product_structure_kind: IfcProductStructureKind
    latest_family_version_alias: str = Field(
        pattern=_SAFE_REFERENCE_PATTERN,
        max_length=16_384,
        description=(_tool_docs.IFC_PRODUCT_FAMILY_CARD_LATEST_FAMILY_VERSION_ALIAS_DESCRIPTION),
    )
    latest_version_no: int = Field(strict=True, ge=1)
    latest_version_created_at: datetime
    created_at: datetime


class IfcProductFamilyRejection(DirectContract):
    __doc__ = _tool_docs.IFC_PRODUCT_FAMILY_REJECTION_DESCRIPTION

    family_version_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    version_no: int = Field(strict=True, ge=1)
    code: Literal["PRODUCT_FAMILY_CONTRACT_INVALID"]
    field_path: str
    reason: str


class IfcProductFamilyListResult(DirectContract):
    families: tuple[IfcProductFamilyCard, ...] = Field(max_length=100)
    rejections: tuple[IfcProductFamilyRejection, ...] = ()
    complete: bool
    next_cursor: str | None = Field(default=None, pattern=_CURSOR_PATTERN, max_length=16_384)


class IfcProductFamilyRootInfoResult(DirectContract):
    target_kind: Literal["family_root"]
    family: IfcProductFamilyCard
    versions: tuple[IfcProductFamilyVersionCard, ...] = Field(min_length=1, max_length=100)
    versions_complete: bool
    next_before_version_no: int | None = Field(default=None, strict=True, ge=1)


class IfcProductFamilyGeometryBindingResult(DirectContract):
    binding_key: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=64)
    geometry_resource_version_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    expected_resource_kind: IfcProductFamilyGeometryResourceKind
    geometry_purpose: IfcProductFamilyGeometryPurpose = Field(
        description=_tool_docs.IFC_PRODUCT_FAMILY_GEOMETRY_PURPOSE_DESCRIPTION
    )


class IfcProductFamilyAssetResult(DirectContract):
    asset_role: IfcProductFamilyAssetRole
    asset_format: str = Field(min_length=1, max_length=64)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(strict=True, gt=0)


from CAD2BIM_mcp.direct.contract_base import OperationNavigation


class IfcProductFamilyVersionInfoResult(DirectContract):
    publication: OperationNavigation | None = None
    standard_model_available: bool = False
    standard_public_parameters_json: str | None = None
    target_kind: Literal["family_version"]
    family_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    family_version_alias: str = Field(pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384)
    parent_family_version_alias: str | None = Field(
        default=None, pattern=_SAFE_REFERENCE_PATTERN, max_length=16_384
    )
    owner_scope: IfcProductFamilyOwnerScope
    version_no: int = Field(strict=True, ge=1)
    display_name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2048)
    usage_instructions: str | None = Field(
        default=None,
        max_length=8_192,
        description=_tool_docs.VERSION_USAGE_INSTRUCTIONS_INFO_DESCRIPTION,
    )
    product_structure_kind: IfcProductStructureKind
    family_contract_json: str = Field(
        min_length=2,
        max_length=MAX_PRODUCT_FAMILY_CONTRACT_JSON_BYTES,
        description=_tool_docs.IFC_PRODUCT_FAMILY_VERSION_INFO_RESULT_FAMILY_CONTRACT_JSON_DESCRIPTION,
    )
    program_entry: Literal["main.py:realize"]
    geometry_bindings: tuple[IfcProductFamilyGeometryBindingResult, ...] = Field(
        min_length=1,
        max_length=MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS,
    )
    assets: tuple[IfcProductFamilyAssetResult, ...] = Field(
        min_length=1,
        max_length=MAX_PRODUCT_FAMILY_VERSION_ASSETS,
    )
    created_at: datetime


class IfcProductFamilyInfoResult(DirectContract):
    info: IfcProductFamilyRootInfoResult | IfcProductFamilyVersionInfoResult | FamilyDraftInfo = Field(
        description=_tool_docs.IFC_PRODUCT_FAMILY_INFO_RESULT_INFO_DESCRIPTION
    )


PRODUCT_FAMILY_LIST_INPUT_ADAPTER = TypeAdapter(DirectIfcProductFamilyListInput)


def validate_product_family_list_input(value: object) -> DirectIfcProductFamilyListInput:
    return PRODUCT_FAMILY_LIST_INPUT_ADAPTER.validate_python(value)


__all__ = [name for name in globals() if name.startswith(("DirectIfc", "IfcProduct"))] + [
    "PRODUCT_FAMILY_LIST_INPUT_ADAPTER",
    "validate_product_family_list_input",
]
