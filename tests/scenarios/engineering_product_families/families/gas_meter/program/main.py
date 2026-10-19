"""Finite-window layout for the selected middle-port gas-meter assembly."""

import math


_NOMINAL_M = 0.1
_HALF_BODY_M = 0.2
_BODY_DATUM_Z_M = 0.14
_UP = [0.0, 0.0, 1.0]
_POSITION_TOL_M = 1e-6


def _add(a, b):
    return [a[i] + b[i] for i in range(3)]


def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def _mul(a, k):
    return [v * k for v in a]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _norm(a):
    return math.hypot(*a)


def _failure(key, message, uncertain=False):
    field = "ambiguities" if uncertain else "violations"
    return {"status": "AMBIGUOUS" if uncertain else "INFEASIBLE",
            field: [{("ambiguity_key" if uncertain else "violation_key"): key,
                     "message": message}]}


_NOMINAL_TARGETS = {'left': {'kind': 'finite_segment', 'start_m': [-0.29, 0, 0], 'end_m': [-0.3, 0, 0], 'outer_diameter_m': 0.2, 'direction_relation': 'same'}, 'right': {'kind': 'finite_segment', 'start_m': [0.3, 0, 0], 'end_m': [0.31, 0, 0], 'outer_diameter_m': 0.2, 'direction_relation': 'same'}}

def _source(ctx, side, standard=False):
    interface = {'left': 'gas_left', 'right': 'gas_right'}[side]
    target = (_NOMINAL_TARGETS[side] if standard else next(
        (item["condition"] for item in ctx.external_constraints()
         if item["interface_key"] == interface), None))
    if target is None or target["kind"] != "finite_segment":
        return _failure("source_input", "A finite external constraint is required")
    try:
        a = target["start_m"]
        b = target["end_m"]
        od = target["outer_diameter_m"]
    except (KeyError, TypeError, ValueError):
        return _failure("source_input", "A source field is missing or invalid")
    try:
        valid_values = (len(a) == 3 and len(b) == 3
                        and all(math.isfinite(v) for v in (*a, *b, od))
                        and od > 0)
    except (TypeError, ValueError):
        valid_values = False
    if not valid_values:
        return _failure("source_values", "A/B and OD must be finite, and OD positive")
    d = _sub(b, a)
    if all(v == 0 for v in d):
        return _failure("source_segment", "Source A and B are the same point")
    length = _norm(d)
    if not math.isfinite(length):
        return _failure("numeric_capacity", "Source length exceeds finite arithmetic", True)
    if length == 0:
        return _failure("numeric_capacity", "Source direction underflows finite arithmetic", True)
    u = [v / length for v in d]
    if not all(math.isfinite(v) for v in u):
        return _failure("numeric_capacity", "Source direction is numerically unstable", True)
    return {"a": list(a), "b": list(b), "od": od, "u": u, "length": length}


def _minimum_length(od):
    c = min(0.018, max(0.008, 0.22 * od))
    # The selected connector's three bands end at 2c+9.2mm.
    # Preserve one band width as a visible terminal nipple margin.
    return 2 * c + 0.0092 + 0.0012


def _lengths(left_min, right_min, sum_lower, sum_upper):
    # Euclidean projection of (.1,.1) onto lL>=minL, lR>=minR,
    # sum_lower<=lL+lR<=sum_upper. Every candidate lies on an active face.
    lo = max(sum_lower, left_min + right_min)
    if lo > sum_upper + 1e-10:
        return None
    candidates = [(max(left_min, _NOMINAL_M), max(right_min, _NOMINAL_M))]
    for total in (lo, sum_upper):
        left = min(total - right_min, max(left_min, total / 2))
        candidates.append((left, total - left))
        candidates.append((left_min, total - left_min))
        candidates.append((total - right_min, right_min))
    for left in (left_min, _NOMINAL_M):
        candidates.append((left, max(right_min, lo - left, _NOMINAL_M)))
    for right in (right_min, _NOMINAL_M):
        candidates.append((max(left_min, lo - right, _NOMINAL_M), right))
    feasible = [(l, r) for l, r in candidates
                if l >= left_min - 1e-10 and r >= right_min - 1e-10
                and lo - 1e-10 <= l + r <= sum_upper + 1e-10]
    return min(feasible, key=lambda pair:
               ((pair[0] - _NOMINAL_M) ** 2 + (pair[1] - _NOMINAL_M) ** 2,
                pair[0], pair[1])) if feasible else None


def _rotation(yaw):
    c, s = math.cos(yaw), math.sin(yaw)
    return [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]


