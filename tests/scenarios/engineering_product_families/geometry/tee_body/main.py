from .parameters import prepare

"""One hollow TEE body with exact ports and a simplified shallow central main."""

import math

import cadquery as cq




def split_main_circle(radius, center, normal):
    return cq.Wire.assembleEdges([
        cq.Edge.makeCircle(radius, center, normal, 0, 180),
        cq.Edge.makeCircle(radius, center, normal, 180, 360),
    ])


def unit(value):
    if not math.isfinite(value.Length) or value.Length <= 0:
        raise ValueError("TEE needs finite nonzero directions")
    return value.normalized()


def ruled_main(radius, first, first_axis, second, second_axis, radial):
    wires = []
    for point, axis in ((first, first_axis), (second, second_axis)):
        plane = cq.Plane(origin=point, xDir=radial, normal=axis)
        wires.append((cq.Workplane(plane).moveTo(radius, 0)
                      .threePointArc((0, radius), (-radius, 0))
                      .threePointArc((0, -radius), (radius, 0)).close()).val())
    return cq.Solid.makeLoft(wires, ruled=True)


def reference(axis):
    trial = cq.Vector(0, 0, 1)
    projected = trial - axis*trial.dot(axis)
    if projected.Length < 32*math.sqrt(2.220446049250313e-16):
        trial = cq.Vector(1, 0, 0)
        projected = trial - axis*trial.dot(axis)
    return unit(projected)


def radial_profile(segments):
    """Ordered upper envelope of the overlapping finite linear-radius parts."""
    lines = [(lo, hi, (rhi-rlo)/(hi-lo), rlo-lo*(rhi-rlo)/(hi-lo))
             for lo, hi, rlo, rhi in segments]
    stations = {value for lo, hi, _, _ in lines for value in (lo, hi)}
    for index, (lo, hi, slope, intercept) in enumerate(lines):
        for other_lo, other_hi, other_slope, other_intercept in lines[index+1:]:
            if slope != other_slope:
                crossing = (other_intercept-intercept)/(slope-other_slope)
                if max(lo, other_lo) < crossing < min(hi, other_hi):
                    stations.add(crossing)
    stations = sorted(stations)
    points = [(stations[0], 0.0)]
    for lo, hi in zip(stations, stations[1:]):
        middle = (lo+hi)/2
        _, _, slope, intercept = max(
            (line for line in lines if line[0] <= middle <= line[1]),
            key=lambda line: line[2]*middle+line[3])
        for point in ((lo, slope*lo+intercept), (hi, slope*hi+intercept)):
            if point != points[-1]:
                points.append(point)
    return points+[(stations[-1], 0.0)]


def axial_profile(origin, axis, segments):
    # Keep the revolve seam away from the shared reference-normal tangencies.
    radial = unit(axis.cross(reference(axis)))
    wire = cq.Wire.makePolygon([origin+axis*station+radial*radius
                               for station, radius in radial_profile(segments)], close=True)
    return cq.Solid.revolve(wire, [], 360, origin, origin+axis)


def collar_radius(scaled_diameter, fitting_diameter):
    if scaled_diameter > fitting_diameter:
        return scaled_diameter*1.1/2
    return (scaled_diameter+fitting_diameter/5)/2*1.05


def curved_neck(radius, points):
    """Sweep the true curved branch on the shared outer/inner centerline."""
    direct = unit(points[-1]-points[0])
    length = (points[-1]-points[0]).Length
    path = cq.Wire.assembleEdges([cq.Edge.makeBezier(points)])
    tangent = unit(points[1]-points[0])
    normal = unit(tangent.cross(direct))
    plane = cq.Plane(origin=points[0], xDir=normal, normal=tangent)
    auxiliary = cq.Wire.assembleEdges([
        cq.Edge.makeBezier([point+normal*length for point in points])
    ])
    # Two semicircles give the same sweep two lateral faces.
    # This keeps shallow curved-neck intersections stable in Boolean fusion.
    return (cq.Workplane(plane).moveTo(radius, 0)
            .threePointArc((0, radius), (-radius, 0))
            .threePointArc((0, -radius), (radius, 0)).close()).sweep(
        path, transition="round", isFrenet=False, clean=False,
        auxSpine=cq.Workplane(obj=auxiliary)).val()


