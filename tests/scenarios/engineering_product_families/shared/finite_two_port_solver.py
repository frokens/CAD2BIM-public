"""Numerical two finite segment solver for the BEND family. No CAD imports."""

import math

from .local_geometry_frame import localize_two_port


_EPS = 2.220446049250313e-16
_POSITION_TOLERANCE_M = 1e-6


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _add(a, b):
    return [x + y for x, y in zip(a, b)]


def _sub(a, b):
    return [x - y for x, y in zip(a, b)]


def _mul(a, s):
    return [x * s for x in a]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _norm(a):
    return math.hypot(*a)


def _unit(a):
    scale = max(abs(x) for x in a)
    if scale == 0:
        return None
    scaled = [x / scale for x in a]
    n = _norm(scaled)
    return [x / n for x in scaled]


def _failure(key, message, *, capacity=False):
    if capacity:
        return {"status": "AMBIGUOUS", "ambiguities": [
            {"ambiguity_key": key, "message": message}]}
    return {"status": "INFEASIBLE", "violations": [
        {"violation_key": key, "message": message}]}


def _finite_vector(value):
    return len(value) == 3 and all(math.isfinite(x) for x in value)


def nominal_targets(diameter_a=0.02, diameter_b=0.02):
    """Product-owned nominal layout; standard_model needs no caller targets."""
    return [{"interface_key": "side_" + side, "condition": {
        "kind": "finite_segment", "start_m": [0.0, 0.0, 0.0],
        "end_m": [end, 0.0, 0.0], "outer_diameter_m": diameter,
        "direction_relation": "same"}}
        for side, end, diameter in (("a", -4.0, diameter_a), ("b", 4.0, diameter_b))]


def _source(ctx, side, targets=None):
    conditions = ctx.external_constraints() if targets is None else targets
    target = next((item["condition"] for item in conditions
                   if item["interface_key"] == "side_" + side), None)
    if target is None or target["kind"] != "finite_segment":
        return _failure("source_condition", f"Side {side} requires a finite_segment condition")
    if target["direction_relation"] == "opposed":
        return _failure("source_direction", f"Side {side} port follows its outward finite segment")
    a, b = target["start_m"], target["end_m"]
    diameter = target["outer_diameter_m"]
    if not all(_finite_vector(p) for p in (a, b)):
        return _failure("source_coordinates", f"Side {side} endpoints must be finite 3D coordinates")
    if not math.isfinite(diameter) or diameter <= 0:
        return _failure("source_od", f"Side {side} OD must be finite and positive")
    delta = _sub(b, a)
    if not all(math.isfinite(x) for x in delta):
        return _failure("numeric_capacity", "Source subtraction exceeds finite arithmetic", capacity=True)
    length = _norm(delta)
    if not math.isfinite(length):
        return _failure("numeric_capacity", "Source length exceeds finite arithmetic", capacity=True)
    unit = _unit(delta)
    if unit is None:
        return _failure("degenerate_source", f"Side {side} has coincident endpoints")
    if _add(a, _mul(unit, length)) == a:
        return _failure("numeric_capacity", "Source direction collapses at coordinate resolution", capacity=True)
    return {"a": a, "b": b, "u": unit, "od": float(diameter), "length": length}


def _frame(axis):
    reference = [0.0, 0.0, 1.0]
    projection = _sub(reference, _mul(axis, _dot(reference, axis)))
    if _norm(projection) < 32 * math.sqrt(_EPS):
        reference = [1.0, 0.0, 0.0]
        projection = _sub(reference, _mul(axis, _dot(reference, axis)))
    return _unit(projection)


def _canonical_opposed_axes(a, b, c, arithmetic_error):
    """One axis expression inside the existing coordinate arithmetic budget.

    The caller owns the established connection C. Original endpoints and
    source dictionaries stay unchanged for its final finite-window checks.
    """
    if (not math.isfinite(arithmetic_error) or arithmetic_error < 0
            or arithmetic_error > _POSITION_TOLERANCE_M / 8
            or _dot(a["u"], b["u"]) >= 0):
        return None
    axis = _unit(_sub(a["b"], b["b"]))
    if axis is None or not _finite_vector(axis) or _dot(axis, a["u"]) <= 0:
        return None
    for side in (a, b):
        for point in (side["a"], side["b"]):
            if _norm(_cross(_sub(point, c), axis)) > arithmetic_error:
                return None
        if _norm(_cross(side["u"], axis)) * side["length"] > arithmetic_error:
            return None
    return dict(a, u=axis), dict(b, u=_mul(axis, -1.0))


