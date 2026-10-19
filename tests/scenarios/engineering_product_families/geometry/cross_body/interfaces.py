from .parameters import prepare

def evaluate(ctx):
    state = prepare(ctx)
    side_data = state['side_data']
    for side in side_data:
        ctx.define_interface(key='side_' + side['key'], origin_m=side['point'], axis=side['axis'], reference_direction=side['ref'], profile={'kind': 'circular', 'diameter_m': side['od']})
