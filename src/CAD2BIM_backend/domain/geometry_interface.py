"""Model-owned local interfaces, independent of their eventual family roles."""

from __future__ import annotations

import math
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D, Vector3, compose_rigid_transform


MAX_GEOMETRY_MODEL_INTERFACES = 64
MAX_GEOMETRY_INTERFACE_PROFILE_POINTS = 64
MAX_GEOMETRY_MODEL_INTERFACES_BYTES = 32 * 1024


class _InterfaceValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CircularInterfaceProfile(_InterfaceValue):
    kind: Literal["circular"] = "circular"
    diameter_m: float = Field(gt=0.0, le=1000.0)


class RectangleInterfaceProfile(_InterfaceValue):
    kind: Literal["rectangle"] = "rectangle"
    width_m: float = Field(gt=0.0, le=1000.0)
    height_m: float = Field(gt=0.0, le=1000.0)


class PolygonInterfaceProfile(_InterfaceValue):
    kind: Literal["polygon"] = "polygon"
    points_m: tuple[tuple[float, float], ...] = Field(
        min_length=3, max_length=MAX_GEOMETRY_INTERFACE_PROFILE_POINTS
    )

    @model_validator(mode="after")
    def require_simple_outline(self) -> Self:
        points = self.points_m
        if len(set(points)) != len(points):
            raise ValueError(
                "interface polygon must have distinct vertices without a closing repeat"
            )
        # Translate for numeric stability without changing the author's origin.
        ox, oy = points[0]
        local = tuple((x - ox, y - oy) for x, y in points)
        twice_area = math.fsum(
            a[0] * b[1] - a[1] * b[0] for a, b in zip(local, local[1:] + local[:1], strict=True)
        )
        if not math.isfinite(twice_area) or twice_area == 0.0:
            raise ValueError("interface polygon must have a finite nonzero area")
        edges = tuple(zip(local, local[1:] + local[:1], strict=True))
        for index, (a, b) in enumerate(edges):
            c = local[(index + 2) % len(local)]
            ab, bc = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
            if ab[0] * bc[1] == ab[1] * bc[0] and ab[0] * bc[0] + ab[1] * bc[1] < 0:
                raise ValueError("interface polygon adjacent edges must not overlap")
        for index, (a, b) in enumerate(edges):
            for other, (c, d) in enumerate(edges[index + 1 :], start=index + 1):
                if other == index + 1 or (index == 0 and other == len(edges) - 1):
                    continue
                if _segments_intersect(a, b, c, d):
                    raise ValueError("interface polygon must be a simple outer outline")
        return self


