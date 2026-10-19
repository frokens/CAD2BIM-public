"""Golden06 ANODE conductor, local origin at the evidenced lead exit."""

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


def build(ctx):
    conductor = cq.Workplane("YZ").circle(0.003).sweep(cq.Workplane().newObject([_wire(0.01)]), isFrenet=False).val()
    if not conductor.isValid() or len(conductor.Solids()) != 1:
        raise ValueError("lead_conductor must be one valid swept OCCT solid")
    return conductor
