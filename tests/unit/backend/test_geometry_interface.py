"""Model annotations are bounded local values, not material-contact claims."""

import math

import pytest
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.domain.geometry_frame import (
    RigidTransform3D,
    rigid_transform_from_axis_reference,
)
from CAD2BIM_backend.domain.geometry_interface import (
    CircularInterfaceProfile,
    GeometryModelInterface,
    GeometryModelInterfaces,
    InterfaceExternalCondition,
    InterfaceFrameRelation,
    PolygonInterfaceProfile,
    RectangleInterfaceProfile,
    evaluate_interface_frame_relation,
    evaluate_interface_external_condition,
    scale_geometry_interfaces,
)
from CAD2BIM_backend.domain.product_realization import RigidTransform3D as ProductFrame


def test_frame_is_the_existing_product_value_and_preserves_local_basis():
    assert ProductFrame is RigidTransform3D
    frame = rigid_transform_from_axis_reference((2, 3, 4), (0, 1, 0), (0, 0, 1))
    assert frame.translation_m == (2, 3, 4)
    assert frame.rotation == ((0, 1, 0), (0, 0, 1), (1, 0, 0))


def test_python_frame_input_rejects_extra_coordinates_instead_of_dropping_them():
    with pytest.raises(ValueError, match="exactly three"):
        rigid_transform_from_axis_reference((0, 0, 0), (0, 0, 1, 0), (1, 0, 0, 0))


@pytest.mark.parametrize(
    "axis, reference",
    [((0, 0, 0), (1, 0, 0)), ((0, 0, 1), (0, 0, 1)), ((0, 0, math.inf), (1, 0, 0))],
)
def test_invalid_frames_are_rejected_without_orthogonalizing(axis, reference):
    with pytest.raises(ValueError):
        rigid_transform_from_axis_reference((0, 0, 0), axis, reference)


@pytest.mark.parametrize(
    "profile",
    [
        {"kind": "circular", "diameter_m": -1},
        {"kind": "circular", "diameter_m": math.nan},
        {"kind": "rectangle", "width_m": 1, "height_m": math.inf},
        {"kind": "circular", "diameter_m": 1, "unknown": True},
    ],
)
def test_profile_is_closed_and_finite(profile):
    with pytest.raises(ValidationError):
        GeometryModelInterface(interface_key="end", local_frame=RigidTransform3D(), profile=profile)


def test_polygon_preserves_authored_origin_and_winding():
    points = ((3, 4), (5, 4), (5, 6), (4, 5), (3, 6))
    assert PolygonInterfaceProfile(points_m=points).points_m == points
    assert PolygonInterfaceProfile(points_m=points[::-1]).points_m == points[::-1]


@pytest.mark.parametrize(
    "points",
    [
        ((0, 0), (1, 0), (2, 0)),
        ((0, 0), (2, 2), (0, 2), (2, 0)),
        ((0, 0), (3, 0), (1, 0), (1, 2), (0, 2)),
        ((0, 0), (2, 0), (2, 2), (0, 2), (1, 0)),
        ((0, 0), (1, 0), (0, 1), (0, 0)),
    ],
)
def test_polygon_rejects_degenerate_crossing_touching_and_overlapping_edges(points):
    with pytest.raises(ValidationError):
        PolygonInterfaceProfile(points_m=points)


def test_interface_collection_bounds_uniqueness_and_byte_budget():
    adapter = TypeAdapter(GeometryModelInterfaces)
    item = GeometryModelInterface(interface_key="end", local_frame=RigidTransform3D())
    assert adapter.validate_python(()) == ()
    with pytest.raises(ValidationError, match="unique"):
        adapter.validate_python((item, item))
    with pytest.raises(ValidationError):
        adapter.validate_python(
            tuple(item.model_copy(update={"interface_key": f"end_{n}"}) for n in range(65))
        )
    points = tuple((math.cos(n * math.tau / 64), math.sin(n * math.tau / 64)) for n in range(64))
    large = item.model_copy(update={"profile": PolygonInterfaceProfile(points_m=points)})
    with pytest.raises(ValidationError, match="byte budget"):
        adapter.validate_python(
            tuple(large.model_copy(update={"interface_key": f"end_{n}"}) for n in range(16))
        )


