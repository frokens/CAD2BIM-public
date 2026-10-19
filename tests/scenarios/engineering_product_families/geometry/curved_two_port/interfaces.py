from .parameters import prepare

def evaluate(ctx):
    state = prepare(ctx)
    pa = state['pa']
    pb = state['pb']
    for key, port in (('side_a', pa), ('side_b', pb)):
        ctx.define_interface(key=key, origin_m=tuple(port['origin_m']), axis=tuple(port['axis']), reference_direction=tuple(port['reference_direction']), profile={'kind': 'circular', 'diameter_m': port['outer_diameter_m']})
