from .parameters import prepare

"""Shared exact two-port fitting: circular bend or coaxial reducer."""

import math

import cadquery as cq




def _unit(value):
    size = value.Length
    if not math.isfinite(size) or size <= 0:
        raise ValueError("Two-port geometry needs a finite nonzero axis")
    return value / size


def _line(a, b):
    return cq.Edge.makeLine(a, b)


def _transition(body_d, end_d):
    # Preserve the frozen Family's minimum occupancy, not a C2 claim for the new cone.
    return max(0.1 * body_d, 1.875 * abs(body_d - end_d) / 2)




def _bend_leg(port, toward_bend, radial, end_d, body_d, length):
    """Keep the exposed port and leg length; use one capped ring and cone."""
    radius, body_radius = end_d / 2, body_d / 2
    if length <= 0.3 * end_d + _transition(body_d, end_d):
        raise ValueError("Bend leg does not match the preserved occupancy contract")
    first, last = 0.085 * end_d, 0.185 * end_d
    ring_radius = min(1.18 * radius, body_radius)
    def point(s, r):
        return port + toward_bend * s + radial * r
    profile = [(0.0, radius), (first, radius), (first, ring_radius),
               (last, ring_radius), (last, radius), (length, body_radius),
               (length, 0.86 * body_radius), (last, 0.86 * radius),
               (0.0, 0.86 * radius), (0.0, radius)]
    # Adjacent edges share a profile vertex. Construct its exact coordinates
    # once rather than repeating the same vector arithmetic for both edges.
    points = [point(*coordinates) for coordinates in profile]
    wire = cq.Wire.assembleEdges([_line(a, b)
                                 for a, b in zip(points, points[1:])])
    return cq.Solid.revolve(wire, [], 360,
                            port.toTuple(), (port + toward_bend).toTuple())


def _straight(port_a, port_b, axis, radial, da, db):
    """Direct tube or one-piece linear reducer with fitting design radial wall."""
    length = (port_b - port_a).Length
    stub_a, stub_b = 0.3 * da, 0.3 * db
    minimum_transition = _transition(max(da, db), min(da, db)) if da != db else 0.0
    if length < stub_a + minimum_transition + stub_b - 1e-12:
        raise ValueError("Direct-through body is shorter than the preserved occupancy")
    transition = length - stub_a - stub_b
    wall = 0.07 * min(da, db)  # Design assumption; external pipe wall is unknown.
    ra, rb = da / 2, db / 2

    def point(s, r):
        return port_a + axis * s + radial * r

    if da == db:
        outer = [_line(point(0, ra), point(length, rb))]
        inner = [_line(point(length, rb - wall), point(0, ra - wall))]
    else:
        outer = [
            _line(point(0, ra), point(stub_a, ra)),
            _line(point(stub_a, ra), point(stub_a + transition, rb)),
            _line(point(stub_a + transition, rb), point(length, rb)),
        ]
        inner = [
            _line(point(length, rb - wall),
                  point(stub_a + transition, rb - wall)),
            _line(point(stub_a + transition, rb - wall), point(stub_a, ra - wall)),
            _line(point(stub_a, ra - wall), point(0, ra - wall)),
        ]
    wire = cq.Wire.assembleEdges(
        outer + [_line(point(length, rb), point(length, rb - wall))]
        + inner + [_line(point(0, ra - wall), point(0, ra))]
    )
    return cq.Solid.revolve(wire, [], 360,
                            port_a.toTuple(), (port_a + axis).toTuple())


def _actual_end(planar_faces, expected_center, diameter, bore_radius, scale):
    tolerance = max(1e-9 * scale, 1e-12)
    candidates = [face for face, center in planar_faces
                  if (center - expected_center).Length <= tolerance]
    if len(candidates) != 1:
        raise ValueError("Fitting does not have one actual planar annular end face")
    face = candidates[0]
    radii = sorted(edge.radius() for edge in face.Edges()
                   if edge.geomType() == "CIRCLE")
    if len(radii) != 2:
        raise ValueError("Actual end face is not bounded by two circular edges")
    expected = [bore_radius, diameter / 2]
    if any(abs(got - want) > max(tolerance, 1e-7 * want)
           for got, want in zip(radii, expected)):
        raise ValueError("Actual end face circular radii differ from design")
    area = math.pi * (expected[1] ** 2 - expected[0] ** 2)
    if not math.isclose(face.Area(), area, rel_tol=1e-6, abs_tol=1e-12):
        raise ValueError("Actual end face annulus area differs from design")
    return face


def build(ctx):
    state = prepare(ctx)
    a = cq.Vector(*state['a'])
    if 'arc_center' in state:
        arc_center = cq.Vector(*state['arc_center'])
    if 'axis' in state:
        axis = cq.Vector(*state['axis'])
    b = cq.Vector(*state['b'])
    if 'body_d' in state:
        body_d = state['body_d']
    if 'bore_a' in state:
        bore_a = state['bore_a']
    if 'bore_b' in state:
        bore_b = state['bore_b']
    da = state['da']
    db = state['db']
    if 'delta' in state:
        delta = state['delta']
    if 'flow_a' in state:
        flow_a = cq.Vector(*state['flow_a'])
    if 'length_a' in state:
        length_a = state['length_a']
    if 'length_b' in state:
        length_b = state['length_b']
    mode = state['mode']
    if 'plane_normal' in state:
        plane_normal = cq.Vector(*state['plane_normal'])
    radial = cq.Vector(*state['radial'])
    reference_a = cq.Vector(*state['reference_a'])
    reference_b = cq.Vector(*state['reference_b'])
    if 'tangent_a' in state:
        tangent_a = cq.Vector(*state['tangent_a'])
    ua = cq.Vector(*state['ua'])
    ub = cq.Vector(*state['ub'])
    scale = state['model_scale']
    if mode == 'straight':
        shape = _straight(a, b, axis, radial, da, db)
    elif mode == 'curved':
        leg_a = _bend_leg(a, flow_a, radial, da, body_d, length_a)
        leg_b = _bend_leg(b, ub * -1, radial, db, body_d, length_b)
        outer = cq.Wire.makeCircle(body_d / 2, tangent_a, flow_a)
        inner = cq.Wire.makeCircle(0.86 * body_d / 2, tangent_a, flow_a)
        bend = cq.Solid.revolve(outer, [inner], math.degrees(delta), arc_center.toTuple(), (arc_center + plane_normal).toTuple())
        # The preserved bend construction joins the legs at tangent annular
        # faces. Reuse their coincident boundaries instead of intersecting them.
        shape = bend.fuse(leg_a, leg_b, glue=True).clean()
    else:
        raise ValueError('Unknown two-port shape mode')
    if not shape.isValid() or len(shape.Solids()) != 1:
        raise ValueError('Two-port fitting did not produce one valid solid')
    # Both port checks inspect the same final shape. Enumerate planar faces
    # and measure each center once; retain each port's own radius/area checks.
    planar_faces = [(face, face.Center()) for face in shape.Faces()
                    if face.geomType() == "PLANE"]
    for key, expected, outward, reference, diameter, bore in (('side_a', a, ua, reference_a, da, bore_a), ('side_b', b, ub, reference_b, db, bore_b)):
        face = _actual_end(planar_faces, expected, diameter, bore, scale)
        pass
    return shape
