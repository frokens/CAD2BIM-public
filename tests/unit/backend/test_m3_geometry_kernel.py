from __future__ import annotations

import math
from pathlib import Path
from random import Random
from typing import Any

import pyarrow as pa
import pytest

from CAD2BIM_backend.infrastructure.cad_measurement import (
    GeometryBlockerError,
    GeometryBudgetError,
    GeometryInputError,
    MEASUREMENT_PAYLOAD_SCHEMAS,
    MeasurementTolerances,
    NamedToleranceTrial,
    PointGroup,
    PointScope,
    analyze_tolerance_candidates,
    compute_measurements,
    quantize_ties_to_even,
)
from CAD2BIM_backend.infrastructure.cad_measurement import analysis as measurement_analysis
from CAD2BIM_backend.domain.cad_measurement import CadToleranceAnalysisResult


def _entity(
    key: str,
    entity_type: str,
    layer: str = "paths",
    *,
    extrusion: tuple[float, float, float] = (0.0, 0.0, 1.0),
    supported: bool = True,
) -> dict[str, Any]:
    return {
        "entity_key": key,
        "entity_type": entity_type,
        "layer": layer,
        "extrusion_x": extrusion[0],
        "extrusion_y": extrusion[1],
        "extrusion_z": extrusion[2],
        "typed_geometry_supported": supported,
        "unsupported_reason": None if supported else "typed_geometry_not_implemented",
    }


def _geometry(
    entity_key: str,
    ordinal: int,
    kind: str,
    x: float,
    y: float,
    z: float = 0.0,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "geometry_element_key": f"{entity_key}_geom_{ordinal:05d}",
        "entity_key": entity_key,
        "geometry_component_kind": kind,
        "component_path": str(ordinal),
        "ordinal": ordinal,
        "x": x,
        "y": y,
        "z": z,
        "bulge": extra.get("bulge"),
        "closed": extra.get("closed"),
        "flags": extra.get("flags"),
    }


def _line_geometry(
    key: str, start: tuple[float, float, float], end: tuple[float, float, float]
) -> list[dict[str, Any]]:
    return [
        _geometry(key, 0, "line_start", *start),
        _geometry(key, 1, "line_end", *end),
    ]


def _tolerances(**changes: float) -> MeasurementTolerances:
    values = {
        "endpoint_quantization_m": 0.1,
        "endpoint_snap_candidate_m": 0.5,
        "endpoint_on_path_candidate_m": 0.5,
        "host_endpoint_guard_m": 0.1,
        "point_target_uniqueness_margin_m": 0.1,
        "straight_turn_tolerance_deg": 1.0,
    }
    values.update(changes)
    return MeasurementTolerances(**values)


def _compute(
    entities: list[dict[str, Any]],
    geometry: list[dict[str, Any]],
    relationships: list[dict[str, Any]] | None = None,
    *,
    tolerances: MeasurementTolerances | None = None,
    scopes: tuple[PointScope, ...] = (),
    candidate_budget: int = 10_000,
    fail_on_blockers: bool = True,
):
    return compute_measurements(
        entities=entities,
        geometry_elements=geometry,
        relationships=relationships or [],
        path_layers=("paths",),
        point_scopes=scopes,
        source_unit_to_m=1.0,
        tolerances=tolerances or _tolerances(),
        candidate_budget=candidate_budget,
        fail_on_blockers=fail_on_blockers,
    )


def _plain(rows):
    return [dict(row) for row in rows]


def test_explicit_ties_to_even_quantization_handles_positive_and_negative_halves() -> None:
    assert [quantize_ties_to_even(value, 1.0) for value in (-2.5, -1.5, -0.5, 0.5, 1.5, 2.5)] == [
        -2,
        -2,
        0,
        0,
        2,
        2,
    ]
    assert quantize_ties_to_even(0.00005, 0.0001) == 0
    assert quantize_ties_to_even(0.00015, 0.0001) == 2


