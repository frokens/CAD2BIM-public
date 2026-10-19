def query(ctx):
    edges = ctx.private_geometry().Edges()
    circles = [edge for edge in edges if edge.geomType() == "CIRCLE"]
    radii = [edge.radius() for edge in circles]
    return {"edge_count": len(edges), "analytic_circle_count": len(circles),
            "radius_range_m": [min(radii), max(radii)] if radii else None,
            "scope": "analytic circle edges only; other curve types remain unmeasured"}
