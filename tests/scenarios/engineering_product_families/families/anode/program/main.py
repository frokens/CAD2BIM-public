"""Place the fixed five-part anode from one finite host segment."""

import math


PARTS = ("cast_body", "core", "exit_seal", "lead_jacket", "lead_conductor")
OFFSETS = {
    "cast_body": (0.0, 0.0, 0.0),
    "core": (-0.243, 0.0, 0.0),
    "exit_seal": (0.225, 0.0, 0.0),
    "lead_jacket": (0.253, 0.0, 0.0),
    "lead_conductor": (0.253, 0.0, 0.0),
}
TIP = (0.337, 0.0, -0.105)


def _add(a, b):
    return [a[i] + b[i] for i in range(3)]


def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def _mul(a, value):
    return [x * value for x in a]


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _norm(a):
    return math.hypot(*a)


def _rotate(rotation, point):
    return [sum(rotation[i][j] * point[j] for j in range(3)) for i in range(3)]


def _fail(key, message, uncertain=False):
    field = "ambiguities" if uncertain else "violations"
    return {"status": "AMBIGUOUS" if uncertain else "INFEASIBLE",
            field: [{("ambiguity_key" if uncertain else "violation_key"): key,
                     "message": message}]}


def _solve(ctx, standard):
    a = ctx.param("host_a_m")
    b = ctx.param("host_b_m")
    od = ctx.param("host_outer_diameter_m")
    station = ctx.param("attachment_station_m")
    if (len(a) != 3 or len(b) != 3
            or not all(math.isfinite(x) for x in (*a, *b, od, station))
            or od <= 0):
        return _fail("source_values", "Host A/B, positive OD and station must be finite")
    axis = _sub(b, a)
    length = _norm(axis)
    if length == 0:
        return _fail("host_axis", "Host A and B must be distinct")
    if not math.isfinite(length):
        return _fail("numeric_capacity", "Host direction exceeds finite arithmetic", True)
    if station < 0 or station > length:
        return _fail("finite_station", "Attachment station must lie on the finite host segment")
    u = _mul(axis, 1 / length)
    if not all(math.isfinite(x) for x in u):
        return _fail("numeric_capacity", "Host direction cannot be resolved", True)
    projected = _sub((0.0, 0.0, 1.0), _mul(u, u[2]))
    if _norm(projected) <= 1e-8:
        projected = _sub((1.0, 0.0, 0.0), _mul(u, u[0]))
    radial_length = _norm(projected)
    if not math.isfinite(radial_length) or radial_length <= 1e-8:
        return _fail("numeric_capacity", "Host radial frame cannot be resolved", True)
    n = _mul(projected, 1 / radial_length)
    y = _cross(n, u)
    rotation = [[u[i], y[i], n[i]] for i in range(3)]
    q = _add(_add(a, _mul(u, station)), _mul(n, od / 2))
    world_translation = _sub(q, _rotate(rotation, TIP))
    origin = (0.0, 0.0, 0.0) if standard else ctx.product_origin()
    if len(origin) != 3 or not all(math.isfinite(x) for x in origin):
        return _fail("origin", "Product origin must be finite")
    local_translation = _sub(world_translation, origin)
    if not all(math.isfinite(x) for x in (*q, *local_translation, *y)):
        return _fail("numeric_capacity", "Anode placement exceeds finite arithmetic", True)
    fragments = []
    for part in PARTS:
        part_translation = _add(local_translation, _rotate(rotation, OFFSETS[part]))
        fragments.append({"fragment_key": part + "_shape", "binding_key": part,
                          "uniform_scale": 1.0,
                          "local_transform": {"translation_m": part_translation,
                                              "rotation": rotation}})
    return {"status": "SOLVED",
            "internal_parameters": {"attachment_point_m": q,
                                    "selected_station_m": station,
                                    "host_axis": u, "outward_radial": n},
            "components": [{"component_key": part} for part in PARTS],
            "fragments": fragments}


def realize(ctx):
    return _solve(ctx, False)


def standard_model(ctx):
    return _solve(ctx, True)