def test_atomic_expansion_restores_polyline_order_closure_and_ocs_deterministically() -> None:
    entities = [
        _entity("line", "LINE"),
        _entity("lw", "LWPOLYLINE", extrusion=(0.0, 1.0, 0.0)),
        _entity("poly", "POLYLINE"),
        _entity("v1", "VERTEX"),
        _entity("v2", "VERTEX"),
        _entity("v3", "VERTEX"),
    ]
    geometry = [
        *_line_geometry("line", (0, 0, 0), (1, 0, 0)),
        _geometry("lw", 0, "polyline_vertex", 1, 2, 3, bulge=0.0, closed=True),
        _geometry("lw", 1, "polyline_vertex", 2, 2, 3, bulge=0.0, closed=True),
        _geometry("lw", 2, "polyline_vertex", 2, 3, 3, bulge=0.0, closed=True),
        _geometry("poly", 0, "polyline_origin", 0, 0, 0, closed=False, flags=0),
        _geometry("v1", 0, "polyline_vertex", 0, 0, 0, bulge=0.0),
        _geometry("v2", 0, "polyline_vertex", 0, 2, 0, bulge=0.0),
        _geometry("v3", 0, "polyline_vertex", 2, 2, 0, bulge=0.0),
    ]
    relationships = [
        {
            "relationship_key": "rel_003",
            "relation_kind": "polyline_vertex",
            "subject_key": "poly",
            "resolved_target_key": "v3",
        },
        {
            "relationship_key": "rel_001",
            "relation_kind": "polyline_vertex",
            "subject_key": "poly",
            "resolved_target_key": "v1",
        },
        {
            "relationship_key": "rel_002",
            "relation_kind": "polyline_vertex",
            "subject_key": "poly",
            "resolved_target_key": "v2",
        },
    ]

    first = _compute(entities, geometry, relationships)
    shuffled_entities, shuffled_geometry, shuffled_relationships = (
        list(entities),
        list(geometry),
        list(relationships),
    )
    random = Random(42)
    random.shuffle(shuffled_entities)
    random.shuffle(shuffled_geometry)
    random.shuffle(shuffled_relationships)
    second = _compute(shuffled_entities, shuffled_geometry, shuffled_relationships)

    for payload_name in MEASUREMENT_PAYLOAD_SCHEMAS:
        assert _plain(getattr(first, payload_name)) == _plain(getattr(second, payload_name))
    lw_edges = [row for row in first.atomic_edges if row["source_entity_key"] == "lw"]
    assert len(lw_edges) == 3
    assert [row["is_closing_segment"] for row in lw_edges] == [False, False, True]
    # ezdxf OCS(0, 1, 0): (1, 2, 3) -> (-1, 3, 2).
    assert (lw_edges[0]["start_x_m"], lw_edges[0]["start_y_m"], lw_edges[0]["start_z_m"]) == (
        -1.0,
        3.0,
        2.0,
    )
    poly_edges = [row for row in first.atomic_edges if row["source_entity_key"] == "poly"]
    assert [
        (row["start_x_m"], row["start_y_m"], row["end_x_m"], row["end_y_m"]) for row in poly_edges
    ] == [
        (0.0, 0.0, 0.0, 2.0),
        (0.0, 2.0, 2.0, 2.0),
    ]


def test_complete_inclusive_relationships_report_xy_intersection_z_and_distinct_targets() -> None:
    entities = [
        _entity("host", "LINE"),
        _entity("cross", "LINE"),
        _entity("branch", "LINE"),
        _entity("shared", "LINE"),
        _entity("point", "POINT", "points"),
    ]
    geometry = [
        *_line_geometry("host", (0, 0, 0), (10, 0, 0)),
        *_line_geometry("cross", (5, -5, 1), (5, 5, 1)),
        *_line_geometry("branch", (5, 0.5, 0), (5, 2, 0)),
        *_line_geometry("shared", (10, 0, 0), (11, 0, 0)),
        _geometry("point", 0, "point_location", 10, 0, 0.1),
    ]
    result = _compute(
        entities,
        geometry,
        tolerances=_tolerances(endpoint_snap_candidate_m=0.5),
        scopes=(PointScope("fixtures", ("points",), 6.0),),
    )

    assert any(row["distance_3d_m"] == 0.5 for row in result.endpoint_on_path)
    intersection = next(
        row
        for row in result.planar_intersections
        if {row["source_entity_a_key"], row["source_entity_b_key"]} == {"host", "cross"}
    )
    assert intersection["intersection_kind"] == "proper_crossing"
    assert intersection["absolute_z_gap_start_m"] == 1.0
    point_rows = [row for row in result.point_to_path if row["point_entity_key"] == "point"]
    shared_rows = [
        row for row in point_rows if row["candidate_quantized_endpoint_key"] == "q:100:0:0"
    ]
    assert len(shared_rows) == 2
    assert {row["target_key"] for row in shared_rows} == {"q:100:0:0"}
    assert [
        row["raw_candidate_rank"]
        for row in sorted(point_rows, key=lambda row: row["raw_candidate_rank"])
    ] == list(range(1, len(point_rows) + 1))
    assert all(row["best_to_second_distinct_target_gap_m"] is not None for row in point_rows)


