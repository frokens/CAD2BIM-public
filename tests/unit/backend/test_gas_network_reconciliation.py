from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
import math
from pathlib import Path
import sys

import pyarrow as pa
import pytest

from tests.scenarios.gas_network_derivation.current_stage_resource_geometry import (
    plan_elbow as source_plan_elbow,
    plan_regulator_interfaces as source_plan_regulator,
    plan_tee as source_plan_tee,
)
from tests.scenarios.gas_network_derivation.current_stage_resource_geometry_worker import (
    plan_elbow_interface as worker_plan_elbow,
    plan_regulator_interfaces_worker as worker_plan_regulator,
    plan_tee_interface as worker_plan_tee,
)
from tests.scenarios.gas_network_derivation.program_source import (
    bundled_program_source,
)
from tests.scenarios.gas_network_derivation.reconciliation_validation import (
    validate_source_path_fidelity,
    validate_writer_closure,
)


ROOT = Path(__file__).parents[3]
SCENARIO = ROOT / "tests/scenarios/gas_network_derivation"


def _module(name: str):
    spec = spec_from_file_location(name, SCENARIO / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    if name == "product_connectivity":
        source, _ = bundled_program_source(SCENARIO / f"{name}.py")
        sys.modules[name] = module
        exec(compile(source, str(SCENARIO / f"{name}.py"), "exec"), module.__dict__)
    else:
        spec.loader.exec_module(module)
    return module


class _Context:
    def __init__(
        self,
        tables: dict[str, pa.Table],
        *,
        params: dict[str, object] | None = None,
    ):
        self.tables = tables
        self.params = params or {}
        self.outputs: dict[str, pa.Table] = {}

    def table(self, name: str) -> pa.Table:
        return self.tables[name]

    def param(self, name: str) -> object:
        return self.params[name]

    def write_table(self, name: str, table: pa.Table) -> None:
        self.outputs[name] = table


def _canonical(
    key: str,
    *,
    od: float,
    pressure: str = "LOW",
    role: str = "pipe",
    entity_type: str | None = None,
) -> dict[str, object]:
    return {
        "source_entity_key": key,
        "source_role": role,
        "source_entity_type": entity_type or ("LINE" if role == "pipe" else "POINT"),
        "pressure_normalized": pressure,
        "outer_diameter_m": od,
        "material": "PE",
        "installation": "BURIED",
        "connection_method": "WELDED",
        "current_status": "ACTIVE",
        "business_location": "道路",
        "source_business_id": key,
        "commissioning_date": None,
    }


def _geometry_key(source: str, ordinal: int) -> str:
    return f"geometry:{source}:{ordinal:05d}"


def _edge(key: str, source: str, start: tuple[float, float, float], end: tuple[float, float, float], ordinal: int = 0) -> dict[str, object]:
    dx, dy, dz = (end[i] - start[i] for i in range(3))
    length = (dx * dx + dy * dy + dz * dz) ** 0.5
    return {
        "atomic_edge_key": key,
        "source_entity_key": source,
        "segment_ordinal": ordinal,
        "source_start_geometry_element_key": _geometry_key(source, ordinal),
        "source_end_geometry_element_key": _geometry_key(source, ordinal + 1),
        "is_closing_segment": False,
        "length_3d_m": length,
        "start_x_m": start[0], "start_y_m": start[1], "start_z_m": start[2],
        "end_x_m": end[0], "end_y_m": end[1], "end_z_m": end[2],
        "unit_x": dx / length, "unit_y": dy / length, "unit_z": dz / length,
        "start_endpoint_key": f"endpoint:{key}:start",
        "end_endpoint_key": f"endpoint:{key}:end",
    }


def _geometry_path(
    source: str,
    points: list[tuple[float, float, float]],
    *,
    entity_type: str,
    geometry_keys: list[str] | None = None,
) -> list[dict[str, object]]:
    assert entity_type in {"LINE", "LWPOLYLINE"}
    assert len(points) >= 2
    if geometry_keys is not None:
        assert len(geometry_keys) == len(points)
    rows = []
    for ordinal, point in enumerate(points):
        rows.append(
            {
                "document_key": "doc:test",
                "geometry_element_key": (
                    geometry_keys[ordinal]
                    if geometry_keys is not None
                    else _geometry_key(source, ordinal)
                ),
                "entity_key": source,
                "geometry_component_kind": (
                    ("line_start" if ordinal == 0 else "line_end")
                    if entity_type == "LINE"
                    else "polyline_vertex"
                ),
                "component_path": (
                    ("start" if ordinal == 0 else "end")
                    if entity_type == "LINE"
                    else f"vertices[{ordinal}]"
                ),
                "ordinal": ordinal,
                "x": point[0],
                "y": point[1],
                "z": point[2],
                "bulge": None,
                "start_width": None,
                "end_width": None,
                "constant_width": 0.0,
                "elevation": 0.0,
                "angle_degrees": None,
                "closed": False,
                "flags": 0,
            }
        )
    return rows


def _point_geometry(
    source: str,
    point: tuple[float | None, float | None, float | None],
) -> dict[str, object]:
    return {
        "document_key": "doc:test",
        "geometry_element_key": _geometry_key(source, 0),
        "entity_key": source,
        "geometry_component_kind": "point_location",
        "component_path": "location",
        "ordinal": 0,
        "x": point[0],
        "y": point[1],
        "z": point[2],
        "bulge": None,
        "start_width": None,
        "end_width": None,
        "constant_width": None,
        "elevation": None,
        "angle_degrees": None,
        "closed": None,
        "flags": None,
    }


def _measurement_path(
    source: str,
    points: list[tuple[float, float, float]],
    *,
    geometry_keys: list[str] | None = None,
    edge_keys: list[str] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if geometry_keys is not None:
        assert len(geometry_keys) == len(points)
    if edge_keys is not None:
        assert len(edge_keys) == len(points) - 1
    edges = [
        _edge(
            edge_keys[ordinal] if edge_keys is not None else f"edge:{source}:{ordinal:05d}",
            source,
            start,
            end,
            ordinal,
        )
        for ordinal, (start, end) in enumerate(zip(points, points[1:]))
    ]
    if geometry_keys is not None:
        for ordinal, edge in enumerate(edges):
            edge["source_start_geometry_element_key"] = geometry_keys[ordinal]
            edge["source_end_geometry_element_key"] = geometry_keys[ordinal + 1]
    endpoints = []
    for ordinal, edge in enumerate(edges):
        endpoints.extend(
            _endpoints(
                edge,
                f"q:{source}:{ordinal:05d}",
                f"q:{source}:{ordinal + 1:05d}",
            )
        )
    return edges, endpoints


def _topology_context(
    *,
    canonical: list[dict[str, object]],
    geometry: list[dict[str, object]],
    edges: list[dict[str, object]],
    endpoints: list[dict[str, object]],
    pairs: pa.Table | None = None,
    endpoint_on_path: pa.Table | None = None,
    point_to_path: pa.Table | None = None,
    source_unit_to_m: object = 1.0,
) -> _Context:
    return _Context(
        {
            "ce": pa.Table.from_pylist(canonical),
            "ge": pa.Table.from_pylist(geometry),
            "ae": pa.Table.from_pylist(edges),
            "ep": pa.Table.from_pylist(endpoints),
            "pairs": pairs if pairs is not None else _empty(),
            "eops": endpoint_on_path if endpoint_on_path is not None else _empty(),
            "ptps": point_to_path if point_to_path is not None else _empty(),
            "riser_endpoint_alignments": pa.table({
                "riser_source_entity_key": pa.array([], type=pa.string()),
                "pipe_source_entity_key": pa.array([], type=pa.string()),
            }),
        },
        params={"source_unit_to_m": source_unit_to_m, "host_endpoint_guard_m": 0.01},
    )


def _connectivity_context(
    topology: _Context,
    *,
    canonical: list[dict[str, object]],
    endpoints: list[dict[str, object]],
) -> _Context:
    return _Context(
        {
            "pieces": topology.outputs["pipe_pieces"],
            "vertices": topology.outputs["pipe_path_vertices"],
            "events": topology.outputs["product_events"],
            "cuts": topology.outputs["host_cuts"],
            "decisions": topology.outputs["candidate_decisions"],
            "canonical": pa.Table.from_pylist(canonical),
            "endpoints": pa.Table.from_pylist(endpoints),
        }
    )


def _reversed_context(ctx: _Context) -> _Context:
    tables = {}
    for name, table in ctx.tables.items():
        rows = table.to_pylist()
        tables[name] = (
            pa.Table.from_pylist(list(reversed(rows)), schema=table.schema)
            if rows
            else table
        )
    return _Context(tables)


def _two_sided_device_context(
    role: str,
    *,
    right_od: float | None = 0.2,
    include_source_origin: bool = True,
    source_origin_point: tuple[float | None, float | None, float | None] = (
        0.0,
        2.0,
        0.0,
    ),
) -> tuple[_Context, str]:
    assert role in {"valve", "regulator"}
    left_source = f"pipe:{role}:left"
    right_source = f"pipe:{role}:right"
    device_source = f"{role}:source"
    left_points = [(-3.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
    right_points = [(0.0, 0.0, 0.0), (5.0, 0.0, 0.0)]
    left_edges, left_endpoints = _measurement_path(left_source, left_points)
    right_edges, right_endpoints = _measurement_path(right_source, right_points)
    shared_node = f"q:{role}:device"
    left_endpoints[-1]["quantized_endpoint_key"] = shared_node
    right_endpoints[0]["quantized_endpoint_key"] = shared_node
    candidates = []
    for ordinal, (edge, parameter) in enumerate(
        ((left_edges[0], 1.0), (right_edges[0], 0.0))
    ):
        row = _point_candidate(
            f"point:{role}:{ordinal}",
            device_source,
            edge,
            target_kind="quantized_endpoint",
            target_key=shared_node,
        )
        row.update(
            point_x_m=0.0,
            point_y_m=2.0,
            point_z_m=0.0,
            projection_x_m=0.0,
            projection_y_m=0.0,
            projection_z_m=0.0,
            host_parameter=parameter,
        )
        candidates.append(row)
    canonical = [
        _canonical(left_source, od=0.1, pressure="MEDIUM"),
        _canonical(right_source, od=right_od, pressure="MEDIUM"),
        _canonical(device_source, od=None, pressure="MEDIUM", role=role),
    ]
    geometry = (
        _geometry_path(left_source, left_points, entity_type="LINE")
        + _geometry_path(right_source, right_points, entity_type="LINE")
    )
    if include_source_origin:
        geometry.append(_point_geometry(device_source, source_origin_point))
    endpoints = left_endpoints + right_endpoints
    topology = _topology_context(
        canonical=canonical,
        geometry=geometry,
        edges=left_edges + right_edges,
        endpoints=endpoints,
        point_to_path=pa.Table.from_pylist(candidates),
    )
    _module("topology_derivation").transform(topology)
    connectivity = _connectivity_context(
        topology,
        canonical=canonical,
        endpoints=endpoints,
    )
    _module("product_connectivity").transform(connectivity)
    return connectivity, "point:" + device_source


def _endpoints(edge: dict[str, object], q_start: str, q_end: str) -> list[dict[str, object]]:
    return [
        {
            "endpoint_key": edge["start_endpoint_key"], "atomic_edge_key": edge["atomic_edge_key"],
            "source_entity_key": edge["source_entity_key"], "side": "start",
            "quantized_endpoint_key": q_start,
            "x_m": edge["start_x_m"], "y_m": edge["start_y_m"], "z_m": edge["start_z_m"],
            "quantized_x_m": edge["start_x_m"], "quantized_y_m": edge["start_y_m"], "quantized_z_m": edge["start_z_m"],
            "inward_tangent_x": edge["unit_x"], "inward_tangent_y": edge["unit_y"], "inward_tangent_z": edge["unit_z"],
        },
        {
            "endpoint_key": edge["end_endpoint_key"], "atomic_edge_key": edge["atomic_edge_key"],
            "source_entity_key": edge["source_entity_key"], "side": "end",
            "quantized_endpoint_key": q_end,
            "x_m": edge["end_x_m"], "y_m": edge["end_y_m"], "z_m": edge["end_z_m"],
            "quantized_x_m": edge["end_x_m"], "quantized_y_m": edge["end_y_m"], "quantized_z_m": edge["end_z_m"],
            "inward_tangent_x": -edge["unit_x"], "inward_tangent_y": -edge["unit_y"], "inward_tangent_z": -edge["unit_z"],
        },
    ]


def _empty() -> pa.Table:
    return pa.table({})


def _point_candidate(
    key: str,
    point: str,
    edge: dict[str, object],
    *,
    target_kind: str,
    target_key: str,
    distance: float = 0.0,
) -> dict[str, object]:
    return {
        "point_to_path_key": key,
        "scope_key": "riser" if "riser" in point else "valve",
        "point_entity_key": point,
        "host_atomic_edge_key": edge["atomic_edge_key"],
        "host_source_entity_key": edge["source_entity_key"],
        "point_x_m": edge["start_x_m"], "point_y_m": edge["start_y_m"], "point_z_m": edge["start_z_m"],
        "projection_x_m": edge["start_x_m"], "projection_y_m": edge["start_y_m"], "projection_z_m": edge["start_z_m"],
        "host_parameter": 0.0, "distance_3d_m": distance,
        "target_kind": target_kind, "target_key": target_key, "raw_candidate_rank": 1,
    }


def _piece(edge: dict[str, object], *, od: float = 0.2) -> dict[str, object]:
    return {
        "pipe_piece_key": "pipe:" + str(edge["atomic_edge_key"]),
        "source_entity_key": edge["source_entity_key"],
        "source_atomic_edge_key": edge["atomic_edge_key"],
        "piece_ordinal": 1,
        "start_node_key": edge["start_endpoint_key"],
        "end_node_key": edge["end_endpoint_key"],
        "start_x_m": edge["start_x_m"],
        "start_y_m": edge["start_y_m"],
        "start_z_m": edge["start_z_m"],
        "end_x_m": edge["end_x_m"],
        "end_y_m": edge["end_y_m"],
        "end_z_m": edge["end_z_m"],
        "length_m": edge["length_3d_m"],
        "unit_x": edge["unit_x"],
        "unit_y": edge["unit_y"],
        "unit_z": edge["unit_z"],
        "outer_diameter_m": od,
        "pressure_normalized": "LOW",
        "material": "PE",
        "installation": "BURIED",
        "connection_method": "WELDED",
        "current_status": "ACTIVE",
        "business_location": "道路",
    }


def _event(
    kind: str,
    *,
    target: str,
    source: str | None = None,
) -> dict[str, object]:
    return {
        "product_event_key": f"node:{target}:{kind.lower()}",
        "event_kind": kind,
        "status": "accepted",
        "reason_code": "unit_witness",
        "origin_kind": "source" if source else "derived",
        "source_entity_key": source,
        "target_kind": "quantized_endpoint",
        "target_key": target,
        "host_atomic_edge_key": None,
        "x_m": 0.0,
        "y_m": 0.0,
        "z_m": 0.0,
        "pressure_normalized": "LOW",
        "outer_diameter_m": 0.2,
        "material": "PE",
        "business_location": "道路",
    }


def _current_stage_decisions() -> dict[str, object]:
    return {
        "geometry": {
            "minimum_fitting_trim_m": 0.04,
            "fitting_trim_factor_on_max_od": 1.5,
            "regulator_reference_interface_od_m": 0.15,
        },
        "component_templates": {
            "regulator": {
                "reference_interface_span_m": 1.787,
                "reference_body_height_m": 3.9640332294847935,
                "target_height_factor_on_max_od": 4.0,
                "target_height_min_m": 0.65,
                "target_height_max_m": 1.2,
                "minimum_interface_clearance_m": 0.05,
            }
        },
    }


def _source_dispositions(canonical):
    return [
        {
            "source_disposition_key": "srcdisp:" + str(row["source_entity_key"]),
            "source_entity_key": row["source_entity_key"],
            "source_entity_type": row["source_entity_type"],
            "source_layer": "unit",
            "ingestion_disposition": "accepted",
            "modeling_disposition": "modeled",
            "reason_code": "accepted",
            "canonical_source_entity_key": None,
            "source_business_id": row["source_entity_key"],
            "details": "unit witness",
        }
        for row in canonical
    ]


def _source_fidelity_inputs(ctx: _Context) -> dict:
    canonical = ctx.tables["ce"].to_pylist()
    return {
        "geometry_elements": ctx.tables["ge"].to_pylist(),
        "canonical_entities": canonical,
        "source_dispositions": _source_dispositions(canonical),
        "atomic_edges": ctx.tables["ae"].to_pylist(),
        "endpoints": ctx.tables["ep"].to_pylist(),
        "source_unit_to_m": ctx.params["source_unit_to_m"],
        "point_candidates": ctx.tables["ptps"].to_pylist(),
        "branch_candidates": ctx.tables["eops"].to_pylist(),
        **{name: ctx.outputs[name].to_pylist() for name in (
            "pipe_pieces", "pipe_path_vertices", "product_events", "host_cuts", "candidate_decisions")},
    }


def test_approved_3709_od_reclassifies_source_node_and_reaches_writer() -> None:
    missing_source = "pipe:other-missing-od"
    paths = {
        "entrec_00003711": [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)],
        "entrec_00003709": [(1.0, 0.0, 0.0), (2.0, 0.0, 0.0)],
        missing_source: [(10.0, 0.0, 0.0), (11.0, 0.0, 0.0)],
    }
    canonical = [
        _canonical("entrec_00003711", od=0.1),
        _canonical("entrec_00003709", od=None),
        _canonical(missing_source, od=None),
    ]
    edges = []
    endpoints = []
    geometry = []
    for source, points in paths.items():
        source_edges, source_endpoints = _measurement_path(source, points)
        edges.extend(source_edges)
        endpoints.extend(source_endpoints)
        geometry.extend(_geometry_path(source, points, entity_type="LINE"))
    for endpoint in endpoints:
        if (endpoint["source_entity_key"] == "entrec_00003711" and endpoint["side"] == "end"
                or endpoint["source_entity_key"] == "entrec_00003709" and endpoint["side"] == "start"):
            endpoint["quantized_endpoint_key"] = "q:-63826460:1710189:0"
    topology = _topology_context(canonical=canonical, geometry=geometry, edges=edges, endpoints=endpoints)
    _module("topology_derivation").transform(topology)
    assert next(row for row in topology.tables["ce"].to_pylist()
                if row["source_entity_key"] == "entrec_00003709")["outer_diameter_m"] is None
    pieces = topology.outputs["pipe_pieces"].to_pylist()
    approved = next(row for row in pieces if row["source_entity_key"] == "entrec_00003709")
    assert approved["outer_diameter_m"] == 0.1
    assert approved["diameter_authority"] == "user_approved_peer_od_20260926:entrec_00003709"
    assert json.loads(approved["diameter_evidence_json"])["peer_source_entity_key"] == "entrec_00003711"
    other = next(row for row in pieces if row["source_entity_key"] == missing_source)
    assert (other["outer_diameter_m"], other["diameter_authority"]) == (None, None)
    event = next(row for row in topology.outputs["product_events"].to_pylist()
                 if row["product_event_key"] == "node:q:-63826460:1710189:0")
    assert event["event_kind"] == "CONNECTOR"
    assert event["reason_code"] == "approved_turn_at_most_2deg"
    _assert_source_fidelity(topology)

    connectivity = _connectivity_context(topology, canonical=canonical, endpoints=endpoints)
    _module("product_connectivity").transform(connectivity)
    segments = [row for row in connectivity.outputs["connection_segments"].to_pylist()
                if row["source_entity_key"] == "entrec_00003709"]
    assert segments and {row["diameter_authority"] for row in segments} == {approved["diameter_authority"]}
    ports = [row for row in connectivity.outputs["world_ports"].to_pylist()
             if row["owner_product_key"] == approved["pipe_piece_key"]]
    assert ports and {row["diameter_authority"] for row in ports} == {approved["diameter_authority"]}
    writer, _ = _writer_tables(canonical=canonical, connectivity=connectivity)
    geometry_input = next(row for row in writer["geometry_inputs"]
                          if row["product_key"] == approved["pipe_piece_key"])
    assert geometry_input["outer_diameter_m"] == 0.1
    assert geometry_input["diameter_authority"] == approved["diameter_authority"]


def test_approved_3709_od_rejects_changed_canonical_peer() -> None:
    source = "entrec_00003709"
    peer = "entrec_00003711"
    canonical = [_canonical(source, od=None), _canonical(peer, od=0.08)]
    topology = _topology_context(canonical=canonical, geometry=[], edges=[], endpoints=[])
    with pytest.raises(ValueError, match="APPROVED_PIPE_OD_SOURCE_MISMATCH"):
        _module("topology_derivation").transform(topology)


def test_approved_09179_second_side_preserves_distinct_endpoint_and_no_cut() -> None:
    valve = "entrec_00009179"
    pipe_102 = "entrec_00000102"
    pipe_101 = "entrec_00000101"
    gap = 0.00361247610399726
    paths = {
        pipe_102: [(-1.0, 0.0, 0.0), (0.0, 0.0, 0.0)],
        pipe_101: [(gap, 0.0, 0.0), (1.0 + gap, 0.0, 0.0)],
    }
    geometry = [_point_geometry(valve, (0.0, 0.0, 0.0))]
    edges = []
    endpoints = []
    for source, points in paths.items():
        source_edges, source_endpoints = _measurement_path(
            source, points, edge_keys=[f"edge:{source}:000000"]
        )
        edges.extend(source_edges)
        endpoints.extend(source_endpoints)
        geometry.extend(_geometry_path(source, points, entity_type="LINE"))
    for endpoint in endpoints:
        if endpoint["source_entity_key"] == pipe_102 and endpoint["side"] == "end":
            endpoint["quantized_endpoint_key"] = "q:-61482347:7497085:0"
        elif endpoint["source_entity_key"] == pipe_101 and endpoint["side"] == "start":
            endpoint["quantized_endpoint_key"] = "q:-61482344:7497121:0"
    by_source = {row["source_entity_key"]: row for row in edges}
    host = _point_candidate(
        "point-to-path:3251026d9581ec5c7a902a5189e3b59a8722825f94cdcb008c5732eb46e3c75f",
        valve, by_source[pipe_102], target_kind="quantized_endpoint",
        target_key="q:-61482347:7497085:0",
    )
    second = _point_candidate(
        "point-to-path:7649762646b60903f4acd473664bf55d37be21f961b53407892b3c2f9c8970f6",
        valve, by_source[pipe_101], target_kind="quantized_endpoint",
        target_key="q:-61482344:7497121:0", distance=gap,
    )
    for candidate, parameter, projection_x in ((host, 1.0, 0.0), (second, 0.0, gap)):
        candidate.update(point_x_m=0.0, point_y_m=0.0, point_z_m=0.0,
                         projection_x_m=projection_x, projection_y_m=0.0,
                         projection_z_m=0.0, host_parameter=parameter)
    canonical = [_canonical(pipe_102, od=0.1), _canonical(pipe_101, od=0.1),
                 _canonical(valve, od=0.1, role="valve")]
    topology = _topology_context(canonical=canonical, geometry=geometry, edges=edges,
                                 endpoints=endpoints, point_to_path=pa.Table.from_pylist([host, second]))
    _module("topology_derivation").transform(topology)
    event = next(row for row in topology.outputs["product_events"].to_pylist()
                 if row["product_event_key"] == "point:" + valve)
    assert (event["host_atomic_edge_key"], event["target_key"], event["x_m"]) == (
        by_source[pipe_102]["atomic_edge_key"], "q:-61482347:7497085:0", 0.0)
    decisions = {row["candidate_key"]: row for row in topology.outputs["candidate_decisions"].to_pylist()}
    assert decisions[host["point_to_path_key"]]["decision_kind"] == "ATTACH"
    assert decisions[second["point_to_path_key"]]["decision_kind"] == "APPROVED_SECOND_CONNECTION_SIDE"
    vertices = topology.outputs["pipe_path_vertices"].to_pylist()
    source_vertices = {source: [row for row in vertices if row["source_entity_key"] == source
                                and event["product_event_key"] in row["event_keys"]]
                       for source in (pipe_102, pipe_101)}
    assert all(len(rows) == 1 for rows in source_vertices.values())
    assert math.dist(tuple(source_vertices[pipe_102][0][axis + "_m"] for axis in "xyz"),
                     tuple(source_vertices[pipe_101][0][axis + "_m"] for axis in "xyz")) == pytest.approx(gap)
    assert not any(row["host_source_entity_key"] == pipe_101
                   for row in topology.outputs["host_cuts"].to_pylist())
    assert not any(row["relation_family"] == "endpoint_snap" and row["accepted"]
                   for row in topology.outputs["candidate_decisions"].to_pylist())
    _assert_source_fidelity(topology)

    connectivity = _connectivity_context(topology, canonical=canonical, endpoints=endpoints)
    _module("product_connectivity").transform(connectivity)
    valve_ports = [row for row in connectivity.outputs["world_ports"].to_pylist()
                   if row["owner_product_key"] == "product:point:" + valve]
    assert {row["source_path_key"] for row in valve_ports} == {"path:" + pipe_101, "path:" + pipe_102}
    assert {row["x_m"] for row in valve_ports} == {0.0, gap}
    assert {row["retained_side"] for row in valve_ports} == {"source_forward", "source_backward"}
    assert sum(row["connection_scope"] == "source_gas_pairing"
               for row in connectivity.outputs["connection_pairs"].to_pylist()) >= 2
    assert not any(row["issue_code"] == "UNRESOLVED_EVENT_INCIDENCE"
                   and row["product_key"] == "product:point:" + valve
                   for row in connectivity.outputs["connectivity_issues"].to_pylist())


@pytest.mark.parametrize("middle,accepted", [((1.0,0.001,0.0),True),
    ((1.0,0.001000001,0.0),False), ((3.0,0.0,0.0),False), ((-1.0,0.0,0.0),False)])
def test_normalization_finite_span_budget_and_order(middle, accepted):
    raw=[(0.0,0.0,0.0),middle,(2.0,0.0,0.0)]
    result=_module("topology_derivation")._finite_path_candidate(raw,{0:raw[0],2:raw[2]})
    assert (result is not None)==accepted


def test_normalization_checks_cumulative_raw_budget():
    module=_module("topology_derivation")
    raw=[(0.0,0.0,0.0),(1.0,0.0,0.0),(2.0,0.0,0.0)]
    first=module._finite_path_candidate(raw,{0:(0.0,0.0008,0.0),2:(2.0,0.0008,0.0)})
    assert first is not None
    assert module._finite_path_candidate(raw,{0:(0.0,0.0016,0.0),2:(2.0,0.0016,0.0)}) is None


def test_normalization_microfold_reaches_writer_and_preserves_raw_evidence():
    from tests.scenarios.family_baseline_input.transformation import compile_tables
    from tests.scenarios.family_baseline_input.realization_projection import project_realization_rows

    source="microfold"
    points=[(0.0,0.0,0.0),(1.0,0.0005,0.0),(2.0,0.0,0.0)]
    edges,endpoints=_measurement_path(source,points)
    canonical=[_canonical(source,od=0.1)]
    canonical[0]["source_entity_type"]="LWPOLYLINE"
    ctx=_topology_context(canonical=canonical,geometry=_geometry_path(source,points,entity_type="LWPOLYLINE"),edges=edges,endpoints=endpoints)
    _module("topology_derivation").transform(ctx)
    _assert_source_fidelity(ctx)
    vertices=ctx.outputs["pipe_path_vertices"].to_pylist()
    assert [(row["x_m"],row["y_m"],row["z_m"]) for row in vertices]==[points[0],points[-1]]
    decision,=ctx.outputs["candidate_decisions"].to_pylist()
    note=json.loads(decision["normalization_evidence_json"])
    assert note["original_vertices_m"]==[list(point) for point in points]
    assert note["max_deviation_m"]==0.0005
    assert ctx.tables["ge"].to_pylist()[1]["y"]==0.0005
    connectivity=_connectivity_context(ctx,canonical=canonical,endpoints=endpoints)
    _module("product_connectivity").transform(connectivity)
    tables,closure_inputs=_writer_tables(canonical=canonical,connectivity=connectivity)
    assert validate_writer_closure(tables,**closure_inputs)["passed"]
    tables["product_instances"]=connectivity.outputs["product_instances"].to_pylist()
    arrow_tables={name:pa.Table.from_pylist(rows) for name,rows in tables.items()}
    baseline=compile_tables(arrow_tables,"a"*64)
    projection=project_realization_rows({**baseline,"ports":arrow_tables["ports"]},
        family_baseline_package_id="baseline",writer_package_id="writer")
    row,=projection["realization_rows"].to_pylist()
    assert json.loads(row["parameters_json"])["world_points_m"]==[list(points[0]),list(points[-1])]
    corrupted=_source_fidelity_inputs(ctx)
    corrupted["candidate_decisions"]=[]
    assert not validate_source_path_fidelity(**corrupted)["passed"]


@pytest.mark.parametrize("fault", ["raw_finite_overshoot", "missing_fixed_anchor", "unknown_source"])
def test_normalization_fidelity_rejects_forged_correspondence(fault):
    source="forged"
    points=([(0.0,0.0,0.0),(0.0022,0.0,0.0),(0.002,0.0,0.0)] if fault=="raw_finite_overshoot"
            else [(0.0,0.0,0.0),(1.0,0.0005,0.0),(2.0,0.0,0.0)])
    edges,endpoints=_measurement_path(source,points)
    canonical=[{**_canonical(source,od=0.1),"source_entity_type":"LWPOLYLINE"}]
    ctx=_topology_context(canonical=canonical,geometry=_geometry_path(source,points,entity_type="LWPOLYLINE"),edges=edges,endpoints=endpoints)
    _module("topology_derivation").transform(ctx)
    inputs=_source_fidelity_inputs(ctx)
    if fault=="raw_finite_overshoot":
        effective=[points[0],(0.0018,0.0,0.0),points[2]]
        note={"authority":"m4-engineering-spatial-normalization-1mm","source_path_key":"path:"+source,
            "source_entity_key":source,"original_vertices_m":points,"effective_vertices_m":effective,
            "retained_vertex_ordinals":[0,2],"fixed_vertex_ordinals":[0,2],
            "max_deviation_m":max(math.dist(a,b) for a,b in zip(points,effective)),"budget_m":0.001,
            "node_map":{},"collapsed_atomic_edge_keys":[]}
        inputs["candidate_decisions"]=[{"relation_family":"source_path_normalization","subject_key":source,
            "normalization_evidence_json":json.dumps(note)}]
    else:
        decision=next(row for row in inputs["candidate_decisions"] if row["relation_family"]=="source_path_normalization")
        note=json.loads(decision["normalization_evidence_json"])
        if fault=="missing_fixed_anchor": note["fixed_vertex_ordinals"]=[]
        else:
            decision["subject_key"]="unknown"
            note.update(source_entity_key="unknown",source_path_key="path:unknown")
        decision["normalization_evidence_json"]=json.dumps(note)
    result=validate_source_path_fidelity(**inputs)
    assert not result["passed"]
    assert any(row["code"].startswith("SOURCE_NORMALIZATION_") for row in result["errors"])


@pytest.mark.parametrize("collapse", [False,True])
def test_normalization_existing_node_preserves_three_ports_and_source_od(collapse):
    paths={"left":[(-2.0,0.0,0.0),(0.0,0.0,0.0)],
           "right":([(0.0,0.0,0.0),(0.0002,0.0002,0.0),(2.0,0.0,0.0)] if collapse
                    else [(0.0,0.0,0.0),(2.0,0.0005,0.0)]),
           "branch":([(0.0002,0.0002,0.0),(0.0002,1.0,0.0)] if collapse
                     else [(0.0,0.0,0.0),(0.0,1.0,0.0)])}
    canonical=[];geometry=[];edges=[];endpoints=[]
    for source,points in paths.items():
        entity_type="LWPOLYLINE" if len(points)>2 else "LINE"
        canonical.append({**_canonical(source,od=.1 if source=="branch" else .3),"source_entity_type":entity_type})
        geometry.extend(_geometry_path(source,points,entity_type=entity_type))
        source_edges,source_endpoints=_measurement_path(source,points)
        edges.extend(source_edges);endpoints.extend(source_endpoints)
    for row in endpoints:
        source=row["source_entity_key"];ordinal=next(edge["segment_ordinal"] for edge in edges if edge["atomic_edge_key"]==row["atomic_edge_key"])
        if source=="left" and row["side"]=="end":row["quantized_endpoint_key"]="q:upstream"
        if source=="right" and ordinal==0 and row["side"]=="start":row["quantized_endpoint_key"]="q:upstream"
        if collapse and source=="right" and ((ordinal==0 and row["side"]=="end") or (ordinal==1 and row["side"]=="start")):
            row["quantized_endpoint_key"]="q:branch"
        if source=="branch" and row["side"]=="start":row["quantized_endpoint_key"]="q:branch" if collapse else "q:upstream"
    ctx=_topology_context(canonical=canonical,geometry=geometry,edges=edges,endpoints=endpoints)
    _module("topology_derivation").transform(ctx)
    _assert_source_fidelity(ctx)
    events=ctx.outputs["product_events"].to_pylist()
    assert [row["event_kind"] for row in events]==["TEE"]
    assert all(row["length_m"]>0 for row in ctx.outputs["pipe_pieces"].to_pylist())
    connectivity=_connectivity_context(ctx,canonical=canonical,endpoints=endpoints)
    _module("product_connectivity").transform(connectivity)
    product=next(row for row in connectivity.outputs["product_instances"].to_pylist() if row["product_kind"]=="TEE")
    ports=[row for row in connectivity.outputs["world_ports"].to_pylist() if row["owner_product_key"]==product["product_key"]]
    assert sorted(row["outer_diameter_m"] for row in ports)==[.1,.3,.3]
    assert {row["port_role"] for row in ports}=={"MAIN_A","MAIN_B","BRANCH"}
    assert len({(row["x_m"],row["y_m"],row["z_m"]) for row in ports})==1
    tables,closure=_writer_tables(canonical=canonical,connectivity=connectivity)
    assert validate_writer_closure(tables,**closure)["passed"]


@pytest.mark.parametrize("eligible,shared_node,expected_change", [(False,False,True),(True,False,False),(False,True,False)])
def test_normalization_protects_eligible_eop_and_real_node_but_not_guard_rejected_candidate(eligible,shared_node,expected_change):
    module=_module("topology_derivation")
    source="protected"
    points=[(0.0,0.0,0.0),(1.0,0.0001,0.0),(2.0,0.0,0.0)]
    edges,endpoints=_measurement_path(source,points)
    geometry=_geometry_path(source,points,entity_type="LWPOLYLINE")
    canonical={source:{**_canonical(source,od=.3),"source_entity_type":"LWPOLYLINE"}}
    if shared_node:
        branch_edges,branch_endpoints=_measurement_path("branch",[points[1],(1.,1.,0.)])
        branch_endpoints[0]["quantized_endpoint_key"]=endpoints[1]["quantized_endpoint_key"]
        edges+=branch_edges;endpoints+=branch_endpoints
        geometry+=_geometry_path("branch",[points[1],(1.,1.,0.)],entity_type="LINE")
        canonical["branch"]=_canonical("branch",od=.1)
    paths,_=module._source_paths(canonical,geometry,edges,1.)
    candidates=[{"host_atomic_edge_key":edges[0]["atomic_edge_key"],"branch_atomic_edge_key":edges[1]["atomic_edge_key"],
                 "passes_host_endpoint_guard":eligible}]
    decisions=module._normalize_source_paths(paths,endpoints,[],candidates)
    assert bool(decisions)==expected_change
    if not expected_change: assert paths[source]["vertices"][1]["coord"]==points[1]


@pytest.mark.parametrize("separate_support", [True,False])
def test_normalization_reuses_source_only_outside_prior_finite_support(separate_support):
    module=_module("topology_derivation")
    middle=[(0.,.0002,0.),(1.,0.,0.),(2.,.0002,0.)] if separate_support else [(0.,.0002,0.),(2.,.0002,0.)]
    raw={"middle":middle,"left":[(-1.,0.,0.),middle[0]],"right":[middle[-1],(3.,0.,0.)]}
    canonical={};geometry=[];edges=[];endpoints=[]
    for source,points in raw.items():
        kind="LWPOLYLINE" if len(points)>2 else "LINE"
        canonical[source]={**_canonical(source,od=.3),"source_entity_type":kind}
        geometry.extend(_geometry_path(source,points,entity_type=kind))
        es,eps=_measurement_path(source,points);edges.extend(es);endpoints.extend(eps)
        for endpoint in eps:
            xyz=tuple(endpoint[axis+"_m"] for axis in "xyz")
            if xyz==middle[-1]:endpoint["quantized_endpoint_key"]="q:a-first"
            elif xyz==middle[0]:endpoint["quantized_endpoint_key"]="q:b-second"
    paths,_=module._source_paths(canonical,geometry,edges,1.)
    decisions=[]
    module._normalize_connected_nodes(paths,endpoints,canonical,[],[],decisions)
    note=json.loads(next(row for row in decisions if row["subject_key"]=="middle")["normalization_evidence_json"])
    assert len(note["node_map"])==(2 if separate_support else 1)
    assert note["original_vertices_m"]==[list(point) for point in middle]
    assert note["max_deviation_m"]==max(math.dist(a,b) for a,b in zip(middle,note["effective_vertices_m"]))
    if separate_support:
        assert note["effective_vertices_m"][1]==list(middle[1])
        assert note["effective_vertices_m"][2]==[2.,0.,0.]
    else:
        assert note["effective_vertices_m"][0]==list(middle[0])


@pytest.mark.parametrize("selected_here", [False,True])
def test_normalization_connector_preserves_material_boundary_and_selected_anode(selected_here):
    raw={"left":[(-1.,0.,0.),(0.,.0001,0.)],"right":[(0.,.0001,0.),(1.,0.,0.)],
         "remote":[(10.,0.,0.),(12.,0.,0.)]}
    canonical=[];geometry=[];edges=[];endpoints=[]
    for source,points in raw.items():
        facts=_canonical(source,od=.3)
        if source=="right":facts.update(material="DUCTILE_IRON",connection_method="MECHANICAL")
        canonical.append(facts);geometry.extend(_geometry_path(source,points,entity_type="LINE"))
        es,eps=_measurement_path(source,points);edges.extend(es);endpoints.extend(eps)
        for endpoint in eps:
            if tuple(endpoint[axis+"_m"] for axis in "xyz")==raw["left"][-1]:
                endpoint["quantized_endpoint_key"]="q:boundary"
    anode=_canonical("anode:selected",od=.3,role="anode");canonical.append(anode)
    candidates=[_point_candidate("point:"+source,"anode:selected",edge,target_kind="edge_interior",
        target_key="interior:"+source,distance=0. if (source=="left")==selected_here else 3.)
        for source,edge in zip(raw,edges) if source in ("left","remote")]
    ctx=_topology_context(canonical=canonical,geometry=geometry,edges=edges,endpoints=endpoints,
        point_to_path=pa.Table.from_pylist(candidates))
    _module("topology_derivation").transform(ctx)
    _assert_source_fidelity(ctx)
    decisions=ctx.outputs["candidate_decisions"].to_pylist()
    assert bool([row for row in decisions if row["relation_family"]=="source_path_normalization"]) is (not selected_here)
    boundary=next(row for row in ctx.outputs["product_events"].to_pylist() if row["product_event_key"]=="node:q:boundary")
    assert boundary["event_kind"]=="CONNECTOR"
    assert boundary["reason_code"]=="non_diameter_business_attribute_change"
    assert [row["material"] for row in ctx.tables["ce"].to_pylist()[:2]]==["PE","DUCTILE_IRON"]
    attached=[row for row in decisions if row["relation_family"]=="point_to_path" and row["accepted"]]
    assert [row["candidate_key"] for row in attached]==["point:left" if selected_here else "point:remote"]


def _assert_source_fidelity(ctx: _Context) -> None:
    result = validate_source_path_fidelity(**_source_fidelity_inputs(ctx))
    assert result["passed"], result


def _writer_tables(
    *,
    canonical: list[dict[str, object]],
    connectivity: _Context,
    xdata: list[dict[str, object]] | None = None,
    reverse_inputs: bool = False,
) -> tuple[dict[str, list[dict]], dict[str, object]]:
    writer = _module("writer_input_closure")
    dispositions = _source_dispositions(canonical)
    xdata = xdata or []
    inputs = {
        "sd": pa.Table.from_pylist(dispositions),
        "ce": pa.Table.from_pylist(canonical),
        "cx": pa.Table.from_pylist(xdata) if xdata else _empty(),
        "pieces": connectivity.tables["pieces"],
        "vertices": connectivity.tables["vertices"],
        "events": connectivity.tables["events"],
        "pi": connectivity.outputs["product_instances"],
        "origins": connectivity.outputs["product_origins"],
        "segments": connectivity.outputs["connection_segments"],
        "wp": connectivity.outputs["world_ports"],
        "cn": connectivity.outputs["connection_pairs"],
        "issues": connectivity.outputs["connectivity_issues"],
    }
    if reverse_inputs:
        inputs = {
            name: (
                pa.Table.from_pylist(list(reversed(table.to_pylist())), schema=table.schema)
                if table.num_rows
                else table
            )
            for name, table in inputs.items()
        }
    ctx = _Context(
        inputs
    )
    writer.transform(ctx)
    tables = {name: table.to_pylist() for name, table in ctx.outputs.items()}
    validation_context = {
        "product_instances": connectivity.outputs["product_instances"].to_pylist(),
        "product_origins": connectivity.outputs["product_origins"].to_pylist(),
        "connection_segments": connectivity.outputs["connection_segments"].to_pylist(),
        "world_ports": connectivity.outputs["world_ports"].to_pylist(),
        "connection_pairs": connectivity.outputs["connection_pairs"].to_pylist(),
        "connectivity_issues": connectivity.outputs["connectivity_issues"].to_pylist(),
        "pipe_pieces": connectivity.tables["pieces"].to_pylist(),
        "pipe_path_vertices": connectivity.tables["vertices"].to_pylist(),
        "product_events": connectivity.tables["events"].to_pylist(),
        "baseline_source_dispositions": dispositions,
        "canonical_entities": canonical,
        "canonical_xdata": xdata,
    }
    return tables, validation_context


# Frozen from the accepted M4 Seed rooted at
# def9554475e356131f57966f762a2082e97d4732940721585dd73001653e2851.
# Source commit: 26f1ab1d2ad8f0f296f0300eda6e426890972895.
# Source CAD SHA256: 02a34f9a4357b814fcdec7eb2db6b6b6b762c888ffbf446c55904485e5d87c25.
# geometry_elements member SHA256:
# c8e08fa0de71d941007e696af0b8e97c28b64dffe4d50bc28d9bb148ad1baee4.
_REAL_19_POINT_SOURCE = "entrec_00005798"
# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


def test_polyline_device_vertex_and_different_source_branch_keep_exact_path_loci() -> None:
    source = "pipe:device-host"
    points = [
        (0.0, 0.0, 0.0),
        (2.0, 0.0, 0.0),
        (4.0, 0.0, 0.0),
        (4.0, 3.0, 0.0),
        (6.0, 3.0, 0.0),
        (8.0, 3.0, 0.0),
        (8.0, 5.0, 0.0),
    ]
    geometry = _geometry_path(source, points, entity_type="LWPOLYLINE")
    edges, endpoints = _measurement_path(source, points)
    target = f"q:{source}:00003"
    candidates = []
    for ordinal, (edge, parameter) in enumerate(((edges[2], 1.0), (edges[3], 0.0))):
        row = _point_candidate(
            f"point:valve:{ordinal}",
            "valve:vertex",
            edge,
            target_kind="quantized_endpoint",
            target_key=target,
        )
        row.update(
            point_x_m=4.0,
            point_y_m=3.0,
            projection_x_m=4.0,
            projection_y_m=3.0,
            host_parameter=parameter,
        )
        candidates.append(row)
    anode = _point_candidate(
        "point:anode:vertex",
        "anode:vertex",
        edges[2],
        target_kind="edge_interior",
        target_key=edges[2]["atomic_edge_key"],
    )
    anode.update(
        point_x_m=4.0,
        point_y_m=3.0,
        projection_x_m=4.0,
        projection_y_m=3.0,
        host_parameter=1.0,
    )
    candidates.append(anode)
    device = _topology_context(
        canonical=[
            _canonical(source, od=0.1, pressure="MEDIUM", entity_type="LWPOLYLINE"),
            _canonical("valve:vertex", od=0.1, pressure="MEDIUM", role="valve"),
            _canonical("anode:vertex", od=0.1, pressure="MEDIUM", role="anode"),
        ],
        geometry=geometry,
        edges=edges,
        endpoints=endpoints,
        point_to_path=pa.Table.from_pylist(candidates),
    )
    _module("topology_derivation").transform(device)
    device_pieces = device.outputs["pipe_pieces"].to_pylist()
    device_vertices = device.outputs["pipe_path_vertices"].to_pylist()
    valve_key = next(
        row["product_event_key"]
        for row in device.outputs["product_events"].to_pylist()
        if row["event_kind"] == "VALVE" and row["status"] == "accepted"
    )
    anode_key = next(
        row["product_event_key"]
        for row in device.outputs["product_events"].to_pylist()
        if row["event_kind"] == "ANODE" and row["status"] == "accepted"
    )
    boundary_event_keys = sorted([anode_key, valve_key])
    assert len(device_pieces) == 2
    assert sum(row["length_m"] for row in device_pieces) == pytest.approx(13.0)
    assert device_pieces[0]["end_event_keys"] == boundary_event_keys
    assert device_pieces[1]["start_event_keys"] == boundary_event_keys
    assert [
        row["geometry_element_key"]
        for row in device_vertices
        if row["pipe_piece_key"] == device_pieces[0]["pipe_piece_key"]
    ] == [_geometry_key(source, ordinal) for ordinal in range(4)]
    assert [
        row["geometry_element_key"]
        for row in device_vertices
        if row["pipe_piece_key"] == device_pieces[1]["pipe_piece_key"]
    ] == [_geometry_key(source, ordinal) for ordinal in range(3, 7)]
    assert (
        device_pieces[0]["end_segment_ordinal"],
        device_pieces[0]["end_parameter"],
        device_pieces[1]["start_segment_ordinal"],
        device_pieces[1]["start_parameter"],
    ) == (2, 1.0, 3, 0.0)
    valve_vertices = [row for row in device_vertices if row["event_keys"] == boundary_event_keys]
    assert len(valve_vertices) == 2
    assert {row["geometry_element_key"] for row in valve_vertices} == {_geometry_key(source, 3)}
    _assert_source_fidelity(device)
    shuffled_device = _topology_context(
        canonical=list(
            reversed(
                [
                    _canonical(source, od=0.1, pressure="MEDIUM", entity_type="LWPOLYLINE"),
                    _canonical("valve:vertex", od=0.1, pressure="MEDIUM", role="valve"),
                    _canonical("anode:vertex", od=0.1, pressure="MEDIUM", role="anode"),
                ]
            )
        ),
        geometry=list(reversed(geometry)),
        edges=list(reversed(edges)),
        endpoints=list(reversed(endpoints)),
        point_to_path=pa.Table.from_pylist(list(reversed(candidates))),
    )
    _module("topology_derivation").transform(shuffled_device)
    assert device.outputs.keys() == shuffled_device.outputs.keys()
    for name in device.outputs:
        assert device.outputs[name].to_pylist() == shuffled_device.outputs[name].to_pylist()

    host = "pipe:branch-host"
    branch = "pipe:real-branch"
    host_points = [(0.0, 0.0, 0.0), (4.0, 0.0, 0.0), (8.0, 0.0, 0.0)]
    branch_points = [(4.0, -2.0, 0.0), (4.0, 0.0, 0.0)]
    host_edges, host_endpoints = _measurement_path(host, host_points)
    branch_edges, branch_endpoints = _measurement_path(branch, branch_points)
    shared_node = f"q:{host}:00001"
    branch_endpoints[-1]["quantized_endpoint_key"] = shared_node
    topology = _topology_context(
        canonical=[
            _canonical(host, od=0.1, pressure="MEDIUM", entity_type="LWPOLYLINE"),
            _canonical(branch, od=0.1, pressure="MEDIUM"),
        ],
        geometry=(
            _geometry_path(host, host_points, entity_type="LWPOLYLINE")
            + _geometry_path(branch, branch_points, entity_type="LINE")
        ),
        edges=host_edges + branch_edges,
        endpoints=host_endpoints + branch_endpoints,
    )
    _module("topology_derivation").transform(topology)
    tee = next(row for row in topology.outputs["product_events"].to_pylist() if row["event_kind"] == "TEE")
    host_pieces = [
        row for row in topology.outputs["pipe_pieces"].to_pylist()
        if row["source_entity_key"] == host
    ]
    assert tee["target_key"] == shared_node
    assert len(host_pieces) == 2
    assert sum(row["length_m"] for row in host_pieces) == pytest.approx(8.0)
    assert host_pieces[0]["end_event_keys"] == [tee["product_event_key"]]
    assert host_pieces[1]["start_event_keys"] == [tee["product_event_key"]]
    _assert_source_fidelity(topology)


def test_meter_is_zero_width_boundary_and_service_saddle_does_not_cut_host() -> None:
    meter_source = "pipe:meter-host"
    meter_points = [
        (0.0, 0.0, 0.0),
        (2.0, 0.0, 0.0),
        (6.0, 0.0, 0.0),
        (6.0, 3.0, 0.0),
        (8.0, 3.0, 0.0),
    ]
    meter_edges, meter_endpoints = _measurement_path(meter_source, meter_points)
    meter_ctx = _topology_context(
        canonical=[_canonical(meter_source, od=0.2, entity_type="LWPOLYLINE")],
        geometry=_geometry_path(meter_source, meter_points, entity_type="LWPOLYLINE"),
        edges=meter_edges,
        endpoints=meter_endpoints,
    )
    _module("topology_derivation").transform(meter_ctx)
    meter = next(row for row in meter_ctx.outputs["product_events"].to_pylist() if row["event_kind"] == "GAS_METER")
    pieces = meter_ctx.outputs["pipe_pieces"].to_pylist()
    event_vertices = [
        row for row in meter_ctx.outputs["pipe_path_vertices"].to_pylist()
        if meter["product_event_key"] in row["event_keys"]
    ]
    assert len(pieces) == 2
    assert [row["length_m"] for row in pieces] == pytest.approx([4.0, 7.0])
    assert pieces[0]["end_x_m"] == pieces[1]["start_x_m"] == 4.0
    assert pieces[0]["end_event_keys"] == pieces[1]["start_event_keys"] == [meter["product_event_key"]]
    assert (
        pieces[0]["start_segment_ordinal"],
        pieces[0]["start_parameter"],
        pieces[0]["end_segment_ordinal"],
        pieces[0]["end_parameter"],
    ) == (0, 0.0, 1, 0.5)
    assert (
        pieces[1]["start_segment_ordinal"],
        pieces[1]["start_parameter"],
        pieces[1]["end_segment_ordinal"],
        pieces[1]["end_parameter"],
    ) == (1, 0.5, 3, 1.0)
    assert len(event_vertices) == 2
    assert all(row["geometry_element_key"] is None for row in event_vertices)
    vertices_by_piece = {
        piece["pipe_piece_key"]: [
            row
            for row in meter_ctx.outputs["pipe_path_vertices"].to_pylist()
            if row["pipe_piece_key"] == piece["pipe_piece_key"]
        ]
        for piece in pieces
    }
    assert [row["sequence_ordinal"] for row in vertices_by_piece[pieces[0]["pipe_piece_key"]]] == [1, 2, 3]
    assert [row["geometry_element_key"] for row in vertices_by_piece[pieces[0]["pipe_piece_key"]]] == [
        _geometry_key(meter_source, 0),
        _geometry_key(meter_source, 1),
        None,
    ]
    assert [
        (row["source_segment_ordinal"], row["source_parameter"])
        for row in vertices_by_piece[pieces[1]["pipe_piece_key"]]
    ] == [(1, 0.5), (2, 0.0), (3, 0.0), (3, 1.0)]
    _assert_source_fidelity(meter_ctx)

    shuffled_meter = _topology_context(
        canonical=[_canonical(meter_source, od=0.2, entity_type="LWPOLYLINE")],
        geometry=list(
            reversed(_geometry_path(meter_source, meter_points, entity_type="LWPOLYLINE"))
        ),
        edges=list(reversed(meter_edges)),
        endpoints=list(reversed(meter_endpoints)),
    )
    _module("topology_derivation").transform(shuffled_meter)
    assert meter_ctx.outputs.keys() == shuffled_meter.outputs.keys()
    for name in meter_ctx.outputs:
        assert meter_ctx.outputs[name].to_pylist() == shuffled_meter.outputs[name].to_pylist()

    host = "pipe:saddle-host"
    branch = "pipe:saddle-branch"
    host_points = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)]
    branch_points = [(5.0, 0.001, 0.0), (6.0, 1.001, 0.0)]
    host_edges, host_endpoints = _measurement_path(host, host_points)
    branch_edges, branch_endpoints = _measurement_path(branch, branch_points)
    relation = {
        "endpoint_on_path_key": "eop:saddle",
        "branch_endpoint_key": branch_edges[0]["start_endpoint_key"],
        "branch_atomic_edge_key": branch_edges[0]["atomic_edge_key"],
        "branch_source_entity_key": branch,
        "host_atomic_edge_key": host_edges[0]["atomic_edge_key"],
        "host_source_entity_key": host,
        "projection_x_m": 5.0,
        "projection_y_m": 0.0,
        "projection_z_m": 0.0,
        "host_parameter": 0.5,
        "distance_3d_m": 0.001,
        "same_source_entity": False,
        "passes_host_endpoint_guard": True,
    }
    saddle_ctx = _topology_context(
        canonical=[
            _canonical(host, od=0.2),
            _canonical(branch, od=0.05),
        ],
        geometry=(
            _geometry_path(host, host_points, entity_type="LINE")
            + _geometry_path(branch, branch_points, entity_type="LINE")
        ),
        edges=host_edges + branch_edges,
        endpoints=host_endpoints + branch_endpoints,
        endpoint_on_path=pa.Table.from_pylist([relation]),
    )
    _module("topology_derivation").transform(saddle_ctx)
    saddle = next(row for row in saddle_ctx.outputs["product_events"].to_pylist() if row["event_kind"] == "SERVICE_SADDLE")
    saddle_host_pieces = [
        row for row in saddle_ctx.outputs["pipe_pieces"].to_pylist()
        if row["source_entity_key"] == host
    ]
    host_vertices = [
        row for row in saddle_ctx.outputs["pipe_path_vertices"].to_pylist()
        if row["source_entity_key"] == host
    ]
    assert len(saddle_host_pieces) == 1
    assert saddle_host_pieces[0]["length_m"] == pytest.approx(10.0)
    assert not any(row["event_key"] == saddle["product_event_key"] for row in saddle_ctx.outputs["host_cuts"].to_pylist())
    inserted, = [row for row in host_vertices if saddle["product_event_key"] in row["event_keys"]]
    assert inserted["geometry_element_key"] is None
    assert inserted["source_segment_ordinal"] == 0
    assert inserted["source_parameter"] == pytest.approx(0.4999)
    assert saddle["x_m"] == pytest.approx(4.999)
    assert saddle["host_parameter"] == inserted["source_parameter"]
    decision = next(row for row in saddle_ctx.outputs["candidate_decisions"].to_pylist()
                    if row["candidate_key"] == "eop:saddle")
    assert decision["host_parameter"] == saddle["host_parameter"]
    evidence = json.loads(saddle["source_note"])
    assert evidence["original_projection_q_m"] == [5.0, 0.0, 0.0]
    assert evidence["effective_axis_intersection_c_m"] == pytest.approx([4.999, 0.0, 0.0])
    _assert_source_fidelity(saddle_ctx)


@pytest.mark.parametrize("branch_start,branch_end,expected_reason", [
    ((5, 1, 0), (6, 1, 0), "saddle_axes_have_no_unique_intersection"),
    ((5, 1, 1), (5, 2, 1), "saddle_axes_do_not_intersect"),
    ((0.02, 0.02, 0), (1.02, 1.02, 0), "saddle_axis_intersection_outside_original_host_guard"),
])
def test_saddle_c_rejects_missing_intersection_or_original_guard_violation(
    branch_start, branch_end, expected_reason,
) -> None:
    def segment(start, end):
        length = math.dist(start, end)
        return {"start": start, "length": length,
                "tangent": tuple((b-a)/length for a, b in zip(start, end))}
    result, reason = _module("topology_derivation")._saddle_axis_station(
        segment((0, 0, 0), (10, 0, 0)), segment(branch_start, branch_end), 0.01,
    )
    assert result is None and reason == expected_reason


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


def test_crossing_paths_do_not_connect_and_all_outputs_are_row_order_stable() -> None:
    sources = {
        "pipe:horizontal": [(-2.0, 0.0, 0.0), (2.0, 0.0, 0.0)],
        "pipe:vertical": [(0.0, -2.0, 0.0), (0.0, 2.0, 0.0)],
    }
    canonical = []
    geometry = []
    edges = []
    endpoints = []
    for source, points in sources.items():
        canonical.append(_canonical(source, od=0.1, pressure="MEDIUM"))
        geometry.extend(_geometry_path(source, points, entity_type="LINE"))
        source_edges, source_endpoints = _measurement_path(source, points)
        edges.extend(source_edges)
        endpoints.extend(source_endpoints)

    ordered = _topology_context(
        canonical=canonical,
        geometry=geometry,
        edges=edges,
        endpoints=endpoints,
    )
    shuffled = _topology_context(
        canonical=list(reversed(canonical)),
        geometry=list(reversed(geometry)),
        edges=list(reversed(edges)),
        endpoints=list(reversed(endpoints)),
    )
    module = _module("topology_derivation")
    module.transform(ordered)
    module.transform(shuffled)

    assert len(ordered.outputs["pipe_pieces"].to_pylist()) == 2
    assert not any(row["event_kind"] in {"TEE", "CROSS"} for row in ordered.outputs["product_events"].to_pylist())
    assert ordered.outputs.keys() == shuffled.outputs.keys()
    for name in ordered.outputs:
        assert ordered.outputs[name].to_pylist() == shuffled.outputs[name].to_pylist()


def test_same_source_nonadjacent_return_node_does_not_invent_a_business_fitting() -> None:
    source = "pipe:self-return"
    points = [
        (0.0, 0.0, 0.0),
        (2.0, 0.0, 0.0),
        (2.0, 2.0, 0.0),
        (0.0, 2.0, 0.0),
        (0.0, 0.0, 0.0),
        (0.0, -2.0, 0.0),
    ]
    geometry = _geometry_path(source, points, entity_type="LWPOLYLINE")
    edges, endpoints = _measurement_path(source, points)
    repeated_node = f"q:{source}:00000"
    for endpoint in endpoints:
        if (
            endpoint["x_m"],
            endpoint["y_m"],
            endpoint["z_m"],
        ) == (0.0, 0.0, 0.0):
            endpoint["quantized_endpoint_key"] = repeated_node
    ctx = _topology_context(
        canonical=[_canonical(source, od=0.1, pressure="MEDIUM", entity_type="LWPOLYLINE")],
        geometry=geometry,
        edges=edges,
        endpoints=endpoints,
    )
    _module("topology_derivation").transform(ctx)

    repeated_incidence = [
        row for row in endpoints if row["quantized_endpoint_key"] == repeated_node
    ]
    assert len(repeated_incidence) == 3
    assert len({row["atomic_edge_key"] for row in repeated_incidence}) == 3
    assert len(ctx.outputs["pipe_pieces"].to_pylist()) == 1
    assert ctx.outputs["pipe_pieces"].to_pylist()[0]["length_m"] == pytest.approx(10.0)
    assert len(ctx.outputs["pipe_path_vertices"].to_pylist()) == len(points)
    assert not any(
        row["event_kind"] in {"TEE", "CROSS", "BEND"}
        for row in ctx.outputs["product_events"].to_pylist()
    )


def test_connectivity_uses_distinct_path_end_tangents_and_stops_at_a_one_degree_turn() -> None:
    source = "pipe:one-degree-turn"
    angle = math.radians(1.0)
    direction = (math.cos(angle), math.sin(angle), 0.0)
    points = [
        (0.0, 0.0, 0.0),
        (2.0, 0.0, 0.0),
        (4.0, 0.0, 0.0),
        (4.0 + 2.0 * direction[0], 2.0 * direction[1], 0.0),
        (4.0 + 4.0 * direction[0], 4.0 * direction[1], 0.0),
    ]
    canonical = [
        _canonical(source, od=0.12, pressure="MEDIUM", entity_type="LWPOLYLINE")
    ]
    edges, endpoints = _measurement_path(source, points)
    topology = _topology_context(
        canonical=canonical,
        geometry=_geometry_path(source, points, entity_type="LWPOLYLINE"),
        edges=edges,
        endpoints=endpoints,
    )
    _module("topology_derivation").transform(topology)
    connectivity = _connectivity_context(
        topology,
        canonical=canonical,
        endpoints=endpoints,
    )
    _module("product_connectivity").transform(connectivity)

    pipe, = connectivity.outputs["product_instances"].to_pylist()
    assert pipe["product_kind"] == "PIPE"
    origin, = connectivity.outputs["product_origins"].to_pylist()
    assert origin["product_key"] == pipe["product_key"]
    assert origin["origin_kind"] == "source_path_interval_start"
    assert (origin["origin_x_m"], origin["origin_y_m"], origin["origin_z_m"]) == points[0]

    segments = connectivity.outputs["connection_segments"].to_pylist()
    assert len(segments) == 2
    assert [
        (
            row["a_x_m"], row["a_y_m"], row["a_z_m"],
            row["b_x_m"], row["b_y_m"], row["b_z_m"],
        )
        for row in segments
    ] == pytest.approx(
        [
            (*points[0], *points[2]),
            (*points[2], *points[4]),
        ]
    )
    assert all(row["outer_diameter_m"] == 0.12 for row in segments)
    assert all(row["diameter_status"] == "known" for row in segments)
    assert all(row["source_path_key"] == "path:" + source for row in segments)
    assert (
        segments[0]["start_segment_ordinal"],
        segments[0]["start_parameter"],
        segments[0]["end_segment_ordinal"],
        segments[0]["end_parameter"],
    ) == (0, 0.0, 1, 1.0)
    assert (
        segments[1]["start_segment_ordinal"],
        segments[1]["start_parameter"],
        segments[1]["end_segment_ordinal"],
        segments[1]["end_parameter"],
    ) == (2, 0.0, 3, 1.0)

    ports = sorted(
        connectivity.outputs["world_ports"].to_pylist(),
        key=lambda row: row["sequence_ordinal"],
    )
    assert [row["port_role"] for row in ports] == ["START", "END"]
    assert ports[0]["segment_key"] == segments[0]["segment_key"]
    assert ports[1]["segment_key"] == segments[1]["segment_key"]
    assert ports[0]["retained_side"] == "source_forward"
    assert ports[1]["retained_side"] == "source_backward"
    assert [ports[0][f"retained_direction_{axis}"] for axis in "xyz"] == pytest.approx(
        (1.0, 0.0, 0.0)
    )
    assert [ports[1][f"retained_direction_{axis}"] for axis in "xyz"] == pytest.approx(
        tuple(-value for value in direction)
    )
    assert [ports[0][f"pipe_outward_{axis}"] for axis in "xyz"] == pytest.approx(
        (-1.0, 0.0, 0.0)
    )
    assert [ports[1][f"pipe_outward_{axis}"] for axis in "xyz"] == pytest.approx(direction)
    assert not any(row["product_kind"] == "BEND" for row in connectivity.outputs["product_instances"].to_pylist())


def _tee_topology_fixture() -> tuple[
    _Context,
    list[dict[str, object]],
    list[dict[str, object]],
    str,
    str,
]:
    host = "pipe:tee-main"
    branch = "pipe:tee-branch"
    host_points = [(-4.0, 0.0, 0.0), (0.0, 0.0, 0.0), (4.0, 0.0, 0.0)]
    branch_points = [(0.0, -3.0, 0.0), (0.0, 0.0, 0.0)]
    canonical = [
        _canonical(host, od=0.1, pressure="MEDIUM", entity_type="LWPOLYLINE"),
        _canonical(branch, od=0.1, pressure="MEDIUM"),
    ]
    host_edges, host_endpoints = _measurement_path(host, host_points)
    branch_edges, branch_endpoints = _measurement_path(branch, branch_points)
    shared_node = f"q:{host}:00001"
    branch_endpoints[-1]["quantized_endpoint_key"] = shared_node
    endpoints = host_endpoints + branch_endpoints
    topology = _topology_context(
        canonical=canonical,
        geometry=(
            _geometry_path(host, host_points, entity_type="LWPOLYLINE")
            + _geometry_path(branch, branch_points, entity_type="LINE")
        ),
        edges=host_edges + branch_edges,
        endpoints=endpoints,
    )
    _module("topology_derivation").transform(topology)
    return topology, canonical, endpoints, host, branch


def test_connectivity_tee_roles_come_from_source_path_and_survive_row_shuffle() -> None:
    topology, canonical, endpoints, host, branch = _tee_topology_fixture()
    base = _connectivity_context(topology, canonical=canonical, endpoints=endpoints)
    shuffled = _reversed_context(base)
    module = _module("product_connectivity")
    module.transform(base)
    module.transform(shuffled)

    tee = next(
        row
        for row in base.outputs["product_instances"].to_pylist()
        if row["product_kind"] == "TEE"
    )
    tee_ports = sorted(
        (
            row
            for row in base.outputs["world_ports"].to_pylist()
            if row["owner_product_key"] == tee["product_key"]
        ),
        key=lambda row: row["port_role"],
    )
    assert [row["port_role"] for row in tee_ports] == ["BRANCH", "MAIN_A", "MAIN_B"]
    assert all(row["role_status"] == "known" for row in tee_ports)
    assert all(row["source_event_key"] is not None for row in tee_ports)
    assert {row["source_path_key"] for row in tee_ports if row["port_role"].startswith("MAIN")} == {
        "path:" + host
    }
    assert next(row for row in tee_ports if row["port_role"] == "BRANCH")["source_path_key"] == "path:" + branch
    assert {row["retained_side"] for row in tee_ports if row["port_role"].startswith("MAIN")} == {
        "source_forward",
        "source_backward",
    }
    assert len(
        [
            row
            for row in base.outputs["connection_pairs"].to_pylist()
            if tee["product_key"] in {
                next(
                    port["owner_product_key"]
                    for port in base.outputs["world_ports"].to_pylist()
                    if port["port_key"] == row["port_a_key"]
                ),
                next(
                    port["owner_product_key"]
                    for port in base.outputs["world_ports"].to_pylist()
                    if port["port_key"] == row["port_b_key"]
                ),
            }
        ]
    ) == 3
    assert base.outputs.keys() == shuffled.outputs.keys()
    for name in base.outputs:
        assert base.outputs[name].to_pylist() == shuffled.outputs[name].to_pylist()

    different_station = _connectivity_context(
        topology,
        canonical=canonical,
        endpoints=endpoints,
    )
    tee_event_key = next(
        row["product_event_key"]
        for row in topology.outputs["product_events"].to_pylist()
        if row["event_kind"] == "TEE"
    )
    vertices = different_station.tables["vertices"].to_pylist()
    main_occurrences = [
        row
        for row in vertices
        if row["source_path_key"] == "path:" + host
        and tee_event_key in row["event_keys"]
    ]
    assert len(main_occurrences) == 2
    main_occurrences[-1]["source_parameter"] = 0.25
    different_station.tables["vertices"] = pa.Table.from_pylist(
        vertices,
        schema=different_station.tables["vertices"].schema,
    )
    module.transform(different_station)
    different_tee = next(
        row
        for row in different_station.outputs["product_instances"].to_pylist()
        if row["source_event_key"] == tee_event_key
    )
    different_ports = [
        row
        for row in different_station.outputs["world_ports"].to_pylist()
        if row["owner_product_key"] == different_tee["product_key"]
    ]
    assert {row["port_role"] for row in different_ports} == {
        "SIDE_A",
        "SIDE_B",
        "SIDE_C",
    }
    assert all(row["role_status"] == "neutral" for row in different_ports)
    branch_issue = next(
        row
        for row in different_station.outputs["connectivity_issues"].to_pylist()
        if row["product_key"] == different_tee["product_key"]
        and row["issue_code"] == "UNRESOLVED_BRANCH_ROLE"
    )
    assert branch_issue["blocked_actions"] == [
        "external_constraint_role_resolution"
    ]


@pytest.mark.parametrize("claimant_kind", ["VALVE", "BEND"])
def test_fitting_and_single_side_claimant_remain_unresolved(
    claimant_kind: str,
) -> None:
    import json

    topology, canonical, endpoints, host, branch = _tee_topology_fixture()
    events = topology.outputs["product_events"].to_pylist()
    tee_event = next(row for row in events if row["event_kind"] == "TEE")
    claimant_source = (
        "device:cross-step-valve" if claimant_kind == "VALVE" else branch
    )
    claimant_event_key = "repair:" + claimant_kind.lower() + ":single-side"
    claimant_event = {
        **tee_event,
        "product_event_key": claimant_event_key,
        "event_kind": claimant_kind,
        "source_entity_key": claimant_source,
        "status": "accepted",
        "reason_code": "accepted_single_incidence_test_witness",
    }
    if claimant_kind == "VALVE":
        claimant_event.update(
            {
                "origin_kind": "source",
                "outer_diameter_m": None,
                "source_origin_x_m": 0.0,
                "source_origin_y_m": 2.0,
                "source_origin_z_m": 0.0,
                "source_origin_geometry_element_key": _geometry_key(
                    claimant_source, 0
                ),
                "source_origin_kind": "source_cad_point",
                "source_origin_status": "known",
                "source_origin_authority": (
                    "source_l0_geometry:" + _geometry_key(claimant_source, 0)
                ),
            }
        )
        canonical = [
            *canonical,
            _canonical(
                claimant_source,
                od=None,
                pressure="MEDIUM",
                role="valve",
            ),
        ]
    events.append(claimant_event)
    topology.outputs["product_events"] = pa.Table.from_pylist(
        events,
        schema=topology.outputs["product_events"].schema,
    )
    vertices = topology.outputs["pipe_path_vertices"].to_pylist()
    branch_occurrences = [
        row
        for row in vertices
        if row["source_path_key"] == "path:" + branch
        and tee_event["product_event_key"] in row["event_keys"]
    ]
    assert len(branch_occurrences) == 1
    branch_occurrences[0]["event_keys"] = sorted(
        {*branch_occurrences[0]["event_keys"], claimant_event_key}
    )
    topology.outputs["pipe_path_vertices"] = pa.Table.from_pylist(
        vertices,
        schema=topology.outputs["pipe_path_vertices"].schema,
    )
    connectivity = _connectivity_context(
        topology,
        canonical=canonical,
        endpoints=endpoints,
    )
    _module("product_connectivity").transform(connectivity)

    products = connectivity.outputs["product_instances"].to_pylist()
    tee = next(row for row in products if row["product_kind"] == "TEE")
    claimant = next(
        row for row in products if row["source_event_key"] == claimant_event_key
    )
    assert claimant["product_kind"] == claimant_kind
    claimant_origin = next(
        row
        for row in connectivity.outputs["product_origins"].to_pylist()
        if row["product_key"] == claimant["product_key"]
    )
    assert (
        claimant_origin["origin_x_m"],
        claimant_origin["origin_y_m"],
        claimant_origin["origin_z_m"],
    ) == ((0.0, 2.0, 0.0) if claimant_kind == "VALVE" else (0.0, 0.0, 0.0))

    ports = connectivity.outputs["world_ports"].to_pylist()
    branch_pipe = next(
        row
        for row in products
        if row["product_kind"] == "PIPE" and row["source_entity_key"] == branch
    )
    branch_source_port = next(
        row
        for row in ports
        if row["owner_product_key"] == branch_pipe["product_key"]
        and tee_event["product_event_key"] in row["source_event_keys"]
    )
    assert branch_source_port["connection_state"] == "unresolved"
    assert {row["port_role"] for row in ports if row["owner_product_key"] == branch_pipe["product_key"]} == {
        "START",
        "END",
    }

    tee_ports = [row for row in ports if row["owner_product_key"] == tee["product_key"]]
    tee_branch = next(row for row in tee_ports if row["port_role"] == "BRANCH")
    tee_main = [row for row in tee_ports if row["port_role"].startswith("MAIN")]
    claimant_ports = [
        row for row in ports if row["owner_product_key"] == claimant["product_key"]
    ]
    assert len(claimant_ports) == 1
    claimant_side = claimant_ports[0]
    assert claimant_side["port_role"] == "SIDE_A"
    assert {
        (
            row["source_path_key"],
            row["source_segment_ordinal"],
            row["source_parameter"],
            row["segment_key"],
            row["outer_diameter_m"],
        )
        for row in (tee_branch, claimant_side)
    } == {
        (
            branch_source_port["source_path_key"],
            branch_source_port["source_segment_ordinal"],
            branch_source_port["source_parameter"],
            branch_source_port["segment_key"],
            branch_source_port["outer_diameter_m"],
        )
    }
    assert tee_branch["connection_state"] == "unresolved"
    assert claimant_side["connection_state"] == "unresolved"
    assert all(row["connection_state"] == "source_business_pairing" for row in tee_main)

    contested_keys = {
        branch_source_port["port_key"],
        tee_branch["port_key"],
        claimant_side["port_key"],
    }
    connections = connectivity.outputs["connection_pairs"].to_pylist()
    assert not any(
        contested_keys & {row["port_a_key"], row["port_b_key"]}
        for row in connections
    )
    assert {
        row["port_key"]
        for row in tee_main
    } <= {
        port_key
        for row in connections
        for port_key in (row["port_a_key"], row["port_b_key"])
    }

    event_keys = {tee_event["product_event_key"], claimant_event_key}
    conflict = {
        row["subject_key"]: row
        for row in connectivity.outputs["connectivity_issues"].to_pylist()
        if row["issue_code"] == "UNRESOLVED_EVENT_PORT_CONFLICT"
    }
    assert set(conflict) == contested_keys
    owner_by_port = {row["port_key"]: row["owner_product_key"] for row in ports}
    for subject, issue in conflict.items():
        assert issue["product_key"] == owner_by_port[subject]
        evidence = json.loads(issue["evidence_json"])
        assert evidence["source_port_key"] == branch_source_port["port_key"]
        assert set(evidence["event_keys"]) == event_keys
    incidence_issue = next(
        row
        for row in connectivity.outputs["connectivity_issues"].to_pylist()
        if row["issue_code"] == "UNRESOLVED_EVENT_INCIDENCE"
        and row["product_key"] == claimant["product_key"]
    )
    incidence_evidence = json.loads(incidence_issue["evidence_json"])
    assert incidence_evidence["incident_count"] == 1
    assert incidence_evidence["required_incident_count"] == 2


def test_connectivity_does_not_reclassify_an_unevidenced_five_way_node() -> None:
    sources = [f"pipe:five-way:{ordinal}" for ordinal in range(5)]
    canonical = [
        _canonical(source, od=0.1, pressure="MEDIUM") for source in sources
    ]
    geometry = []
    edges = []
    endpoints = []
    directions = [
        (-1.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, -1.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, -1.0),
    ]
    for source, start in zip(sources, directions, strict=True):
        points = [start, (0.0, 0.0, 0.0)]
        geometry.extend(_geometry_path(source, points, entity_type="LINE"))
        source_edges, source_endpoints = _measurement_path(source, points)
        source_endpoints[-1]["quantized_endpoint_key"] = "q:five-way"
        edges.extend(source_edges)
        endpoints.extend(source_endpoints)
    topology = _topology_context(
        canonical=canonical,
        geometry=geometry,
        edges=edges,
        endpoints=endpoints,
    )
    _module("topology_derivation").transform(topology)
    topology.outputs["product_events"] = pa.Table.from_pylist(
        [
            row
            for row in topology.outputs["product_events"].to_pylist()
            if row["target_key"] != "q:five-way"
        ],
        schema=topology.outputs["product_events"].schema,
    )
    connectivity = _connectivity_context(
        topology,
        canonical=canonical,
        endpoints=endpoints,
    )
    _module("product_connectivity").transform(connectivity)

    products = connectivity.outputs["product_instances"].to_pylist()
    assert [row["product_kind"] for row in products] == ["PIPE"] * 5
    assert connectivity.outputs["connection_pairs"].num_rows == 0
    unresolved_ports = [
        row
        for row in connectivity.outputs["world_ports"].to_pylist()
        if row["connection_state"] == "unresolved"
    ]
    assert len(unresolved_ports) == 5
    issues = connectivity.outputs["connectivity_issues"].to_pylist()
    assert {row["subject_key"] for row in issues if row["affected_field"] == "source_business_pairing"} == {
        row["port_key"] for row in unresolved_ports
    }
    assert all(
        row["blocked_actions"]
        for row in issues
        if row["affected_field"] == "source_business_pairing"
    )


@pytest.mark.parametrize(
    ("role", "product_kind"),
    [("valve", "VALVE"), ("regulator", "REGULATOR_ASSEMBLY")],
)
def test_device_access_sides_keep_their_own_od_and_source_origin(
    role: str,
    product_kind: str,
) -> None:
    connectivity, event_key = _two_sided_device_context(role)
    products = connectivity.outputs["product_instances"].to_pylist()
    product = next(row for row in products if row["product_kind"] == product_kind)
    assert not {
        "geometry_strategy",
        "representation_strategy",
        "component_resource_key",
        "body_scale",
    } & product.keys()
    origin = next(
        row
        for row in connectivity.outputs["product_origins"].to_pylist()
        if row["product_key"] == product["product_key"]
    )
    assert origin["origin_kind"] == "source_cad_point"
    assert origin["origin_status"] == "known"
    assert (origin["origin_x_m"], origin["origin_y_m"], origin["origin_z_m"]) == (
        0.0,
        2.0,
        0.0,
    )
    assert origin["source_geometry_element_key"] == _geometry_key(f"{role}:source", 0)
    ports = sorted(
        (
            row
            for row in connectivity.outputs["world_ports"].to_pylist()
            if row["source_event_key"] == event_key
        ),
        key=lambda row: row["port_role"],
    )
    assert [row["port_role"] for row in ports] == (
        ["HIGH_PRESSURE", "LOW_PRESSURE"] if role == "regulator" else ["SIDE_A", "SIDE_B"]
    )
    assert all(row["role_status"] == ("defaulted" if role == "regulator" else "neutral") for row in ports)
    assert [row["outer_diameter_m"] for row in ports] == [0.1, 0.2]
    assert all(row["diameter_status"] == "known" for row in ports)
    assert all((row["x_m"], row["y_m"], row["z_m"]) == (0.0, 0.0, 0.0) for row in ports)
    assert all(row["retained_side"] in {"source_forward", "source_backward"} for row in ports)
    assert not any(row["port_role"] == "INNER" for row in connectivity.outputs["world_ports"].to_pylist())
    assert not any(
        row["connection_scope"] == "assembly_internal"
        for row in connectivity.outputs["connection_pairs"].to_pylist()
    )
    if role == "regulator":
        assert not any(
            row["product_key"] == product["product_key"]
            and row["affected_field"] == "pressure_side_role"
            for row in connectivity.outputs["connectivity_issues"].to_pylist()
        )


@pytest.mark.parametrize("role", ["valve", "regulator"])
def test_missing_device_side_od_stays_missing_without_cross_side_borrowing(role: str) -> None:
    connectivity, event_key = _two_sided_device_context(role, right_od=None)
    ports = sorted(
        (
            row
            for row in connectivity.outputs["world_ports"].to_pylist()
            if row["source_event_key"] == event_key
        ),
        key=lambda row: row["port_role"],
    )
    assert [row["outer_diameter_m"] for row in ports] == [0.1, None]
    assert [row["diameter_status"] for row in ports] == ["known", "missing"]
    assert all(row["outer_diameter_m"] != 0.032 for row in ports)
    missing = [
        row
        for row in connectivity.outputs["connectivity_issues"].to_pylist()
        if row["affected_field"] == "outer_diameter_m"
        and row["resolution_status"] == "open"
    ]
    assert len(missing) == 1
    assert missing[0]["subject_key"] == ports[1]["port_key"]
    assert set(missing[0]["blocked_actions"]) == {
        "family_geometry",
        "interface_profile",
        "ifc_network_binding",
    }


@pytest.mark.parametrize("fault", ["missing_row", "null_coordinate"])
def test_missing_source_origin_preserves_nulls_and_only_its_explicit_blocker(
    fault: str,
) -> None:
    connectivity, event_key = _two_sided_device_context(
        "valve",
        include_source_origin=fault != "missing_row",
        source_origin_point=(None, 2.0, 0.0),
    )
    product = next(
        row
        for row in connectivity.outputs["product_instances"].to_pylist()
        if row["source_event_key"] == event_key
    )
    origin = next(
        row
        for row in connectivity.outputs["product_origins"].to_pylist()
        if row["product_key"] == product["product_key"]
    )
    assert origin["origin_status"] == "missing"
    assert (origin["origin_x_m"], origin["origin_y_m"], origin["origin_z_m"]) == (
        (None, None, None)
        if fault == "missing_row"
        else (None, 2.0, 0.0)
    )
    assert origin["source_geometry_element_key"] == (
        None if fault == "missing_row" else _geometry_key("valve:source", 0)
    )
    issue = next(
        row
        for row in connectivity.outputs["connectivity_issues"].to_pylist()
        if row["product_key"] == product["product_key"]
        and row["affected_field"] == "product_origin"
    )
    assert issue["resolution_status"] == "open"
    assert issue["blocked_actions"] == ["family_geometry"]


def test_saddle_and_anode_are_non_cut_attachments_while_meter_is_a_zero_width_pairing() -> None:
    host = "pipe:attachment-host"
    branch = "pipe:attachment-branch"
    anode_source = "anode:attachment"
    host_points = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)]
    branch_points = [(5.0, 0.001, 0.0), (5.0, 1.0, 0.0)]
    host_edges, host_endpoints = _measurement_path(host, host_points)
    branch_edges, branch_endpoints = _measurement_path(branch, branch_points)
    anode = _point_candidate(
        "point:attachment-anode",
        anode_source,
        host_edges[0],
        target_kind="edge_interior",
        target_key=host_edges[0]["atomic_edge_key"],
    )
    anode.update(
        point_x_m=5.0,
        point_y_m=0.05,
        projection_x_m=5.0,
        projection_y_m=0.0,
        host_parameter=0.5,
    )
    relation = {
        "endpoint_on_path_key": "eop:attachment-saddle",
        "branch_endpoint_key": branch_edges[0]["start_endpoint_key"],
        "branch_atomic_edge_key": branch_edges[0]["atomic_edge_key"],
        "branch_source_entity_key": branch,
        "host_atomic_edge_key": host_edges[0]["atomic_edge_key"],
        "host_source_entity_key": host,
        "projection_x_m": 5.0,
        "projection_y_m": 0.0,
        "projection_z_m": 0.0,
        "host_parameter": 0.5,
        "distance_3d_m": 0.001,
        "same_source_entity": False,
        "passes_host_endpoint_guard": True,
    }
    canonical = [
        _canonical(host, od=0.2),
        _canonical(branch, od=0.05),
        _canonical(anode_source, od=0.2, role="anode"),
    ]
    endpoints = host_endpoints + branch_endpoints
    topology = _topology_context(
        canonical=canonical,
        geometry=(
            _geometry_path(host, host_points, entity_type="LINE")
            + _geometry_path(branch, branch_points, entity_type="LINE")
            + [_point_geometry(anode_source, (5.0, 0.05, 0.0))]
        ),
        edges=host_edges + branch_edges,
        endpoints=endpoints,
        endpoint_on_path=pa.Table.from_pylist([relation]),
        point_to_path=pa.Table.from_pylist([anode]),
    )
    _module("topology_derivation").transform(topology)
    connectivity = _connectivity_context(
        topology,
        canonical=canonical,
        endpoints=endpoints,
    )
    _module("product_connectivity").transform(connectivity)
    products = connectivity.outputs["product_instances"].to_pylist()
    assert {row["product_kind"] for row in products} >= {"PIPE", "SERVICE_SADDLE", "ANODE"}
    attachments = [
        row
        for row in connectivity.outputs["connection_pairs"].to_pylist()
        if row["fact_status"] == "source_attachment"
    ]
    assert len(attachments) == 2
    assert all(row["connection_scope"] == "non_cut_attachment" for row in attachments)
    host_segment = next(
        row
        for row in connectivity.outputs["connection_segments"].to_pylist()
        if row["source_entity_key"] == host
    )
    assert (host_segment["a_x_m"], host_segment["b_x_m"]) == (0.0, 10.0)
    attachment_ports = [
        row
        for row in connectivity.outputs["world_ports"].to_pylist()
        if row["access_semantics"] == "non_cut_attachment"
    ]
    assert len(attachment_ports) == 2
    assert all(row["retained_side"] == "not_applicable" for row in attachment_ports)
    assert all(row["segment_key"] == host_segment["segment_key"] for row in attachment_ports)

    meter_source = "pipe:zero-width-meter"
    meter_points = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)]
    meter_edges, meter_endpoints = _measurement_path(meter_source, meter_points)
    meter_canonical = [_canonical(meter_source, od=0.1)]
    meter_topology = _topology_context(
        canonical=meter_canonical,
        geometry=_geometry_path(meter_source, meter_points, entity_type="LINE"),
        edges=meter_edges,
        endpoints=meter_endpoints,
    )
    _module("topology_derivation").transform(meter_topology)
    meter_connectivity = _connectivity_context(
        meter_topology,
        canonical=meter_canonical,
        endpoints=meter_endpoints,
    )
    _module("product_connectivity").transform(meter_connectivity)
    meter_product = next(
        row
        for row in meter_connectivity.outputs["product_instances"].to_pylist()
        if row["product_kind"] == "GAS_METER"
    )
    meter_origin = next(
        row
        for row in meter_connectivity.outputs["product_origins"].to_pylist()
        if row["product_key"] == meter_product["product_key"]
    )
    assert meter_origin["origin_kind"] == "approved_design_location"
    assert (meter_origin["origin_x_m"], meter_origin["origin_y_m"]) == (5.0, 0.0)
    meter_ports = [
        row
        for row in meter_connectivity.outputs["world_ports"].to_pylist()
        if row["owner_product_key"] == meter_product["product_key"]
    ]
    assert len(meter_ports) == 2
    assert {(row["x_m"], row["y_m"], row["z_m"]) for row in meter_ports} == {
        (5.0, 0.0, 0.0)
    }
    assert all(row["fact_status"] == "source_business_pairing" for row in meter_connectivity.outputs["connection_pairs"].to_pylist())


