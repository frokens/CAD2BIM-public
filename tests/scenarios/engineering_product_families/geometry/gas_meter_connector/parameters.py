import math

def prepare(ctx):
    od_m = ctx.param('od_m')
    length_m = ctx.param('length_m')
    try:
        finite_input = all((math.isfinite(value) for value in (od_m, length_m)))
    except (TypeError, ValueError):
        finite_input = False
    if not finite_input:
        raise ValueError('CONNECTOR_NONFINITE')
    if od_m <= 2e-06:
        raise ValueError('CONNECTOR_BREP_SECTION')
    od = od_m * 1000
    length = length_m * 1000
    if not all((math.isfinite(value) for value in (od, length))):
        raise ValueError('CONNECTOR_NUMERIC_CAPACITY')
    radius = od / 2
    collar_length = min(18, max(8, od * 0.22))
    if length < 2 * collar_length + 9.2 + 1.2 - 1e-09:
        raise ValueError('CONNECTOR_TERMINAL_MARGIN')
    state = {'collar_length': collar_length, 'length': length, 'length_m': length_m, 'od_m': od_m, 'radius': radius}
    return state