def _solve(ctx, standard=False):
    left, right = _source(ctx, "left", standard), _source(ctx, "right", standard)
    if "status" in left:
        return left
    if "status" in right:
        return right
    try:
        origin = ctx.product_origin()
    except (KeyError, TypeError, ValueError):
        return _failure("origin", "Product origin is unavailable")
    try:
        valid_origin = len(origin) == 3 and all(math.isfinite(v) for v in origin)
    except (TypeError, ValueError):
        valid_origin = False
    if not valid_origin:
        return _failure("origin", "Product origin must be a finite 3D point")
    # The selected v4 body's local datum and attachments sit at z=.14 m.
    # A yaw may rotate the horizontal gas axis while its +Z stays upright.
    u = right["u"]
    if (abs(u[2]) > 1e-8 or _norm(_add(left["u"], u)) > 1e-8):
        return _failure("unsupported_axis", "Only opposite, horizontal direct axes are supported")
    if min(left["od"], right["od"]) <= 2e-6:
        return _failure("numeric_capacity", "Connector section is below the BREP margin", True)
    base = left["a"]
    span = _sub(right["a"], base)
    if not all(math.isfinite(v) for v in span):
        return _failure("numeric_capacity", "Source separation exceeds finite arithmetic", True)
    transverse = _sub(span, _mul(u, _dot(span, u)))
    if not all(math.isfinite(v) for v in transverse):
        return _failure("numeric_capacity", "Source projection exceeds finite arithmetic", True)
    if _norm(transverse) > _POSITION_TOL_M / 8:
        return _failure("noncollinear", "Source centerlines do not share one direct axis")
    left_stations = [_dot(_sub(p, base), u) for p in (left["a"], left["b"])]
    right_stations = [_dot(_sub(p, base), u) for p in (right["a"], right["b"])]
    a, b = min(left_stations), max(left_stations)
    c, d = min(right_stations), max(right_stations)
    lmin, rmin = _minimum_length(left["od"]), _minimum_length(right["od"])
    if not all(math.isfinite(v) for v in (a, b, c, d, lmin, rmin)):
        return _failure("numeric_capacity", "Station arithmetic exceeds finite capacity", True)
    sum_lower = c - b - 2 * _HALF_BODY_M
    sum_upper = d - a - 2 * _HALF_BODY_M
    if not all(math.isfinite(v) for v in (sum_lower, sum_upper)):
        return _failure("numeric_capacity", "Window difference exceeds finite capacity", True)
    lengths = _lengths(lmin, rmin, sum_lower, sum_upper)
    if lengths is None:
        return _failure("finite_window", "The two finite windows cannot contain both connector ends")
    ll, lr = lengths
    xlo = max(a + _HALF_BODY_M + ll, c - _HALF_BODY_M - lr)
    xhi = min(b + _HALF_BODY_M + ll, d - _HALF_BODY_M - lr)
    if not all(math.isfinite(v) for v in (ll, lr, xlo, xhi)):
        return _failure("numeric_capacity", "Layout arithmetic exceeds finite capacity", True)
    if xlo > xhi:
        return _failure("numeric_capacity", "Active boundary is numerically unresolved", True)
    x = xlo  # deterministic smallest feasible body station
    left_port = x - _HALF_BODY_M - ll
    right_port = x + _HALF_BODY_M + lr
    if not (a - 1e-12 <= left_port <= b + 1e-12
            and c - 1e-12 <= right_port <= d + 1e-12):
        return _failure("numeric_capacity", "Selected finite-window witness is unstable", True)
    center = _add(base, _mul(u, x))
    yaw = math.atan2(u[1], u[0])
    # Standard construction uses the source design as local coordinates;
    # Backend places that same local model at its requested root O once.
    product_origin = [0.0, 0.0, 0.0] if standard else list(origin)
    left_inner = _add(center, _mul(u, -_HALF_BODY_M))
    right_inner = _add(center, _mul(u, _HALF_BODY_M))
    left_outer = _add(left_inner, _mul(u, -ll))
    right_outer = _add(right_inner, _mul(u, lr))
    if not all(math.isfinite(v) for p in (center, left_outer, right_outer) for v in p):
        return _failure("numeric_capacity", "World placement exceeds finite capacity", True)
    solved = {"left_length_m": ll, "right_length_m": lr,
              "left_outer_diameter_m": left["od"], "right_outer_diameter_m": right["od"],
              "body_center_m": center, "left_internal_m": left_inner,
              "right_internal_m": right_inner, "left_external_m": left_outer,
              "right_external_m": right_outer, "axis_right": u}
    body_translation = _sub(_sub(center, product_origin),
                            [0.0, 0.0, _BODY_DATUM_Z_M])
    return {"status": "SOLVED", "internal_parameters": solved,
            "components": [{"component_key": key} for key in
                           ("body", "left_fitting", "right_fitting")],
            "fragments": [
                {"fragment_key": "body_shape", "binding_key": "body_geometry",
                 "uniform_scale": 1.0,
                 "local_transform": {"translation_m": body_translation,
                                     "rotation": _rotation(yaw)}},
                {"fragment_key": "left_shape", "binding_key": "connector_geometry",

                 "uniform_scale": 1.0,
                 "local_transform": {"translation_m": _sub(left_inner, product_origin),
                                     "rotation": _rotation(yaw + math.pi)}},
                {"fragment_key": "right_shape", "binding_key": "connector_geometry",

                 "uniform_scale": 1.0,
                 "local_transform": {"translation_m": _sub(right_inner, product_origin),
                                     "rotation": _rotation(yaw)}},
            ]}


def realize(ctx):
    return _solve(ctx)


def standard_model(ctx):
    return _solve(ctx, standard=True)
