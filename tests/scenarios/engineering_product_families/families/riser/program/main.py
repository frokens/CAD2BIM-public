"""Pure numeric RISER authoring from fixed external bottom and top targets."""

import math


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _infeasible(key, message):
    return {"status": "INFEASIBLE", "violations": [{"violation_key": key, "message": message}]}


def _capacity(message):
    return {"status": "AMBIGUOUS", "ambiguities": [
        {"ambiguity_key": "numeric_capacity", "message": message}
    ]}


def _unit(values):
    # Scaling first avoids overflow/underflow in the norm of finite source vectors.
    scale = max(abs(value) for value in values)
    if scale == 0:
        return None
    scaled = [value / scale for value in values]
    norm = math.hypot(*scaled)
    return [value / norm for value in scaled]


def _frame(axis):
    preferred = [0.0, 0.0, 1.0]
    projection = [preferred[i] - _dot(preferred, axis) * axis[i] for i in range(3)]
    if math.hypot(*projection) <= 32 * math.sqrt(2.220446049250313e-16):
        preferred = [1.0, 0.0, 0.0]
        projection = [preferred[i] - _dot(preferred, axis) * axis[i] for i in range(3)]
    x = _unit(projection)
    y = _unit(_cross(axis, x))
    return x, y


def _solve(ctx, targets, *, standard):
    if len(targets) != 2 or any(target["condition"]["kind"] != "fixed_point" for target in targets):
        return _infeasible("fixed_endpoints", "RISER requires fixed bottom and top conditions")
    by_key = {target["interface_key"]: target["condition"] for target in targets}
    if "bottom" not in by_key or "top" not in by_key:
        return _infeasible("fixed_endpoints", "RISER requires fixed bottom and top conditions")
    bottom_target, top_target = by_key["bottom"], by_key["top"]
    bottom, world_top = bottom_target["point_m"], top_target["point_m"]
    direction = [world_top[i] - bottom[i] for i in range(3)]
    height = math.hypot(*direction)
    diameter = bottom_target.get("outer_diameter_m")
    if diameter is None or top_target.get("outer_diameter_m") != diameter:
        return _infeasible("endpoint_diameter", "RISER endpoints require one equal outside diameter")
    section = ctx.param("section")
    origin = ctx.product_origin()

    if any(not math.isfinite(value) for value in bottom):
        return _infeasible("bottom_finite", "RISER bottom coordinates must be finite")
    if any(not math.isfinite(value) for value in direction):
        return _capacity("Fixed endpoint difference exceeds finite numeric capacity")
    if any(not math.isfinite(value) for value in origin):
        return _infeasible("origin_finite", "RISER product origin coordinates must be finite")
    if not math.isfinite(height):
        return _capacity("Fixed endpoint distance exceeds finite numeric capacity")
    if height <= 0:
        return _infeasible("height", "RISER actual height must be finite and positive")
    if not math.isfinite(diameter) or diameter <= 0:
        return _infeasible("outer_diameter", "RISER outside diameter must be finite and positive")
    thickness_values = section["known_wall_thickness_m"]
    if len(thickness_values) > 1:
        return _infeasible("section", "RISER accepts at most one known wall thickness")
    if thickness_values and (not math.isfinite(thickness_values[0])
                             or not 0 < thickness_values[0] < diameter / 2):
        return _infeasible("wall_thickness", "Known wall thickness must be below the outer radius")

    axis = _unit(direction)
    if axis is None:
        return _infeasible("zero_direction", "RISER requires a nonzero source direction")
    for target, actual_axis in ((bottom_target, [-value for value in axis]), (top_target, axis)):
        target_axis = _unit(target["axis"])
        relation = target.get("direction_relation", "opposed")
        if target_axis is None or relation not in ("same", "opposed"):
            return _infeasible("endpoint_direction", "RISER endpoint direction is invalid")
        sign = 1 if relation == "same" else -1
        aligned = [sign * value for value in target_axis]
        angle = math.atan2(math.hypot(*_cross(actual_axis, aligned)), _dot(actual_axis, aligned))
        if angle > target.get("angular_tolerance_rad", 1e-6):
            return _infeasible("endpoint_direction", "RISER fixed axis differs from its actual outward endpoint axis")
    frame_x, frame_y = _frame(axis)
    displacement = [height * value for value in axis]
    if standard:
        local_bottom = [0.0, 0.0, 0.0]
        local_top = displacement
    else:
        if any(not math.isfinite(value) for value in world_top):
            return _capacity("World top arithmetic exceeds finite numeric capacity")
        if world_top == bottom:
            return _capacity("Positive actual height collapses at source-coordinate resolution")
        local_bottom = [bottom[i] - origin[i] for i in range(3)]
        local_top = [world_top[i] - origin[i] for i in range(3)]
    if any(not math.isfinite(value) for point in (local_bottom, local_top) for value in point):
        return _capacity("World-to-local subtraction exceeds finite numeric capacity")
    local_displacement = [local_top[i] - local_bottom[i] for i in range(3)]
    if any(not math.isfinite(value) for value in local_displacement):
        return _capacity("Local endpoint difference exceeds finite numeric capacity")
    local_length = math.hypot(*local_displacement)
    if not math.isfinite(local_length) or not math.isclose(local_length, height, rel_tol=1e-12, abs_tol=0):
        return _capacity("Endpoint rounding cannot preserve the supplied axial height")
    if _dot(local_displacement, axis) <= 0:
        return _capacity("Endpoint rounding cannot preserve the supplied direction")

    placement = {"translation_m": local_bottom,
                 "rotation": [[frame_x[i], frame_y[i], axis[i]] for i in range(3)]}
    internal = {
        "local_bottom_m": [0.0, 0.0, 0.0], "local_top_m": [0.0, 0.0, height],
        "axis_unit": [0.0, 0.0, 1.0], "frame_x": [1.0, 0.0, 0.0], "frame_y": [0.0, 1.0, 0.0],
        "bottom_outward_axis": [0.0, 0.0, -1.0], "top_outward_axis": [0.0, 0.0, 1.0],
        "height_m": height, "outer_diameter_m": diameter,
    }
    return {
        "status": "SOLVED", "internal_parameters": internal,
        "components": [{"component_key": "riser"}],
        "fragments": [{"fragment_key": "riser_body", "binding_key": "riser_body_geometry", "local_transform": placement, "uniform_scale": 1.0}],
    }


def realize(ctx):
    return _solve(ctx, ctx.external_constraints(), standard=False)


def standard_model(ctx):
    targets = [
        {"interface_key": "bottom", "condition": {"kind": "fixed_point", "point_m": [0, 0, 0],
         "axis": [0, 0, 1], "outer_diameter_m": 0.25}},
        {"interface_key": "top", "condition": {"kind": "fixed_point", "point_m": [0, 0, 3],
         "axis": [0, 0, -1], "outer_diameter_m": 0.25}},
    ]
    return _solve(ctx, targets, standard=True)
