"""Closed Direct Provider contracts for IFC spatial structure."""

from __future__ import annotations

import unicodedata
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import ConfigDict, Field, TypeAdapter, field_validator, model_validator

from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    IfcSpatialStructureListQuery,
    MAX_IFC_SPATIAL_BLOCKER_SAMPLES,
    MAX_IFC_SPATIAL_NODES,
    MAX_IFC_SPATIAL_OPERATIONS,
    MAX_IFC_SPATIAL_PAGE_SIZE,
)
from CAD2BIM_mcp.direct.contract_base import DirectContract
from CAD2BIM_mcp.tool_docs import ifc_spatial as _tool_docs


IfcSpatialClassInput: TypeAlias = Literal["site", "building", "building_storey", "space"]
IfcCompositionTypeInput: TypeAlias = Literal["COMPLEX", "ELEMENT", "PARTIAL"]
IfcSpacePredefinedTypeInput: TypeAlias = Literal[
    "BERTH",
    "EXTERNAL",
    "GFA",
    "INTERNAL",
    "PARKING",
    "SPACE",
    "USERDEFINED",
    "NOTDEFINED",
]
IfcSpatialClearFieldInput: TypeAlias = Literal[
    "description",
    "object_type",
    "long_name",
    "ref_latitude",
    "ref_longitude",
    "ref_elevation",
    "predefined_type",
    "elevation_with_flooring",
]
IfcSpatialActionResult: TypeAlias = Literal[
    "create",
    "update",
    "reparent_preserve_world",
    "set_project_world_placement",
    "delete_if_empty",
]

_DIRECT_ALIAS_PATTERN = r"^direct_[0-9]{3,}$"
_GLOBAL_ID_PATTERN = r"^[0-9A-Za-z_$]{22}$"
_CLIENT_REF_PATTERN = r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"
_CURSOR_PATTERN = r"^cur\..+$"
IfcGlobalIdInput: TypeAlias = Annotated[str, Field(pattern=_GLOBAL_ID_PATTERN)]


def _remove_omission_defaults(schema: dict[str, Any]) -> None:
    for property_schema in schema.get("properties", {}).values():
        if property_schema.get("default") is None:
            property_schema.pop("default")


def _normalize_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("value must not contain control characters")
    return value.strip()


class IfcProjectWorldPlacementInput(DirectContract):
    origin: tuple[float, float, float] = Field(
        description=_tool_docs.IFC_PROJECT_WORLD_PLACEMENT_INPUT_ORIGIN_DESCRIPTION
    )
    axis: tuple[float, float, float] = Field(
        description=_tool_docs.IFC_PROJECT_WORLD_PLACEMENT_INPUT_AXIS_DESCRIPTION
    )
    ref_direction: tuple[float, float, float] = Field(
        description=_tool_docs.IFC_PROJECT_WORLD_PLACEMENT_INPUT_REF_DIRECTION_DESCRIPTION
    )


class IfcProjectParentInput(DirectContract):
    kind: Literal["project"] = Field(
        default="project",
        description=_tool_docs.IFC_PROJECT_PARENT_INPUT_KIND_DESCRIPTION,
    )


class IfcExistingSpatialParentInput(DirectContract):
    kind: Literal["existing"] = Field(
        default="existing",
        description=_tool_docs.IFC_EXISTING_SPATIAL_PARENT_INPUT_KIND_DESCRIPTION,
    )
    global_id: IfcGlobalIdInput = Field(
        description=_tool_docs.IFC_EXISTING_SPATIAL_PARENT_INPUT_GLOBAL_ID_DESCRIPTION,
    )


class IfcClientRefSpatialParentInput(DirectContract):
    kind: Literal["client_ref"] = Field(
        default="client_ref",
        description=_tool_docs.IFC_CLIENT_REF_SPATIAL_PARENT_INPUT_KIND_DESCRIPTION,
    )
    client_ref: str = Field(
        pattern=_CLIENT_REF_PATTERN,
        description=_tool_docs.IFC_CLIENT_REF_SPATIAL_PARENT_INPUT_CLIENT_REF_DESCRIPTION,
    )


IfcSpatialParentInput: TypeAlias = Annotated[
    IfcProjectParentInput | IfcExistingSpatialParentInput | IfcClientRefSpatialParentInput,
    Field(discriminator="kind"),
]


