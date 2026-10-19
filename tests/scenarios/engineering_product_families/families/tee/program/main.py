"""Pure numeric TEE proposal from three finite, identified source segments."""

import math

from .shared_solver import _canonical_opposed_axes
from .local_geometry_frame import frame, point_in_frame, direction_in_frame


EPS = 2.220446049250313e-16
POSITION_TOL = 1e-6
DIRECTION_TOL = 1e-7


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def add(a, b):
    return [x + y for x, y in zip(a, b)]


def sub(a, b):
    return [x - y for x, y in zip(a, b)]


def mul(a, s):
    return [x * s for x in a]


def cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def norm(a):
    return math.hypot(*a)


def unit(a):
    scale = max(abs(x) for x in a)
    if scale == 0:
        return None
    scaled = [x/scale for x in a]
    return [x/norm(scaled) for x in scaled]


def finite3(value):
    return len(value) == 3 and all(math.isfinite(x) for x in value)


def fail(key, message, *, ambiguous=False):
    kind = "ambiguities" if ambiguous else "violations"
    item = "ambiguity_key" if ambiguous else "violation_key"
    return {"status": "AMBIGUOUS" if ambiguous else "INFEASIBLE",
            kind: [{item: key, "message": message}]}


def source(targets, roles, key):
    target = targets.get("side_" + key)
    if target is None or target["kind"] != "finite_segment" or target["direction_relation"] != "same":
        return fail("source_condition", f"Side {key} needs a finite_segment with same direction")
    a, b, d = target["start_m"], target["end_m"], target["outer_diameter_m"]
    role = roles[key]
    if not finite3(a) or not finite3(b):
        return fail("source_coordinates", f"Side {key} needs finite A/B coordinates")
    if not math.isfinite(d) or d <= 0:
        return fail("source_od", f"Side {key} needs positive finite OD")
    if role not in ("main_a", "main_b", "branch", "independent"):
        return fail("source_role", f"Side {key} has invalid role")
    length = norm(sub(b, a))
    if not math.isfinite(length) or length <= 0:
        return fail("source_length", f"Side {key} has no finite positive length")
    u = unit(sub(b, a))
    if u is None:
        return fail("numeric_capacity", "Source direction cannot be resolved", ambiguous=True)
    return {"key": key, "a": a, "b": b, "u": u, "od": d, "length": length,
            "role": role}


def reference(axis):
    preferred = [0.0, 0.0, 1.0]
    projected = sub(preferred, mul(axis, dot(preferred, axis)))
    if norm(projected) < 32*math.sqrt(EPS):
        preferred = [1.0, 0.0, 0.0]
        projected = sub(preferred, mul(axis, dot(preferred, axis)))
    return unit(projected)


def intersection(sides):
    # Use the best-conditioned two lines; then verify all three independent axes.
    pairs = [(norm(cross(sides[i]["u"], sides[j]["u"])), i, j)
             for i, j in ((0, 1), (0, 2), (1, 2))]
    sine, i, j = max(pairs)
    if sine < 1e-10:
        return fail("common_intersection", "Three parallel axes do not give a unique tee junction")
    a, b = sides[i], sides[j]
    w = sub(b["a"], a["a"])
    n = cross(a["u"], b["u"])
    ta = dot(cross(w, b["u"]), n)/(sine*sine)
    c = add(a["a"], mul(a["u"], ta))
    if not finite3(c):
        return fail("numeric_capacity", "Intersection exceeds finite arithmetic", ambiguous=True)
    for side in sides:
        station = dot(sub(c, side["a"]), side["u"])
        foot = add(side["a"], mul(side["u"], station))
        if norm(sub(c, foot)) > POSITION_TOL:
            return fail("common_intersection", "Three source axes have no common 3D point")
    return c


