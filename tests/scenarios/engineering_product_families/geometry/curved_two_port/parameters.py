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

def prepare(ctx):
    ports = ctx.param('ports')
    specification = ctx.param('shape')
    mode = specification['mode']
    pa, pb = (ports['side_a'], ports['side_b'])
    a, b = (vector(pa['origin_m']), vector(pb['origin_m']))
    ua, ub = (vector(pa['axis']), vector(pb['axis']))
    reference_a = vector(pa['reference_direction'])
    reference_b = vector(pb['reference_direction'])
    da, db = (pa['outer_diameter_m'], pb['outer_diameter_m'])
    radial = unit(vector(specification['frame_x']))
    if abs(dot(radial, ua)) > 1e-07 or abs(dot(radial, ub)) > 1e-07:
        raise ValueError('Two-port radial frame is invalid')
    for axis, reference in ((ua, reference_a), (ub, reference_b)):
        if (not all(math.isfinite(value) for value in reference)
                or abs(norm(reference) - 1.0) > 1e-07 or abs(dot(reference, axis)) > 1e-07):
            raise ValueError('Port reference directions must be finite unit vectors perpendicular to their axes')
    if (0.07 if mode == 'straight' else 0.025) * min(da, db) <= 1e-06:
        raise ValueError('Designed section is below the BREP resolution margin')
    model_scale = max(da, db, norm(sub(b, a)))
    if mode == 'straight':
        if norm(add(ua, ub)) > 1e-07:
            raise ValueError('Direct-through inputs are inconsistent')
        axis = unit(sub(b, a))
        if norm(add(axis, ua)) > 1e-07:
            raise ValueError('Direct-through port order is inconsistent')
        bore_a, bore_b = (da / 2 - 0.07 * min(da, db), db / 2 - 0.07 * min(da, db))
    elif mode == 'curved':
        center = vector(specification['local_center_m'])
        body_d = specification['body_outer_diameter_m']
        delta = specification['turn_radian']
        bend_scale = specification['bend_scale_m']
        radius = specification['arc_radius_m']
        model_scale = max(model_scale, body_d)
        if not 0 < delta <= math.radians(160) + 1e-12 or bend_scale <= 0:
            raise ValueError('Curved branch needs a supported positive turn and scale')
        if not math.isclose(radius, 0.9 * bend_scale / math.tan(delta / 2), rel_tol=1e-09):
            raise ValueError('Arc radius does not match solved bend scale')
        if radius <= 0.51 * body_d - 1e-10 * body_d:
            raise ValueError('Arc radius does not clear the body outside radius')
        fa, fb = (1.1 * da, 1.1 * db)
        if not math.isclose(body_d, max(fa, fb), rel_tol=1e-09):
            raise ValueError('Curved body OD differs from solved envelope')
        ea = min(body_d * (body_d - fa) / (fa + fb) + 0.5 * body_d, 2 * body_d)
        eb = min(body_d * (body_d - fb) / (fa + fb) + 0.5 * body_d, 2 * body_d)
        if norm(sub(a, add(center, scale(ua, bend_scale + ea)))) > max(1e-09 * model_scale, 1e-12):
            raise ValueError('Port A does not match the solved bend relation')
        if norm(sub(b, add(center, scale(ub, bend_scale + eb)))) > max(1e-09 * model_scale, 1e-12):
            raise ValueError('Port B does not match the solved bend relation')
        flow_a = scale(ua, -1)
        plane_normal = unit(cross(flow_a, ub))
        turn_radial = unit(cross(plane_normal, flow_a))
        if abs(dot(flow_a, ub) - math.cos(delta)) > 1e-07:
            raise ValueError('Curved source axes do not match the turn')
        takeoff = 0.9 * bend_scale
        tangent_a = add(center, scale(ua, takeoff))
        tangent_b = add(center, scale(ub, takeoff))
        length_a = norm(sub(tangent_a, a))
        length_b = norm(sub(tangent_b, b))
        for length, end_d in ((length_a, da), (length_b, db)):
            transition = max(.1 * body_d, 1.875 * abs(body_d - end_d) / 2)
            if length <= .3 * end_d + transition:
                raise ValueError('Bend leg does not match the preserved occupancy contract')
        arc_center = add(tangent_a, scale(turn_radial, radius))
        arc_end = add(add(tangent_a, scale(flow_a, radius * math.sin(delta))), scale(turn_radial, radius * (1 - math.cos(delta))))
        if norm(sub(arc_end, tangent_b)) > max(1e-08 * model_scale, 1e-11):
            raise ValueError('Arc does not close at second tangent')
        bore_a, bore_b = (0.86 * da / 2, 0.86 * db / 2)
    else:
        raise ValueError('Unknown two-port shape mode')
    state = {'a': a, 'b': b, 'da': da, 'db': db, 'mode': mode, 'pa': pa, 'pb': pb, 'radial': radial, 'reference_a': reference_a, 'reference_b': reference_b, 'ua': ua, 'ub': ub}
    state['model_scale'] = model_scale
    if mode == 'straight':
        state['axis'] = axis
        state['bore_a'] = bore_a
        state['bore_b'] = bore_b
    elif mode == 'curved':
        state['arc_center'] = arc_center
        state['body_d'] = body_d
        state['bore_a'] = bore_a
        state['bore_b'] = bore_b
        state['delta'] = delta
        state['flow_a'] = flow_a
        state['length_a'] = length_a
        state['length_b'] = length_b
        state['model_scale'] = model_scale
        state['plane_normal'] = plane_normal
        state['tangent_a'] = tangent_a
    return state
