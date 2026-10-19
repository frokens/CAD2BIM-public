"""Pure numeric detailed, compact or node placement of one fixed regulator assembly."""

import math

from .local_geometry_frame import compose, localize_two_port


_HALF_BODY_SPAN_M = 0.8937003968253989
_BODY_SPAN_M = 1.7874007936507978
_BODY_PORT_OD_M = 0.15
_UP = [0.0, 0.0, 1.0]
_EPS = 2.220446049250313e-16
_POSITION_TOLERANCE_M = 1e-6
_MIN_BODY_SCALE = 0.4


def _add(a, b):
    return [a[i] + b[i] for i in range(3)]


def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def _mul(a, scalar):
    return [scalar * value for value in a]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _norm(a):
    return math.hypot(*a)


def _unit(a):
    length = _norm(a)
    return _mul(a, 1.0 / length) if length > 0 and math.isfinite(length) else None


def _reference(axis):
    preferred = _UP
    projection = _sub(preferred, _mul(axis, _dot(preferred, axis)))
    if _norm(projection) < 32 * math.sqrt(_EPS):
        preferred = [1.0, 0.0, 0.0]
        projection = _sub(preferred, _mul(axis, _dot(preferred, axis)))
    return _unit(projection)


def _rotation(yaw):
    c, s = math.cos(yaw), math.sin(yaw)
    return [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]


def _inverse_yaw(vector, yaw):
    c, s = math.cos(yaw), math.sin(yaw)
    return [c * vector[0] + s * vector[1],
            -s * vector[0] + c * vector[1], vector[2]]


def _failure(key, message, *, uncertain=False):
    if uncertain:
        return {"status": "AMBIGUOUS", "ambiguities": [{
            "ambiguity_key": key, "message": message}]}
    return {"status": "INFEASIBLE", "violations": [{
        "violation_key": key, "message": message}]}


_NOMINAL_TARGETS = {'high': {'kind': 'finite_segment', 'start_m': [0, 0, 0], 'end_m': [-4, 0, 0], 'outer_diameter_m': 0.1, 'direction_relation': 'same'}, 'low': {'kind': 'finite_segment', 'start_m': [0, 0, 0], 'end_m': [0, 4, 0], 'outer_diameter_m': 0.15, 'direction_relation': 'same'}}

def _source(ctx, side, standard=False):
    interface = {'high': 'high_external', 'low': 'low_external'}[side]
    target = (_NOMINAL_TARGETS[side] if standard else next(
        (item["condition"] for item in ctx.external_constraints()
         if item["interface_key"] == interface), None))
    if target is None or target["kind"] != "finite_segment":
        return _failure("source_input", "A finite external constraint is required")
    a, b = target["start_m"], target["end_m"]
    od = target["outer_diameter_m"]
    direction = _sub(b, a)
    length = _norm(direction)
    if not math.isfinite(length):
        return _failure("numeric_capacity", "Source length exceeds finite arithmetic", uncertain=True)
    unit = _unit(direction)
    if unit is None:
        return _failure("degenerate_source", "A finite source segment cannot have coincident ends")
    return {"a": list(a), "b": list(b), "u": unit, "od": od, "length": length}


def _transition(body_d, end_d):
    # BEND v3 preserves this numeric occupancy, although its new cone is linear.
    return max(0.1 * body_d, 1.875 * abs(body_d - end_d) / 2)