def test_anode_endpoint_attachment_does_not_consume_a_straight_connector_pairing() -> None:
    left_source = "pipe:anode-straight:left"
    right_source = "pipe:anode-straight:right"
    anode_source = "anode:straight-through"
    left_points = [(-2.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
    right_points = [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0)]
    left_edges, left_endpoints = _measurement_path(left_source, left_points)
    right_edges, right_endpoints = _measurement_path(right_source, right_points)
    shared_node = "q:anode-straight-through"
    left_endpoints[-1]["quantized_endpoint_key"] = shared_node
    right_endpoints[0]["quantized_endpoint_key"] = shared_node
    candidate = _point_candidate(
        "point:anode-straight-through",
        anode_source,
        left_edges[0],
        target_kind="quantized_endpoint",
        target_key=shared_node,
    )
    candidate.update(
        point_x_m=0.0,
        point_y_m=0.0,
        point_z_m=0.0,
        projection_x_m=0.0,
        projection_y_m=0.0,
        projection_z_m=0.0,
        host_parameter=1.0,
    )
    canonical = [
        _canonical(left_source, od=0.1, pressure="MEDIUM"),
        _canonical(right_source, od=0.1, pressure="MEDIUM"),
        _canonical(anode_source, od=0.1, pressure="MEDIUM", role="anode"),
    ]
    endpoints = left_endpoints + right_endpoints
    topology = _topology_context(
        canonical=canonical,
        geometry=(
            _geometry_path(left_source, left_points, entity_type="LINE")
            + _geometry_path(right_source, right_points, entity_type="LINE")
            + [_point_geometry(anode_source, (0.0, 0.0, 0.0))]
        ),
        edges=left_edges + right_edges,
        endpoints=endpoints,
        point_to_path=pa.Table.from_pylist([candidate]),
    )
    _module("topology_derivation").transform(topology)
    connectivity = _connectivity_context(
        topology,
        canonical=canonical,
        endpoints=endpoints,
    )
    _module("product_connectivity").transform(connectivity)

    pipe_products = {
        row["product_key"]
        for row in connectivity.outputs["product_instances"].to_pylist()
        if row["product_kind"] == "PIPE"
    }
    ports = connectivity.outputs["world_ports"].to_pylist()
    gas_ports = {
        row["port_key"]
        for row in ports
        if row["owner_product_key"] in pipe_products
        and row["access_semantics"] == "source_pipe_endpoint"
        and (row["x_m"], row["y_m"], row["z_m"]) == (0.0, 0.0, 0.0)
    }
    assert len(gas_ports) == 2
    connections = connectivity.outputs["connection_pairs"].to_pylist()
    connector = next(row for row in connectivity.outputs["product_instances"].to_pylist()
                     if row["product_kind"] == "CONNECTOR")
    connector_ports = {row["port_key"] for row in ports if row["owner_product_key"] == connector["product_key"]}
    gas_pairs = [row for row in connections if {row["port_a_key"], row["port_b_key"]} & gas_ports]
    assert len(gas_pairs) == 2
    assert all(row["fact_status"] == "source_business_pairing"
               and len({row["port_a_key"], row["port_b_key"]} & connector_ports) == 1 for row in gas_pairs)
    attachment = next(
        row for row in connections if row["fact_status"] == "source_attachment"
    )
    assert not gas_ports & {attachment["port_a_key"], attachment["port_b_key"]}
    attachment_contacts = [
        row
        for row in ports
        if row["owner_product_key"] in pipe_products
        and row["access_semantics"] == "source_attachment_contact"
    ]
    assert len(attachment_contacts) == 1
    assert attachment_contacts[0]["port_key"] in {
        attachment["port_a_key"],
        attachment["port_b_key"],
    }


@pytest.mark.parametrize(
    ("reverse_rows", "regulator_source", "valve_source"),
    [
        (False, "device:a-regulator", "device:z-valve"),
        (True, "device:a-regulator", "device:z-valve"),
        (False, "device:z-regulator", "device:a-valve"),
    ],
)
def test_duplicate_device_events_do_not_clone_an_already_claimed_gas_endpoint(
    reverse_rows: bool,
    regulator_source: str,
    valve_source: str,
) -> None:
    import json

    left_source = "pipe:duplicate-event:left"
    right_source = "pipe:duplicate-event:right"
    left_points = [(-2.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
    right_points = [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0)]
    left_edges, left_endpoints = _measurement_path(left_source, left_points)
    right_edges, right_endpoints = _measurement_path(right_source, right_points)
    shared_node = "q:duplicate-device-event"
    left_endpoints[-1]["quantized_endpoint_key"] = shared_node
    right_endpoints[0]["quantized_endpoint_key"] = shared_node
    canonical = [
        _canonical(left_source, od=0.1, pressure="MEDIUM"),
        _canonical(right_source, od=0.1, pressure="MEDIUM"),
    ]
    geometry = (
        _geometry_path(left_source, left_points, entity_type="LINE")
        + _geometry_path(right_source, right_points, entity_type="LINE")
    )
    candidates = []
    source_by_role = {
        "regulator": regulator_source,
        "valve": valve_source,
    }
    origin_by_source = {
        regulator_source: (0.0, 2.0, 0.0),
        valve_source: (0.0, 3.0, 0.0),
    }
    for role, source in source_by_role.items():
        origin = origin_by_source[source]
        canonical.append(
            _canonical(source, od=None, pressure="MEDIUM", role=role)
        )
        geometry.append(_point_geometry(source, origin))
        for ordinal, (edge, parameter) in enumerate(
            ((left_edges[0], 1.0), (right_edges[0], 0.0))
        ):
            candidate = _point_candidate(
                f"point:{role}:duplicate-event:{ordinal}",
                source,
                edge,
                target_kind="quantized_endpoint",
                target_key=shared_node,
            )
            candidate.update(
                point_x_m=0.0,
                point_y_m=origin[1],
                point_z_m=0.0,
                projection_x_m=0.0,
                projection_y_m=0.0,
                projection_z_m=0.0,
                host_parameter=parameter,
            )
            candidates.append(candidate)
    endpoints = left_endpoints + right_endpoints
    topology = _topology_context(
        canonical=canonical,
        geometry=geometry,
        edges=left_edges + right_edges,
        endpoints=endpoints,
        point_to_path=pa.Table.from_pylist(candidates),
    )
    _module("topology_derivation").transform(topology)
    connectivity = _connectivity_context(
        topology,
        canonical=canonical,
        endpoints=endpoints,
    )
    if reverse_rows:
        connectivity = _reversed_context(connectivity)
    _module("product_connectivity").transform(connectivity)

    products = connectivity.outputs["product_instances"].to_pylist()
    device_products = {
        row["source_entity_key"]: row
        for row in products
        if row["source_entity_key"] in set(source_by_role.values())
        and row["assembly_role"] != "assembly_component"
    }
    assert {
        source: row["product_kind"] for source, row in device_products.items()
    } == {
        regulator_source: "REGULATOR_ASSEMBLY",
        valve_source: "VALVE",
    }
    origins_by_product = {
        row["product_key"]: row
        for row in connectivity.outputs["product_origins"].to_pylist()
    }
    for source, product in device_products.items():
        origin = origins_by_product[product["product_key"]]
        assert origin["origin_status"] == "known"
        assert (
            origin["origin_x_m"],
            origin["origin_y_m"],
            origin["origin_z_m"],
        ) == origin_by_source[source]
        assert origin["source_geometry_element_key"] == _geometry_key(source, 0)

    pipe_product_by_source = {
        row["source_entity_key"]: row
        for row in products
        if row["product_kind"] == "PIPE"
    }
    assert set(pipe_product_by_source) == {left_source, right_source}
    segments_by_source = {
        row["source_entity_key"]: row
        for row in connectivity.outputs["connection_segments"].to_pylist()
    }
    assert set(segments_by_source) == {left_source, right_source}

    ports = connectivity.outputs["world_ports"].to_pylist()
    pipe_products = {
        row["product_key"] for row in pipe_product_by_source.values()
    }
    pipe_ports = [
        row
        for row in ports
        if row["owner_product_key"] in pipe_products
    ]
    assert {
        product_key: {row["port_role"] for row in pipe_ports if row["owner_product_key"] == product_key}
        for product_key in pipe_products
    } == {
        product_key: {"START", "END"} for product_key in pipe_products
    }
    assert all(row["access_semantics"] == "source_pipe_endpoint" for row in pipe_ports)

    contested_pipe_ports = {
        row["port_key"]: row
        for row in pipe_ports
        if (row["x_m"], row["y_m"], row["z_m"]) == (0.0, 0.0, 0.0)
    }
    assert {
        row["source_path_key"] for row in contested_pipe_ports.values()
    } == {"path:" + left_source, "path:" + right_source}
    event_keys = {"point:" + source for source in source_by_role.values()}
    assert all(
        set(row["source_event_keys"]) == event_keys
        and row["connection_state"] == "unresolved"
        for row in contested_pipe_ports.values()
    )

    expected_source_sides = {
        (
            row["source_path_key"],
            row["source_segment_ordinal"],
            row["source_parameter"],
            row["segment_key"],
            row["outer_diameter_m"],
            row["diameter_status"],
        )
        for row in contested_pipe_ports.values()
    }
    contested_access_ports = {}
    for source, product in device_products.items():
        device_ports = [
            row for row in ports if row["owner_product_key"] == product["product_key"]
        ]
        contested_access_ports.update(
            {row["port_key"]: row for row in device_ports}
        )
        assert {row["port_role"] for row in device_ports} == (
            {"HIGH_PRESSURE", "LOW_PRESSURE"} if source == regulator_source else {"SIDE_A", "SIDE_B"}
        )
        assert all(
            row["source_event_key"] == "point:" + source
            and row["source_event_keys"] == ["point:" + source]
            and row["connection_state"] == "unresolved"
            for row in device_ports
        )
        assert {
            (
                row["source_path_key"],
                row["source_segment_ordinal"],
                row["source_parameter"],
                row["segment_key"],
                row["outer_diameter_m"],
                row["diameter_status"],
            )
            for row in device_ports
        } == expected_source_sides
        assert {row["segment_key"] for row in device_ports} == {
            segments_by_source[left_source]["segment_key"],
            segments_by_source[right_source]["segment_key"],
        }

    assert connectivity.outputs["connection_pairs"].to_pylist() == []
    conflict = [
        row
        for row in connectivity.outputs["connectivity_issues"].to_pylist()
        if row["issue_code"] == "UNRESOLVED_EVENT_PORT_CONFLICT"
    ]
    conflict_by_subject = {row["subject_key"]: row for row in conflict}
    assert set(conflict_by_subject) == {
        *contested_pipe_ports,
        *contested_access_ports,
    }
    source_port_by_locus = {
        (
            row["source_path_key"],
            row["source_segment_ordinal"],
            row["source_parameter"],
            row["segment_key"],
        ): port_key
        for port_key, row in contested_pipe_ports.items()
    }
    for port_key, issue in conflict_by_subject.items():
        subject = (
            contested_pipe_ports[port_key]
            if port_key in contested_pipe_ports
            else contested_access_ports[port_key]
        )
        assert issue["product_key"] == subject["owner_product_key"]
        assert issue["affected_field"] == "source_business_pairing"
        assert issue["resolution_status"] == "open"
        assert issue["blocked_actions"] == ["external_constraint_role_resolution"]
        evidence = json.loads(issue["evidence_json"])
        expected_source_port_key = (
            port_key
            if port_key in contested_pipe_ports
            else source_port_by_locus[
                (
                    subject["source_path_key"],
                    subject["source_segment_ordinal"],
                    subject["source_parameter"],
                    subject["segment_key"],
                )
            ]
        )
        assert evidence["source_port_key"] == expected_source_port_key
        assert set(evidence["event_keys"]) == event_keys


def _riser_topology_context() -> tuple[_Context, list[dict], list[dict]]:
    pipe_source = "pipe:riser-host"
    riser_source = "riser:source"
    points = [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (4.0, 0.0, 0.0)]
    edges, endpoints = _measurement_path(pipe_source, points)
    target = f"q:{pipe_source}:00000"
    candidate = _point_candidate(
        "point:riser-inference",
        riser_source,
        edges[0],
        target_kind="quantized_endpoint",
        target_key=target,
        distance=0.000094,
    )
    canonical = [
        _canonical(pipe_source, od=0.08, pressure="MEDIUM", entity_type="LWPOLYLINE"),
        _canonical(riser_source, od=None, pressure="MEDIUM", role="riser"),
    ]
    topology = _topology_context(
        canonical=canonical,
        geometry=(
            _geometry_path(pipe_source, points, entity_type="LWPOLYLINE")
            + [_point_geometry(riser_source, (0.0, 0.0, 0.0))]
        ),
        edges=edges,
        endpoints=endpoints,
        point_to_path=pa.Table.from_pylist([candidate]),
    )
    return topology, canonical, endpoints


def _riser_inference_context() -> tuple[_Context, str]:
    topology, canonical, endpoints = _riser_topology_context()
    _module("topology_derivation").transform(topology)
    assert topology.outputs["pipe_pieces"].to_pylist()[0]["source_atomic_edge_key"] is None
    return (
        _connectivity_context(topology, canonical=canonical, endpoints=endpoints),
        "point:riser:source",
    )


def test_riser_od_inference_uses_exact_terminal_locus_on_a_multi_segment_piece() -> None:
    connectivity, event_key = _riser_inference_context()
    _module("product_connectivity").transform(connectivity)
    riser = next(
        row
        for row in connectivity.outputs["product_instances"].to_pylist()
        if row["source_event_key"] == event_key
    )
    assert riser["product_kind"] == "RISER"
    assert riser["height_m"] == 1.5
    assert riser["height_authority"]
    origin = next(
        row
        for row in connectivity.outputs["product_origins"].to_pylist()
        if row["product_key"] == riser["product_key"]
    )
    assert origin["origin_status"] == "known"
    assert origin["source_geometry_element_key"] == _geometry_key("riser:source", 0)
    assert (origin["origin_x_m"], origin["origin_y_m"], origin["origin_z_m"]) == (
        0.0,
        0.0,
        0.0,
    )
    ports = [
        row
        for row in connectivity.outputs["world_ports"].to_pylist()
        if row["owner_product_key"] == riser["product_key"]
    ]
    assert [row["port_role"] for row in sorted(ports, key=lambda row: row["sequence_ordinal"])] == [
        "BOTTOM",
        "TOP",
    ]
    assert {
        row["port_role"]: (row["x_m"], row["y_m"], row["z_m"])
        for row in ports
    } == {
        "BOTTOM": (0.0, 0.0, 0.0),
        "TOP": (0.0, 0.0, 1.5),
    }
    assert all(row["outer_diameter_m"] == 0.08 for row in ports)
    assert all(row["diameter_status"] == "inferred" for row in ports)
    assert all(
        row["diameter_authority"].startswith(
            "authorized_unique_unsnapped_endpoint_od_inference:"
        )
        for row in ports
    )
    inferred = next(
        row
        for row in connectivity.outputs["connectivity_issues"].to_pylist()
        if row["product_key"] == riser["product_key"]
        and row["affected_field"] == "outer_diameter_m"
        and row["resolution_status"] == "resolved_by_authorized_inference"
    )
    assert inferred["issue_code"] == "RISER_OUTER_DIAMETER_INFERRED"
    assert inferred["blocked_actions"] == []
    port_keys = {row["port_key"] for row in ports}
    assert any(
        port_keys & {row["port_a_key"], row["port_b_key"]}
        for row in connectivity.outputs["connection_pairs"].to_pylist()
    )
    assert inferred["resolution_status"] == "resolved_by_authorized_inference"
    import json
    assert json.loads(inferred["evidence_json"])["business_connection_accepted"] is True
    assert json.loads(inferred["evidence_json"])["physical_connection_accepted"] is False


@pytest.mark.parametrize("fault", ["pressure_mismatch", "missing_host_od", "negative_distance", "nonterminal", "host_edge_mismatch", "competing_device"])
def test_riser_od_inference_stays_missing_without_unique_source_authority(fault: str) -> None:
    topology, canonical, endpoints = _riser_topology_context()
    candidates = topology.tables["ptps"].to_pylist()
    if fault == "pressure_mismatch":
        canonical[1]["pressure_normalized"] = "LOW"
    elif fault == "missing_host_od":
        canonical[0]["outer_diameter_m"] = None
    elif fault == "negative_distance":
        candidates[0]["distance_3d_m"] = -0.1
    elif fault == "nonterminal":
        candidates[0]["target_key"] = "q:pipe:riser-host:00001"
    elif fault == "host_edge_mismatch":
        candidates[0]["host_atomic_edge_key"] = "edge:pipe:riser-host:000001"
    elif fault == "competing_device":
        canonical.append(_canonical("valve:competitor", od=0.08, pressure="MEDIUM", role="valve"))
        candidates.append({**candidates[0], "point_entity_key": "valve:competitor", "point_to_path_key": "candidate:valve"})
    topology.tables["ce"] = pa.Table.from_pylist(canonical)
    topology.tables["ptps"] = pa.Table.from_pylist(candidates)
    _module("topology_derivation").transform(topology)
    event = next(row for row in topology.outputs["product_events"].to_pylist() if row["product_event_key"] == "point:riser:source")
    assert event["status"] == "unresolved"
    assert event["outer_diameter_m"] is None
    assert event["diameter_status"] == "missing"
    assert event["source_origin_status"] == "known"
    assert (event["source_origin_x_m"], event["source_origin_y_m"], event["source_origin_z_m"]) == (0.0, 0.0, 0.0)
    assert not any(row["event_kind"] == "RISER_BEND" for row in topology.outputs["product_events"].to_pylist())


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("missing_midpoint", "NONCONTIGUOUS_SOURCE_GEOMETRY"),
        ("duplicate_ordinal", "NONCONTIGUOUS_SOURCE_GEOMETRY"),
        ("gapped_ordinal", "NONCONTIGUOUS_SOURCE_GEOMETRY"),
        ("none_ordinal", "INVALID_SOURCE_ORDINAL"),
        ("non_integer_ordinal", "INVALID_SOURCE_ORDINAL"),
        ("none_coordinate", "INVALID_SOURCE_VALUE"),
        ("degenerate", "DEGENERATE_SOURCE_SEGMENT"),
        ("closed", "UNSUPPORTED_CLOSED_SOURCE_PATH"),
        ("bulge", "UNSUPPORTED_SOURCE_BULGE"),
        ("tiny_bulge", "UNSUPPORTED_SOURCE_BULGE"),
    ],
)
def test_invalid_source_path_is_rejected_explicitly(fault: str, message: str) -> None:
    source = "pipe:invalid"
    points = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 1.0, 0.0)]
    geometry = _geometry_path(source, points, entity_type="LWPOLYLINE")
    edges, endpoints = _measurement_path(source, points)
    if fault == "missing_midpoint":
        geometry.pop(1)
    elif fault == "duplicate_ordinal":
        geometry[2]["ordinal"] = 1
    elif fault == "gapped_ordinal":
        geometry[2]["ordinal"] = 3
    elif fault == "none_ordinal":
        geometry[1]["ordinal"] = None
    elif fault == "non_integer_ordinal":
        geometry[1]["ordinal"] = 1.0
    elif fault == "none_coordinate":
        geometry[0]["x"] = None
    elif fault == "degenerate":
        geometry[1]["x"] = 0.0
        edges[0]["end_x_m"] = 0.0
        edges[1]["start_x_m"] = 0.0
    elif fault == "closed":
        geometry[0]["closed"] = True
    elif fault == "bulge":
        geometry[0]["bulge"] = 0.25
    elif fault == "tiny_bulge":
        geometry[0]["bulge"] = 1.0e-14
    ctx = _topology_context(
        canonical=[_canonical(source, od=0.1, pressure="MEDIUM", entity_type="LWPOLYLINE")],
        geometry=geometry,
        edges=edges,
        endpoints=endpoints,
    )
    with pytest.raises(ValueError, match=message):
        _module("topology_derivation").transform(ctx)