def test_frame_relation_is_closed_directional_and_compactly_serialized():
    relation = InterfaceFrameRelation(
        direction_relation="same",
        orientation_requirement="axis_only",
    )
    assert relation.model_dump(mode="json") == {
        "direction_relation": "same",
        "orientation_requirement": "axis_only",
    }
    with pytest.raises(ValidationError, match="unused clocking"):
        InterfaceFrameRelation(
            direction_relation="same",
            orientation_requirement="axis_only",
            expected_b_from_a=RigidTransform3D(rotation=((0, -1, 0), (1, 0, 0), (0, 0, 1))),
        )
    clocked = InterfaceFrameRelation(
        direction_relation="same",
        orientation_requirement="full_frame",
        expected_b_from_a=RigidTransform3D(rotation=((0, -1, 0), (1, 0, 0), (0, 0, 1))),
    )
    assert clocked.expected_b_from_a is not None
    with pytest.raises(ValidationError, match="contradicts"):
        InterfaceFrameRelation(
            direction_relation="opposed",
            orientation_requirement="full_frame",
            expected_b_from_a=RigidTransform3D(),
        )


def test_frame_relation_evaluation_separates_position_axis_and_clocking():
    left = RigidTransform3D(translation_m=(10, 20, 30))
    clocked_right = RigidTransform3D(
        translation_m=(10.003, 20.004, 30.012),
        rotation=((0, -1, 0), (1, 0, 0), (0, 0, 1)),
    )
    axis_only = evaluate_interface_frame_relation(
        left,
        clocked_right,
        InterfaceFrameRelation(direction_relation="same", orientation_requirement="axis_only"),
        position_tolerance_m=0.02,
        angular_tolerance_rad=1e-6,
    )
    assert axis_only.origin_distance_m == pytest.approx(0.013)
    assert axis_only.axial_offset_m == pytest.approx(0.012)
    assert axis_only.lateral_offset_m == pytest.approx(0.005)
    assert axis_only.direction_residual_rad == pytest.approx(0.0)
    assert axis_only.orientation_residual_rad is None
    assert axis_only.profile_satisfied is None
    assert axis_only.satisfied is True

    full_frame = evaluate_interface_frame_relation(
        left,
        clocked_right.model_copy(update={"translation_m": left.translation_m}),
        InterfaceFrameRelation(direction_relation="same", orientation_requirement="full_frame"),
        position_tolerance_m=1e-6,
        angular_tolerance_rad=0.01,
    )
    assert full_frame.direction_residual_rad == pytest.approx(0.0)
    assert full_frame.orientation_residual_rad == pytest.approx(math.pi / 2)
    assert full_frame.satisfied is False


def test_opposed_and_offset_relations_use_left_times_relative_convention():
    left = rigid_transform_from_axis_reference((4, 5, 6), (0, 1, 0), (0, 0, 1))
    relative = RigidTransform3D(
        translation_m=(2, 3, 4),
        rotation=((1, 0, 0), (0, -1, 0), (0, 0, -1)),
    )
    expected_right = RigidTransform3D(
        translation_m=(7, 9, 8),
        rotation=((0, -1, 0), (0, 0, -1), (1, 0, 0)),
    )
    evaluation = evaluate_interface_frame_relation(
        left,
        expected_right,
        InterfaceFrameRelation(
            direction_relation="opposed",
            orientation_requirement="full_frame",
            expected_b_from_a=relative,
        ),
        position_tolerance_m=1e-9,
        angular_tolerance_rad=1e-9,
    )
    assert evaluation.origin_distance_m == pytest.approx(0.0)
    assert evaluation.direction_residual_rad == pytest.approx(0.0)
    assert evaluation.orientation_residual_rad == pytest.approx(0.0)
    assert evaluation.satisfied is True


def test_profile_equality_is_length_tolerant_and_polygon_order_independent():
    relation = InterfaceFrameRelation(
        direction_relation="same",
        orientation_requirement="axis_only",
        profile_requirement="equal",
    )

    def profile_result(left_profile, right_profile):
        return evaluate_interface_frame_relation(
            RigidTransform3D(),
            RigidTransform3D(),
            relation,
            position_tolerance_m=0,
            angular_tolerance_rad=0,
            left_profile=left_profile,
            right_profile=right_profile,
        )

    assert (
        profile_result(
            CircularInterfaceProfile(diameter_m=0.2),
            CircularInterfaceProfile(diameter_m=0.2 + 1e-10),
        ).profile_satisfied
        is True
    )
    assert (
        profile_result(
            RectangleInterfaceProfile(width_m=2, height_m=3),
            RectangleInterfaceProfile(width_m=2, height_m=4),
        ).satisfied
        is False
    )
    polygon = PolygonInterfaceProfile(points_m=((0, 0), (2, 0), (2, 1), (0, 1)))
    reversed_shifted = PolygonInterfaceProfile(points_m=((2, 1), (2, 0), (0, 0), (0, 1)))
    assert profile_result(polygon, reversed_shifted).satisfied is True
    assert profile_result(polygon, None).profile_satisfied is False