def _bend_spec(source, scale):
    exterior, interior = source["od"], _BODY_PORT_OD_M * scale
    u = source["u"]
    delta = math.atan2(_norm(_cross(u, _UP)), max(-1.0, min(1.0, -u[2])))
    if delta > math.radians(150) + 64 * _EPS:
        return _failure("unsupported_turn", "The exterior-to-body turn exceeds the detailed REGULATOR 150-degree domain")
    straight = delta <= 64 * _EPS
    if (0.07 if straight else 0.025) * min(exterior, interior) <= 1e-6:
        return _failure("numeric_capacity", "The fitting section is below BEND v3's BREP margin", uncertain=True)
    if straight:
        body_d = max(exterior, interior)
        minimum = 0.3 * exterior + (0 if exterior == interior else
                    _transition(body_d, min(exterior, interior))) + 0.3 * interior
        return {"mode": "straight", "delta": 0.0, "ea": 0.0, "eb": 0.0,
                "minimum": minimum, "preferred": minimum,
                "body_d": body_d, "outer_d": exterior, "inner_d": interior}
    fa, fb = 1.1 * exterior, 1.1 * interior
    body_d = max(fa, fb)
    ea = min(body_d * (body_d - fa) / (fa + fb) + 0.5 * body_d, 2 * body_d)
    eb = min(body_d * (body_d - fb) / (fa + fb) + 0.5 * body_d, 2 * body_d)
    margin = 1e-6 * body_d
    leg_a = (0.3 * exterior + _transition(body_d, exterior) + margin - ea) / 0.1
    leg_b = (0.3 * interior + _transition(body_d, interior) + margin - eb) / 0.1
    curvature = 0.51 * body_d * math.tan(delta / 2) / 0.9
    minimum = max(leg_a, leg_b, curvature, 1e-6 * body_d)
    preferred = body_d * (0.4 * delta + 0.2)
    if not all(math.isfinite(x) for x in (ea, eb, minimum, preferred)):
        return _failure("numeric_capacity", "BEND v3 design arithmetic exceeds finite capacity", uncertain=True)
    return {"mode": "curved", "delta": delta, "ea": ea, "eb": eb,
            "minimum": minimum, "preferred": max(minimum, preferred),
            "curvature": curvature, "body_d": body_d,
            "outer_d": exterior, "inner_d": interior}


def _roots(a, b, c):
    if not all(math.isfinite(value) for value in (a, b, c)):
        raise RuntimeError("numeric_capacity: REGULATOR root coefficients exceed finite capacity")
    if abs(a) < 1e-13:
        return [-c / b] if abs(b) >= 1e-13 else []
    discriminant = b * b - 4 * a * c
    if not math.isfinite(discriminant):
        raise RuntimeError("numeric_capacity: REGULATOR root discriminant exceeds finite capacity")
    if discriminant < -1e-10 * max(1.0, b * b, abs(a * c)):
        return []
    discriminant = max(0.0, discriminant)
    return [(-b - math.sqrt(discriminant)) / (2 * a),
            (-b + math.sqrt(discriminant)) / (2 * a)]


def _q(source, spec, k, station):
    qa = k + spec["ea"] if spec["mode"] == "curved" else 0.0
    qb = k + spec["eb"] if spec["mode"] == "curved" else k
    p = _add(source["a"], _mul(source["u"], station))
    q = _add(_sub(p, _mul(source["u"], qa)), _mul(_UP, qb))
    return p, q