@pytest.mark.parametrize(
    ("source_unit_to_m", "message"),
    [
        (None, "INVALID_SOURCE_VALUE field=source_unit_to_m"),
        (float("nan"), "INVALID_SOURCE_VALUE field=source_unit_to_m"),
        (float("inf"), "INVALID_SOURCE_VALUE field=source_unit_to_m"),
        (0.0, "INVALID_SOURCE_UNIT_TO_M"),
        (-1.0, "INVALID_SOURCE_UNIT_TO_M"),
    ],
)
def test_source_unit_parameter_must_be_explicit_positive_and_finite(
    source_unit_to_m: object,
    message: str,
) -> None:
    source = "pipe:unit"
    points = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)]
    edges, endpoints = _measurement_path(source, points)
    ctx = _topology_context(
        canonical=[_canonical(source, od=0.1, pressure="MEDIUM")],
        geometry=_geometry_path(source, points, entity_type="LINE"),
        edges=edges,
        endpoints=endpoints,
        source_unit_to_m=source_unit_to_m,
    )
    with pytest.raises(ValueError, match=message):
        _module("topology_derivation").transform(ctx)


@pytest.mark.parametrize("fault", ["geometry_reference", "coordinate"])
def test_atomic_edge_must_exactly_reference_its_source_path(fault: str) -> None:
    source = "pipe:source-reference"
    points = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 2.0, 0.0)]
    geometry = _geometry_path(source, points, entity_type="LWPOLYLINE")
    edges, endpoints = _measurement_path(source, points)
    if fault == "geometry_reference":
        edges[0]["source_end_geometry_element_key"] = "geometry:another-source:00001"
        message = "ATOMIC_EDGE_SOURCE_VERTEX_MISMATCH"
    else:
        edges[0]["end_x_m"] = 1.25
        message = "ATOMIC_EDGE_COORDINATE_MISMATCH"
    ctx = _topology_context(
        canonical=[_canonical(source, od=0.1, pressure="MEDIUM", entity_type="LWPOLYLINE")],
        geometry=geometry,
        edges=edges,
        endpoints=endpoints,
    )
    with pytest.raises(ValueError, match=message):
        _module("topology_derivation").transform(ctx)


