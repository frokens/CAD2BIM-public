"""Pure numeric PIPE solver with fixed external endpoints and a retained middle path."""

import math

from .local_geometry_frame import frame, point_in_frame, direction_in_frame


_BINARY64_EPSILON = 2.220446049250313e-16


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def _distance_squared(a, b):
    return sum((a[i] - b[i]) ** 2 for i in range(3))


def _binary_fraction(value):
    """Exact integer / power-of-two representation of one finite JSON float."""
    if not math.isfinite(value):
        return None
    numerator = value
    exponent = 0
    while numerator != math.floor(numerator):
        numerator *= 2
        exponent += 1
        if exponent > 1074 or not math.isfinite(numerator):
            return None
    return int(numerator), exponent


def _fraction_difference(positive, negative):
    exponent = max(positive[1], negative[1])
    return (positive[0] * 2 ** (exponent - positive[1])
            - negative[0] * 2 ** (exponent - negative[1]), exponent)


def _product_equal(a, b, c, d):
    left, left_exponent = a[0] * b[0], a[1] + b[1]
    right, right_exponent = c[0] * d[0], c[1] + d[1]
    if left_exponent < right_exponent:
        left *= 2 ** (right_exponent - left_exponent)
    else:
        right *= 2 ** (left_exponent - right_exponent)
    return left == right


def _line_relation(a, b, c):
    """Return +1 forward, -1 reverse, 0 turned, or None if unresolved."""
    coordinates = [[_binary_fraction(point[i]) for i in range(3)] for point in (a, b, c)]
    if any(value is None for point in coordinates for value in point):
        return None
    left = [_fraction_difference(coordinates[1][i], coordinates[0][i]) for i in range(3)]
    right = [_fraction_difference(coordinates[2][i], coordinates[1][i]) for i in range(3)]
    if all(value[0] == 0 for value in left) or all(value[0] == 0 for value in right):
        return None
    for i, j in ((0, 1), (1, 2), (2, 0)):
        if not _product_equal(left[i], right[j], left[j], right[i]):
            return 0
    for i in range(3):
        if left[i][0]:
            return 1 if (left[i][0] > 0) == (right[i][0] > 0) else -1
    return None


def _same_forward_line(a, b, c):
    relation = _line_relation(a, b, c)
    return None if relation is None else relation == 1


def _segment_distance_squared(a, b, c, d):
    """Finite-segment minimum, including parallel and end-point cases."""
    u, v, w = _sub(b, a), _sub(d, c), _sub(a, c)
    uu, uv, vv, uw, vw = _dot(u, u), _dot(u, v), _dot(v, v), _dot(u, w), _dot(v, w)
    candidates = []
    for s in (0.0, 1.0):
        t = max(0.0, min(1.0, (vw + s * uv) / vv))
        candidates.append(_distance_squared(
            [a[i] + s * u[i] for i in range(3)], [c[i] + t * v[i] for i in range(3)]
        ))
    for t in (0.0, 1.0):
        s = max(0.0, min(1.0, (t * uv - uw) / uu))
        candidates.append(_distance_squared(
            [a[i] + s * u[i] for i in range(3)], [c[i] + t * v[i] for i in range(3)]
        ))
    denominator = uu * vv - uv * uv
    if denominator > 0:
        s = (uv * vw - vv * uw) / denominator
        t = (uu * vw - uv * uw) / denominator
        if 0 <= s <= 1 and 0 <= t <= 1:
            candidates.append(_distance_squared(
                [a[i] + s * u[i] for i in range(3)], [c[i] + t * v[i] for i in range(3)]
            ))
    return min(candidates)


def _infeasible(key, message):
    return {"status": "INFEASIBLE", "violations": [
        {"violation_key": key, "message": message}
    ]}


def _unverified(key, message):
    return {"status": "AMBIGUOUS", "ambiguities": [
        {"ambiguity_key": key, "message": message}
    ]}


