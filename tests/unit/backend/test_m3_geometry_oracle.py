from __future__ import annotations

import itertools
import math
import random
from typing import Any

import pytest

from CAD2BIM_backend.infrastructure.cad_measurement import (
    GeometryBlockerError,
    GeometryBudgetError,
    MeasurementBundle,
    MeasurementTolerances,
    PointScope,
    compute_measurements,
    quantize_ties_to_even,
)


def _entity(
    key: str,
    *,
    entity_type: str = "LINE",
    layer: str = "paths",
) -> dict[str, Any]:
    return {
        "document_key": "doc_test",
        "entity_key": key,
        "entity_scope": "top_level_modelspace",
        "entity_type": entity_type,
        "layer": layer,
        "typed_geometry_supported": True,
        "unsupported_reason": None,
        "extrusion_x": 0.0,
        "extrusion_y": 0.0,
        "extrusion_z": 1.0,
    }


def _line_geometry(
    key: str,
    start: tuple[float, float, float],
    end: tuple[float, float, float],
) -> list[dict[str, Any]]:
    rows = []
    for ordinal, (kind, point) in enumerate((("line_start", start), ("line_end", end))):
        rows.append(
            {
                "document_key": "doc_test",
                "geometry_element_key": f"geom:{key}:{ordinal}",
                "entity_key": key,
                "geometry_component_kind": kind,
                "ordinal": ordinal,
                "x": point[0],
                "y": point[1],
                "z": point[2],
                "bulge": None,
            }
        )
    return rows


def _point_geometry(key: str, point: tuple[float, float, float]) -> list[dict[str, Any]]:
    return [
        {
            "document_key": "doc_test",
            "geometry_element_key": f"geom:{key}:0",
            "entity_key": key,
            "geometry_component_kind": "point_location",
            "ordinal": 0,
            "x": point[0],
            "y": point[1],
            "z": point[2],
            "bulge": None,
        }
    ]


def _tolerances(
    *,
    quantization: float = 0.0001,
    snap: float = 0.001,
    endpoint_on_path: float = 0.001,
    guard: float = 0.001,
    uniqueness: float = 0.001,
) -> MeasurementTolerances:
    return MeasurementTolerances(
        endpoint_quantization_m=quantization,
        endpoint_snap_candidate_m=snap,
        endpoint_on_path_candidate_m=endpoint_on_path,
        host_endpoint_guard_m=guard,
        point_target_uniqueness_margin_m=uniqueness,
        straight_turn_tolerance_deg=0.5,
    )


def _compute(
    entities: list[dict[str, Any]],
    geometry: list[dict[str, Any]],
    *,
    point_scopes: tuple[PointScope, ...] = (),
    tolerances: MeasurementTolerances | None = None,
) -> MeasurementBundle:
    return compute_measurements(
        entities=entities,
        geometry_elements=geometry,
        relationships=[],
        path_layers=("paths",),
        point_scopes=point_scopes,
        source_unit_to_m=1.0,
        tolerances=tolerances or _tolerances(),
        candidate_budget=100_000,
    )


def test_m3_quantization_uses_explicit_decimal_ties_to_even() -> None:
    quantum = 0.0001
    assert [
        quantize_ties_to_even(value, quantum)
        for value in (-0.00025, -0.00015, -0.00005, 0.00005, 0.00015, 0.00025)
    ] == [-2, -2, 0, 0, 2, 2]


def test_m3_endpoint_pairs_match_complete_brute_force_oracle_without_truncation() -> None:
    entities = []
    geometry = []
    for index in range(9):
        key = f"ray_{index:02d}"
        angle = 2.0 * math.pi * index / 9
        entities.append(_entity(key))
        geometry.extend(
            _line_geometry(key, (0.0, 0.0, 0.0), (10 * math.cos(angle), 10 * math.sin(angle), 0.0))
        )

    bundle = _compute(entities, geometry)
    endpoint_by_key = {row["endpoint_key"]: row for row in bundle.endpoints}
    expected = set()
    for left, right in itertools.combinations(bundle.endpoints, 2):
        if left["atomic_edge_key"] == right["atomic_edge_key"]:
            continue
        distance = math.dist(
            (left["x_m"], left["y_m"], left["z_m"]),
            (right["x_m"], right["y_m"], right["z_m"]),
        )
        if distance <= 0.001:
            expected.add(frozenset((left["endpoint_key"], right["endpoint_key"])))
    actual = {
        frozenset((row["endpoint_a_key"], row["endpoint_b_key"])) for row in bundle.endpoint_pairs
    }

    assert len(endpoint_by_key) == 18
    assert len(actual) == 36
    assert actual == expected


def test_m3_candidate_budget_fails_instead_of_returning_a_truncated_bundle() -> None:
    entities = []
    geometry = []
    for index in range(7):
        key = f"ray_{index:02d}"
        angle = 2.0 * math.pi * index / 7
        entities.append(_entity(key))
        geometry.extend(
            _line_geometry(
                key,
                (0.0, 0.0, 0.0),
                (10 * math.cos(angle), 10 * math.sin(angle), 0.0),
            )
        )

    with pytest.raises(GeometryBudgetError) as raised:
        compute_measurements(
            entities=entities,
            geometry_elements=geometry,
            relationships=[],
            path_layers=("paths",),
            point_scopes=(),
            source_unit_to_m=1.0,
            tolerances=_tolerances(),
            candidate_budget=10,
        )

    assert raised.value.limit == 10
    assert raised.value.observed == 11