def test_interface_scaling_changes_lengths_only_and_revalidates_profiles():
    rotation = ((0, -1, 0), (1, 0, 0), (0, 0, 1))
    interfaces = (
        GeometryModelInterface(
            interface_key="round",
            local_frame=RigidTransform3D(translation_m=(1, -2, 3), rotation=rotation),
            profile=CircularInterfaceProfile(diameter_m=0.2),
        ),
        GeometryModelInterface(
            interface_key="rect",
            local_frame=RigidTransform3D(),
            profile=RectangleInterfaceProfile(width_m=2, height_m=3),
        ),
        GeometryModelInterface(
            interface_key="poly",
            local_frame=RigidTransform3D(),
            profile=PolygonInterfaceProfile(points_m=((0, 0), (2, 0), (0, 1))),
        ),
    )
    scaled = scale_geometry_interfaces(interfaces, 2.5)
    assert scaled[0].local_frame.translation_m == (2.5, -5.0, 7.5)
    assert scaled[0].local_frame.rotation == rotation
    assert scaled[0].profile.diameter_m == pytest.approx(0.5)
    assert scaled[1].profile.width_m == pytest.approx(5)
    assert scaled[1].profile.height_m == pytest.approx(7.5)
    assert scaled[2].profile.points_m == ((0, 0), (5, 0), (0, 2.5))
    assert interfaces[0].local_frame.translation_m == (1, -2, 3)

    for invalid in (0, -1, math.inf, math.nan):
        with pytest.raises(ValueError, match="finite and positive"):
            scale_geometry_interfaces(interfaces, invalid)
    with pytest.raises(ValidationError):
        scale_geometry_interfaces(
            (
                GeometryModelInterface(
                    interface_key="too_large",
                    local_frame=RigidTransform3D(),
                    profile=CircularInterfaceProfile(diameter_m=600),
                ),
            ),
            2,
        )


@pytest.mark.parametrize(
    ("position_tolerance_m", "angular_tolerance_rad"),
    ((-1, 0), (0, -1), (math.nan, 0), (0, math.inf), (0, math.pi + 0.01)),
)
def test_frame_evaluation_rejects_invalid_tolerances(position_tolerance_m, angular_tolerance_rad):
    with pytest.raises(ValueError):
        evaluate_interface_frame_relation(
            RigidTransform3D(),
            RigidTransform3D(),
            InterfaceFrameRelation(direction_relation="same", orientation_requirement="axis_only"),
            position_tolerance_m=position_tolerance_m,
            angular_tolerance_rad=angular_tolerance_rad,
        )


def test_frame_evaluation_rejects_finite_offsets_that_overflow_distance():
    with pytest.raises(ValueError, match="distance exceeds finite arithmetic"):
        evaluate_interface_frame_relation(
            RigidTransform3D(translation_m=(1e200, 0, 0)),
            RigidTransform3D(translation_m=(-1e200, 0, 0)),
            InterfaceFrameRelation(direction_relation="same", orientation_requirement="axis_only"),
            position_tolerance_m=1,
            angular_tolerance_rad=1,
        )


def _external_condition(*, position=None, direction=None, roll=None, profile=None, **tolerances):
    return InterfaceExternalCondition.model_validate(
        {
            "position": {"position_kind": "free"} if position is None else position,
            "direction": {"direction_kind": "none"} if direction is None else direction,
            "roll": {"roll_kind": "none"} if roll is None else roll,
            "profile": {"profile_kind": "none"} if profile is None else profile,
            "position_tolerance_m": 1 / 1024,
            "angular_tolerance_rad": 1e-6,
            **tolerances,
        }
    )


