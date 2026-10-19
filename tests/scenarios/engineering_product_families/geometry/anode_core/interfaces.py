"""Parameter-defined interfaces; no native geometry is loaded."""

def evaluate(ctx):
    total_length = .496
    ctx.define_interface(key="left_tip", origin_m=(0, 0, 0), axis=(-1, 0, 0), reference_direction=(0, 0, 1), profile=None)
    ctx.define_interface(key="lead_exit", origin_m=(total_length, 0, 0), axis=(1, 0, 0), reference_direction=(0, 0, 1), profile=None)
