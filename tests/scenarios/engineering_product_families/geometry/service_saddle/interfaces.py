from .parameters import prepare

def evaluate(ctx):
    state = prepare(ctx)
    db = state['db']
    direction = state['direction']
    port = state['port']
    reference = state['reference']
    ctx.define_interface(key='host_datum', origin_m=(0, 0, 0), axis=(0, 0, 1), reference_direction=(1, 0, 0), profile=None)
    ctx.define_interface(key='gas_branch', origin_m=tuple((x * port for x in direction)), axis=tuple(direction), reference_direction=tuple(reference), profile={'kind': 'circular', 'diameter_m': db})
