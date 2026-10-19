"""RISER source projection risks; every fixture here is synthetic."""

from __future__ import annotations

import copy
import json

import pytest

from tests.scenarios.engineering_product_families.inputs.riser import (
    project_riser_rows, provider_request, synthetic_requests, validate_provider_structure,
)


ALIAS = "cur.synthetic-test"


def _tables() -> dict:
    key = "product:synthetic:riser"
    return {
        "products": [{"product_key": key, "product_kind": "RISER",
            "source_entity_key": "synthetic-entity", "source_event_key": "synthetic-event",
            "source_disposition_key": "synthetic-disposition", "origin_key": "synthetic-origin",
            "height_m": 3.0, "height_authority": "approved-height",
            "planned_pipe_sections": [{"window_status": "pending_geometry", "start": None}]}],
        "source_dispositions": [{"source_disposition_key": "synthetic-disposition",
            "source_entity_key": "synthetic-entity", "ingestion_disposition": "accepted",
            "modeling_disposition": "modeled", "reason_code": "synthetic"}],
        "geometry_inputs": [{"product_key": key, "geometry_input_key": "synthetic-geometry",
            "input_kind": "RISER_SOURCE_AND_APPROVED_HEIGHT_FACTS",
            "source_path_key": None, "pipe_piece_key": None,
            "origin_key": "synthetic-origin", "height_m": 3.0,
            "height_authority": "approved-height", "length_m": None,
            "outer_diameter_m": 0.25, "diameter_status": "known",
            "diameter_authority": "source-od", "status": "known"}],
        "axis_vertices": [{"axis_vertex_key": "base", "product_key": key,
            "axis_role": "riser_base", "sequence_ordinal": 1,
            "source_entity_key": "synthetic-entity", "authority_key": "source-base",
            "x_m": 20.0, "y_m": 10.0, "z_m": 2.0},
            {"axis_vertex_key": "top", "product_key": key,
            "axis_role": "riser_top", "sequence_ordinal": 2,
            "source_entity_key": "synthetic-entity", "authority_key": "approved-height",
            "x_m": 20.0, "y_m": 10.0, "z_m": 5.0}],
        "product_origins": [{"origin_key": "synthetic-origin", "product_key": key,
            "source_entity_key": "synthetic-entity", "source_event_key": "synthetic-event",
            "origin_status": "known", "authority_key": "source-base",
            "origin_x_m": 19.0, "origin_y_m": 9.0, "origin_z_m": 1.0}],
        "ports": [{"port_key": f"{role}-port", "owner_product_key": key,
            "port_role": role, "access_semantics": "riser_vertical_endpoint",
            "position_status": None, "x_m": None, "y_m": None, "z_m": None,
            "outer_diameter_m": 0.25, "diameter_status": "known",
            "diameter_authority": "source-od"} for role in ("BOTTOM", "TOP")],
        "property_values": [], "unresolved_items": [], "connections": [],
    }


def _issue(tables: dict, action: str) -> dict:
    return {"unresolved_item_key": "synthetic-issue", "product_key": tables["products"][0]["product_key"],
        "issue_code": "SYNTHETIC_HOST", "subject_key": "synthetic-host",
        "affected_field": "host_pairing", "resolution_status": "open",
        "blocked_actions": [action], "authority_key": "synthetic-authority",
        "evidence_json": None}


def test_source_base_height_and_origin_remain_independent() -> None:
    record = project_riser_rows(_tables())[0]
    request = provider_request(record, ALIAS, "direct_001")
    invocation = json.loads(request["invocation_json"])
    public = invocation["public_parameters"]
    assert public == {"section": {"known_wall_thickness_m": []}}
    bottom, top = invocation["external_constraints"]
    assert bottom["condition"] == {"kind": "fixed_point", "point_m": [20.0, 10.0, 2.0],
                                   "axis": [0.0, 0.0, 1.0], "direction_relation": "opposed", "outer_diameter_m": 0.25}
    assert top["condition"] == {"kind": "fixed_point", "point_m": [20.0, 10.0, 5.0],
                                "axis": [0.0, 0.0, -1.0], "direction_relation": "opposed", "outer_diameter_m": 0.25}
    assert invocation["product_origin"] == [19.0, 9.0, 1.0]
    assert record["planned_pipe_sections"] == [{"window_status": "pending_geometry", "start": None}]
    assert record["port_position_statuses"] == {"BOTTOM": None, "TOP": None}
    assert record["input_ready"] and record["family_geometry_allowed"]


