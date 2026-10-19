"""CONNECTOR: finite two-source placement and one exact geometry fragment."""

import math

from .local_geometry_frame import frame, point_in_frame, direction_in_frame

from .shared_solver import (
    _add, _canonical_opposed_axes, _cross, _dot, _failure, _finite_vector, _frame, _mul, _norm,
    _source, _sub, _unit, nominal_node, nominal_targets, with_construction,
    realize as solve_bend, standard_model as standard_bend,
)


def _direct(ctx, a, b, raw_a, raw_b, *, standard):
    origin = ctx.product_origin()
    if not _finite_vector(origin):
        return _failure("origin", "Product origin must be finite")
    ua, ub = a["u"], b["u"]
    turn = math.atan2(_norm(_cross(ua, ub)), max(-1.0, min(1.0, -_dot(ua, ub))))
    if turn != 0:
        return _failure("unsupported_domain", "Direct sleeve requires exactly opposite source axes")
    if _dot(ua, ub) >= 0:
        return _failure("unsupported_domain", "Same-direction sources cannot form a direct sleeve")
    c = a["a"]
    if _norm(_cross(_sub(b["a"], c), ua)) > 1e-6:
        return _failure("unsupported_axes", "Direct source axes are offset")
    coordinate_scale = max(1.0, *(abs(x) for p in (a["a"], a["b"], b["a"], b["b"]) for x in p))
    if 128 * 2.220446049250313e-16 * coordinate_scale > 1e-6 / 8:
        return _failure("numeric_capacity", "Source coordinates cannot resolve the position tolerance", capacity=True)
    bounds = []
    for side in (a, b):
        low = _dot(_sub(side["a"], c), side["u"])
        high = _dot(_sub(side["b"], c), side["u"])
        if not all(math.isfinite(x) for x in (low, high)) or high <= low:
            return _failure("numeric_capacity", "Finite source window cannot be resolved", capacity=True)
        bounds.append((low, high))
    da, db = a["od"], b["od"]
    d = max(da, db)
    if 0.07 * min(da, db) <= 1e-6:
        return _failure("numeric_capacity", "Sleeve wall is below the BREP resolution margin", capacity=True)
    # Two shoulders occupy 0.16L each; the central S occupies 0.52L.
    # Their radial rises give open physical lower bounds on L. The BREP land
    # resolution is a separate technical capacity limit, not physical fit.
    rise = abs(da - db) / 2
    physical_floor = max(0.10 * d / 0.16, rise / 0.52)
    window_floor = bounds[0][0] + bounds[1][0]
    upper = bounds[0][1] + bounds[1][1]
    if not all(math.isfinite(x) for x in (physical_floor, window_floor, upper)):
        return _failure("numeric_capacity", "Sleeve length interval exceeds numeric capacity", capacity=True)
    if upper <= physical_floor or window_floor > upper:
        return nominal_node(a, b, c, origin, standard, "minimum_body_exceeds_windows")
    shape_lower = physical_floor * (1 + 1e-6)
    technical_lower = 1e-5 / 0.08
    lower = max(shape_lower, window_floor, technical_lower)
    if lower > upper:
        return _failure("numeric_capacity", "Physical sleeve may fit, but BREP land resolution or foldback margin is insufficient", capacity=True)
    length = min(max(1.54 * d, lower), upper)
    axis = _mul(ua, -1.0)
    lo = max(-bounds[0][1], bounds[1][0] - length)
    hi = min(-bounds[0][0], bounds[1][1] - length)
    if lo > hi:
        return nominal_node(a, b, c, origin, standard, "body_outside_both_windows")
    x = lo
    port_a = _add(c, _mul(axis, x))
    port_b = _add(port_a, _mul(axis, length))
    for side, port in ((raw_a, port_a), (raw_b, port_b)):
        displacement = _sub(port, side["a"])
        station = _dot(displacement, side["u"])
        if _norm(_sub(displacement, _mul(side["u"], station))) > 1e-6:
            return _failure("unsupported_axes", "Solved port is outside its actual source axis")
        if not -1e-6 <= station <= side["length"] + 1e-6:
            return _failure("no_finite_fit", "Solved port is outside a finite source window")
    reference = c if standard else origin
    local_a, local_b = (_sub(p, reference) for p in (port_a, port_b))
    frame_x = _frame(axis)
    reference_a, reference_b = _frame(ua), _frame(ub)
    if any(value is None for value in (frame_x, reference_a, reference_b)):
        return _failure("numeric_capacity", "Cannot construct direct sleeve frame", capacity=True)
    ports = {
        "side_a": {"origin_m": local_a, "axis": ua, "reference_direction": reference_a, "outer_diameter_m": da},
        "side_b": {"origin_m": local_b, "axis": ub, "reference_direction": reference_b, "outer_diameter_m": db},
    }
    placement = frame(local_a, axis, frame_x)
    for port in ports.values():
        port["origin_m"] = point_in_frame(port["origin_m"], placement)
        port["axis"] = direction_in_frame(port["axis"], placement)
        port["reference_direction"] = direction_in_frame(port["reference_direction"], placement)
    internal = {"ports": ports, "shape": {"mode": "straight", "frame_x": direction_in_frame(frame_x, placement),
        "body_outer_diameter_m": 1.2 * d, "sleeve_length_m": length}}
    return {"status": "SOLVED", "internal_parameters": internal,
            "components": [{"component_key": "connector"}],
            "fragments": [{"fragment_key": "connector_body", "binding_key": "connector_body_geometry",
                            "local_transform": placement, "uniform_scale": 1.0}]}


def _solve(ctx, *, standard):
    targets = nominal_targets(0.2, 0.08) if standard else None
    a = _source(ctx, "a", targets)
    if "status" in a:
        return a
    b = _source(ctx, "b", targets)
    if "status" in b:
        return b
    raw_a, raw_b = a, b
    if a["a"] == b["a"]:
        coordinate_scale = max(1.0, *(abs(x) for p in (a["a"], a["b"], b["a"], b["b"]) for x in p))
        scale = max(coordinate_scale, a["length"], b["length"], a["od"], b["od"])
        canonical = _canonical_opposed_axes(a, b, a["a"], 128 * 2.220446049250313e-16 * scale)
        if canonical is not None:
            a, b = canonical
    turn = math.atan2(_norm(_cross(a["u"], b["u"])),
                      max(-1.0, min(1.0, -_dot(a["u"], b["u"]))))
    if turn == 0:
        proposal = _direct(ctx, a, b, raw_a, raw_b, standard=standard)
    else:
        proposal = (standard_bend(ctx, layout_fallback=nominal_node) if standard
                    else solve_bend(ctx, layout_fallback=nominal_node))
    if proposal["status"] == "SOLVED":
        proposal["components"][0]["component_key"] = "connector"
        proposal["fragments"][0]["fragment_key"] = "connector_body"
        if proposal["fragments"][0]["binding_key"] != "sphere_geometry":
            proposal["fragments"][0]["binding_key"] = "connector_body_geometry"
    else:
        for issue in proposal.get("violations", []):
            issue["message"] = issue["message"].replace("BEND", "CONNECTOR")
    return with_construction(proposal)


def realize(ctx):
    return _solve(ctx, standard=False)


def standard_model(ctx):
    return _solve(ctx, standard=True)
