import math

def vector(values):
    return tuple(values)

def add(a, b):
    return tuple((a[i] + b[i] for i in range(3)))

def sub(a, b):
    return tuple((a[i] - b[i] for i in range(3)))

def scale(a, value):
    return tuple((x * value for x in a))

def dot(a, b):
    return sum((a[i] * b[i] for i in range(3)))

def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])

def norm(a):
    return math.hypot(*a)

def unit(value):
    length = norm(value)
    if not math.isfinite(length) or length <= 0:
        raise ValueError('direction must be finite and nonzero')
    return scale(value, 1 / length)

def reference(axis):
    preferred = (0, 0, 1)
    projected = sub(preferred, scale(axis, dot(preferred, axis)))
    if norm(projected) < 32 * math.sqrt(2.220446049250313e-16):
        preferred = (1, 0, 0)
        projected = sub(preferred, scale(axis, dot(preferred, axis)))
    return unit(projected)

def prepare(ctx):
    specification = ctx.param('shape')
    mode = specification['mode']
    ports = ctx.param('ports')
    pa, pb = (ports['side_a'], ports['side_b'])
    a, b = (vector(pa['origin_m']), vector(pb['origin_m']))
    ua, ub = (unit(vector(pa['axis'])), unit(vector(pb['axis'])))
    radial = unit(vector(specification['frame_x']))
    ref_a, ref_b = (unit(vector(pa['reference_direction'])), unit(vector(pb['reference_direction'])))
    da, db = (pa['outer_diameter_m'], pb['outer_diameter_m'])
    core_d = specification['body_outer_diameter_m']
    if not all((math.isfinite(x) for x in (da, db, core_d))):
        raise ValueError('CONNECTOR dimensions must be finite')
    if min(da, db, core_d) <= 0 or abs(dot(radial, ua)) > 1e-07 or abs(dot(radial, ub)) > 1e-07:
        raise ValueError('CONNECTOR diameters or frame are invalid')
    if any(abs(norm(vector(p['reference_direction']))-1) > 1e-07 for p in (pa, pb)) or abs(dot(ref_a, ua)) > 1e-07 or abs(dot(ref_b, ub)) > 1e-07:
        raise ValueError('CONNECTOR local port references must be perpendicular to their axes')
    if mode == 'straight':
        length = specification['sleeve_length_m']
        if length <= 0 or norm(add(ua, ub)) > 1e-07:
            raise ValueError('Straight sleeve parameters are inconsistent')
        if not math.isclose(core_d, 1.2 * max(da, db), rel_tol=1e-09):
            raise ValueError('Sleeve outer envelope differs from selected S profile')
        axis = unit(sub(b, a))
        if norm(add(axis, ua)) > 1e-07 or abs(norm(sub(b, a)) - length) > 1e-09 * length:
            raise ValueError('Sleeve length or port order is inconsistent')
        descending = da > db
        smaller, larger = min(da, db), max(da, db)
        land, shoulder = .08 * length, .16 * length
        span = length - 2 * (land + shoulder)
        if shoulder <= .10 * larger or span <= (larger - smaller) / 2:
            raise ValueError('Sleeve length violates actual S contour occupancy')
        bore_a, bore_b = (0.86 * da / 2, 0.86 * db / 2)
    elif mode == 'curved':
        center = vector(specification['local_center_m'])
        delta, bend_scale = (specification['turn_radian'], specification['bend_scale_m'])
        radius = specification['arc_radius_m']
        if not 0 < delta <= math.radians(150) + 1e-12 or bend_scale <= 0:
            raise ValueError('Curved connector needs a supported true turn')
        if not math.isclose(radius, 0.9 * bend_scale / math.tan(delta / 2), rel_tol=1e-09):
            raise ValueError('Curved radius differs from solved B relation')
        if radius <= 0.51 * core_d - 1e-10 * core_d:
            raise ValueError('Curved core intersects itself')
        bore_a, bore_b = (0.86 * da / 2, 0.86 * db / 2)
        fa, fb = (1.1 * da, 1.1 * db)
        if not math.isclose(core_d, max(fa, fb), rel_tol=1e-09):
            raise ValueError('Curved core diameter differs from baseline numerical relation')
        ea = min(core_d * (core_d - fa) / (fa + fb) + 0.5 * core_d, 2 * core_d)
        eb = min(core_d * (core_d - fb) / (fa + fb) + 0.5 * core_d, 2 * core_d)
        if norm(sub(sub(a, center), scale(ua, bend_scale + ea))) > 1e-08 * max(1, core_d):
            raise ValueError('Side A takeoff differs from solved B+e')
        if norm(sub(sub(b, center), scale(ub, bend_scale + eb))) > 1e-08 * max(1, core_d):
            raise ValueError('Side B takeoff differs from solved B+e')
        flow_a = scale(ua, -1)
        plane_normal = unit(cross(flow_a, ub))
        turn_radial = unit(cross(plane_normal, flow_a))
        if abs(dot(flow_a, ub) - math.cos(delta)) > 1e-07:
            raise ValueError('Source axes do not match solved true turn')
        tangent_a = add(center, scale(ua, 0.9 * bend_scale))
        tangent_b = add(center, scale(ub, 0.9 * bend_scale))
        length_a = norm(sub(tangent_a, a))
        length_b = norm(sub(tangent_b, b))
        for length, end_d in ((length_a, da), (length_b, db)):
            if length <= 1.1 * (1.1 * end_d + core_d) * .05 + 1e-6 * core_d:
                raise ValueError('Curved leg has no room for its selected annular collar')
            if min(end_d / 2, 1.1 * end_d / 2, core_d / 2) <= 1e-6:
                raise ValueError('Curved leg falls below native BREP resolution')
        arc_center = add(tangent_a, scale(turn_radial, radius))
        arc_end = add(add(tangent_a, scale(flow_a, radius * math.sin(delta))),
                      scale(turn_radial, radius * (1 - math.cos(delta))))
        if norm(sub(arc_end, tangent_b)) > 1e-8 * max(1, core_d):
            raise ValueError('Circular arc does not close at second tangent')
    else:
        raise ValueError('Unknown CONNECTOR branch')
    state = {'a': a, 'b': b, 'core_d': core_d, 'da': da, 'db': db, 'mode': mode, 'pa': pa, 'pb': pb, 'radial': radial, 'ref_a': ref_a, 'ref_b': ref_b, 'ua': ua, 'ub': ub}
    if mode == 'straight':
        state['bore_a'] = bore_a
        state['bore_b'] = bore_b
        state['descending'] = descending
        state['length'] = length
    elif mode == 'curved':
        state['bend_scale'] = bend_scale
        state['bore_a'] = bore_a
        state['bore_b'] = bore_b
        state['center'] = center
        state['delta'] = delta
        state['radius'] = radius
        state['flow_a'] = flow_a
        state['plane_normal'] = plane_normal
        state['tangent_a'] = tangent_a
        state['arc_center'] = arc_center
        state['length_a'] = length_a
        state['length_b'] = length_b
    return state
