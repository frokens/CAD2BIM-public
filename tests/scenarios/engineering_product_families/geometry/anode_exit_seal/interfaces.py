"""Parameter-defined interfaces; no native geometry is loaded."""

def evaluate(ctx):
    length = .04
    ctx.define_interface(key="body_side", origin_m=(0, 0, 0), axis=(-1, 0, 0), reference_direction=(0, 0, 1), profile=None)
    ctx.define_interface(key="lead_exit_passage", origin_m=(0.028, 0, 0), axis=(1, 0, 0), reference_direction=(0, 0, 1), profile=None)
    ctx.define_interface(key="outer_end", origin_m=(length, 0, 0), axis=(1, 0, 0), reference_direction=(0, 0, 1), profile=None)
