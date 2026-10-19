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

def profile_values(radius, port_radius, seam):
    s = port_radius+seam
    if not 0 < s < radius:
        raise ValueError("CROSS seam cap is outside chamber")
    x = math.sqrt(radius*radius-s*s)
    transition = 2*seam*s/x
    return s, x, x+transition, -x/s

def prepare(ctx):
    center = vector(ctx.param('local_center_m'))
    radius = ctx.param('chamber_radius_m')
    wall = ctx.param('chamber_wall_m')
    seam = ctx.param('seam_allowance_m')
    if not all((math.isfinite(x) and x > 0 for x in (radius, wall, seam))) or wall >= radius:
        raise ValueError('CROSS chamber dimensions are invalid')
    side_data = []
    for key in 'abcd':
        port = vector(ctx.param(f'side_{key}_local_port_m'))
        axis = unit(vector(ctx.param(f'side_{key}_outward_axis')))
        ref = unit(vector(ctx.param(f'side_{key}_reference_axis')))
        od = ctx.param(f'side_{key}_outer_diameter_m')
        wi = ctx.param(f'side_{key}_wall_m')
        if not all((math.isfinite(x) for x in (od, wi))) or not 0 < wi < od / 2:
            raise ValueError('CROSS port wall or OD is invalid')
        if abs(dot(ref, axis)) > 1e-07:
            raise ValueError('CROSS reference must be perpendicular to its port axis')
        axial = dot(sub(port, center), axis)
        residual = norm(sub(sub(port, center), scale(axis, axial)))
        if residual > max(1e-07 * od, 1e-10) or axial <= 0:
            raise ValueError('CROSS port is off its outward centerline')
        side_data.append({'key': key, 'point': port, 'axis': axis, 'ref': ref, 'od': od, 'wall': wi, 'q': axial})
    for i, a in enumerate(side_data):
        for b in side_data[i + 1:]:
            if norm(sub(a['axis'], b['axis'])) <= 1e-07:
                raise ValueError('CROSS has coincident rays')
            theta = math.atan2(norm(cross(a['axis'], b['axis'])), max(-1.0, min(1.0, dot(a['axis'], b['axis']))))
            for inner in (False, True):
                rho = radius - wall if inner else radius
                ra = a['od'] / 2 - (a['wall'] if inner else 0) + seam
                rb = b['od'] / 2 - (b['wall'] if inner else 0) + seam
                if rho <= max(ra, rb) or math.asin(ra / rho) + math.asin(rb / rho) >= theta - 1e-08:
                    raise ValueError('CROSS chamber cap openings overlap')
    for side in side_data:
        for inner in (False, True):
            chamber_radius = radius - wall if inner else radius
            port_radius = side['od'] / 2 - (side['wall'] if inner else 0)
            endpoint = (max(side['q'], radius) + max(1e-5 * side['od'], 1e-8)) if inner else side['q']
            _, _, last, _ = profile_values(chamber_radius, port_radius, seam)
            if endpoint <= last:
                raise ValueError('CROSS port precedes its transition')
    state = {'center': center, 'radius': radius, 'seam': seam, 'side_data': side_data, 'wall': wall}
    return state