def test_wider_endpoint_snap_is_review_only_and_meter_replaces_middle_interval() -> None:
    module = _module("topology_derivation")
    first = _edge("edge:a", "pipe:a", (0.0, 0.0, 0.0), (10.0, 0.0, 0.0))
    second = _edge("edge:b", "pipe:b", (10.01, 0.0, 0.0), (11.01, 0.0, 0.0))
    endpoints = _endpoints(first, "q:0", "q:10") + _endpoints(second, "q:10.01", "q:11.01")
    ctx = _topology_context(
        canonical=[_canonical("pipe:a", od=0.2), _canonical("pipe:b", od=0.2)],
        geometry=(
            _geometry_path("pipe:a", [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)], entity_type="LINE")
            + _geometry_path("pipe:b", [(10.01, 0.0, 0.0), (11.01, 0.0, 0.0)], entity_type="LINE")
        ),
        edges=[first, second],
        endpoints=endpoints,
        pairs=pa.Table.from_pylist([{
                "endpoint_pair_key": "pair:1", "quantized_endpoint_a_key": "q:10", "quantized_endpoint_b_key": "q:10.01",
                "source_entity_a_key": "pipe:a", "source_entity_b_key": "pipe:b", "distance_3d_m": 0.01,
        }]),
    )
    module.transform(ctx)
    decision = ctx.outputs["candidate_decisions"].to_pylist()[0]
    assert decision["decision_kind"] == "REVIEW"
    meter = next(row for row in ctx.outputs["product_events"].to_pylist() if row["event_kind"] == "GAS_METER")
    meter_pieces = [row for row in ctx.outputs["pipe_pieces"].to_pylist() if row["source_atomic_edge_key"] == meter["host_atomic_edge_key"]]
    assert len(meter_pieces) == 2
    assert abs(sum(row["length_m"] for row in meter_pieces) - 10.0) < 1e-9


