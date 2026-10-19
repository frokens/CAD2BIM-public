"""Entry-neutral ABI for one exact ProductFamily realization execution."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Literal, Mapping, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
    model_serializer,
)

from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.geometry_frame import (
    RigidTransform3D,
    Vector3,
    compose_rigid_transform,
    inverse_rigid_transform,
    rigid_transform_from_axis_reference,
)
from CAD2BIM_backend.domain.geometry_interface import (
    CircularInterfaceProfile,
    GeometryInterfaceProfile,
    GeometryModelInterfaces,
    InterfaceFrameEvaluation,
    InterfaceFrameRelation,
    InterfaceExternalCondition,
    InterfaceExternalEvaluation,
    _unit_vector,
    evaluate_interface_external_condition,
    evaluate_interface_frame_relation,
)
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.ifc_geometry_basis import IfcLengthUnit
from CAD2BIM_backend.domain.ifc_geometry_construction import (
    GeometryConstruction,
    IfcGeometryRepresentationRoute,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometrySummary,
    GeometryBuildInputs,
    _PreparedGeometryFunctionParameters,
)
from CAD2BIM_backend.domain.product_family import (
    MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS,
    ProductFamilyContract,
    ProductFamilyModelInterfaceSource,
    ProductFamilyFixedParameterSource,
    ProductFamilyPublicParameterSource,
    normalize_product_family_parameter_values,
    normalize_product_family_public_parameters,
    standard_product_family_structure,
)


MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS = 64
MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES = 65_536
MAX_PRODUCT_REALIZATION_SOURCE_TOTAL_BYTES = 262_144
MAX_PRODUCT_REALIZATION_SOURCE_BUNDLE_BYTES = 272 * 1024
MAX_PRODUCT_REALIZATION_RESULT_BYTES = 1024 * 1024
MAX_PRODUCT_REALIZATION_CONTROL_BYTES = 1024 * 1024
PRODUCT_REALIZATION_WALL_SECONDS = 30.0
PRODUCT_REALIZATION_CPU_SECONDS = 30.0
PRODUCT_REALIZATION_RSS_BYTES = 2 * 1024**3
PRODUCT_REALIZATION_SCRATCH_BYTES = 256 * 1024**2
PRODUCT_REALIZATION_THREADS = 8
PRODUCT_REALIZATION_FRAME_TOLERANCE_M = 1e-6
PRODUCT_REALIZATION_ANGLE_TOLERANCE_RAD = 1e-6
_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _RealizationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProductRealizationStatus(StrEnum):
    SOLVED = "SOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    INFEASIBLE = "INFEASIBLE"
    CONTRACT_REJECTED = "CONTRACT_REJECTED"


class ProductRealizationExecutionPolicy(_RealizationModel):
    """Fixed read-only capabilities exposed to one realization invocation."""

    mode: Literal["preview"] = "preview"
    unit: Literal["METRE"] = "METRE"
    frame_tolerance_m: Literal[PRODUCT_REALIZATION_FRAME_TOLERANCE_M] = (
        PRODUCT_REALIZATION_FRAME_TOLERANCE_M
    )
    angular_tolerance_rad: Literal[PRODUCT_REALIZATION_ANGLE_TOLERANCE_RAD] = (
        PRODUCT_REALIZATION_ANGLE_TOLERANCE_RAD
    )


class _ProductRealizationInstallationCondition(_RealizationModel):
    """A standard external installation target in project-world metres."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    position_tolerance_m: float = Field(default=1e-6, gt=0, le=1)
    angular_tolerance_rad: float = Field(default=1e-6, gt=0, le=math.pi)
    outer_diameter_m: float | None = Field(default=None, gt=0, le=1000,
        description="Optional circular diameter of the bound output interface, in metres. When supplied it is independently compared; omission does not waive a Family profile requirement.")

    @field_validator("start_m", "end_m", "point_m", "axis", "reference_direction",
                     mode="before", check_fields=False)
    @classmethod
    def reject_boolean_coordinates(cls, value):
        if isinstance(value, (list, tuple)) and any(isinstance(item, bool) for item in value):
            raise ValueError("installation target coordinates must be numbers, not booleans")
        return value

    @field_validator("axis", "reference_direction", check_fields=False)
    @classmethod
    def normalize_direction(cls, value):
        return None if value is None else _unit_vector(value)

    @model_validator(mode="after")
    def require_defined_roll(self) -> Self:
        reference = getattr(self, "reference_direction", None)
        if reference is not None:
            if self.direction_relation == "none":
                raise ValueError("installation roll requires an explicit direction relation")
            if abs(math.fsum(a*b for a, b in zip(self.axis, reference))) > 1e-9:
                raise ValueError("installation roll reference must be perpendicular to its target axis")
        return self


class ProductRealizationFiniteSegmentCondition(_ProductRealizationInstallationCondition):
    """The full directed segment bounds allowed connection positions, not product length."""

    kind: Literal["finite_segment"]
    start_m: Vector3 = Field(
        description="Start point in metres of the complete allowed connection window; it does not fix a product endpoint."
    )
    end_m: Vector3 = Field(
        description="End point in metres of the complete allowed connection window; using its full length is not required."
    )

    @property
    def length_m(self) -> float:
        return math.hypot(*(end-start for start, end in zip(self.start_m, self.end_m)))

    @property
    def axis(self) -> Vector3:
        return _unit_vector(tuple(end-start for start, end in zip(self.start_m, self.end_m)))

    @model_validator(mode="after")
    def require_finite_nonzero_segment(self) -> Self:
        if not math.isfinite(self.length_m) or self.length_m == 0:
            raise ValueError("installation segment must have a finite nonzero length")
        return self


class ProductRealizationFiniteSegmentPositionCondition(ProductRealizationFiniteSegmentCondition):
    direction_relation: Literal["none"]


class ProductRealizationFiniteSegmentDirectedCondition(ProductRealizationFiniteSegmentCondition):
    direction_relation: Literal["same", "opposed"]
    reference_direction: Vector3 | None = None


class ProductRealizationFixedPointCondition(_ProductRealizationInstallationCondition):
    kind: Literal["fixed_point"]
    point_m: Vector3 = Field(description="World-space target for the bound output interface origin, in metres.")


class ProductRealizationFixedPointPositionCondition(ProductRealizationFixedPointCondition):
    direction_relation: Literal["none"]


class ProductRealizationFixedPointDirectedCondition(ProductRealizationFixedPointCondition):
    direction_relation: Literal["same", "opposed"]
    axis: Vector3
    reference_direction: Vector3 | None = None


class ProductRealizationInfiniteAxisCondition(_ProductRealizationInstallationCondition):
    kind: Literal["infinite_axis"]
    point_m: Vector3
    axis: Vector3


class ProductRealizationInfiniteAxisPositionCondition(ProductRealizationInfiniteAxisCondition):
    direction_relation: Literal["none"]


class ProductRealizationInfiniteAxisDirectedCondition(ProductRealizationInfiniteAxisCondition):
    direction_relation: Literal["same", "opposed"]
    reference_direction: Vector3 | None = None


# Each complete literal branch is closed and directly shareable as a Schema reference.
ProductRealizationInstallationCondition = (
    ProductRealizationFiniteSegmentPositionCondition | ProductRealizationFiniteSegmentDirectedCondition
    | ProductRealizationFixedPointPositionCondition | ProductRealizationFixedPointDirectedCondition
    | ProductRealizationInfiniteAxisPositionCondition | ProductRealizationInfiniteAxisDirectedCondition
)


def interface_external_condition_from_installation(
    condition: ProductRealizationInstallationCondition,
) -> InterfaceExternalCondition:
    """Project standard inputs to the existing actual-interface evaluator."""

    if isinstance(condition, ProductRealizationFixedPointCondition):
        position = {"position_kind": "point", "point_m": condition.point_m}
    else:
        finite = isinstance(condition, ProductRealizationFiniteSegmentCondition)
        position = {
            "position_kind": "line",
            "point_m": condition.start_m if finite else condition.point_m,
            "direction": condition.axis,
            "extent": ({"extent_kind": "bounded", "minimum_m": 0.0,
                        "maximum_m": condition.length_m} if finite else {"extent_kind": "unbounded"}),
        }
    return InterfaceExternalCondition.model_validate({
        "position": position,
        "direction": ({"direction_kind": "none"} if condition.direction_relation == "none"
                      else {"direction_kind": condition.direction_relation, "axis": condition.axis}),
        "roll": ({"roll_kind": "none"} if getattr(condition, "reference_direction", None) is None
                 else {"roll_kind": "reference", "reference_direction": condition.reference_direction}),
        "profile": ({"profile_kind": "none"} if condition.outer_diameter_m is None
                    else {"profile_kind": "equal", "profile": {
                        "kind": "circular", "diameter_m": condition.outer_diameter_m}}),
        "position_tolerance_m": condition.position_tolerance_m,
        "angular_tolerance_rad": condition.angular_tolerance_rad,
    })