def _station_candidates(high, low, hs, ls, kh, kl, span):
    # Q_high.z == Q_low.z is linear in the two finite source stations.
    _, qh0 = _q(high, hs, kh, 0.0)
    _, ql0 = _q(low, ls, kl, 0.0)
    z0 = ql0[2] - qh0[2]
    zh, zl = -high["u"][2], low["u"][2]
    lh, ll = high["length"], low["length"]
    paths = []
    if abs(zh) + abs(zl) > 1e-12:
        endpoints = []
        if abs(zl) > 1e-12:
            for h in (0.0, lh):
                l = -(z0 + zh * h) / zl
                if -1e-9 <= l <= ll + 1e-9:
                    endpoints.append((h, min(ll, max(0.0, l))))
        if abs(zh) > 1e-12:
            for l in (0.0, ll):
                h = -(z0 + zl * l) / zh
                if -1e-9 <= h <= lh + 1e-9:
                    endpoints.append((min(lh, max(0.0, h)), l))
        if len(endpoints) >= 2:
            paths.append((endpoints[0], max(endpoints[1:], key=lambda p:
                          math.hypot(p[0] - endpoints[0][0], p[1] - endpoints[0][1]))))
    elif abs(z0) <= _POSITION_TOLERANCE_M / 8:
        residual = min(lh, ll)
        paths.append(((lh, ll), (lh - residual, ll - residual)))
        corners = ((0.0, 0.0), (lh, 0.0), (0.0, ll), (lh, ll))
        for a in corners:
            for b in corners:
                if a != b:
                    paths.append((a, b))
        for fixed in (0.0, lh):
            paths.append(((fixed, 0.0), (fixed, ll)))
        for fixed in (0.0, ll):
            paths.append(((0.0, fixed), (lh, fixed)))
    results = []
    for first, last in paths:
        _, qh = _q(high, hs, kh, first[0])
        _, ql = _q(low, ls, kl, first[1])
        difference = [ql[0] - qh[0], ql[1] - qh[1]]
        velocity = [low["u"][i] * (last[1] - first[1]) -
                    high["u"][i] * (last[0] - first[0]) for i in (0, 1)]
        quadratic = sum(x*x for x in velocity)
        linear = 2 * sum(difference[i] * velocity[i] for i in (0, 1))
        constant = sum(x*x for x in difference) - span * span
        roots = ([0.0, 1.0] if quadratic < 1e-20 and abs(linear) < 1e-12
                 and abs(constant) < 1e-9 * max(1.0, span * span) else
                 _roots(quadratic, linear, constant))
        for t in roots:
            if -1e-10 <= t <= 1 + 1e-10:
                h = first[0] + min(1.0, max(0.0, t)) * (last[0] - first[0])
                l = first[1] + min(1.0, max(0.0, t)) * (last[1] - first[1])
                results.append((h, l))
    return results


def _elbow_envelope(source, spec, p, q, k):
    qa = k + spec["ea"] if spec["mode"] == "curved" else 0.0
    center = _sub(p, _mul(source["u"], qa))
    sagitta = (0.9 * k / math.tan(spec["delta"] / 2) *
               (1 - math.cos(spec["delta"] / 2))
               if spec["mode"] == "curved" else 0.0)
    radius = spec["body_d"] / 2 + sagitta
    return [(min(p[i], center[i], q[i]) - radius,
             max(p[i], center[i], q[i]) + radius) for i in range(3)]


def _opposed_source_axis(high, low):
    return (_dot(high["u"], low["u"]) < 0 and
            _norm(_cross(high["u"], low["u"])) <= 64 * _EPS and
            _norm(_cross(_sub(high["a"], low["a"]), low["u"])) <= _POSITION_TOLERANCE_M / 8)


def _ordered_source_stations(high, low, ph, pl):
    if not _opposed_source_axis(high, low):
        return True
    return (_dot(_sub(pl, ph), low["u"]) > 0 and
            0 <= _dot(_sub(ph, high["a"]), high["u"]) < high["length"] and
            0 <= _dot(_sub(pl, low["a"]), low["u"]) < low["length"])


def _candidate(high, low, hs, ls, kh, kl, scale, origin, stations=None):
    span = _BODY_SPAN_M * scale
    choices = (stations if stations is not None else
               _station_candidates(high, low, hs, ls, kh, kl, span))
    for h, l in choices:
        ph, qh = _q(high, hs, kh, h)
        pl, ql = _q(low, ls, kl, l)
        if not _ordered_source_stations(high, low, ph, pl):
            continue
        difference = _sub(ql, qh)
        if (abs(difference[2]) > _POSITION_TOLERANCE_M / 8 or
                abs(math.hypot(*difference[:2]) - span) > _POSITION_TOLERANCE_M / 8):
            continue
        # The source-side ordering must not turn the two fittings through each other.
        source_separation = _sub(pl, ph)
        if _dot(difference[:2], source_separation[:2]) <= 0:
            continue
        high_envelope = _elbow_envelope(high, hs, ph, qh, kh)
        low_envelope = _elbow_envelope(low, ls, pl, ql, kl)
        if all(high_envelope[i][0] <= low_envelope[i][1] and
               low_envelope[i][0] <= high_envelope[i][1] for i in range(3)):
            # An overlapping conservative envelope needs a stronger numeric
            # clearance argument before it may become a complete proposal.
            continue
        yaw = math.atan2(difference[1], difference[0])
        body_center = _mul(_add(qh, ql), 0.5)
        return (ph, qh, pl, ql, body_center, yaw, kh, kl)
    return None