def _writer_multisegment_points(segment_length_m: float = 2.0):
    angle = math.radians(1.0)
    direction = (math.cos(angle), math.sin(angle), 0.0)
    length = segment_length_m
    return [
        (0.0, 0.0, 0.0),
        (length, 0.0, 0.0),
        (2.0 * length, 0.0, 0.0),
        (
            2.0 * length + length * direction[0],
            length * direction[1],
            0.0,
        ),
        (
            2.0 * length + 2.0 * length * direction[0],
            2.0 * length * direction[1],
            0.0,
        ),
    ]


def _writer_multisegment_fixture(
    *,
    pressure: str = "MEDIUM",
    segment_length_m: float = 2.0,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    _Context,
]:
    source = "pipe:writer-multisegment"
    points = _writer_multisegment_points(segment_length_m)
    canonical = [
        _canonical(source, od=0.12, pressure=pressure, entity_type="LWPOLYLINE")
    ]
    xdata = [
        {
            "canonical_xdata_key": "xdata:writer-multisegment:asset",
            "source_entity_key": source,
            "assignment_key": "ASSET_CODE",
            "assignment_value": "PIPE-UNIT-001",
        }
    ]
    edges, endpoints = _measurement_path(source, points)
    topology = _topology_context(
        canonical=canonical,
        geometry=_geometry_path(source, points, entity_type="LWPOLYLINE"),
        edges=edges,
        endpoints=endpoints,
    )
    _module("topology_derivation").transform(topology)
    connectivity = _connectivity_context(
        topology,
        canonical=canonical,
        endpoints=endpoints,
    )
    _module("product_connectivity").transform(connectivity)
    return canonical, xdata, connectivity


