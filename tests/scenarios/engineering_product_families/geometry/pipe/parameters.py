import math

def vector(values):
    return tuple(values)

def add(a, b):
    return tuple((a[i] + b[i] for i in range(3)))

def sub(a, b):
    return tuple((a[i] - b[i] for i in range(3)))

def scale(a, value):
    return tuple((x * value for x in a))

def dot(a, b):
    return sum((a[i] * b[i] for i in range(3)))

def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])

def norm(a):
    return math.hypot(*a)

def unit(value):
    length = norm(value)
    if not math.isfinite(length) or length <= 0:
        raise ValueError('direction must be finite and nonzero')
    return scale(value, 1 / length)

def reference(axis):
    preferred = (0, 0, 1)
    projected = sub(preferred, scale(axis, dot(axis, preferred)))
    if norm(projected) <= 32 * math.sqrt(2.220446049250313e-16):
        preferred = (1, 0, 0)
        projected = sub(preferred, scale(axis, dot(axis, preferred)))
    return unit(projected)

def _binary_fraction(value):
    if not math.isfinite(value):
        return None
    numerator = value
    exponent = 0
    while numerator != math.floor(numerator):
        numerator *= 2
        exponent += 1
        if exponent > 1074 or not math.isfinite(numerator):
            return None
    return int(numerator), exponent

def _fraction_difference(positive, negative):
    exponent = max(positive[1], negative[1])
    return (positive[0] * 2 ** (exponent - positive[1])
            - negative[0] * 2 ** (exponent - negative[1]), exponent)

def _product_equal(a, b, c, d):
    left, left_exponent = a[0] * b[0], a[1] + b[1]
    right, right_exponent = c[0] * d[0], c[1] + d[1]
    if left_exponent < right_exponent:
        left *= 2 ** (right_exponent - left_exponent)
    else:
        right *= 2 ** (left_exponent - right_exponent)
    return left == right

def _same_forward_line(a, b, c):
    """Prove one source vertex is redundant in binary64-coordinate geometry."""
    coordinates = [[_binary_fraction(point[i]) for i in range(3)] for point in (a, b, c)]
    if any(value is None for point in coordinates for value in point):
        return None
    left = [_fraction_difference(coordinates[1][i], coordinates[0][i]) for i in range(3)]
    right = [_fraction_difference(coordinates[2][i], coordinates[1][i]) for i in range(3)]
    if all(value[0] == 0 for value in left) or all(value[0] == 0 for value in right):
        return None
    for i, j in ((0, 1), (1, 2), (2, 0)):
        if not _product_equal(left[i], right[j], left[j], right[i]):
            return False
    for i in range(3):
        if left[i][0]:
            return (left[i][0] > 0) == (right[i][0] > 0)
    return None

def _roundoff_forward_line(a, b, c):
    """Recognize a zero turn within binary64 coordinate arithmetic noise."""
    left, right, chord = sub(b, a), sub(c, b), sub(c, a)
    length = norm(chord)
    if length <= 0 or dot(left, right) <= 0:
        return False
    # The cap is below OCCT's existing linear precision; this is not a model
    # simplification tolerance. Large coordinates cannot enlarge it unboundedly.
    bound = min(1e-10, 32 * max(math.ulp(value) for point in (a, b, c) for value in point))
    return norm(cross(left, chord)) / length <= bound

def _sweep_topology(coordinates):
    """Retained source indices; input/receipt keeps the full ordered path."""
    indices = [0]
    for index in range(1, len(coordinates) - 1):
        same = _same_forward_line(
            coordinates[indices[-1]], coordinates[index], coordinates[index + 1]
        )
        if same is not True:
            # Recheck every omitted source point against the proposed final
            # chord, so roundoff bounds do not accumulate over a long chain.
            same = all(_roundoff_forward_line(coordinates[indices[-1]], coordinates[prior], coordinates[index + 1])
                       for prior in range(indices[-1] + 1, index + 1))
        if same is not True:
            indices.append(index)
    indices.append(len(coordinates) - 1)
    return indices

def prepare(ctx):
    coordinates = ctx.param('local_points_m')
    outer_diameter = ctx.param('outer_diameter_m')
    references = ctx.param('endpoint_reference_directions')
    section = ctx.param('section')
    thickness_values = section['known_wall_thickness_m']
    if not math.isfinite(outer_diameter) or outer_diameter <= 0:
        raise ValueError('outer diameter must be finite and positive')
    if len(coordinates) < 2:
        raise ValueError('PIPE needs at least two ordered path points')
    if len(thickness_values) > 1:
        raise ValueError('section has more than one wall thickness')
    thickness = thickness_values[0] if thickness_values else None
    if thickness is not None and (not 0 < thickness < outer_diameter / 2):
        raise ValueError('known wall thickness must be between zero and outer radius')
    points = [(*point,) for point in coordinates]
    segments = [sub(right, left) for left, right in zip(points, points[1:])]
    lengths = [norm(segment) for segment in segments]
    if any((not math.isfinite(length) or length <= 0 for length in lengths)):
        raise ValueError('PIPE path has a nonpositive or nonfinite segment')
    tangents = [unit(segment) for segment in segments]
    if len(references) != 2 or any(len(ref) != 3 or abs(norm(ref)-1) > 1e-8
            or abs(dot(ref, axis)) > 1e-8 for ref, axis in zip(references, (tangents[0], tangents[-1]))):
        raise ValueError('PIPE endpoint references must be unit and perpendicular to their axes')
    sweep_indices = _sweep_topology(coordinates)
    chain = [points[index] for index in sweep_indices]
    chain_directions = [unit(sub(b, a)) for a, b in zip(chain, chain[1:])]
    miter_normals = [chain_directions[0]]
    miter_normals.extend(unit(add(a, b)) for a, b in zip(chain_directions, chain_directions[1:]))
    miter_normals.append(chain_directions[-1])
    for index, direction in enumerate(chain_directions):
        if dot(miter_normals[index], direction) <= 0 or dot(miter_normals[index + 1], direction) <= 0:
            raise ValueError('PIPE miter normals do not face through the segment')
    state = {'outer_diameter': outer_diameter, 'points': points, 'tangents': tangents, 'thickness': thickness,
             'chain': chain, 'chain_directions': chain_directions, 'miter_normals': miter_normals, 'references': references}
    return state
