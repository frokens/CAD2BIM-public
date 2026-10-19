from .parameters import prepare, scale

def evaluate(ctx):
    state = prepare(ctx)
    outer_diameter = state['outer_diameter']
    points = state['points']
    tangents = state['tangents']
    for index, (key, point, normal) in enumerate((('inlet', points[0], scale(tangents[0], -1)), ('outlet', points[-1], tangents[-1]))):
        ctx.define_interface(key=key, origin_m=point, axis=normal, reference_direction=state['references'][index], profile={'kind': 'circular', 'diameter_m': outer_diameter})