def _writer_contested_attachment_fixture() -> tuple[
    list[dict[str, object]],
    _Context,
]:
    left_source = "pipe:writer-contested:left"
    right_source = "pipe:writer-contested:right"
    valve_source = "valve:writer-contested"
    regulator_source = "regulator:writer-contested"
    anode_source = "anode:writer-noncut"
    left_points = [(-2.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
    right_points = [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0)]
    left_edges, left_endpoints = _measurement_path(left_source, left_points)
    right_edges, right_endpoints = _measurement_path(right_source, right_points)
    shared_node = "q:writer-contested"
    left_endpoints[-1]["quantized_endpoint_key"] = shared_node
    right_endpoints[0]["quantized_endpoint_key"] = shared_node
    canonical = [
        _canonical(left_source, od=0.1, pressure="MEDIUM"),
        _canonical(right_source, od=0.1, pressure="MEDIUM"),
        _canonical(valve_source, od=None, pressure="MEDIUM", role="valve"),
        _canonical(
            regulator_source,
            od=None,
            pressure="MEDIUM",
            role="regulator",
        ),
        _canonical(anode_source, od=0.1, pressure="MEDIUM", role="anode"),
    ]
    geometry = (
        _geometry_path(left_source, left_points, entity_type="LINE")
        + _geometry_path(right_source, right_points, entity_type="LINE")
        + [_point_geometry(valve_source, (0.0, 2.0, 0.0))]
        + [_point_geometry(regulator_source, (0.0, 3.0, 0.0))]
        + [_point_geometry(anode_source, (0.0, 0.0, 0.0))]
    )
    candidates = []
    for role, source, source_y in (
        ("valve", valve_source, 2.0),
        ("regulator", regulator_source, 3.0),
    ):
        for ordinal, (edge, parameter) in enumerate(
            ((left_edges[0], 1.0), (right_edges[0], 0.0))
        ):
            candidate = _point_candidate(
                f"point:{role}:writer-contested:{ordinal}",
                source,
                edge,
                target_kind="quantized_endpoint",
                target_key=shared_node,
            )
            candidate.update(
                point_x_m=0.0,
                point_y_m=source_y,
                point_z_m=0.0,
                projection_x_m=0.0,
                projection_y_m=0.0,
                projection_z_m=0.0,
                host_parameter=parameter,
            )
            candidates.append(candidate)
    anode = _point_candidate(
        "point:anode:writer-noncut",
        anode_source,
        left_edges[0],
        target_kind="quantized_endpoint",
        target_key=shared_node,
    )
    anode.update(
        point_x_m=0.0,
        point_y_m=0.0,
        point_z_m=0.0,
        projection_x_m=0.0,
        projection_y_m=0.0,
        projection_z_m=0.0,
        host_parameter=1.0,
    )
    candidates.append(anode)
    endpoints = left_endpoints + right_endpoints
    topology = _topology_context(
        canonical=canonical,
        geometry=geometry,
        edges=left_edges + right_edges,
        endpoints=endpoints,
        point_to_path=pa.Table.from_pylist(candidates),
    )
    _module("topology_derivation").transform(topology)
    connectivity = _connectivity_context(
        topology,
        canonical=canonical,
        endpoints=endpoints,
    )
    _module("product_connectivity").transform(connectivity)
    return canonical, connectivity


def test_writer_preserves_complete_multisegment_source_facts_and_row_order() -> None:
    canonical, xdata, connectivity = _writer_multisegment_fixture()
    tables, validation_context = _writer_tables(
        canonical=canonical,
        xdata=xdata,
        connectivity=connectivity,
    )
    shuffled, _ = _writer_tables(
        canonical=canonical,
        xdata=xdata,
        connectivity=connectivity,
        reverse_inputs=True,
    )
    assert tables == shuffled
    assert set(tables) == {
        "source_dispositions",
        "products",
        "axis_vertices",
        "geometry_inputs",
        "ports",
        "connections",
        "systems",
        "system_members",
        "materials_styles",
        "property_values",
        "spatial_nodes",
        "spatial_assignments",
        "piperun_members",
        "unresolved_items",
        "product_origins",
        "connection_segments",
    }

    pipe, = [row for row in tables["products"] if row["product_kind"] == "PIPE"]
    upstream_vertices = connectivity.tables["vertices"].to_pylist()
    axis = sorted(tables["axis_vertices"], key=lambda row: row["sequence_ordinal"])
    assert len(axis) == 5
    for actual, expected in zip(axis, upstream_vertices, strict=True):
        assert actual["product_key"] == pipe["product_key"]
        for field in (
            "pipe_piece_key",
            "sequence_ordinal",
            "source_path_key",
            "source_entity_key",
            "geometry_element_key",
            "source_segment_ordinal",
            "source_parameter",
            "x_m",
            "y_m",
            "z_m",
            "event_keys",
        ):
            assert actual[field] == expected[field]

    pipe_geometry, = [
        row
        for row in tables["geometry_inputs"]
        if row["product_key"] == pipe["product_key"]
    ]
    assert pipe_geometry["input_kind"] == "PIPE_SOURCE_PATH_FACTS"
    assert pipe_geometry["length_m"] == pytest.approx(8.0)
    assert pipe_geometry["outer_diameter_m"] == 0.12
    assert tables["product_origins"] == connectivity.outputs[
        "product_origins"
    ].to_pylist()
    assert tables["connection_segments"] == connectivity.outputs[
        "connection_segments"
    ].to_pylist()
    assert tables["ports"] == connectivity.outputs["world_ports"].to_pylist()
    assert tables["connections"] == connectivity.outputs[
        "connection_pairs"
    ].to_pylist()
    property_row = next(
        row
        for row in tables["property_values"]
        if row["source_key"] == xdata[0]["canonical_xdata_key"]
    )
    assert (property_row["property_name"], property_row["value_string"]) == (
        "ASSET_CODE",
        "PIPE-UNIT-001",
    )
    retired = {
        "component_resource_key",
        "geometry_strategy",
        "trim_start_m",
        "trim_end_m",
        "takeoff_m",
        "connection_direction_a_x",
        "connection_direction_b_x",
        "local_direction_x",
    }
    assert not any(
        retired & set(row)
        for name in ("products", "geometry_inputs", "ports", "connections")
        for row in tables[name]
    )
    result = validate_writer_closure(tables, **validation_context)
    assert result["passed"], result["errors"]


def test_writer_validator_does_not_trust_output_source_classification() -> None:
    import copy

    canonical, xdata, connectivity = _writer_multisegment_fixture()
    tables, validation_context = _writer_tables(
        canonical=canonical,
        xdata=xdata,
        connectivity=connectivity,
    )
    accepted = validate_writer_closure(tables, **validation_context)
    assert accepted["passed"], accepted["errors"]
    broken = copy.deepcopy(tables)
    pipe = next(row for row in broken["products"] if row["product_kind"] == "PIPE")
    assert pipe["origin_kind"] == "source"
    pipe["origin_kind"] = None
    expected_xdata_key = xdata[0]["canonical_xdata_key"]
    removed = [
        row
        for row in broken["property_values"]
        if row["owner_key"] == pipe["product_key"]
        and row["source_key"] == expected_xdata_key
    ]
    assert len(removed) == 1
    broken["property_values"] = [
        row for row in broken["property_values"] if row not in removed
    ]

    result = validate_writer_closure(broken, **validation_context)
    assert result["passed"] is False
    assert {
        "PRODUCT_ORIGIN_KIND_MISMATCH",
        "SOURCE_XDATA_PROPERTY_MISMATCH",
    } <= {row["code"] for row in result["errors"]}


def test_writer_validator_accepts_a_real_design_event_classification() -> None:
    canonical, xdata, connectivity = _writer_multisegment_fixture(
        pressure="LOW",
        segment_length_m=4.0,
    )
    design_event = next(
        row
        for row in connectivity.tables["events"].to_pylist()
        if row["origin_kind"] == "design"
    )
    assert design_event["event_kind"] == "GAS_METER"
    tables, validation_context = _writer_tables(
        canonical=canonical,
        xdata=xdata,
        connectivity=connectivity,
    )
    design_product = next(
        row
        for row in tables["products"]
        if row["source_event_key"] == design_event["product_event_key"]
    )
    assert design_product["origin_kind"] == "design"
    derivation = [
        row
        for row in tables["property_values"]
        if row["owner_key"] == design_product["product_key"]
        and row["property_name"] == "DerivationRule"
        and row["source_key"] == design_event["product_event_key"]
    ]
    assert len(derivation) == 1
    assert derivation[0]["value_string"] == design_event["reason_code"]
    result = validate_writer_closure(tables, **validation_context)
    assert result["passed"], result["errors"]


def test_writer_preserves_all_contested_owners_and_non_cut_attachment() -> None:
    canonical, connectivity = _writer_contested_attachment_fixture()
    tables, validation_context = _writer_tables(
        canonical=canonical,
        connectivity=connectivity,
    )
    assert tables["ports"] == connectivity.outputs["world_ports"].to_pylist()
    assert tables["connections"] == connectivity.outputs[
        "connection_pairs"
    ].to_pylist()
    conflicts = [
        row
        for row in tables["unresolved_items"]
        if row["issue_code"] == "UNRESOLVED_EVENT_PORT_CONFLICT"
    ]
    assert conflicts
    conflict_subjects = {row["subject_key"] for row in conflicts}
    conflicting_ports = [
        row for row in tables["ports"] if row["port_key"] in conflict_subjects
    ]
    assert len(conflicting_ports) == len(conflict_subjects)
    assert all(row["connection_state"] == "unresolved" for row in conflicting_ports)
    assert all(
        row["blocked_actions"] == ["external_constraint_role_resolution"]
        for row in conflicts
    )
    attachments = [
        row
        for row in tables["connections"]
        if row["fact_status"] == "source_attachment"
    ]
    assert len(attachments) == 1
    assert attachments[0]["connection_scope"] == "non_cut_attachment"
    assert not any(
        row["fact_status"] == "source_business_pairing"
        and conflict_subjects
        & {row["port_a_key"], row["port_b_key"]}
        for row in tables["connections"]
    )
    pipe_keys = {
        row["product_key"]
        for row in tables["products"]
        if row["product_kind"] == "PIPE"
    }
    run_members = [
        row for row in tables["piperun_members"] if row["row_kind"] == "member"
    ]
    assert {row["product_key"] for row in run_members} == pipe_keys
    assert len({row["piperun_key"] for row in run_members}) == len(pipe_keys)
    result = validate_writer_closure(tables, **validation_context)
    assert result["passed"], result["errors"]


@pytest.mark.parametrize(
    ("fault", "expected_code"),
    [
        ("fabricated_winner", "CONNECTION_PROJECTION_MISMATCH"),
        ("dropped_conflict", "UNRESOLVED_PROJECTION_MISMATCH"),
    ],
)
def test_writer_validator_rejects_contested_winner_or_lost_issue(
    fault: str,
    expected_code: str,
) -> None:
    import copy

    canonical, connectivity = _writer_contested_attachment_fixture()
    tables, validation_context = _writer_tables(
        canonical=canonical,
        connectivity=connectivity,
    )
    broken = copy.deepcopy(tables)
    conflicts = [
        row
        for row in broken["unresolved_items"]
        if row["issue_code"] == "UNRESOLVED_EVENT_PORT_CONFLICT"
    ]
    if fault == "dropped_conflict":
        broken["unresolved_items"].remove(conflicts[0])
    else:
        contested_source_ports = [
            row
            for row in broken["ports"]
            if row["port_key"] in {issue["subject_key"] for issue in conflicts}
            and row["access_semantics"] == "source_pipe_endpoint"
        ]
        assert len(contested_source_ports) == 2
        broken["connections"].append(
            {
                "connection_key": "connection:fabricated-contested-winner",
                "port_a_key": contested_source_ports[0]["port_key"],
                "port_b_key": contested_source_ports[1]["port_key"],
                "connection_scope": "source_gas_pairing",
                "connection_reason": "fabricated_winner",
                "authority_key": "unit_negative_witness",
                "fact_status": "source_business_pairing",
            }
        )
    result = validate_writer_closure(broken, **validation_context)
    assert result["passed"] is False
    assert expected_code in {row["code"] for row in result["errors"]}


@pytest.mark.parametrize(
    ("fault", "expected_code"),
    [
        ("merged_disconnected_runs", "PIPERUN_UNSUPPORTED_MERGE"),
        ("duplicate_run_product", "INVALID_PIPERUN_MEMBER_OR_SEQUENCE"),
        ("duplicate_material_owner", "INVALID_MATERIAL_BINDING"),
    ],
)
def test_writer_validator_rejects_illegal_run_or_material_multiplicity(
    fault: str,
    expected_code: str,
) -> None:
    import copy

    canonical, connectivity = _writer_contested_attachment_fixture()
    tables, validation_context = _writer_tables(
        canonical=canonical,
        connectivity=connectivity,
    )
    accepted = validate_writer_closure(tables, **validation_context)
    assert accepted["passed"], accepted["errors"]
    broken = copy.deepcopy(tables)

    if fault == "merged_disconnected_runs":
        definitions = sorted(
            (
                row
                for row in broken["piperun_members"]
                if row["row_kind"] == "run_definition"
            ),
            key=lambda row: row["piperun_key"],
        )
        members = sorted(
            (
                row
                for row in broken["piperun_members"]
                if row["row_kind"] == "member"
            ),
            key=lambda row: row["product_key"],
        )
        assert len(definitions) == len(members) == 2
        assert len({row["homogeneity_key"] for row in definitions}) == 1
        retained = definitions[0]
        broken["piperun_members"] = [retained]
        for ordinal, member in enumerate(members, start=1):
            member["piperun_key"] = retained["piperun_key"]
            member["piperun_row_key"] = (
                f"pr:mem:{retained['piperun_key']}:{member['product_key']}"
            )
            member["name"] = retained["name"]
            member["membership_ordinal"] = ordinal
            member["homogeneity_key"] = retained["homogeneity_key"]
            broken["piperun_members"].append(member)
    elif fault == "duplicate_run_product":
        definition = next(
            row
            for row in broken["piperun_members"]
            if row["row_kind"] == "run_definition"
        )
        member = next(
            row
            for row in broken["piperun_members"]
            if row["row_kind"] == "member"
            and row["piperun_key"] == definition["piperun_key"]
        )
        duplicate_run_key = "piperun:duplicate-membership-witness"
        duplicate_definition = dict(definition)
        duplicate_definition.update(
            piperun_row_key="pr:def:" + duplicate_run_key,
            piperun_key=duplicate_run_key,
            name="Duplicate membership witness",
        )
        duplicate_member = dict(member)
        duplicate_member.update(
            piperun_row_key=(
                "pr:mem:" + duplicate_run_key + ":" + str(member["product_key"])
            ),
            piperun_key=duplicate_run_key,
            name="Duplicate membership witness",
            membership_ordinal=1,
        )
        broken["piperun_members"].extend(
            [duplicate_definition, duplicate_member]
        )
    else:
        binding = next(
            row
            for row in broken["materials_styles"]
            if row["row_kind"] == "binding"
        )
        duplicate = dict(binding)
        duplicate["material_style_key"] = (
            str(binding["material_style_key"]) + ":duplicate-owner-witness"
        )
        broken["materials_styles"].append(duplicate)

    result = validate_writer_closure(broken, **validation_context)
    assert result["passed"] is False
    assert expected_code in {row["code"] for row in result["errors"]}


@pytest.mark.parametrize(
    ("fault", "expected_code"),
    [
        ("missing_internal_vertex", "AXIS_SOURCE_PROJECTION_MISMATCH"),
        ("arc_length", "GEOMETRY_SOURCE_FACT_MISMATCH"),
        ("segment", "SEGMENT_PROJECTION_MISMATCH"),
        ("port", "PORT_PROJECTION_MISMATCH"),
        ("origin", "ORIGIN_PROJECTION_MISMATCH"),
        ("property_missing", "SOURCE_XDATA_PROPERTY_MISMATCH"),
        ("property_duplicate", "SOURCE_XDATA_PROPERTY_MISMATCH"),
        ("property_value", "SOURCE_XDATA_PROPERTY_MISMATCH"),
        ("property_null_source", "SOURCE_XDATA_PROPERTY_MISMATCH"),
        ("derived_rule_missing", "SOURCE_XDATA_PROPERTY_MISMATCH"),
        ("derived_rule_duplicate", "SOURCE_XDATA_PROPERTY_MISMATCH"),
        ("derived_rule_value", "SOURCE_XDATA_PROPERTY_MISMATCH"),
    ],
)
def test_writer_validator_rejects_source_fact_drift(
    fault: str,
    expected_code: str,
) -> None:
    import copy

    canonical, xdata, connectivity = _writer_multisegment_fixture(
        **({"pressure": "LOW", "segment_length_m": 4.0} if fault.startswith("derived_rule") else {})
    )
    tables, validation_context = _writer_tables(
        canonical=canonical,
        xdata=xdata,
        connectivity=connectivity,
    )
    broken = copy.deepcopy(tables)
    if fault == "missing_internal_vertex":
        del broken["axis_vertices"][2]
    elif fault == "arc_length":
        broken["geometry_inputs"][0]["length_m"] += 0.25
    elif fault == "segment":
        broken["connection_segments"][0]["b_x_m"] += 0.25
    elif fault == "port":
        broken["ports"][0]["retained_side"] = "wrong_side"
    elif fault.startswith(("property_", "derived_rule_")):
        rows = broken["property_values"]
        row = next(row for row in rows if (
            row["property_name"] == "DerivationRule" if fault.startswith("derived_rule")
            else row["source_key"] == xdata[0]["canonical_xdata_key"]
        ))
        if fault.endswith("missing"):
            rows.remove(row)
        elif fault.endswith("duplicate"):
            duplicate = copy.deepcopy(row)
            duplicate["property_value_key"] += ":independent-duplicate"
            rows.append(duplicate)
        elif fault.endswith("null_source"):
            row["source_key"] = None
        else:
            row["value_string"] = "wrong-source-value"
    else:
        broken["product_origins"][0]["origin_x_m"] += 0.25
    result = validate_writer_closure(broken, **validation_context)
    assert result["passed"] is False
    assert expected_code in {row["code"] for row in result["errors"]}


def _reconciliation_driver_module():
    path = ROOT / "scripts/maintenance/run_gas_network_reconciliation.py"
    spec = spec_from_file_location("gas_reconciliation_driver_test", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reconciliation_compare_module():
    path = ROOT / "scripts/maintenance/compare_gas_network_reconciliation.py"
    spec = spec_from_file_location("gas_reconciliation_compare_test", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_topology_riser_alignment_inline_table_preserves_all_122_approved_pairs() -> None:
    from types import SimpleNamespace

    from CAD2BIM_backend.application.data_transform_service import _inline_arrow_inputs
    from CAD2BIM_backend.domain.data_transform import InlineTable

    module = _reconciliation_driver_module()
    definition, = module._topology_inline_tables()
    table = InlineTable.model_validate(definition)
    assert len(table.rows) == 122
    assert table.model_dump(mode="json") == definition
    arrow_input, = _inline_arrow_inputs(SimpleNamespace(inline_tables=(table,)))
    assert arrow_input.input_name == "riser_endpoint_alignments"
    rows = arrow_input.reader.read_all().to_pylist()
    pairs = [(row["riser_source_entity_key"], row["pipe_source_entity_key"]) for row in rows]
    assert pairs == [tuple(pair.split(":")) for pair in module._APPROVED_RISER_ENDPOINT_PAIRS]
    assert len(dict(pairs)) == 122


def test_topology_plan_bundles_controlled_code_and_explicit_unit_parameters() -> None:
    from CAD2BIM_backend.infrastructure.data_transform.capabilities import (
        PYTHON_ARROW_APPROVED_LIBRARIES,
        PYTHON_ARROW_SAFE_STDLIB,
    )
    from CAD2BIM_backend.infrastructure.worker_runtime.python_user_code import compile_entry

    module = _reconciliation_driver_module()
    name, inputs, outputs, path, package_name = module._topology_plan(
        "data_071", "data_084", "data_092"
    )
    assert name == "topology"
    assert inputs[0] == ("ge", "data_071", "geometry_elements")
    assert [row[0] for row in outputs] == [
        "candidate_decisions",
        "host_cuts",
        "pipe_pieces",
        "pipe_path_vertices",
        "product_events",
    ]
    assert next(row for row in outputs if row[0] == "pipe_path_vertices")[2] == [
        "pipe_piece_key",
        "sequence_ordinal",
    ]
    source, evidence = bundled_program_source(path)
    assert evidence == {}
    assert "def transform(ctx):" in source
    compile_entry(
        source,
        "transform",
        allowed_import_roots={
            *PYTHON_ARROW_APPROVED_LIBRARIES,
            *PYTHON_ARROW_SAFE_STDLIB,
        },
    )
    lineage = {
        "source_cad_artifact": {"sha256": module.APPROVED_SOURCE_CAD_SHA256},
        "host_endpoint_guard_m": 0.025,
        "unit_decision": {
            "authority": "GAS-UNIT-210",
            "header_unit_status": "CONFLICTED_WITH_PROJECT_DECISION",
            "source_drawing_unit": "INCH_DECLARED",
            "selected_source_xy_unit": "METRE",
            "target_ifc_length_unit": "METRE",
            "source_unit_to_m": 1.0,
            "source_to_project_rotation_rad": 0.0,
            "source_to_project_translation_m": [0.0, 0.0, 0.0],
            "outer_diameter_unit_rule": "kj millimetres to metres independently of source XY",
        },
    }
    parameters = module._topology_parameters(lineage)
    assert {row["name"]: row["value"] for row in parameters} == {
        "source_unit_to_m": 1.0,
        "host_endpoint_guard_m": 0.025,
        "source_cad_sha256": module.APPROVED_SOURCE_CAD_SHA256,
        "authority": "GAS-UNIT-210",
        "header_unit_status": "CONFLICTED_WITH_PROJECT_DECISION",
        "source_drawing_unit": "INCH_DECLARED",
        "selected_source_xy_unit": "METRE",
        "target_ifc_length_unit": "METRE",
        "outer_diameter_unit_rule": "kj millimetres to metres independently of source XY",
        "source_to_project_rotation_rad": 0.0,
        "source_to_project_translation_m": [0.0, 0.0, 0.0],
    }
    command = module._command(
        inputs=inputs,
        outputs=outputs,
        code=source,
        name=package_name,
        intended_use="unit contract",
        parameters=parameters,
        inline_tables=module._topology_inline_tables(),
    )
    dumped = command.model_dump(mode="json")
    assert dumped["parameters"] == parameters
    assert dumped["inline_tables"] == module._topology_inline_tables()
    assert dumped["inputs"][0] == {
        "input_name": "ge",
        "package_alias": "data_071",
        "payload_name": "geometry_elements",
    }


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        (None, None),
        ("wrong_id", "expected exact Package id"),
        ("different_l0_geometry_member", "exact L0 members"),
        ("wrong_unit", "source or unit"),
    ],
)
def test_topology_lineage_accepts_exact_inputs_and_rejects_identity_geometry_or_unit_drift(
    monkeypatch,
    fault: str | None,
    message: str | None,
) -> None:
    import asyncio
    import hashlib
    import json
    from types import SimpleNamespace

    from CAD2BIM_backend.domain.data_runtime import (
        ArtifactVersionRef,
        DataPackageKind,
        PackageMemberKind,
        SOURCE_L0_LOGICAL_KEYS,
        SOURCE_L0_PAYLOAD_ORDER,
        source_l0_payload_member_name,
    )

    module = _reconciliation_driver_module()
    aliases = {"l0": "data_071", "canonical": "data_084", "measurement": "data_092"}
    ids = {role: f"package:{role}" for role in aliases}
    downloads: dict[str, bytes] = {}

    def artifact(label: str, sha256: str | None = None) -> ArtifactVersionRef:
        return ArtifactVersionRef(
            artifact_id=f"artifact:{label}",
            version_id=f"version:{label}",
            sha256=sha256 or hashlib.sha256(label.encode()).hexdigest(),
        )

    def member(
        role: str,
        member_name: str,
        kind: PackageMemberKind,
        media_type: str,
        *,
        logical_payload_name: str | None = None,
        logical_key_fields: tuple[str, ...] = (),
        row_count: int | None = None,
        exact_artifact: ArtifactVersionRef | None = None,
    ):
        return SimpleNamespace(
            member_id=f"member:{role}:{member_name}",
            owner_id="owner",
            tenant_id="tenant",
            task_id="task",
            package_id=ids[role],
            member_name=member_name,
            member_kind=kind,
            artifact=exact_artifact or artifact(f"{role}:{member_name}"),
            media_type=media_type,
            row_count=row_count,
            logical_payload_name=logical_payload_name,
            logical_key_fields=logical_key_fields,
        )

    l0_members = []
    l0_payload_members = {}
    for payload_name in SOURCE_L0_PAYLOAD_ORDER:
        payload_member = member(
            "l0",
            source_l0_payload_member_name(payload_name),
            PackageMemberKind.PAYLOAD,
            "application/vnd.apache.parquet",
            row_count=1,
            logical_payload_name=None,
            logical_key_fields=(),
        )
        l0_members.append(payload_member)
        l0_payload_members[payload_name] = payload_member
        assert SOURCE_L0_LOGICAL_KEYS[payload_name]
    l0_members.append(
        member(
            "l0",
            "source/original.dxf",
            PackageMemberKind.SOURCE,
            "application/dxf",
            exact_artifact=artifact("source", module.APPROVED_SOURCE_CAD_SHA256),
        )
    )

    canonical_payloads = ("canonical_entities",)
    measurement_payloads = (
        "atomic_edges",
        "endpoints",
        "endpoint_pairs",
        "endpoint_on_path",
        "point_to_path",
    )

    def payload_members(role: str, names: tuple[str, ...]):
        return [
            member(
                role,
                f"payloads/{name}/part-00000.parquet",
                PackageMemberKind.PAYLOAD,
                "application/vnd.apache.parquet",
                logical_payload_name=name,
                logical_key_fields=(f"{name}_key",),
                row_count=1,
            )
            for name in names
        ]

    canonical_members = payload_members("canonical", canonical_payloads)
    measurement_members = payload_members("measurement", measurement_payloads)
    l0_inputs = {
        name: {
            "package_id": ids["l0"],
            "member_id": l0_payload_members[name].member_id,
            "payload_name": name,
        }
        for name in SOURCE_L0_PAYLOAD_ORDER
    }
    canonical_manifest = {
        "package_id": ids["canonical"],
        "transformation": {
            "inputs": [l0_inputs[name] for name in ("entities", "geometry_elements", "xdata_tags")]
        },
    }
    measurement_manifest = {
        "package_id": ids["measurement"],
        "transformation": {
            "inputs": [l0_inputs[name] for name in ("entities", "geometry_elements", "relationships")],
            "tool_name": "cad_compute_measurements",
            "source_package_id": ids["l0"],
            "source_unit_to_m": 1.0,
            "tolerance_spec": {"host_endpoint_guard_m": 0.025},
        },
    }
    l0_manifest = {
        "source": {"sha256": module.APPROVED_SOURCE_CAD_SHA256},
    }
    if fault == "different_l0_geometry_member":
        canonical_manifest["transformation"]["inputs"][1] = {
            **canonical_manifest["transformation"]["inputs"][1],
            "member_id": "member:l0:different-geometry",
        }
    if fault == "wrong_unit":
        measurement_manifest["transformation"]["source_unit_to_m"] = 0.0254

    manifests = {
        "l0": l0_manifest,
        "canonical": canonical_manifest,
        "measurement": measurement_manifest,
    }
    packages = {}
    for role, role_members in (
        ("l0", l0_members),
        ("canonical", canonical_members),
        ("measurement", measurement_members),
    ):
        content = json.dumps(manifests[role], sort_keys=True).encode()
        manifest_artifact = artifact(
            f"{role}:manifest",
            hashlib.sha256(content).hexdigest(),
        )
        manifest_member = member(
            role,
            "manifest.json",
            PackageMemberKind.MANIFEST,
            "application/json",
            exact_artifact=manifest_artifact,
        )
        downloads[manifest_artifact.artifact_id] = content
        packages[aliases[role]] = SimpleNamespace(
            package=SimpleNamespace(
                package_id=ids[role],
                package_kind=(
                    DataPackageKind.SOURCE_L0 if role == "l0" else DataPackageKind.DERIVED
                ),
                owner_id="owner",
                tenant_id="tenant",
                task_id="task",
            ),
            members=tuple(role_members + [manifest_member]),
        )

    async def package(*args):
        return packages[args[-1]]

    async def download(owner_id, artifact_id, version_id, **kwargs):
        return SimpleNamespace(content=downloads[artifact_id])

    monkeypatch.setattr(module, "_package", package)
    bindings = {
        role: (
            alias,
            "package:not-l0" if fault == "wrong_id" and role == "l0" else ids[role],
        )
        for role, alias in aliases.items()
    }
    inputs = module._topology_plan(*aliases.values())[1]
    invocation = module._verify_topology_inputs(
        None,
        SimpleNamespace(download_version=download),
        SimpleNamespace(owner_id="owner", run_id="run"),
        bindings=bindings,
        inputs=inputs,
    )
    if message is not None:
        with pytest.raises(RuntimeError, match=message):
            asyncio.run(invocation)
    else:
        evidence = asyncio.run(invocation)
        assert evidence["same_exact_l0_ancestry_verified"] is True
        assert evidence["unit_decision"]["source_unit_to_m"] == 1.0
        assert len(evidence["exact_inputs"]) == 7
        assert evidence["exact_inputs"][0]["member_id"] == l0_payload_members[
            "geometry_elements"
        ].member_id


def test_connectivity_plan_is_self_contained_and_has_only_source_fact_outputs() -> None:
    from CAD2BIM_backend.infrastructure.data_transform.capabilities import (
        PYTHON_ARROW_APPROVED_LIBRARIES,
        PYTHON_ARROW_SAFE_STDLIB,
    )
    from CAD2BIM_backend.infrastructure.worker_runtime.python_user_code import compile_entry

    module = _reconciliation_driver_module()
    name, inputs, outputs, path, package_name = module._connectivity_plan(
        "data_101", "data_084", "data_092"
    )
    assert name == "connectivity"
    assert inputs == (
        ("pieces", "data_101", "pipe_pieces"),
        ("vertices", "data_101", "pipe_path_vertices"),
        ("events", "data_101", "product_events"),
        ("cuts", "data_101", "host_cuts"),
        ("decisions", "data_101", "candidate_decisions"),
        ("canonical", "data_084", "canonical_entities"),
        ("endpoints", "data_092", "endpoints"),
    )
    assert [row[0] for row in outputs] == [
        "product_instances",
        "product_origins",
        "connection_segments",
        "world_ports",
        "connection_pairs",
        "connectivity_issues",
    ]
    assert all(row[0] != "owner_frames" for row in outputs)
    source, evidence = bundled_program_source(path)
    assert evidence == {}
    assert "current_stage_resource_geometry_worker" not in source
    assert "plan_elbow_interface" not in source
    assert "plan_tee_interface" not in source
    assert "plan_regulator_interfaces_worker" not in source
    compile_entry(
        source,
        "transform",
        allowed_import_roots={
            *PYTHON_ARROW_APPROVED_LIBRARIES,
            *PYTHON_ARROW_SAFE_STDLIB,
        },
    )
    command = module._command(
        inputs=inputs,
        outputs=outputs,
        code=source,
        name=package_name,
        intended_use="unit contract",
    )
    assert [row.payload_name for row in command.outputs] == [row[0] for row in outputs]


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        (None, None),
        ("wrong_topology_id", "expected exact Package id"),
        ("drifted_ancestor_member", "exact L0/canonical/measurement members"),
        ("drifted_unit_parameter", "unit parameters"),
        ("missing_inline_table", "inline tables"),
        ("missing_alignment_row", "inline tables"),
        ("extra_alignment_row", "inline tables"),
        ("drifted_alignment", "inline tables"),
        ("drifted_alignment_column_type", "inline tables"),
    ],
)
def test_connectivity_lineage_binds_exact_topology_and_approved_unit_parameters(
    monkeypatch,
    fault: str | None,
    message: str | None,
) -> None:
    import asyncio
    import hashlib
    import json
    from types import SimpleNamespace

    from CAD2BIM_backend.domain.data_runtime import (
        ArtifactVersionRef,
        DataPackageKind,
        PackageMemberKind,
    )

    module = _reconciliation_driver_module()
    aliases = {
        "l0": "data_071",
        "canonical": "data_084",
        "measurement": "data_092",
        "topology": "data_101",
    }
    ids = {role: f"package:{role}" for role in aliases}

    def artifact(label: str, sha256: str | None = None) -> ArtifactVersionRef:
        return ArtifactVersionRef(
            artifact_id=f"artifact:{label}",
            version_id=f"version:{label}",
            sha256=sha256 or hashlib.sha256(label.encode()).hexdigest(),
        )

    ancestry_inputs = []
    for input_name, alias, payload_name in module._topology_plan(
        aliases["l0"], aliases["canonical"], aliases["measurement"]
    )[1]:
        role = next(role for role, value in aliases.items() if value == alias)
        ancestry_inputs.append(
            {
                "input_name": input_name,
                "alias": alias,
                "package_id": ids[role],
                "member_id": f"member:{role}:{payload_name}",
                "payload_name": payload_name,
                "artifact": artifact(f"{role}:{payload_name}").model_dump(mode="json"),
            }
        )
    ancestry = {
        "exact_inputs": ancestry_inputs,
        "manifest_sha256": {
            "l0": "1" * 64,
            "canonical": "2" * 64,
            "measurement": "3" * 64,
        },
        "source_cad_artifact": artifact(
            "source", module.APPROVED_SOURCE_CAD_SHA256
        ).model_dump(mode="json"),
        "unit_decision": {
            "authority": "GAS-UNIT-210",
            "header_unit_status": "CONFLICTED_WITH_PROJECT_DECISION",
            "source_drawing_unit": "INCH_DECLARED",
            "selected_source_xy_unit": "METRE",
            "target_ifc_length_unit": "METRE",
            "source_unit_to_m": 1.0,
            "source_to_project_rotation_rad": 0.0,
            "source_to_project_translation_m": [0.0, 0.0, 0.0],
            "outer_diameter_unit_rule": "kj millimetres to metres independently of source XY",
        },
        "same_exact_l0_ancestry_verified": True,
        "host_endpoint_guard_m": 0.025,
    }

    topology_members = []
    for payload_name in (
        "pipe_pieces",
        "pipe_path_vertices",
        "product_events",
        "host_cuts",
        "candidate_decisions",
    ):
        topology_members.append(
            SimpleNamespace(
                member_id=f"member:topology:{payload_name}",
                owner_id="owner",
                tenant_id="tenant",
                task_id="task",
                package_id=ids["topology"],
                member_name=f"payloads/{payload_name}/part-00000.parquet",
                member_kind=PackageMemberKind.PAYLOAD,
                artifact=artifact(f"topology:{payload_name}"),
                media_type="application/vnd.apache.parquet",
                row_count=1,
                logical_payload_name=payload_name,
                logical_key_fields=(f"{payload_name}_key",),
            )
        )
    manifest = {
        "package_id": ids["topology"],
        "transformation": {
            "inputs": [
                {
                    field: row[field]
                    for field in ("input_name", "package_id", "member_id", "payload_name")
                }
                for row in ancestry_inputs
            ],
            "parameters": module._topology_parameters(ancestry),
            "inline_tables": module._topology_inline_tables(),
        },
    }
    if fault == "drifted_ancestor_member":
        manifest["transformation"]["inputs"][0]["member_id"] = "member:drifted"
    if fault == "drifted_unit_parameter":
        manifest["transformation"]["parameters"][0]["value"] = 0.0254
    if fault == "missing_inline_table":
        del manifest["transformation"]["inline_tables"]
    elif fault == "missing_alignment_row":
        manifest["transformation"]["inline_tables"][0]["rows"].pop()
    elif fault == "extra_alignment_row":
        table = manifest["transformation"]["inline_tables"][0]
        table["rows"].append(table["rows"][0])
    elif fault == "drifted_alignment":
        manifest["transformation"]["inline_tables"][0]["rows"][0]["values"][1]["value"] = "unapproved-host"
    elif fault == "drifted_alignment_column_type":
        manifest["transformation"]["inline_tables"][0]["columns"][0]["type"] = "integer"
    manifest_content = json.dumps(manifest, sort_keys=True).encode()
    manifest_artifact = artifact(
        "topology:manifest", hashlib.sha256(manifest_content).hexdigest()
    )
    topology_members.append(
        SimpleNamespace(
            member_id="member:topology:manifest",
            owner_id="owner",
            tenant_id="tenant",
            task_id="task",
            package_id=ids["topology"],
            member_name="manifest.json",
            member_kind=PackageMemberKind.MANIFEST,
            artifact=manifest_artifact,
            media_type="application/json",
            row_count=None,
            logical_payload_name=None,
            logical_key_fields=(),
        )
    )
    topology = SimpleNamespace(
        package=SimpleNamespace(
            package_id=ids["topology"],
            package_kind=DataPackageKind.DERIVED,
            owner_id="owner",
            tenant_id="tenant",
            task_id="task",
        ),
        members=tuple(topology_members),
    )

    async def verify_ancestry(*args, **kwargs):
        return ancestry

    async def package(*args):
        return topology

    async def download(owner_id, artifact_id, version_id, **kwargs):
        assert artifact_id == manifest_artifact.artifact_id
        return SimpleNamespace(content=manifest_content)

    monkeypatch.setattr(module, "_verify_topology_inputs", verify_ancestry)
    monkeypatch.setattr(module, "_package", package)
    bindings = {
        role: (
            alias,
            "package:wrong-topology"
            if role == "topology" and fault == "wrong_topology_id"
            else ids[role],
        )
        for role, alias in aliases.items()
    }
    inputs = module._connectivity_plan(
        aliases["topology"], aliases["canonical"], aliases["measurement"]
    )[1]
    invocation = module._verify_connectivity_inputs(
        None,
        SimpleNamespace(download_version=download),
        SimpleNamespace(owner_id="owner", run_id="run"),
        bindings=bindings,
        inputs=inputs,
    )
    if message is not None:
        with pytest.raises(RuntimeError, match=message):
            asyncio.run(invocation)
    else:
        evidence = asyncio.run(invocation)
        assert evidence["matches_exact_topology_ancestry"] is True
        assert evidence["topology_package_id"] == ids["topology"]
        assert len(evidence["exact_inputs"]) == 7
        assert {row["input_name"] for row in evidence["exact_inputs"]} == {
            row[0] for row in inputs
        }


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        (None, None),
        ("wrong_connectivity_id", "expected exact Package id"),
        ("missing_input_payload", "writer input payload missing"),
        ("drifted_connectivity_ancestry", "exact source ancestry"),
        ("missing_writer_payload", "sixteen source-fact payloads"),
    ],
)
def test_writer_lineage_binds_exact_source_closure_and_sixteen_payloads(
    monkeypatch,
    fault: str | None,
    message: str | None,
) -> None:
    import asyncio
    from types import SimpleNamespace

    module = _reconciliation_driver_module()
    aliases = {
        "l0": "data_071",
        "canonical": "data_084",
        "measurement": "data_092",
        "topology": "data_101",
        "connectivity": "data_112",
        "writer": "data_120",
    }
    ids = {role: f"package:{role}" for role in aliases}
    inputs = module._writer_plan(
        aliases["canonical"], aliases["topology"], aliases["connectivity"]
    )[1]
    connectivity_inputs = module._connectivity_plan(
        aliases["topology"], aliases["canonical"], aliases["measurement"]
    )[1]

    def artifact(label: str):
        return SimpleNamespace(
            artifact_id=f"artifact:{label}",
            version_id=f"version:{label}",
            sha256=(label.encode().hex() + "0" * 64)[:64],
            model_dump=lambda **_: {
                "artifact_id": f"artifact:{label}",
                "version_id": f"version:{label}",
                "sha256": (label.encode().hex() + "0" * 64)[:64],
            },
        )

    packages = {
        alias: SimpleNamespace(
            package=SimpleNamespace(package_id=ids[role]),
            members=(),
        )
        for role, alias in aliases.items()
    }
    payloads = {alias: {} for alias in aliases.values()}
    for name, alias, payload_name in inputs:
        member = SimpleNamespace(
            member_id=f"member:{name}",
            artifact=artifact(name),
        )
        payloads[alias][payload_name] = SimpleNamespace(member=member)
    expected_outputs = [
        row[0]
        for row in module._writer_plan(
            aliases["canonical"], aliases["topology"], aliases["connectivity"]
        )[2]
    ]
    for payload_name in expected_outputs:
        payloads[aliases["writer"]][payload_name] = SimpleNamespace(
            member=SimpleNamespace(
                member_id=f"member:writer:{payload_name}",
                artifact=artifact("writer:" + payload_name),
            )
        )
    if fault == "missing_input_payload":
        del payloads[aliases["connectivity"]]["connection_segments"]
    if fault == "missing_writer_payload":
        del payloads[aliases["writer"]]["connection_segments"]

    ancestry_inputs = [
        {
            "input_name": name,
            "alias": alias,
            "payload_name": payload_name,
            "package_id": ids[
                next(role for role, value in aliases.items() if value == alias)
            ],
            "member_id": f"member:ancestor:{name}",
            "artifact": artifact("ancestor:" + name).model_dump(),
        }
        for name, alias, payload_name in connectivity_inputs
    ]
    ancestry = {
        "exact_inputs": ancestry_inputs,
        "matches_exact_topology_ancestry": True,
    }
    connectivity_manifest = {
        "package_id": ids["connectivity"],
        "transformation": {
            "inputs": [
                {
                    field: row[field]
                    for field in (
                        "input_name",
                        "package_id",
                        "member_id",
                        "payload_name",
                    )
                }
                for row in ancestry_inputs
            ]
        },
    }
    if fault == "drifted_connectivity_ancestry":
        connectivity_manifest["transformation"]["inputs"][0][
            "member_id"
        ] = "member:drifted"
    writer_manifest_inputs = []
    for name, alias, payload_name in inputs:
        member = payloads[alias].get(payload_name)
        if member is None:
            continue
        role = next(role for role, value in aliases.items() if value == alias)
        writer_manifest_inputs.append(
            {
                "input_name": name,
                "package_id": ids[role],
                "member_id": member.member.member_id,
                "payload_name": payload_name,
            }
        )
    writer_manifest = {
        "package_id": ids["writer"],
        "transformation": {"inputs": writer_manifest_inputs},
    }

    async def verify_connectivity(*args, **kwargs):
        return ancestry

    async def package(*args):
        return packages[args[-1]]

    async def manifest(*args):
        package_id = args[-1].package.package_id
        if package_id == ids["connectivity"]:
            return connectivity_manifest, "c" * 64
        if package_id == ids["writer"]:
            return writer_manifest, "w" * 64
        raise AssertionError(package_id)

    class Resolver:
        def __init__(self, *args):
            pass

        def parquet_payloads(self, package):
            alias = next(
                alias
                for role, alias in aliases.items()
                if ids[role] == package.package.package_id
            )
            return payloads[alias]

    monkeypatch.setattr(module, "_verify_connectivity_inputs", verify_connectivity)
    monkeypatch.setattr(module, "_package", package)
    monkeypatch.setattr(module, "_manifest", manifest)
    monkeypatch.setattr(module, "ExactPayloadResolver", Resolver)
    bindings = {
        role: (
            aliases[role],
            "package:wrong-connectivity"
            if fault == "wrong_connectivity_id" and role == "connectivity"
            else ids[role],
        )
        for role in ("l0", "canonical", "measurement", "topology", "connectivity")
    }
    if fault == "missing_writer_payload":
        bindings["writer"] = (aliases["writer"], ids["writer"])
        invocation = module._verify_writer_package(
            None,
            None,
            SimpleNamespace(owner_id="owner", run_id="run"),
            bindings=bindings,
        )
    else:
        invocation = module._verify_writer_inputs(
            None,
            None,
            SimpleNamespace(owner_id="owner", run_id="run"),
            bindings=bindings,
            inputs=inputs,
        )
    if message is not None:
        with pytest.raises(RuntimeError, match=message):
            asyncio.run(invocation)
    else:
        evidence = asyncio.run(invocation)
        assert evidence["matches_exact_source_ancestry"] is True
        assert evidence["connectivity_package_id"] == ids["connectivity"]
        assert len(evidence["exact_inputs"]) == 12


def test_writer_preserves_partial_origin_each_side_od_and_issue_actions() -> None:
    connectivity, event_key = _two_sided_device_context(
        "valve",
        right_od=None,
        source_origin_point=(None, 2.0, 0.0),
    )
    canonical = connectivity.tables["canonical"].to_pylist()
    tables, validation_context = _writer_tables(
        canonical=canonical,
        connectivity=connectivity,
    )
    product = next(
        row for row in tables["products"] if row["source_event_key"] == event_key
    )
    origin = next(
        row
        for row in tables["product_origins"]
        if row["product_key"] == product["product_key"]
    )
    assert (origin["origin_x_m"], origin["origin_y_m"], origin["origin_z_m"]) == (
        None,
        2.0,
        0.0,
    )
    assert origin["origin_status"] == "missing"
    ports = sorted(
        (row for row in tables["ports"] if row["source_event_key"] == event_key),
        key=lambda row: row["port_role"],
    )
    assert [row["outer_diameter_m"] for row in ports] == [0.1, None]
    assert [row["diameter_status"] for row in ports] == ["known", "missing"]
    assert tables["product_origins"] == connectivity.outputs[
        "product_origins"
    ].to_pylist()
    assert tables["connection_segments"] == connectivity.outputs[
        "connection_segments"
    ].to_pylist()
    assert tables["ports"] == connectivity.outputs["world_ports"].to_pylist()
    assert tables["connections"] == connectivity.outputs[
        "connection_pairs"
    ].to_pylist()
    expected_issues = {
        row["issue_key"]: row
        for row in connectivity.outputs["connectivity_issues"].to_pylist()
    }
    actual_issues = {
        row["unresolved_item_key"]: row for row in tables["unresolved_items"]
    }
    assert set(actual_issues) == set(expected_issues)
    for issue_key, expected in expected_issues.items():
        actual = actual_issues[issue_key]
        for field, value in expected.items():
            if field == "issue_key":
                continue
            assert actual[field] == value
    missing_od = next(
        row
        for row in tables["unresolved_items"]
        if row["affected_field"] == "outer_diameter_m"
        and row["resolution_status"] == "open"
    )
    assert missing_od["subject_key"] == ports[1]["port_key"]
    assert missing_od["blocked_actions"] == [
        "family_geometry",
        "ifc_network_binding",
        "interface_profile",
    ]
    result = validate_writer_closure(tables, **validation_context)
    assert result["passed"], result["errors"]