def actual_end(planar_faces, point, outside, inside):
    tolerance = max(1e-9*outside, 1e-12)
    candidates = [face for face, center in planar_faces
                  if (center-point).Length <= tolerance]
    if len(candidates) != 1:
        near = [((center-point).Length, len(face.Wires()))
                for face, center in planar_faces]
        raise ValueError(f"TEE lacks one actual planar annular end at "
                         f"{point.toTuple()}: candidates={len(candidates)}, "
                         f"nearest={sorted(near)[:4]}")
    face = candidates[0]
    radii = sorted(edge.radius() for edge in face.Edges()
                   if edge.geomType() == "CIRCLE")
    if len(face.Wires()) != 2 or len(radii) != 2 or any(
            abs(got-want) > max(tolerance, 1e-7*want)
            for got, want in zip(radii, (inside, outside))):
        raise ValueError(f"TEE actual end radii differ: got={radii}, "
                         f"expected={(inside, outside)}, wires={len(face.Wires())}")
    return face


def actual_end_angle(face, axis, outside):
    """Measure the end plane through a real circular edge using build capabilities."""
    edge = max((edge for edge in face.Edges() if edge.geomType() == "CIRCLE"),
               key=lambda edge: edge.radius())
    center = face.Center()
    radial = edge.startPoint()-center
    if abs(radial.Length-outside) > max(1e-9*outside, 1e-12):
        raise ValueError("TEE actual end circle is not centered on its face")
    radial = unit(radial)
    tangent = unit(axis.cross(radial))
    probe = center+tangent*outside
    tangent_error = edge.distance(cq.Vertex.makeVertex(*probe.toTuple()))/outside
    sine_error = math.hypot(abs(radial.dot(axis)), tangent_error)
    return math.asin(min(1.0, sine_error))


def check_actual_end_outward(shape, face, axis, outside, inside):
    """The annular wall must be material inward and empty outward."""
    edge = max((edge for edge in face.Edges() if edge.geomType() == "CIRCLE"),
               key=lambda edge: edge.radius())
    radial = unit(edge.startPoint()-face.Center())
    wall = face.Center()+radial*(inside+.75*(outside-inside))
    step = (outside-inside)/8
    probe_radius = step/2
    inward = cq.Solid.makeSphere(probe_radius, wall-axis*step)
    outward = cq.Solid.makeSphere(probe_radius, wall+axis*step)
    probe_volume = inward.Volume()
    inward_material = shape.intersect(inward).Volume()
    outward_material = shape.intersect(outward).Volume()
    if inward_material < .9*probe_volume or outward_material > 1e-4*probe_volume:
        raise ValueError("TEE actual end material does not follow outward physical axis: "
                         f"inward_m3={inward_material}, outward_m3={outward_material}")


