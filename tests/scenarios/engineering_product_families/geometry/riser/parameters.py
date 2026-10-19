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
    bottom = (*ctx.param('local_bottom_m'),)
    top = (*ctx.param('local_top_m'),)
    axis = unit((*ctx.param('axis_unit'),))
    frame_x = unit((*ctx.param('frame_x'),))
    height = ctx.param('height_m')
    diameter = ctx.param('outer_diameter_m')
    thickness_values = ctx.param('section')['known_wall_thickness_m']
    if not math.isfinite(height) or height <= 0 or (not math.isfinite(diameter)) or (diameter <= 0):
        raise ValueError('RISER height and OD must be finite and positive')
    if len(thickness_values) > 1:
        raise ValueError('RISER section permits at most one known wall thickness')
    thickness = thickness_values[0] if thickness_values else None
    radius = diameter / 2
    if thickness is not None and (not math.isfinite(thickness) or not 0 < thickness < radius):
        raise ValueError('RISER known wall thickness must be below the outer radius')
    if abs(dot(axis, frame_x)) > 1e-09:
        raise ValueError('RISER frame reference is not perpendicular to the axis')
    if norm(sub(top, add(bottom, scale(axis, height)))) > max(1e-12, 1e-09 * height):
        raise ValueError('RISER local top does not match axis and height')
    state = {'axis': axis, 'bottom': bottom, 'diameter': diameter, 'frame_x': frame_x, 'height': height, 'radius': radius, 'thickness': thickness, 'top': top}
    return state
