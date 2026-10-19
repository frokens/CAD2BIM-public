"""Closed domain contracts for project geometry basis and IFC contexts.

The contracts express business choices only.  STEP identifiers, mutable
IfcOpenShell entities, Artifact identities, and revision selectors are kept out
of this boundary.
"""

from __future__ import annotations

import unicodedata
from typing import Annotated, ClassVar, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


IfcSIPrefix: TypeAlias = Literal[
    "ATTO",
    "CENTI",
    "DECA",
    "DECI",
    "EXA",
    "FEMTO",
    "GIGA",
    "HECTO",
    "KILO",
    "MEGA",
    "MICRO",
    "MILLI",
    "NANO",
    "PETA",
    "PICO",
    "TERA",
]
IfcContextType: TypeAlias = Literal["Model", "Plan"]
IfcTargetView: TypeAlias = Literal[
    "ELEVATION_VIEW",
    "GRAPH_VIEW",
    "MODEL_VIEW",
    "PLAN_VIEW",
    "REFLECTED_PLAN_VIEW",
    "SECTION_VIEW",
    "SKETCH_VIEW",
    "USERDEFINED",
    "NOTDEFINED",
]
MAX_IFC_CONTEXT_OPERATIONS = 64
MAX_IFC_REPRESENTATION_CONTEXTS = 256


class IfcGeometryBasisError(Exception):
    """Base failure for the geometry-basis capability."""

    code: ClassVar[str] = "IFC_GEOMETRY_BASIS_ERROR"


class IfcGeometryBasisBusinessError(IfcGeometryBasisError, ValueError):
    """A deterministic command or model precondition failure."""


class IfcGeometryBasisNoChangeError(IfcGeometryBasisBusinessError):
    code = "IFC_NO_CHANGES"


class IfcRepresentationContextNotFoundError(IfcGeometryBasisBusinessError, LookupError):
    code = "IFC_REPRESENTATION_CONTEXT_NOT_FOUND"

    def __init__(self, context_identifier: str) -> None:
        self.context_identifier = context_identifier
        super().__init__(f"representation context {context_identifier!r} was not found")


class IfcRepresentationContextAlreadyExistsError(IfcGeometryBasisBusinessError):
    code = "IFC_REPRESENTATION_CONTEXT_ALREADY_EXISTS"

    def __init__(self, context_identifier: str) -> None:
        self.context_identifier = context_identifier
        super().__init__(f"representation context {context_identifier!r} already exists")


class IfcRepresentationContextIncompatibleError(IfcGeometryBasisBusinessError):
    code = "IFC_REPRESENTATION_CONTEXT_INCOMPATIBLE"

    def __init__(self, context_identifier: str, reason: str) -> None:
        self.context_identifier = context_identifier
        super().__init__(f"representation context {context_identifier!r} is incompatible: {reason}")


class IfcGeometryBasisIntegrityError(IfcGeometryBasisError, RuntimeError):
    """Exact IFC bytes violate a server-owned profile invariant."""

    code = "IFC_MODEL_INTEGRITY_ERROR"


class IfcGeometryBasisCompilationError(IfcGeometryBasisError, RuntimeError):
    """A compiler or generated-candidate postcondition failed."""

    code = "IFC_GEOMETRY_BASIS_COMPILATION_FAILED"


class IfcGeometryBasisContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _normalize_label(value: object) -> object:
    if not isinstance(value, str):
        return value
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("value must not contain control characters")
    return value.strip()


def _nonzero(values: tuple[float, ...]) -> bool:
    return any(value != 0.0 for value in values)


class IfcLengthUnit(IfcGeometryBasisContract):
    name: Literal["METRE"] = "METRE"
    prefix: IfcSIPrefix | None = None


class IfcPlaneAngleUnit(IfcGeometryBasisContract):
    name: Literal["RADIAN"] = "RADIAN"
    prefix: IfcSIPrefix | None = None


class IfcProjectUnitChange(IfcGeometryBasisContract):
    length_unit: IfcLengthUnit | None = None
    plane_angle_unit: IfcPlaneAngleUnit | None = None

    @model_validator(mode="after")
    def require_explicit_unit(self) -> "IfcProjectUnitChange":
        if self.length_unit is None and self.plane_angle_unit is None:
            raise ValueError("project_unit_change must specify at least one unit")
        return self