def _bound(low, high):
    """Outward binary64 bounds used only for PIPE miter separation."""
    if not math.isfinite(low) or not math.isfinite(high):
        return (-math.inf, math.inf)
    # Cover both the rounded operation and this outward addition/subtraction.
    # The positive subnormal floor also covers underflow at zero. Use only the
    # arithmetic in the existing modeling capability (no nextafter extension).
    padding = max(4 * _BINARY64_EPSILON * max(abs(low), abs(high)), 2e-323)
    return (low - padding, high + padding)


def _badd(a, b):
    return _bound(a[0] + b[0], a[1] + b[1])


def _bmul(a, b):
    products = [x * y for x in a for y in b]
    if any(not math.isfinite(value) for value in products):
        return (-math.inf, math.inf)
    return _bound(min(products), max(products))


def _bdiv(a, b):
    if b[0] <= 0 <= b[1]:
        return (-math.inf, math.inf)
    return _bmul(a, _bound(1 / b[1], 1 / b[0]))


def _bdot(a, b):
    result = (0.0, 0.0)
    for left, right in zip(a, b):
        result = _badd(result, _bmul(left, right))
    return result


def _bnorm(vector):
    squared = (0.0, 0.0)
    for low, high in vector:
        minimum = 0.0 if low <= 0 <= high else min(low * low, high * high)
        squared = _badd(squared, _bound(minimum, max(low * low, high * high)))
    if not math.isfinite(squared[1]) or squared[1] < 0:
        return (0.0, math.inf)
    low, high = _bound(math.sqrt(max(0.0, squared[0])), math.sqrt(squared[1]))
    return (max(0.0, low), high)


def _pipe_miter_bounds(points, tangents, planes, radius, baseline):
    # Track world-to-local subtraction as well as local direction arithmetic.
    # The original nominal tangents/planes remain the construction facts.
    positions = [[_bound(p[i] - baseline[i], p[i] - baseline[i]) for i in range(3)]
                 for p in points]
    directions = []
    for index, (left, right) in enumerate(zip(positions, positions[1:])):
        delta = [_badd(b, (-a[1], -a[0])) for a, b in zip(left, right)]
        length = _bnorm(delta)
        direction = [_bdiv(value, length) for value in delta]
        directions.append([(min(value[0], nominal), max(value[1], nominal))
                           for value, nominal in zip(direction, tangents[index])])
    result = []
    for index, direction in enumerate(directions):
        previous = directions[index - 1] if index else direction
        following = directions[index + 1] if index + 1 < len(directions) else direction
        normals, reaches = [], []
        for neighbor, nominal in zip((previous, following), planes[index]):
            normal = [_badd(a, b) for a, b in zip(direction, neighbor)]
            normal = [(min(value[0], value0), max(value[1], value0))
                      for value, value0 in zip(normal, nominal)]
            denominator = _bdot(normal, direction)
            if denominator[0] <= 0:
                break
            transverse = [_badd(_bdiv(n, denominator), (-e[1], -e[0]))
                          for n, e in zip(normal, direction)]
            reach = _bmul((radius, radius), _bnorm(transverse))[1]
            if not math.isfinite(reach):
                break
            normals.append(normal)
            reaches.append(reach)
        if len(normals) != 2:
            result.append(None)
            continue
        endpoints = positions[index:index + 2]
        box = []
        for axis in range(3):
            reach = _bmul((0.0, max(reaches)),
                          (0.0, max(abs(value) for value in direction[axis])))[1]
            padding = _badd((radius, radius), (0.0, reach))[1]
            box.append(_bound(min(point[axis][0] for point in endpoints) - padding,
                              max(point[axis][1] for point in endpoints) + padding))
        result.append((endpoints, direction, normals, box))
    return result