def _segments_intersect(a, b, c, d) -> bool:
    def cross(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def on_segment(p, q, r):
        return min(p[0], q[0]) <= r[0] <= max(p[0], q[0]) and min(p[1], q[1]) <= r[1] <= max(
            p[1], q[1]
        )

    abc, abd, cda, cdb = cross(a, b, c), cross(a, b, d), cross(c, d, a), cross(c, d, b)
    if any(not math.isfinite(value) for value in (abc, abd, cda, cdb)):
        raise ValueError("interface polygon coordinates exceed finite arithmetic")
    return (
        (
            (abc > 0) != (abd > 0)
            and (cda > 0) != (cdb > 0)
            and all(v != 0 for v in (abc, abd, cda, cdb))
        )
        or (abc == 0 and on_segment(a, b, c))
        or (abd == 0 and on_segment(a, b, d))
        or (cda == 0 and on_segment(c, d, a))
        or (cdb == 0 and on_segment(c, d, b))
    )


GeometryInterfaceProfile = Annotated[
    CircularInterfaceProfile | RectangleInterfaceProfile | PolygonInterfaceProfile,
    Field(discriminator="kind"),
]


class InterfaceFrameRelation(_InterfaceValue):
    """One explicit target relation from interface A to interface B."""

    direction_relation: Literal["same", "opposed"]
    orientation_requirement: Literal["axis_only", "full_frame"]
    profile_requirement: Literal["none", "equal"] = "none"
    expected_b_from_a: RigidTransform3D | None = None

    @model_validator(mode="after")
    def validate_relative_rotation(self) -> Self:
        if self.expected_b_from_a is None:
            return self
        baseline = _relation_baseline(self.direction_relation)
        relative_z = _rotation_column(self.expected_b_from_a.rotation, 2)
        baseline_z = _rotation_column(baseline.rotation, 2)
        if not _vectors_close(relative_z, baseline_z):
            raise ValueError("interface relative frame Z axis contradicts its direction relation")
        if self.orientation_requirement == "axis_only" and not _matrices_close(
            self.expected_b_from_a.rotation, baseline.rotation
        ):
            raise ValueError("axis-only interface relation cannot preserve unused clocking")
        return self

    @model_serializer(mode="wrap")
    def serialize_compact(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> dict[str, object]:
        data = handler(self)
        assert isinstance(data, dict)
        if data.get("profile_requirement") == "none":
            data.pop("profile_requirement", None)
        if data.get("expected_b_from_a") is None:
            data.pop("expected_b_from_a", None)
        return data


class InterfaceFrameEvaluation(_InterfaceValue):
    relation: InterfaceFrameRelation
    position_tolerance_m: float = Field(ge=0.0)
    angular_tolerance_rad: float = Field(ge=0.0, le=math.pi)
    origin_distance_m: float = Field(ge=0.0)
    axial_offset_m: float
    lateral_offset_m: float = Field(ge=0.0)
    direction_residual_rad: float = Field(ge=0.0, le=math.pi)
    orientation_residual_rad: float | None = Field(default=None, ge=0.0, le=math.pi)
    profile_satisfied: bool | None = None
    satisfied: bool

    @field_validator(
        "position_tolerance_m",
        "angular_tolerance_rad",
        "origin_distance_m",
        "axial_offset_m",
        "lateral_offset_m",
        "direction_residual_rad",
        "orientation_residual_rad",
    )
    @classmethod
    def require_finite_measurement(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("interface frame evaluation values must be finite")
        return value


def _unit_vector(value: Vector3) -> Vector3:
    length = math.hypot(*value)
    if not math.isfinite(length) or length == 0:
        raise ValueError("interface target direction must be finite and nonzero")
    # A unit vector's norm can differ by one floating-point rounding unit.
    # Preserve it on transport/readback instead of repeatedly dividing and
    # changing the frozen invocation. This is not a geometric tolerance.
    if abs(length - 1.0) <= math.ulp(1.0):
        return tuple(value)
    return tuple(component / length for component in value)


class InterfacePointPosition(_InterfaceValue):
    position_kind: Literal["point"] = "point"
    point_m: Vector3


class InterfaceUnboundedLineExtent(_InterfaceValue):
    extent_kind: Literal["unbounded"] = "unbounded"


class InterfaceBoundedLineExtent(_InterfaceValue):
    extent_kind: Literal["bounded"] = "bounded"
    minimum_m: float
    maximum_m: float

    @model_validator(mode="after")
    def require_ordered_interval(self) -> Self:
        if self.minimum_m > self.maximum_m:
            raise ValueError("interface line extent is inverted")
        return self


class InterfaceLinePosition(_InterfaceValue):
    position_kind: Literal["line"] = "line"
    point_m: Vector3
    direction: Vector3
    extent: Annotated[InterfaceUnboundedLineExtent | InterfaceBoundedLineExtent,
                      Field(discriminator="extent_kind")]

    _normalize_direction = field_validator("direction")(_unit_vector)


class InterfaceFreePosition(_InterfaceValue):
    position_kind: Literal["free"] = "free"


InterfacePositionRequirement = Annotated[
    InterfacePointPosition | InterfaceLinePosition | InterfaceFreePosition,
    Field(discriminator="position_kind"),
]


class InterfaceUnconstrainedDirection(_InterfaceValue):
    direction_kind: Literal["none"] = "none"


class InterfaceAxisDirection(_InterfaceValue):
    direction_kind: Literal["same", "opposed"]
    axis: Vector3

    _normalize_axis = field_validator("axis")(_unit_vector)


InterfaceDirectionRequirement = Annotated[
    InterfaceUnconstrainedDirection | InterfaceAxisDirection,
    Field(discriminator="direction_kind"),
]


class InterfaceUnconstrainedRoll(_InterfaceValue):
    roll_kind: Literal["none"] = "none"


class InterfaceReferenceRoll(_InterfaceValue):
    roll_kind: Literal["reference"] = "reference"
    reference_direction: Vector3

    _normalize_reference = field_validator("reference_direction")(_unit_vector)


InterfaceRollRequirement = Annotated[
    InterfaceUnconstrainedRoll | InterfaceReferenceRoll,
    Field(discriminator="roll_kind"),
]


class InterfaceUnconstrainedProfile(_InterfaceValue):
    profile_kind: Literal["none"] = "none"


class InterfaceEqualProfile(_InterfaceValue):
    profile_kind: Literal["equal"] = "equal"
    profile: GeometryInterfaceProfile


InterfaceProfileRequirement = Annotated[
    InterfaceUnconstrainedProfile | InterfaceEqualProfile,
    Field(discriminator="profile_kind"),
]


class InterfaceExternalCondition(_InterfaceValue):
    """Independent requirements on an actual interface in project-world coordinates."""

    position: InterfacePositionRequirement
    direction: InterfaceDirectionRequirement
    roll: InterfaceRollRequirement
    profile: InterfaceProfileRequirement
    position_tolerance_m: float = Field(gt=0, le=1)
    angular_tolerance_rad: float = Field(gt=0, le=math.pi)

    @model_validator(mode="after")
    def require_defined_roll(self) -> Self:
        if isinstance(self.roll, InterfaceReferenceRoll):
            if not isinstance(self.direction, InterfaceAxisDirection):
                raise ValueError("interface roll requires an explicit target axis")
            if abs(sum(a*b for a, b in zip(self.direction.axis, self.roll.reference_direction))) > 1e-9:
                raise ValueError("interface roll reference must be perpendicular to its target axis")
        return self


class InterfaceExternalEvaluation(_InterfaceValue):
    condition: InterfaceExternalCondition
    position_residual_m: float | None = Field(default=None, ge=0)
    line_projection_m: float | None = None
    line_extent_residual_m: float | None = Field(default=None, ge=0)
    direction_residual_rad: float | None = Field(default=None, ge=0, le=math.pi)
    roll_residual_rad: float | None = Field(default=None, ge=0, le=math.pi)
    profile_satisfied: bool | None = None
    satisfied: bool


def evaluate_interface_external_condition(
    actual: RigidTransform3D,
    condition: InterfaceExternalCondition,
    *,
    actual_profile: GeometryInterfaceProfile | None = None,
) -> InterfaceExternalEvaluation:
    """Measure each declared degree of freedom; free never invents interface facts."""

    position = condition.position
    distance = projection = extent_residual = direction_residual = roll_residual = None
    profile_satisfied = None
    if isinstance(position, (InterfacePointPosition, InterfaceLinePosition)):
        delta = tuple(p-a for p, a in zip(actual.translation_m, position.point_m))
        if isinstance(position, InterfacePointPosition):
            distance = math.hypot(*delta)
        else:
            projection = math.fsum(d*u for d, u in zip(delta, position.direction))
            distance = math.hypot(*(d-projection*u for d, u in zip(delta, position.direction)))
            if isinstance(position.extent, InterfaceBoundedLineExtent):
                extent_residual = max(position.extent.minimum_m-projection,
                                      projection-position.extent.maximum_m, 0.0)
    if isinstance(condition.direction, InterfaceAxisDirection):
        sign = 1 if condition.direction.direction_kind == "same" else -1
        expected_axis = tuple(sign*v for v in condition.direction.axis)
        direction_residual = math.acos(max(-1.0, min(1.0, math.fsum(
            a*b for a, b in zip(expected_axis, _rotation_column(actual.rotation, 2))))))
    if isinstance(condition.roll, InterfaceReferenceRoll):
        roll_residual = math.acos(max(-1.0, min(1.0, math.fsum(
            a*b for a, b in zip(condition.roll.reference_direction, _rotation_column(actual.rotation, 0))))))
    if isinstance(condition.profile, InterfaceEqualProfile):
        profile_satisfied = _profiles_equal(condition.profile.profile, actual_profile)
    satisfied = (
        (distance is None or distance <= condition.position_tolerance_m)
        and (extent_residual is None or extent_residual <= condition.position_tolerance_m)
        and (direction_residual is None or direction_residual <= condition.angular_tolerance_rad)
        and (roll_residual is None or roll_residual <= condition.angular_tolerance_rad)
        and profile_satisfied is not False
    )
    return InterfaceExternalEvaluation(
        condition=condition, position_residual_m=distance, line_projection_m=projection,
        line_extent_residual_m=extent_residual, direction_residual_rad=direction_residual,
        roll_residual_rad=roll_residual, profile_satisfied=profile_satisfied, satisfied=satisfied,
    )


class GeometryModelInterface(_InterfaceValue):
    interface_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    local_frame: RigidTransform3D
    profile: GeometryInterfaceProfile | None = None


def _validate_interfaces(
    values: tuple[GeometryModelInterface, ...],
) -> tuple[GeometryModelInterface, ...]:
    keys = tuple(item.interface_key for item in values)
    if len(set(keys)) != len(keys):
        raise ValueError("model interface keys must be unique")
    if (
        len(canonical_json([item.model_dump(mode="json") for item in values]).encode("utf-8"))
        > MAX_GEOMETRY_MODEL_INTERFACES_BYTES
    ):
        raise ValueError("model interfaces exceed their byte budget")
    return values


GeometryModelInterfaces = Annotated[
    tuple[GeometryModelInterface, ...],
    Field(max_length=MAX_GEOMETRY_MODEL_INTERFACES),
    AfterValidator(_validate_interfaces),
]


def resolve_fixed_geometry_interfaces(
    declared: GeometryModelInterfaces,
    built: GeometryModelInterfaces,
) -> GeometryModelInterfaces:
    """Use one consistent fixed-model annotation in Preview and publication."""
    if declared and built and declared != built:
        raise ValueError("Declared Geometry interfaces differ from the actual fixed build")
    return declared or built


_GEOMETRY_MODEL_INTERFACES_ADAPTER = TypeAdapter(GeometryModelInterfaces)


def _rotation_column(rotation, column: int) -> tuple[float, float, float]:
    return tuple(rotation[row][column] for row in range(3))


def _vectors_close(left, right) -> bool:
    return all(
        math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9) for a, b in zip(left, right, strict=True)
    )


def _matrices_close(left, right) -> bool:
    return all(
        _vectors_close(left_row, right_row) for left_row, right_row in zip(left, right, strict=True)
    )


def _relation_baseline(direction_relation: Literal["same", "opposed"]) -> RigidTransform3D:
    if direction_relation == "same":
        return RigidTransform3D()
    return RigidTransform3D(rotation=((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0)))


def _rotation_residual(left, right) -> float:
    trace = sum(left[row][column] * right[row][column] for row in range(3) for column in range(3))
    return math.acos(max(-1.0, min(1.0, (trace - 1.0) / 2.0)))


def _profiles_equal(
    left: GeometryInterfaceProfile | None,
    right: GeometryInterfaceProfile | None,
) -> bool:
    if left is None or right is None or left.kind != right.kind:
        return False
    if isinstance(left, CircularInterfaceProfile) and isinstance(right, CircularInterfaceProfile):
        return math.isclose(left.diameter_m, right.diameter_m, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(left, RectangleInterfaceProfile) and isinstance(right, RectangleInterfaceProfile):
        return math.isclose(
            left.width_m, right.width_m, rel_tol=1e-9, abs_tol=1e-9
        ) and math.isclose(left.height_m, right.height_m, rel_tol=1e-9, abs_tol=1e-9)
    assert isinstance(left, PolygonInterfaceProfile)
    assert isinstance(right, PolygonInterfaceProfile)
    if len(left.points_m) != len(right.points_m):
        return False

    def point_matches(a, b) -> bool:
        return _vectors_close((*a, 0.0), (*b, 0.0))

    count = len(left.points_m)
    return any(
        all(
            point_matches(left.points_m[index], right.points_m[(offset + step * index) % count])
            for index in range(count)
        )
        for offset in range(count)
        for step in (1, -1)
    )


def evaluate_interface_frame_relation(
    left: RigidTransform3D,
    right: RigidTransform3D,
    relation: InterfaceFrameRelation,
    *,
    position_tolerance_m: float,
    angular_tolerance_rad: float,
    left_profile: GeometryInterfaceProfile | None = None,
    right_profile: GeometryInterfaceProfile | None = None,
) -> InterfaceFrameEvaluation:
    if not math.isfinite(position_tolerance_m) or position_tolerance_m < 0.0:
        raise ValueError("interface position tolerance must be finite and nonnegative")
    if (
        not math.isfinite(angular_tolerance_rad)
        or angular_tolerance_rad < 0.0
        or angular_tolerance_rad > math.pi
    ):
        raise ValueError("interface angular tolerance must be finite and within [0, pi]")
    relative = relation.expected_b_from_a or _relation_baseline(relation.direction_relation)
    expected = compose_rigid_transform(left, relative)
    offset = tuple(right.translation_m[index] - expected.translation_m[index] for index in range(3))
    if not all(math.isfinite(value) for value in offset):
        raise ValueError("interface frame offset exceeds finite arithmetic")
    try:
        origin_distance = math.sqrt(
            sum(
                (expected.translation_m[index] - right.translation_m[index]) ** 2
                for index in range(3)
            )
        )
    except OverflowError as exc:
        raise ValueError("interface frame distance exceeds finite arithmetic") from exc
    left_z = _rotation_column(left.rotation, 2)
    axial_offset = sum(value * axis for value, axis in zip(offset, left_z, strict=True))
    lateral_offset = math.sqrt(max(0.0, origin_distance**2 - axial_offset**2))
    if not all(math.isfinite(value) for value in (origin_distance, axial_offset, lateral_offset)):
        raise ValueError("interface frame distance exceeds finite arithmetic")
    expected_z = _rotation_column(expected.rotation, 2)
    right_z = _rotation_column(right.rotation, 2)
    direction_residual = math.acos(
        max(
            -1.0,
            min(
                1.0,
                sum(a * b for a, b in zip(expected_z, right_z, strict=True)),
            ),
        )
    )
    orientation_residual = (
        _rotation_residual(expected.rotation, right.rotation)
        if relation.orientation_requirement == "full_frame"
        else None
    )
    profile_satisfied = (
        _profiles_equal(left_profile, right_profile)
        if relation.profile_requirement == "equal"
        else None
    )
    satisfied = (
        origin_distance <= position_tolerance_m
        and direction_residual <= angular_tolerance_rad
        and (orientation_residual is None or orientation_residual <= angular_tolerance_rad)
        and profile_satisfied is not False
    )
    return InterfaceFrameEvaluation(
        relation=relation,
        position_tolerance_m=position_tolerance_m,
        angular_tolerance_rad=angular_tolerance_rad,
        origin_distance_m=origin_distance,
        axial_offset_m=axial_offset,
        lateral_offset_m=lateral_offset,
        direction_residual_rad=direction_residual,
        orientation_residual_rad=orientation_residual,
        profile_satisfied=profile_satisfied,
        satisfied=satisfied,
    )


def scale_geometry_interfaces(
    interfaces: GeometryModelInterfaces,
    uniform_scale: float,
) -> GeometryModelInterfaces:
    if not math.isfinite(uniform_scale) or uniform_scale <= 0.0:
        raise ValueError("geometry interface uniform scale must be finite and positive")
    validated = _GEOMETRY_MODEL_INTERFACES_ADAPTER.validate_python(interfaces)
    scaled: list[GeometryModelInterface] = []
    for item in validated:
        profile = item.profile
        if isinstance(profile, CircularInterfaceProfile):
            profile = CircularInterfaceProfile(diameter_m=profile.diameter_m * uniform_scale)
        elif isinstance(profile, RectangleInterfaceProfile):
            profile = RectangleInterfaceProfile(
                width_m=profile.width_m * uniform_scale,
                height_m=profile.height_m * uniform_scale,
            )
        elif isinstance(profile, PolygonInterfaceProfile):
            profile = PolygonInterfaceProfile(
                points_m=tuple((x * uniform_scale, y * uniform_scale) for x, y in profile.points_m)
            )
        scaled.append(
            GeometryModelInterface(
                interface_key=item.interface_key,
                local_frame=RigidTransform3D(
                    translation_m=tuple(
                        value * uniform_scale for value in item.local_frame.translation_m
                    ),
                    rotation=item.local_frame.rotation,
                ),
                profile=profile,
            )
        )
    return _GEOMETRY_MODEL_INTERFACES_ADAPTER.validate_python(tuple(scaled))
