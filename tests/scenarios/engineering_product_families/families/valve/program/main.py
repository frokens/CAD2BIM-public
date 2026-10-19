"""VALVE assembly placement over the shared finite two-port solver."""

import math

from .local_geometry_frame import point_in_parent, direction_in_parent

from .shared_solver import realize as shared_realize, standard_model as shared_standard_model


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _add(a, b):
    return [x + y for x, y in zip(a, b)]


def _mul(a, scale):
    return [x * scale for x in a]


def _horizontal(direction):
    length = math.hypot(direction[0], direction[1])
    if length <= 1e-10:
        return None
    return [direction[0] / length, direction[1] / length]


def _upper_transform(parameters, lower_placement):
    ports = {key: dict(port, origin_m=point_in_parent(port["origin_m"], lower_placement),
                      axis=direction_in_parent(port["axis"], lower_placement))
             for key, port in parameters["ports"].items()}
    shape = dict(parameters["shape"])
    if shape["mode"] == "curved":
        shape["local_center_m"] = point_in_parent(shape["local_center_m"], lower_placement)
    outward_a = ports["side_a"]["axis"]
    outward_b = ports["side_b"]["axis"]
    if shape["mode"] == "straight":
        port_a = ports["side_a"]["origin_m"]
        port_b = ports["side_b"]["origin_m"]
        anchor = _mul(_add(port_a, port_b), 0.5)
        tangent = [-item for item in outward_a]
    else:
        center, turn = shape["local_center_m"], shape["turn_radian"]
        flow = [-item for item in outward_a]
        # The second outward ray's component normal to the incoming tangent
        # fixes the actual 3D bend plane. No XY projection enters the anchor.
        component = _add(outward_b, _mul(flow, -_dot(outward_b, flow)))
        magnitude = math.sqrt(_dot(component, component))
        if magnitude <= 1e-10:
            return None
        radial = _mul(component, 1.0 / magnitude)
        half = turn / 2.0
        radius = shape["arc_radius_m"]
        tangent_a = _add(center, _mul(outward_a, 0.9 * shape["bend_scale_m"]))
        anchor = _add(_add(tangent_a, _mul(flow, radius * math.sin(half))),
                      _mul(radial, radius * (1.0 - math.cos(half))))
        tangent = _add(_mul(flow, math.cos(half)), _mul(radial, math.sin(half)))

    horizontal = _horizontal(tangent)
    if horizontal is None:
        # A vertical midpoint tangent has no yaw. Only declared source
        # directions may supply a stable horizontal facing direction.
        horizontal = (_horizontal([-item for item in outward_a])
                      or _horizontal(outward_b))
    if horizontal is None:
        return None
    x, y = horizontal
    return {"translation_m": anchor,
            "rotation": [[x, -y, 0.0], [y, x, 0.0], [0.0, 0.0, 1.0]]}


def _as_valve(proposal):
    if proposal["status"] != "SOLVED":
        return proposal
    parameters = proposal["internal_parameters"]
    middle_od = (parameters["shape"]["body_outer_diameter_m"]
                 if parameters["shape"]["mode"] == "curved" else
                 max(port["outer_diameter_m"] for port in parameters["ports"].values()))
    upper_scale = middle_od / 0.33
    if not math.isfinite(upper_scale) or upper_scale <= 0.0:
        return {"status": "INFEASIBLE", "violations": [{
            "violation_key": "upper_uniform_scale",
            "message": "Upper scale from the lower middle OD must be positive and finite"}]}
    upper_pose = _upper_transform(proposal["internal_parameters"], proposal["fragments"][0]["local_transform"])
    if upper_pose is None:
        return {"status": "AMBIGUOUS", "ambiguities": [{
            "ambiguity_key": "upper_yaw",
            "message": "Upper yaw has no stable horizontal source direction"}]}
    lower = proposal["fragments"][0]
    lower["fragment_key"] = "valve_lower"
    proposal["internal_parameters"]["upper_uniform_scale"] = upper_scale
    proposal["components"] = [
        {"component_key": "valve"},
        {"component_key": "flow_body"},
    ]
    proposal["fragments"] = [
        {"fragment_key": "valve_upper", "binding_key": "valve_upper_geometry",
         "uniform_scale": upper_scale, "local_transform": upper_pose},
        lower,
    ]
    return proposal


def realize(ctx):
    return _as_valve(shared_realize(ctx))


def standard_model(ctx):
    return _as_valve(shared_standard_model(ctx, diameter_a=0.3, diameter_b=0.3))