def test_unique_equal_signature_endpoint_on_path_tee_remains_review_only() -> None:
    module = _module("topology_derivation")
    branch = _edge("edge:branch", "pipe:branch", (0.0, 0.001, 0.0), (0.0, 1.0, 0.0))
    host = _edge("edge:host", "pipe:host", (-5.0, 0.0, 0.0), (5.0, 0.0, 0.0))
    endpoints = _endpoints(branch, "q:branch", "q:branch-end") + _endpoints(host, "q:host-a", "q:host-b")
    ctx = _topology_context(
        canonical=[_canonical("pipe:branch", od=0.1), _canonical("pipe:host", od=0.1)],
        geometry=(
            _geometry_path("pipe:branch", [(0.0, 0.001, 0.0), (0.0, 1.0, 0.0)], entity_type="LINE")
            + _geometry_path("pipe:host", [(-5.0, 0.0, 0.0), (5.0, 0.0, 0.0)], entity_type="LINE")
        ),
        edges=[branch, host],
        endpoints=endpoints,
        endpoint_on_path=pa.Table.from_pylist([{
            "endpoint_on_path_key": "eop:1", "branch_endpoint_key": branch["start_endpoint_key"],
            "branch_atomic_edge_key": branch["atomic_edge_key"], "branch_source_entity_key": branch["source_entity_key"],
            "host_atomic_edge_key": host["atomic_edge_key"], "host_source_entity_key": host["source_entity_key"],
            "projection_x_m": 0.0, "projection_y_m": 0.0, "projection_z_m": 0.0,
            "host_parameter": 0.5, "distance_3d_m": 0.001,
            "same_source_entity": False, "passes_host_endpoint_guard": True,
        }]),
    )
    module.transform(ctx)
    decision = next(row for row in ctx.outputs["candidate_decisions"].to_pylist() if row["relation_family"] == "endpoint_on_path")
    assert decision["decision_kind"] == "REVIEW"
    assert decision["accepted"] is False
    assert not any(row["product_event_key"].startswith("eop:") for row in ctx.outputs["product_events"].to_pylist())


def test_writer_keeps_riser_origin_height_and_connectivity_owned_od() -> None:
    connectivity, _, _ = _accepted_terminal_riser_connectivity_context()
    _module("product_connectivity").transform(connectivity)
    canonical = connectivity.tables["canonical"].to_pylist()
    tables, validation_context = _writer_tables(
        canonical=canonical,
        connectivity=connectivity,
    )
    riser = next(row for row in tables["products"] if row["product_kind"] == "RISER")
    origin = next(
        row
        for row in tables["product_origins"]
        if row["product_key"] == riser["product_key"]
    )
    assert (origin["origin_x_m"], origin["origin_y_m"], origin["origin_z_m"]) == (
        0.0,
        0.0,
        0.0,
    )
    axis = sorted(
        (
            row
            for row in tables["axis_vertices"]
            if row["product_key"] == riser["product_key"]
        ),
        key=lambda row: row["sequence_ordinal"],
    )
    assert [(row["x_m"], row["y_m"], row["z_m"]) for row in axis] == [
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 1.5),
    ]
    assert all(
        row[field] is None
        for row in axis
        for field in (
            "source_path_key",
            "pipe_piece_key",
            "source_segment_ordinal",
            "source_parameter",
        )
    )
    geometry = next(
        row
        for row in tables["geometry_inputs"]
        if row["product_key"] == riser["product_key"]
    )
    assert geometry["input_kind"] == "RISER_SOURCE_AND_APPROVED_HEIGHT_FACTS"
    assert geometry["height_m"] == 1.5
    assert geometry["outer_diameter_m"] == 0.1
    assert not any(
        row["property_name"].startswith("InferredOuterDiameter")
        for row in tables["property_values"]
    )
    assert tables["ports"] == connectivity.outputs["world_ports"].to_pylist()
    assert tables["connections"] == connectivity.outputs[
        "connection_pairs"
    ].to_pylist()
    derived_products = [
        row for row in tables["products"] if row["origin_kind"] == "derived"
    ]
    assert {row["product_kind"] for row in derived_products} == {"RISER_BEND"}
    event_by_key = {
        row["product_event_key"]: row
        for row in connectivity.tables["events"].to_pylist()
    }
    for product in derived_products:
        event = event_by_key[product["source_event_key"]]
        derivation = [
            row
            for row in tables["property_values"]
            if row["owner_key"] == product["product_key"]
            and row["property_name"] == "DerivationRule"
            and row["source_key"] == event["product_event_key"]
        ]
        assert len(derivation) == 1
        assert derivation[0]["value_string"] == event["reason_code"]
    result = validate_writer_closure(tables, **validation_context)
    assert result["passed"], result["errors"]


def _accepted_terminal_riser_connectivity_context(
    *, approved_competing_regulator: bool = False, serial_relation: bool = False,
    missing_pipe_od: bool = False,
) -> tuple[
    _Context,
    dict[str, object],
    dict[str, object],
]:
    topology = _module("topology_derivation")
    pipe = _edge("edge:pipe", "pipe:a", (0.0, 0.0, 0.0), (10.0, 0.0, 0.0))
    endpoints = _endpoints(pipe, "q:start", "q:end")
    candidate = _point_candidate("point:1", "riser:a", pipe, target_kind="quantized_endpoint", target_key="q:start")
    canonical = [
        _canonical("pipe:a", od=0.1, pressure="MEDIUM"),
        _canonical("riser:a", od=0.1, pressure="MEDIUM", role="riser"),
    ]
    candidates = [candidate]
    device_geometry = [_point_geometry("riser:a", (0.0, 0.0, 0.0))]
    if approved_competing_regulator:
        topology._RISER_CANDIDATE_SELECTIONS = {"riser:a": "point:1"}
        canonical[1]["pressure_normalized"] = "LOW"
        canonical.append(_canonical("regulator:competitor", od=0.1, pressure="MEDIUM", role="regulator"))
        candidates.append({**candidate, "point_entity_key": "regulator:competitor", "point_to_path_key": "point:regulator"})
        device_geometry.append(_point_geometry("regulator:competitor", (0.0, 0.0, 0.0)))
    if serial_relation:
        assert approved_competing_regulator
        topology._SERIAL_REGULATOR_RISERS = {"regulator:competitor": ("pipe:a", "riser:a")}
        canonical[0]["pressure_normalized"] = "MEDIUM_B"
        canonical[2]["pressure_normalized"] = "MEDIUM_A"
        canonical[2]["outer_diameter_m"] = None
    first = _topology_context(
        canonical=canonical,
        geometry=(
            _geometry_path(
                "pipe:a",
                [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)],
                entity_type="LINE",
            )
            + device_geometry
        ),
        edges=[pipe],
        endpoints=endpoints,
        point_to_path=pa.Table.from_pylist(candidates),
    )
    topology.transform(first)
    events = first.outputs["product_events"].to_pylist()
    riser = next(row for row in events if row["event_kind"] == "RISER")
    bend = next(row for row in events if row["event_kind"] == "RISER_BEND")
    assert riser["status"] == "accepted"
    assert bend["target_key"] == "q:start"
    if missing_pipe_od:
        # Exercise missing dimensions at the connectivity owner with a declared
        # accepted relation; this does not extend topology's association rules.
        canonical[0]["outer_diameter_m"] = None
        pieces = first.outputs["pipe_pieces"].to_pylist()
        for piece in pieces:
            piece["outer_diameter_m"] = None
        first.outputs["pipe_pieces"] = pa.Table.from_pylist(
            pieces, schema=first.outputs["pipe_pieces"].schema,
        )
    return (
        _connectivity_context(
            first,
            canonical=canonical,
            endpoints=endpoints,
        ),
        riser,
        bend,
    )


@pytest.mark.parametrize("side,station", [("start", 0.0), ("end", 10.0)])
def test_approved_riser_alignment_keeps_terminal_identity_and_effective_origin(side, station) -> None:
    pipe = _edge("edge:host", "host", (0.0, 0.0, 0.0), (10.0, 0.0, 0.0))
    endpoints = _endpoints(pipe, "q:start", "q:end")
    original = (station, 0.009, 0.0)
    candidate = _point_candidate("point:alignment", "riser", pipe,
        target_kind="quantized_endpoint", target_key="q:" + side, distance=0.009)
    candidate.update(point_x_m=station, point_y_m=0.009, projection_x_m=station,
                     host_parameter=station / 10.0)
    canonical = [_canonical("host", od=0.1, pressure="MEDIUM"),
                 _canonical("riser", od=0.1, pressure="MEDIUM", role="riser")]
    first = _topology_context(canonical=canonical,
        geometry=_geometry_path("host", [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)], entity_type="LINE")
                 + [_point_geometry("riser", original)],
        edges=[pipe], endpoints=endpoints, point_to_path=pa.Table.from_pylist([candidate]))
    first.tables["riser_endpoint_alignments"] = pa.table({
        "riser_source_entity_key": ["riser"], "pipe_source_entity_key": ["host"],
    })
    _module("topology_derivation").transform(first)
    events = first.outputs["product_events"].to_pylist()
    riser = next(row for row in events if row["event_kind"] == "RISER")
    bend = next(row for row in events if row["event_kind"] == "RISER_BEND")
    assert riser["status"] == "accepted"
    assert riser["target_key"] == bend["target_key"] == "q:" + side
    assert riser["host_parameter"] == station / 10.0
    note = json.loads(riser["source_note"])
    assert note["original_cad_point_m"] == list(original)
    assert note["effective_point_m"] == [station, 0.0, 0.0]
    assert note["translation_m"] == [0.0, -0.009, 0.0]
    assert first.outputs["pipe_pieces"].num_rows == 1
    assert all(row["host_parameter"] == station / 10.0
               for row in first.outputs["host_cuts"].to_pylist())
    second = _connectivity_context(first, canonical=canonical, endpoints=endpoints)
    _module("product_connectivity").transform(second)
    origins = second.outputs["product_origins"].to_pylist()
    origin = next(row for row in origins if row["product_key"] == "product:point:riser")
    assert origin["origin_kind"] == "approved_source_point_alignment"
    assert [origin["origin_" + axis + "_m"] for axis in "xyz"] == [station, 0.0, 0.0]
    assert origin["source_geometry_element_key"] == _geometry_key("riser", 0)


def test_unique_terminal_riser_uses_explicit_riser_bend() -> None:
    connectivity = _module("product_connectivity")
    second, riser, bend = _accepted_terminal_riser_connectivity_context()
    connectivity.transform(second)
    products = second.outputs["product_instances"].to_pylist()
    assert {row["product_kind"] for row in products} == {
        "PIPE",
        "RISER",
        "RISER_BEND",
    }
    riser_product = next(row for row in products if row["product_kind"] == "RISER")
    bend_key = "product:" + bend["product_event_key"]
    assert next(row for row in products if row["product_key"] == bend_key)[
        "product_kind"
    ] == "RISER_BEND"

    origins = second.outputs["product_origins"].to_pylist()
    riser_origin = next(
        row for row in origins if row["product_key"] == riser_product["product_key"]
    )
    assert riser_origin["origin_kind"] == "source_cad_point"
    assert riser_origin["source_geometry_element_key"] == _geometry_key("riser:a", 0)
    assert (
        riser_origin["origin_x_m"],
        riser_origin["origin_y_m"],
        riser_origin["origin_z_m"],
    ) == (0.0, 0.0, 0.0)

    ports = second.outputs["world_ports"].to_pylist()
    riser_ports = sorted(
        (
            row
            for row in ports
            if row["owner_product_key"] == riser_product["product_key"]
        ),
        key=lambda row: row["sequence_ordinal"],
    )
    assert [row["port_role"] for row in riser_ports] == ["BOTTOM", "TOP"]
    assert [(row["x_m"], row["y_m"], row["z_m"]) for row in riser_ports] == [
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 1.5),
    ]
    assert all(
        (
            row["source_path_key"],
            row["pipe_piece_key"],
            row["source_segment_ordinal"],
            row["source_parameter"],
            row["segment_key"],
        )
        == (None, None, None, None, None)
        for row in riser_ports
    )

    bend_ports = [row for row in ports if row["owner_product_key"] == bend_key]
    assert len(bend_ports) == 2
    assert {row["source_side"] for row in bend_ports} == {"start", "bottom"}
    pipe_bend_port = next(row for row in bend_ports if row["source_side"] == "start")
    riser_bend_port = next(row for row in bend_ports if row["source_side"] == "bottom")
    assert pipe_bend_port["source_path_key"] == "path:pipe:a"
    assert (
        riser_bend_port["source_path_key"],
        riser_bend_port["pipe_piece_key"],
        riser_bend_port["source_segment_ordinal"],
        riser_bend_port["source_parameter"],
        riser_bend_port["segment_key"],
    ) == (None, None, None, None, None)

    owner_by_port = {row["port_key"]: row["owner_product_key"] for row in ports}
    connections = [
        row
        for row in second.outputs["connection_pairs"].to_pylist()
        if bend_key in {
            owner_by_port[row["port_a_key"]],
            owner_by_port[row["port_b_key"]],
        }
    ]
    assert len(connections) == 2
    assert {
        next(
            owner_by_port[port_key]
            for port_key in (row["port_a_key"], row["port_b_key"])
            if owner_by_port[port_key] != bend_key
        )
        for row in connections
    } == {
        next(row["product_key"] for row in products if row["product_kind"] == "PIPE"),
        riser_product["product_key"],
    }
    segment, = second.outputs["connection_segments"].to_pylist()
    assert (segment["a_x_m"], segment["b_x_m"]) == (0.0, 10.0)
    assert not any(
        "trim" in field
        for table in (
            second.outputs["product_instances"],
            second.outputs["world_ports"],
            second.outputs["connection_segments"],
        )
        for field in table.column_names
    )


def test_writer_pressure_difference_riser_access_uses_source_event_authority() -> None:
    connectivity, _, _ = _accepted_terminal_riser_connectivity_context()
    canonical = connectivity.tables["canonical"].to_pylist()
    next(row for row in canonical if row["source_role"] == "riser")["pressure_normalized"] = "LOW"
    connectivity.tables["canonical"] = pa.Table.from_pylist(canonical)
    # Model the accepted pressure-difference association seen at source 6249.
    events = connectivity.tables["events"].to_pylist()
    next(row for row in events if row["event_kind"] == "RISER").update(
        pressure_normalized="LOW", pressure_group="LOW"
    )
    connectivity.tables["events"] = pa.Table.from_pylist(events, schema=connectivity.tables["events"].schema)
    _module("product_connectivity").transform(connectivity)
    tables, context = _writer_tables(canonical=canonical, connectivity=connectivity)
    assert validate_writer_closure(tables, **context)["passed"]
    access = next(row for row in context["world_ports"]
                  if row["access_semantics"] == "source_gas_access" and row["source_path_key"] is None)
    assert access["pressure_group"] == "LOW"
    pair = next(row for row in context["connection_pairs"]
                if access["port_key"] in (row["port_a_key"], row["port_b_key"]))
    pair_keys = {pair["port_a_key"], pair["port_b_key"]}
    # A matching but wrong pair cannot override the independently known RISER fact.
    for row in context["world_ports"] + tables["ports"]:
        if row["port_key"] in pair_keys:
            row["pressure_group"] = "MEDIUM"
    report = validate_writer_closure(tables, **context)
    assert "PORT_PRESSURE_GROUP_MISMATCH" in {row["code"] for row in report["errors"]}


def test_approved_riser_association_retains_unapproved_regulator_port_competition() -> None:
    connectivity, riser, bend = _accepted_terminal_riser_connectivity_context(approved_competing_regulator=True)
    assert riser["status"] == "accepted"
    _module("product_connectivity").transform(connectivity)
    ports = connectivity.outputs["world_ports"].to_pylist()
    owners = {row["port_key"]: row["owner_product_key"] for row in ports}
    pairs = connectivity.outputs["connection_pairs"].to_pylist()
    assert len(pairs) == 1
    assert {owners[pairs[0][key]] for key in ("port_a_key", "port_b_key")} == {
        "product:"+riser["product_event_key"], "product:"+bend["product_event_key"]
    }
    assert any(row["issue_code"] == "UNRESOLVED_EVENT_PORT_CONFLICT"
               for row in connectivity.outputs["connectivity_issues"].to_pylist())
    regulator_ports = [row for row in ports if row["owner_product_key"] == "product:point:regulator:competitor"]
    assert len(regulator_ports) == 1
    assert regulator_ports[0]["port_role"] == "HIGH_PRESSURE"
    assert regulator_ports[0]["connection_state"] == "unresolved"
    tables, context = _writer_tables(canonical=connectivity.tables["canonical"].to_pylist(), connectivity=connectivity)
    assert validate_writer_closure(tables, **context)["passed"]


@pytest.mark.parametrize("missing_pipe_od", [False, True])
def test_approved_serial_regulator_preserves_parent_and_defers_retained_windows(missing_pipe_od: bool) -> None:
    connectivity, riser, bend = _accepted_terminal_riser_connectivity_context(
        approved_competing_regulator=True, serial_relation=True, missing_pipe_od=missing_pipe_od,
    )
    _module("product_connectivity").transform(connectivity)
    products = connectivity.outputs["product_instances"].to_pylist()
    parent, = [row for row in products if row["product_kind"] == "PIPE"]
    sections = {row["section_role"]: row for row in parent["planned_pipe_sections"]}
    assert set(sections) == {"UPSTREAM", "DOWNSTREAM"}
    assert sections["UPSTREAM"]["pressure_group"] == "MEDIUM"
    assert sections["DOWNSTREAM"]["pressure_group"] == "LOW"
    assert parent["pressure_normalized"] == "MEDIUM_B"
    assert all(row["window_status"] == "pending_geometry" for row in sections.values())
    segment, = connectivity.outputs["connection_segments"].to_pylist()
    assert {row["source_segment_key"] for row in sections.values()} == {segment["segment_key"]}
    assert (segment["a_x_m"], segment["b_x_m"]) == (0.0, 10.0)
    ports = connectivity.outputs["world_ports"].to_pylist()
    planned = [row for row in ports if row["position_status"] == "pending_geometry"]
    assert len(planned) == 6
    assert all(row[field] is None for row in planned for field in (
        "x_m", "y_m", "z_m", "source_segment_ordinal", "source_parameter", "source_side", "segment_key",
    ))
    assert all(row["outer_diameter_m"] == (None if missing_pipe_od else 0.1) for row in planned)
    assert all(row["diameter_status"] == ("missing" if missing_pipe_od else "known") for row in planned)
    pipe_ports = {row["port_role"]: row for row in planned if row["owner_product_key"] == parent["product_key"]}
    assert set(pipe_ports) == {"UPSTREAM_OUT", "DOWNSTREAM_IN", "DOWNSTREAM_OUT"}
    assert all(row["pressure_normalized"] == "MEDIUM_B" for row in pipe_ports.values())
    assert pipe_ports["UPSTREAM_OUT"]["retained_direction_x"] == 1.0
    assert pipe_ports["DOWNSTREAM_IN"]["retained_direction_x"] == -1.0
    assert pipe_ports["DOWNSTREAM_OUT"]["retained_direction_x"] == 1.0
    regulator_key = "product:point:regulator:competitor"
    regulator_ports = {row["port_role"]: row for row in planned if row["owner_product_key"] == regulator_key}
    assert set(regulator_ports) == {"HIGH_PRESSURE", "LOW_PRESSURE"}
    assert all(row["role_status"] == "known" for row in regulator_ports.values())
    bend_key = "product:" + bend["product_event_key"]
    bend_horizontal, = [row for row in planned if row["owner_product_key"] == bend_key]
    pairs = connectivity.outputs["connection_pairs"].to_pylist()
    pair_set = {frozenset((row["port_a_key"], row["port_b_key"])) for row in pairs}
    assert len(pairs) == len(pair_set) == 4
    assert len({row[key] for row in pairs for key in ("port_a_key", "port_b_key")}) == 8
    for first, second in (
        (pipe_ports["UPSTREAM_OUT"], regulator_ports["HIGH_PRESSURE"]),
        (regulator_ports["LOW_PRESSURE"], pipe_ports["DOWNSTREAM_IN"]),
        (pipe_ports["DOWNSTREAM_OUT"], bend_horizontal),
    ):
        assert frozenset((first["port_key"], second["port_key"])) in pair_set
    assert all(row["source_entity_key"] != "riser:a" for row in products if row["planned_pipe_sections"])
    source_port, = [row for row in ports if row["access_semantics"] == "source_path_reference"]
    assert source_port["connection_state"] == "reference_only"
    assert source_port["position_status"] == "source_reference"
    assert not any(source_port["port_key"] in pair for pair in pair_set)
    issue_codes = {row["issue_code"] for row in connectivity.outputs["connectivity_issues"].to_pylist()}
    assert {"PENDING_PLANNED_PIPE_SECTION_GEOMETRY", "PENDING_SERIAL_INTERFACE_CONSTRAINTS"} <= issue_codes
    assert not {"UNRESOLVED_EVENT_PORT_CONFLICT", "UNRESOLVED_EVENT_INCIDENCE"} & issue_codes
    tables, context = _writer_tables(canonical=connectivity.tables["canonical"].to_pylist(), connectivity=connectivity)
    report = validate_writer_closure(tables, **context)
    assert report["passed"], report["errors"]
    assert next(row for row in tables["geometry_inputs"] if row["product_key"] == parent["product_key"])["status"] == "pending_retained_sections"
    parent_run, = [row["piperun_key"] for row in tables["piperun_members"]
                   if row["row_kind"] == "member" and row["product_key"] == parent["product_key"]]
    assert [row["product_key"] for row in tables["piperun_members"]
            if row["row_kind"] == "member" and row["piperun_key"] == parent_run] == [parent["product_key"]]


@pytest.mark.parametrize("corruption", ["window", "pressure", "direction"])
def test_serial_validator_rejects_matching_wrong_port_projections(corruption: str) -> None:
    connectivity, _, _ = _accepted_terminal_riser_connectivity_context(
        approved_competing_regulator=True, serial_relation=True,
    )
    _module("product_connectivity").transform(connectivity)
    tables, context = _writer_tables(canonical=connectivity.tables["canonical"].to_pylist(), connectivity=connectivity)
    for row in context["world_ports"] + tables["ports"]:
        if row["pipe_section_key"] and row["pressure_group"] == "LOW":
            if corruption == "window":
                row["x_m"] = 0.25
            elif corruption == "pressure":
                row["pressure_group"] = "MEDIUM"
            else:
                row["retained_direction_x"] *= -1.0
    result = validate_writer_closure(tables, **context)
    assert not result["passed"], corruption


def test_duplicate_riser_bend_claims_preserve_the_shared_bottom_without_a_winner() -> None:
    import json

    connectivity, riser_event, bend_event = (
        _accepted_terminal_riser_connectivity_context()
    )
    duplicate_bend_key = bend_event["product_event_key"] + ":alternate"
    events = connectivity.tables["events"].to_pylist()
    events.append({**bend_event, "product_event_key": duplicate_bend_key})
    connectivity.tables["events"] = pa.Table.from_pylist(
        events,
        schema=connectivity.tables["events"].schema,
    )
    vertices = connectivity.tables["vertices"].to_pylist()
    bend_occurrences = [
        row
        for row in vertices
        if bend_event["product_event_key"] in row["event_keys"]
    ]
    assert len(bend_occurrences) == 1
    bend_occurrences[0]["event_keys"] = sorted(
        {*bend_occurrences[0]["event_keys"], duplicate_bend_key}
    )
    connectivity.tables["vertices"] = pa.Table.from_pylist(
        vertices,
        schema=connectivity.tables["vertices"].schema,
    )
    _module("product_connectivity").transform(connectivity)

    products = connectivity.outputs["product_instances"].to_pylist()
    riser = next(row for row in products if row["product_kind"] == "RISER")
    bend_event_keys = {
        bend_event["product_event_key"],
        duplicate_bend_key,
    }
    bend_products = {
        row["source_event_key"]: row
        for row in products
        if row["product_kind"] == "RISER_BEND"
    }
    assert set(bend_products) == bend_event_keys

    ports = connectivity.outputs["world_ports"].to_pylist()
    pipe = next(row for row in products if row["product_kind"] == "PIPE")
    pipe_start = next(
        row
        for row in ports
        if row["owner_product_key"] == pipe["product_key"]
        and row["port_role"] == "START"
    )
    riser_bottom = next(
        row
        for row in ports
        if row["owner_product_key"] == riser["product_key"]
        and row["port_role"] == "BOTTOM"
    )
    assert pipe_start["connection_state"] == "unresolved"
    assert riser_bottom["connection_state"] == "unresolved"
    assert set(riser_bottom["source_event_keys"]) == {
        riser_event["product_event_key"],
        *bend_event_keys,
    }
    assert set(pipe_start["source_event_keys"]) == {
        riser_event["product_event_key"],
        *bend_event_keys,
    }
    assert (
        riser_bottom["source_path_key"],
        riser_bottom["pipe_piece_key"],
        riser_bottom["source_segment_ordinal"],
        riser_bottom["source_parameter"],
        riser_bottom["segment_key"],
    ) == (None, None, None, None, None)

    contested_access_ports = {}
    for event_key, product in bend_products.items():
        access_ports = [
            row for row in ports if row["owner_product_key"] == product["product_key"]
        ]
        assert {row["source_side"] for row in access_ports} == {"start", "bottom"}
        assert all(
            row["source_event_key"] == event_key
            and row["source_event_keys"] == [event_key]
            and row["connection_state"] == "unresolved"
            for row in access_ports
        )
        bottom_access = next(
            row for row in access_ports if row["source_side"] == "bottom"
        )
        assert (
            bottom_access["source_path_key"],
            bottom_access["pipe_piece_key"],
            bottom_access["source_segment_ordinal"],
            bottom_access["source_parameter"],
            bottom_access["segment_key"],
            bottom_access["outer_diameter_m"],
        ) == (None, None, None, None, None, 0.1)
        start_access = next(
            row for row in access_ports if row["source_side"] == "start"
        )
        assert (
            start_access["source_path_key"],
            start_access["source_segment_ordinal"],
            start_access["source_parameter"],
            start_access["segment_key"],
            start_access["outer_diameter_m"],
        ) == (
            pipe_start["source_path_key"],
            pipe_start["source_segment_ordinal"],
            pipe_start["source_parameter"],
            pipe_start["segment_key"],
            pipe_start["outer_diameter_m"],
        )
        contested_access_ports.update(
            {row["port_key"]: row for row in access_ports}
        )

    assert connectivity.outputs["connection_pairs"].to_pylist() == []
    source_ports = {
        pipe_start["port_key"]: pipe_start,
        riser_bottom["port_key"]: riser_bottom,
    }
    conflicts = {
        row["subject_key"]: row
        for row in connectivity.outputs["connectivity_issues"].to_pylist()
        if row["issue_code"] == "UNRESOLVED_EVENT_PORT_CONFLICT"
    }
    assert set(conflicts) == {*source_ports, *contested_access_ports}
    owner_by_port = {row["port_key"]: row["owner_product_key"] for row in ports}
    for subject, issue in conflicts.items():
        assert issue["product_key"] == owner_by_port[subject]
        evidence = json.loads(issue["evidence_json"])
        expected_source_port = (
            subject
            if subject in source_ports
            else (
                riser_bottom["port_key"]
                if contested_access_ports[subject]["source_side"] == "bottom"
                else pipe_start["port_key"]
            )
        )
        assert evidence["source_port_key"] == expected_source_port
        assert set(evidence["event_keys"]) == bend_event_keys


