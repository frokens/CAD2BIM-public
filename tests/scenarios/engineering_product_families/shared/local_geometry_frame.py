"""Resource-only rigid frames: shape dimensions and declared port roll stay intact."""

import math


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _unit(vector):
    length = math.hypot(*vector)
    if not math.isfinite(length) or length <= 0:
        raise ValueError("Local geometry frame requires finite nonzero axes")
    return [value / length for value in vector]


def frame(origin, x_axis, z_axis):
    """Return local-to-parent placement; rotation columns are the local axes."""
    x = _unit(x_axis)
    z = _unit([value - _dot(z_axis, x) * axis for value, axis in zip(z_axis, x)])
    y = _unit(_cross(z, x))
    z = _cross(x, y)
    return {"translation_m": list(origin),
            "rotation": [[x[index], y[index], z[index]] for index in range(3)]}


def direction_in_frame(vector, placement):
    rotation = placement["rotation"]
    return [sum(rotation[row][column] * vector[row] for row in range(3))
            for column in range(3)]


def point_in_frame(point, placement):
    return direction_in_frame([value - offset for value, offset in
                               zip(point, placement["translation_m"])], placement)


def direction_in_parent(vector, placement):
    return [_dot(row, vector) for row in placement["rotation"]]


def point_in_parent(point, placement):
    return [value + offset for value, offset in
            zip(direction_in_parent(point, placement), placement["translation_m"])]


def compose(parent, local):
    columns = [direction_in_parent([row[column] for row in local["rotation"]], parent)
               for column in range(3)]
    return {"translation_m": point_in_parent(local["translation_m"], parent),
            "rotation": [[columns[column][row] for column in range(3)] for row in range(3)]}


def ports_in_frame(ports, placement):
    return {key: dict(port, origin_m=point_in_frame(port["origin_m"], placement),
                      axis=direction_in_frame(port["axis"], placement),
                      reference_direction=direction_in_frame(port["reference_direction"], placement))
            for key, port in ports.items()}


def localize_two_port(parameters):
    """Factor pose from an already solved two-port body, without solving it again."""
    ports, shape = parameters["ports"], parameters["shape"]
    origin = (shape["local_center_m"] if shape["mode"] == "curved" else
              [(a + b) * 0.5 for a, b in zip(ports["side_a"]["origin_m"],
                                            ports["side_b"]["origin_m"])])
    placement = frame(origin, [-value for value in ports["side_a"]["axis"]], shape["frame_x"])
    local_shape = dict(shape, frame_x=direction_in_frame(shape["frame_x"], placement))
    if shape["mode"] == "curved":
        local_shape["local_center_m"] = point_in_frame(shape["local_center_m"], placement)
    return {"ports": ports_in_frame(ports, placement), "shape": local_shape}, placement