def test_curve_turns_bounds_frames_and_empty_arrow_payloads_have_fixed_schemas() -> None:
    entities = [_entity("poly", "LWPOLYLINE")]
    geometry = [
        _geometry("poly", 0, "polyline_vertex", 0, 0, bulge=0.0, closed=False),
        _geometry("poly", 1, "polyline_vertex", 1, 0, bulge=0.0, closed=False),
        _geometry("poly", 2, "polyline_vertex", 2, 0.01, bulge=0.0, closed=False),
    ]
    result = _compute(entities, geometry, tolerances=_tolerances(straight_turn_tolerance_deg=1.0))

    assert len(result.curve_turns) == 1
    assert result.curve_turns[0]["within_straight_tolerance"] is True
    assert dict(result.entity_bounds[0]) | {} == {
        "document_key": "doc_0001",
        "source_entity_key": "poly",
        "source_entity_type": "LWPOLYLINE",
        "source_layer": "paths",
        "selected_as_path": True,
        "selected_as_point": False,
        "geometry_element_count": 3,
        "min_x_m": 0.0,
        "min_y_m": 0.0,
        "min_z_m": 0.0,
        "max_x_m": 2.0,
        "max_y_m": 0.01,
        "max_z_m": 0.0,
        "center_x_m": 1.0,
        "center_y_m": 0.005,
        "center_z_m": 0.0,
        "size_x_m": 2.0,
        "size_y_m": 0.01,
        "size_z_m": 0.0,
    }
    for frame in result.local_frames:
        x = [frame[f"x_axis_{axis}"] for axis in "xyz"]
        y = [frame[f"y_axis_{axis}"] for axis in "xyz"]
        z = [frame[f"z_axis_{axis}"] for axis in "xyz"]
        assert math.isclose(sum(value * value for value in x), 1.0)
        assert math.isclose(sum(a * b for a, b in zip(x, y)), 0.0, abs_tol=1e-12)
        assert math.isclose(sum(a * b for a, b in zip(x, z)), 0.0, abs_tol=1e-12)
    tables = result.to_arrow_tables()
    assert set(tables) == set(MEASUREMENT_PAYLOAD_SCHEMAS)
    assert all(isinstance(table, pa.Table) for table in tables.values())
    assert tables["endpoint_pairs"].schema == MEASUREMENT_PAYLOAD_SCHEMAS["endpoint_pairs"]


@pytest.mark.parametrize(
    ("entities", "geometry", "tolerances", "code"),
    [
        (
            [_entity("bad", "LINE")],
            _line_geometry("bad", (0, 0, 0), (math.nan, 1, 0)),
            _tolerances(),
            "NONFINITE_COORDINATE",
        ),
        (
            [_entity("bad", "LINE")],
            _line_geometry("bad", (0, 0, 0), (0, 0, 0)),
            _tolerances(),
            "ZERO_LENGTH_EDGE",
        ),
        (
            [_entity("bad", "LWPOLYLINE")],
            [
                _geometry("bad", 0, "polyline_vertex", 0, 0, bulge=0.1),
                _geometry("bad", 1, "polyline_vertex", 1, 0, bulge=0.0),
            ],
            _tolerances(),
            "NONZERO_BULGE",
        ),
        (
            [_entity("bad", "LINE")],
            _line_geometry("bad", (0, 0, 0), (0.4, 0, 0)),
            _tolerances(endpoint_quantization_m=1.0),
            "QUANTIZATION_COLLAPSE",
        ),
    ],
)
def test_compute_rejects_every_geometry_blocker_atomically(
    entities, geometry, tolerances, code
) -> None:
    with pytest.raises(GeometryBlockerError) as raised:
        _compute(entities, geometry, tolerances=tolerances)
    assert {row["diagnostic_code"] for row in raised.value.diagnostics} == {code}


def test_candidate_budget_fails_instead_of_returning_a_truncated_prefix() -> None:
    entities = [_entity(f"line_{index}", "LINE") for index in range(3)]
    geometry = [
        row
        for index in range(3)
        for row in _line_geometry(f"line_{index}", (0, index * 0.1, 0), (1, index * 0.1, 0))
    ]
    with pytest.raises(GeometryBudgetError) as raised:
        _compute(
            entities,
            geometry,
            tolerances=_tolerances(endpoint_snap_candidate_m=1.0),
            candidate_budget=1,
        )
    assert raised.value.observed == raised.value.limit + 1


