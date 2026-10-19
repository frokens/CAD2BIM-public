"""Pure endpoint datums for the bounded polyline authoring fixture."""
import math

def unit(value):
    length = math.hypot(*value)
    if length <= 0:
        raise ValueError("path has duplicate points")
    return tuple(x / length for x in value)

def reference(axis):
    candidate = min(((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                    key=lambda item: abs(sum(a * b for a, b in zip(item, axis))))
    dot = sum(a * b for a, b in zip(candidate, axis))
    return unit(tuple(candidate[i] - axis[i] * dot for i in range(3)))

def evaluate(ctx):
    points = ctx.param("points_m")
    first = unit(tuple(points[1][i] - points[0][i] for i in range(3)))
    last = unit(tuple(points[-1][i] - points[-2][i] for i in range(3)))
    for key, origin, axis in (("inlet", points[0], tuple(-x for x in first)),
                              ("outlet", points[-1], last), ("datum", points[0], first)):
        ctx.define_interface(key=key, origin_m=origin, axis=axis,
                             reference_direction=reference(axis),
                             profile=None if key == "datum" else {
                                 "kind": "circular", "diameter_m": ctx.param("outer_diameter_m")})
