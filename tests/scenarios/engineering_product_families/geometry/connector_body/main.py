from .parameters import prepare

"""One CONNECTOR modeling function: selected S sleeve or repaired full curved fitting."""

import math

import cadquery as cq



def _unit(vector):
    if not math.isfinite(vector.Length) or vector.Length <= 0:
        raise ValueError("CONNECTOR needs finite nonzero axes")
    return vector.normalized()


def _line(a, b):
    return cq.Edge.makeLine(a, b)




def _s_curve(workplane, x, radius, span, delta):
    """Selected target's two tangent circular arcs, not a C2 spline."""
    if abs(delta) < 1e-10:
        return workplane.lineTo(x + span, radius)
    if span <= abs(delta):
        raise ValueError("PROFILE_FOLDBACK: S biarc span must exceed radial rise")
    sign = 1 if delta > 0 else -1
    arc_radius = (span * span + delta * delta) / (4 * abs(delta))
    angle = 2 * math.atan(abs(delta) / span)
    qx = arc_radius * math.sin(angle / 2)
    qy = sign * arc_radius * (1 - math.cos(angle / 2))
    return (workplane
            .threePointArc((x + qx, radius + qy),
                           (x + span / 2, radius + delta / 2))
            .threePointArc((x + span - qx, radius + delta - qy),
                           (x + span, radius + delta)))


def _s_radius(dx, radius, span, delta):
    if abs(delta) < 1e-10:
        return radius
    sign = 1 if delta > 0 else -1
    arc_radius = (span * span + delta * delta) / (4 * abs(delta))
    if dx <= span / 2:
        return radius + sign * (arc_radius - math.sqrt(max(0, arc_radius * arc_radius - dx * dx)))
    return radius + delta - sign * (arc_radius - math.sqrt(max(0, arc_radius * arc_radius - (span - dx) ** 2)))


def _ascending_sleeve(length, da, db):
    if da > db:
        raise ValueError("Internal sleeve construction requires ascending OD")
    land, shoulder = 0.08 * length, 0.16 * length
    start = -length / 2 + land + shoulder
    finish = length / 2 - land - shoulder
    span = finish - start
    addition = 0.10 * db
    if shoulder <= addition or span <= (db - da) / 2:
        raise ValueError("Sleeve length violates actual S contour occupancy")
    ra, rb = da / 2, db / 2
    outer = cq.Workplane("XY").moveTo(-length / 2, 0).lineTo(-length / 2, ra)
    outer = outer.lineTo(-length / 2 + land, ra)
    outer = _s_curve(outer, -length / 2 + land, ra, shoulder, addition)
    outer = _s_curve(outer, start, ra + addition, span, rb - ra)
    outer = _s_curve(outer, finish, rb + addition, shoulder, -addition)
    outer = outer.lineTo(length / 2, rb).lineTo(length / 2, 0).close()
    body = outer.revolve(360, (0, 0), (1, 0)).val()
    ria, rib = .86 * ra, .86 * rb
    inner = cq.Workplane("XY").moveTo(-length / 2 - .001, 0).lineTo(-length / 2 - .001, ria)
    inner = inner.lineTo(start, ria)
    inner = _s_curve(inner, start, ria, span, rib - ria)
    inner = inner.lineTo(length / 2 + .001, rib).lineTo(length / 2 + .001, 0).close()
    body = body.cut(inner.revolve(360, (0, 0), (1, 0)).val())
    tool_radius, depth = .010 * db, .006 * db
    for fraction in (.12, .88):
        x = start + fraction * span
        radius = _s_radius(x - start, ra + addition, span, rb - ra)
        cutter = cq.Solid.makeTorus(radius + tool_radius - depth, tool_radius,
                                    cq.Vector(x, 0, 0), cq.Vector(1, 0, 0))
        body = body.cut(cutter)
    return body.clean()


def _orient_sleeve(shape, start, end, descending):
    if descending:
        # The selected S target avoids invalid descending booleans by making
        # the ascending profile first and rotating it rigidly by 180 degrees.
        shape = shape.rotate((0, 0, 0), (0, 0, 1), 180)
    axis = _unit(end - start)
    x_axis = cq.Vector(1, 0, 0)
    rotation_axis = x_axis.cross(axis)
    if rotation_axis.Length < 1e-12:
        if x_axis.dot(axis) < 0:
            shape = shape.rotate((0, 0, 0), (0, 0, 1), 180)
    else:
        shape = shape.rotate((0, 0, 0), rotation_axis.toTuple(),
                             math.degrees(math.atan2(rotation_axis.Length, x_axis.dot(axis))))
    return shape.translate(((start + end) * .5).toTuple())


def _curved_leg(port, inward, radial, end_d, core_d, length):
    """One hollow leg carrying the baseline shoulder and a full annular collar."""
    r_port, r_old, r_core = end_d / 2, 1.1 * end_d / 2, core_d / 2
    thickness = (1.1 * end_d + core_d) * .05
    collar_end = 1.1 * thickness
    collar_start = .1 * thickness
    if 1.1 * end_d > core_d:
        collar_radius = (1.1 * end_d / 2) * 1.1
    else:
        collar_radius = (1.1 * end_d + core_d / 5) * .525
    if length <= collar_end + 1e-6 * core_d:
        raise ValueError("Curved leg has no room for its selected annular collar")
    if min(r_port, r_old, r_core) <= 1e-6:
        raise ValueError("Curved leg falls below native BREP resolution")

    def point(s, r):
        return port + inward * s + radial * r

    # The source terminal was a solid 1.1D circle. Repair only its exterior
    # terminal margin to actual D; retain collar radii and axial placement.
    profile = [
        (0.0, r_port), (collar_start, r_old),
        (collar_start, collar_radius), (collar_end, collar_radius),
        (collar_end, r_old), (length, r_core),
        (length, .86 * r_core), (collar_end, .86 * r_old),
        (collar_start, .86 * r_old), (0.0, .86 * r_port),
        (0.0, r_port),
    ]
    wire = cq.Wire.assembleEdges([_line(point(*left), point(*right))
                                  for left, right in zip(profile, profile[1:])])
    return cq.Solid.revolve(wire, [], 360, port.toTuple(), (port + inward).toTuple())