def select_roles(sides):
    roles = [side["role"] for side in sides]
    selected = None
    if sorted(roles) == ["branch", "main_a", "main_b"]:
        selected = (next(side for side in sides if side["role"] == "main_a"),
                    next(side for side in sides if side["role"] == "main_b"),
                    next(side for side in sides if side["role"] == "branch"))
        a, b, branch = selected
        if (abs(dot(branch["u"], a["u"])) <= 1-1e-6
                and abs(dot(branch["u"], b["u"])) <= 1-1e-6):
            return selected
        # Source continuity need not be the geometric through pair. Only the
        # existing tangent-branch conflict reuses the unique direction selector;
        # source dictionaries, roles and side keys remain unchanged.
    elif roles != ["independent"]*3:
        return fail("source_roles", "Roles must identify all three sides or all be independent")
    pairs = []
    for i, j in ((0, 1), (0, 2), (1, 2)):
        angle = math.acos(max(-1.0, min(1.0, dot(sides[i]["u"], sides[j]["u"]))))
        pairs.append((math.pi-angle, i, j))
    pairs = sorted(pairs)
    if pairs[1][0] - pairs[0][0] <= DIRECTION_TOL:
        if selected is not None:
            # No unique replacement: preserve the original known-role result
            # and let its existing tangent-branch check reject it.
            return selected
        return fail("main_pair", "Main pair is tied within direction tolerance", ambiguous=True)
    _, i, j = pairs[0]
    main = sorted((sides[i], sides[j]), key=lambda side: tuple(side["u"]))
    branch = next(side for side in sides if side is not sides[i] and side is not sides[j])
    return (*main, branch)


def collar(diameter, fitting_diameter):
    scaled = 1.1*diameter
    thickness = (scaled+fitting_diameter)*.05
    radius = (scaled*.55 if scaled > fitting_diameter
              else (scaled+fitting_diameter/5)*.525)
    return thickness, radius


def point_segment_projection(point, start, end):
    segment = sub(end, start)
    length_squared = dot(segment, segment)
    if length_squared == 0:
        return start
    station = max(0.0, min(1.0, dot(sub(point, start), segment)/length_squared))
    return add(start, mul(segment, station))


def point_segment_distance(point, start, end):
    return norm(sub(point, point_segment_projection(point, start, end)))


def collar_capsule_gap(center, axis, half_thickness, radius, start, end, capsule_radius, tolerance):
    delta = sub(center, point_segment_projection(center, start, end))
    distance = norm(delta)
    if distance == 0:
        return -math.inf
    normal = mul(delta, 1/distance)
    support = half_thickness*abs(dot(normal, axis))+radius*norm(cross(normal, axis))
    return distance-support-capsule_radius-tolerance


def main_middle(a, b, bend, turn):
    if turn == 0:
        return [0.0, 0.0, 0.0]
    flow = mul(a["u"], -1)
    normal = unit(cross(flow, b["u"]))
    radial = unit(cross(normal, flow))
    half = turn/2
    along = .9*bend*2*math.sin(half/2)**2
    across = .9*bend*math.cos(half)*math.tan(half/2)
    return add(mul(a["u"], along), mul(radial, across))


def axial_envelope_gap(point, axis, start, end, first_radius, last_radius):
    """Exact minimum distance to a finite linear-radius axial envelope."""
    projection = dot(point, axis)
    perpendicular_squared = max(0.0, dot(point, point)-projection*projection)
    slope = (last_radius-first_radius)/(end-start)
    stations = [start, end, max(start, min(end, projection))]
    if abs(slope) < 1:
        stationary = projection+slope*math.sqrt(perpendicular_squared/(1-slope*slope))
        if start <= stationary <= end:
            stations.append(stationary)
    return min(norm(sub(point, mul(axis, station)))
               -(first_radius+slope*(station-start)) for station in stations)


