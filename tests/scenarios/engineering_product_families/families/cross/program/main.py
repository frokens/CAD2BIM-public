"""Pure numeric four-source CROSS proposal; no shape is constructed here."""

import math

from .local_geometry_frame import frame, point_in_frame, direction_in_frame


EPS = 2.220446049250313e-16
POSITION_TOL = 1e-6
ANGLE_MARGIN = 1e-8


def dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def add(a, b):
    return [x+y for x, y in zip(a, b)]


def sub(a, b):
    return [x-y for x, y in zip(a, b)]


def mul(a, s):
    return [s*x for x in a]


def cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def norm(a):
    return math.hypot(*a)


def unit(a):
    scale = max(abs(x) for x in a)
    if scale == 0:
        return None
    v = [x/scale for x in a]
    return [x/norm(v) for x in v]


def finite3(value):
    return len(value) == 3 and all(math.isfinite(x) for x in value)


def fail(key, message, *, ambiguous=False):
    field = "ambiguities" if ambiguous else "violations"
    item = "ambiguity_key" if ambiguous else "violation_key"
    return {"status": "AMBIGUOUS" if ambiguous else "INFEASIBLE",
            field: [{item: key, "message": message}]}


def reference(axis):
    trial = [0.0, 0.0, 1.0]
    projected = sub(trial, mul(axis, dot(trial, axis)))
    if norm(projected) < 32*math.sqrt(EPS):
        trial = [1.0, 0.0, 0.0]
        projected = sub(trial, mul(axis, dot(trial, axis)))
    return unit(projected)


def source(targets, key):
    target = targets.get("side_" + key)
    if target is None or target["kind"] != "finite_segment" or target["direction_relation"] != "same":
        return fail("source_condition", f"Side {key} needs a finite_segment with same direction")
    a, b, d = target["start_m"], target["end_m"], target["outer_diameter_m"]
    if not finite3(a) or not finite3(b):
        return fail("source_coordinates", f"Side {key} needs finite 3D endpoints")
    if not math.isfinite(d) or d <= 0:
        return fail("source_od", f"Side {key} needs positive finite OD")
    v = sub(b, a)
    length = norm(v)
    if not math.isfinite(length) or length <= 0:
        return fail("source_length", f"Side {key} has no positive finite length")
    u = unit(v)
    if u is None:
        return fail("numeric_capacity", "Source direction cannot be resolved", ambiguous=True)
    return {"key": key, "a": a, "b": b, "u": u, "od": d}


def intersection(sides):
    # Choose the best conditioned pair, but accept only a common point on
    # every line. In particular, the midpoint of two skew closest points
    # is never promoted into a junction.
    pairs = [(norm(cross(a["u"], b["u"])), a, b)
             for i, a in enumerate(sides) for b in sides[i+1:]]
    sine, a, b = max(pairs, key=lambda row: row[0])
    if sine < 1e-10:
        return fail("common_intersection", "Four parallel axes lack a unique junction")
    w = sub(b["a"], a["a"])
    n = cross(a["u"], b["u"])
    station = dot(cross(w, b["u"]), n)/(sine*sine)
    c = add(a["a"], mul(a["u"], station))
    if not finite3(c):
        return fail("numeric_capacity", "Intersection exceeds finite arithmetic", ambiguous=True)
    for side in sides:
        station = dot(sub(c, side["a"]), side["u"])
        foot = add(side["a"], mul(side["u"], station))
        if norm(sub(c, foot)) > POSITION_TOL:
            return fail("common_intersection", "Four source lines have no common 3D point")
    return c


def neck_interval(port_radius, rho_offset, seam, maximum, clearance):
    """Solve x + 2*seam*s/x <= maximum exactly on both sides of its minimum.

    rho = sqrt(s*s+x*x) + rho_offset. This returns the full finite
    chamber-radius interval, rather than assuming neck length monotonicity.
    """
    s = port_radius+seam
    allowed = maximum-clearance
    a = 2*seam*s
    if not math.isfinite(allowed) or allowed <= 0:
        return None
    discriminant = allowed*allowed-4*a
    if discriminant < 0:
        return None
    root = math.sqrt(discriminant)
    xlo = 2*a/(allowed+root)
    xhi = (allowed+root)/2
    return (math.hypot(s, xlo)+rho_offset, math.hypot(s, xhi)+rho_offset)