def test_analyzer_uses_same_kernel_and_only_recommends_a_blocker_free_scalar_plateau() -> None:
    entities = [_entity("a", "LINE"), _entity("b", "LINE")]
    geometry = [
        *_line_geometry("a", (0, 0, 0), (1, 0, 0)),
        *_line_geometry("b", (0, 2, 0), (1, 2, 0)),
    ]
    trials = tuple(
        NamedToleranceTrial(f"t{index}", _tolerances(endpoint_snap_candidate_m=value), ())
        for index, value in enumerate((0.1, 0.2, 0.3), start=1)
    )
    analysis = analyze_tolerance_candidates(
        entities=entities,
        geometry_elements=geometry,
        relationships=[],
        path_layers=("paths",),
        point_groups=(),
        source_unit_to_m=1.0,
        trials=trials,
        candidate_budget=100,
    )
    assert analysis.varying_parameter == "endpoint_snap_candidate_m"
    assert analysis.stable_interval_trial_keys == ("t1", "t2")
    assert analysis.suggested_starting_trial_key == "t1"

    multidimensional = analyze_tolerance_candidates(
        entities=entities,
        geometry_elements=geometry,
        relationships=[],
        path_layers=("paths",),
        point_groups=(),
        source_unit_to_m=1.0,
        trials=(
            NamedToleranceTrial("a", _tolerances(endpoint_snap_candidate_m=0.1), ()),
            NamedToleranceTrial(
                "b",
                _tolerances(endpoint_snap_candidate_m=0.2, endpoint_on_path_candidate_m=0.6),
                (),
            ),
        ),
        candidate_budget=100,
    )
    assert multidimensional.suggested_starting_trial_key is None


def test_zero_trial_analysis_is_unquantized_bounded_and_domain_compatible() -> None:
    entities = [_entity("a", "LINE")]
    geometry = _line_geometry("a", (0, 0, 0), (1, 0, 0))
    analysis = analyze_tolerance_candidates(
        entities=entities,
        geometry_elements=geometry,
        relationships=[],
        path_layers=("paths",),
        point_groups=(),
        source_unit_to_m=1.0,
        trials=(),
        candidate_budget=100,
    )
    assert analysis.suggestion_reason == "no_explicit_trials"
    assert analysis.suggested_starting_trial_key is None
    assert analysis.coverage.compute_blocked is False
    assert len(analysis.distributions) <= 32
    assert len(analysis.generated_probes) <= 64
    CadToleranceAnalysisResult.model_validate(
        {
            "package_alias": "data_001",
            "document_key": "doc_0001",
            "source_unit_to_m": 1.0,
            **analysis.to_domain_payload(),
        }
    )


def test_analyzer_nearest_point_distribution_uses_exact_3d_after_spatial_index() -> None:
    entities = [
        _entity("xy_nearest", "LINE"),
        _entity("true_3d_nearest", "LINE"),
        _entity("device", "POINT", "points"),
    ]
    geometry = [
        *_line_geometry("xy_nearest", (-1, 0, 100), (1, 0, 100)),
        *_line_geometry("true_3d_nearest", (10, -1, 0), (10, 1, 0)),
        _geometry("device", 0, "point_location", 0, 0, 0),
    ]

    result = analyze_tolerance_candidates(
        entities=entities,
        geometry_elements=geometry,
        relationships=[],
        path_layers=("paths",),
        point_groups=(PointGroup("devices", ("points",)),),
        source_unit_to_m=1.0,
        trials=(),
        candidate_budget=100,
    )

    distribution = next(
        item for item in result.distributions if item.metric == "nearest_point_to_path_distance"
    )
    assert distribution.scope_key == "devices"
    assert distribution.count == 1
    assert distribution.minimum == pytest.approx(10.0)
    assert distribution.maximum == pytest.approx(10.0)


