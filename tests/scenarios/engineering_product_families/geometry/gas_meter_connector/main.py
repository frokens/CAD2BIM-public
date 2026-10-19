from .parameters import prepare

import math

import cadquery as cq


def _cylinder(radius, length, start, direction=(1, 0, 0)):
    return cq.Solid.makeCylinder(
        radius, length, cq.Vector(*start), cq.Vector(*direction)
    )


def _soft_cylinder(radius, length, start, fillet):
    return cq.Workplane(
        obj=_cylinder(radius, length, start)
    ).edges().fillet(fillet).val()


def _hex_prism(radius, length, start, chamfer):
    plane = cq.Plane(origin=start, xDir=(0, 1, 0), normal=(1, 0, 0))
    return (
        cq.Workplane(plane)
        .polygon(6, 2 * radius)
        .extrude(length)
        .edges("not %CIRCLE")
        .chamfer(chamfer)
        .val()
    )


def build(ctx):
    state = prepare(ctx)
    collar_length = state['collar_length']
    length = state['length']
    radius = state['radius']
    # v2's 1/.6 mm treatments stay exact for its selected nominal examples.
    # Below those sizes, scale edge treatments to the same six-part profile.
    fillet = min(1.0, 0.2 * radius, 0.2 * collar_length)
    chamfer = min(0.6, 0.1 * radius, 0.1 * collar_length)

    parts = [
        _cylinder(radius, length, (0, 0, 0)),
        _soft_cylinder(radius * 1.18, collar_length, (0, 0, 0), fillet),
        _hex_prism(radius * 1.36, collar_length, (collar_length, 0, 0), chamfer),
    ]
    for index in range(3):
        offset = 2 * collar_length + 2 + index * 3
        parts.append(_cylinder(radius + 0.7, 1.2, (offset, 0, 0)))
    if len(parts) != 6 or any(not part.isValid() or len(part.Solids()) != 1 for part in parts):
        raise ValueError("CONNECTOR_SOURCE_COMPLETENESS_6")

    return cq.Compound.makeCompound(parts).scale(0.001)