def test_missing_and_conflicting_source_facts_do_not_gain_defaults() -> None:
    tables = _tables()
    tables["axis_vertices"][1]["z_m"] = None
    tables["geometry_inputs"][0]["outer_diameter_m"] = None
    record = project_riser_rows(tables)[0]
    assert record["source_top_m"] == [20.0, 10.0, None]
    assert record["direction"] is None
    assert record["outer_diameter_m"] is None
    assert not record["input_ready"] and not record["family_geometry_allowed"]
    assert "source_top_missing_or_invalid" in record["input_readiness_reasons"]
    with pytest.raises(ValueError, match="blocks family_geometry"):
        provider_request(record, ALIAS, "direct_001")


def test_axis_height_conflict_is_not_replaced_with_default_vertical() -> None:
    tables = _tables()
    tables["axis_vertices"][1]["x_m"] = 20.3
    record = project_riser_rows(tables)[0]
    assert record["direction"] != [0.0, 0.0, 1.0]
    assert "axis_height_conflict" in record["input_readiness_reasons"]
    assert not record["input_ready"]


def test_finite_coordinates_with_overflowing_axis_distance_are_diagnosed() -> None:
    tables = _tables()
    tables["axis_vertices"][0]["x_m"] = -1e308
    tables["axis_vertices"][1]["x_m"] = 1e308
    record = project_riser_rows(tables)[0]
    assert record["direction"] is None
    assert "axis_distance_technically_unrepresentable" in record["input_readiness_reasons"]


def test_external_host_issue_does_not_block_own_geometry() -> None:
    tables = _tables()
    tables["unresolved_items"] = [_issue(tables, "external_business_host_resolution")]
    record = project_riser_rows(tables)[0]
    assert record["input_ready"] and record["family_geometry_allowed"]
    assert record["unresolved_items"][0]["blocked_actions"] == ["external_business_host_resolution"]
    validate_provider_structure(record, ALIAS, "direct_001")
    assert provider_request(record, ALIAS, "direct_001")["mode"] == "single"


def test_family_geometry_action_block_cannot_be_bypassed_by_flag() -> None:
    tables = _tables()
    tables["unresolved_items"] = [_issue(tables, "family_geometry")]
    record = project_riser_rows(tables)[0]
    assert record["input_ready"] and not record["family_geometry_allowed"]
    forged = copy.deepcopy(record)
    forged["family_geometry_allowed"] = True
    forged["family_geometry_blockers"] = []
    with pytest.raises(ValueError, match="differs from retained source facts"):
        provider_request(forged, ALIAS, "direct_001")
    with pytest.raises(ValueError, match="blocks family_geometry"):
        provider_request(record, ALIAS, "direct_001")


def test_approved_diameter_inference_requires_matching_issue_and_ports() -> None:
    tables = _tables()
    geometry = tables["geometry_inputs"][0]
    geometry["diameter_status"] = "inferred"
    geometry["diameter_authority"] = "approved-inference"
    for port in tables["ports"]:
        port["diameter_status"] = "inferred"
        port["diameter_authority"] = "approved-inference"
    tables["unresolved_items"] = [{"unresolved_item_key": "inference", "product_key": geometry["product_key"],
        "issue_code": "RISER_OUTER_DIAMETER_INFERRED", "subject_key": "synthetic-entity",
        "affected_field": "outer_diameter_m", "resolution_status": "resolved_by_authorized_inference",
        "blocked_actions": [], "authority_key": "approved-inference",
        "evidence_json": json.dumps({"outer_diameter_m": 0.25, "diameter_status": "inferred",
            "authority": "m4-user-decisions-20260920", "endpoint_key": "synthetic-endpoint",
            "host_atomic_edge_key": "synthetic-edge"})}]
    record = project_riser_rows(tables)[0]
    assert record["input_ready"] and record["family_geometry_allowed"]
    assert record["diameter_inference_evidence"]["endpoint_key"] == "synthetic-endpoint"
    tables["ports"][1]["outer_diameter_m"] = 0.20
    conflicted = project_riser_rows(tables)[0]
    assert not conflicted["input_ready"]
    assert "top_port_diameter_conflict" in conflicted["input_readiness_reasons"]


def test_fixed_synthetic_requests_parse_including_zero_height_R3() -> None:
    requests = synthetic_requests(ALIAS, "direct_001")
    assert [item["case"] for item in requests] == ["R1", "R2", "R3"]
    assert all(item["source_kind"] == "synthetic_target_case_not_cad_fact" for item in requests)
    r3 = json.loads(requests[2]["provider_request"]["invocation_json"])
    public = r3["public_parameters"]
    bottom, top = r3["external_constraints"]
    assert bottom["condition"]["point_m"] == top["condition"]["point_m"]
    assert public["section"] == {"known_wall_thickness_m": []}