def test_m3_point_to_path_keeps_every_raw_candidate_and_distinct_target_rank() -> None:
    entities = [_entity("probe", entity_type="POINT", layer="probes")]
    geometry = _point_geometry("probe", (0.0, 0.0, 0.0))
    expected_distances = []
    for index in range(1, 25):
        key = f"host_{index:02d}"
        offset = index / 1000
        entities.append(_entity(key))
        geometry.extend(_line_geometry(key, (-1.0, offset, 0.0), (1.0, offset, 0.0)))
        expected_distances.append(offset)

    bundle = _compute(
        entities,
        geometry,
        point_scopes=(PointScope("probe_scope", ("probes",), 0.025),),
        tolerances=_tolerances(snap=0.00001, endpoint_on_path=0.00001),
    )
    candidates = list(bundle.point_to_path)

    assert len(candidates) == 24
    assert sorted(row["distance_3d_m"] for row in candidates) == pytest.approx(expected_distances)
    assert sorted(row["raw_candidate_rank"] for row in candidates) == list(range(1, 25))
    assert sorted(row["distinct_target_rank"] for row in candidates) == list(range(1, 25))
    assert len({row["target_key"] for row in candidates}) == 24


def test_m3_xy_crossing_with_different_z_is_only_a_planar_fact() -> None:
    entities = [_entity("low"), _entity("high")]
    geometry = [
        *_line_geometry("low", (-1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        *_line_geometry("high", (0.0, -1.0, 2.0), (0.0, 1.0, 2.0)),
    ]

    bundle = _compute(entities, geometry)

    assert len(bundle.planar_intersections) == 1
    intersection = bundle.planar_intersections[0]
    assert intersection["intersection_kind"] == "proper_crossing"
    assert (intersection["xy_start_x_m"], intersection["xy_start_y_m"]) == pytest.approx((0.0, 0.0))
    assert intersection["a_z_start_m"] in {0.0, 2.0}
    assert intersection["b_z_start_m"] in {0.0, 2.0}
    assert intersection["a_z_start_m"] != intersection["b_z_start_m"]
    assert intersection["absolute_z_gap_start_m"] == pytest.approx(2.0)
    assert bundle.endpoint_pairs == ()
    assert not hasattr(bundle, "topology_nodes")


def test_m3_rows_and_logical_keys_are_deterministic_under_input_reordering() -> None:
    entities = [
        _entity("a"),
        _entity("b"),
        _entity("probe", entity_type="POINT", layer="probes"),
    ]
    geometry = [
        *_line_geometry("a", (-1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        *_line_geometry("b", (0.0, -1.0, 0.0), (0.0, 1.0, 0.0)),
        *_point_geometry("probe", (0.25, 0.0005, 0.0)),
    ]
    scope = (PointScope("probe_scope", ("probes",), 0.5),)
    expected = _compute(entities, geometry, point_scopes=scope)

    randomizer = random.Random(20260824)
    shuffled_entities = list(entities)
    shuffled_geometry = list(geometry)
    randomizer.shuffle(shuffled_entities)
    randomizer.shuffle(shuffled_geometry)
    actual = _compute(shuffled_entities, shuffled_geometry, point_scopes=scope)

    assert actual == expected
    assert actual.to_arrow_tables() == expected.to_arrow_tables()


def test_m3_multiple_cad_inputs_use_separate_calls_without_cross_source_state() -> None:
    first = _compute(
        [_entity("cad_a_line")],
        _line_geometry("cad_a_line", (0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
    )
    second = _compute(
        [_entity("cad_b_line_1"), _entity("cad_b_line_2")],
        [
            *_line_geometry("cad_b_line_1", (10.0, 0.0, 0.0), (11.0, 0.0, 0.0)),
            *_line_geometry("cad_b_line_2", (11.0, 0.0, 0.0), (12.0, 0.0, 0.0)),
        ],
    )

    assert {row["source_entity_key"] for row in first.atomic_edges} == {"cad_a_line"}
    assert {row["source_entity_key"] for row in second.atomic_edges} == {
        "cad_b_line_1",
        "cad_b_line_2",
    }
    assert not (
        {row["source_entity_key"] for row in first.atomic_edges}
        & {row["source_entity_key"] for row in second.atomic_edges}
    )


def test_m3_quantization_collapse_blocks_compute_atomically() -> None:
    entities = [_entity("short")]
    geometry = _line_geometry("short", (0.0002, 0.0002, 0.0), (0.0004, 0.0004, 0.0))

    with pytest.raises(GeometryBlockerError) as raised:
        _compute(entities, geometry, tolerances=_tolerances(quantization=0.001))

    assert {row["diagnostic_code"] for row in raised.value.diagnostics} == {"QUANTIZATION_COLLAPSE"}


def test_m3_empty_relations_keep_all_ten_typed_payloads() -> None:
    bundle = _compute(
        [_entity("single")],
        _line_geometry("single", (0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
    )
    tables = bundle.to_arrow_tables()

    assert tuple(tables) == (
        "atomic_edges",
        "endpoints",
        "endpoint_pairs",
        "endpoint_on_path",
        "planar_intersections",
        "point_to_path",
        "curve_turns",
        "entity_bounds",
        "local_frames",
        "tolerance_diagnostics",
    )
    assert tables["endpoint_pairs"].num_rows == 0
    assert tables["endpoint_pairs"].schema.names[0] == "endpoint_pair_key"