def opening_lower_bound(sides, wall, seam, upper, margin):
    # Each cap sum decreases strictly as R grows. Binary search is used only
    # for this monotonic angle condition, never for the U-shaped neck.
    caps = []
    for side in sides:
        d = side["od"]
        wi = min(.04*d, .032*max(x["od"] for x in sides))
        caps.append(((d/2+seam, 0.0), (d/2-wi+seam, wall)))
    lower = max(radius+offset for pair in caps for radius, offset in pair)

    def clear(r):
        for i, first in enumerate(sides):
            for j in range(i+1, len(sides)):
                theta = math.atan2(norm(cross(first["u"], sides[j]["u"])),
                                   max(-1.0, min(1.0, dot(first["u"], sides[j]["u"]))))
                for inner in (0, 1):
                    si, wi = caps[i][inner]
                    sj, wj = caps[j][inner]
                    if r-wi <= si or r-wj <= sj:
                        return False
                    if math.asin(si/(r-wi))+math.asin(sj/(r-wj)) >= theta-margin:
                        return False
        return True

    if not clear(upper):
        return None
    if clear(lower):
        return lower
    high = upper
    for _ in range(100):
        middle = (lower+high)/2
        if middle == lower or middle == high:
            break
        if clear(middle):
            high = middle
        else:
            lower = middle
    return high


