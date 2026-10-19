from .parameters import prepare

"""Parametric OCCT saddle collar and true oblique branch; local host is X."""

import cadquery as cq


def _cylinder(radius, length, point, axis):
    return cq.Solid.makeCylinder(radius, length, cq.Vector(*point), cq.Vector(*axis))


def _face(shape, point, outer_radius, bore_radius):
    tolerance = max(1e-8, outer_radius * 1e-6)
    candidates = [face for face in shape.Faces()
                  if face.geomType() == "PLANE"
                  and (face.Center() - point).Length < tolerance]
    for face in candidates:
        radii = sorted(edge.radius() for edge in face.Edges() if edge.geomType() == "CIRCLE")
        if len(radii) == 2 and abs(radii[0] - bore_radius) < tolerance and abs(radii[1] - outer_radius) < tolerance:
            return face
    raise ValueError("SERVICE_SADDLE lacks its actual annular branch end")


def build(ctx):
    state = prepare(ctx)
    db = state['db']
    dh = state['dh']
    direction = state['direction']
    half = state['half']
    port = state['port']
    q = state['q']
    # The selected source defines its collar/boss/neck/flange dimensions in mm.
    # Build in that native scale and return a metre BREP. No host pipe is made.
    dh *= 1000
    db *= 1000
    half *= 1000
    port *= 1000
    radius = dh / 2
    outer = .58 * dh
    bore = .43 * db
    axis = cq.Vector(*direction)
    # Form the exterior before cutting the host cavity once. The fusion need
    # not process an inner collar boundary that the following cut recreates.
    collar = _cylinder(outer, 2 * half, (-half, 0, 0), (1, 0, 0))

    boss_start = (radius - .25 * db) / q
    neck_start = port - .7 * db / q
    flange_thickness = (1.1 * db + 1.44 * db) * .05
    flange_start = port - 1.1 * flange_thickness / q
    if min(neck_start - boss_start, flange_start - neck_start,
           port - flange_start) <= 0:
        raise ValueError("SERVICE_SADDLE oblique contour stations fold back")

    def at(station):
        return axis * station

    # Approximate the reference exterior by a cylindrical root and neck. This
    # removes conical intersections at the host while preserving the collar,
    # overall station, flange envelope, annular port and true oblique axis.
    flange_radius = ((1.1 * db + 1.44 * db / 5) / 2) * 1.05
    flange_end = flange_start + flange_thickness / q
    # Appearance-only: retain the visible annular port with a blind recess.
    # This does not represent a communicating branch-to-host gas passage.
    cavity_start = port - .5 * db / q
    if cavity_start <= boss_start:
        raise ValueError("SERVICE_SADDLE visible port recess exceeds its branch")
    contour = [(0, boss_start), (.65 * db, boss_start),
               (.65 * db, neck_start), (.5 * db, neck_start),
               (.5 * db, flange_start), (flange_radius, flange_start),
               (flange_radius, flange_end), (.5 * db, flange_end),
               (.5 * db, port), (bore, port),
               (bore, cavity_start), (0, cavity_start)]
    radial_axis = cq.Plane((0, 0, 0), normal=axis).xDir
    branch = cq.Solid.revolve(cq.Wire.makePolygon(
        [at(station) + radial_axis * radial for radial, station in contour],
        close=True), [], 360, (0, 0, 0), axis)
    # Keep the exact cylindrical host void. The open branch recess is part of
    # the revolution; no branch-through-bore Boolean is required.
    host_tool_length = max(4 * dh, 2 * half + 2 * db)
    host_tool = _cylinder(radius, host_tool_length,
                          (-host_tool_length / 2, 0, 0), (1, 0, 0))
    saddle = collar.fuse(branch).cut(host_tool).clean()
    if not saddle.isValid() or len(saddle.Solids()) != 1 or len(saddle.Shells()) != 1:
        raise ValueError("SERVICE_SADDLE did not produce one valid closed solid")
    saddle = saddle.scale(.001)
    _face(saddle, cq.Vector(*[x * port / 1000 for x in direction]), db / 2000, bore / 1000)
    return saddle
