"""Ordinary METRE OCCT modeling function: a bounded polyline with miter joints."""

import math

import cadquery as cq


def _segment_distance(a, b, c, d):
    """Minimum finite-segment distance, including parallel and endpoint cases."""
    u, v, w = b - a, d - c, a - c
    aa, bb, cc, dd, ee = u.dot(u), u.dot(v), v.dot(v), u.dot(w), v.dot(w)
    denominator = aa * cc - bb * bb
    candidates = []
    for s in (0.0, 1.0):
        t = max(0.0, min(1.0, (ee + s * bb) / cc))
        candidates.append((w + u * s - v * t).Length)
    for t in (0.0, 1.0):
        s = max(0.0, min(1.0, (t * bb - dd) / aa))
        candidates.append((w + u * s - v * t).Length)
    if denominator > 1e-14 * aa * cc:
        s, t = (bb * ee - cc * dd) / denominator, (aa * ee - bb * dd) / denominator
        if 0 <= s <= 1 and 0 <= t <= 1:
            candidates.append((w + u * s - v * t).Length)
    return min(candidates)


def _reference(axis):
    candidate = min((cq.Vector(1, 0, 0), cq.Vector(0, 1, 0), cq.Vector(0, 0, 1)),
                    key=lambda item: abs(item.dot(axis)))
    return (candidate - axis * candidate.dot(axis)).normalized()


def build(ctx):
    points = [cq.Vector(*point) for point in ctx.param("points_m")]
    od = ctx.param("outer_diameter_m")
    section = ctx.param("section")
    thickness = section["wall_thickness_m"]
    mode = section["mode"]
    if mode == "solid_envelope":
        if thickness != 0:
            raise ValueError("solid_envelope requires zero wall_thickness_m; inner bore is unknown")
        inner = 0.0
    elif mode == "wall_thickness":
        if not 0 < thickness < od / 2:
            raise ValueError("wall_thickness must be positive and below outer radius")
        inner = od - 2 * thickness
    else:
        raise ValueError("unsupported section mode")
    vectors = [b - a for a, b in zip(points, points[1:])]
    lengths = [v.Length for v in vectors]
    if any(length < 0.001 for length in lengths):
        raise ValueError("path has duplicate points or a segment below 1 mm")
    if sum(lengths) > 1000:
        raise ValueError("path total length exceeds 1000 m")
    directions = [vector.normalized() for vector in vectors]
    corner_reaches = [0.0]
    for left, right in zip(directions, directions[1:]):
        angle = math.acos(max(-1.0, min(1.0, left.dot(right))))
        if angle > math.pi * 2 / 3:
            raise ValueError("miter turn exceeds 120 degrees")
        corner_reaches.append(od / 2 * math.tan(angle / 2))
    corner_reaches.append(0.0)
    for index, length in enumerate(lengths):
        if length <= corner_reaches[index] + corner_reaches[index + 1] + 0.001:
            raise ValueError("segment is too short for its mitered end sections")
    for index in range(len(vectors)):
        for other in range(index + 2, len(vectors)):
            if _segment_distance(points[index], points[index + 1], points[other], points[other + 1]) <= od + 0.001:
                raise ValueError("nonadjacent path segments overlap or have insufficient separation")
    path = cq.Wire.makePolygon(points, close=False)
    plane = cq.Plane(origin=points[0], xDir=_reference(directions[0]), normal=directions[0])
    profile = cq.Workplane(plane).circle(od / 2)
    if inner:
        profile = profile.circle(inner / 2)
    shape = profile.sweep(path, transition="right", isFrenet=False).val()
    if not shape.isValid() or len(shape.Solids()) != 1:
        raise ValueError("mitered path did not produce one valid OCCT solid")
    return shape
