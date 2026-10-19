"""Finite-source, pure-numeric SERVICE_SADDLE proposal."""

import math


EPS = 2.220446049250313e-16
POSITION_TOL = 1e-6


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _sub(a, b):
    return [x - y for x, y in zip(a, b)]


def _add(a, b):
    return [x + y for x, y in zip(a, b)]


def _mul(a, factor):
    return [x * factor for x in a]


def _norm(a):
    return math.hypot(*a)


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _world_reference(axis):
    # Match the project's world-Z, then world-X, full-frame convention.
    for candidate in ([0.0, 0.0, 1.0], [1.0, 0.0, 0.0]):
        projected = _sub(candidate, _mul(axis, _dot(candidate, axis)))
        length = _norm(projected)
        if length >= 32 * math.sqrt(EPS):
            return _mul(projected, 1 / length)
    return None


def _round_frame_components(values):
    # Only close the IEEE rounding residue of this Family's unit-frame output.
    if any(not math.isfinite(value) or abs(value) > 1 + 64 * EPS for value in values):
        return None
    return [(1.0 if value > 0 else -1.0) if abs(value) > 1 else value for value in values]


def _fail(key, message, ambiguous=False):
    if ambiguous:
        return {"status": "AMBIGUOUS", "ambiguities": [{"ambiguity_key": key, "message": message}]}
    return {"status": "INFEASIBLE", "violations": [{"violation_key": key, "message": message}]}


def _source(key, target):
    if key == "host":
        a, b, diameter = target["a_m"], target["b_m"], target["outer_diameter_m"]
    else:
        if target is None or target["kind"] != "finite_segment" or target["direction_relation"] != "same":
            return _fail("source_condition", "GAS branch needs a finite_segment with same direction")
        a, b, diameter = target["start_m"], target["end_m"], target["outer_diameter_m"]
    if len(a) != 3 or len(b) != 3 or not all(math.isfinite(x) for x in a + b):
        return _fail("source_coordinates", key + " needs finite 3D endpoints")
    if not math.isfinite(diameter) or diameter <= 0:
        return _fail("source_od", key + " needs positive finite OD")
    vector = _sub(b, a)
    length = _norm(vector)
    if not math.isfinite(length) or length <= 0:
        return _fail("source_length", key + " has no positive finite length")
    direction = _mul(vector, 1 / length)
    return {"a": a, "b": b, "u": direction, "length": length, "od": diameter}


def _detailed_layout(host, branch, available, branch_station, dx, q):
    dh, db = host["od"], branch["od"]
    extent = (.58 * dh * abs(dx) + .72 * db) / q
    port_station = (dh / 2 + 2 * db) / q
    if not all(math.isfinite(x) and x > 0 for x in (extent, port_station)):
        return _fail("numeric_capacity", "Saddle extent or port station is not representable", True)
    if extent > available:
        return _fail("host_window", "Actual junction envelope exceeds the finite host half-window")
    margin = 64 * EPS * max(dh, db, extent, available)
    if extent + margin > available:
        return _fail("joint_resolution", "Host fit exists below the numerical seam margin", True)
    half_length = min(available, max(.75 * dh, extent + margin))
    port_on_source = branch_station + port_station
    if not math.isfinite(port_on_source):
        return _fail("numeric_capacity", "Branch port station exceeds finite arithmetic", True)
    if port_on_source < 0 or port_on_source > branch["length"]:
        return _fail("branch_window", "Branch source does not contain the outward port station")
    if min(port_on_source, branch["length"] - port_on_source) <= margin:
        return _fail("joint_resolution", "Branch port is at numeric resolution of a finite end", True)
    if min(dh, db, half_length) <= 1e-6:
        return _fail("native_resolution", "Selected contour is below OCCT metre-scale resolution", True)
    return {"mode": "detailed", "collar_half_length_m": half_length,
            "branch_port_station_m": port_station}


