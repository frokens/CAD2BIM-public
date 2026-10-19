from .parameters import prepare

"""One continuous mitered PIPE body from a complete Family-local path."""

import math

import cadquery as cq


_BINARY64_EPSILON = 2.220446049250313e-16












def _reference(axis):
    preferred = cq.Vector(0, 0, 1)
    projected = preferred - axis * axis.dot(preferred)
    # Projection near the square-root floating-point precision scale loses
    # reliable direction. Only then use X, never for an ordinary tilted axis.
    if projected.Length <= 32 * math.sqrt(_BINARY64_EPSILON):
        preferred = cq.Vector(1, 0, 0)
        projected = preferred - axis * axis.dot(preferred)
    return projected.normalized()


def _miter_segment(start, end, direction, start_normal, end_normal,
                   outer_radius, thickness, *, square_start=False, square_end=False):
    """Clip a straight section at the two shared analytical miter planes."""
    length = (end - start).Length
    start_dot = start_normal.dot(direction)
    end_dot = end_normal.dot(direction)
    h0 = outer_radius * start_normal.cross(direction).Length / start_dot
    h1 = outer_radius * end_normal.cross(direction).Length / end_dot
    # At the chain ends prepare() supplies the segment direction itself as
    # the plane normal. Extrusion already creates that exact square end face;
    # extending beyond it only to intersect it back is redundant.
    start_extension_length = 0 if square_start else h0 + outer_radius
    end_extension_length = 0 if square_end else h1 + outer_radius
    extended = length + start_extension_length + end_extension_length
    extent = 2 * extended
    start_extension = start - direction * start_extension_length
    plane = cq.Plane(origin=start_extension, xDir=_reference(direction), normal=direction)
    profile = (cq.Workplane(plane).moveTo(outer_radius, 0)
               .threePointArc((0, outer_radius), (-outer_radius, 0))
               .threePointArc((0, -outer_radius), (outer_radius, 0)).close())
    if thickness is not None:
        inner_radius = outer_radius - thickness
        profile = (profile.moveTo(inner_radius, 0)
                   .threePointArc((0, inner_radius), (-inner_radius, 0))
                   .threePointArc((0, -inner_radius), (inner_radius, 0)).close())
    cylinder = profile.extrude(extended, clean=False).val()
    if not square_start:
        first = (cq.Workplane(cq.Plane(origin=start, xDir=_reference(start_normal),
                                   normal=start_normal))
                 .box(2 * extent, 2 * extent, extent,
                      centered=(True, True, False)).val())
        cylinder = cylinder.intersect(first)
    if not square_end:
        last = (cq.Workplane(cq.Plane(origin=end, xDir=_reference(end_normal * -1),
                                    normal=end_normal * -1))
                .box(2 * extent, 2 * extent, extent,
                     centered=(True, True, False)).val())
        cylinder = cylinder.intersect(last)
    return cylinder


def build(ctx):
    state = prepare(ctx)
    outer_diameter = state['outer_diameter']
    thickness = state['thickness']
    chain = [cq.Vector(*v) for v in state['chain']]
    chain_directions = [cq.Vector(*v) for v in state['chain_directions']]
    miter_normals = [cq.Vector(*v) for v in state['miter_normals']]
    shape = None
    for index, (start, end, direction) in enumerate(zip(chain, chain[1:], chain_directions)):
        part = _miter_segment(start, end, direction,
                              miter_normals[index], miter_normals[index + 1],
                              outer_diameter / 2, thickness,
                              square_start=index == 0,
                              square_end=index == len(chain_directions) - 1)
        shape = part if shape is None else shape.fuse(part)
    if shape is None or not shape.isValid() or len(shape.Solids()) != 1 or len(shape.Shells()) != 1:
        raise ValueError("PIPE mitered chain did not yield one valid closed solid")

    return shape
