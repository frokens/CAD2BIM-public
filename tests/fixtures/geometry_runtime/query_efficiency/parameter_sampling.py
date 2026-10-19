from OCP.BRepAdaptor import BRepAdaptor_Curve

def query(ctx):
    edges = ctx.private_geometry().Edges()
    sampled = []
    for index, edge in enumerate(edges[:16]):
        curve = BRepAdaptor_Curve(edge.wrapped)
        first, last = curve.FirstParameter(), curve.LastParameter()
        points = []
        for j in range(9):
            point = curve.Value(first + (last - first) * j / 8)
            points.append([point.X(), point.Y(), point.Z()])
        sampled.append({"edge_index": index, "curve_type": edge.geomType(),
                        "parameter_range": [first, last], "points_m": points})
    return {"edge_count": len(edges), "sampled": sampled,
            "scope": "exploratory native-parameter samples of at most 16 edges; "
                     "not equal arc-length samples or a global-distance certificate"}