def _k_candidates_at_stations(high, low, hs, ls, h, l, span):
    """Eliminate equal height, then solve the remaining horizontal circle exactly."""
    _, qh = _q(high, hs, 0.0, h)
    _, ql = _q(low, ls, 0.0, l)
    difference = _sub(ql, qh)
    vh = _UP if hs["mode"] == "straight" else _sub(_UP, high["u"])
    vl = _UP if ls["mode"] == "straight" else _sub(_UP, low["u"])
    if abs(vl[2]) < 1e-12:
        return []
    base = [difference[i] - vl[i] * difference[2] / vl[2] for i in (0, 1)]
    velocity = [vl[i] * vh[2] / vl[2] - vh[i] for i in (0, 1)]
    answer = []
    for kh in _roots(sum(x*x for x in velocity),
                     2 * sum(base[i] * velocity[i] for i in (0, 1)),
                     sum(x*x for x in base) - span * span):
        kl = (kh * vh[2] - difference[2]) / vl[2]
        if math.isfinite(kh) and math.isfinite(kl):
            answer.append((kh, kl))
    return answer


def _k_values(spec, extent):
    minimum, preferred = spec["minimum"], spec["preferred"]
    values = [preferred, minimum]
    step = max(spec["body_d"], 0.05)
    values += [minimum + step * fraction for fraction in
               (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)]
    values += [preferred + step * fraction for fraction in (-0.5, 0.5, 1.0, 2.0)]
    values += [minimum + extent * fraction for fraction in (0.1, 0.25, 0.5, 1.0)]
    return sorted({x for x in values if x >= minimum and math.isfinite(x)})


def _fit_scale(high, low, scale, origin):
    hs, ls = _bend_spec(high, scale), _bend_spec(low, scale)
    if "status" in hs:
        return hs
    if "status" in ls:
        return ls
    extent = max(high["length"], low["length"], _BODY_SPAN_M * scale,
                 high["od"], low["od"])
    horizontal = abs(high["u"][2]) + abs(low["u"][2]) < 1e-12
    if horizontal:
        # Both body ports are at the same height, so this is the exact k relation.
        offset = high["a"][2] - low["a"][2] + hs["eb"] - ls["eb"]
        kh_values = _k_values(hs, extent) + [
            value - offset for value in _k_values(ls, extent)]
        pairs = [(kh, kh + offset) for kh in kh_values]
    else:
        pairs = [(kh, kl) for kh in _k_values(hs, extent)
                 for kl in _k_values(ls, extent)]
    for kh, kl in pairs:
        if kh < hs["minimum"] - 1e-10 or kl < ls["minimum"] - 1e-10:
            continue
        attempt = _candidate(high, low, hs, ls, kh, kl, scale, origin)
        if attempt is not None:
            return hs, ls, attempt
    if not horizontal:
        # A finite grid in B cannot certify failure. Conversely, exact roots
        # at selected source stations can provide an additional real witness.
        fractions = (0.0, 0.1, 0.2, 0.25, 0.5, 0.75, 1.0)
        stations = [(high["length"] * a, low["length"] * b)
                    for a in fractions for b in fractions]
        for h, l in stations:
            for kh, kl in _k_candidates_at_stations(high, low, hs, ls, h, l,
                                                     _BODY_SPAN_M * scale):
                if kh < hs["minimum"] or kl < ls["minimum"] or max(kh, kl) > 4 * extent:
                    continue
                attempt = _candidate(high, low, hs, ls, kh, kl, scale, origin,
                                     stations=((h, l),))
                if attempt is not None:
                    return hs, ls, attempt
    return None


def _opposite_horizontal_certificate(high, low, scale):
    # Necessary finite-window bound for opposed, equal-OD horizontal sources.
    if (abs(high["od"] - low["od"]) > 1e-12 or
            abs(high["u"][2]) + abs(low["u"][2]) > 1e-12 or
            _norm(_add(high["u"], low["u"])) > 1e-10 or
            _norm(_cross(_sub(low["a"], high["a"]), low["u"])) > 1e-8 or
            _dot(_sub(low["a"], high["a"]), low["u"]) < -1e-10):
        return None
    spec = _bend_spec(high, scale)
    if "status" in spec or spec["mode"] != "curved":
        return None
    base = _dot(_sub(low["a"], high["a"]), low["u"])
    window = base + high["length"] + low["length"]
    return window - (_BODY_SPAN_M * scale + 2 * (spec["curvature"] + spec["ea"]))


