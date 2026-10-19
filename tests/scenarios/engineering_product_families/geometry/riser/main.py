from .parameters import prepare

"""One straight RISER body; named endpoints are evaluated independently."""

import math

import cadquery as cq




def _end_face(shape, expected_center, expected_area, height, diameter, expected_wires):
    planar = [face for face in shape.Faces() if face.geomType() == "PLANE"]
    tolerance = max(1e-12, 1e-9 * max(height, diameter))
    matches = [face for face in planar if (face.Center() - expected_center).Length <= tolerance]
    if len(matches) != 1:
        raise ValueError("RISER body does not have one planar face at each end")
    face = matches[0]
    if not math.isclose(face.Area(), expected_area, rel_tol=1e-7, abs_tol=0):
        raise ValueError("RISER actual end face area does not match its section")
    if len(face.Wires()) != expected_wires:
        raise ValueError("RISER actual end face does not match the requested wall evidence")
    return face


def build(ctx):
    state = prepare(ctx)
    axis = cq.Vector(*state['axis'])
    bottom = cq.Vector(*state['bottom'])
    diameter = state['diameter']
    frame_x = cq.Vector(*state['frame_x'])
    height = state['height']
    radius = state['radius']
    thickness = state['thickness']
    top = cq.Vector(*state['top'])
    plane = cq.Plane(origin=bottom, xDir=frame_x, normal=axis)
    profile = cq.Workplane(plane).circle(radius)
    if thickness is not None:
        profile = profile.circle(radius - thickness)
    shape = profile.extrude(height).val()
    if not shape.isValid() or len(shape.Solids()) != 1:
        raise ValueError("RISER did not produce one valid solid")

    expected_area = math.pi * (radius**2 - (radius - thickness)**2) if thickness is not None else math.pi * radius**2
    for key, outward, center in (("bottom", axis * -1, bottom), ("top", axis, top)):
        face = _end_face(shape, center, expected_area, height, diameter, 2 if thickness is not None else 1)
    return shape