def _curved(a, b, center, ua, ub, radial, da, db, core_d, delta, bend_scale, radius):
    fa, fb = 1.1 * da, 1.1 * db
    if not math.isclose(core_d, max(fa, fb), rel_tol=1e-9):
        raise ValueError("Curved core diameter differs from baseline numerical relation")
    ea = min(core_d * (core_d - fa) / (fa + fb) + .5 * core_d, 2 * core_d)
    eb = min(core_d * (core_d - fb) / (fa + fb) + .5 * core_d, 2 * core_d)
    if (a - center - ua * (bend_scale + ea)).Length > 1e-8 * max(1, core_d):
        raise ValueError("Side A takeoff differs from solved B+e")
    if (b - center - ub * (bend_scale + eb)).Length > 1e-8 * max(1, core_d):
        raise ValueError("Side B takeoff differs from solved B+e")
    flow_a = ua * -1
    plane_normal = _unit(flow_a.cross(ub))
    turn_radial = _unit(plane_normal.cross(flow_a))
    if abs(flow_a.dot(ub) - math.cos(delta)) > 1e-7:
        raise ValueError("Source axes do not match solved true turn")
    tangent_a = center + ua * (.9 * bend_scale)
    tangent_b = center + ub * (.9 * bend_scale)
    leg_a = _curved_leg(a, flow_a, radial, da, core_d, (tangent_a - a).Length)
    leg_b = _curved_leg(b, ub * -1, radial, db, core_d, (tangent_b - b).Length)
    arc_center = tangent_a + turn_radial * radius
    arc_end = (tangent_a + flow_a * (radius * math.sin(delta))
               + turn_radial * (radius * (1 - math.cos(delta))))
    if (arc_end - tangent_b).Length > 1e-8 * max(1, core_d):
        raise ValueError("Circular arc does not close at second tangent")
    outer = cq.Wire.makeCircle(core_d / 2, tangent_a, flow_a)
    inner = cq.Wire.makeCircle(.86 * core_d / 2, tangent_a, flow_a)
    arc = cq.Solid.revolve(outer, [inner], math.degrees(delta),
                           arc_center.toTuple(), (arc_center + plane_normal).toTuple())
    # The revolved core and both legs meet at the solved tangent annuli.
    return arc.fuse(leg_a, leg_b, glue=True).clean()


def _actual_end(shape, center, diameter, bore):
    tolerance = max(1e-9 * diameter, 1e-12)
    candidates = [face for face in shape.Faces()
                  if face.geomType() == "PLANE" and (face.Center() - center).Length <= tolerance]
    if len(candidates) != 1:
        raise ValueError("CONNECTOR lacks one actual planar annular end face")
    face = candidates[0]
    radii = sorted(edge.radius() for edge in face.Edges() if edge.geomType() == "CIRCLE")
    expected = (bore, diameter / 2)
    if len(radii) != 2 or any(abs(got - want) > max(tolerance, 1e-7 * want)
                              for got, want in zip(radii, expected)):
        raise ValueError("CONNECTOR actual end radii differ from solved interface")
    return face


def build(ctx):
    state = prepare(ctx)
    a = cq.Vector(*state['a'])
    b = cq.Vector(*state['b'])
    if 'bend_scale' in state:
        bend_scale = state['bend_scale']
    if 'bore_a' in state:
        bore_a = state['bore_a']
    if 'bore_b' in state:
        bore_b = state['bore_b']
    if 'center' in state:
        center = cq.Vector(*state['center'])
    core_d = state['core_d']
    da = state['da']
    db = state['db']
    if 'delta' in state:
        delta = state['delta']
    if 'descending' in state:
        descending = state['descending']
    if 'length' in state:
        length = state['length']
    mode = state['mode']
    radial = cq.Vector(*state['radial'])
    if 'radius' in state:
        radius = state['radius']
    ref_a = cq.Vector(*state['ref_a'])
    ref_b = cq.Vector(*state['ref_b'])
    ua = cq.Vector(*state['ua'])
    ub = cq.Vector(*state['ub'])
    if mode == 'straight':
        shape = _ascending_sleeve(length, min(da, db), max(da, db))
        shape = _orient_sleeve(shape, a, b, descending)
    elif mode == 'curved':
        shape = _curved(a, b, center, ua, ub, radial, da, db, core_d, delta, bend_scale, radius)
    else:
        raise ValueError('Unknown CONNECTOR branch')
    if not shape.isValid() or len(shape.Solids()) != 1 or len(shape.Shells()) != 1:
        raise ValueError('CONNECTOR did not produce one valid closed solid')
    for key, point, outward, reference, diameter, bore in (('side_a', a, ua, ref_a, da, bore_a), ('side_b', b, ub, ref_b, db, bore_b)):
        face = _actual_end(shape, point, diameter, bore)
        pass
    return shape