def branch_port_margin(a, b, branch, ea, eb, ebranch, bend, neck, turn, fmain, fmax):
    """Check both terminal directions against their opposite finite core envelopes."""
    middle = main_middle(a, b, bend, turn)
    axis = branch["u"]
    controls = (middle, mul(axis, .7*neck), mul(axis, .9*neck), mul(axis, neck))
    if .7*neck-dot(middle, axis) <= 0 or norm(sub(controls[1], middle)) <= .05*branch["od"]:
        return -math.inf
    neck_end = controls[-1]
    projected_middle = mul(axis, dot(middle, axis))
    deviation = max(point_segment_distance(point, middle, neck_end)
                    for point in controls+(projected_middle,))
    scaled = 1.1*branch["od"]
    thickness, collar_outer = collar(branch["od"], fmax)
    port_station = neck+ebranch
    collar_start = port_station-1.1*thickness
    collar_end = port_station-.1*thickness
    if collar_end <= neck:
        return -math.inf
    tolerance = 2e-6*fmax
    margins = []
    for side, allowance in ((a, ea), (b, eb)):
        port = mul(side["u"], bend+allowance)
        port_radius = side["od"]/2
        neck_gap = point_segment_distance(port, middle, neck_end)-deviation-fmax/2
        taper_gap = axial_envelope_gap(port, axis, neck, collar_end, fmax/2, scaled/2)
        collar_gap = axial_envelope_gap(port, axis, collar_start, collar_end,
                                        collar_outer, collar_outer)
        terminal_gap = axial_envelope_gap(port, axis, collar_end, port_station,
                                          scaled/2, branch["od"]/2)
        extension_gap = axial_envelope_gap(port, axis, port_station,
                                            port_station+.001*branch["od"],
                                            branch["od"]*.43, branch["od"]*.43)
        margins.extend(gap-port_radius-tolerance for gap in
                       (neck_gap, taper_gap, collar_gap, terminal_gap, extension_gap))
        main_thickness, main_radius = collar(side["od"], fmain)
        collar_center = mul(side["u"], bend+allowance-.6*main_thickness)
        margins.append(collar_capsule_gap(collar_center, side["u"], main_thickness/2,
                       main_radius, middle, neck_end, deviation+fmax/2, tolerance))
    center = mul(axis, port_station-.6*thickness)
    start, end = mul(a["u"], bend), mul(b["u"], bend)
    if turn == 0:
        margins.append(collar_capsule_gap(center, axis, thickness/2, collar_outer,
                                         start, end, fmain/2, tolerance))
    else:
        ta, tb = mul(a["u"], .9*bend), mul(b["u"], .9*bend)
        sagitta = norm(sub(middle, mul(add(ta, tb), .5)))
        margins.extend((
            collar_capsule_gap(center, axis, thickness/2, collar_outer, start, ta, fmain/2, tolerance),
            collar_capsule_gap(center, axis, thickness/2, collar_outer, ta, tb, sagitta+fmain/2, tolerance),
            collar_capsule_gap(center, axis, thickness/2, collar_outer, tb, end, fmain/2, tolerance),
        ))
    return min(margins)