def test_external_point_measures_world_distance_with_a_tilted_actual_frame():
    # A 3-4-12 displacement has norm 13, regardless of the interface's tilted axes.
    # Binary fractions make the acceptance boundary exact rather than a rounding test.
    actual = RigidTransform3D(
        translation_m=(10 + 3 / 512, -20 + 4 / 512, 30 + 12 / 512),
        rotation=((0.8, 0, 0.6), (0, 1, 0), (-0.6, 0, 0.8)),
    )
    snapshot = actual.model_dump()
    condition = _external_condition(
        position={"position_kind": "point", "point_m": (10, -20, 30)},
        position_tolerance_m=13 / 512,
    )
    measured = evaluate_interface_external_condition(actual, condition)
    assert measured.position_residual_m == 13 / 512
    assert measured.satisfied is True
    assert measured.line_projection_m is None
    assert measured.line_extent_residual_m is None
    assert measured.direction_residual_rad is None
    assert measured.roll_residual_rad is None
    assert measured.profile_satisfied is None
    assert actual.model_dump() == snapshot
    outside = _external_condition(
        position=condition.position,
        position_tolerance_m=12 / 512,
    )
    assert evaluate_interface_external_condition(actual, outside).satisfied is False


@pytest.mark.parametrize("direction_scale", (1e-12, 1, 1000))
def test_external_tilted_line_normalizes_direction_and_separates_axial_lateral_distance(
    direction_scale,
):
    # Orthonormal basis u=(3/5,0,4/5), v=(4/5,0,-3/5), w=(0,1,0).
    # p=a+2*u+(3/512)*v+(4/512)*w: axial station 2 m, lateral distance 5/512 m.
    actual = RigidTransform3D(
        translation_m=(10 + 1.2 + 0.8 * 3 / 512, -20 + 4 / 512, 30 + 1.6 - 0.6 * 3 / 512),
        rotation=((0.8, 0, 0.6), (0, 1, 0), (-0.6, 0, 0.8)),
    )
    condition = _external_condition(
        position={
            "position_kind": "line",
            "point_m": (10, -20, 30),
            "direction": (3 * direction_scale, 0, 4 * direction_scale),
            "extent": {"extent_kind": "bounded", "minimum_m": 1.5, "maximum_m": 2.5},
        },
        position_tolerance_m=0.01,
    )
    measured = evaluate_interface_external_condition(actual, condition)
    assert condition.position.direction == pytest.approx((0.6, 0, 0.8))
    assert measured.line_projection_m == pytest.approx(2, abs=1e-13)
    assert measured.position_residual_m == pytest.approx(5 / 512, abs=1e-13)
    assert measured.line_extent_residual_m == 0
    assert measured.satisfied is True


@pytest.mark.parametrize(
    "line_origin,direction,minimum,maximum,station",
    [
        ((10, -20, 30), (-3, 0, -4), -2.5, -1.5, -2),
        ((16, -20, 38), (3, 0, 4), -8.5, -7.5, -8),
    ],
)
def test_external_line_representation_changes_preserve_the_same_physical_segment(
    line_origin, direction, minimum, maximum, station,
):
    # Reversing the line direction negates stations; shifting its origin 10 m
    # along the line subtracts 10 m. Both descriptions contain the same p=a+2*u.
    actual = RigidTransform3D(translation_m=(11.2, -20, 31.6))
    measured = evaluate_interface_external_condition(
        actual,
        _external_condition(
            position={
                "position_kind": "line",
                "point_m": line_origin,
                "direction": direction,
                "extent": {"extent_kind": "bounded", "minimum_m": minimum, "maximum_m": maximum},
            }
        ),
    )
    assert measured.line_projection_m == pytest.approx(station, abs=1e-13)
    assert measured.position_residual_m == pytest.approx(0, abs=1e-13)
    assert measured.line_extent_residual_m == 0
    assert measured.satisfied is True


