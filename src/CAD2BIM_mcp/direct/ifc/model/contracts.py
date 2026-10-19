"""Direct provider contracts for durable IFC model capabilities."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import Field, StrictBool, field_validator, model_validator

from CAD2BIM_backend.domain.ifc_geometry_basis import (
    MAX_IFC_REPRESENTATION_CONTEXTS,
)

from CAD2BIM_mcp.direct.contract_base import (
    DirectContract,
    NonNegativeInt64,
    PositiveInt64,
    _non_blank_no_nul,
)
from CAD2BIM_mcp.tool_docs import ifc_model as _tool_docs


class IfcProjectResult(DirectContract):
    global_id: str = Field(pattern=r"^[0-9A-Za-z_$]{22}$")
    name: str = Field(min_length=1, max_length=255)

    _name = field_validator("name")(_non_blank_no_nul)


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


def _nonzero(values: tuple[float, ...]) -> bool:
    return any(value != 0.0 for value in values)


class IfcLengthUnitInput(DirectContract):
    name: Literal["METRE"] = Field(
        description=_tool_docs.IFC_LENGTH_UNIT_INPUT_NAME_DESCRIPTION,
    )
    prefix: IfcSIPrefix | None = Field(
        default=None,
        description=(_tool_docs.IFC_LENGTH_UNIT_INPUT_PREFIX_DESCRIPTION),
    )


class IfcPlaneAngleUnitInput(DirectContract):
    name: Literal["RADIAN"] = Field(
        description=_tool_docs.IFC_PLANE_ANGLE_UNIT_INPUT_NAME_DESCRIPTION,
    )
    prefix: IfcSIPrefix | None = Field(
        default=None,
        description=_tool_docs.IFC_PLANE_ANGLE_UNIT_INPUT_PREFIX_DESCRIPTION,
    )


class IfcProjectUnitChangeInput(DirectContract):
    length_unit: IfcLengthUnitInput | None = Field(
        default=None,
        description=_tool_docs.IFC_PROJECT_UNIT_CHANGE_INPUT_LENGTH_UNIT_DESCRIPTION,
    )
    plane_angle_unit: IfcPlaneAngleUnitInput | None = Field(
        default=None,
        description=_tool_docs.IFC_PROJECT_UNIT_CHANGE_INPUT_PLANE_ANGLE_UNIT_DESCRIPTION,
    )

    @model_validator(mode="after")
    def require_explicit_unit(self) -> "IfcProjectUnitChangeInput":
        if self.length_unit is None and self.plane_angle_unit is None:
            raise ValueError("project_unit_change must specify at least one unit")
        return self


class IfcWorldCoordinateSystem2DInput(DirectContract):
    kind: Literal["2d"] = Field(
        description=_tool_docs.IFC_WORLD_COORDINATE_SYSTEM2_DINPUT_KIND_DESCRIPTION,
    )
    origin: tuple[float, float] = Field(
        description=_tool_docs.IFC_WORLD_COORDINATE_SYSTEM2_DINPUT_ORIGIN_DESCRIPTION,
    )
    ref_direction: tuple[float, float] = Field(
        description=_tool_docs.IFC_WORLD_COORDINATE_SYSTEM2_DINPUT_REF_DIRECTION_DESCRIPTION,
    )

    @field_validator("ref_direction")
    @classmethod
    def require_nonzero_ref_direction(
        cls,
        value: tuple[float, float],
    ) -> tuple[float, float]:
        if not _nonzero(value):
            raise ValueError("ref_direction must be non-zero")
        return value


class IfcWorldCoordinateSystem3DInput(DirectContract):
    kind: Literal["3d"] = Field(
        description=_tool_docs.IFC_WORLD_COORDINATE_SYSTEM3_DINPUT_KIND_DESCRIPTION,
    )
    origin: tuple[float, float, float] = Field(
        description=_tool_docs.IFC_WORLD_COORDINATE_SYSTEM3_DINPUT_ORIGIN_DESCRIPTION,
    )
    axis: tuple[float, float, float] = Field(
        description=_tool_docs.IFC_WORLD_COORDINATE_SYSTEM3_DINPUT_AXIS_DESCRIPTION,
    )
    ref_direction: tuple[float, float, float] = Field(
        description=(_tool_docs.IFC_WORLD_COORDINATE_SYSTEM3_DINPUT_REF_DIRECTION_DESCRIPTION),
    )

    @field_validator("axis", "ref_direction")
    @classmethod
    def require_nonzero_direction(
        cls,
        value: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        if not _nonzero(value):
            raise ValueError("direction must be non-zero")
        return value

    @model_validator(mode="after")
    def require_independent_directions(self) -> "IfcWorldCoordinateSystem3DInput":
        ax, ay, az = self.axis
        rx, ry, rz = self.ref_direction
        cross = (ay * rz - az * ry, az * rx - ax * rz, ax * ry - ay * rx)
        if not _nonzero(cross):
            raise ValueError("axis and ref_direction must not be parallel")
        return self


IfcWorldCoordinateSystemInput: TypeAlias = (
    IfcWorldCoordinateSystem2DInput | IfcWorldCoordinateSystem3DInput
)


class IfcLocalCoordinateReferenceChangeInput(DirectContract):
    mode: Literal["local"] = Field(
        description=_tool_docs.IFC_LOCAL_COORDINATE_REFERENCE_CHANGE_INPUT_MODE_DESCRIPTION,
    )


class IfcProjectedCrsCoordinateReferenceChangeInput(DirectContract):
    mode: Literal["projected_crs"] = Field(
        description=_tool_docs.IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_MODE_DESCRIPTION,
    )
    source_context_identifier: str = Field(
        min_length=1,
        max_length=255,
        description=(
            _tool_docs.IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_SOURCE_CONTEXT_IDENTIFIER_DESCRIPTION
        ),
    )
    crs_name: str = Field(
        pattern=r"^EPSG:[1-9][0-9]*$",
        max_length=255,
        description=_tool_docs.IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_CRS_NAME_DESCRIPTION,
    )
    description: str | None = Field(
        default=None,
        max_length=1024,
        description=_tool_docs.IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_DESCRIPTION_DESCRIPTION,
    )
    geodetic_datum: str | None = Field(
        default=None,
        max_length=255,
        description=_tool_docs.IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_GEODETIC_DATUM_DESCRIPTION,
    )
    vertical_datum: str | None = Field(
        default=None,
        max_length=255,
        description=_tool_docs.IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_VERTICAL_DATUM_DESCRIPTION,
    )
    map_projection: str | None = Field(
        default=None,
        max_length=255,
        description=_tool_docs.IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_MAP_PROJECTION_DESCRIPTION,
    )
    map_zone: str | None = Field(
        default=None,
        max_length=255,
        description=_tool_docs.IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_MAP_ZONE_DESCRIPTION,
    )
    map_unit: IfcLengthUnitInput | None = Field(
        default=None,
        description=_tool_docs.IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_MAP_UNIT_DESCRIPTION,
    )
    eastings: float = Field(
        description=_tool_docs.IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_EASTINGS_DESCRIPTION,
    )
    northings: float = Field(
        description=_tool_docs.IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_NORTHINGS_DESCRIPTION,
    )
    orthogonal_height: float = Field(
        description=_tool_docs.IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_ORTHOGONAL_HEIGHT_DESCRIPTION,
    )
    x_axis_abscissa: float = Field(
        description=_tool_docs.IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_X_AXIS_ABSCISSA_DESCRIPTION,
    )
    x_axis_ordinate: float = Field(
        description=_tool_docs.IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_X_AXIS_ORDINATE_DESCRIPTION,
    )
    scale: float = Field(
        gt=0.0,
        description=_tool_docs.IFC_PROJECTED_CRS_COORDINATE_REFERENCE_CHANGE_INPUT_SCALE_DESCRIPTION,
    )

    @field_validator("source_context_identifier", "crs_name")
    @classmethod
    def validate_labels(cls, value: str | None) -> str | None:
        if value is not None:
            return _non_blank_no_nul(value)
        return value

    @model_validator(mode="after")
    def require_nonzero_x_axis(
        self,
    ) -> "IfcProjectedCrsCoordinateReferenceChangeInput":
        if self.x_axis_abscissa == 0.0 and self.x_axis_ordinate == 0.0:
            raise ValueError("map conversion x-axis direction must be non-zero")
        return self


IfcCoordinateReferenceChangeInput: TypeAlias = (
    IfcLocalCoordinateReferenceChangeInput | IfcProjectedCrsCoordinateReferenceChangeInput
)


class _IfcContextOperationInput(DirectContract):
    context_identifier: str = Field(
        min_length=1,
        max_length=255,
        description=(_tool_docs.IFC_CONTEXT_OPERATION_INPUT_CONTEXT_IDENTIFIER_DESCRIPTION),
    )

    _identifier = field_validator("context_identifier")(_non_blank_no_nul)


class CreateParentContextInput(_IfcContextOperationInput):
    action: Literal["create_parent_context"] = Field(
        description=_tool_docs.CREATE_PARENT_CONTEXT_INPUT_ACTION_DESCRIPTION,
    )
    context_type: IfcContextType = Field(
        description=_tool_docs.CREATE_PARENT_CONTEXT_INPUT_CONTEXT_TYPE_DESCRIPTION,
    )
    coordinate_space_dimension: Literal[2, 3] = Field(
        description=_tool_docs.CREATE_PARENT_CONTEXT_INPUT_COORDINATE_SPACE_DIMENSION_DESCRIPTION,
    )
    precision: float = Field(
        gt=0.0,
        description=_tool_docs.CREATE_PARENT_CONTEXT_INPUT_PRECISION_DESCRIPTION,
    )
    world_coordinate_system: IfcWorldCoordinateSystemInput = Field(
        description=_tool_docs.CREATE_PARENT_CONTEXT_INPUT_WORLD_COORDINATE_SYSTEM_DESCRIPTION,
    )
    true_north: tuple[float, float] | None = Field(
        default=None,
        description=_tool_docs.CREATE_PARENT_CONTEXT_INPUT_TRUE_NORTH_DESCRIPTION,
    )

    @field_validator("true_north")
    @classmethod
    def require_nonzero_true_north(
        cls,
        value: tuple[float, float] | None,
    ) -> tuple[float, float] | None:
        if value is not None and not _nonzero(value):
            raise ValueError("true_north must be non-zero")
        return value

    @model_validator(mode="after")
    def require_compatible_dimension(self) -> "CreateParentContextInput":
        if self.coordinate_space_dimension == 2 and self.world_coordinate_system.kind != "2d":
            raise ValueError("2D context requires a 2D world_coordinate_system")
        if self.coordinate_space_dimension == 3 and self.world_coordinate_system.kind != "3d":
            raise ValueError("3D context requires a 3D world_coordinate_system")
        if self.context_type == "Model" and self.coordinate_space_dimension != 3:
            raise ValueError("Model parent context must be three-dimensional")
        if self.context_type == "Plan" and self.coordinate_space_dimension != 2:
            raise ValueError("Plan parent context must be two-dimensional")
        return self


class UpdateParentContextInput(_IfcContextOperationInput):
    action: Literal["update_parent_context"] = Field(
        description=_tool_docs.UPDATE_PARENT_CONTEXT_INPUT_ACTION_DESCRIPTION,
    )
    context_type: IfcContextType | None = Field(
        default=None,
        description=_tool_docs.UPDATE_PARENT_CONTEXT_INPUT_CONTEXT_TYPE_DESCRIPTION,
    )
    coordinate_space_dimension: Literal[2, 3] | None = Field(
        default=None,
        description=(_tool_docs.UPDATE_PARENT_CONTEXT_INPUT_COORDINATE_SPACE_DIMENSION_DESCRIPTION),
    )
    precision: float | None = Field(
        default=None,
        gt=0.0,
        description=_tool_docs.UPDATE_PARENT_CONTEXT_INPUT_PRECISION_DESCRIPTION,
    )
    world_coordinate_system: IfcWorldCoordinateSystemInput | None = Field(
        default=None,
        description=_tool_docs.UPDATE_PARENT_CONTEXT_INPUT_WORLD_COORDINATE_SYSTEM_DESCRIPTION,
    )
    true_north: tuple[float, float] | None = Field(
        default=None,
        description=(_tool_docs.UPDATE_PARENT_CONTEXT_INPUT_TRUE_NORTH_DESCRIPTION),
    )

    @field_validator("true_north")
    @classmethod
    def require_nonzero_true_north(
        cls,
        value: tuple[float, float] | None,
    ) -> tuple[float, float] | None:
        if value is not None and not _nonzero(value):
            raise ValueError("true_north must be non-zero")
        return value

    @model_validator(mode="after")
    def require_explicit_update(self) -> "UpdateParentContextInput":
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


class CreateSubcontextInput(_IfcContextOperationInput):
    action: Literal["create_subcontext"] = Field(
        description=_tool_docs.CREATE_SUBCONTEXT_INPUT_ACTION_DESCRIPTION,
    )
    context_type: IfcContextType = Field(
        description=_tool_docs.CREATE_SUBCONTEXT_INPUT_CONTEXT_TYPE_DESCRIPTION,
    )
    parent_context_identifier: str = Field(
        min_length=1,
        max_length=255,
        description=(_tool_docs.CREATE_SUBCONTEXT_INPUT_PARENT_CONTEXT_IDENTIFIER_DESCRIPTION),
    )
    target_view: IfcTargetView = Field(
        description=(_tool_docs.CREATE_SUBCONTEXT_INPUT_TARGET_VIEW_DESCRIPTION),
    )
    target_scale: float | None = Field(
        default=None,
        gt=0.0,
        description=_tool_docs.CREATE_SUBCONTEXT_INPUT_TARGET_SCALE_DESCRIPTION,
    )
    user_defined_target_view: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description=_tool_docs.CREATE_SUBCONTEXT_INPUT_USER_DEFINED_TARGET_VIEW_DESCRIPTION,
    )

    _parent_identifier = field_validator("parent_context_identifier")(_non_blank_no_nul)

    @field_validator("user_defined_target_view")
    @classmethod
    def validate_user_view(cls, value: str | None) -> str | None:
        return _non_blank_no_nul(value) if value is not None else value

    @model_validator(mode="after")
    def validate_user_defined_view(self) -> "CreateSubcontextInput":
        if (self.target_view == "USERDEFINED") != (self.user_defined_target_view is not None):
            raise ValueError(
                "user_defined_target_view is required only for USERDEFINED target_view"
            )
        return self


class UpdateSubcontextInput(_IfcContextOperationInput):
    action: Literal["update_subcontext"] = Field(
        description=_tool_docs.UPDATE_SUBCONTEXT_INPUT_ACTION_DESCRIPTION,
    )
    context_type: IfcContextType | None = Field(
        default=None,
        description=_tool_docs.UPDATE_PARENT_CONTEXT_INPUT_CONTEXT_TYPE_DESCRIPTION,
    )
    parent_context_identifier: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description=_tool_docs.UPDATE_SUBCONTEXT_INPUT_PARENT_CONTEXT_IDENTIFIER_DESCRIPTION,
    )
    target_view: IfcTargetView | None = Field(
        default=None,
        description=_tool_docs.UPDATE_SUBCONTEXT_INPUT_TARGET_VIEW_DESCRIPTION,
    )
    target_scale: float | None = Field(
        default=None,
        gt=0.0,
        description=_tool_docs.UPDATE_SUBCONTEXT_INPUT_TARGET_SCALE_DESCRIPTION,
    )
    user_defined_target_view: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description=(_tool_docs.UPDATE_SUBCONTEXT_INPUT_USER_DEFINED_TARGET_VIEW_DESCRIPTION),
    )

    @field_validator("parent_context_identifier", "user_defined_target_view")
    @classmethod
    def validate_optional_labels(cls, value: str | None) -> str | None:
        return _non_blank_no_nul(value) if value is not None else value

    @model_validator(mode="after")
    def require_explicit_update(self) -> "UpdateSubcontextInput":
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


IfcContextOperationInput: TypeAlias = (
    CreateParentContextInput
    | UpdateParentContextInput
    | CreateSubcontextInput
    | UpdateSubcontextInput
)


class IfcSiUnitResult(DirectContract):
    name: Literal["METRE", "RADIAN"]
    prefix: IfcSIPrefix | None = None


class IfcLocalCoordinateReferenceResult(DirectContract):
    mode: Literal["local"] = "local"


class IfcProjectedCrsCoordinateReferenceResult(DirectContract):
    mode: Literal["projected_crs"] = "projected_crs"
    source_context_identifier: str = Field(min_length=1, max_length=255)
    crs_name: str = Field(pattern=r"^EPSG:[1-9][0-9]*$", max_length=255)
    description: str | None = Field(default=None, max_length=1024)
    geodetic_datum: str | None = Field(default=None, max_length=255)
    vertical_datum: str | None = Field(default=None, max_length=255)
    map_projection: str | None = Field(default=None, max_length=255)
    map_zone: str | None = Field(default=None, max_length=255)
    map_unit: IfcSiUnitResult | None = None
    eastings: float
    northings: float
    orthogonal_height: float
    x_axis_abscissa: float
    x_axis_ordinate: float
    scale: float = Field(gt=0.0)


IfcCoordinateReferenceResult: TypeAlias = Annotated[
    IfcLocalCoordinateReferenceResult | IfcProjectedCrsCoordinateReferenceResult,
    Field(discriminator="mode"),
]


class IfcGeometryBasisResult(DirectContract):
    length_unit: IfcSiUnitResult
    plane_angle_unit: IfcSiUnitResult
    precision: float = Field(gt=0.0)
    coordinate_reference: IfcCoordinateReferenceResult
    main_context_identifier: str = Field(min_length=1, max_length=255)
    main_context_type: Literal["Model"] = "Model"
    coordinate_space_dimension: Literal[3] = 3
    update_semantics: Literal["reinterpret_existing_values"] = "reinterpret_existing_values"


class SourceRevision(DirectContract):
    revision_ref: str = Field(pattern=r"^cur\..+$", max_length=16_384,
        description=_tool_docs.IFC_SOURCE_REVISION_REFERENCE_DESCRIPTION)
    revision_no: PositiveInt64


class IfcModelInfoResult(DirectContract):
    ifc_schema: Literal["IFC4X3_ADD2"] = "IFC4X3_ADD2"
    project: IfcProjectResult
    size_bytes: PositiveInt64 = Field(
        description=_tool_docs.IFC_MODEL_INFO_RESULT_SIZE_BYTES_DESCRIPTION
    )
    geometry_basis_configured: StrictBool
    geometry_basis: IfcGeometryBasisResult | None
    site_count: NonNegativeInt64
    element_count: NonNegativeInt64
    source_revision: SourceRevision | None = None


class IfcModelInitializeResult(DirectContract):
    operation_alias: str = Field(pattern=r"^op_[0-9]{3,}$", max_length=32)
    model: IfcModelInfoResult


class IfcModelConfigureGeometryBasisResult(DirectContract):
    operation_alias: str = Field(pattern=r"^op_[0-9]{3,}$", max_length=32)
    model: IfcModelInfoResult


class IfcWorldCoordinateSystem2DResult(DirectContract):
    kind: Literal["2d"] = "2d"
    origin: tuple[float, float]
    ref_direction: tuple[float, float]


class IfcWorldCoordinateSystem3DResult(DirectContract):
    kind: Literal["3d"] = "3d"
    origin: tuple[float, float, float]
    axis: tuple[float, float, float]
    ref_direction: tuple[float, float, float]


IfcWorldCoordinateSystemResult: TypeAlias = Annotated[
    IfcWorldCoordinateSystem2DResult | IfcWorldCoordinateSystem3DResult,
    Field(discriminator="kind"),
]


class IfcRepresentationContextResult(DirectContract):
    context_identifier: str = Field(min_length=1, max_length=255)
    kind: Literal["parent_context", "subcontext"]
    context_type: IfcContextType
    coordinate_space_dimension: Literal[2, 3]
    parent_context_identifier: str | None = Field(default=None, min_length=1, max_length=255)
    target_view: IfcTargetView | None = None
    target_scale: float | None = Field(default=None, gt=0.0)
    user_defined_target_view: str | None = Field(default=None, min_length=1, max_length=255)
    representation_count: NonNegativeInt64
    precision: float | None = Field(default=None, gt=0.0)
    world_coordinate_system: IfcWorldCoordinateSystemResult | None = None
    true_north: tuple[float, float] | None = None


class IfcRepresentationContextCatalogResult(DirectContract):
    contexts: tuple[IfcRepresentationContextResult, ...] = Field(
        max_length=MAX_IFC_REPRESENTATION_CONTEXTS
    )


def to_domain_geometry_basis_command(
    project_unit_change: IfcProjectUnitChangeInput | None,
    coordinate_reference_change: IfcCoordinateReferenceChangeInput | None,
    context_operations: tuple[IfcContextOperationInput, ...],
):
    """Convert only explicitly submitted Provider fields into the domain command."""

    from CAD2BIM_backend.domain.ifc_geometry_basis import (
        ConfigureIfcGeometryBasisCommand,
    )

    payload: dict[str, Any] = {"context_operations": []}
    if project_unit_change is not None:
        payload["project_unit_change"] = project_unit_change.model_dump(
            mode="python",
            exclude_unset=True,
        )
    if coordinate_reference_change is not None:
        payload["coordinate_reference_change"] = coordinate_reference_change.model_dump(
            mode="python",
            exclude_unset=True,
        )
    payload["context_operations"] = [
        operation.model_dump(mode="python", exclude_unset=True) for operation in context_operations
    ]
    return ConfigureIfcGeometryBasisCommand.model_validate(payload)


def geometry_basis_result(summary: Any | None) -> IfcGeometryBasisResult | None:
    if summary is None:
        return None
    return IfcGeometryBasisResult.model_validate(summary.model_dump(mode="python"))


def representation_context_catalog_result(
    catalog: Any,
) -> IfcRepresentationContextCatalogResult:
    return IfcRepresentationContextCatalogResult.model_validate(catalog.model_dump(mode="python"))


class IfcModelPrimaryResult(DirectContract):
    result_kind: Literal["ifc_model"] = "ifc_model"
    ifc_schema: Literal["IFC4X3_ADD2"] = "IFC4X3_ADD2"
    project_global_id: str = Field(pattern=r"^[0-9A-Za-z_$]{22}$")
    project_name: str = Field(min_length=1, max_length=255)
    result: Literal["model_initialized"] = "model_initialized"

    _project_name = field_validator("project_name")(_non_blank_no_nul)


class IfcModelInitializeOperationDetail(DirectContract):
    detail_kind: Literal["ifc_model_initialize"]
    ifc_schema: Literal["IFC4X3_ADD2"]
    project_name: str = Field(min_length=1, max_length=255)
    project_global_id: str = Field(pattern=r"^[0-9A-Za-z_$]{22}$")
    result: Literal["model_initialized"] | None

    _project_name = field_validator("project_name")(_non_blank_no_nul)