def joint_scales(a, b, branch, ea, eb, ebranch, limits, preferred, fmain, fmax, turn):
    """Return the first certified finite B/k witness in deterministic preference order."""
    b_low, b_high, k_low, k_high = limits
    bpref, kpref = preferred
    ta, ra = collar(a["od"], fmain)
    tb, rb = collar(b["od"], fmain)
    tv, rv = collar(branch["od"], fmax)
    main_constraints = (
        (ea, eb, (a["od"]+b["od"])/2+2e-6*fmax),
        (ea-.6*ta, eb-.6*tb,
         math.hypot(ra, ta/2)+math.hypot(rb, tb/2)+2e-6*fmax),
    )

    def distance(x, y):
        return norm(sub(x, y))

    # Only the actual main port distance gives a necessary infeasibility proof.
    for index, (first, second, clearance) in enumerate(main_constraints):
        def gap(scale):
            return distance(mul(a["u"], scale+first), mul(b["u"], scale+second))-clearance
        if gap(b_high) < 0:
            return None, index == 0
        if gap(b_low) < 0:
            low, high = b_low, b_high
            while high-low > POSITION_TOL/8:
                middle = (low+high)/2
                if gap(middle) < 0:
                    low = middle
                else:
                    high = middle
            b_low = high

    branch_data = []
    for side, allowance in ((a, ea), (b, eb)):
        thickness, radius = collar(side["od"], fmain)
        cosine = dot(side["u"], branch["u"])
        branch_data.extend((
            (side, allowance, ebranch, cosine,
             (side["od"]+branch["od"])/2+2e-6*fmax),
            (side, allowance-.6*thickness, ebranch-.6*tv, cosine,
             math.hypot(radius, thickness/2)+math.hypot(rv, tv/2)+2e-6*fmax),
        ))

    margins = {}
    def margin(scale, neck):
        key = (scale, neck)
        if key in margins:
            return margins[key]
        margins[key] = min(
            *(distance(mul(side["u"], scale+allowance),
                       mul(branch["u"], neck+branch_allowance))-clearance
              for side, allowance, branch_allowance, _, clearance in branch_data),
            branch_port_margin(a, b, branch, ea, eb, ebranch, scale, neck, turn, fmain, fmax),
        )
        return margins[key]

    tolerance = 1e-12*fmax
    if b_low <= bpref <= b_high and k_low <= kpref <= k_high and margin(bpref, kpref) >= -tolerance:
        return (bpref, kpref), False

    def candidate(scale):
        choices = [min(max(kpref, k_low), k_high), k_low, k_high]
        for _, allowance, branch_allowance, cosine, clearance in branch_data:
            q = scale+allowance
            discriminant = clearance*clearance-q*q*max(0.0, 1-cosine*cosine)
            if discriminant >= 0:
                half = math.sqrt(discriminant)
                choices.extend((q*cosine-half-branch_allowance,
                                q*cosine+half-branch_allowance))
        for neck in sorted(set(choices), key=lambda value: (abs(value-kpref), value)):
            if k_low <= neck <= k_high and margin(scale, neck) >= -tolerance:
                return scale, neck
        return None

    anchors = [b_low, b_high, min(max(bpref, b_low), b_high)]
    for _, allowance, branch_allowance, cosine, clearance in branch_data:
        sine2 = max(0.0, 1-cosine*cosine)
        if sine2 > 1e-14:
            anchors.append(clearance/math.sqrt(sine2)-allowance)
        for neck in (k_low, k_high, kpref):
            q = neck+branch_allowance
            discriminant = clearance*clearance-q*q*sine2
            if discriminant >= 0:
                half = math.sqrt(discriminant)
                anchors.extend((q*cosine-half-allowance, q*cosine+half-allowance))
    anchors = sorted({value for value in anchors if b_low <= value <= b_high},
                     key=lambda value: (abs(value-bpref), value))
    for scale in anchors:
        trial = candidate(scale)
        if trial is not None:
            return trial, False

    # Reuse the finite contact samples, retaining each complete (B,k) witness.
    neck_choices = []
    for neck in (min(max(kpref, k_low), k_high), k_low, k_high):
        if neck not in neck_choices:
            neck_choices.append(neck)
    for neck in neck_choices:
        for index in range(5):
            scale = b_low+(b_high-b_low)*index/4
            if margin(scale, neck) >= -tolerance:
                return (scale, neck), False
        # A sampled feasible boundary is already a witness. No global preference
        # minimization or uncertified empty-domain conclusion is needed.
    return None, False


def _default_node(sides, station, origin, standard, reason):
    ports = {}
    for side in sides:
        point = side["a"]
        ports["side_" + side["key"]] = {
            "origin_m": sub(point, station), "axis": side["u"],
            "reference_direction": reference(side["u"]), "outer_diameter_m": side["od"]}
    translation = [0.0, 0.0, 0.0] if standard else sub(station, origin)
    if not all(math.isfinite(v) for vector in [translation] + [p["origin_m"] for p in ports.values()] for v in vector):
        return fail("numeric_capacity", "Default node placement exceeds finite arithmetic", ambiguous=True)
    geometry = {"diameter_m": max(side["od"] for side in sides), "interfaces": [
        {"kind": "circular", "key": key, "origin_m": port["origin_m"],
         "axis": port["axis"], "reference_direction": port["reference_direction"],
         "diameter_m": port["outer_diameter_m"]} for key, port in ports.items()]}
    return {"status": "SOLVED", "internal_parameters": {
        **{"side_" + side["key"] + "_outer_diameter_m": side["od"] for side in sides},
        "sphere_diameter_m": geometry["diameter_m"], "sphere_interfaces": geometry["interfaces"],
        "construction": "default", "default_reason": reason},
        "components": [{"component_key": "tee"}],
        "fragments": [{"fragment_key": "tee_body", "binding_key": "sphere_geometry",
            "uniform_scale": 1.0, "local_transform": {"translation_m": translation,
                "rotation": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]}}]}


