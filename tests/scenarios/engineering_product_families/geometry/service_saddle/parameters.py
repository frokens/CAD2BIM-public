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
    projected = sub(preferred, scale(axis, dot(axis, preferred)))
    if norm(projected) <= 1e-08:
        preferred = (1, 0, 0)
        projected = sub(preferred, scale(axis, dot(axis, preferred)))
    return unit(projected)

def prepare(ctx):
    dh = ctx.param('host_outer_diameter_m')
    db = ctx.param('branch_outer_diameter_m')
    specification = ctx.param('shape')
    direction = ctx.param('branch_local_direction')
    reference = ctx.param('branch_local_reference_axis')
    if specification['mode'] != 'detailed':
        raise ValueError('SERVICE_SADDLE shape mode is unsupported')
    half = specification['collar_half_length_m']
    port = specification['branch_port_station_m']
    if not all((math.isfinite(value) for value in (dh, db, half, port, *direction, *reference))):
        raise ValueError('SERVICE_SADDLE dimensions must be finite')
    if min(dh, db, half, port) <= 0 or len(direction) != 3 or len(reference) != 3:
        raise ValueError('SERVICE_SADDLE dimensions or direction are invalid')
    dx, dy, dz = direction
    q = math.hypot(dy, dz)
    if q <= 0 or dz <= 0 or abs(dy) > 1e-09 or (abs(math.hypot(*direction) - 1) > 1e-08):
        raise ValueError('SERVICE_SADDLE needs a resolved outward local branch direction')
    if abs(math.hypot(*reference) - 1) > 1e-08 or abs(sum((a * b for a, b in zip(reference, direction)))) > 1e-08:
        raise ValueError('SERVICE_SADDLE GAS reference must be unit and normal to branch')
    extent = (0.58 * dh * abs(dx) + 0.72 * db) / q
    if half + 1e-10 * max(dh, db) < extent:
        raise ValueError('SERVICE_SADDLE junction exceeds finite collar')
    if abs(port * q - (dh / 2 + 2 * db)) > 1e-09 * max(dh, db):
        raise ValueError('SERVICE_SADDLE port station differs from selected contour')
    state = {'db': db, 'dh': dh, 'direction': direction, 'dx': dx, 'half': half, 'port': port, 'q': q, 'reference': reference}
    return state
