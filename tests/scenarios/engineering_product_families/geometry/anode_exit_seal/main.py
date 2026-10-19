"""Golden06 ANODE tapered exit seal, local origin at its body-side plane."""

import cadquery as cq


def build(ctx):
    length = 0.04
    seal = cq.Solid.makeCone(0.013, 0.010, length, cq.Vector(0, 0, 0), cq.Vector(1, 0, 0))
    seal = seal.cut(cq.Solid.makeCylinder(0.006, 0.028, cq.Vector(0, 0, 0), cq.Vector(1, 0, 0)))
    seal = seal.cut(cq.Solid.makeCylinder(0.005, 0.012, cq.Vector(0.028, 0, 0), cq.Vector(1, 0, 0))).clean()
    if not seal.isValid() or len(seal.Solids()) != 1:
        raise ValueError("exit_seal must be one valid hollow OCCT solid")
    return seal
