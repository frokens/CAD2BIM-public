from .parameters import prepare, scale

def evaluate(ctx):
    state = prepare(ctx)
    axis = state['axis']
    bottom = state['bottom']
    diameter = state['diameter']
    frame_x = state['frame_x']
    top = state['top']
    for key, center, outward in (('bottom', bottom, scale(axis, -1)), ('top', top, axis)):
        ctx.define_interface(key=key, origin_m=center, axis=outward, reference_direction=frame_x, profile={'kind': 'circular', 'diameter_m': diameter})
