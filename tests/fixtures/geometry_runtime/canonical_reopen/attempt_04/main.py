import math
import cadquery as cq

def _unit(v):
    n = math.sqrt(sum(x*x for x in v))
    if n < 1e-9:
        raise ValueError("zero direction")
    return tuple(x/n for x in v)

def _cylinder_between(a, b, r):
    ax, ay, az = a
    bx, by, bz = b
    v = (bx-ax, by-ay, bz-az)
    L = math.sqrt(sum(x*x for x in v))
    if L < 1e-8:
        return cq.Workplane("XY").sphere(r).val()
    d = tuple(x/L for x in v)
    inset = 0.02
    start = tuple(a[i] - inset*d[i] for i in range(3))
    length = L + 2.0*inset
    return cq.Solid.makeCylinder(r, length, cq.Vector(*start), cq.Vector(*d))

def _route(points, radii):
    out = None
    for i in range(len(points)-1):
        s = _cylinder_between(points[i], points[i+1], radii[i])
        out = s if out is None else out.fuse(s)
    for p, r in zip(points[1:-1], radii[1:-1]):
        out = out.fuse(cq.Solid.makeSphere(r, cq.Vector(*p)))
    return out

def build(ctx):
    p1 = (ctx.param("p1_x"), ctx.param("p1_y"), ctx.param("p1_z"))
    out_axis = _unit((ctx.param("out_nx"), ctx.param("out_ny"), ctx.param("out_nz")))
    in_axis = (0.0, 0.0, -1.0)
    in_od = ctx.param("in_od_m")
    out_od = ctx.param("out_od_m")
    in_id = ctx.param("in_id_m")
    out_id = ctx.param("out_id_m")
    if in_id >= in_od or out_id >= out_od:
        raise ValueError("bore must be smaller than OD")
    pin = (0.0, 0.0, 0.0)
    qin = tuple(pin[i] - in_axis[i]*0.12 for i in range(3))
    qout = tuple(p1[i] - out_axis[i]*0.12 for i in range(3))
    mid = (0.12, 0.0, 0.0)
    # Fixed central body dimensions; route/adapters vary with endpoint facts.
    body = cq.Workplane("XY").box(0.24, 0.20, 0.18).translate((0.12,0,0)).val()
    outer = _route([qin, mid, qout], [in_od/2, max(in_od,out_od)/2+0.006, out_od/2])
    outer = outer.fuse(cq.Solid.makeCylinder(in_od/2, 0.12, cq.Vector(*pin), cq.Vector(*( -x for x in in_axis))))
    outer = outer.fuse(cq.Solid.makeCylinder(out_od/2, 0.12, cq.Vector(*qout), cq.Vector(*out_axis)))
    shape = body.fuse(outer)
    inner = _route([pin, mid, p1], [in_id/2, min(in_id,out_id)/2, out_id/2])
    # The port bores are deliberately open at both actual terminal faces.
    result = shape.cut(inner)
    return result.clean()