@pytest.mark.parametrize(
    "lateral,station,extent_error,satisfied",
    [
        (0, -0.5, 0, True),
        (0, 0.75, 0, True),
        (0, -0.5 - 1 / 1024, 1 / 1024, True),
        (0, 0.75 + 1 / 1024, 1 / 1024, True),
        (0, -0.5 - 2 / 1024, 2 / 1024, False),
        (0, 0.75 + 2 / 1024, 2 / 1024, False),
        (2 / 1024, 0, 0, False),
        (1 / 1024, 0.75 + 1 / 1024, 1 / 1024, True),
    ],
)
def test_external_bounded_line_calibrates_independent_extent_and_lateral_boundaries(
    lateral, station, extent_error, satisfied,
):
    # At the last case both independent residuals equal tolerance; distance to
    # the finite segment would be sqrt(2)*tolerance and is a different criterion.
    measured = evaluate_interface_external_condition(
        RigidTransform3D(translation_m=(7 + lateral, -11, station)),
        _external_condition(
            position={
                "position_kind": "line",
                "point_m": (7, -11, 0),
                "direction": (0, 0, 20),
                "extent": {"extent_kind": "bounded", "minimum_m": -0.5, "maximum_m": 0.75},
            }
        ),
    )
    assert measured.position_residual_m == lateral
    assert measured.line_projection_m == station
    assert measured.line_extent_residual_m == extent_error
    assert measured.satisfied is satisfied


def test_external_unbounded_line_has_no_extent_error_at_distant_axial_station():
    measured = evaluate_interface_external_condition(
        RigidTransform3D(translation_m=(7, -11, 100)),
        _external_condition(
            position={
                "position_kind": "line",
                "point_m": (7, -11, 0),
                "direction": (0, 0, 5),
                "extent": {"extent_kind": "unbounded"},
            }
        ),
    )
    assert measured.line_projection_m == 100
    assert measured.position_residual_m == 0
    assert measured.line_extent_residual_m is None
    assert measured.satisfied is True


@pytest.mark.parametrize(
    "direction,expected_angle,satisfied",
    [
        ({"direction_kind": "same", "axis": (6, 0, 8)}, 0, True),
        ({"direction_kind": "opposed", "axis": (-6, 0, -8)}, 0, True),
        ({"direction_kind": "same", "axis": (-6, 0, -8)}, math.pi, False),
        ({"direction_kind": "opposed", "axis": (6, 0, 8)}, math.pi, False),
        ({"direction_kind": "same", "axis": (0, 10, 0)}, math.pi / 2, False),
    ],
)
def test_external_free_position_still_checks_the_tilted_actual_outward_axis(
    direction, expected_angle, satisfied,
):
    actual = RigidTransform3D(
        translation_m=(123, -456, 789),
        rotation=((0.8, 0, 0.6), (0, 1, 0), (-0.6, 0, 0.8)),
    )
    measured = evaluate_interface_external_condition(actual, _external_condition(direction=direction))
    assert measured.position_residual_m is None
    assert measured.direction_residual_rad == pytest.approx(expected_angle, abs=1e-12)
    assert measured.roll_residual_rad is None
    assert measured.profile_satisfied is None
    assert measured.satisfied is satisfied


@pytest.mark.parametrize(
    "rotation,direction_angle,roll_angle",
    [
        (((0.8, 0, 0.6), (0, 1, 0), (-0.6, 0, 0.8)), 0, 0),
        (((0, -0.8, 0.6), (1, 0, 0), (0, 0.6, 0.8)), 0, math.pi / 2),
        (((-0.8, 0, 0.6), (0, -1, 0), (0.6, 0, 0.8)), 0, math.pi),
        (((0.8, 0.6, 0), (0, 0, -1), (-0.6, 0.8, 0)), math.pi / 2, 0),
    ],
)
def test_external_roll_and_direction_have_separate_correct_and_wrong_witnesses(
    rotation, direction_angle, roll_angle,
):
    # The middle witnesses spin X/Y around unchanged Z. The last rotates Y/Z
    # around unchanged X: direction fails while the reference itself still matches.
    measured = evaluate_interface_external_condition(
        RigidTransform3D(translation_m=(3, -5, 7), rotation=rotation),
        _external_condition(
            direction={"direction_kind": "same", "axis": (3, 0, 4)},
            roll={"roll_kind": "reference", "reference_direction": (4, 0, -3)},
        ),
    )
    assert measured.direction_residual_rad == pytest.approx(direction_angle, abs=1e-12)
    assert measured.roll_residual_rad == pytest.approx(roll_angle, abs=1e-12)
    assert measured.satisfied is (direction_angle == 0 and roll_angle == 0)


