from .parameters import prepare, profile_values

"""One analytic hollow CROSS with four independent annular end interfaces."""

import math

import cadquery as cq




def unit(v):
    if not math.isfinite(v.Length) or v.Length <= 0:
        raise ValueError("CROSS needs finite nonzero directions")
    return v.normalized()






def orient(shape, axis):
    x = cq.Vector(1, 0, 0)
    rotation = x.cross(axis)
    if rotation.Length < 1e-12:
        return shape if axis.x > 0 else shape.rotate((0, 0, 0), (0, 0, 1), 180)
    return shape.rotate((0, 0, 0), rotation.normalized().toTuple(),
                        math.degrees(math.atan2(rotation.Length, x.dot(axis))))


def neck(chamber_radius, port_radius, seam, endpoint):
    s, x, last, slope = profile_values(chamber_radius, port_radius, seam)
    p = lambda axial, radial: cq.Vector(axial, radial, 0)
    d = last-x
    edges = [
        cq.Edge.makeLine(p(0, 0), p(0, s)),
        cq.Edge.makeLine(p(0, s), p(x, s)),
        cq.Edge.makeBezier([p(x, s), p(x+d/3, s+slope*d/3),
                            p(x+2*d/3, port_radius), p(last, port_radius)]),
        cq.Edge.makeLine(p(last, port_radius), p(endpoint, port_radius)),
        cq.Edge.makeLine(p(endpoint, port_radius), p(endpoint, 0)),
        cq.Edge.makeLine(p(endpoint, 0), p(0, 0)),
    ]
    return cq.Solid.revolve(cq.Wire.assembleEdges(edges), [], 360, (0, 0, 0), (1, 0, 0))


def envelope(radius, side_data, seam, *, inner=False, wall=0):
    r = radius-wall if inner else radius
    shape = cq.Solid.makeSphere(r, angleDegrees1=-90, angleDegrees2=90)
    for side in side_data:
        port_radius = side["od"]/2-(side["wall"] if inner else 0)
        # A source port can end before the full outer sphere reaches its
        # axial pole. The inner cutter must traverse that whole sphere lobe;
        # q > transition end and wall < seam keep the lobe inside this bore.
        end = (max(side["q"], radius)+max(1e-5*side["od"], 1e-8)
               if inner else side["q"])
        shape = shape.fuse(orient(neck(r, port_radius, seam, end), side["axis"]))
    return shape


def actual_end(shape, point, outside, inside):
    tolerance = max(1e-7*outside, 1e-10)
    matches = [face for face in shape.Faces() if face.geomType() == "PLANE"
               and (face.Center()-point).Length <= tolerance]
    if len(matches) != 1:
        raise ValueError(f"CROSS lacks one actual annular end at {point.toTuple()}: "
                         f"found {len(matches)}")
    face = matches[0]
    radii = sorted(edge.radius() for edge in face.Edges()
                   if edge.geomType() == "CIRCLE")
    if len(face.Wires()) != 2 or len(radii) != 2 or any(
            abs(got-want) > max(tolerance, 1e-6*want)
            for got, want in zip(radii, (inside, outside))):
        raise ValueError(f"CROSS end radii differ: {radii} != {(inside, outside)}")
    return face


def build(ctx):
    state = prepare(ctx)
    center = cq.Vector(*state['center'])
    radius = state['radius']
    seam = state['seam']
    side_data = [{**side,"point":cq.Vector(*side["point"]),"axis":cq.Vector(*side["axis"]),"ref":cq.Vector(*side["ref"])} for side in state['side_data']]
    wall = state['wall']
    outer = envelope(radius, side_data, seam)
    bore = envelope(radius, side_data, seam, inner=True, wall=wall)
    shape = outer.cut(bore).translate(center)
    if not shape.isValid() or len(shape.Solids()) != 1 or len(shape.Shells()) != 1:
        raise ValueError("CROSS must be one valid, closed material solid")
    if not bore.isValid() or len(bore.Solids()) != 1:
        raise ValueError("CROSS bore is not a single connected passage")
    # The saved BREP is independently classified at the center and along
    # every bore; the controlled modeling surface has no point classifier.
    for side in side_data:
        face = actual_end(shape, side["point"], side["od"]/2,
                          side["od"]/2-side["wall"])
    return shape
