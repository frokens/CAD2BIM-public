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
    trial = (0, 0, 1)
    projected = sub(trial, scale(axis, dot(trial, axis)))
    if norm(projected) < 32 * math.sqrt(2.220446049250313e-16):
        trial = (1, 0, 0)
        projected = sub(trial, scale(axis, dot(trial, axis)))
    return unit(projected)

def prepare(ctx):
    supplied_ports = ctx.param('ports')
    specification = ctx.param('shape')
    mode = specification['mode']
    center = vector(specification['local_center_m'])
    keys = [specification[name] for name in ('main_a_key', 'main_b_key', 'branch_key')]
    if sorted(keys) != ['a', 'b', 'c']:
        raise ValueError('TEE role keys must be a permutation of three sources')
    ports, axes, diameters, refs = ({}, {}, {}, {})
    for key in 'abc':
        port = supplied_ports['side_' + key]
        ports[key] = vector(port['origin_m'])
        axes[key] = unit(vector(port['axis']))
        diameters[key] = port['outer_diameter_m']
        refs[key] = unit(vector(port['reference_direction']))
        if not math.isfinite(diameters[key]) or diameters[key] <= 0:
            raise ValueError('TEE diameter must be finite and positive')
        if abs(norm(vector(port['reference_direction']))-1) > 1e-07 or abs(dot(refs[key], axes[key])) > 1e-07:
            raise ValueError('TEE local port reference is not perpendicular to its axis')
    a, b, branch = keys
    ua, ub, uv = (axes[a], axes[b], axes[branch])
    da, db, dv = (diameters[a], diameters[b], diameters[branch])
    delta = specification['turn_radian'] if mode in ('curved', 'near_zero') else 0.0
    bend = specification['bend_scale_m']
    radius = specification['arc_radius_m'] if mode == 'curved' else 0.0
    neck = specification['branch_neck_scale_m']
    if not all((math.isfinite(x) for x in (delta, bend, radius, neck))) or bend <= 0 or neck <= 0:
        raise ValueError('TEE scales must be finite and positive')
    actual_delta = math.atan2(norm(cross(ua, ub)), max(-1, min(1, -dot(ua, ub))))
    if abs(actual_delta - delta) > 1e-07 or delta > math.radians(150) + 1e-12:
        raise ValueError('TEE main turn differs from solved directions')
    fmain = 1.1 * max(da, db)
    fa, fb = (1.1 * da, 1.1 * db)
    ea = min(fmain * (fmain - fa) / (fa + fb) + 0.5 * fmain, 2 * fmain) + 0.3 * fmain
    eb = min(fmain * (fmain - fb) / (fa + fb) + 0.5 * fmain, 2 * fmain) + 0.3 * fmain
    fmax = 1.1 * max(da, db, dv)
    main_transition_radius = fmax / 2
    ebranch = 2.1 * fmax * (fmax - 1.1 * dv) / (fmax + dv) + 0.45 * fmax
    if norm(sub(sub(ports[a], center), scale(ua, bend + ea))) > 1e-08 * max(1, fmain):
        raise ValueError('TEE main A port differs from solved occupancy')
    if norm(sub(sub(ports[b], center), scale(ub, bend + eb))) > 1e-08 * max(1, fmain):
        raise ValueError('TEE main B port differs from solved occupancy')
    if norm(sub(sub(ports[branch], center), scale(uv, neck + ebranch))) > 1e-08 * max(1, fmax):
        raise ValueError('TEE branch port differs from solved neck')
    physical_axes = dict(axes)
    physical_ports = dict(ports)
    approximation_budget = min(5e-05, 0.0002 * min(da, db))
    if mode == 'near_zero':
        approximation_bound = (max(bend + ea, bend + eb) + fmain / 2 + bend) * math.sin(delta / 2)
        if not 0 < delta <= math.radians(0.01) or approximation_bound > approximation_budget:
            raise ValueError('TEE near-zero main exceeds its local approximation envelope')
        main_axis = unit(sub(ub, ua))
        physical_axes[a], physical_axes[b] = (scale(main_axis, -1), main_axis)
        physical_ports[a] = add(center, scale(physical_axes[a], bend + ea))
        physical_ports[b] = add(center, scale(physical_axes[b], bend + eb))
    ta = add(center, scale(physical_axes[a], 0.9 * bend))
    tb = add(center, scale(physical_axes[b], 0.9 * bend))
    main_outer_radius = fmain / 2
    main_inner_radius = main_outer_radius * 0.86
    if mode == 'straight':
        if delta != 0 or radius != 0 or norm(add(ua, ub)) > 1e-07:
            raise ValueError('TEE straight main parameters disagree')
        middle = center
        outer_parts, void_parts = ([], [])
    elif mode == 'near_zero':
        middle = center
        outer_parts, void_parts = ([], [])
    elif mode == 'curved':
        if delta <= 0 or radius <= main_outer_radius * 1.01 - 1e-10 * fmain:
            raise ValueError('TEE curved main folds into itself')
        if abs(radius - 0.9 * bend / math.tan(delta / 2)) > 1e-08 * fmain:
            raise ValueError('TEE radius differs from solved relation')
        flow = scale(ua, -1)
        normal = unit(cross(flow, ub))
        radial = unit(cross(normal, flow))
        arc_center = add(ta, scale(radial, radius))
        end = add(add(ta, scale(flow, radius * math.sin(delta))), scale(radial, radius * (1 - math.cos(delta))))
        if norm(sub(end, tb)) > 1e-08 * max(1, fmain):
            raise ValueError('TEE arc does not close at second tangent')
        half = delta / 2
        middle = add(add(ta, scale(flow, radius * math.sin(half))), scale(radial, radius * (1 - math.cos(half))))
    else:
        raise ValueError('Unknown TEE main mode')
    branch_points = [middle, add(center, scale(uv, 0.7 * neck)), add(center, scale(uv, 0.9 * neck)), add(center, scale(uv, neck))]
    if norm(sub(branch_points[1], branch_points[0])) <= 0.05 * dv:
        raise ValueError('TEE branch curve has no clearance from main midpoint')
    branch_offset = sub(sub(middle, center), scale(uv, dot(sub(middle, center), uv)))
    branch_first_axial_distance = dot(sub(branch_points[1], branch_points[0]), uv)
    branch_approximation_budget = min(5e-05, 0.0002 * min(da, db, dv))
    branch_straight = branch_first_axial_distance > 0 and norm(branch_offset) * (1 + fmax / 2 / branch_first_axial_distance) <= branch_approximation_budget
    for diameter, allowance in ((da, ea), (db, eb)):
        thickness = (1.1 * diameter + fmain) * .05
        if bend + allowance - .1 * thickness <= bend:
            raise ValueError('TEE main leg cannot hold A transition and collar')
    thickness = (1.1 * dv + fmax) * .05
    if neck + ebranch - .1 * thickness <= neck:
        raise ValueError('TEE branch leg cannot hold A transition and collar')
    state = {'a': a, 'approximation_budget': approximation_budget, 'axes': axes, 'b': b, 'bend': bend, 'branch': branch, 'branch_offset': branch_offset, 'branch_points': branch_points, 'branch_straight': branch_straight, 'center': center, 'delta': delta, 'diameters': diameters, 'dv': dv, 'ea': ea, 'eb': eb, 'ebranch': ebranch, 'fmain': fmain, 'fmax': fmax, 'key': key, 'main_inner_radius': main_inner_radius, 'main_outer_radius': main_outer_radius, 'main_transition_radius': main_transition_radius, 'mode': mode, 'neck': neck, 'physical_axes': physical_axes, 'physical_ports': physical_ports, 'ports': ports, 'refs': refs, 'ta': ta, 'tb': tb, 'ub': ub, 'uv': uv}
    if mode == 'near_zero':
        pass
    if mode == 'straight':
        state['middle'] = middle
        state['outer_parts'] = outer_parts
        state['void_parts'] = void_parts
    elif mode == 'near_zero':
        state['middle'] = middle
        state['outer_parts'] = outer_parts
        state['void_parts'] = void_parts
    elif mode == 'curved':
        state['arc_center'] = arc_center
        state['flow'] = flow
        state['middle'] = middle
        state['normal'] = normal
    return state