def _scale_breakpoints(high, low, *, detailed):
    lower, upper = _MIN_BODY_SCALE, 1.0
    certificate = _opposite_horizontal_certificate(high, low, upper) if detailed else None
    if certificate is not None and certificate < 0:
        at_lower = _opposite_horizontal_certificate(high, low, lower)
        if at_lower is not None and at_lower < 0:
            return []
        if at_lower is not None:
            lo, hi = lower, upper
            for _ in range(55):
                middle = (lo + hi) / 2
                if _opposite_horizontal_certificate(high, low, middle) >= 0:
                    lo = middle
                else:
                    hi = middle
            upper = lo
    return sorted({lower, upper, *(source["od"] / _BODY_PORT_OD_M
                                   for source in (high, low)
                                   if lower < source["od"] / _BODY_PORT_OD_M < upper)})


def _search_scale(high, low, fit_at_scale, *, detailed):
    points = _scale_breakpoints(high, low, detailed=detailed)
    if not points:
        return None
    queue = [(0, -hi, lo, hi) for lo, hi in zip(points, points[1:])]
    cache = {1.0: None}  # Scale one was tested first.
    while queue:
        queue = sorted(queue)
        depth, _, lo, hi = queue[0]
        queue = queue[1:]
        middle = (lo + hi) / 2
        for scale in (hi, middle, lo):
            if scale in cache:
                continue
            if len(cache) >= 128:
                queue = []
                break
            fit = fit_at_scale(scale)
            if fit is not None and "status" in fit and fit["status"] != "SOLVED":
                return fit
            cache[scale] = fit
            if fit is not None:
                return scale, fit
        if (len(cache) >= 128 or middle <= lo or middle >= hi or
                _BODY_SPAN_M * (hi - lo) <= _POSITION_TOLERANCE_M / 8):
            continue
        queue.extend(((depth + 1, -hi, middle, hi),
                      (depth + 1, -middle, lo, middle)))
    return None




def _fragment(side, source, spec, p, q, k, origin, yaw):
    # BEND local +X reference at its inner +Z port maps to the source IFC
    # body's local -Y Plane.RefDirection after the body's own yaw.
    elbow_yaw = yaw - math.pi / 2
    local_q = _inverse_yaw(_sub(q, p), elbow_yaw)
    local_u = _inverse_yaw(source["u"], elbow_yaw)
    local_w = _UP
    if spec["mode"] == "curved":
        qa, qb = k + spec["ea"], k + spec["eb"]
        local_center = _mul(local_u, -qa)
        frame_x = _unit(_cross(_mul(local_u, -1), local_w))
        radius = 0.9 * k / math.tan(spec["delta"] / 2)
    else:
        local_center = [0.0, 0.0, 0.0]
        frame_x = _reference(_mul(local_u, -1))
        radius = 0.0
    ports = {
        "side_a": {"origin_m": [0.0, 0.0, 0.0], "axis": local_u,
                   "reference_direction": _reference(local_u), "outer_diameter_m": source["od"]},
        "side_b": {"origin_m": local_q, "axis": local_w,
                   "reference_direction": [1.0, 0.0, 0.0], "outer_diameter_m": spec["inner_d"]},
    }
    shape = {"mode": spec["mode"], "frame_x": frame_x}
    if spec["mode"] == "curved":
        shape.update(local_center_m=local_center, body_outer_diameter_m=spec["body_d"],
                     turn_radian=spec["delta"], bend_scale_m=k, arc_radius_m=radius)
    params = {"ports": ports, "shape": shape}
    if spec["mode"] == "curved":
        predicted_q = _add(local_center, _mul(local_w, qb))
        if _norm(_sub(predicted_q, local_q)) > _POSITION_TOLERANCE_M / 8:
            return None
    params, local_pose = localize_two_port(params)
    prefix = side + "_"
    solved = {prefix + key: value for key, value in params.items()}
    component = {"component_key": side + "_elbow"}
    fragment = {"fragment_key": side + "_elbow_shape",
                "binding_key": "curved_two_port_geometry",
                 "uniform_scale": 1.0,
                "local_transform": compose({"translation_m": _sub(p, origin),
                                             "rotation": _rotation(elbow_yaw)}, local_pose)}
    return solved, component, fragment