def _pipe_support(piece, axis, anchor, radius):
    endpoints, direction, normals, _ = piece
    ranges = []
    for point, normal in zip(endpoints, normals):
        denominator = _bdot(normal, direction)
        if denominator[0] <= 0:
            return (-math.inf, math.inf)
        ratio = _bdiv(_bdot(axis, direction), denominator)
        transverse = [_badd(a, (-value[1], -value[0]))
                      for a, value in zip(axis, [_bmul(ratio, n) for n in normal])]
        reach = _bmul((radius, radius), _bnorm(transverse))[1]
        offset = [_badd(p, (-q[1], -q[0])) for p, q in zip(point, anchor)]
        center = _bdot(axis, offset)
        ranges.append(_bound(center[0] - reach, center[1] + reach))
    return (min(value[0] for value in ranges), max(value[1] for value in ranges))


def _pipe_separated(left, right, radius):
    if left is None or right is None:
        return False
    # An enclosing box of the axially extended cylinders is a safe broad phase;
    # no ill-conditioned near-parallel closest-point division can admit a pair.
    if any(a[1] < b[0] or b[1] < a[0] for a, b in zip(left[3], right[3])):
        return True
    anchor = left[0][0]
    for normal in left[2] + right[2]:
        a = _pipe_support(left, normal, anchor, radius)
        b = _pipe_support(right, normal, anchor, radius)
        if a[1] < b[0] or b[1] < a[0]:
            return True
    return False


