"""Golden06 ANODE cast body at its own source-anchor coordinates, in metres."""

import cadquery as cq


def _octagon(width, height, chamfer):
    half_width, half_height = width / 2, height / 2
    return [
        (-half_width + chamfer, -half_height),
        (half_width - chamfer, -half_height),
        (half_width, -half_height + chamfer),
        (half_width, half_height - chamfer),
        (half_width - chamfer, half_height),
        (-half_width + chamfer, half_height),
        (-half_width, half_height - chamfer),
        (-half_width, -half_height + chamfer),
    ]


def build(ctx):
    length, width, height = 0.45, 0.11, 0.09
    taper, chamfer, end_ratio = 0.038, 0.012, 0.68
    wires = []
    for x, scale in (
        (-length / 2, end_ratio),
        (-length / 2 + taper, 1.0),
        (length / 2 - taper, 1.0),
        (length / 2, end_ratio),
    ):
        wires.append(
            cq.Workplane("YZ", origin=(x, 0, 0))
            .polyline(_octagon(width * scale, height * scale, chamfer * scale))
            .close()
            .val()
        )
    body = cq.Solid.makeLoft(wires, ruled=True)
    core_hole = cq.Solid.makeCylinder(
        0.006, length + 0.002, cq.Vector(-length / 2 - 0.001, 0, 0), cq.Vector(1, 0, 0)
    )
    body = body.cut(core_hole).clean()
    if not body.isValid() or len(body.Solids()) != 1:
        raise ValueError("cast_body must be one valid holed OCCT solid")
    return body
