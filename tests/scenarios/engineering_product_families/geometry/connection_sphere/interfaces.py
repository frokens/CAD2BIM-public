def evaluate(ctx):
    for interface in ctx.param("interfaces"):
        profile = (None if interface["kind"] == "datum" else
                   {"kind": "circular", "diameter_m": interface["diameter_m"]})
        ctx.define_interface(
            key=interface["key"], origin_m=interface["origin_m"],
            axis=interface["axis"], reference_direction=interface["reference_direction"],
            profile=profile,
        )