class _CreateIfcSpatialOperationInput(DirectContract):
    action: Literal["create"] = "create"
    client_ref: str = Field(
        pattern=_CLIENT_REF_PATTERN,
        description=_tool_docs.CREATE_IFC_SPATIAL_OPERATION_INPUT_CLIENT_REF_DESCRIPTION,
    )
    parent: IfcSpatialParentInput
    project_world_placement: IfcProjectWorldPlacementInput
    name: str = Field(min_length=1, max_length=255)
    composition_type: IfcCompositionTypeInput
    description: str | None = Field(default=None, max_length=4096)
    object_type: str | None = Field(default=None, min_length=1, max_length=255)
    long_name: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("name", "description", "object_type", "long_name", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return _normalize_text(value)


class CreateIfcSiteOperationInput(_CreateIfcSpatialOperationInput):
    spatial_class: Literal["site"] = "site"
    ref_latitude: float | None = Field(
        default=None,
        ge=-90.0,
        le=90.0,
        description=_tool_docs.CREATE_IFC_SITE_OPERATION_INPUT_REF_LATITUDE_DESCRIPTION,
    )
    ref_longitude: float | None = Field(
        default=None,
        ge=-180.0,
        le=180.0,
        description=_tool_docs.CREATE_IFC_SITE_OPERATION_INPUT_REF_LONGITUDE_DESCRIPTION,
    )
    ref_elevation: float | None = Field(
        default=None,
        description=_tool_docs.CREATE_IFC_SITE_OPERATION_INPUT_REF_ELEVATION_DESCRIPTION,
    )


class CreateIfcBuildingOperationInput(_CreateIfcSpatialOperationInput):
    spatial_class: Literal["building"] = "building"


class CreateIfcBuildingStoreyOperationInput(_CreateIfcSpatialOperationInput):
    spatial_class: Literal["building_storey"] = "building_storey"


class CreateIfcSpaceOperationInput(_CreateIfcSpatialOperationInput):
    spatial_class: Literal["space"] = "space"
    predefined_type: IfcSpacePredefinedTypeInput | None = Field(
        default=None,
        description=_tool_docs.CREATE_IFC_SPACE_OPERATION_INPUT_PREDEFINED_TYPE_DESCRIPTION,
    )
    elevation_with_flooring: float | None = Field(
        default=None,
        description=(
            _tool_docs.CREATE_IFC_SPACE_OPERATION_INPUT_ELEVATION_WITH_FLOORING_DESCRIPTION
        ),
    )


CreateIfcSpatialOperationInput: TypeAlias = Annotated[
    CreateIfcSiteOperationInput
    | CreateIfcBuildingOperationInput
    | CreateIfcBuildingStoreyOperationInput
    | CreateIfcSpaceOperationInput,
    Field(discriminator="spatial_class"),
]


class IfcSpatialPatchInput(DirectContract):
    __doc__ = _tool_docs.IFC_SPATIAL_PATCH_INPUT_DESCRIPTION

    model_config = ConfigDict(json_schema_extra=_remove_omission_defaults)

    name: str = Field(default=None, min_length=1, max_length=255)
    description: str = Field(default=None, max_length=4096)
    object_type: str = Field(default=None, min_length=1, max_length=255)
    long_name: str = Field(default=None, min_length=1, max_length=255)
    composition_type: IfcCompositionTypeInput = Field(default=None)
    ref_latitude: float = Field(default=None, ge=-90.0, le=90.0)
    ref_longitude: float = Field(default=None, ge=-180.0, le=180.0)
    ref_elevation: float = Field(default=None)
    predefined_type: IfcSpacePredefinedTypeInput = Field(default=None)
    elevation_with_flooring: float = Field(default=None)

    @field_validator("name", "description", "object_type", "long_name", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return _normalize_text(value)


class UpdateIfcSpatialOperationInput(DirectContract):
    action: Literal["update"] = "update"
    target_global_id: IfcGlobalIdInput
    patch: IfcSpatialPatchInput
    clear_fields: tuple[IfcSpatialClearFieldInput, ...] = Field(default=(), max_length=8)


class ReparentIfcSpatialOperationInput(DirectContract):
    action: Literal["reparent_preserve_world"] = "reparent_preserve_world"
    target_global_id: IfcGlobalIdInput
    new_parent: IfcSpatialParentInput


class SetIfcSpatialPlacementOperationInput(DirectContract):
    action: Literal["set_project_world_placement"] = "set_project_world_placement"
    target_global_id: IfcGlobalIdInput
    project_world_placement: IfcProjectWorldPlacementInput
    preserve_descendant_project_world: bool = Field(
        default=False,
        description=(
            _tool_docs.SET_IFC_SPATIAL_PLACEMENT_OPERATION_INPUT_PRESERVE_DESCENDANT_PROJECT_WORLD_DESCRIPTION
        ),
    )


class DeleteIfcSpatialOperationInput(DirectContract):
    action: Literal["delete_if_empty"] = "delete_if_empty"
    target_global_id: IfcGlobalIdInput


IfcSpatialOperationInput: TypeAlias = Annotated[
    CreateIfcSpatialOperationInput
    | UpdateIfcSpatialOperationInput
    | ReparentIfcSpatialOperationInput
    | SetIfcSpatialPlacementOperationInput
    | DeleteIfcSpatialOperationInput,
    Field(discriminator="action"),
]


class IfcSpatialStructureListQueryInput(DirectContract):
    spatial_classes: tuple[IfcSpatialClassInput, ...] = Field(
        default=(),
        max_length=4,
        description=_tool_docs.IFC_SPATIAL_STRUCTURE_LIST_QUERY_INPUT_SPATIAL_CLASSES_DESCRIPTION,
    )
    global_ids: tuple[IfcGlobalIdInput, ...] = Field(
        default=(),
        max_length=256,
        description=_tool_docs.IFC_SPATIAL_STRUCTURE_LIST_QUERY_INPUT_GLOBAL_IDS_DESCRIPTION,
    )
    parent_global_id: str | None = Field(
        default=None,
        pattern=_GLOBAL_ID_PATTERN,
        description=_tool_docs.IFC_SPATIAL_STRUCTURE_LIST_QUERY_INPUT_PARENT_GLOBAL_ID_DESCRIPTION,
    )
    page_size: int = Field(
        default=64,
        strict=True,
        ge=1,
        le=MAX_IFC_SPATIAL_PAGE_SIZE,
        description=_tool_docs.IFC_SPATIAL_STRUCTURE_LIST_QUERY_INPUT_PAGE_SIZE_DESCRIPTION,
    )

    @field_validator("spatial_classes")
    @classmethod
    def require_unique_classes(
        cls, values: tuple[IfcSpatialClassInput, ...]
    ) -> tuple[IfcSpatialClassInput, ...]:
        if len(values) != len(set(values)):
            raise ValueError("spatial_classes must not contain duplicates")
        return values


class DirectIfcSpatialListFirstPageInput(DirectContract):
    direct_session_alias: str = Field(pattern=_DIRECT_ALIAS_PATTERN, max_length=32)
    spatial_classes: tuple[IfcSpatialClassInput, ...] = Field(
        default=(),
        max_length=4,
        description=_tool_docs.DIRECT_IFC_SPATIAL_LIST_FIRST_PAGE_INPUT_SPATIAL_CLASSES_DESCRIPTION,
    )
    global_ids: tuple[IfcGlobalIdInput, ...] = Field(
        default=(),
        max_length=256,
        description=_tool_docs.DIRECT_IFC_SPATIAL_LIST_FIRST_PAGE_INPUT_GLOBAL_IDS_DESCRIPTION,
    )
    parent_global_id: str | None = Field(
        default=None,
        pattern=_GLOBAL_ID_PATTERN,
        description=_tool_docs.IFC_SPATIAL_STRUCTURE_LIST_QUERY_INPUT_PARENT_GLOBAL_ID_DESCRIPTION,
    )
    page_size: int = Field(
        default=64,
        strict=True,
        ge=1,
        le=MAX_IFC_SPATIAL_PAGE_SIZE,
        description=_tool_docs.DIRECT_IFC_SPATIAL_LIST_FIRST_PAGE_INPUT_PAGE_SIZE_DESCRIPTION,
    )

    @field_validator("spatial_classes")
    @classmethod
    def require_unique_classes(
        cls, values: tuple[IfcSpatialClassInput, ...]
    ) -> tuple[IfcSpatialClassInput, ...]:
        if len(values) != len(set(values)):
            raise ValueError("spatial_classes must not contain duplicates")
        return values


class DirectIfcSpatialListContinuationInput(DirectContract):
    direct_session_alias: str = Field(pattern=_DIRECT_ALIAS_PATTERN, max_length=32)
    cursor: str = Field(
        pattern=_CURSOR_PATTERN,
        max_length=16_384,
        description=_tool_docs.DIRECT_IFC_SPATIAL_LIST_CONTINUATION_INPUT_CURSOR_DESCRIPTION,
    )


DirectIfcSpatialListInput: TypeAlias = (
    DirectIfcSpatialListFirstPageInput | DirectIfcSpatialListContinuationInput
)
_DIRECT_SPATIAL_LIST_INPUT = TypeAdapter(DirectIfcSpatialListInput)


class IfcSpatialBlockerSummaryResult(DirectContract):
    child_spatial_count: int = Field(ge=0)
    contained_element_count: int = Field(ge=0)
    referenced_element_count: int = Field(ge=0)
    servicing_system_count: int = Field(ge=0)
    boundary_count: int = Field(ge=0)
    covering_count: int = Field(ge=0)
    interference_count: int = Field(ge=0)
    group_count: int = Field(ge=0)
    external_placement_count: int = Field(ge=0)
    other_business_reference_count: int = Field(ge=0)
    sample_global_ids: tuple[str, ...] = Field(max_length=MAX_IFC_SPATIAL_BLOCKER_SAMPLES)


class IfcSpatialNodeResult(DirectContract):
    global_id: IfcGlobalIdInput
    ifc_class: Literal["IfcSite", "IfcBuilding", "IfcBuildingStorey", "IfcSpace"]
    spatial_class: IfcSpatialClassInput
    name: str
    description: str | None
    object_type: str | None
    long_name: str | None
    composition_type: IfcCompositionTypeInput
    predefined_type: IfcSpacePredefinedTypeInput | None
    ref_latitude: float | None
    ref_longitude: float | None
    ref_elevation: float | None
    elevation_with_flooring: float | None
    parent_global_id: IfcGlobalIdInput
    parent_ifc_class: Literal[
        "IfcProject", "IfcSite", "IfcBuilding", "IfcBuildingStorey", "IfcSpace"
    ]
    project_world_placement: IfcProjectWorldPlacementInput
    blockers: IfcSpatialBlockerSummaryResult


class IfcSpatialAppliedResult(DirectContract):
    action: IfcSpatialActionResult
    client_ref: str | None = Field(pattern=_CLIENT_REF_PATTERN)
    target_global_id: IfcGlobalIdInput | None
    global_id: IfcGlobalIdInput


class IfcSpatialStructureApplyResult(DirectContract):
    operation_alias: str = Field(pattern=r"^op_[0-9]{3,}$", max_length=32)
    applied_count: int = Field(ge=1, le=MAX_IFC_SPATIAL_OPERATIONS)
    applied: tuple[IfcSpatialAppliedResult, ...] = Field(
        min_length=1,
        max_length=MAX_IFC_SPATIAL_OPERATIONS,
    )
    spatial_summary: tuple[IfcSpatialNodeResult, ...] = Field(max_length=MAX_IFC_SPATIAL_NODES)

    @model_validator(mode="after")
    def require_applied_count(self) -> "IfcSpatialStructureApplyResult":
        if self.applied_count != len(self.applied):
            raise ValueError("applied_count must equal the number of applied items")
        return self


class IfcSpatialStructureListResult(DirectContract):
    items: tuple[IfcSpatialNodeResult, ...] = Field(max_length=MAX_IFC_SPATIAL_PAGE_SIZE)
    returned_count: int = Field(ge=0, le=MAX_IFC_SPATIAL_PAGE_SIZE)
    next_cursor: str | None = Field(default=None, pattern=_CURSOR_PATTERN, max_length=16_384)

    @model_validator(mode="after")
    def require_returned_count(self) -> "IfcSpatialStructureListResult":
        if self.returned_count != len(self.items):
            raise ValueError("returned_count must equal the number of items")
        return self


def to_domain_spatial_apply_command(
    operations: tuple[IfcSpatialOperationInput, ...],
) -> ApplyIfcSpatialStructureCommand:
    return ApplyIfcSpatialStructureCommand.model_validate(
        {
            "operations": [
                operation.model_dump(mode="python", exclude_none=True) for operation in operations
            ]
        }
    )


def to_domain_spatial_list_query(
    query: IfcSpatialStructureListQueryInput | DirectIfcSpatialListFirstPageInput,
) -> IfcSpatialStructureListQuery:
    return IfcSpatialStructureListQuery.model_validate(
        query.model_dump(mode="python", exclude={"direct_session_alias"})
    )


def validate_direct_spatial_list_input(
    value: object,
) -> DirectIfcSpatialListFirstPageInput | DirectIfcSpatialListContinuationInput:
    return _DIRECT_SPATIAL_LIST_INPUT.validate_python(value)


def ifc_spatial_node_result(node: Any) -> IfcSpatialNodeResult:
    return IfcSpatialNodeResult.model_validate(node.model_dump(mode="python"))


def ifc_spatial_apply_result(result: Any) -> IfcSpatialStructureApplyResult:
    return IfcSpatialStructureApplyResult(
        operation_alias=result.operation_alias,
        applied_count=result.applied_count,
        applied=tuple(
            IfcSpatialAppliedResult(
                action=item.action,
                client_ref=item.client_ref,
                target_global_id=item.target_global_id,
                global_id=item.global_id,
            )
            for item in result.applied
        ),
        spatial_summary=tuple(ifc_spatial_node_result(item) for item in result.spatial_summary),
    )


def ifc_spatial_list_result(result: Any) -> IfcSpatialStructureListResult:
    return IfcSpatialStructureListResult(
        items=tuple(ifc_spatial_node_result(item) for item in result.items),
        returned_count=result.returned_count,
        next_cursor=result.next_cursor,
    )


__all__ = [
    name
    for name in globals()
    if name.startswith("Ifc")
    or name.startswith("Direct")
    or name.startswith("Create")
    or name.startswith("Update")
    or name.startswith("Reparent")
    or name.startswith("Set")
    or name.startswith("Delete")
    or name.startswith("to_domain")
    or name.startswith("validate_direct")
    or name.startswith("ifc_spatial")
]
