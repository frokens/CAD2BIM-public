from .parameters import prepare

def evaluate(ctx):
    state = prepare(ctx)
    length_m = state['length_m']
    od_m = state['od_m']
    ctx.define_interface(key='body_joint', origin_m=(0, 0, 0), axis=(-1, 0, 0), reference_direction=(0, 0, 1))
    ctx.define_interface(key='gas_end', origin_m=(length_m, 0, 0), axis=(1, 0, 0), reference_direction=(0, 0, 1), profile={'kind': 'circular', 'diameter_m': od_m})