def _solve(ctx, targets, roles, *, standard):
    sides = [source(targets, roles, key) for key in "abc"]
    for side in sides:
        if "status" in side:
            return side
    origin = ctx.product_origin()
    if not finite3(origin):
        return fail("origin", "Product origin must be finite")
    coordinate_scale = max(1.0, *(abs(x) for side in sides for p in (side["a"], side["b"]) for x in p))
    if 128*EPS*coordinate_scale > POSITION_TOL/8:
        return fail("numeric_capacity", "World coordinate scale cannot resolve intersection tolerance", ambiguous=True)
    c = intersection(sides)
    if "status" in c:
        return c
    selected = select_roles(sides)
    if "status" in selected:
        return selected
    a, b, branch = selected
    pair_scale = max(a["length"], b["length"], a["od"], b["od"])
    pair_coordinate_scale = max(1.0, *(abs(x) for side in (a, b)
                                      for point in (side["a"], side["b"]) for x in point))
    arithmetic_error = 128*EPS*max(pair_coordinate_scale, pair_scale)
    canonical = _canonical_opposed_axes(a, b, c, arithmetic_error)
    if canonical is not None:
        a, b = canonical
        sides = [a if side["key"] == a["key"] else
                 b if side["key"] == b["key"] else side for side in sides]
    angle = math.atan2(norm(cross(a["u"], b["u"])),
                       max(-1.0, min(1.0, -dot(a["u"], b["u"]))))
    if angle > math.radians(150)+64*EPS:
        return fail("unsupported_domain", "True main turn exceeds 150 degrees")
    if angle == 0 and norm(add(a["u"], b["u"])) > DIRECTION_TOL:
        return fail("unsupported_domain", "Same-direction main rays are not a straight tee")
    # Source topology errors cannot become a default merely because a later
    # detailed occupancy test also fails.
    if abs(dot(branch["u"], a["u"])) > 1-1e-6 or abs(dot(branch["u"], b["u"])) > 1-1e-6:
        return fail("branch_tangent", "Branch cannot share a main ray")
    bounds = {}
    for side in sides:
        low = dot(sub(side["a"], c), side["u"])
        high = dot(sub(side["b"], c), side["u"])
        if not all(math.isfinite(x) for x in (low, high)) or high <= low:
            return fail("numeric_capacity", "Finite source window cannot be resolved", ambiguous=True)
        bounds[side["key"]] = (low, high)
    fa, fb = 1.1*a["od"], 1.1*b["od"]
    fmain = max(fa, fb)
    fmax = max(1.1*side["od"] for side in sides)
    ea = min(fmain*(fmain-fa)/(fa+fb)+.5*fmain, 2*fmain)+.3*fmain
    eb = min(fmain*(fmain-fb)/(fa+fb)+.5*fmain, 2*fmain)+.3*fmain
    ebranch = 2.1*fmax*(fmax-1.1*branch["od"])/(fmax+branch["od"])+.45*fmax
    bpref = fmain*(.4*angle+.2)+.3*fmain
    kpref = 1.4*fmain
    # The selected A main sweep is fmain/2; its arc must not fold over itself.
    main_radius = fmain/2
    curvature_floor = (main_radius*1.01*math.tan(angle/2)/.9 if angle else 0.0)
    b_low = max(bounds[a["key"]][0]-ea, bounds[b["key"]][0]-eb,
                curvature_floor, .05*fmain)
    b_high = min(bounds[a["key"]][1]-ea, bounds[b["key"]][1]-eb)
    k_low = max(bounds[branch["key"]][0]-ebranch, .05*fmax)
    k_high = bounds[branch["key"]][1]-ebranch
    if not all(math.isfinite(x) for x in (b_low, b_high, k_low, k_high, bpref, kpref)):
        return fail("numeric_capacity", "TEE feasible interval exceeds finite arithmetic", ambiguous=True)
    if b_low > b_high or k_low > k_high:
        return _default_node(sides, c, origin, standard, "main_or_branch_window_empty")
    solved, proven_empty = joint_scales(
        a, b, branch, ea, eb, ebranch,
        (b_low, b_high, k_low, k_high), (bpref, kpref), fmain, fmax, angle)
    if solved is None:
        if proven_empty:
            return _default_node(sides, c, origin, standard, "main_port_clearance_unavailable")
        return _default_node(sides, c, origin, standard, "joint_scale_layout_not_found")
    bend, neck = solved
    # The near-zero body is a deliberate local representation of this TEE's
    # main, not a change to the source connection frames or a generic angle
    # tolerance. Geometry independently checks the same envelope and its faces.
    near_zero_limit = math.radians(.01)
    approximation_budget = min(50e-6, 2e-4*min(a["od"], b["od"]))
    main_end = max(bend+ea, bend+eb)
    approximation_bound = (main_end+fmain/2+bend)*math.sin(angle/2)
    near_zero = 0 < angle <= near_zero_limit and approximation_bound <= approximation_budget
    radius = .9*bend/math.tan(angle/2) if angle and not near_zero else 0.0
    if angle and not near_zero and (not math.isfinite(radius) or radius/fmain > 1e8):
        return fail("numeric_capacity", "True arc radius exceeds supported modeling resolution", ambiguous=True)
    q = {a["key"]: bend+ea, b["key"]: bend+eb,
         branch["key"]: neck+ebranch}
    ports = {side["key"]: add(c, mul(side["u"], q[side["key"]])) for side in sides}
    for i, first in enumerate(sides):
        for second in sides[i+1:]:
            if (norm(sub(ports[first["key"]], ports[second["key"]])) <
                    .5*(first["od"]+second["od"]) + 1e-6*fmax):
                return fail("joint_scale_resolution",
                            "Joint B/k candidate lost port clearance in arithmetic",
                            ambiguous=True)
    reference_point = c if standard else origin
    shape = {
        "mode": "straight" if angle == 0 else "near_zero" if near_zero else "curved",
        "local_center_m": sub(c, reference_point),
        "bend_scale_m": bend,
        "branch_neck_scale_m": neck,
        "main_a_key": a["key"], "main_b_key": b["key"], "branch_key": branch["key"],
    }
    if angle != 0:
        shape["turn_radian"] = angle
        if not near_zero:
            shape["arc_radius_m"] = radius
    frames = {}
    for side in sides:
        key = side["key"]
        frames["side_" + key] = {"origin_m": sub(ports[key], reference_point), "axis": side["u"],
            "reference_direction": reference(side["u"]), "outer_diameter_m": side["od"]}
    if not all(finite3(port["origin_m"]) for port in frames.values()):
        return fail("numeric_capacity", "Local port conversion exceeds finite arithmetic", ambiguous=True)
    flow = mul(a["u"], -1.0)
    normal = cross(flow, b["u"])
    if norm(normal) <= 32*math.sqrt(EPS):
        normal = cross(flow, branch["u"])
    placement = frame(shape["local_center_m"], flow, normal)
    shape["local_center_m"] = [0.0, 0.0, 0.0]
    for port in frames.values():
        port["origin_m"] = point_in_frame(port["origin_m"], placement)
        port["axis"] = direction_in_frame(port["axis"], placement)
        port["reference_direction"] = direction_in_frame(port["reference_direction"], placement)
    params = {"ports": frames, "shape": shape}
    internal = {**params, "construction": "detailed", "default_reason": "none",
                **{"side_" + side["key"] + "_outer_diameter_m": side["od"] for side in sides}}
    return {"status": "SOLVED", "internal_parameters": internal,
            "components": [{"component_key": "tee"}],
            "fragments": [{"fragment_key": "tee_body", "binding_key": "tee_body_geometry",
                           "uniform_scale": 1.0, "local_transform": placement}]}


def realize(ctx):
    targets = {item["interface_key"]: item["condition"] for item in ctx.external_constraints()}
    roles = {key: ctx.param("side_" + key + "_role") for key in "abc"}
    return _solve(ctx, targets, roles, standard=False)


def standard_model(ctx):
    targets = {
        "side_a": {"kind": "finite_segment", "start_m": [0, 0, 0], "end_m": [-4, 0, 0],
                   "outer_diameter_m": .2, "direction_relation": "same"},
        "side_b": {"kind": "finite_segment", "start_m": [0, 0, 0], "end_m": [4, 0, 0],
                   "outer_diameter_m": .2, "direction_relation": "same"},
        "side_c": {"kind": "finite_segment", "start_m": [0, 0, 0], "end_m": [0, 4, 0],
                   "outer_diameter_m": .1, "direction_relation": "same"},
    }
    roles = {key: ctx.param("side_" + key + "_role") for key in "abc"}
    return _solve(ctx, targets, roles, standard=True)