def _compact_stations(high, low, scale):
    # Horizontal squared separation is a convex quadratic on the two finite
    # station intervals. Its minimum is at a stationary interior point or an
    # edge minimum; its maximum is at a corner. Solve along that finite pair.
    delta = _sub(low["a"], high["a"])[:2]
    uh, ul = high["u"][:2], low["u"][:2]
    lh, ll = high["length"], low["length"]
    # Opposed sides of one source axis carry an order: high lies before low
    # along low's outward axis. Center the feasible ordered interval so that
    # an available positive residual is not lost to a boundary-first root.
    horizontal = math.hypot(*ul)
    separation = _sub(high["a"], low["a"])
    if _opposed_source_axis(high, low):
        if horizontal == 0:
            return None
        span = _BODY_SPAN_M * scale / horizontal
        high_start = _dot(separation, low["u"])
        lower, upper = max(high_start - lh, -span), min(high_start, ll - span)
        if not all(math.isfinite(value) for value in (span, high_start, lower, upper)):
            return _failure("numeric_capacity", "Compact ordered station arithmetic exceeds finite capacity", uncertain=True)
        if lower >= upper:
            return None
        station = lower * 0.5 + upper * 0.5
        h, l = high_start - station, station + span
        return (h, l) if 0 <= h <= lh and 0 <= l <= ll else None
    corners = [(0.0, 0.0), (lh, 0.0), (0.0, ll), (lh, ll)]
    candidates = list(corners)
    hh, hl, ll2 = _dot(uh, uh), -_dot(uh, ul), _dot(ul, ul)
    rh, rl = _dot(uh, delta), -_dot(ul, delta)
    determinant = hh * ll2 - hl * hl
    if determinant > 1e-20:
        h, l = (rh * ll2 - hl * rl) / determinant, (hh * rl - hl * rh) / determinant
        if 0 <= h <= lh and 0 <= l <= ll:
            candidates.append((h, l))
    for h in (0.0, lh):
        if ll2 > 0:
            l = (rl - hl * h) / ll2
            if 0 <= l <= ll:
                candidates.append((h, l))
    for l in (0.0, ll):
        if hh > 0:
            h = (rh - hl * l) / hh
            if 0 <= h <= lh:
                candidates.append((h, l))
    distances = [sum((delta[i] - h * uh[i] + l * ul[i]) ** 2 for i in (0, 1))
                 for h, l in candidates]
    if not all(math.isfinite(value) for value in distances):
        return _failure("numeric_capacity", "Compact station arithmetic exceeds finite capacity", uncertain=True)
    first = candidates[distances.index(min(distances))]
    last = corners[distances.index(max(distances[:4]))]
    base = [delta[i] - first[0] * uh[i] + first[1] * ul[i] for i in (0, 1)]
    velocity = [(last[1] - first[1]) * ul[i] - (last[0] - first[0]) * uh[i] for i in (0, 1)]
    quadratic, linear = _dot(velocity, velocity), 2 * _dot(base, velocity)
    constant = _dot(base, base) - (_BODY_SPAN_M * scale) ** 2
    roots = [0.0] if constant == 0 else _roots(quadratic, linear, constant)
    for t in roots:
        if 0 <= t <= 1:
            h = first[0] + t * (last[0] - first[0])
            l = first[1] + t * (last[1] - first[1])
            if 0 <= h <= lh and 0 <= l <= ll:
                return h, l
    return None


