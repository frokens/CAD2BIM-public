"""Golden06 ANODE lead jacket, local origin at the evidenced lead exit."""

import math

import cadquery as cq


def _wire(extra):
    straight, radius, drop = 0.044, 0.04, 0.055
    start = cq.Vector(0, 0, 0)
    tangent = cq.Vector(straight, 0, 0)
    middle = cq.Vector(straight + radius / math.sqrt(2), 0, -radius + radius / math.sqrt(2))
    bend_end = cq.Vector(straight + radius, 0, -radius)
    end = cq.Vector(straight + radius, 0, -radius - drop - extra)
    return cq.Wire.assembleEdges([
        cq.Edge.makeLine(start, tangent),
        cq.Edge.makeThreePointArc(tangent, middle, bend_end),
        cq.Edge.makeLine(bend_end, end),
    ])


def _sweep(radius, extra):
    return cq.Workplane("YZ").circle(radius).sweep(cq.Workplane().newObject([_wire(extra)]), isFrenet=False).val()


def build(ctx):
    jacket = _sweep(0.005, 0).cut(_sweep(0.003, 0.01)).clean()
    if not jacket.isValid() or len(jacket.Solids()) != 1:
        raise ValueError("lead_jacket must be one valid hollow swept OCCT solid")
    return jacket