def test_external_profile_free_condition_preserves_complete_actual_frame_without_a_gas_profile():
    interface = GeometryModelInterface(
        interface_key="survey_datum",
        local_frame=RigidTransform3D(
            translation_m=(4, 5, 6),
            rotation=((0, 0, 1), (1, 0, 0), (0, 1, 0)),
        ),
        profile=None,
    )
    before = interface.model_dump()
    measured = evaluate_interface_external_condition(
        interface.local_frame, _external_condition(), actual_profile=interface.profile,
    )
    assert measured.satisfied is True
    assert measured.position_residual_m is None
    assert measured.direction_residual_rad is None
    assert measured.roll_residual_rad is None
    assert measured.profile_satisfied is None
    assert interface.model_dump() == before
    with pytest.raises(ValidationError, match="orthonormal"):
        RigidTransform3D(rotation=((1, 0, 0), (0, 0, 0), (0, 0, 1)))


@pytest.mark.parametrize(
    "actual_profile,satisfied",
    [
        (CircularInterfaceProfile(diameter_m=0.25), True),
        (CircularInterfaceProfile(diameter_m=0.26), False),
        (RectangleInterfaceProfile(width_m=0.25, height_m=0.25), False),
        (None, False),
    ],
)
def test_external_profile_equality_is_not_disabled_by_free_position(actual_profile, satisfied):
    measured = evaluate_interface_external_condition(
        RigidTransform3D(translation_m=(20, -30, 40)),
        _external_condition(
            profile={"profile_kind": "equal", "profile": {"kind": "circular", "diameter_m": 0.25}}
        ),
        actual_profile=actual_profile,
    )
    assert measured.position_residual_m is None
    assert measured.profile_satisfied is satisfied
    assert measured.satisfied is satisfied


@pytest.mark.parametrize(
    "overrides",
    [
        {"position": {"position_kind": "point", "point_m": (0, math.nan, 0)}},
        {"position": {"position_kind": "point", "point_m": (math.inf, 0, 0)}},
        {"position": {"position_kind": "free", "point_m": (0, 0, 0)}},
        {"direction": {"direction_kind": "same", "axis": (0, 0, 0)}},
        {"direction": {"direction_kind": "opposed", "axis": (0, math.inf, 1)}},
        {"direction": {"direction_kind": "same", "axis": (0, 1)}},
        {"direction": {"direction_kind": "none", "axis": (0, 0, 1)}},
        {"roll": {"roll_kind": "reference", "reference_direction": (1, 0, 0)}},
        {"roll": {"roll_kind": "none", "reference_direction": (1, 0, 0)}},
        {"direction": {"direction_kind": "same", "axis": (0, 0, 1)}, "roll": {"roll_kind": "reference", "reference_direction": (0, 0, 1)}},
        {"direction": {"direction_kind": "opposed", "axis": (0, 0, 1)}, "roll": {"roll_kind": "reference", "reference_direction": (1, 0, 1)}},
        {"direction": {"direction_kind": "same", "axis": (0, 0, 1)}, "roll": {"roll_kind": "reference", "reference_direction": (0, 0, 0)}},
        {"direction": {"direction_kind": "same", "axis": (0, 0, 1)}, "roll": {"roll_kind": "reference", "reference_direction": (math.nan, 0, 0)}},
        {"position_tolerance_m": math.inf},
        {"angular_tolerance_rad": math.nan},
    ],
)
def test_external_condition_rejects_nonfinite_zero_incomplete_and_contradictory_inputs(overrides):
    with pytest.raises(ValidationError):
        _external_condition(**overrides)


@pytest.mark.parametrize(
    "direction,extent",
    [
        ((0, 0, 0), {"extent_kind": "unbounded"}),
        ((0, math.inf, 1), {"extent_kind": "unbounded"}),
        ((0, 0, math.nan), {"extent_kind": "unbounded"}),
        ((0, 0, 1), {"extent_kind": "bounded", "minimum_m": 1, "maximum_m": -1}),
        ((0, 0, 1), {"extent_kind": "bounded", "minimum_m": 0, "maximum_m": math.inf}),
        ((0, 0, 1), {"extent_kind": "bounded", "minimum_m": math.nan, "maximum_m": 1}),
    ],
)
def test_external_line_rejects_invalid_direction_or_extent_before_measurement(direction, extent):
    with pytest.raises(ValidationError):
        _external_condition(
            position={"position_kind": "line", "point_m": (0, 0, 0), "direction": direction, "extent": extent}
        )