def test_near_best_multiple_device_nodes_remain_ambiguous() -> None:
    module = _module("topology_derivation")
    left = _edge("edge:left", "pipe:left", (0.0, 0.0, 0.0), (4.0, 0.0, 0.0))
    right = _edge("edge:right", "pipe:right", (0.003, 0.0, 0.0), (0.003, 4.0, 0.0))
    endpoints = _endpoints(left, "q:left", "q:left-end") + _endpoints(right, "q:right", "q:right-end")
    rows = [
        _point_candidate("point:left", "valve:a", left, target_kind="quantized_endpoint", target_key="q:left", distance=0.0),
        _point_candidate("point:right", "valve:a", right, target_kind="quantized_endpoint", target_key="q:right", distance=0.003),
    ]
    ctx = _topology_context(
        canonical=[_canonical("pipe:left", od=0.1), _canonical("pipe:right", od=0.1), _canonical("valve:a", od=0.1, role="valve")],
        geometry=(
            _geometry_path("pipe:left", [(0.0, 0.0, 0.0), (4.0, 0.0, 0.0)], entity_type="LINE")
            + _geometry_path("pipe:right", [(0.003, 0.0, 0.0), (0.003, 4.0, 0.0)], entity_type="LINE")
        ),
        edges=[left, right],
        endpoints=endpoints,
        point_to_path=pa.Table.from_pylist(rows),
    )
    module.transform(ctx)
    valve = next(row for row in ctx.outputs["product_events"].to_pylist() if row["event_kind"] == "VALVE")
    assert valve["status"] == "unresolved"
    assert valve["reason_code"] == "ambiguous"
    assert not any(row["accepted"] for row in ctx.outputs["candidate_decisions"].to_pylist() if row["relation_family"] == "point_to_path")


def test_writer_plan_bundles_source_fact_code_and_sixteen_outputs() -> None:
    from CAD2BIM_backend.infrastructure.data_transform.capabilities import (
        PYTHON_ARROW_APPROVED_LIBRARIES,
        PYTHON_ARROW_SAFE_STDLIB,
    )
    from CAD2BIM_backend.infrastructure.worker_runtime.python_user_code import compile_entry

    module = _reconciliation_driver_module()
    name, inputs, outputs, path, package_name = module._writer_plan(
        "data_084",
        "data_101",
        "data_112",
    )
    assert name == "writer"
    assert inputs == (
        ("sd", "data_084", "source_dispositions"),
        ("ce", "data_084", "canonical_entities"),
        ("cx", "data_084", "canonical_xdata"),
        ("pieces", "data_101", "pipe_pieces"),
        ("vertices", "data_101", "pipe_path_vertices"),
        ("events", "data_101", "product_events"),
        ("pi", "data_112", "product_instances"),
        ("origins", "data_112", "product_origins"),
        ("segments", "data_112", "connection_segments"),
        ("wp", "data_112", "world_ports"),
        ("cn", "data_112", "connection_pairs"),
        ("issues", "data_112", "connectivity_issues"),
    )
    assert [row[0] for row in outputs] == [
        "source_dispositions",
        "products",
        "axis_vertices",
        "geometry_inputs",
        "ports",
        "connections",
        "systems",
        "system_members",
        "materials_styles",
        "property_values",
        "spatial_nodes",
        "spatial_assignments",
        "piperun_members",
        "unresolved_items",
        "product_origins",
        "connection_segments",
    ]
    schemas = _module("writer_input_closure")._output_schemas()
    assert set(schemas) == {row[0] for row in outputs}
    for output_name, _, key_fields in outputs:
        assert key_fields == [schemas[output_name].names[0]]
    source, evidence = bundled_program_source(path)
    assert evidence == {}
    for retired in (
        "owner_frames",
        "current_stage_resource_geometry_worker",
        "trim_start_m",
        "trim_end_m",
        "takeoff_m",
        "InterfaceFaceSeparation",
        "InferredOuterDiameter",
    ):
        assert retired not in source
    compile_entry(
        source,
        "transform",
        allowed_import_roots={
            *PYTHON_ARROW_APPROVED_LIBRARIES,
            *PYTHON_ARROW_SAFE_STDLIB,
        },
    )
    command = module._command(
        inputs=inputs,
        outputs=outputs,
        code=source,
        name=package_name,
        intended_use="unit source-fact writer contract",
    )
    dumped = command.model_dump(mode="json")
    assert len(dumped["inputs"]) == 12
    assert len(dumped["outputs"]) == 16


@pytest.mark.parametrize("fault", ["missing_vertex", "changed_vertex"])
def test_compare_rejects_l0_vertex_drift_even_when_writer_matches_wrong_topology(fault) -> None:
    module = _reconciliation_compare_module()
    canonical, xdata, connectivity = _writer_multisegment_fixture()
    source = canonical[0]["source_entity_key"]
    points = _writer_multisegment_points()
    edges, endpoints = _measurement_path(source, points)
    geometry = _geometry_path(source, points, entity_type="LWPOLYLINE")

    vertices = connectivity.tables["vertices"].to_pylist()
    if fault == "missing_vertex":
        vertices.pop(1)  # Collinear: endpoints, tangents, and total length are unchanged.
        for ordinal, row in enumerate(vertices, 1):
            row["sequence_ordinal"] = ordinal
    else:
        vertices[1]["x_m"] += 0.25  # Still collinear, so even the arc length agrees.
    connectivity.tables["vertices"] = pa.Table.from_pylist(vertices, schema=connectivity.tables["vertices"].schema)
    writer, context = _writer_tables(canonical=canonical, xdata=xdata, connectivity=connectivity)
    result = module._compare_tables(
        {"canonical_entities": canonical, "canonical_xdata": xdata,
         "source_dispositions": context["baseline_source_dispositions"]},
        {**{name: context[name] for name in ("pipe_pieces", "pipe_path_vertices", "product_events")},
         "host_cuts": [], "candidate_decisions": connectivity.tables["decisions"].to_pylist()},
        {name: context[name] for name in ("product_instances", "product_origins", "connection_segments",
                                        "world_ports", "connection_pairs", "connectivity_issues")},
        writer, l0={"geometry_elements": geometry},
        measurement={"atomic_edges": edges, "endpoints": endpoints,
                     "point_to_path": [], "endpoint_on_path": []}, source_unit_to_m=1.0,
    )
    assert result["source_fact_closure"]["passed"], result["source_fact_closure"]
    assert result["source_path_fidelity"]["passed"] is False
    expected = "SOURCE_VERTEX_COVERAGE_MISMATCH" if fault == "missing_vertex" else "SOURCE_VERTEX_COORDINATE_MISMATCH"
    assert expected in {row["code"] for row in result["source_path_fidelity"]["errors"]}
    assert result["passed"] is False


@pytest.mark.parametrize(("mapping", "modeled", "passed"), [
    (None, True, True),
    ("self", True, True),
    ("another-source", True, False),
    (None, False, False),
])
def test_source_path_fidelity_preserves_direct_source_disposition_contract(mapping, modeled, passed) -> None:
    source = "pipe:direct-source"
    points = [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0)]
    edges, endpoints = _measurement_path(source, points)
    ctx = _topology_context(
        canonical=[_canonical(source, od=0.1, pressure="MEDIUM")],
        geometry=_geometry_path(source, points, entity_type="LINE"), edges=edges, endpoints=endpoints,
    )
    _module("topology_derivation").transform(ctx)
    inputs = _source_fidelity_inputs(ctx)
    disposition = inputs["source_dispositions"][0]
    disposition["canonical_source_entity_key"] = source if mapping == "self" else mapping
    disposition["modeling_disposition"] = "modeled" if modeled else "non_product"
    result = validate_source_path_fidelity(**inputs)
    assert result["passed"] is passed, result
    assert {row["code"] for row in result["errors"]} == (set() if passed else {"SOURCE_PATH_DENOMINATOR_INVALID"})


@pytest.mark.parametrize(("fault", "code"), [
    ("gap", "SOURCE_PATH_COVERAGE_MISMATCH"),
    ("overlap", "SOURCE_PATH_COVERAGE_MISMATCH"),
    ("atomic_fragmentation", "SOURCE_UNSUPPORTED_PRODUCT_SPLIT"),
    ("cut_at_wrong_event_locus", "SOURCE_CUT_EVIDENCE_INVALID"),
])
def test_source_path_fidelity_rejects_interval_or_business_boundary_drift(fault, code) -> None:
    source = "pipe:source-validation"
    points = [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (4.0, 1.0, 0.0)]
    edges, endpoints = _measurement_path(source, points)
    candidate = _point_candidate("candidate:valve", "valve:source-validation", edges[0],
                                 target_kind="quantized_endpoint", target_key=f"q:{source}:00001")
    candidate.update(point_x_m=2.0, projection_x_m=2.0, host_parameter=1.0)
    ctx = _topology_context(
        canonical=[_canonical(source, od=0.1, pressure="MEDIUM", entity_type="LWPOLYLINE"),
                   _canonical("valve:source-validation", od=0.1, pressure="MEDIUM", role="valve")],
        geometry=_geometry_path(source, points, entity_type="LWPOLYLINE"),
        edges=edges, endpoints=endpoints, point_to_path=pa.Table.from_pylist([candidate]),
    )
    _module("topology_derivation").transform(ctx)
    inputs = _source_fidelity_inputs(ctx)
    assert validate_source_path_fidelity(**inputs)["passed"]
    left, right = inputs["pipe_pieces"]
    if fault == "gap":
        right["start_parameter"] = 0.25
    elif fault == "overlap":
        right.update(start_segment_ordinal=0, start_parameter=0.75)
    elif fault == "atomic_fragmentation":
        inputs["product_events"] = []
        inputs["host_cuts"] = []
        left["end_event_keys"] = right["start_event_keys"] = []
        for row in inputs["pipe_path_vertices"]:
            row["event_keys"] = []
    else:
        # Keep an accepted event and a correctly located cut, but move the event's
        # actual measurement target. A self-reported cut alone must not suffice.
        for row in inputs["product_events"]:
            row["target_key"] = f"q:{source}:00000"
    result = validate_source_path_fidelity(**inputs)
    assert result["passed"] is False
    assert code in {row["code"] for row in result["errors"]}, result


@pytest.mark.asyncio
async def test_compare_reads_exact_source_members_and_propagates_source_failure(monkeypatch, tmp_path) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    module = _reconciliation_compare_module()
    canonical, xdata, connectivity = _writer_multisegment_fixture()
    source = canonical[0]["source_entity_key"]
    points = _writer_multisegment_points()
    edges, endpoints = _measurement_path(source, points)
    geometry = _geometry_path(source, [tuple(value * 1000 for value in point) for point in points], entity_type="LWPOLYLINE")
    writer, context = _writer_tables(canonical=canonical, xdata=xdata, connectivity=connectivity)
    payloads = {
        "l0": {"geometry_elements": geometry},
        "canonical": {"canonical_entities": canonical, "canonical_xdata": xdata,
                      "source_dispositions": context["baseline_source_dispositions"]},
        "measurement": {"atomic_edges": edges, "endpoints": endpoints,
                        "point_to_path": [], "endpoint_on_path": []},
        "topology": {**{name: context[name] for name in ("pipe_pieces", "pipe_path_vertices", "product_events")},
                     "host_cuts": [], "candidate_decisions": connectivity.tables["decisions"].to_pylist()},
        "connectivity": {name: context[name] for name in ("product_instances", "product_origins", "connection_segments",
                                                        "world_ports", "connection_pairs", "connectivity_issues")},
        "writer": writer,
    }
    values = {"database_url": "unused", "storage_root": tmp_path,
              "configured_subject": "unit", "direct_session_alias": "direct_001"}
    for ordinal, role in enumerate(module.SOURCE_ROLES, 1):
        values[role+"_package_alias"] = f"data_{ordinal:03d}"
        values["expected_"+role+"_package_id"] = "package:"+role
    bindings = module._require_bindings(SimpleNamespace(**values))
    reads = {}

    async def read_tables(repository, artifacts, scope, alias, names, *, expected_package_id):
        role = next(role for role, binding in bindings.items() if binding[0] == alias)
        assert expected_package_id == bindings[role][1]
        reads[role] = names
        package = SimpleNamespace(catalog_entry=SimpleNamespace(alias=alias),
                                  package=SimpleNamespace(package_id=expected_package_id), members=[])
        return package, {name: payloads[role][name] for name in names}

    database = SimpleNamespace(session_factory=None, dispose=AsyncMock())
    scope = SimpleNamespace(identity=SimpleNamespace(tenant_id="tenant", task_id="task"), run_id="run")
    monkeypatch.setattr(module, "Settings", SimpleNamespace)
    monkeypatch.setattr(module, "create_database", lambda settings: database)
    for name in ("PostgresControlPlaneRepository", "PostgresIdentityDirectory", "ArtifactService", "LocalStorage"):
        monkeypatch.setattr(module, name, lambda *args: None)
    monkeypatch.setattr(module, "DirectScopeResolver", lambda *args: SimpleNamespace(resolve=AsyncMock(return_value=scope)))
    monkeypatch.setattr(module, "bind_tenant_execution_context", lambda identity: nullcontext())
    lineage = {"unit_decision": {"source_unit_to_m": 0.001}}
    for _ in range(3):
        lineage = {"source_ancestry": lineage}
    monkeypatch.setattr(module, "_verify_writer_package", AsyncMock(return_value=lineage))
    monkeypatch.setattr(module, "_tables", read_tables)

    valid = await module._run(SimpleNamespace(**values))
    assert valid["status"] == "passed", valid
    assert reads["l0"] == {"geometry_elements"}
    assert reads["measurement"] == {"atomic_edges", "endpoints", "point_to_path", "endpoint_on_path"}
    assert {"host_cuts", "candidate_decisions"} <= reads["topology"]
    # Independent L0 is immutable; topology and writer share the same wrong coordinate.
    payloads["topology"]["pipe_path_vertices"][1]["x_m"] += 0.25
    writer["axis_vertices"][1]["x_m"] += 0.25
    invalid = await module._run(SimpleNamespace(**values))
    assert invalid["source_fact_closure"]["passed"]
    assert invalid["source_path_fidelity"]["passed"] is False
    assert invalid["status"] == "failed"
    assert database.dispose.await_count == 2


def test_compare_requires_six_explicit_distinct_package_bindings() -> None:
    from types import SimpleNamespace

    module = _reconciliation_compare_module()
    values = {}
    for ordinal, role in enumerate(module.SOURCE_ROLES, start=1):
        values[role + "_package_alias"] = f"data_{ordinal:03d}"
        values["expected_" + role + "_package_id"] = f"package:{role}"
    bindings = module._require_bindings(SimpleNamespace(**values))
    assert bindings == {
        role: (
            values[role + "_package_alias"],
            values["expected_" + role + "_package_id"],
        )
        for role in module.SOURCE_ROLES
    }
    assert len(bindings) == 6

    missing = dict(values)
    missing["expected_writer_package_id"] = None
    with pytest.raises(SystemExit, match="writer requires an explicit alias"):
        module._require_bindings(SimpleNamespace(**missing))

    duplicate = dict(values)
    duplicate["writer_package_alias"] = duplicate["connectivity_package_alias"]
    with pytest.raises(SystemExit, match="distinct explicit Package aliases"):
        module._require_bindings(SimpleNamespace(**duplicate))


def _approved_node_fixture(directions, pressures, *, ods=None, device=None):
    canonical=[]; geometry=[]; edges=[]; endpoints=[]; candidates=[]
    for index,(direction,pressure) in enumerate(zip(directions,pressures)):
        source="source:"+str(index)
        points=[(0.0,0.0,0.0),tuple(3.0*v for v in direction)]
        canonical.append(_canonical(source,od=(ods[index] if ods else 0.1),pressure=pressure))
        geometry.extend(_geometry_path(source,points,entity_type="LINE"))
        path_edges,path_endpoints=_measurement_path(source,points)
        path_endpoints[0]["quantized_endpoint_key"]="q:approved-node"
        edges.extend(path_edges); endpoints.extend(path_endpoints)
        if device:
            candidates.append(_point_candidate("candidate:"+source,device,path_edges[0],
                target_kind="quantized_endpoint",target_key="q:approved-node"))
    if device:
        canonical.append(_canonical(device,od=0.1,pressure="MEDIUM_A",role="regulator"))
        geometry.append(_point_geometry(device,(0.0,0.0,0.0)))
    topology=_topology_context(canonical=canonical,geometry=geometry,edges=edges,endpoints=endpoints,
        point_to_path=pa.Table.from_pylist(candidates) if candidates else None)
    _module("topology_derivation").transform(topology)
    connectivity=_connectivity_context(topology,canonical=canonical,endpoints=endpoints)
    _module("product_connectivity").transform(connectivity)
    return topology,connectivity,canonical


@pytest.mark.parametrize(("angle","expected"),[(0.0,"CONNECTOR"),(0.5,"CONNECTOR"),(2.0,"CONNECTOR"),(2.001,"BEND")])
def test_approved_small_turn_preserves_real_source_directions(angle,expected):
    radians=math.radians(angle)
    topology,connectivity,_=_approved_node_fixture([(-1.0,0.0,0.0),(math.cos(radians),math.sin(radians),0.0)],["MEDIUM_A","MEDIUM_A"])
    event=next(row for row in topology.outputs["product_events"].to_pylist() if row["product_event_key"]=="node:q:approved-node")
    assert event["event_kind"]==expected
    ports=[row for row in connectivity.outputs["world_ports"].to_pylist() if row["owner_product_key"]=="product:node:q:approved-node"]
    assert len(ports)==2
    second=next(row for row in ports if row["source_path_key"]=="path:source:1")
    assert second["retained_direction_x"]==pytest.approx(math.cos(radians))
    assert second["retained_direction_y"]==pytest.approx(math.sin(radians))


@pytest.mark.parametrize("ods",[(0.1,0.1),(0.1,0.2)])
def test_approved_medium_layers_use_connector_without_losing_labels_or_od(ods):
    topology,connectivity,canonical=_approved_node_fixture([(-1.0,0.0,0.0),(1.0,0.0,0.0)],["MEDIUM_A","MEDIUM_B"],ods=ods)
    event=next(row for row in topology.outputs["product_events"].to_pylist() if row["product_event_key"]=="node:q:approved-node")
    assert event["event_kind"]=="CONNECTOR"
    ports=[row for row in connectivity.outputs["world_ports"].to_pylist() if row["owner_product_key"]=="product:node:q:approved-node"]
    assert {(row["source_path_key"],row["pressure_normalized"],row["outer_diameter_m"],row["pressure_group"]) for row in ports}=={
        ("path:source:0","MEDIUM_A",ods[0],"MEDIUM"),("path:source:1","MEDIUM_B",ods[1],"MEDIUM")}
    tables,context=_writer_tables(canonical=canonical,connectivity=connectivity)
    assert validate_writer_closure(tables,**context)["passed"]
    assert {row["pressure_normalized"] for row in tables["systems"] if row["parent_system_key"] is not None}=={"MEDIUM"}
    if ods[0]==ods[1]:
        assert len([row for row in tables["piperun_members"] if row["row_kind"]=="run_definition"])==1


@pytest.mark.parametrize("ambiguous",[False,True])
def test_approved_three_source_tee_preserves_bent_main_and_unique_choice(ambiguous):
    directions=([(-1.0,0.0,0.0),(math.cos(0.2),math.sin(0.2),0.0),(0.0,1.0,0.0)] if not ambiguous else
                [(math.cos(a),math.sin(a),0.0) for a in (0.0,2*math.pi/3,4*math.pi/3)])
    topology,base,canonical=_approved_node_fixture(directions,["MEDIUM_A"]*3)
    shuffled=_reversed_context(base); _module("product_connectivity").transform(shuffled)
    assert base.outputs["world_ports"].to_pylist()==shuffled.outputs["world_ports"].to_pylist()
    ports=[row for row in base.outputs["world_ports"].to_pylist() if row["owner_product_key"]=="product:node:q:approved-node"]
    if ambiguous:
        assert {row["role_status"] for row in ports}=={"neutral"}
        assert any(row["issue_code"]=="UNRESOLVED_BRANCH_ROLE" for row in base.outputs["connectivity_issues"].to_pylist())
    else:
        assert {row["source_path_key"] for row in ports if row["port_role"].startswith("MAIN")}=={"path:source:0","path:source:1"}
        assert next(row for row in ports if row["source_path_key"]=="path:source:1")["retained_direction_y"]==pytest.approx(math.sin(0.2))


@pytest.mark.parametrize(("pressures","roles","status"),[
    (["LOW","MEDIUM_B"],["LOW_PRESSURE","HIGH_PRESSURE"],"known"),
    (["MEDIUM_A","MEDIUM_A"],["HIGH_PRESSURE","LOW_PRESSURE"],"defaulted"),
    (["MEDIUM_B"],["HIGH_PRESSURE"],"defaulted"),
])
def test_approved_regulator_role_authority_and_source_note_preserve_missing_side(pressures,roles,status):
    _,connectivity,canonical=_approved_node_fixture([(-1.0,0.0,0.0),(1.0,0.0,0.0)][:len(pressures)],pressures,
        ods=[0.1,0.2][:len(pressures)],device="regulator:approved")
    shuffled=_reversed_context(connectivity); _module("product_connectivity").transform(shuffled)
    assert connectivity.outputs["world_ports"].to_pylist()==shuffled.outputs["world_ports"].to_pylist()
    product_key="product:point:regulator:approved"
    ports=sorted([row for row in connectivity.outputs["world_ports"].to_pylist() if row["owner_product_key"]==product_key],key=lambda row:row["source_path_key"])
    assert [row["port_role"] for row in ports]==roles
    assert {row["role_status"] for row in ports}=={status}
    assert [row["outer_diameter_m"] for row in ports]==[0.1,0.2][:len(pressures)]
    tables,context=_writer_tables(canonical=canonical,connectivity=connectivity)
    assert validate_writer_closure(tables,**context)["passed"]
    notes=[row for row in tables["property_values"] if row["property_name"]=="SourceNote"]
    assert len(notes)==int(status=="defaulted")
    if notes:
        assert notes[0]["value_string"]=="高低压默认指定"
        broken={name:[dict(row) for row in rows] for name,rows in tables.items()}
        broken["property_values"]=[row for row in broken["property_values"] if row["property_name"]!="SourceNote"]
        assert not validate_writer_closure(broken,**context)["passed"]
    issues=connectivity.outputs["connectivity_issues"].to_pylist()
    assert not any(row["issue_code"]=="UNRESOLVED_PRESSURE_SIDE_ROLE" for row in issues)
    assert any(row["issue_code"]=="UNRESOLVED_EVENT_INCIDENCE" for row in issues)==(len(pressures)==1)


def test_worker_interface_adapter_matches_source_values_and_angle_boundary() -> None:
    directions = [(1.0, 0.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    diameters = [0.2, 0.2, 0.2]
    source_tee = source_plan_tee(
        directions=directions,
        outer_diameters_m=diameters,
    )
    worker_tee = worker_plan_tee(directions, diameters)
    assert [row["takeoff_m"] for row in worker_tee["ports"]] == pytest.approx(
        [row.takeoff_m for row in source_tee.ports]
    )
    assert [row["point_m"] for row in worker_tee["ports"]] == pytest.approx(
        [row.point_m for row in source_tee.ports]
    )

    angle = math.radians(15.0)
    outlet = (math.cos(angle), math.sin(angle), 0.0)
    source_elbow = source_plan_elbow(
        inlet_direction=(1.0, 0.0, 0.0),
        outlet_direction=outlet,
        inlet_diameter_m=0.2,
        outlet_diameter_m=0.1,
        should_extend=True,
    )
    worker_elbow = worker_plan_elbow(
        (1.0, 0.0, 0.0),
        outlet,
        0.2,
        0.1,
        should_extend=True,
    )
    assert [row["takeoff_m"] for row in worker_elbow["ports"]] == pytest.approx(
        [row.takeoff_m for row in source_elbow.ports]
    )
    below = math.radians(14.999)
    below_outlet = (math.cos(below), math.sin(below), 0.0)
    with pytest.raises(ValueError):
        source_plan_elbow(
            inlet_direction=(1.0, 0.0, 0.0),
            outlet_direction=below_outlet,
            inlet_diameter_m=0.2,
            outlet_diameter_m=0.1,
        )
    with pytest.raises(ValueError):
        worker_plan_elbow((1.0, 0.0, 0.0), below_outlet, 0.2, 0.1)

    regulator_arguments = _current_stage_decisions()
    template = regulator_arguments["component_templates"]["regulator"]
    kwargs = {
        "reference_interface_span_m": template["reference_interface_span_m"],
        "reference_interface_outer_diameter_m": regulator_arguments["geometry"][
            "regulator_reference_interface_od_m"
        ],
        "reference_body_height_m": template["reference_body_height_m"],
        "target_height_factor_on_max_od": template["target_height_factor_on_max_od"],
        "target_height_min_m": template["target_height_min_m"],
        "target_height_max_m": template["target_height_max_m"],
        "minimum_interface_clearance_m": template["minimum_interface_clearance_m"],
    }
    source_regulator = source_plan_regulator(
        horizontal_outward_directions=((-1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        outer_diameters_m=(0.15, 0.1),
        **kwargs,
    )
    worker_regulator = worker_plan_regulator(
        ((-1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        (0.15, 0.1),
        **kwargs,
    )
    for worker_point, source_point in zip(
        worker_regulator["external_port_points_m"],
        source_regulator.external_port_points_m,
        strict=True,
    ):
        assert worker_point == pytest.approx(source_point)
    for worker_point, source_point in zip(
        worker_regulator["vertical_port_points_m"],
        source_regulator.vertical_port_points_m,
        strict=True,
    ):
        assert worker_point == pytest.approx(source_point)
    for worker_point, source_point in zip(
        worker_regulator["elbow_inner_points_m"],
        source_regulator.elbow_inner_points_m,
        strict=True,
    ):
        assert worker_point == pytest.approx(source_point)
    assert worker_regulator["horizontal_takeoffs_m"] == pytest.approx(
        source_regulator.horizontal_takeoffs_m
    )
    for planner in (source_plan_regulator, worker_plan_regulator):
        with pytest.raises(ValueError):
            if planner is source_plan_regulator:
                planner(
                    horizontal_outward_directions=((1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
                    outer_diameters_m=(0.15, 0.1),
                    **kwargs,
                )
            else:
                planner(
                    ((1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
                    (0.15, 0.1),
                    **kwargs,
                )
