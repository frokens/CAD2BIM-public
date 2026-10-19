"""Parameter-defined interfaces; no native geometry is loaded."""

def evaluate(ctx):
    ctx.define_interface(key="lead_exit", origin_m=(0, 0, 0), axis=(1, 0, 0), reference_direction=(0, 0, 1), profile=None)
    ctx.define_interface(key="free_lead_tip", origin_m=(0.084, 0, -0.105), axis=(0, 0, -1), reference_direction=(1, 0, 0), profile=None)