def _transition(body_d, end_d):
    # Quintic smoothstep has maximum meridian slope 1.875*radial delta / length.
    return max(0.1 * body_d, 1.875 * abs(body_d - end_d) / 2)


def nominal_node(a, b, station, origin, standard, reason):
    """Explicit opt-in layout callback with two independent source-A ports."""
    ports = {}
    for key, side in (("side_a", a), ("side_b", b)):
        reference = _frame(side["u"])
        if reference is None:
            return _failure("numeric_capacity", "Cannot construct nominal port frame", capacity=True)
        ports[key] = {"origin_m": _sub(side["a"], station), "axis": side["u"],
                      "reference_direction": reference, "outer_diameter_m": side["od"]}
    translation = [0.0, 0.0, 0.0] if standard else _sub(station, origin)
    positions = [translation] + [port["origin_m"] for port in ports.values()]
    if any(not math.isfinite(value) for position in positions for value in position):
        return _failure("numeric_capacity", "Nominal placement exceeds finite arithmetic", capacity=True)
    # These single-fragment fittings have no separate body attachment interface.
    # Their only interface ODs are the two supplied source ODs.
    geometry = {"diameter_m": max(a["od"], b["od"]), "interfaces": [
        {"kind": "circular", "key": key, "origin_m": port["origin_m"],
         "axis": port["axis"], "reference_direction": port["reference_direction"],
         "diameter_m": port["outer_diameter_m"]} for key, port in ports.items()]}
    return {"status": "SOLVED", "internal_parameters": {
        "sphere_diameter_m": geometry["diameter_m"], "sphere_interfaces": geometry["interfaces"],
        "construction": "default", "default_reason": reason},
        "components": [{"component_key": "bend"}],
        "fragments": [{"fragment_key": "bend_body", "binding_key": "sphere_geometry",
            "uniform_scale": 1.0, "local_transform": {"translation_m": translation,
                "rotation": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]}}]}


def with_construction(proposal):
    if proposal["status"] == "SOLVED" and "construction" not in proposal["internal_parameters"]:
        proposal["internal_parameters"].update(construction="detailed", default_reason="none")
    return proposal


