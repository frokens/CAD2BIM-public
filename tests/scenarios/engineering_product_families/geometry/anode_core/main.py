"""Golden06 ANODE metal core, local origin at its actual left tip, in metres."""

import cadquery as cq


def build(ctx):
    radius, tip_length, total_length = 0.006, 0.002, 0.496
    tip = cq.Solid.makeCone(0.005, radius, tip_length, cq.Vector(0, 0, 0), cq.Vector(1, 0, 0))
    shaft = cq.Solid.makeCylinder(radius, total_length - tip_length, cq.Vector(tip_length, 0, 0), cq.Vector(1, 0, 0))
    core = tip.fuse(shaft).clean()
    if not core.isValid() or len(core.Solids()) != 1:
        raise ValueError("core must be one valid OCCT solid")
    return core