def _sphere_parameters(diameter, ports):
    return {"diameter_m": diameter, "interfaces": [
        {"kind": "circular", "key": key, "origin_m": port["origin_m"],
         "axis": port["axis"], "reference_direction": port["reference_direction"],
         "diameter_m": port["outer_diameter_m"]} for key, port in ports.items()]}


def _sphere_layout(high, low, ph, pl, qh, ql, center, yaw, body_node, origin, construction, reason, scale=1.0):
    body_reference = [math.sin(yaw), -math.cos(yaw), 0.0]
    solved = {"construction": construction, "default_reason": reason}
    if body_node is None:
        solved["body_uniform_scale"] = scale
    else:
        solved.update(body_sphere_diameter_m=body_node["diameter_m"], body_sphere_interfaces=body_node["interfaces"])
    fragments = [{"fragment_key": "body_shape",
                  "binding_key": "regulator_body_asset" if body_node is None else "sphere_geometry",

                  "uniform_scale": scale if body_node is None else 1.0,
                  "local_transform": {"translation_m": _sub(center, origin), "rotation": _rotation(yaw)}}]
    for side, source, external, internal in (("high", high, ph, qh), ("low", low, pl, ql)):
        sphere_center = _add(_mul(external, 0.5), _mul(internal, 0.5))
        ports = {
            "side_a": {"origin_m": _sub(external, sphere_center), "axis": source["u"],
                       "reference_direction": _reference(source["u"]), "outer_diameter_m": source["od"]},
            "side_b": {"origin_m": _sub(internal, sphere_center), "axis": _UP,
                       "reference_direction": body_reference, "outer_diameter_m": _BODY_PORT_OD_M * scale},
        }
        geometry = _sphere_parameters(max(source["od"], _BODY_PORT_OD_M * scale), ports)
        solved.update({side + "_sphere_diameter_m": geometry["diameter_m"], side + "_sphere_interfaces": geometry["interfaces"]})
        fragments.append({"fragment_key": side + "_elbow_shape", "binding_key": "sphere_geometry",
            "uniform_scale": 1.0, "local_transform": {"translation_m": _sub(sphere_center, origin), "rotation": _rotation(0)}})
    if not all(math.isfinite(value) for f in fragments for value in f["local_transform"]["translation_m"]):
        return _failure("numeric_capacity", "Spherical placement exceeds finite arithmetic", uncertain=True)
    return {"status": "SOLVED", "internal_parameters": solved,
            "components": [{"component_key": key} for key in ("assembly", "body", "high_elbow", "low_elbow")],
            "fragments": fragments}


def _compact_layout(high, low, origin, reason, scale=1.0):
    stations = _compact_stations(high, low, scale)
    if stations is None or "status" in stations:
        return stations
    ph = _add(high["a"], _mul(high["u"], stations[0]))
    pl = _add(low["a"], _mul(low["u"], stations[1]))
    if not _ordered_source_stations(high, low, ph, pl):
        return None
    difference = _sub(pl, ph)
    if abs(math.hypot(*difference[:2]) - _BODY_SPAN_M * scale) > _POSITION_TOLERANCE_M / 8:
        return _failure("layout_precision", "Compact body span does not close", uncertain=True)
    yaw = math.atan2(difference[1], difference[0])
    center = _add(_mul(ph, 0.5), _mul(pl, 0.5))
    center[2] = max(ph[2], pl[2]) + 0.5 * max(high["od"], low["od"], _BODY_PORT_OD_M * scale)
    if not math.isfinite(center[2]) or not center[2] > max(ph[2], pl[2]):
        return _failure("numeric_capacity", "Compact vertical offset is not representable", uncertain=True)
    axis = [math.cos(yaw), math.sin(yaw), 0.0]
    qh = _sub(center, _mul(axis, _HALF_BODY_SPAN_M * scale))
    ql = _add(center, _mul(axis, _HALF_BODY_SPAN_M * scale))
    return _sphere_layout(high, low, ph, pl, qh, ql, center, yaw,
                          None, origin, "compact", reason, scale)