def _solve(ctx, *, standard, max_turn_degrees=150, layout_fallback=None, targets=None):
    a = _source(ctx, "a", targets)
    if "status" in a:
        return a
    b = _source(ctx, "b", targets)
    if "status" in b:
        return b
    origin = ctx.product_origin()
    if not _finite_vector(origin):
        return _failure("origin", "Product origin must be finite")
    raw_a, raw_b = a, b
    ua, ub = a["u"], b["u"]
    cross = _cross(ua, ub)
    sine = _norm(cross)
    cosine = max(-1.0, min(1.0, -_dot(ua, ub)))
    delta = math.atan2(sine, cosine)
    if delta > math.radians(max_turn_degrees) + 64 * _EPS:
        return _failure("unsupported_domain", f"BEND supports true turns up to {max_turn_degrees} degrees")
    offset = _sub(b["a"], a["a"])
    scale = max(a["length"], b["length"], a["od"], b["od"])
    coordinate_scale = max(1.0, *(abs(x) for p in (a["a"], a["b"], b["a"], b["b"]) for x in p))
    arithmetic_error = 128 * _EPS * max(coordinate_scale, scale)
    if arithmetic_error > _POSITION_TOLERANCE_M / 8:
        return _failure("numeric_capacity", "Source arithmetic cannot resolve the formal position tolerance", capacity=True)
    residual_tol = _POSITION_TOLERANCE_M
    if sine == 0:
        if cosine < 0:
            return _failure("unsupported_domain", "Same-direction sources are outside the BEND turn domain")
        if _norm(_cross(offset, ua)) > residual_tol:
            return _failure("unsupported_axes", "Parallel offset source axes do not meet")
        # Opposite collinear rays have no unique line intersection. Side A's
        # unretained source endpoint is the fixed role reference station.
        c = list(a["a"])
        delta = 0.0
    else:
        if a["a"] == b["a"]:
            # This exact source point is already the line intersection; no
            # ill-conditioned division by a tiny turn angle is needed.
            c = list(a["a"])
        else:
            if sine < 1e-10:
                return _failure("numeric_capacity", "Line intersection is ill-conditioned at this tiny nonzero angle", capacity=True)
            t_a = _dot(_cross(offset, ub), cross) / (sine * sine)
            t_b = _dot(_cross(offset, ua), cross) / (sine * sine)
            if 128 * _EPS * max(coordinate_scale, abs(t_a), abs(t_b)) > _POSITION_TOLERANCE_M / 8:
                return _failure("numeric_capacity", "Intersection arithmetic cannot resolve the formal position tolerance", capacity=True)
            c = _add(a["a"], _mul(ua, t_a))
            c_b = _add(b["a"], _mul(ub, t_b))
            if not all(math.isfinite(x) for x in (*c, *c_b)):
                return _failure("numeric_capacity", "Line intersection exceeds finite arithmetic", capacity=True)
            if _norm(_sub(c, c_b)) > residual_tol:
                return _failure("unsupported_axes", "Skew source axes have no common 3D intersection")

    canonical = _canonical_opposed_axes(a, b, c, arithmetic_error)
    if canonical is not None:
        a, b = canonical
        ua, ub = a["u"], b["u"]
        delta = 0.0

    bounds = []
    for side in (a, b):
        low = _dot(_sub(side["a"], c), side["u"])
        high = _dot(_sub(side["b"], c), side["u"])
        if not all(math.isfinite(x) for x in (low, high)) or high <= low:
            return _failure("numeric_capacity", "Finite source window cannot be resolved", capacity=True)
        bounds.append((low, high))

    da, db = a["od"], b["od"]
    # OCCT's default geometric confusion is 1e-7 model metres. Require the
    # smallest designed axial/radial feature to clear it by an order of magnitude.
    smallest_feature = (0.07 if delta == 0 else 0.025) * min(da, db)
    if smallest_feature <= 1e-6:
        return _failure("numeric_capacity", "Designed section feature is below the BREP resolution margin", capacity=True)
    if delta == 0:
        # Local A port is station x; B port is station x+L on axis -ua.
        # The C2 transition minimum is a lower bound on intrinsic body length,
        # not a fixed length and not a detachable compensation pipe.
        stub_a, stub_b = 0.3 * da, 0.3 * db
        minimum_transition = _transition(max(da, db), min(da, db)) if da != db else 0.0
        minimum_length = stub_a + minimum_transition + stub_b
        interval_low = max(minimum_length, bounds[0][0] + bounds[1][0])
        interval_high = bounds[0][1] + bounds[1][1]
        if not all(math.isfinite(value) for value in (minimum_length, interval_low, interval_high)):
            return _failure("numeric_capacity", "Direct-through length exceeds numeric capacity", capacity=True)
        if interval_low > interval_high:
            if layout_fallback is not None:
                return layout_fallback(a, b, c, origin, standard, "minimum_body_exceeds_windows")
            return _failure("no_finite_fit", "Direct-through minimum body length exceeds the finite source windows")
        length = interval_low
        axis = _mul(ua, -1.0)
        # world A=C+x*axis; world B=C+(x+L)*axis.
        # side A signed source coordinate is -x; side B is x+L.
        lo = max(-bounds[0][1], bounds[1][0] - length)
        hi = min(-bounds[0][0], bounds[1][1] - length)
        if lo > hi:
            if layout_fallback is not None:
                return layout_fallback(a, b, c, origin, standard, "body_outside_both_windows")
            return _failure("no_finite_fit", "Direct-through body cannot fit both finite source windows")
        x = lo
        port_a = _add(c, _mul(axis, x))
        port_b = _add(port_a, _mul(axis, length))
        mode = "straight"
        radius = 0.0
        body_d = max(da, db)
        bend_scale = 0.0
    else:
        fa, fb = 1.1 * da, 1.1 * db
        body_d = max(fa, fb)
        ea = min(body_d * (body_d - fa) / (fa + fb) + 0.5 * body_d, 2 * body_d)
        eb = min(body_d * (body_d - fb) / (fa + fb) + 0.5 * body_d, 2 * body_d)
        preferred = body_d * (0.4 * delta + 0.2)
        if not all(math.isfinite(value) for value in (fa, fb, body_d, ea, eb, preferred)):
            return _failure("numeric_capacity", "Bend design arithmetic exceeds finite capacity", capacity=True)
        # Arc tangent takeoff is R*tan(delta/2)=0.9B. The actual straight
        # lengths are e_i+0.1B; each must hold its collar, stub and quintic
        # transition. Radius exceeds the body outer radius by 1% of body OD.
        margin = 1e-6 * body_d
        minimum_a = 0.3 * da + _transition(body_d, da) + margin
        minimum_b = 0.3 * db + _transition(body_d, db) + margin
        curvature = ((0.5 + 0.01) * body_d * math.tan(delta / 2) / 0.9)
        lower = max(bounds[0][0] - ea, bounds[1][0] - eb,
                    (minimum_a - ea) / 0.1, (minimum_b - eb) / 0.1,
                    curvature, 1e-6 * body_d)
        upper = min(bounds[0][1] - ea, bounds[1][1] - eb)
        if not all(math.isfinite(value) for value in (lower, upper)):
            return _failure("numeric_capacity", "Bend feasible interval exceeds finite capacity", capacity=True)
        if lower > upper:
            if layout_fallback is not None:
                return layout_fallback(a, b, c, origin, standard, "curvature_transition_window_empty")
            return _failure("no_finite_fit", "Finite windows, curvature and transition constraints have empty intersection")
        tangent_half = math.tan(delta / 2)
        capacity_upper = body_d * (1e8 * tangent_half / 0.9)
        supported_upper = min(upper, capacity_upper)
        if lower > supported_upper:
            return _failure("numeric_capacity", "Physical bend fits, but its arc radius exceeds supported modeling resolution", capacity=True)
        bend_scale = min(max(preferred, lower), supported_upper)
        radius = 0.9 * bend_scale / tangent_half
        if radius / body_d > 1e8 and bend_scale > lower:
            inward = bend_scale * (1 - 8 * _EPS)
            if inward >= lower:
                bend_scale = inward
                radius = 0.9 * bend_scale / tangent_half
        if not math.isfinite(radius) or radius / body_d > 1e8:
            return _failure("numeric_capacity", "Arc radius exceeds supported modeling resolution", capacity=True)
        port_a = _add(c, _mul(ua, bend_scale + ea))
        port_b = _add(c, _mul(ub, bend_scale + eb))
        mode = "curved"

    if any(not math.isfinite(x) for p in (port_a, port_b) for x in p):
        return _failure("numeric_capacity", "Port coordinates exceed finite arithmetic", capacity=True)
    for side, port in ((raw_a, port_a), (raw_b, port_b)):
        displacement = _sub(port, side["a"])
        station = _dot(displacement, side["u"])
        foot = _add(side["a"], _mul(side["u"], station))
        if _norm(_sub(port, foot)) > _POSITION_TOLERANCE_M:
            return _failure("unsupported_axes", "Solved port is outside its actual source axis")
        if station < -_POSITION_TOLERANCE_M or station > side["length"] + _POSITION_TOLERANCE_M:
            return _failure("no_finite_fit", "Solved port is outside its finite source window")
    reference = c if standard else origin
    local_a, local_b, local_c = (_sub(p, reference) for p in (port_a, port_b, c))
    if any(not math.isfinite(x) for p in (local_a, local_b, local_c) for x in p):
        return _failure("numeric_capacity", "World-to-local conversion exceeds finite arithmetic", capacity=True)
    local_scale = max(1.0, *(abs(x) for p in (local_a, local_b, local_c) for x in p))
    if 128 * _EPS * local_scale > 1e-8 * min(da, db):
        return _failure("numeric_capacity", "Local placement cannot resolve the fitting section", capacity=True)
    frame_x = (_frame(_mul(ua, -1.0)) if delta == 0
               else _unit(_cross(_mul(ua, -1.0), ub)))
    if frame_x is None:
        return _failure("numeric_capacity", "Cannot construct a stable local frame", capacity=True)
    reference_a, reference_b = _frame(ua), _frame(ub)
    if reference_a is None or reference_b is None:
        return _failure("numeric_capacity", "Cannot construct both port reference frames", capacity=True)
    ports = {
        "side_a": {"origin_m": local_a, "axis": ua,
                   "reference_direction": reference_a, "outer_diameter_m": da},
        "side_b": {"origin_m": local_b, "axis": ub,
                   "reference_direction": reference_b, "outer_diameter_m": db},
    }
    shape = {"mode": mode, "frame_x": frame_x}
    if mode == "curved":
        shape.update(local_center_m=local_c, body_outer_diameter_m=body_d,
                     turn_radian=delta, bend_scale_m=bend_scale, arc_radius_m=radius)
    internal, placement = localize_two_port({"ports": ports, "shape": shape})
    return {
        "status": "SOLVED", "internal_parameters": internal,
        "components": [{"component_key": "bend"}],
        "fragments": [{"fragment_key": "bend_body", "binding_key": "curved_two_port_geometry",
                       "uniform_scale": 1.0, "local_transform": placement}],
    }


def realize(ctx, *, max_turn_degrees=150, layout_fallback=None):
    return _solve(ctx, standard=False, max_turn_degrees=max_turn_degrees, layout_fallback=layout_fallback)


def standard_model(ctx, *, max_turn_degrees=150, layout_fallback=None,
                   diameter_a=0.02, diameter_b=0.02):
    return _solve(ctx, standard=True, max_turn_degrees=max_turn_degrees,
                  layout_fallback=layout_fallback, targets=nominal_targets(diameter_a, diameter_b))