def build(ctx):
    state = prepare(ctx)
    a = state['a']
    approximation_budget = state['approximation_budget']
    if 'arc_center' in state:
        arc_center = cq.Vector(*state['arc_center'])
    axes = {k:cq.Vector(*v) for k,v in state['axes'].items()}
    b = state['b']
    bend = state['bend']
    branch = state['branch']
    branch_offset = cq.Vector(*state['branch_offset'])
    branch_points = [cq.Vector(*v) for v in state['branch_points']]
    branch_straight = state['branch_straight']
    center = cq.Vector(*state['center'])
    delta = state['delta']
    diameters = state['diameters']
    dv = state['dv']
    ea = state['ea']
    eb = state['eb']
    ebranch = state['ebranch']
    if 'flow' in state:
        flow = cq.Vector(*state['flow'])
    fmain = state['fmain']
    fmax = state['fmax']
    key = state['key']
    main_inner_radius = state['main_inner_radius']
    main_outer_radius = state['main_outer_radius']
    main_transition_radius = state['main_transition_radius']
    if 'middle' in state:
        middle = cq.Vector(*state['middle'])
    mode = state['mode']
    neck = state['neck']
    if 'normal' in state:
        normal = cq.Vector(*state['normal'])
    if 'outer_parts' in state:
        outer_parts = state['outer_parts']
    physical_axes = {k:cq.Vector(*v) for k,v in state['physical_axes'].items()}
    physical_ports = {k:cq.Vector(*v) for k,v in state['physical_ports'].items()}
    ports = {k:cq.Vector(*v) for k,v in state['ports'].items()}
    ta = cq.Vector(*state['ta'])
    tb = cq.Vector(*state['tb'])
    ub = cq.Vector(*state['ub'])
    uv = cq.Vector(*state['uv'])
    if 'void_parts' in state:
        void_parts = state['void_parts']
    if mode == "curved":
        main_sagitta = (middle-(ta+tb)*.5).Length
        simple_main = (branch_straight and delta <= math.radians(1) and
                       main_sagitta+branch_offset.Length <= .05*main_inner_radius)
        if simple_main:
            outer_main = ruled_main(main_outer_radius, ta, flow, tb, ub, normal)
            inner_main = ruled_main(main_inner_radius, ta, flow, tb, ub, normal)
        else:
            outer_main = cq.Solid.revolve(split_main_circle(main_outer_radius, ta, flow), [],
                                         math.degrees(delta), arc_center, arc_center+normal)
            inner_main = cq.Solid.revolve(split_main_circle(main_inner_radius, ta, flow), [],
                                         math.degrees(delta), arc_center, arc_center+normal)
        outer_parts, void_parts = [outer_main], [inner_main]

    outer_main_profile = [(-bend, bend, main_outer_radius, main_outer_radius)]
    inner_main_profile = [(-bend, bend, main_inner_radius, main_inner_radius)]
    for key, allowance in ((a, ea), (b, eb)):
        direction, diameter = physical_axes[key], diameters[key]
        scaled = 1.1*diameter
        thickness = (scaled+fmain)*.05
        terminal = bend+allowance
        collar_start, collar_end = terminal-1.1*thickness, terminal-.1*thickness
        collar = collar_radius(scaled, fmain)
        outer = [(.9*bend, bend, main_outer_radius, main_outer_radius),
                 (bend, collar_end, main_transition_radius, scaled/2),
                 (collar_start, collar_end, collar, collar),
                 (collar_end, terminal, scaled/2, diameter/2)]
        inner = [(.9*bend, bend, main_inner_radius, main_inner_radius),
                 (bend, collar_end, main_inner_radius, scaled*.43),
                 (collar_end, terminal, scaled*.43, diameter*.43),
                 (terminal, terminal+.001*diameter, diameter*.43, diameter*.43)]
        if mode in ("straight", "near_zero"):
            for target, segments in ((outer_main_profile, outer), (inner_main_profile, inner)):
                target.extend([(-hi, -lo, rhi, rlo) for lo, hi, rlo, rhi in segments]
                              if key == a else segments)
        else:
            outer_parts.append(axial_profile(center, direction, outer))
            void_parts.append(axial_profile(center, direction, inner))
    if mode in ("straight", "near_zero"):
        outer_parts.append(axial_profile(center, physical_axes[b], outer_main_profile))
        void_parts.append(axial_profile(center, physical_axes[b], inner_main_profile))

    scaled = 1.1*dv
    thickness = (scaled+fmax)*.05
    terminal = neck+ebranch
    collar_start, collar_end = terminal-1.1*thickness, terminal-.1*thickness
    collar = collar_radius(scaled, fmax)
    outer = [(neck, collar_end, fmax/2, scaled/2),
             (collar_start, collar_end, collar, collar),
             (collar_end, terminal, scaled/2, dv/2)]
    inner = [(neck, collar_end, fmax*.43, scaled*.43),
             (collar_end, terminal, scaled*.43, dv*.43),
             (terminal, terminal+.001*dv, dv*.43, dv*.43)]
    if branch_straight:
        start = (middle-center).dot(uv)
        outer.insert(0, (start, neck, .9*main_outer_radius if mode == "curved" else fmax/2, fmax/2))
        inner.insert(0, (start, neck, .9*main_inner_radius if mode == "curved" else fmax*.43, fmax*.43))
    else:
        outer_parts.append(curved_neck(fmax/2, branch_points))
        void_parts.append(curved_neck(fmax*.43, branch_points))
    outer_parts.append(axial_profile(center, uv, outer))
    void_parts.append(axial_profile(center, uv, inner))
    # The final unified cut removes branch material from the full bent main bore.
    # Cutting the main before fusing the branch leaves a blocked arc off midpoint.
    assembled = outer_parts[0]
    for part in outer_parts[1:]:
        assembled = assembled.fuse(part)
    passages = void_parts[0]
    for part in void_parts[1:]:
        passages = passages.fuse(part)
    shape = assembled.cut(passages)
    valid, solids, shells = shape.isValid(), len(shape.Solids()), len(shape.Shells())
    if not valid or solids != 1 or shells != 1:
        raise ValueError("TEE did not produce one valid closed hollow solid: "
                         f"result_valid={valid}, result_solid={solids}, result_shell={shells}")
    planar_faces = [(face, face.Center()) for face in shape.Faces()
                    if face.geomType() == "PLANE"]
    for key in "abc":
        face = actual_end(planar_faces, physical_ports[key], .5*diameters[key], .43*diameters[key])
        if mode == "near_zero":
            normal_error = actual_end_angle(face, axes[key], .5*diameters[key])
            physical_error = actual_end_angle(face, physical_axes[key], .5*diameters[key])
            center_error = (face.Center()-ports[key]).Length
            limit = 1e-8 if key == branch else approximation_budget
            if center_error > limit or physical_error > 1e-6 or normal_error > math.radians(.01):
                raise ValueError("TEE near-zero measured end differs from logical source port: "
                                 f"side={key}, center_m={center_error}, "
                                 f"physical_normal_rad={physical_error}, source_normal_rad={normal_error}")
            check_actual_end_outward(shape, face, physical_axes[key],
                                     .5*diameters[key], .43*diameters[key])
    return shape