def _node_layout(ctx, high, low, origin, reason):
    stations = ctx.param("node_stations")
    if stations["kind"] == "unavailable":
        return _failure("connection_station_unresolved", "Node construction needs confirmed source connection stations", uncertain=True)
    ph, pl = stations["high_m"], stations["low_m"]
    for source, point in ((high, ph), (low, pl)):
        if point == source["a"] or point == source["b"]:
            continue
        offset = _sub(point, source["a"])
        station = _dot(offset, source["u"])
        if not math.isfinite(station):
            return _failure("numeric_capacity", "Node station arithmetic exceeds finite capacity", uncertain=True)
        projected = _add(source["a"], _mul(source["u"], station))
        if not 0 <= station <= source["length"] or _norm(_sub(point, projected)) > _POSITION_TOLERANCE_M:
            return _failure("node_station_not_permitted", "Confirmed node station contradicts its finite source permission")
    center = _add(_mul(ph, 0.5), _mul(pl, 0.5))
    body_ports = {}
    for key, point in (("geometric_left_disk", ph), ("geometric_right_disk", pl)):
        body_ports[key] = {"origin_m": _sub(point, center), "axis": [0.0, 0.0, -1.0],
                           "reference_direction": [0.0, -1.0, 0.0], "outer_diameter_m": _BODY_PORT_OD_M}
    body_node = _sphere_parameters(max(high["od"], low["od"], _BODY_PORT_OD_M), body_ports)
    return _sphere_layout(high, low, ph, pl, ph, pl, center, 0.0, body_node, origin, "node", reason)


def _solve(ctx, standard=False):
    high, low = _source(ctx, "high", standard), _source(ctx, "low", standard)
    if "status" in high:
        return high
    if "status" in low:
        return low
    product_origin = ctx.product_origin()
    # A standard construction is Family-local; Backend places its root at O.
    origin = [0.0, 0.0, 0.0] if standard else product_origin
    fit = _fit_scale(high, low, 1.0, origin)
    scale = 1.0
    reason = "detailed_layout_not_found"
    if fit is not None and "status" in fit:
        if (fit["status"] != "INFEASIBLE" or
                fit["violations"][0]["violation_key"] != "unsupported_turn"):
            return fit
        reason = "detailed_turn_unsupported"
        fit = None
    if fit is None:
        if reason != "detailed_turn_unsupported":
            selected = _search_scale(
                high, low, lambda candidate: _fit_scale(high, low, candidate, origin),
                detailed=True)
            if selected is not None and "status" in selected:
                return selected
            if selected is not None:
                scale, fit = selected
    if fit is None:
        compact = _compact_layout(high, low, origin, reason)
        if compact is None:
            selected = _search_scale(
                high, low, lambda candidate: _compact_layout(high, low, origin, reason, candidate),
                detailed=False)
            if selected is not None and "status" in selected:
                return selected
            if selected is not None:
                compact = selected[1]
        if compact is not None:
            return compact
        node_reason = ("detailed_turn_unsupported_and_compact_not_found" if reason == "detailed_turn_unsupported"
                       else "detailed_and_compact_layout_not_found")
        return _node_layout(ctx, high, low, origin, node_reason)
    hs, ls, (ph, qh, pl, ql, center, yaw, kh, kl) = fit
    h = _fragment("high", high, hs, ph, qh, kh, origin, yaw)
    l = _fragment("low", low, ls, pl, ql, kl, origin, yaw)
    if h is None or l is None:
        return _failure("layout_precision", "The selected 3D ports do not close in BEND local coordinates", uncertain=True)
    solved = {"body_uniform_scale": scale,
              "construction": "detailed", "default_reason": "none"}
    solved.update(h[0])
    solved.update(l[0])
    return {"status": "SOLVED", "internal_parameters": solved,
            "components": [
                {"component_key": "assembly"},
                {"component_key": "body"},
                h[1], l[1],
            ],
            "fragments": [
                {"fragment_key": "body_shape", "binding_key": "regulator_body_asset",
                 "uniform_scale": scale,
                 "local_transform": {"translation_m": _sub(center, origin),
                                     "rotation": _rotation(yaw)}}, h[2], l[2],
            ]}


def realize(ctx):
    return _solve(ctx)


def standard_model(ctx):
    return _solve(ctx, standard=True)