class ProductRealizationExternalConstraint(_RealizationModel):
    """A requirement with provenance; reported external contact remains unverified."""

    interface_key: str = Field(pattern=_KEY_PATTERN)
    source_kind: Literal["design_target", "observed_interface"] = "design_target"
    evidence_reference: str | None = Field(default=None, min_length=1, max_length=16_384)
    condition: ProductRealizationInstallationCondition

    @model_validator(mode="after")
    def require_observation_reference(self) -> Self:
        if self.source_kind == "observed_interface" and self.evidence_reference is None:
            raise ValueError("observed interface conditions require an exact evidence reference")
        return self


class ProductRealizationInvocation(_RealizationModel):
    """Current entry-neutral facts; all exposed interfaces come from Geometry receipts."""

    realization_kind: Literal["self_geometry", "standard_model"] = "self_geometry"
    public_parameters: dict[str, JsonValue] = Field(default_factory=dict)
    product_origin: tuple[float, float, float]
    length_unit: IfcLengthUnit
    external_constraints: tuple[ProductRealizationExternalConstraint, ...] = Field(default=(), max_length=64)

    @field_validator("product_origin", mode="before")
    @classmethod
    def reject_boolean_product_origin(cls, value):
        if isinstance(value, (list, tuple)) and any(isinstance(item, bool) for item in value):
            raise ValueError("product_origin coordinates must be numbers, not booleans")
        return value

    @field_validator("product_origin")
    @classmethod
    def require_finite_product_origin(
        cls,
        value: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        if any(not math.isfinite(item) for item in value):
            raise ValueError("product_origin must contain three finite coordinates")
        return value

    @model_validator(mode="after")
    def require_closed_invocation(self) -> Self:
        if self.external_constraints and self.realization_kind != "self_geometry":
            raise ValueError("explicit external constraints require self_geometry invocation")
        keys = [item.interface_key for item in self.external_constraints]
        if len(keys) != len(set(keys)):
            raise ValueError("external interface constraints must be unique")
        if len(canonical_json(self.public_parameters).encode("utf-8")) > 65_536:
            raise ValueError("product realization parameters exceed their byte budget")
        return self


class ProductRealizationValidationScenario(ProductRealizationInvocation):
    """Authoring evidence wrapper; the Runtime never receives expected_status."""

    scenario_key: str = Field(pattern=_KEY_PATTERN)
    expected_status: ProductRealizationStatus

    def to_invocation(self) -> ProductRealizationInvocation:
        return ProductRealizationInvocation.model_validate(
            self.model_dump(exclude={"scenario_key", "expected_status"})
        )


class _ResolvedGeometryBinding(_RealizationModel):
    binding_key: str = Field(pattern=_KEY_PATTERN)
    geometry_content_digest: str = Field(pattern=_SHA256_PATTERN)
    # Backend selects this from the exact SYSTEM resource; authors cannot set it.
    ifc_representation_route: IfcGeometryRepresentationRoute = IfcGeometryRepresentationRoute.TFS


class ResolvedMappedGeometryBinding(_ResolvedGeometryBinding):
    resource_kind: Literal[GeometryResourceKind.MAPPED_MODEL] = GeometryResourceKind.MAPPED_MODEL
    geometry: CanonicalGeometry | None = None
    interfaces: GeometryModelInterfaces = ()

    @model_validator(mode="after")
    def require_exact_brep(self) -> Self:
        if self.geometry is None:
            return self
        if self.geometry.format is not CanonicalGeometryFormat.OCCT_BREP_V1:
            raise ValueError("product realization mapped geometry must use canonical OCCT BREP")
        if hashlib.sha256(self.geometry.payload).hexdigest() != self.geometry.sha256:
            raise ValueError("product realization mapped geometry bytes do not match their SHA")
        return self


class ResolvedModelingFunctionBinding(_ResolvedGeometryBinding):
    resource_kind: Literal[GeometryResourceKind.MODELING_FUNCTION] = (
        GeometryResourceKind.MODELING_FUNCTION
    )
    source_bundle: bytes = Field(
        min_length=1, max_length=MAX_PRODUCT_REALIZATION_SOURCE_BUNDLE_BYTES
    )
    source_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    parameter_schema: dict[str, JsonValue]
    geometry_inputs: GeometryBuildInputs = ()
    # Selected only by the backend's exact, owned SYSTEM resource policy.
    source_reuse_mode: Literal["exact", "rigid_two_port"] = "exact"

    @model_validator(mode="after")
    def require_exact_source(self) -> Self:
        if hashlib.sha256(self.source_bundle).hexdigest() != self.source_bundle_sha256:
            raise ValueError("product realization function source does not match its SHA")
        canonical_json(self.parameter_schema)
        return self


ResolvedProductGeometryBinding = Annotated[
    ResolvedMappedGeometryBinding | ResolvedModelingFunctionBinding,
    Field(discriminator="resource_kind"),
]


def geometry_source_request_parameters(binding, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Project only the known SYSTEM body's rigid pose out of its source key.

    Generic author code retains every parameter and its observable ordering.
    The selected two-port source still builds the first original input; this
    projection supplies no alternative geometry or numeric solving formula.
    """
    if (not isinstance(binding, ResolvedModelingFunctionBinding)
            or binding.source_reuse_mode != "rigid_two_port"
            or parameters["shape"]["mode"] != "curved"):
        return parameters
    return {
        key: ({name: value for name, value in item.items()
               if name not in ("frame_x", "local_center_m")} if key == "shape" else
              {side: {name: value for name, value in port.items()
                      if name not in ("origin_m", "axis", "reference_direction")}
               for side, port in item.items()} if key == "ports" else item)
        for key, item in parameters.items()
    }


def geometry_source_to_fragment(
    binding,
    source_parameters: dict[str, JsonValue],
    target_parameters: dict[str, JsonValue],
    *,
    uniform_scale: float = 1.0,
) -> RigidTransform3D | None:
    """Match a representative's actual frame to a checked current fragment.

    A failed match means this request needs its exact full-parameter source.
    The independently evaluated current interfaces are never replaced by the
    representative's interfaces. Local BREP already contains uniform scale.
    """
    if source_parameters == target_parameters:
        return RigidTransform3D()
    if (not isinstance(binding, ResolvedModelingFunctionBinding)
            or binding.source_reuse_mode != "rigid_two_port"
            or source_parameters["shape"]["mode"] != "curved"
            or target_parameters["shape"]["mode"] != "curved"
            or geometry_source_request_parameters(binding, source_parameters)
               != geometry_source_request_parameters(binding, target_parameters)):
        return None

    def unit(vector):
        length = math.hypot(*vector)
        return tuple(value / length for value in vector) if length > 1e-12 else None

    def frame(parameters):
        first = unit(parameters["ports"]["side_a"]["axis"])
        second = unit(parameters["ports"]["side_b"]["axis"])
        if first is None or second is None:
            return None
        normal = unit((first[1] * second[2] - first[2] * second[1],
                       first[2] * second[0] - first[0] * second[2],
                       first[0] * second[1] - first[1] * second[0]))
        if normal is None:
            return None
        return rigid_transform_from_axis_reference(
            tuple(value * uniform_scale for value in parameters["shape"]["local_center_m"]),
            normal, first)

    source_frame, target_frame = frame(source_parameters), frame(target_parameters)
    if source_frame is None or target_frame is None:
        return None
    transform = compose_rigid_transform(target_frame, inverse_rigid_transform(source_frame))

    def rotate(vector):
        return tuple(sum(a * b for a, b in zip(row, vector, strict=True))
                     for row in transform.rotation)

    def distance(left, right):
        return math.hypot(*(a - b for a, b in zip(left, right, strict=True)))

    ports = target_parameters["ports"]
    model_scale = max(ports["side_a"]["outer_diameter_m"],
                      ports["side_b"]["outer_diameter_m"],
                      target_parameters["shape"]["body_outer_diameter_m"],
                      distance(ports["side_a"]["origin_m"], ports["side_b"]["origin_m"]))
    position_tolerance = max(1e-9 * model_scale, 1e-12) * uniform_scale
    for side in ("side_a", "side_b"):
        source, target = source_parameters["ports"][side], ports[side]
        rotated = rotate(source["origin_m"])
        placed = tuple(value * uniform_scale + offset
                       for value, offset in zip(rotated, transform.translation_m, strict=True))
        if (distance(placed, tuple(value * uniform_scale for value in target["origin_m"]))
                > position_tolerance
                or distance(rotate(source["axis"]), target["axis"]) > 1e-7):
            return None
    source_radial = unit(source_parameters["shape"]["frame_x"])
    target_radial = unit(target_parameters["shape"]["frame_x"])
    if (source_radial is None or target_radial is None
            or distance(rotate(source_radial), target_radial) > 1e-7):
        return None
    return transform


class ProductGeometryInterfaceEvaluation(_RealizationModel):
    fragment_key: str = Field(pattern=_KEY_PATTERN)
    binding_key: str = Field(pattern=_KEY_PATTERN)
    normalized_parameters: dict[str, JsonValue] = Field(default_factory=dict)
    interfaces: GeometryModelInterfaces = ()
    uniform_scale: float = Field(default=1.0, gt=0.0, allow_inf_nan=False)

    @field_validator("uniform_scale", mode="before")
    @classmethod
    def reject_boolean_uniform_scale(cls, value):
        if isinstance(value, bool):
            raise ValueError("uniform_scale must be a finite positive number, not a boolean")
        return value

    @model_serializer(mode="wrap")
    def preserve_empty_interface_receipt(self, handler):
        result = handler(self)
        if not self.interfaces:
            result.pop("interfaces", None)
        if self.uniform_scale == 1.0:
            result.pop("uniform_scale", None)
        return result

    @field_validator("normalized_parameters")
    @classmethod
    def require_json_domain(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        canonical_json(value)
        return value


class ProductGeometryInstanceReceipt(ProductGeometryInterfaceEvaluation):
    geometry_sha256: str = Field(pattern=_SHA256_PATTERN)
    geometry_summary: GeometrySummary | None = None

    @classmethod
    def from_evaluation(cls, evaluation: ProductGeometryInterfaceEvaluation, *,
                        geometry_sha256: str, geometry_summary: GeometrySummary | None = None):
        """Bind one checked source to this invocation's actual interfaces."""
        return cls(**evaluation.model_dump(mode="python"),
                   geometry_sha256=geometry_sha256, geometry_summary=geometry_summary)


class ProductRealizationComponentPlacement(_RealizationModel):
    component_key: str = Field(pattern=_KEY_PATTERN)
    local_transform: RigidTransform3D | None = None


class ProductRealizationAuthorFragmentPlacement(_RealizationModel):
    fragment_key: str = Field(pattern=_KEY_PATTERN)
    binding_key: str = Field(pattern=_KEY_PATTERN)
    local_transform: RigidTransform3D | None = None
    uniform_scale: float = Field(default=1.0, gt=0.0, allow_inf_nan=False)

    @field_validator("uniform_scale", mode="before")
    @classmethod
    def reject_boolean_uniform_scale(cls, value):
        if isinstance(value, bool):
            raise ValueError("uniform_scale must be a finite positive number, not a boolean")
        return value


class ProductRealizationFragmentPlacement(ProductRealizationAuthorFragmentPlacement):
    """Canonical execution facts; Geometry arguments have one Backend owner."""

    geometry_parameters: dict[str, JsonValue]

    @field_validator("geometry_parameters")
    @classmethod
    def require_bounded_geometry_parameters(
        cls,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        if len(canonical_json(value).encode("utf-8")) > 65_536:
            raise ValueError("fragment geometry parameters exceed their byte budget")
        return value


class ProductRealizationComponentWorldTransform(_RealizationModel):
    component_key: str = Field(pattern=_KEY_PATTERN)
    project_world_transform: RigidTransform3D


class ProductRealizationInterfaceWorldFrame(_RealizationModel):
    interface_key: str = Field(pattern=_KEY_PATTERN)
    project_world_frame: RigidTransform3D
    profile: GeometryInterfaceProfile | None = None
    source: ProductFamilyModelInterfaceSource
    component_local_frame: RigidTransform3D

class SolvedProductRealizationAuthorProposal(_RealizationModel):
    status: Literal[ProductRealizationStatus.SOLVED] = ProductRealizationStatus.SOLVED
    internal_parameters: dict[str, JsonValue] = Field(default_factory=dict)
    components: tuple[ProductRealizationComponentPlacement, ...] = Field(min_length=1)
    fragments: tuple[ProductRealizationAuthorFragmentPlacement, ...] = Field(min_length=1)

    @field_validator("internal_parameters")
    @classmethod
    def require_internal_json(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if len(canonical_json(value).encode("utf-8")) > 65_536:
            raise ValueError("internal parameters exceed their byte budget")
        return value


class SolvedProductRealizationProposal(SolvedProductRealizationAuthorProposal):
    fragments: tuple[ProductRealizationFragmentPlacement, ...] = Field(min_length=1)


class ProductRealizationAmbiguity(_RealizationModel):
    ambiguity_key: str = Field(pattern=_KEY_PATTERN)
    message: str = Field(min_length=1, max_length=512)


class AmbiguousProductRealizationProposal(_RealizationModel):
    status: Literal[ProductRealizationStatus.AMBIGUOUS] = ProductRealizationStatus.AMBIGUOUS
    ambiguities: tuple[ProductRealizationAmbiguity, ...] = Field(min_length=1, max_length=64)


class ProductRealizationViolation(_RealizationModel):
    violation_key: str = Field(pattern=_KEY_PATTERN)
    message: str = Field(min_length=1, max_length=512)


class InfeasibleProductRealizationProposal(_RealizationModel):
    status: Literal[ProductRealizationStatus.INFEASIBLE] = ProductRealizationStatus.INFEASIBLE
    violations: tuple[ProductRealizationViolation, ...] = Field(min_length=1, max_length=64)


class RejectedProductRealizationProposal(_RealizationModel):
    """Backend contract rejection; never an author-declared infeasible solution."""
    status: Literal[ProductRealizationStatus.CONTRACT_REJECTED] = ProductRealizationStatus.CONTRACT_REJECTED
    rejection: ProductRealizationViolation


ProductRealizationProposal = Annotated[
    SolvedProductRealizationProposal
    | AmbiguousProductRealizationProposal
    | InfeasibleProductRealizationProposal
    | RejectedProductRealizationProposal,
    Field(discriminator="status"),
]


ProductRealizationAuthorProposal = Annotated[
    SolvedProductRealizationAuthorProposal
    | AmbiguousProductRealizationProposal
    | InfeasibleProductRealizationProposal,
    Field(discriminator="status"),
]


class ProductRealizationRequest(_RealizationModel):
    family_contract: ProductFamilyContract
    family_contract_digest: str = Field(pattern=_SHA256_PATTERN)
    source_bundle: bytes = Field(
        min_length=1, max_length=MAX_PRODUCT_REALIZATION_SOURCE_BUNDLE_BYTES
    )
    source_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    geometry_bindings: tuple[ResolvedProductGeometryBinding, ...] = Field(
        min_length=1,
        max_length=MAX_PRODUCT_FAMILY_GEOMETRY_BINDINGS,
    )
    invocation: ProductRealizationInvocation
    policy: ProductRealizationExecutionPolicy = Field(
        default_factory=ProductRealizationExecutionPolicy
    )

    @model_validator(mode="after")
    def require_exact_input_closure(self) -> Self:
        self._require_invocation()
        self._require_closure()
        return self

    def _require_closure(self) -> None:
        if self.family_contract_digest != sha256_digest(self.family_contract):
            raise ValueError("product realization family contract digest does not match")
        if hashlib.sha256(self.source_bundle).hexdigest() != self.source_bundle_sha256:
            raise ValueError("product realization source bundle does not match its SHA")
        binding_keys = tuple(item.binding_key for item in self.geometry_bindings)
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("product realization binding keys must be unique")
        required = {
            option.binding_key
            for fragment in self.family_contract.geometry_fragments
            for option in fragment.binding_options
        }
        if set(binding_keys) != required:
            raise ValueError("product realization exact bindings do not match the family contract")
        bindings = {item.binding_key: item for item in self.geometry_bindings}
        for fragment in self.family_contract.geometry_fragments:
            for option_index, option in enumerate(fragment.binding_options):
                binding = bindings[option.binding_key]
                expected = (set(binding.parameter_schema["properties"])
                            if isinstance(binding, ResolvedModelingFunctionBinding) else set())
                if option.parameter_sources.default_source is not None or set(option.parameter_sources.overrides) != expected:
                    raise ValueError(
                        f"fragment {fragment.fragment_key} binding_options.{option_index}.parameter_sources "
                        "must cover every exact Geometry parameter"
                    )
                if (isinstance(binding, ResolvedMappedGeometryBinding)
                        and binding.geometry is not None
                        and binding.geometry.format != CanonicalGeometryFormat.OCCT_BREP_V1):
                    raise ValueError("self geometry requires canonical OCCT BREP bindings")
        return self

    def _require_invocation(self) -> None:
        if self.invocation.length_unit != IfcLengthUnit():
            raise ValueError(
                "product realization Runtime request requires product_origin normalized to METRE"
            )
        validate_product_realization_explicit_requirements(self.family_contract, self.invocation)
        normalized = normalize_product_family_public_parameters(
            self.family_contract, self.invocation.public_parameters,
        )
        if normalized != self.invocation.public_parameters:
            raise ValueError("product realization requires normalized public parameters")
        if self.invocation.realization_kind == "standard_model":
            standard_product_family_structure(self.family_contract)


class _PreparedProductRealizationRequest:
    """Private exact closure, fully validated and owned for one batch lifecycle."""

    __slots__ = ("__base",)

    def __init__(self, base: ProductRealizationRequest) -> None:
        self.__base = base.model_copy(deep=True)

    def restore_invocation(self, value) -> ProductRealizationRequest:
        invocation = ProductRealizationInvocation.model_validate(value)
        # Only this private owner may reuse validated closure fields. A deep
        # per-item copy protects both preparation and other items from mutation.
        request = self.__base.model_copy(deep=True, update={"invocation": invocation})
        request._require_invocation()
        return request

    def project_normalized_invocation(self, invocation: ProductRealizationInvocation) -> ProductRealizationRequest:
        """Same input owner projects its already-normalized invocation once."""
        return self.__base.model_copy(deep=True, update={"invocation": invocation.model_copy(deep=True)})


def _prepare_geometry_binding_parameters(bindings):
    return {binding.binding_key: _PreparedGeometryFunctionParameters(binding.parameter_schema)
            for binding in bindings if isinstance(binding, ResolvedModelingFunctionBinding)}


def validate_product_realization_explicit_requirements(contract, invocation) -> None:
    if invocation.realization_kind == "standard_model":
        return
    interfaces = {item.interface_key: item for item in contract.interfaces}
    requirements = {item.interface_key: item.external_constraint for item in contract.interfaces
                    if item.external_constraint is not None}
    supplied = {item.interface_key: item for item in invocation.external_constraints}
    if set(supplied) - set(requirements):
        raise ValueError("external condition references an interface without a Family requirement")
    for key, requirement in requirements.items():
        if key not in supplied:
            if requirement.required:
                raise ValueError(f"required external condition is missing: {key}")
            continue
        condition = supplied[key].condition
        if condition.kind not in requirement.allowed_kinds:
            raise ValueError(f"external condition kind is not permitted for {key}")
        if condition.direction_relation not in requirement.allowed_directions:
            raise ValueError(f"external direction mode is not permitted for {key}")
        if requirement.roll_required and getattr(condition, "reference_direction", None) is None:
            raise ValueError(f"external roll cannot be relaxed for {key}")
        if (requirement.profile_required or interfaces[key].connection_profile is not None) and condition.outer_diameter_m is None:
            raise ValueError(f"external profile cannot be relaxed for {key}")
        if (condition.position_tolerance_m > requirement.maximum_position_tolerance_m
            or condition.angular_tolerance_rad > requirement.maximum_angular_tolerance_rad):
            raise ValueError(f"external tolerance exceeds the Family limit for {key}")


def product_realization_structure(request: ProductRealizationRequest) -> ProductFamilyContract:
    return (standard_product_family_structure(request.family_contract)
            if request.invocation.realization_kind == "standard_model" else request.family_contract)


def standard_model_root_transform(
    request: ProductRealizationRequest,
) -> RigidTransform3D:
    standard = request.family_contract.standard_model
    if request.invocation.realization_kind != "standard_model" or standard is None:
        raise ValueError("standard placement requires the exact Family standard model contract")
    return RigidTransform3D(translation_m=request.invocation.product_origin)


class ProductRealizationFrameDiagnostic(_RealizationModel):
    """A Backend-derived check; its frames are declared coordinates, not contact evidence."""

    check_kind: Literal["interface_coincidence", "connection_profile"]
    check_key: str = Field(pattern=_KEY_PATTERN)
    interface_key: str = Field(pattern=_KEY_PATTERN)
    peer_interface_key: str | None = Field(default=None, pattern=_KEY_PATTERN)
    evaluation: InterfaceFrameEvaluation


class ProductRealizationExternalConditionCheck(_RealizationModel):
    interface_key: str = Field(pattern=_KEY_PATTERN)
    source_kind: Literal["design_target", "observed_interface"]
    evidence_reference: str | None = None
    evaluation: InterfaceExternalEvaluation
    external_actual_connection_status: Literal["NOT_CHECKED"] = "NOT_CHECKED"


class ProductRealizationResult(_RealizationModel):
    execution_stage: Literal["solved", "interface_constraints_evaluated", "instantiated"] = Field(
        default="instantiated", description=(
            "Completed execution stage. interface_constraints_evaluated means the solved named "
            "interfaces were evaluated against declared Family/internal and invocation external "
            "conditions; the result may be FAIL and does not modify adjacent products."))
    invocation: ProductRealizationInvocation
    policy: ProductRealizationExecutionPolicy
    family_contract_digest: str = Field(pattern=_SHA256_PATTERN)
    binding_digest: str = Field(pattern=_SHA256_PATTERN)
    source_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    normalized_public_parameters: dict[str, JsonValue]
    proposal: ProductRealizationProposal
    instance_receipts: tuple[ProductGeometryInstanceReceipt, ...] = ()
    interface_evaluations: tuple[ProductGeometryInterfaceEvaluation, ...] = ()
    component_project_world_transforms: tuple[ProductRealizationComponentWorldTransform, ...] = ()
    interface_project_world_frames: tuple[ProductRealizationInterfaceWorldFrame, ...] = ()
    frame_check_status: Literal["PASS", "FAIL"] | None = Field(default=None, description=(
        "Result of evaluating solved named-interface frames/profiles against declared Family "
        "internal constraints and invocation.external_constraints, not current adjacent-object "
        "fit, physical end-face contact, pipe trimming or IFC port connectivity."))
    frame_diagnostics: tuple[ProductRealizationFrameDiagnostic, ...] = ()
    external_condition_checks: tuple[ProductRealizationExternalConditionCheck, ...] = ()

    @model_serializer(mode="wrap")
    def serialize_optional_checks(self, handler):
        result = handler(self)
        if not self.external_condition_checks:
            result.pop("external_condition_checks", None)
        if self.frame_check_status is None:
            result.pop("frame_check_status", None)
        if not self.frame_diagnostics:
            result.pop("frame_diagnostics", None)
        return result

    @model_validator(mode="after")
    def require_result_shape(self) -> Self:
        if self.normalized_public_parameters != self.invocation.public_parameters:
            raise ValueError("result normalized parameters differ from its frozen invocation")
        if self.execution_stage != "instantiated" and self.instance_receipts:
            raise ValueError("uninstantiated result cannot publish geometry receipts")
        if self.execution_stage == "solved" and self.interface_evaluations:
            raise ValueError("solve-only result cannot publish evaluated interfaces")
        if len(canonical_json(self.normalized_public_parameters).encode("utf-8")) > 65_536:
            raise ValueError("normalized public parameters exceed their byte budget")
        fragment_keys = tuple(item.fragment_key for item in self.instance_receipts)
        if len(fragment_keys) != len(set(fragment_keys)):
            raise ValueError("product realization receipt fragment keys must be unique")
        if isinstance(self.proposal, SolvedProductRealizationProposal):
            component_keys = tuple(
                item.component_key for item in self.component_project_world_transforms
            )
            if len(component_keys) != len(set(component_keys)):
                raise ValueError("solved realization component world transforms must be unique")
            interface_keys = tuple(
                item.interface_key for item in self.interface_project_world_frames
            )
            if len(interface_keys) != len(set(interface_keys)):
                raise ValueError("solved realization interface world frames must be unique")
        elif (
            self.instance_receipts
            or self.interface_evaluations
            or self.component_project_world_transforms
            or self.interface_project_world_frames
            or self.frame_check_status is not None
            or self.frame_diagnostics
            or self.external_condition_checks
        ):
            raise ValueError("non-solved realization cannot publish solved geometry facts")
        return self


class ProductRealizationPreviewEvidence(_RealizationModel):
    """Derived same-solve evidence carried by the existing exact Preview snapshot."""

    contract_digest: str = Field(pattern=_SHA256_PATTERN)
    invocation: ProductRealizationInvocation
    realization: ProductRealizationResult
    geometry_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def require_same_solve(self) -> Self:
        if self.realization.proposal.status is not ProductRealizationStatus.SOLVED:
            raise ValueError("preview evidence requires solved geometry")
        if self.realization.normalized_public_parameters != self.invocation.public_parameters:
            raise ValueError("preview evidence changed its normalized public parameters")
        return self


class ProductRealizationOutputNeed(StrEnum):
    SOLVE_ONLY = "solve_only"
    INTERFACES = "interfaces"
    LOCAL_FRAGMENTS = "local_fragments"
    PREVIEW_WORLD = "preview_world"


MAX_PRODUCT_REALIZATION_BATCH_ITEMS = 1024


class ProductRealizationGeometryExecution(_RealizationModel):
    """Typed solve, composed world geometry and optional exact local instances."""

    realization: ProductRealizationResult
    output_need: ProductRealizationOutputNeed = ProductRealizationOutputNeed.PREVIEW_WORLD
    geometry: CanonicalGeometry | None = None
    instance_geometries: dict[str, CanonicalGeometry] | None = None
    instance_constructions: dict[str, GeometryConstruction] | None = None
    instance_source_transforms: dict[str, RigidTransform3D] | None = None

    @model_validator(mode="after")
    def require_solved_geometry_closure(self) -> Self:
        if self.instance_source_transforms is not None:
            if self.output_need is not ProductRealizationOutputNeed.LOCAL_FRAGMENTS:
                raise ValueError("source transforms are private to local-fragment execution")
            if (self.instance_geometries is None
                    or not self.instance_source_transforms.keys() <= self.instance_geometries.keys()):
                raise ValueError("source transforms require their exact fragment bytes")
        if self.instance_constructions is not None:
            if self.output_need is not ProductRealizationOutputNeed.LOCAL_FRAGMENTS:
                raise ValueError("construction facts are private to local-fragment execution")
            if self.instance_geometries is None or not self.instance_constructions.keys() <= self.instance_geometries.keys():
                raise ValueError("construction facts require the corresponding exact fragment bytes")
        solved = isinstance(self.realization.proposal, SolvedProductRealizationProposal)
        needs_world = self.output_need is ProductRealizationOutputNeed.PREVIEW_WORLD
        if needs_world and any(receipt.geometry_summary is None for receipt in self.realization.instance_receipts):
            raise ValueError("preview-world execution requires full instance measurements")
        instantiated = self.realization.execution_stage == "instantiated"
        if (solved and needs_world and instantiated) != (self.geometry is not None):
            raise ValueError("only a SOLVED realization can publish preview geometry")
        if self.output_need in (ProductRealizationOutputNeed.SOLVE_ONLY, ProductRealizationOutputNeed.INTERFACES) and self.instance_geometries is not None:
            raise ValueError("solve-only execution cannot publish instance geometry")
        if self.output_need is ProductRealizationOutputNeed.LOCAL_FRAGMENTS and self.instance_geometries is None:
            raise ValueError("local-fragment execution requires exact instance geometry")
        if self.geometry is not None:
            if self.geometry.summary is None:
                raise ValueError("preview-world execution requires full geometry measurements")
            if self.geometry.format is not CanonicalGeometryFormat.OCCT_BREP_V1:
                raise ValueError("product realization preview geometry must be canonical OCCT BREP")
            if hashlib.sha256(self.geometry.payload).hexdigest() != self.geometry.sha256:
                raise ValueError(
                    "product realization preview geometry bytes do not match their SHA"
                )
        if self.instance_geometries is not None:
            receipts = {item.fragment_key: item for item in self.realization.instance_receipts}
            if set(self.instance_geometries) != set(receipts):
                raise ValueError("fragment geometry must match every receipt exactly")
            if not solved and self.instance_geometries:
                raise ValueError("non-solved execution cannot contain instance geometry")
            for fragment_key, geometry in self.instance_geometries.items():
                receipt = receipts[fragment_key]
                if (geometry.format is not CanonicalGeometryFormat.OCCT_BREP_V1
                        or geometry.sha256 != receipt.geometry_sha256
                        or hashlib.sha256(geometry.payload).hexdigest() != geometry.sha256):
                    raise ValueError("fragment geometry does not match its exact receipt")
        return self


class ProductFamilyStandardModelManifest(_RealizationModel):
    """Exact Family source, geometry bindings and standard-model geometry."""

    family_version_id: str = Field(min_length=1, max_length=80)
    contract_digest: str = Field(pattern=_SHA256_PATTERN)
    source_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    binding_digest: str = Field(pattern=_SHA256_PATTERN)
    geometry_sha256: str = Field(pattern=_SHA256_PATTERN)
    geometry_summary: GeometrySummary


def product_geometry_binding_digest(
    bindings: Iterable[tuple[str, GeometryResourceKind, str]],
) -> str:
    """Identify the business content of exact bindings, independent of resource IDs."""
    return sha256_digest(
        [
            {
                "binding_key": key,
                "resource_kind": kind.value,
                "geometry_content_digest": digest,
            }
            for key, kind, digest in sorted(bindings, key=lambda value: value[0])
        ]
    )


def product_realization_binding_digest(request: ProductRealizationRequest) -> str:
    return product_geometry_binding_digest(
        (item.binding_key, item.resource_kind, item.geometry_content_digest)
        for item in request.geometry_bindings
    )


def resolve_product_realization_world_facts(
    request: ProductRealizationRequest,
    proposal: ProductRealizationProposal,
    receipts: tuple[ProductGeometryInstanceReceipt, ...],
    *,
    _parameter_rules=None,
    _proposal_validated: bool = False,
) -> tuple[
    tuple[ProductRealizationComponentWorldTransform, ...],
    tuple[ProductRealizationInterfaceWorldFrame, ...],
]:
    """Close one proposal and resolve the single geometry/interface transform chain."""

    component_world_facts: tuple[ProductRealizationComponentWorldTransform, ...] = ()
    interface_world_facts: tuple[ProductRealizationInterfaceWorldFrame, ...] = ()
    if isinstance(proposal, SolvedProductRealizationProposal):
        if not _proposal_validated:
            proposal = validate_product_realization_proposal(request, proposal, _parameter_rules=_parameter_rules)
        receipt_by_fragment = {item.fragment_key: item for item in receipts}
        if len(receipt_by_fragment) != len(receipts):
            raise ValueError("geometry receipt fragment keys must be unique")
        expected_fragment_keys = {item.fragment_key for item in proposal.fragments}
        if set(receipt_by_fragment) != expected_fragment_keys:
            raise ValueError("solved realization requires one actual receipt per fragment")
        for fragment in proposal.fragments:
            receipt = receipt_by_fragment[fragment.fragment_key]
            if receipt.binding_key != fragment.binding_key:
                raise ValueError("solved realization fragment uses the wrong exact binding")
            if canonical_json(receipt.normalized_parameters) != canonical_json(
                fragment.geometry_parameters
            ):
                raise ValueError("geometry receipt parameters differ from the validated proposal")
            if receipt.uniform_scale != fragment.uniform_scale:
                raise ValueError("geometry receipt scale differs from the validated proposal")
        component_world_facts, interface_world_facts = _resolve_project_world_facts(
            request,
            proposal,
            receipts,
        )
    elif receipts:
        raise ValueError("non-solved realization cannot publish geometry instances")
    return component_world_facts, interface_world_facts


def validate_product_realization_proposal(
    request: ProductRealizationRequest,
    proposal: ProductRealizationProposal | ProductRealizationAuthorProposal,
    *,
    _parameter_rules: Mapping[str, _PreparedGeometryFunctionParameters] | None = None,
) -> ProductRealizationProposal:
    """Validate and close one complete numeric solution before any Geometry execution."""

    if not isinstance(proposal, SolvedProductRealizationAuthorProposal):
        return proposal

    canonical_input = isinstance(proposal, SolvedProductRealizationProposal)

    contract = request.family_contract
    structure = product_realization_structure(request)
    component_keys = tuple(item.component_key for item in proposal.components)
    expected_component_keys = tuple(item.component_key for item in structure.components)
    if len(component_keys) != len(set(component_keys)) or set(component_keys) != set(
        expected_component_keys
    ):
        raise ValueError("solved realization components do not match the family contract")
    fragment_keys = tuple(item.fragment_key for item in proposal.fragments)
    expected_fragment_keys = tuple(item.fragment_key for item in structure.geometry_fragments)
    if len(fragment_keys) != len(set(fragment_keys)) or set(fragment_keys) != set(
        expected_fragment_keys
    ):
        raise ValueError("solved realization fragments do not match the family contract")

    solved = normalize_product_family_parameter_values(
        contract.solved_parameters,
        proposal.internal_parameters,
    )
    if canonical_json(solved) != canonical_json(proposal.internal_parameters):
        raise ValueError("solved parameters must be complete and normalized against the Family contract")

    component_definitions = {
        item.component_key: (index, item)
        for index, item in enumerate(structure.components)
    }
    normalized_components: list[ProductRealizationComponentPlacement] = []
    for proposal_index, component in enumerate(proposal.components):
        definition_index, definition = component_definitions[component.component_key]
        if definition.local_transform_mode == "program":
            if component.local_transform is None:
                raise ValueError(
                    f"program component {component.component_key} must provide local_transform"
                )
            normalized_components.append(component)
            continue
        expected_transform = definition.fixed_local_transform or RigidTransform3D()
        if component.local_transform is not None and component.local_transform != expected_transform:
            raise ValueError(
                "Product realization proposal conflicts with a fixed component placement; "
                "reason=FIXED_COMPONENT_TRANSFORM_MISMATCH; "
                f"definition_path=family_contract.components.{definition_index}.fixed_local_transform; "
                f"proposal_path=proposal.components.{proposal_index}.local_transform; "
                f"component_key={component.component_key}"
            )
        normalized_components.append(
            component.model_copy(update={"local_transform": expected_transform})
        )

    fragment_definitions = {
        item.fragment_key: (index, item)
        for index, item in enumerate(structure.geometry_fragments)
    }
    bindings = {item.binding_key: item for item in request.geometry_bindings}
    rules = (_prepare_geometry_binding_parameters(request.geometry_bindings)
             if _parameter_rules is None else _parameter_rules)

    def source_value(source, *, source_path: str):
        if isinstance(source, ProductFamilyFixedParameterSource):
            return source.value
        if isinstance(source, ProductFamilyPublicParameterSource):
            return request.invocation.public_parameters[source.parameter_key]
        try:
            return solved[source.parameter_key]
        except KeyError as exc:
            raise ValueError(
                "selected Geometry binding requires an absent solved parameter; "
                f"definition_path={source_path}; source_parameter_key={source.parameter_key}"
            ) from exc

    def source_identity(source) -> str:
        identity = f"source_kind={source.source_kind}"
        parameter_key = getattr(source, "parameter_key", None)
        if parameter_key is not None:
            identity += f"; source_parameter_key={parameter_key}"
        return identity

    normalized_fragments: list[ProductRealizationFragmentPlacement] = []
    for proposal_index, fragment in enumerate(proposal.fragments):
        definition_index, definition = fragment_definitions[fragment.fragment_key]
        option_match = next(
            ((index, option) for index, option in enumerate(definition.binding_options)
             if option.binding_key == fragment.binding_key),
            None,
        )
        if option_match is None:
            raise ValueError(
                "Product realization proposal selects an undeclared Geometry binding; "
                f"definition_path=family_contract.geometry_fragments.{definition_index}.binding_options; "
                f"proposal_path=proposal.fragments.{proposal_index}.binding_key; "
                f"fragment_key={fragment.fragment_key}; binding_key={fragment.binding_key}"
            )
        option_index, option = option_match
        option_path = f"family_contract.geometry_fragments.{definition_index}.binding_options.{option_index}"
        if definition.local_transform_mode == "program":
            if fragment.local_transform is None:
                raise ValueError(
                    f"program fragment {fragment.fragment_key} must provide local_transform"
                )
            local_transform = fragment.local_transform
        else:
            expected_transform = definition.fixed_local_transform or RigidTransform3D()
            if fragment.local_transform is not None and fragment.local_transform != expected_transform:
                raise ValueError(
                    "Product realization proposal conflicts with a fixed fragment placement; "
                    "reason=FIXED_FRAGMENT_TRANSFORM_MISMATCH; "
                    f"definition_path=family_contract.geometry_fragments.{definition_index}.fixed_local_transform; "
                    f"proposal_path=proposal.fragments.{proposal_index}.local_transform; "
                    f"fragment_key={fragment.fragment_key}"
                )
            local_transform = expected_transform

        expected_parameters = {
            key: source_value(source, source_path=f"{option_path}.parameter_sources.overrides.{key}")
            for key, source in option.parameter_sources.overrides.items()
        }
        binding = bindings[option.binding_key]
        if isinstance(binding, ResolvedModelingFunctionBinding):
            prepared = rules[binding.binding_key]
            checked = prepared.normalize_for(binding.parameter_schema, expected_parameters)
            if canonical_input:
                if set(fragment.geometry_parameters) != set(checked):
                    raise ValueError(f"fragment {fragment.fragment_key} must provide every exact Geometry parameter")
                for parameter_name, source in option.parameter_sources.overrides.items():
                    if canonical_json(checked[parameter_name]) != canonical_json(fragment.geometry_parameters[parameter_name]):
                        raise ValueError(
                            "Product realization proposal conflicts with a declared Geometry parameter source; "
                            "reason=GEOMETRY_PARAMETER_SOURCE_MISMATCH; "
                            f"definition_path={option_path}.parameter_sources.overrides.{parameter_name}; "
                            f"proposal_path=proposal.fragments.{proposal_index}.geometry_parameters.{parameter_name}; "
                            f"fragment_key={fragment.fragment_key}; parameter_name={parameter_name}; {source_identity(source)}"
                        )
            if fragment.uniform_scale != 1.0:
                raise ValueError("modeling-function fragment uniform_scale must be 1")
        elif expected_parameters or (canonical_input and fragment.geometry_parameters):
            raise ValueError("mapped fragment geometry_parameters must be empty")
        else:
            checked = {}
        expected_scale = source_value(
            option.uniform_scale_source,
            source_path=f"{option_path}.uniform_scale_source",
        )
        if fragment.uniform_scale != expected_scale:
            raise ValueError(
                "Product realization proposal conflicts with the declared uniform scale source; "
                "reason=UNIFORM_SCALE_SOURCE_MISMATCH; "
                f"definition_path={option_path}.uniform_scale_source; "
                f"proposal_path=proposal.fragments.{proposal_index}.uniform_scale; "
                f"fragment_key={fragment.fragment_key}; parameter_name=uniform_scale; "
                f"{source_identity(option.uniform_scale_source)}"
            )

        normalized_fragments.append(
            ProductRealizationFragmentPlacement.model_validate({
                **fragment.model_dump(mode="python"), "local_transform": local_transform,
                "geometry_parameters": fragment.geometry_parameters if canonical_input else checked,
            })
        )

    return SolvedProductRealizationProposal.model_validate({
            **proposal.model_dump(mode="python"),
            "components": tuple(normalized_components),
            "fragments": tuple(normalized_fragments),
        })


class ProductRealizationInterfaceContractError(ValueError):
    """An exact Geometry resource omitted a Family-referenced interface key."""


def _model_source_component_local(proposal, receipts, source):
    fragment = next(item for item in proposal.fragments if item.fragment_key == source.fragment_key)
    receipt = next(item for item in receipts if item.fragment_key == fragment.fragment_key)
    model_interface = next((item for item in receipt.interfaces
                            if item.interface_key == source.model_interface_key), None)
    if model_interface is None:
        raise ProductRealizationInterfaceContractError("the selected Geometry has no referenced model interface")
    return compose_rigid_transform(fragment.local_transform, model_interface.local_frame), model_interface.profile


def finalize_product_realization(
    request: ProductRealizationRequest,
    proposal: ProductRealizationProposal,
    receipts: tuple[ProductGeometryInstanceReceipt, ...],
    *,
    output_need: ProductRealizationOutputNeed = ProductRealizationOutputNeed.PREVIEW_WORLD,
    _parameter_rules=None,
    interface_evaluations: tuple[ProductGeometryInterfaceEvaluation, ...] = (),
    _proposal_validated: bool = False,
) -> ProductRealizationResult:
    """The single business finalizer for validated numeric and actual Geometry results."""

    if not _proposal_validated:
        proposal = validate_product_realization_proposal(request, proposal, _parameter_rules=_parameter_rules)
    component_world_facts: tuple[ProductRealizationComponentWorldTransform, ...] = ()
    interface_world_facts: tuple[ProductRealizationInterfaceWorldFrame, ...] = ()
    frame_status: Literal["PASS", "FAIL"] | None = None
    diagnostics: tuple[ProductRealizationFrameDiagnostic, ...] = ()
    external_checks: tuple[ProductRealizationExternalConditionCheck, ...] = ()
    if output_need is ProductRealizationOutputNeed.SOLVE_ONLY:
        if receipts:
            raise ValueError("solve-only realization cannot publish Geometry receipts")
    elif isinstance(proposal, SolvedProductRealizationProposal):
        component_world_facts, interface_world_facts = resolve_product_realization_world_facts(
            request,
            proposal,
            interface_evaluations if output_need is ProductRealizationOutputNeed.INTERFACES else receipts,
            _parameter_rules=_parameter_rules,
            _proposal_validated=True,
        )
        frame_status, diagnostics = _check_realization_frames(
            request,
            interface_world_facts,
        )
        facts = {item.interface_key: item for item in interface_world_facts}
        external_checks = tuple(ProductRealizationExternalConditionCheck(
            interface_key=item.interface_key, source_kind=item.source_kind,
            evidence_reference=item.evidence_reference,
            evaluation=evaluate_interface_external_condition(
                facts[item.interface_key].project_world_frame,
                interface_external_condition_from_installation(item.condition),
                actual_profile=facts[item.interface_key].profile,
            ),
        ) for item in request.invocation.external_constraints)
        frame_status = ("PASS" if all(item.evaluation.satisfied for item in diagnostics)
                        and all(item.evaluation.satisfied for item in external_checks) else "FAIL")
    elif receipts:
        raise ValueError("non-solved realization cannot publish Geometry receipts")

    binding_digest = product_realization_binding_digest(request)
    return ProductRealizationResult(
        execution_stage=("solved" if output_need is ProductRealizationOutputNeed.SOLVE_ONLY or not isinstance(proposal, SolvedProductRealizationProposal)
                         else "interface_constraints_evaluated" if output_need is ProductRealizationOutputNeed.INTERFACES
                         else "instantiated"),
        invocation=request.invocation,
        policy=request.policy,
        family_contract_digest=request.family_contract_digest,
        binding_digest=binding_digest,
        source_bundle_sha256=request.source_bundle_sha256,
        normalized_public_parameters=request.invocation.public_parameters,
        proposal=proposal,
        instance_receipts=receipts,
        interface_evaluations=interface_evaluations,
        component_project_world_transforms=component_world_facts,
        interface_project_world_frames=interface_world_facts,
        frame_check_status=frame_status,
        frame_diagnostics=diagnostics,
        external_condition_checks=external_checks,
    )


def validate_product_realization_result_integrity(
    request: ProductRealizationRequest,
    result: ProductRealizationResult,
    *,
    output_need: ProductRealizationOutputNeed | None = None,
    _parameter_rules: Mapping[str, _PreparedGeometryFunctionParameters] | None = None,
) -> None:
    """Verify request identity, normalized proposal and fragment associations."""

    _validate_product_realization_result_identity(request, result)
    normalized_proposal = validate_product_realization_proposal(
        request,
        result.proposal,
        _parameter_rules=_parameter_rules,
    )
    if normalized_proposal != result.proposal:
        raise ValueError("product realization result proposal is not fully normalized")
    _validate_product_realization_result_facts(request, result, output_need=output_need)


def validate_product_realization_instantiation_precheck(
    request: ProductRealizationRequest,
    precheck: ProductRealizationResult,
) -> None:
    """Check that a host-accepted numeric result addresses this Geometry input.

    The private host-to-child frame carries the already normalized proposal.
    Check what instantiation consumes without rerunning Family parameter-source
    projection, Geometry schema normalization or numerical interface checks.
    """
    if (precheck.execution_stage != "interface_constraints_evaluated"
            or precheck.frame_check_status != "PASS"
            or not isinstance(precheck.proposal, SolvedProductRealizationProposal)):
        raise ValueError("instantiation requires checked PASS interfaces")
    _validate_product_realization_result_identity(request, precheck)
    structure = product_realization_structure(request)
    components = tuple(item.component_key for item in precheck.proposal.components)
    if len(components) != len(set(components)) or set(components) != {
            item.component_key for item in structure.components}:
        raise ValueError("instantiation components do not match the family contract")
    fragments = {item.fragment_key: item for item in precheck.proposal.fragments}
    definitions = {item.fragment_key: item for item in structure.geometry_fragments}
    if (len(fragments) != len(precheck.proposal.fragments)
            or set(fragments) != set(definitions)):
        raise ValueError("instantiation fragments do not match the family contract")
    bindings = {item.binding_key for item in request.geometry_bindings}
    for key, fragment in fragments.items():
        if (fragment.binding_key not in bindings
                or fragment.binding_key not in {
                    option.binding_key for option in definitions[key].binding_options}):
            raise ValueError("instantiation fragment uses an undeclared Geometry binding")
    _validate_product_realization_fragment_receipts(
        precheck.proposal, precheck.interface_evaluations,
    )


def complete_product_realization_instantiation(
    precheck: ProductRealizationResult,
    receipts: tuple[ProductGeometryInstanceReceipt, ...],
) -> ProductRealizationResult:
    """Add source receipts while retaining the accepted numeric result."""
    return precheck.model_copy(update={"execution_stage": "instantiated", "instance_receipts": receipts})


def validate_product_realization_instantiation_result(
    request: ProductRealizationRequest,
    precheck: ProductRealizationResult,
    result: ProductRealizationResult,
    *,
    output_need: ProductRealizationOutputNeed,
) -> None:
    """Verify Geometry's additions to the same runtime's checked numeric result.

    Apply and Preview retain the host-validated precheck and pass that exact
    result to instantiation. The Geometry child checks the received precheck's
    request and fragment associations; its response may add receipts, never change the numeric
    proposal, interfaces, world facts, checks or input identity.
    """
    if (precheck.execution_stage != "interface_constraints_evaluated"
            or precheck.frame_check_status != "PASS"
            or not isinstance(precheck.proposal, SolvedProductRealizationProposal)
            or result.execution_stage != "instantiated"
            or output_need not in (ProductRealizationOutputNeed.LOCAL_FRAGMENTS,
                                   ProductRealizationOutputNeed.PREVIEW_WORLD)):
        raise ValueError("instantiation requires its checked numeric result")
    _validate_product_realization_result_identity(request, result)
    changed_fields = {"execution_stage", "instance_receipts"}
    # Compare the actual wire business representation. Lists and tuples inside
    # JsonValue have the same wire meaning; sequence order remains significant.
    if (result.model_dump(mode="json", exclude=changed_fields)
            != precheck.model_dump(mode="json", exclude=changed_fields)):
        raise ValueError("instantiation changed its checked numeric result")
    _validate_product_realization_fragment_receipts(
        result.proposal, result.instance_receipts,
        require_summary=output_need is ProductRealizationOutputNeed.PREVIEW_WORLD,
    )
    checked_interfaces = {item.fragment_key: item for item in precheck.interface_evaluations}
    for receipt in result.instance_receipts:
        checked = checked_interfaces.get(receipt.fragment_key)
        if checked is None or receipt.interfaces != checked.interfaces:
            raise ValueError("instantiation receipt changed its checked Geometry interfaces")


def _validate_product_realization_result_identity(request, result) -> None:

    expected_identity = (
        product_realization_binding_digest(request),
        request.source_bundle_sha256,
        request.invocation.public_parameters,
        request.invocation,
        request.policy,
        request.family_contract_digest,
    )
    actual_identity = (
        result.binding_digest,
        result.source_bundle_sha256,
        result.normalized_public_parameters,
        result.invocation,
        result.policy,
        result.family_contract_digest,
    )
    if actual_identity != expected_identity:
        raise ValueError("product realization result identity does not match its exact request")


def _validate_product_realization_result_facts(request, result, *, output_need) -> None:
    solved = isinstance(result.proposal, SolvedProductRealizationProposal)
    if output_need is None:
        output_need = (
            ProductRealizationOutputNeed.SOLVE_ONLY
            if result.execution_stage == "solved"
            else ProductRealizationOutputNeed.INTERFACES if result.execution_stage == "interface_constraints_evaluated"
            else ProductRealizationOutputNeed.LOCAL_FRAGMENTS
        )
    actual_fields = (
        result.instance_receipts,
        result.interface_evaluations,
        result.component_project_world_transforms,
        result.interface_project_world_frames,
        result.frame_check_status,
        result.frame_diagnostics,
        result.external_condition_checks,
    )
    if output_need is ProductRealizationOutputNeed.SOLVE_ONLY:
        if any(value not in ((), None) for value in actual_fields):
            raise ValueError("solve-only result cannot contain actual Geometry facts")
    elif solved:
        evaluations = result.interface_evaluations if output_need is ProductRealizationOutputNeed.INTERFACES else result.instance_receipts
        _validate_product_realization_fragment_receipts(
            result.proposal, evaluations,
            require_summary=output_need is ProductRealizationOutputNeed.PREVIEW_WORLD,
        )
        expected_components = {item.component_key for item in request.family_contract.components}
        actual_components = {
            item.component_key for item in result.component_project_world_transforms
        }
        expected_interfaces = {item.interface_key for item in request.family_contract.interfaces}
        actual_interfaces = {item.interface_key for item in result.interface_project_world_frames}
        if actual_components != expected_components or actual_interfaces != expected_interfaces:
            raise ValueError("product realization actual world facts are incomplete")
        if result.frame_check_status is None:
            raise ValueError("actual product realization requires a Backend frame status")


def _validate_product_realization_fragment_receipts(
    proposal, evaluations, *, require_summary: bool = False,
) -> None:
    receipt_by_fragment = {item.fragment_key: item for item in evaluations}
    fragments = {item.fragment_key: item for item in proposal.fragments}
    if len(receipt_by_fragment) != len(evaluations) or set(receipt_by_fragment) != set(fragments):
        raise ValueError("product realization receipts do not match all solved fragments")
    for fragment_key, receipt in receipt_by_fragment.items():
        fragment = fragments[fragment_key]
        if receipt.binding_key != fragment.binding_key:
            raise ValueError("product realization receipt uses the wrong exact binding")
        if canonical_json(receipt.normalized_parameters) != canonical_json(fragment.geometry_parameters):
            raise ValueError("product realization receipt parameters changed after preflight")
        if receipt.uniform_scale != fragment.uniform_scale:
            raise ValueError("product realization receipt scale changed after preflight")
        if require_summary and receipt.geometry_summary is None:
            raise ValueError("preview-world result requires measured fragment summaries")


def _resolve_project_world_facts(
    request: ProductRealizationRequest,
    proposal: SolvedProductRealizationProposal,
    receipts: tuple[ProductGeometryInstanceReceipt, ...],
) -> tuple[
    tuple[ProductRealizationComponentWorldTransform, ...],
    tuple[ProductRealizationInterfaceWorldFrame, ...],
]:
    contract = product_realization_structure(request)
    component_proposals = {item.component_key: item for item in proposal.components}
    component_contracts = {item.component_key: item for item in contract.components}
    resolved: dict[str, RigidTransform3D] = {}

    def resolve(key: str) -> RigidTransform3D:
        current = resolved.get(key)
        if current is not None:
            return current
        definition = component_contracts[key]
        parent = (
            RigidTransform3D(translation_m=request.invocation.product_origin)
            if definition.parent_component_key is None
            else resolve(definition.parent_component_key)
        )
        current = compose_rigid_transform(
            parent,
            component_proposals[key].local_transform,
        )
        resolved[key] = current
        return current

    component_facts = tuple(
        ProductRealizationComponentWorldTransform(
            component_key=item.component_key,
            project_world_transform=resolve(item.component_key),
        )
        for item in contract.components
    )
    interface_facts: list[ProductRealizationInterfaceWorldFrame] = []
    for definition in contract.interfaces:
        component_local, profile = _model_source_component_local(proposal, receipts, definition.source)
        interface_facts.append(
            ProductRealizationInterfaceWorldFrame(
                interface_key=definition.interface_key,
                project_world_frame=compose_rigid_transform(
                    resolve(definition.owner_component_key), component_local
                ),
                profile=profile,
                source=definition.source,
                component_local_frame=component_local,
            )
        )
    return component_facts, tuple(interface_facts)


def _check_realization_frames(
    request: ProductRealizationRequest,
    world_facts: tuple[ProductRealizationInterfaceWorldFrame, ...],
) -> tuple[Literal["PASS", "FAIL"], tuple[ProductRealizationFrameDiagnostic, ...]]:
    contract = request.family_contract
    facts = {item.interface_key: item for item in world_facts}
    diagnostics: list[ProductRealizationFrameDiagnostic] = []
    for definition in contract.interfaces:
        requirement = definition.connection_profile
        if requirement is None:
            continue
        diameter = requirement.outer_diameter_m
        if requirement.outer_diameter_parameter_key is not None:
            diameter = float(request.invocation.public_parameters[requirement.outer_diameter_parameter_key])
        assert diameter is not None
        fact = facts[definition.interface_key]
        diagnostics.append(ProductRealizationFrameDiagnostic(
            check_kind="connection_profile", check_key=definition.interface_key,
            interface_key=definition.interface_key,
            evaluation=evaluate_interface_frame_relation(
                fact.project_world_frame, fact.project_world_frame,
                InterfaceFrameRelation(direction_relation="same", orientation_requirement="axis_only", profile_requirement="equal"),
                position_tolerance_m=request.policy.frame_tolerance_m,
                angular_tolerance_rad=request.policy.angular_tolerance_rad,
                left_profile=CircularInterfaceProfile(diameter_m=diameter), right_profile=fact.profile,
            ),
        ))
    for constraint in contract.constraints:
        left, right = facts[constraint.interface_a_key], facts[constraint.interface_b_key]
        relation = constraint.frame_relation
        evaluation = evaluate_interface_frame_relation(
            left.project_world_frame, right.project_world_frame, relation,
            position_tolerance_m=constraint.position_tolerance_m,
            angular_tolerance_rad=constraint.angular_tolerance_rad,
            left_profile=left.profile, right_profile=right.profile,
        )
        diagnostics.append(ProductRealizationFrameDiagnostic(
            check_kind="interface_coincidence", check_key=constraint.constraint_key,
            interface_key=left.interface_key, peer_interface_key=right.interface_key,
            evaluation=evaluation,
        ))
    return (
        "PASS" if all(item.evaluation.satisfied for item in diagnostics) else "FAIL",
        tuple(diagnostics),
    )


__all__ = [
    'AmbiguousProductRealizationProposal',
    'InfeasibleProductRealizationProposal',
    'MAX_PRODUCT_REALIZATION_RESULT_BYTES',
    'PRODUCT_REALIZATION_ANGLE_TOLERANCE_RAD',
    'PRODUCT_REALIZATION_CPU_SECONDS',
    'PRODUCT_REALIZATION_FRAME_TOLERANCE_M',
    'PRODUCT_REALIZATION_RSS_BYTES',
    'PRODUCT_REALIZATION_SCRATCH_BYTES',
    'PRODUCT_REALIZATION_THREADS',
    'PRODUCT_REALIZATION_WALL_SECONDS',
    'ProductGeometryInstanceReceipt',
    'ProductRealizationAmbiguity',
    'ProductRealizationComponentPlacement',
    'ProductRealizationComponentWorldTransform',
    'ProductRealizationExecutionPolicy',
    'ProductRealizationFrameDiagnostic',
    'ProductRealizationFragmentPlacement',
    'ProductRealizationInterfaceWorldFrame',
    'ProductRealizationInvocation',
    'ProductRealizationOutputNeed',
    'ProductRealizationProposal',
    'ProductRealizationAuthorProposal',
    'ProductRealizationAuthorFragmentPlacement',
    'SolvedProductRealizationAuthorProposal',
    'ProductRealizationGeometryExecution',
    'ProductRealizationRequest',
    'ProductRealizationResult',
    'ProductRealizationStatus',
    'ProductRealizationValidationScenario',
    'ProductRealizationViolation',
    'ResolvedMappedGeometryBinding',
    'ResolvedModelingFunctionBinding',
    'ResolvedProductGeometryBinding',
    'RigidTransform3D',
    'SolvedProductRealizationProposal',
    'compose_rigid_transform',
    'finalize_product_realization',
    'product_realization_binding_digest',
    'product_geometry_binding_digest',
    'rigid_transform_from_axis_reference',
    'resolve_product_realization_world_facts',
    'standard_model_root_transform',
    'validate_product_realization_proposal',
    'validate_product_realization_result_integrity',
    'validate_product_realization_instantiation_result',
]
