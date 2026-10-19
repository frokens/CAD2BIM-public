"""A nominal connection sphere with independently declared interfaces."""

import cadquery as cq


def build(ctx):
    shape = cq.Solid.makeSphere(ctx.param("diameter_m") / 2,
                                angleDegrees1=-90, angleDegrees2=90,
                                angleDegrees3=360)
    return shape
