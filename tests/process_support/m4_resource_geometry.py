"""Pure, metre-based geometry planning used by the private M4 compiler.

This is a local adaptation of the separately reviewed Golden resource equations.
It has no CAD, topology, catalog, MCP, or repository-discovery dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Sequence


Vector = tuple[float, float, float]
_EPSILON = 1.0e-12


class ResourceGeometryError(ValueError):
    """The submitted, already-decided resource inputs cannot form the shape."""


@dataclass(frozen=True, slots=True)
class PlannedPort:
    source_index: int
    point_m: Vector
    outward_direction: Vector
    outer_diameter_m: float


@dataclass(frozen=True, slots=True)
class ElbowPlan:
    ports: tuple[PlannedPort, PlannedPort]
    centerline_m: tuple[Vector, ...]
    fitting_diameter_m: float


@dataclass(frozen=True, slots=True)
class TeePlan:
    ports: tuple[PlannedPort, PlannedPort, PlannedPort]
    main_centerline_m: tuple[Vector, ...]
    branch_centerline_m: tuple[Vector, ...]
    fitting_diameter_m: float


def _add(left: Vector, right: Vector) -> Vector:
    return tuple(a + b for a, b in zip(left, right))  # type: ignore[return-value]


def _sub(left: Vector, right: Vector) -> Vector:
    return tuple(a - b for a, b in zip(left, right))  # type: ignore[return-value]


def _scale(value: Vector, factor: float) -> Vector:
    return tuple(component * factor for component in value)  # type: ignore[return-value]


def _dot(left: Vector, right: Vector) -> float:
    return sum(a * b for a, b in zip(left, right))


def _cross(left: Vector, right: Vector) -> Vector:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _length(value: Vector) -> float:
    return math.sqrt(_dot(value, value))


def unit(value: Sequence[float]) -> Vector:
    if len(value) != 3:
        raise ResourceGeometryError("a direction requires three components")
    vector = tuple(float(component) for component in value)
    magnitude = _length(vector)  # type: ignore[arg-type]
    if not math.isfinite(magnitude) or magnitude <= _EPSILON:
        raise ResourceGeometryError("a direction must be finite and non-zero")
    return _scale(vector, 1.0 / magnitude)  # type: ignore[arg-type]


def _positive(value: float, label: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ResourceGeometryError(f"{label} must be finite and positive")
    return result


def _contract_number(contract: Mapping[str, Any], name: str) -> float:
    value = contract.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ResourceGeometryError(f"realization contract field {name!r} must be finite")
    return float(value)


def _arc(start: Vector, middle: Vector, end: Vector, segments: int) -> tuple[Vector, ...]:
    first, second = _sub(middle, start), _sub(end, start)
    normal_raw = _cross(first, second)
    normal_sq = _dot(normal_raw, normal_raw)
    if normal_sq <= 1.0e-18:
        return (start, middle, end)
    offset = _scale(
        _add(
            _scale(_cross(second, normal_raw), _dot(first, first)),
            _scale(_cross(normal_raw, first), _dot(second, second)),
        ),
        1.0 / (2.0 * normal_sq),
    )
    center = _add(start, offset)
    normal = unit(normal_raw)
    x_axis = unit(_sub(start, center))
    y_axis = _cross(normal, x_axis)

    def angle(point: Vector) -> float:
        relative = _sub(point, center)
        return math.atan2(_dot(relative, y_axis), _dot(relative, x_axis))

    start_angle = angle(start)
    middle_angle = (angle(middle) - start_angle) % (2.0 * math.pi)
    end_angle = (angle(end) - start_angle) % (2.0 * math.pi)
    sweep = end_angle - 2.0 * math.pi if middle_angle > end_angle else end_angle
    radius = _length(_sub(start, center))
    return tuple(
        _add(
            center,
            _add(
                _scale(x_axis, radius * math.cos(start_angle + sweep * index / segments)),
                _scale(y_axis, radius * math.sin(start_angle + sweep * index / segments)),
            ),
        )
        for index in range(segments + 1)
    )


def plan_elbow(
    *,
    inward_directions: Sequence[Sequence[float]],
    outer_diameters_m: Sequence[float],
    realization_contract: Mapping[str, Any],
    should_extend: bool = False,
) -> ElbowPlan:
    """Plan the approved two-port resource around local origin.

    Directions point from each interface toward the fitting origin. Returned
    port directions point outward and the centreline includes both interfaces.
    """

    if (
        realization_contract.get("declared_port_count") != 2
        or len(inward_directions) != 2
        or len(outer_diameters_m) != 2
    ):
        raise ResourceGeometryError("an elbow requires two directions and diameters")
    inward = tuple(unit(value) for value in inward_directions)
    outward = tuple(_scale(value, -1.0) for value in inward)
    diameters = tuple(_positive(value, "outer diameter") for value in outer_diameters_m)
    cosine = max(-1.0, min(1.0, _dot(outward[0], outward[1])))
    angle = math.acos(cosine)
    minimum_angle = _contract_number(realization_contract, "minimum_angle_degrees")
    if math.degrees(angle) + 1.0e-9 < minimum_angle:
        raise ResourceGeometryError(
            f"elbow angle is below the approved {minimum_angle:g} degree minimum"
        )

    takeoff = realization_contract.get("canonical_takeoff")
    if not isinstance(takeoff, Mapping):
        raise ResourceGeometryError("bend canonical_takeoff contract is required")
    diameter_scale = _contract_number(takeoff, "diameter_scale")
    straight_bias = _contract_number(takeoff, "straight_bias_factor")
    straight_cap = _contract_number(takeoff, "straight_cap_factor")
    bend_angle_factor = _contract_number(takeoff, "bend_angle_factor")
    bend_bias = _contract_number(takeoff, "bend_bias_factor")
    extension_factor = _contract_number(takeoff, "extension_factor")
    scaled = tuple(value * diameter_scale for value in diameters)
    fitting_diameter = max(scaled)
    straight = tuple(
        min(
            fitting_diameter * (fitting_diameter - value)
            / max(sum(scaled), _EPSILON)
            + fitting_diameter * straight_bias,
            fitting_diameter * straight_cap,
        )
        for value in scaled
    )
    bend_length = (
        fitting_diameter * bend_angle_factor * (math.pi - angle)
        + bend_bias * fitting_diameter
    )
    if should_extend:
        straight = tuple(value + fitting_diameter * extension_factor for value in straight)
        bend_length += fitting_diameter * extension_factor
    takeoffs = tuple(bend_length + value for value in straight)
    port_points = tuple(_scale(outward[index], takeoffs[index]) for index in range(2))
    arc_leg = bend_length * 0.9
    arc_start, arc_end = (_scale(direction, arc_leg) for direction in outward)
    summed = _add(outward[0], outward[1])
    if math.isclose(angle, math.pi, abs_tol=1.0e-9) or _length(summed) <= 1.0e-9:
        arc_middle = (0.0, 0.0, 0.0)
    else:
        middle_direction = unit(summed)
        middle_length = arc_leg * (1.0 / math.cos(angle / 2.0) - math.tan(angle / 2.0))
        arc_middle = _scale(middle_direction, middle_length)
    segments = (int(max(1, 16 - (angle // (math.pi / 4.0)) ** 2)) + 2
                if angle >= math.pi / 2.0 else 6)
    centerline = (port_points[0], *_arc(arc_start, arc_middle, arc_end, segments), port_points[1])
    return ElbowPlan(
        ports=tuple(
            PlannedPort(index, port_points[index], outward[index], diameters[index])
            for index in range(2)
        ),  # type: ignore[arg-type]
        centerline_m=_deduplicate(centerline),
        fitting_diameter_m=fitting_diameter,
    )


def plan_tee(
    *,
    inward_directions: Sequence[Sequence[float]],
    outer_diameters_m: Sequence[float],
    realization_contract: Mapping[str, Any],
    bend_realization_contract: Mapping[str, Any],
) -> TeePlan:
    """Plan the approved three-port tee without choosing any business meaning."""

    if (
        realization_contract.get("declared_port_count") != 3
        or len(inward_directions) != 3
        or len(outer_diameters_m) != 3
    ):
        raise ResourceGeometryError("a tee requires three directions and diameters")
    inward = tuple(unit(value) for value in inward_directions)
    diameters = tuple(_positive(value, "outer diameter") for value in outer_diameters_m)
    pairs = ((0, 1), (0, 2), (1, 2))
    main = min(pairs, key=lambda pair: (_dot(inward[pair[0]], inward[pair[1]]), pair))
    branch = next(index for index in range(3) if index not in main)
    elbow = plan_elbow(
        inward_directions=(inward[main[0]], inward[main[1]]),
        outer_diameters_m=(diameters[main[0]], diameters[main[1]]),
        realization_contract=bend_realization_contract,
        should_extend=True,
    )
    branch_outward = _scale(inward[branch], -1.0)
    takeoff = realization_contract.get("branch_takeoff")
    if not isinstance(takeoff, Mapping):
        raise ResourceGeometryError("tee branch_takeoff contract is required")
    branch_scaled = diameters[branch] * _contract_number(
        takeoff, "branch_diameter_scale"
    )
    main_diameter = max(diameters) * _contract_number(
        takeoff, "main_diameter_scale"
    )
    stub_length = elbow.fitting_diameter_m * _contract_number(takeoff, "stub_factor")
    branch_straight = (
        _contract_number(takeoff, "branch_difference_factor")
        * main_diameter * (main_diameter - branch_scaled)
        / max(main_diameter + diameters[branch], _EPSILON)
        + main_diameter * _contract_number(takeoff, "branch_bias_factor")
    )
    branch_point = _scale(branch_outward, stub_length + branch_straight)
    main_ports = {
        main[index]: PlannedPort(
            main[index], elbow.ports[index].point_m, elbow.ports[index].outward_direction,
            diameters[main[index]],
        )
        for index in range(2)
    }
    main_ports[branch] = PlannedPort(branch, branch_point, branch_outward, diameters[branch])
    branch_line = _deduplicate(((0.0, 0.0, 0.0), _scale(branch_outward, stub_length), branch_point))
    return TeePlan(
        ports=tuple(main_ports[index] for index in range(3)),  # type: ignore[arg-type]
        main_centerline_m=elbow.centerline_m,
        branch_centerline_m=branch_line,
        fitting_diameter_m=max(elbow.fitting_diameter_m, main_diameter),
    )


def _deduplicate(points: Sequence[Vector]) -> tuple[Vector, ...]:
    result: list[Vector] = []
    for point in points:
        if not result or math.dist(result[-1], point) > 1.0e-9:
            result.append(point)
    if len(result) < 2:
        raise ResourceGeometryError("resource centreline collapsed")
    return tuple(result)