def test_analyzer_prepares_l0_once_and_requantizes_each_trial(monkeypatch) -> None:
    entities = [_entity("a", "LINE")]
    geometry = _line_geometry("a", (0, 0, 0), (1, 0, 0))
    original_prepare = measurement_analysis.k._prepare
    prepare_calls = 0

    def counted_prepare(**kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        return original_prepare(**kwargs)

    monkeypatch.setattr(measurement_analysis.k, "_prepare", counted_prepare)
    result = analyze_tolerance_candidates(
        entities=entities,
        geometry_elements=geometry,
        relationships=[],
        path_layers=("paths",),
        point_groups=(),
        source_unit_to_m=1.0,
        trials=(
            NamedToleranceTrial("safe", _tolerances(endpoint_quantization_m=0.1), ()),
            NamedToleranceTrial("collapse", _tolerances(endpoint_quantization_m=2.0), ()),
        ),
        candidate_budget=100,
    )

    assert prepare_calls == 1
    assert result.trials[0].quantization_collapse_edge_count == 0
    assert result.trials[1].quantization_collapse_edge_count == 1
    assert result.trials[1].compute_blocked is True


def test_analyzer_accepts_every_explicit_trial_count_from_one_through_eight() -> None:
    entities = [_entity("a", "LINE")]
    geometry = _line_geometry("a", (0, 0, 0), (1, 0, 0))
    all_trials = tuple(
        NamedToleranceTrial(
            f"t{index}",
            _tolerances(endpoint_snap_candidate_m=0.01 * index),
            (),
        )
        for index in range(1, 10)
    )
    for trial_count in range(1, 9):
        analysis = analyze_tolerance_candidates(
            entities=entities,
            geometry_elements=geometry,
            relationships=[],
            path_layers=("paths",),
            point_groups=(),
            source_unit_to_m=1.0,
            trials=all_trials[:trial_count],
            candidate_budget=100,
        )
        assert len(analysis.trials) == trial_count
        assert analysis.suggestion_reason in {
            "stable_single_parameter_interval",
            "no_stable_interval",
        }
    with pytest.raises(GeometryInputError):
        analyze_tolerance_candidates(
            entities=entities,
            geometry_elements=geometry,
            relationships=[],
            path_layers=("paths",),
            point_groups=(),
            source_unit_to_m=1.0,
            trials=all_trials,
            candidate_budget=100,
        )


def test_declared_layers_and_quantized_int64_range_have_stable_error_codes() -> None:
    entities = [_entity("a", "LINE")]
    geometry = _line_geometry("a", (0, 0, 0), (1, 0, 0))
    with pytest.raises(GeometryInputError) as missing:
        compute_measurements(
            entities=entities,
            geometry_elements=geometry,
            relationships=[],
            path_layers=("missing",),
            point_scopes=(),
            source_unit_to_m=1.0,
            tolerances=_tolerances(),
            candidate_budget=100,
        )
    assert missing.value.code == "CAD_LAYER_NOT_FOUND"
    with pytest.raises(GeometryInputError) as out_of_range:
        quantize_ties_to_even(1e20, 1e-9)
    assert out_of_range.value.code == "CAD_ENDPOINT_QUANTIZATION_RANGE_EXCEEDED"

    mixed_entities = [_entity("a_range", "LINE"), _entity("b_collapse", "LINE")]
    mixed_geometry = [
        *_line_geometry("a_range", (1e20, 0, 0), (1e20 + 1e10, 0, 0)),
        *_line_geometry("b_collapse", (0, 0, 0), (0.4, 0, 0)),
    ]
    with pytest.raises(GeometryBlockerError) as mixed:
        _compute(
            mixed_entities,
            mixed_geometry,
            tolerances=_tolerances(endpoint_quantization_m=1.0),
        )
    assert {row["diagnostic_code"] for row in mixed.value.diagnostics} == {
        "QUANTIZATION_RANGE_EXCEEDED",
        "QUANTIZATION_COLLAPSE",
    }


def test_exact_schema_nullability_and_code_artifact_limit() -> None:
    nullable = {
        "atomic_edges": {"source_start_geometry_element_key", "source_end_geometry_element_key"},
        "endpoints": {"source_geometry_element_key"},
        "point_to_path": {
            "candidate_quantized_endpoint_key",
            "best_to_second_distinct_target_gap_m",
        },
        "curve_turns": {"deflection_xy_deg", "signed_xy_turn_deg"},
        "tolerance_diagnostics": {
            "scope_key",
            "count",
            "sample_ordinal",
            "source_entity_key",
            "atomic_edge_key",
            "measured_value",
            "threshold_name",
            "threshold_value",
        },
    }
    for payload, schema in MEASUREMENT_PAYLOAD_SCHEMAS.items():
        assert {field.name for field in schema if field.nullable} == nullable.get(payload, set())
    kernel_path = (
        Path(__file__).resolve().parents[3]
        / "src/CAD2BIM_backend/infrastructure/cad_measurement/kernel.py"
    )
    assert len(kernel_path.read_bytes()) <= 65_536