class IfcWorldCoordinateSystem2D(IfcGeometryBasisContract):
    kind: Literal["2d"] = "2d"
    origin: tuple[float, float]
    ref_direction: tuple[float, float]

    @field_validator("ref_direction")
    @classmethod
    def require_nonzero_ref_direction(cls, value: tuple[float, float]) -> tuple[float, float]:
        if not _nonzero(value):
            raise ValueError("ref_direction must be non-zero")
        return value


class IfcWorldCoordinateSystem3D(IfcGeometryBasisContract):
    kind: Literal["3d"] = "3d"
    origin: tuple[float, float, float]
    axis: tuple[float, float, float]
    ref_direction: tuple[float, float, float]

    @field_validator("axis", "ref_direction")
    @classmethod
    def require_nonzero_direction(
        cls, value: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        if not _nonzero(value):
            raise ValueError("direction must be non-zero")
        return value

    @model_validator(mode="after")
    def require_independent_directions(self) -> "IfcWorldCoordinateSystem3D":
        ax, ay, az = self.axis
        rx, ry, rz = self.ref_direction
        cross = (ay * rz - az * ry, az * rx - ax * rz, ax * ry - ay * rx)
        if not _nonzero(cross):
            raise ValueError("axis and ref_direction must not be parallel")
        return self


IfcWorldCoordinateSystem: TypeAlias = Annotated[
    IfcWorldCoordinateSystem2D | IfcWorldCoordinateSystem3D,
    Field(discriminator="kind"),
]


class IfcLocalCoordinateReferenceChange(IfcGeometryBasisContract):
    mode: Literal["local"] = "local"


class IfcProjectedCrsCoordinateReferenceChange(IfcGeometryBasisContract):
    mode: Literal["projected_crs"] = "projected_crs"
    source_context_identifier: str = Field(min_length=1, max_length=255)
    crs_name: str = Field(pattern=r"^EPSG:[1-9][0-9]*$", max_length=255)
    description: str | None = Field(default=None, max_length=1024)
    geodetic_datum: str | None = Field(default=None, max_length=255)
    vertical_datum: str | None = Field(default=None, max_length=255)
    map_projection: str | None = Field(default=None, max_length=255)
    map_zone: str | None = Field(default=None, max_length=255)
    map_unit: IfcLengthUnit | None = None
    eastings: float
    northings: float
    orthogonal_height: float
    x_axis_abscissa: float
    x_axis_ordinate: float
    scale: float = Field(gt=0.0)

    @field_validator(
        "source_context_identifier",
        "crs_name",
        "description",
        "geodetic_datum",
        "vertical_datum",
        "map_projection",
        "map_zone",
        mode="before",
    )
    @classmethod
    def normalize_labels(cls, value: object) -> object:
        return _normalize_label(value)

    @model_validator(mode="after")
    def require_nonzero_x_axis(self) -> "IfcProjectedCrsCoordinateReferenceChange":
        if self.x_axis_abscissa == 0.0 and self.x_axis_ordinate == 0.0:
            raise ValueError("map conversion x-axis direction must be non-zero")
        return self


IfcCoordinateReferenceChange: TypeAlias = Annotated[
    IfcLocalCoordinateReferenceChange | IfcProjectedCrsCoordinateReferenceChange,
    Field(discriminator="mode"),
]


class _IfcContextOperation(IfcGeometryBasisContract):
    context_identifier: str = Field(min_length=1, max_length=255)

    @field_validator("context_identifier", mode="before")
    @classmethod
    def normalize_context_identifier(cls, value: object) -> object:
        return _normalize_label(value)


class CreateParentContextOperation(_IfcContextOperation):
    action: Literal["create_parent_context"] = "create_parent_context"
    context_type: IfcContextType
    coordinate_space_dimension: Literal[2, 3]
    precision: float = Field(gt=0.0)
    world_coordinate_system: IfcWorldCoordinateSystem
    true_north: tuple[float, float] | None = None

    @field_validator("true_north")
    @classmethod
    def require_nonzero_true_north(
        cls, value: tuple[float, float] | None
    ) -> tuple[float, float] | None:
        if value is not None and not _nonzero(value):
            raise ValueError("true_north must be non-zero")
        return value

    @model_validator(mode="after")
    def require_compatible_dimension(self) -> "CreateParentContextOperation":
        if self.coordinate_space_dimension == 2 and self.world_coordinate_system.kind != "2d":
            raise ValueError("2D context requires a 2D world_coordinate_system")
        if self.coordinate_space_dimension == 3 and self.world_coordinate_system.kind != "3d":
            raise ValueError("3D context requires a 3D world_coordinate_system")
        if self.context_type == "Model" and self.coordinate_space_dimension != 3:
            raise ValueError("Model parent context must be three-dimensional")
        if self.context_type == "Plan" and self.coordinate_space_dimension != 2:
            raise ValueError("Plan parent context must be two-dimensional")
        return self


class UpdateParentContextOperation(_IfcContextOperation):
    action: Literal["update_parent_context"] = "update_parent_context"
    context_type: IfcContextType | None = None
    coordinate_space_dimension: Literal[2, 3] | None = None
    precision: float | None = Field(default=None, gt=0.0)
    world_coordinate_system: IfcWorldCoordinateSystem | None = None
    true_north: tuple[float, float] | None = None

    @field_validator("true_north")
    @classmethod
    def require_nonzero_true_north(
        cls, value: tuple[float, float] | None
    ) -> tuple[float, float] | None:
        if value is not None and not _nonzero(value):
            raise ValueError("true_north must be non-zero")
        return value

    @model_validator(mode="after")
    def require_explicit_update(self) -> "UpdateParentContextOperation":
        changed = self.model_fields_set - {"action", "context_identifier"}
        if not changed:
            raise ValueError("update_parent_context must specify at least one changed field")
        for field_name in (
            "context_type",
            "coordinate_space_dimension",
            "precision",
            "world_coordinate_system",
        ):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null when explicitly updated")
        dimension = self.coordinate_space_dimension
        if dimension is not None and self.world_coordinate_system is None:
            raise ValueError("changing context dimension requires world_coordinate_system")
        if (
            dimension == 2
            and self.world_coordinate_system is not None
            and self.world_coordinate_system.kind != "2d"
        ):
            raise ValueError("2D context requires a 2D world_coordinate_system")
        if (
            dimension == 3
            and self.world_coordinate_system is not None
            and self.world_coordinate_system.kind != "3d"
        ):
            raise ValueError("3D context requires a 3D world_coordinate_system")
        return self


class CreateSubcontextOperation(_IfcContextOperation):
    action: Literal["create_subcontext"] = "create_subcontext"
    context_type: IfcContextType
    parent_context_identifier: str = Field(min_length=1, max_length=255)
    target_view: IfcTargetView
    target_scale: float | None = Field(default=None, gt=0.0)
    user_defined_target_view: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("parent_context_identifier", "user_defined_target_view", mode="before")
    @classmethod
    def normalize_labels(cls, value: object) -> object:
        return _normalize_label(value)

    @model_validator(mode="after")
    def validate_user_defined_view(self) -> "CreateSubcontextOperation":
        if (self.target_view == "USERDEFINED") != (self.user_defined_target_view is not None):
            raise ValueError(
                "user_defined_target_view is required only for USERDEFINED target_view"
            )
        return self


class UpdateSubcontextOperation(_IfcContextOperation):
    action: Literal["update_subcontext"] = "update_subcontext"
    context_type: IfcContextType | None = None
    parent_context_identifier: str | None = Field(default=None, min_length=1, max_length=255)
    target_view: IfcTargetView | None = None
    target_scale: float | None = Field(default=None, gt=0.0)
    user_defined_target_view: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("parent_context_identifier", "user_defined_target_view", mode="before")
    @classmethod
    def normalize_labels(cls, value: object) -> object:
        return _normalize_label(value)

    @model_validator(mode="after")
    def require_explicit_update(self) -> "UpdateSubcontextOperation":
        changed = self.model_fields_set - {"action", "context_identifier"}
        if not changed:
            raise ValueError("update_subcontext must specify at least one changed field")
        for field_name in ("context_type", "parent_context_identifier", "target_view"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null when explicitly updated")
        if self.target_view == "USERDEFINED" and self.user_defined_target_view is None:
            raise ValueError("USERDEFINED target_view requires user_defined_target_view")
        if (
            self.user_defined_target_view is not None
            and self.target_view is not None
            and self.target_view != "USERDEFINED"
        ):
            raise ValueError("user_defined_target_view requires USERDEFINED target_view")
        return self


IfcContextOperation: TypeAlias = Annotated[
    CreateParentContextOperation
    | UpdateParentContextOperation
    | CreateSubcontextOperation
    | UpdateSubcontextOperation,
    Field(discriminator="action"),
]


class ConfigureIfcGeometryBasisCommand(IfcGeometryBasisContract):
    project_unit_change: IfcProjectUnitChange | None = None
    coordinate_reference_change: IfcCoordinateReferenceChange | None = None
    context_operations: tuple[IfcContextOperation, ...] = Field(
        default=(), max_length=MAX_IFC_CONTEXT_OPERATIONS
    )

    @model_validator(mode="after")
    def require_nonempty_change(self) -> "ConfigureIfcGeometryBasisCommand":
        if (
            self.project_unit_change is None
            and self.coordinate_reference_change is None
            and not self.context_operations
        ):
            raise ValueError("geometry basis command must contain at least one change")
        return self

    def operation_request_payload(self) -> dict[str, object]:
        """Preserve submitted fields and required union discriminators for durable history."""

        payload = _explicit_contract_payload(self)
        if not isinstance(payload, dict):
            raise TypeError("geometry basis operation request must be an object")
        return payload


_REQUIRED_LITERAL_FIELDS = frozenset({"action", "kind", "mode", "name"})


def _explicit_contract_payload(value: object) -> object:
    if isinstance(value, BaseModel):
        fields = type(value).model_fields
        return {
            field_name: _explicit_contract_payload(getattr(value, field_name))
            for field_name in fields
            if field_name in value.model_fields_set or field_name in _REQUIRED_LITERAL_FIELDS
        }
    if isinstance(value, tuple):
        return [_explicit_contract_payload(item) for item in value]
    return value


class IfcProductBodyContextFacts(IfcGeometryBasisContract):
    """Actual Body subcontext attributes, including unsupported configurations."""

    context_type: str | None
    target_view: str | None
    parent_entity_type: str | None
    parent_context_type: str | None
    parent_coordinate_space_dimension: int | None


class IfcSiUnitSummary(IfcGeometryBasisContract):
    name: Literal["METRE", "RADIAN"]
    prefix: IfcSIPrefix | None = None


class IfcLocalCoordinateReferenceSummary(IfcGeometryBasisContract):
    mode: Literal["local"] = "local"


class IfcProjectedCrsCoordinateReferenceSummary(IfcGeometryBasisContract):
    mode: Literal["projected_crs"] = "projected_crs"
    source_context_identifier: str = Field(min_length=1, max_length=255)
    crs_name: str = Field(pattern=r"^EPSG:[1-9][0-9]*$", max_length=255)
    description: str | None = Field(default=None, max_length=1024)
    geodetic_datum: str | None = Field(default=None, max_length=255)
    vertical_datum: str | None = Field(default=None, max_length=255)
    map_projection: str | None = Field(default=None, max_length=255)
    map_zone: str | None = Field(default=None, max_length=255)
    map_unit: IfcSiUnitSummary | None = None
    eastings: float
    northings: float
    orthogonal_height: float
    x_axis_abscissa: float
    x_axis_ordinate: float
    scale: float = Field(gt=0.0)

    @model_validator(mode="after")
    def require_nonzero_x_axis(self) -> "IfcProjectedCrsCoordinateReferenceSummary":
        if self.x_axis_abscissa == 0.0 and self.x_axis_ordinate == 0.0:
            raise ValueError("map conversion x-axis direction must be non-zero")
        return self


IfcCoordinateReferenceSummary: TypeAlias = Annotated[
    IfcLocalCoordinateReferenceSummary | IfcProjectedCrsCoordinateReferenceSummary,
    Field(discriminator="mode"),
]


class IfcGeometryBasisSummary(IfcGeometryBasisContract):
    length_unit: IfcSiUnitSummary
    plane_angle_unit: IfcSiUnitSummary
    precision: float = Field(gt=0.0)
    coordinate_reference: IfcCoordinateReferenceSummary
    main_context_identifier: str = Field(min_length=1, max_length=255)
    main_context_type: Literal["Model"] = "Model"
    coordinate_space_dimension: Literal[3] = 3
    update_semantics: Literal["reinterpret_existing_values"] = "reinterpret_existing_values"


class IfcRepresentationContextSummary(IfcGeometryBasisContract):
    context_identifier: str = Field(min_length=1, max_length=255)
    kind: Literal["parent_context", "subcontext"]
    context_type: IfcContextType
    coordinate_space_dimension: Literal[2, 3]
    parent_context_identifier: str | None = Field(default=None, min_length=1, max_length=255)
    target_view: IfcTargetView | None = None
    target_scale: float | None = Field(default=None, gt=0.0)
    user_defined_target_view: str | None = Field(default=None, min_length=1, max_length=255)
    representation_count: int = Field(ge=0)
    precision: float | None = Field(default=None, gt=0.0)
    world_coordinate_system: IfcWorldCoordinateSystem | None = None
    true_north: tuple[float, float] | None = None

    @field_validator("context_identifier", "parent_context_identifier", mode="before")
    @classmethod
    def normalize_identifiers(cls, value: object) -> object:
        return _normalize_label(value)

    @field_validator("true_north")
    @classmethod
    def require_nonzero_true_north(
        cls, value: tuple[float, float] | None
    ) -> tuple[float, float] | None:
        if value is not None and not _nonzero(value):
            raise ValueError("true_north must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_kind_specific_fields(self) -> "IfcRepresentationContextSummary":
        if self.kind == "parent_context":
            if any(
                value is not None
                for value in (
                    self.parent_context_identifier,
                    self.target_view,
                    self.target_scale,
                    self.user_defined_target_view,
                )
            ):
                raise ValueError("parent context must not contain subcontext fields")
            if self.precision is None or self.world_coordinate_system is None:
                raise ValueError("parent context requires precision and world_coordinate_system")
        else:
            if self.parent_context_identifier is None or self.target_view is None:
                raise ValueError("subcontext requires parent_context_identifier and target_view")
            if (
                self.precision is not None
                or self.world_coordinate_system is not None
                or self.true_north is not None
            ):
                raise ValueError("subcontext must not repeat inherited parent context fields")
            if (self.target_view == "USERDEFINED") != (self.user_defined_target_view is not None):
                raise ValueError(
                    "user_defined_target_view is required only for USERDEFINED target_view"
                )
        return self


class IfcRepresentationContextCatalog(IfcGeometryBasisContract):
    contexts: tuple[IfcRepresentationContextSummary, ...] = Field(
        default=(), max_length=MAX_IFC_REPRESENTATION_CONTEXTS
    )

    @model_validator(mode="after")
    def require_unique_sorted_identifiers(self) -> "IfcRepresentationContextCatalog":
        identifiers = tuple(item.context_identifier for item in self.contexts)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("context catalog identifiers must be unique")
        if identifiers != tuple(sorted(identifiers)):
            raise ValueError("context catalog must be sorted by ContextIdentifier")
        return self


__all__ = [
    "ConfigureIfcGeometryBasisCommand",
    "CreateParentContextOperation",
    "CreateSubcontextOperation",
    "IfcContextOperation",
    "IfcCoordinateReferenceChange",
    "IfcCoordinateReferenceSummary",
    "IfcGeometryBasisBusinessError",
    "IfcGeometryBasisCompilationError",
    "IfcGeometryBasisContract",
    "IfcGeometryBasisError",
    "IfcGeometryBasisIntegrityError",
    "IfcGeometryBasisNoChangeError",
    "IfcGeometryBasisSummary",
    "IfcLengthUnit",
    "IfcLocalCoordinateReferenceChange",
    "IfcLocalCoordinateReferenceSummary",
    "IfcPlaneAngleUnit",
    "IfcProjectUnitChange",
    "IfcProjectedCrsCoordinateReferenceChange",
    "IfcProjectedCrsCoordinateReferenceSummary",
    "IfcRepresentationContextAlreadyExistsError",
    "IfcRepresentationContextCatalog",
    "IfcRepresentationContextIncompatibleError",
    "IfcRepresentationContextNotFoundError",
    "IfcRepresentationContextSummary",
    "IfcSIPrefix",
    "IfcSiUnitSummary",
    "IfcTargetView",
    "IfcWorldCoordinateSystem",
    "IfcWorldCoordinateSystem2D",
    "IfcWorldCoordinateSystem3D",
    "MAX_IFC_CONTEXT_OPERATIONS",
    "MAX_IFC_REPRESENTATION_CONTEXTS",
    "UpdateParentContextOperation",
    "UpdateSubcontextOperation",
]