def _solve(ctx, targets, *, standard):
    sides = [source(targets, key) for key in "abcd"]
    for side in sides:
        if "status" in side:
            return side
    origin = ctx.product_origin()
    if not finite3(origin):
        return fail("origin", "Product origin must be finite")
    scale = max(1.0, *(abs(x) for side in sides for p in (side["a"], side["b"]) for x in p))
    if 128*EPS*scale > POSITION_TOL/8:
        return fail("numeric_capacity", "World coordinates cannot resolve intersection tolerance",
                    ambiguous=True)
    c = intersection(sides)
    if "status" in c:
        return c
    for i, a in enumerate(sides):
        for b in sides[i+1:]:
            if norm(sub(a["u"], b["u"])) <= 1e-7:
                return fail("coincident_ports", "Two retained rays coincide")
    windows = {}
    for side in sides:
        low = dot(sub(side["a"], c), side["u"])
        high = dot(sub(side["b"], c), side["u"])
        if not all(math.isfinite(x) for x in (low, high)) or high <= low:
            return fail("numeric_capacity", "Finite source window cannot be resolved",
                        ambiguous=True)
        windows[side["key"]] = (low, high)

    dmax = max(side["od"] for side in sides)
    wall = seam = .04*dmax
    if wall == 0 or seam == 0:
        return fail("numeric_capacity", "Chamber scale underflows finite arithmetic",
                    ambiguous=True)
    raw_bounds, bounds = [], []
    for side in sides:
        d = side["od"]
        wi = min(.04*d, .032*dmax)
        if d/2 == 0 or wi == 0 or d/2-wi <= 0:
            return fail("numeric_capacity", "Port radii or wall underflow finite arithmetic",
                        ambiguous=True)
        # The port must lie on its retained finite ray and leave a true
        # straight neck after the spherical transition. The margin below is
        # only an arithmetic representability allowance, not a physical
        # 0.1 OD minimum inherited from the independent sample.
        high = windows[side["key"]][1]
        clearance = 64*EPS*max(dmax, abs(high), 1e-9)
        for port_radius, offset in ((d/2, 0.0), (d/2-wi, wall)):
            s = port_radius+seam
            if s <= 0 or 2*seam*s == 0 or not math.isfinite(2*seam*s):
                return fail("numeric_capacity", "Neck scale exceeds finite arithmetic",
                            ambiguous=True)
            raw = neck_interval(port_radius, offset, seam, high, 0.0)
            if raw is None:
                return fail("no_finite_fit", "Source window is too short for a neck")
            raw_bounds.append(raw)
            interval = neck_interval(port_radius, offset, seam, high, clearance)
            if interval is None:
                return fail("joint_resolution", "Positive neck exists below numeric margin",
                            ambiguous=True)
            bounds.append(interval)
    raw_lo = max(pair[0] for pair in raw_bounds)
    raw_hi = min(pair[1] for pair in raw_bounds)
    if raw_lo > raw_hi:
        return fail("no_finite_fit", "Four neck constraints have no common chamber radius")
    rlo = max(pair[0] for pair in bounds)
    rhi = min(pair[1] for pair in bounds)
    if not all(math.isfinite(x) for x in (rlo, rhi)):
        return fail("numeric_capacity", "Chamber interval exceeds finite arithmetic", ambiguous=True)
    if rlo > rhi:
        return fail("joint_resolution", "Common neck fit is below numeric margin",
                    ambiguous=True)
    angle_lo = opening_lower_bound(sides, wall, seam, rhi, ANGLE_MARGIN)
    if angle_lo is None:
        if opening_lower_bound(sides, wall, seam, raw_hi, 0.0) is not None:
            return fail("joint_resolution", "Opening fit is below numeric margin",
                        ambiguous=True)
        return fail("opening_overlap", "Four inner or outer chamber caps overlap")
    rlo = max(rlo, angle_lo)
    if rlo > rhi:
        return fail("joint_resolution", "Opening fit is below numeric margin",
                    ambiguous=True)
    radius = min(max(.68*dmax, rlo), rhi)
    q = {}
    for side in sides:
        d = side["od"]
        wi = min(.04*d, .032*dmax)
        floor = max(windows[side["key"]][0], 0.0)
        clearance = 64*EPS*max(dmax, abs(windows[side["key"]][1]), 1e-9)
        for port_radius, offset in ((d/2, 0.0), (d/2-wi, wall)):
            s = port_radius+seam
            x = math.sqrt(max(0.0, (radius-offset)**2-s*s))
            if x <= 0:
                return fail("joint_resolution", "Chamber neck lost numeric clearance", ambiguous=True)
            floor = max(floor, x+2*seam*s/x+clearance)
        high = windows[side["key"]][1]
        if floor > high:
            return fail("joint_resolution", "Computed neck misses finite window", ambiguous=True)
        q[side["key"]] = min(max(1.5*d, floor), high)
    datum = c if standard else origin
    primary = sides[0]["u"]
    secondary = max((side["u"] for side in sides[1:]), key=lambda axis: norm(cross(primary, axis)))
    placement = frame(sub(c, datum), primary, cross(primary, secondary))
    params = {"local_center_m": [0.0, 0.0, 0.0], "chamber_radius_m": radius,
              "chamber_wall_m": wall, "seam_allowance_m": seam}
    for side in sides:
        key = side["key"]
        params[f"side_{key}_local_port_m"] = point_in_frame(sub(add(c, mul(side["u"], q[key])), datum), placement)
        params[f"side_{key}_outward_axis"] = direction_in_frame(side["u"], placement)
        params[f"side_{key}_reference_axis"] = direction_in_frame(reference(side["u"]), placement)
        params[f"side_{key}_outer_diameter_m"] = side["od"]
        params[f"side_{key}_wall_m"] = min(.04*side["od"], .032*dmax)
    internal = params
    return {"status": "SOLVED", "internal_parameters": internal,
            "components": [{"component_key": "cross"}],
            "fragments": [{"fragment_key": "cross_body", "binding_key": "cross_body_geometry",
                           "uniform_scale": 1.0, "local_transform": placement}]}


def realize(ctx):
    targets = {item["interface_key"]: item["condition"] for item in ctx.external_constraints()}
    return _solve(ctx, targets, standard=False)


def standard_model(ctx):
    targets = {
        "side_a": {"kind": "finite_segment", "start_m": [0, 0, 0], "end_m": [-4, 0, 0],
                   "outer_diameter_m": .3, "direction_relation": "same"},
        "side_b": {"kind": "finite_segment", "start_m": [0, 0, 0], "end_m": [4, 0, 0],
                   "outer_diameter_m": .3, "direction_relation": "same"},
        "side_c": {"kind": "finite_segment", "start_m": [0, 0, 0], "end_m": [0, -4, 0],
                   "outer_diameter_m": .5, "direction_relation": "same"},
        "side_d": {"kind": "finite_segment", "start_m": [0, 0, 0], "end_m": [0, 4, 0],
                   "outer_diameter_m": .5, "direction_relation": "same"},
    }
    return _solve(ctx, targets, standard=True)