def _solve(ctx, targets, middle_points, *, standard):
    if len(targets) != 2 or any(target["condition"]["kind"] != "fixed_point" for target in targets):
        return _infeasible("fixed_endpoints", "PIPE requires fixed inlet and outlet conditions")
    by_key = {target["interface_key"]: target["condition"] for target in targets}
    if "inlet" not in by_key or "outlet" not in by_key:
        return _infeasible("fixed_endpoints", "PIPE requires fixed inlet and outlet conditions")
    inlet, outlet = by_key["inlet"], by_key["outlet"]
    points = [inlet["point_m"]] + middle_points + [outlet["point_m"]]
    outer_diameter = inlet.get("outer_diameter_m")
    if outer_diameter is None or outlet.get("outer_diameter_m") != outer_diameter:
        return _infeasible("endpoint_diameter", "PIPE endpoints require one equal outside diameter")
    section = ctx.param("section")
    origin = ctx.product_origin()
    if len(points) < 2:
        return _infeasible("path_cardinality", "PIPE requires at least two path points")
    if not math.isfinite(outer_diameter) or outer_diameter <= 0:
        return _infeasible("outer_diameter", "PIPE outer diameter must be positive")
    if len(section["known_wall_thickness_m"]) > 1:
        return _infeasible("section", "PIPE accepts at most one known wall thickness")
    if section["known_wall_thickness_m"]:
        thickness = section["known_wall_thickness_m"][0]
        if not math.isfinite(thickness) or not (0 < thickness < outer_diameter / 2):
            return _infeasible("wall_thickness", "Known wall thickness must be below the outer radius")
    if any(not math.isfinite(value) for point in points for value in point):
        return _infeasible("path_finite", "All path coordinates must be finite")

    vectors = [_sub(b, a) for a, b in zip(points, points[1:])]
    lengths = [math.sqrt(_dot(vector, vector)) for vector in vectors]
    for index, length in enumerate(lengths):
        if length == 0:
            if points[index] == points[index + 1]:
                return _infeasible("zero_segment", "Adjacent PIPE path points coincide")
            return _unverified("numeric_capacity", "Positive segment length underflows numeric resolution")
    if any(not math.isfinite(length) for length in lengths):
        return _unverified("numeric_capacity", "Path arithmetic exceeds finite numeric capacity")
    outward_axes = [[-value / lengths[0] for value in vectors[0]],
                    [value / lengths[-1] for value in vectors[-1]]]
    for condition, actual_axis in zip((inlet, outlet), outward_axes):
        axis = condition["axis"]
        scale = max(abs(value) for value in axis)
        relation = condition.get("direction_relation", "opposed")
        if not math.isfinite(scale) or scale <= 0 or relation not in ("same", "opposed"):
            return _infeasible("endpoint_direction", "PIPE endpoint direction is invalid")
        scaled_axis = [value / scale for value in axis]
        norm = math.hypot(*scaled_axis)
        sign = 1 if relation == "same" else -1
        aligned = [sign * value / norm for value in scaled_axis]
        angle = math.atan2(math.hypot(
            actual_axis[1] * aligned[2] - actual_axis[2] * aligned[1],
            actual_axis[2] * aligned[0] - actual_axis[0] * aligned[2],
            actual_axis[0] * aligned[1] - actual_axis[1] * aligned[0]), _dot(actual_axis, aligned))
        if angle > condition.get("angular_tolerance_rad", 1e-6):
            return _infeasible("endpoint_direction", "PIPE fixed axis differs from its actual outward endpoint axis")
    # Source subdivisions are not physical corners. Use maximal exactly
    # forward-collinear chains for feasibility, but retain every source point
    # in the solved path passed to Geometry below.
    chain_points = [points[0]]
    for index in range(1, len(vectors)):
        same = _same_forward_line(points[index - 1], points[index], points[index + 1])
        if same is None:
            return _unverified("numeric_capacity", "Collinearity arithmetic exceeds finite capacity")
        if not same:
            chain_points.append(points[index])
    chain_points.append(points[-1])
    chain_vectors = [_sub(b, a) for a, b in zip(chain_points, chain_points[1:])]
    chain_lengths = [math.sqrt(_dot(vector, vector)) for vector in chain_vectors]
    if any(not math.isfinite(length) or length <= 0 for length in chain_lengths):
        return _unverified("numeric_capacity", "Collapsed chain length is numerically unresolved")
    tangents = [[value / length for value in vector]
                for vector, length in zip(chain_vectors, chain_lengths)]
    radius = outer_diameter / 2

    # For the start and end miter planes of one chain, s0(v) and s1(v)
    # depend on the *same* cross-section offset v. Their minimum axial gap
    # is L-r*||Proj_e_perp(n0/(n0.e)-n1/(n1.e))||, not a sum of two separate
    # maximum reaches. Free ends use normal e.
    miter_planes = []
    for index, (direction, length) in enumerate(zip(tangents, chain_lengths)):
        previous = tangents[index - 1] if index else direction
        following = tangents[index + 1] if index + 1 < len(tangents) else direction
        n0 = [previous[i] + direction[i] for i in range(3)]
        n1 = [direction[i] + following[i] for i in range(3)]
        miter_planes.append((n0, n1))
        denominator0 = _dot(n0, direction)
        denominator1 = _dot(n1, direction)
        if denominator0 <= 0 or denominator1 <= 0:
            start_foldback = (index > 0 and denominator0 <= 0
                              and _line_relation(chain_points[index - 1], chain_points[index],
                                                 chain_points[index + 1]) == -1)
            end_foldback = (index + 1 < len(tangents) and denominator1 <= 0
                            and _line_relation(chain_points[index], chain_points[index + 1],
                                               chain_points[index + 2]) == -1)
            if start_foldback or end_foldback:
                return _infeasible("foldback", "An exact 180-degree foldback has no finite miter")
            return _unverified("miter_normal", "Miter plane orientation is numerically unresolved")
        difference = [n0[i] / denominator0 - n1[i] / denominator1 for i in range(3)]
        axial = _dot(difference, direction)
        transverse = [difference[i] - axial * direction[i] for i in range(3)]
        minimum_required = radius * math.sqrt(_dot(transverse, transverse))
        if not math.isfinite(minimum_required):
            return _unverified("numeric_capacity", "Miter gap arithmetic exceeds finite capacity")
        gap = length - minimum_required
        if not math.isfinite(gap):
            return _unverified("numeric_capacity", "Miter gap exceeds finite numeric capacity")
        uncertainty = 16 * _BINARY64_EPSILON * max(length, minimum_required)
        if abs(gap) <= uncertainty:
            return _unverified("miter_boundary", "Miter gap is at numeric resolution")
        if gap < 0:
            return _infeasible("miter_room", "Miter planes cross within chain %d" % index)

    baseline = points[0] if standard else origin
    miter_bounds = _pipe_miter_bounds(chain_points, tangents, miter_planes, radius, baseline)
    for index in range(len(chain_vectors)):
        for other in range(index + 2, len(chain_vectors)):
            separation_sq = _segment_distance_squared(
                chain_points[index], chain_points[index + 1],
                chain_points[other], chain_points[other + 1],
            )
            if not math.isfinite(separation_sq):
                return _unverified("numeric_capacity", "Separation arithmetic exceeds finite capacity")
            if separation_sq == 0:
                return _infeasible("self_intersection", "Nonadjacent centerline segments intersect")
            if not _pipe_separated(miter_bounds[index], miter_bounds[other], radius):
                return _unverified(
                    "body_overlap_unverified",
                    "Nonadjacent miter sections have no reliable separating plane or enclosing-box gap",
                )

    # Keep the exact solved world path; only its rigid pose moves to placement.
    relative_points = [_sub(point, baseline) for point in points]
    if any(not math.isfinite(value) for point in relative_points for value in point):
        return _unverified("numeric_capacity", "World-to-local subtraction exceeds finite capacity")
    def cross(a, b):
        return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]
    def reference(axis):
        preferred = [0.0, 0.0, 1.0]
        projected = [preferred[i] - axis[i]*_dot(axis, preferred) for i in range(3)]
        if math.hypot(*projected) <= 32*math.sqrt(_BINARY64_EPSILON):
            preferred = [1.0, 0.0, 0.0]
            projected = [preferred[i] - axis[i]*_dot(axis, preferred) for i in range(3)]
        length = math.hypot(*projected)
        return [value/length for value in projected]
    x_axis = tangents[0]
    plane_normal = [0.0, 0.0, 0.0]
    for tangent in tangents[1:]:
        candidate = cross(x_axis, tangent)
        if math.hypot(*candidate) > math.hypot(*plane_normal):
            plane_normal = candidate
    if math.hypot(*plane_normal) <= 32*math.sqrt(_BINARY64_EPSILON):
        plane_normal = reference(x_axis)
    placement = frame(relative_points[0], x_axis, plane_normal)
    local_points = [point_in_frame(point, placement) for point in relative_points]
    endpoint_references = [direction_in_frame(reference(axis), placement) for axis in outward_axes]
    if len(points) == 2:
        # An exact two-endpoint straight body has no relative turn to encode.
        local_points = [[0.0, 0.0, 0.0], [lengths[0], 0.0, 0.0]]
        endpoint_references = [[0.0, 0.0, 1.0], [0.0, 0.0, 1.0]]
    return {
        "status": "SOLVED",
        "internal_parameters": {"local_points_m": local_points, "outer_diameter_m": outer_diameter,
                                "endpoint_reference_directions": endpoint_references},
        "components": [{"component_key": "pipe"}],
        "fragments": [{
            "fragment_key": "pipe_body", "binding_key": "pipe_body_geometry",
            "local_transform": placement,
            "uniform_scale": 1.0,
        }],
    }


def realize(ctx):
    return _solve(ctx, ctx.external_constraints(), ctx.param("middle_points_m"), standard=False)


def standard_model(ctx):
    # The accepted nominal example is the 2 m + 3 m right-angle path, OD 0.2 m.
    # Installation endpoints are never read by this independent entry.
    targets = [
        {"interface_key": "inlet", "condition": {"kind": "fixed_point", "point_m": [0, 0, 0],
         "axis": [1, 0, 0], "outer_diameter_m": 0.2}},
        {"interface_key": "outlet", "condition": {"kind": "fixed_point", "point_m": [2, 3, 0],
         "axis": [0, -1, 0], "outer_diameter_m": 0.2}},
    ]
    return _solve(ctx, targets, [[2, 0, 0]], standard=True)
