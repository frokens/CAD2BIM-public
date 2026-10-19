"""Closed contracts for the IFC project spatial structure.

The public coordinate frame is always project-world.  Mutable IfcOpenShell
entities, STEP identifiers, revision selectors, and durable repository
identities do not cross this boundary.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Annotated, ClassVar, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from CAD2BIM_backend.domain.ifc_model import EncodedIfcRevision, IfcRevisionInspection


IfcSpatialClass: TypeAlias = Literal["site", "building", "building_storey", "space"]
IfcCompositionType: TypeAlias = Literal["COMPLEX", "ELEMENT", "PARTIAL"]
IfcSpatialAggregateParentClass: TypeAlias = Literal[
    "project", "site", "building", "building_storey", "space"
]
IfcSpacePredefinedType: TypeAlias = Literal[
    "BERTH", "EXTERNAL", "GFA", "INTERNAL", "PARKING", "SPACE", "USERDEFINED", "NOTDEFINED"
]

MAX_IFC_SPATIAL_OPERATIONS = 256
MAX_IFC_SPATIAL_NODES = 4096
MAX_IFC_SPATIAL_PAGE_SIZE = 256
MAX_IFC_SPATIAL_BLOCKER_SAMPLES = 8
MAX_IFC_SPATIAL_IMPACT_ENTITIES = 200_000
MAX_IFC_SPATIAL_IMPACT_WORK_UNITS = 5_000_000

_GLOBAL_ID = re.compile(r"^[0-9A-Za-z_$]{22}$")
_CLIENT_REF = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


class IfcSpatialStructureError(Exception):
    code: ClassVar[str] = "IFC_SPATIAL_STRUCTURE_ERROR"


class IfcSpatialStructureBusinessError(IfcSpatialStructureError, ValueError):
    """Deterministic command or current-model precondition failure."""


class IfcSpatialTargetNotFoundError(IfcSpatialStructureBusinessError, LookupError):
    code = "IFC_SPATIAL_TARGET_NOT_FOUND"


class IfcSpatialClassMismatchError(IfcSpatialStructureBusinessError):
    code = "IFC_SPATIAL_CLASS_MISMATCH"


class IfcSpatialParentInvalidError(IfcSpatialStructureBusinessError):
    code = "IFC_SPATIAL_PARENT_INVALID"


class IfcSpatialCycleError(IfcSpatialStructureBusinessError):
    code = "IFC_SPATIAL_CYCLE"


class IfcSpatialReferenceInvalidError(IfcSpatialStructureBusinessError):
    code = "IFC_SPATIAL_REFERENCE_INVALID"


class IfcSpatialPlacementInvalidError(IfcSpatialStructureBusinessError):
    code = "IFC_SPATIAL_PLACEMENT_INVALID"


class IfcSpatialOperationConflictError(IfcSpatialStructureBusinessError):
    code = "IFC_SPATIAL_OPERATION_CONFLICT"


class IfcSpatialNotEmptyError(IfcSpatialStructureBusinessError):
    code = "IFC_SPATIAL_NOT_EMPTY"

    def __init__(self, global_id: str, blockers: "IfcSpatialBlockerSummary") -> None:
        self.global_id = global_id
        self.blockers = blockers
        super().__init__(f"spatial object {global_id!r} is not empty")


class IfcSpatialNoChangeError(IfcSpatialStructureBusinessError):
    code = "IFC_NO_CHANGES"


class IfcSpatialResourceLimitError(IfcSpatialStructureBusinessError):
    code = "IFC_SPATIAL_RESOURCE_LIMIT"


class IfcSpatialStructureIntegrityError(IfcSpatialStructureError, RuntimeError):
    code = "IFC_MODEL_INTEGRITY_ERROR"


class IfcSpatialStructureCompilationError(IfcSpatialStructureError, RuntimeError):
    code = "IFC_SPATIAL_STRUCTURE_COMPILATION_FAILED"


class IfcSpatialCursorError(IfcSpatialStructureError, ValueError):
    code = "CURSOR_INVALID"


class IfcSpatialCursorExpiredError(IfcSpatialCursorError):
    code = "CURSOR_EXPIRED"


def ifc_spatial_parent_child_violation(
    parent_class: IfcSpatialAggregateParentClass,
    parent_composition_type: IfcCompositionType | None,
    child_class: IfcSpatialClass,
    child_composition_type: IfcCompositionType,
) -> str | None:
    """Return the single domain-level aggregate rule violation, if any."""

    if parent_class == "project":
        if child_class != "site":
            return "only IfcSite may be aggregated directly by IfcProject"
        if child_composition_type == "PARTIAL":
            return "a root IfcSite cannot be PARTIAL"
        return None

    if parent_class == child_class:
        if parent_composition_type != "COMPLEX" or child_composition_type != "PARTIAL":
            return "same-class aggregation requires a COMPLEX parent and PARTIAL child"
        return None

    allowed = {
        ("site", "building"),
        ("site", "space"),
        ("building", "building_storey"),
        ("building_storey", "space"),
    }
    if (parent_class, child_class) not in allowed:
        return f"{parent_class} cannot aggregate {child_class}"
    if parent_composition_type == "COMPLEX":
        return "a COMPLEX spatial object may aggregate only PARTIAL objects of its class"
    if child_composition_type == "PARTIAL":
        return "a PARTIAL spatial object must be part of a same-class COMPLEX"
    return None


class IfcSpatialContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _label(value: object) -> object:
    if not isinstance(value, str):
        return value
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("value must not contain control characters")
    return value.strip()


def _require_global_id(value: str) -> str:
    if _GLOBAL_ID.fullmatch(value) is None:
        raise ValueError("value must be a valid IFC GlobalId")
    return value


class IfcProjectWorldPlacement(IfcSpatialContract):
    origin: tuple[float, float, float]
    axis: tuple[float, float, float]
    ref_direction: tuple[float, float, float]

    @model_validator(mode="after")
    def require_rigid_right_handed_frame(self) -> "IfcProjectWorldPlacement":
        values = (*self.origin, *self.axis, *self.ref_direction)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("placement values must be finite")
        axis_norm = math.sqrt(sum(value * value for value in self.axis))
        ref_norm = math.sqrt(sum(value * value for value in self.ref_direction))
        tolerance = 1e-9
        if abs(axis_norm - 1.0) > tolerance or abs(ref_norm - 1.0) > tolerance:
            raise ValueError("axis and ref_direction must be unit vectors")
        dot = sum(a * b for a, b in zip(self.axis, self.ref_direction, strict=True))
        if abs(dot) > tolerance:
            raise ValueError("axis and ref_direction must be orthogonal")
        # Y is deliberately derived as Z cross X.  This establishes a rigid,
        # non-mirrored right-handed frame without accepting an ambiguous Y.
        cross = (
            self.axis[1] * self.ref_direction[2] - self.axis[2] * self.ref_direction[1],
            self.axis[2] * self.ref_direction[0] - self.axis[0] * self.ref_direction[2],
            self.axis[0] * self.ref_direction[1] - self.axis[1] * self.ref_direction[0],
        )
        if abs(math.sqrt(sum(value * value for value in cross)) - 1.0) > tolerance:
            raise ValueError("placement must define a non-degenerate right-handed frame")
        return self


class IfcProjectParentReference(IfcSpatialContract):
    kind: Literal["project"] = "project"


class IfcExistingSpatialParentReference(IfcSpatialContract):
    kind: Literal["existing"] = "existing"
    global_id: str

    _valid_global_id = field_validator("global_id")(_require_global_id)


class IfcClientRefSpatialParentReference(IfcSpatialContract):
    kind: Literal["client_ref"] = "client_ref"
    client_ref: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


IfcSpatialParentReference: TypeAlias = Annotated[
    IfcProjectParentReference
    | IfcExistingSpatialParentReference
    | IfcClientRefSpatialParentReference,
    Field(discriminator="kind"),
]


class IfcSpatialAttributes(IfcSpatialContract):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    object_type: str | None = Field(default=None, min_length=1, max_length=255)
    long_name: str | None = Field(default=None, min_length=1, max_length=255)
    composition_type: IfcCompositionType
    ref_latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    ref_longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    ref_elevation: float | None = None
    predefined_type: IfcSpacePredefinedType | None = None
    elevation_with_flooring: float | None = None

    @field_validator("name", "description", "object_type", "long_name", mode="before")
    @classmethod
    def normalize_labels(cls, value: object) -> object:
        return _label(value)


class IfcSpatialPatch(IfcSpatialContract):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    object_type: str | None = Field(default=None, min_length=1, max_length=255)
    long_name: str | None = Field(default=None, min_length=1, max_length=255)
    composition_type: IfcCompositionType | None = None
    ref_latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    ref_longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    ref_elevation: float | None = None
    predefined_type: IfcSpacePredefinedType | None = None
    elevation_with_flooring: float | None = None

    @field_validator("name", "description", "object_type", "long_name", mode="before")
    @classmethod
    def normalize_labels(cls, value: object) -> object:
        return _label(value)

    @model_validator(mode="after")
    def reject_null_patch_values(self) -> "IfcSpatialPatch":
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null; use clear_fields when allowed")
        return self


IfcSpatialClearField: TypeAlias = Literal[
    "description", "object_type", "long_name", "ref_latitude", "ref_longitude",
    "ref_elevation", "predefined_type", "elevation_with_flooring"
]


class CreateIfcSpatialOperation(IfcSpatialContract):
    action: Literal["create"] = "create"
    client_ref: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
    spatial_class: IfcSpatialClass
    parent: IfcSpatialParentReference
    project_world_placement: IfcProjectWorldPlacement
    name: str = Field(min_length=1, max_length=255)
    composition_type: IfcCompositionType
    description: str | None = Field(default=None, max_length=4096)
    object_type: str | None = Field(default=None, min_length=1, max_length=255)
    long_name: str | None = Field(default=None, min_length=1, max_length=255)
    ref_latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    ref_longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    ref_elevation: float | None = None
    predefined_type: IfcSpacePredefinedType | None = None
    elevation_with_flooring: float | None = None

    @field_validator("name", "description", "object_type", "long_name", mode="before")
    @classmethod
    def normalize_labels(cls, value: object) -> object:
        return _label(value)

    def attributes(self) -> IfcSpatialAttributes:
        return IfcSpatialAttributes(**self.model_dump(exclude={"action", "client_ref", "spatial_class", "parent", "project_world_placement"}))


class UpdateIfcSpatialOperation(IfcSpatialContract):
    action: Literal["update"] = "update"
    target_global_id: str
    patch: IfcSpatialPatch
    clear_fields: tuple[IfcSpatialClearField, ...] = ()

    _valid_global_id = field_validator("target_global_id")(_require_global_id)

    @model_validator(mode="after")
    def require_unambiguous_change(self) -> "UpdateIfcSpatialOperation":
        if not self.patch.model_fields_set and not self.clear_fields:
            raise ValueError("update must contain an explicit patch or clear_fields")
        if len(set(self.clear_fields)) != len(self.clear_fields):
            raise ValueError("clear_fields must not contain duplicates")
        overlap = self.patch.model_fields_set.intersection(self.clear_fields)
        if overlap:
            raise ValueError("a field cannot be patched and cleared in the same operation")
        return self


class ReparentIfcSpatialOperation(IfcSpatialContract):
    action: Literal["reparent_preserve_world"] = "reparent_preserve_world"
    target_global_id: str
    new_parent: IfcSpatialParentReference

    _valid_global_id = field_validator("target_global_id")(_require_global_id)


class SetIfcSpatialPlacementOperation(IfcSpatialContract):
    action: Literal["set_project_world_placement"] = "set_project_world_placement"
    target_global_id: str
    project_world_placement: IfcProjectWorldPlacement
    preserve_descendant_project_world: bool = False

    _valid_global_id = field_validator("target_global_id")(_require_global_id)


class DeleteIfcSpatialOperation(IfcSpatialContract):
    action: Literal["delete_if_empty"] = "delete_if_empty"
    target_global_id: str

    _valid_global_id = field_validator("target_global_id")(_require_global_id)


IfcSpatialOperation: TypeAlias = Annotated[
    CreateIfcSpatialOperation
    | UpdateIfcSpatialOperation
    | ReparentIfcSpatialOperation
    | SetIfcSpatialPlacementOperation
    | DeleteIfcSpatialOperation,
    Field(discriminator="action"),
]


class ApplyIfcSpatialStructureCommand(IfcSpatialContract):
    operations: tuple[IfcSpatialOperation, ...] = Field(
        min_length=1, max_length=MAX_IFC_SPATIAL_OPERATIONS
    )

    @model_validator(mode="after")
    def require_unique_request_local_identity(self) -> "ApplyIfcSpatialStructureCommand":
        refs = [op.client_ref for op in self.operations if isinstance(op, CreateIfcSpatialOperation)]
        if len(refs) != len(set(refs)):
            raise ValueError("create client_ref values must be unique within the command")
        target_actions: dict[str, set[str]] = {}
        for operation in self.operations:
            target = getattr(operation, "target_global_id", None)
            if target is None:
                continue
            actions = target_actions.setdefault(target, set())
            if operation.action in actions:
                raise ValueError("the same target cannot repeat an action")
            actions.add(operation.action)
        for actions in target_actions.values():
            if "delete_if_empty" in actions and len(actions) > 1:
                raise ValueError("delete_if_empty conflicts with every other action on its target")
        return self

    def operation_request_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_none=True)


class IfcSpatialBlockerSummary(IfcSpatialContract):
    child_spatial_count: int = Field(default=0, ge=0)
    contained_element_count: int = Field(default=0, ge=0)
    referenced_element_count: int = Field(default=0, ge=0)
    servicing_system_count: int = Field(default=0, ge=0)
    boundary_count: int = Field(default=0, ge=0)
    covering_count: int = Field(default=0, ge=0)
    interference_count: int = Field(default=0, ge=0)
    group_count: int = Field(default=0, ge=0)
    external_placement_count: int = Field(default=0, ge=0)
    other_business_reference_count: int = Field(default=0, ge=0)
    sample_global_ids: tuple[str, ...] = Field(
        default=(), max_length=MAX_IFC_SPATIAL_BLOCKER_SAMPLES
    )

    @property
    def total_count(self) -> int:
        return sum(
            getattr(self, name)
            for name in type(self).model_fields
            if name.endswith("_count")
        )


class IfcSpatialNodeSummary(IfcSpatialContract):
    global_id: str
    ifc_class: Literal["IfcSite", "IfcBuilding", "IfcBuildingStorey", "IfcSpace"]
    spatial_class: IfcSpatialClass
    name: str
    description: str | None = None
    object_type: str | None = None
    long_name: str | None = None
    composition_type: IfcCompositionType
    predefined_type: IfcSpacePredefinedType | None = None
    ref_latitude: float | None = None
    ref_longitude: float | None = None
    ref_elevation: float | None = None
    elevation_with_flooring: float | None = None
    parent_global_id: str
    parent_ifc_class: Literal[
        "IfcProject", "IfcSite", "IfcBuilding", "IfcBuildingStorey", "IfcSpace"
    ]
    project_world_placement: IfcProjectWorldPlacement
    blockers: IfcSpatialBlockerSummary

    _valid_ids = field_validator("global_id", "parent_global_id")(_require_global_id)

    @property
    def child_spatial_count(self) -> int:
        return self.blockers.child_spatial_count

    @property
    def contained_element_count(self) -> int:
        return self.blockers.contained_element_count


class IfcSpatialStructureCatalog(IfcSpatialContract):
    project_global_id: str
    items: tuple[IfcSpatialNodeSummary, ...] = Field(
        default=(), max_length=MAX_IFC_SPATIAL_NODES
    )

    _valid_project_id = field_validator("project_global_id")(_require_global_id)

    @model_validator(mode="after")
    def require_unique_sorted_items(self) -> "IfcSpatialStructureCatalog":
        keys = tuple((item.ifc_class, item.global_id) for item in self.items)
        if len({item.global_id for item in self.items}) != len(self.items):
            raise ValueError("spatial GlobalId values must be unique")
        if keys != tuple(sorted(keys)):
            raise ValueError("spatial items must be sorted by IFC class and GlobalId")
        return self


class IfcSpatialStructureListQuery(IfcSpatialContract):
    spatial_classes: tuple[IfcSpatialClass, ...] = ()
    global_ids: tuple[str, ...] = ()
    parent_global_id: str | None = None
    page_size: int = Field(default=64, ge=1, le=MAX_IFC_SPATIAL_PAGE_SIZE)
    after: tuple[str, str] | None = None

    @field_validator("global_ids")
    @classmethod
    def validate_global_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _require_global_id(value)
        if len(values) != len(set(values)):
            raise ValueError("global_ids must not contain duplicates")
        return values

    @field_validator("parent_global_id")
    @classmethod
    def validate_parent_id(cls, value: str | None) -> str | None:
        return None if value is None else _require_global_id(value)


class IfcSpatialStructurePage(IfcSpatialContract):
    items: tuple[IfcSpatialNodeSummary, ...]
    returned_count: int = Field(ge=0)
    next_after: tuple[str, str] | None = None


class IfcSpatialCreatedBinding(IfcSpatialContract):
    """One request-local create reference resolved to its durable IFC identity."""

    client_ref: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
    global_id: str

    _valid_global_id = field_validator("global_id")(_require_global_id)


class IfcSpatialStructureCompilation(IfcSpatialContract):
    """Closed compiler output; Application never guesses request-local identities."""

    candidate: EncodedIfcRevision
    created: tuple[IfcSpatialCreatedBinding, ...] = ()
    affected_global_ids: tuple[str, ...] = ()

    @field_validator("affected_global_ids")
    @classmethod
    def validate_affected_global_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _require_global_id(value)
        if values != tuple(sorted(set(values))):
            raise ValueError("affected_global_ids must be unique and sorted")
        return values

    @property
    def content(self) -> bytes:
        """Compatibility projection for pure compiler callers."""

        return self.candidate.content

    @property
    def inspection(self) -> IfcRevisionInspection:
        """Compatibility projection for the shared revision writer contract."""

        return self.candidate.inspection


IfcSpatialAppliedAction: TypeAlias = Literal[
    "create",
    "update",
    "reparent_preserve_world",
    "set_project_world_placement",
    "delete_if_empty",
]


class IfcSpatialAppliedItem(IfcSpatialContract):
    action: IfcSpatialAppliedAction
    global_id: str
    client_ref: str | None = Field(
        default=None, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"
    )
    target_global_id: str | None = None

    _valid_global_id = field_validator("global_id")(_require_global_id)

    @field_validator("target_global_id")
    @classmethod
    def validate_target_global_id(cls, value: str | None) -> str | None:
        return None if value is None else _require_global_id(value)

    @model_validator(mode="after")
    def validate_action_identity(self) -> "IfcSpatialAppliedItem":
        if self.action == "create":
            if self.client_ref is None or self.target_global_id is not None:
                raise ValueError("create result requires client_ref and no target_global_id")
        elif self.client_ref is not None or self.target_global_id != self.global_id:
            raise ValueError(
                "non-create result requires target_global_id equal to global_id and no client_ref"
            )
        return self


class IfcSpatialStructureApplyResult(IfcSpatialContract):
    operation_alias: str = Field(pattern=r"^op_[0-9]{3,}$", max_length=32)
    applied_count: int = Field(ge=1, le=MAX_IFC_SPATIAL_OPERATIONS)
    applied: tuple[IfcSpatialAppliedItem, ...]
    spatial_summary: tuple[IfcSpatialNodeSummary, ...] = Field(
        default=(), max_length=MAX_IFC_SPATIAL_NODES
    )

    @model_validator(mode="after")
    def validate_applied_count(self) -> "IfcSpatialStructureApplyResult":
        if self.applied_count != len(self.applied):
            raise ValueError("applied_count must equal the number of applied items")
        return self


class IfcSpatialStructureListResult(IfcSpatialContract):
    items: tuple[IfcSpatialNodeSummary, ...]
    returned_count: int = Field(ge=0, le=MAX_IFC_SPATIAL_PAGE_SIZE)
    next_cursor: str | None = Field(
        default=None,
        pattern=r"^cur\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$",
        max_length=16_384,
    )

    @model_validator(mode="after")
    def validate_returned_count(self) -> "IfcSpatialStructureListResult":
        if self.returned_count != len(self.items):
            raise ValueError("returned_count must equal the number of items")
        return self


def project_spatial_catalog(
    catalog: IfcSpatialStructureCatalog,
    query: IfcSpatialStructureListQuery,
) -> IfcSpatialStructurePage:
    """Apply bounded AND filters; cursor signing remains an Application concern."""

    classes = set(query.spatial_classes)
    ids = set(query.global_ids)
    filtered = [
        item
        for item in catalog.items
        if (not classes or item.spatial_class in classes)
        and (not ids or item.global_id in ids)
        and (query.parent_global_id is None or item.parent_global_id == query.parent_global_id)
        and (query.after is None or (item.ifc_class, item.global_id) > query.after)
    ]
    page = tuple(filtered[: query.page_size])
    next_after = None
    if len(filtered) > len(page) and page:
        last = page[-1]
        next_after = (last.ifc_class, last.global_id)
    return IfcSpatialStructurePage(
        items=page,
        returned_count=len(page),
        next_after=next_after,
    )


__all__ = [name for name in globals() if name.startswith("Ifc") or name.startswith("Apply") or name.startswith("Create") or name.startswith("Update") or name.startswith("Reparent") or name.startswith("Set") or name.startswith("Delete") or name.startswith("MAX_") or name in {"ifc_spatial_parent_child_violation", "project_spatial_catalog"}]