def _solve(ctx, host_target, target, standard):
    host, branch = _source("host", host_target), _source("branch", target)
    for source in (host, branch):
        if "status" in source:
            return source
    origin = ctx.product_origin()
    if len(origin) != 3 or not all(math.isfinite(x) for x in origin):
        return _fail("origin", "Product origin must be finite")
    scale = max(1.0, *(abs(x) for source in (host, branch) for point in (source["a"], source["b"]) for x in point))
    if 128 * EPS * scale > POSITION_TOL / 8:
        return _fail("numeric_capacity", "World coordinates cannot resolve the intersection tolerance", True)

    u, d = host["u"], branch["u"]
    dx = _dot(u, d)
    transverse = _sub(d, _mul(u, dx))
    q = _norm(transverse)
    if q == 0:
        return _fail("parallel_axis", "Branch has no radial direction relative to host")
    if not math.isfinite(q) or q <= 64 * EPS:
        return _fail("numeric_capacity", "Radial direction cannot be represented reliably", True)
    radial = _mul(transverse, 1 / q)
    side = _cross(radial, u)
    if abs(_norm(side) - 1) > 1e-8:
        return _fail("numeric_capacity", "Host frame cannot be resolved", True)

    # Intersect actual 3D source axes. A midpoint of skew closest points is not a junction.
    w = _sub(branch["a"], host["a"])
    normal = _cross(u, d)
    q2 = _dot(normal, normal)
    host_station = _dot(_cross(w, d), normal) / q2
    center = _add(host["a"], _mul(u, host_station))
    branch_station = _dot(_sub(center, branch["a"]), d)
    branch_foot = _add(branch["a"], _mul(d, branch_station))
    if not all(math.isfinite(x) for x in center) or not math.isfinite(branch_station):
        return _fail("numeric_capacity", "Source intersection exceeds finite arithmetic", True)
    if _norm(_sub(center, branch_foot)) > POSITION_TOL:
        return _fail("axis_intersection", "Host and branch axes do not meet in 3D")
    host_left = host_station
    host_right = host["length"] - host_station
    if min(host_left, host_right) <= 0:
        return _fail("host_window", "Host junction is not inside its finite segment")

    dh, db = host["od"], branch["od"]
    available = min(host_left, host_right)
    shape = _detailed_layout(host, branch, available, branch_station, dx, q)
    reason = "none"
    if "status" in shape:
        if shape["status"] != "INFEASIBLE":
            return shape
        reason = shape["violations"][0]["violation_key"]
        if reason not in ("host_window", "branch_window"):
            return shape
        # Legal source connection was established above. Only a detailed
        # finite-layout failure selects this nominal expression, before build.
        shape = None

    world_ref = _world_reference(d)
    if world_ref is None:
        return _fail("numeric_capacity", "GAS full-frame reference cannot be resolved", True)
    local_ref = _round_frame_components([_dot(world_ref, axis) for axis in (u, side, radial)])
    local_direction = _round_frame_components([dx, 0.0, q])
    if local_ref is None or local_direction is None:
        return _fail("numeric_capacity", "Local branch frame exceeds unit component precision", True)
    rotation = [[u[0], side[0], radial[0]],
                [u[1], side[1], radial[1]],
                [u[2], side[2], radial[2]]]
    if shape is None:
        # A translation-only node preserves the original endpoint without a
        # rotate/inverse-rotate round trip at the strict finite-window boundary.
        rotation = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        local_direction, local_ref = d, world_ref
    anchor = center if standard else _sub(center, origin)
    internal = {"local_center_m": anchor,
                "branch_outer_diameter_m": db,
                "construction": "node" if shape is None else "detailed", "default_reason": reason,
                "radial_fraction": q, "host_available_halfwidth_m": available}
    if shape is None:
        params = {"diameter_m": max(dh, db), "interfaces": [
            {"kind": "datum", "key": "host_datum", "origin_m": [0.0, 0.0, 0.0],
             "axis": radial, "reference_direction": u},
            {"kind": "circular", "key": "gas_branch", "origin_m": _sub(branch["a"], center),
             "axis": d, "reference_direction": world_ref, "diameter_m": db}]}
        internal.update(sphere_diameter_m=params["diameter_m"], sphere_interfaces=params["interfaces"])
    else:
        params = {"host_outer_diameter_m": dh, "branch_outer_diameter_m": db, "shape": shape,
                  "branch_local_direction": local_direction, "branch_local_reference_axis": local_ref}
        internal.update(shape=shape, branch_local_direction=local_direction, branch_local_reference_axis=local_ref)
    return {"status": "SOLVED", "internal_parameters": internal,
            "components": [{"component_key": "saddle"}],
            "fragments": [{"fragment_key": "saddle_body",
                           "binding_key": "sphere_geometry" if shape is None else "service_saddle_geometry",
                           "uniform_scale": 1.0,
                           "local_transform": {"translation_m": anchor, "rotation": rotation}}]}


def realize(ctx):
    targets = {item["interface_key"]: item["condition"] for item in ctx.external_constraints()}
    host = {"a_m": ctx.param("host_a_m"), "b_m": ctx.param("host_b_m"),
            "outer_diameter_m": ctx.param("host_outer_diameter_m")}
    return _solve(ctx, host, targets.get("gas_branch"), False)


def standard_model(ctx):
    target = {"kind": "finite_segment", "start_m": [0, 0, 0], "end_m": [0, 0, 2],
              "outer_diameter_m": .063, "direction_relation": "same"}
    host = {"a_m": ctx.param("host_a_m"), "b_m": ctx.param("host_b_m"),
            "outer_diameter_m": ctx.param("host_outer_diameter_m")}
    return _solve(ctx, host, target, True)
