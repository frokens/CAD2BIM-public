from .parameters import prepare

def evaluate(ctx):
    state = prepare(ctx)
    axes = state['axes']
    diameters = state['diameters']
    key = state['key']
    ports = state['ports']
    refs = state['refs']
    for key in 'abc':
        ctx.define_interface(key='side_' + key, origin_m=ports[key], axis=axes[key], reference_direction=refs[key], profile={'kind': 'circular', 'diameter_m': diameters[key]})
