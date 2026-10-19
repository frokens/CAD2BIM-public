"""Project four exact CROSS source windows without realizing the product."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence

from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import validate_product_realization_preview_provider_input
from tests.scenarios.engineering_product_families.inputs.bend import _close, _finite, _one, _point
from tests.scenarios.engineering_product_families.inputs.transition import source_components as _source_components


SIDES = (("SIDE_A", "side_a"), ("SIDE_B", "side_b"), ("SIDE_C", "side_c"), ("SIDE_D", "side_d"))
PARAMETER_SUFFIXES = ("a_m", "b_m", "outer_diameter_m")


def project_cross_rows(source: Mapping[str, Sequence[dict]]) -> list[dict]:
    grouped: dict[str, dict[str, list[dict]]] = {}
    for name in ("product_origins", "ports", "unresolved_items", "peer_geometry_inputs",
                 "peer_axis_vertices", "peer_unresolved_items"):
        owner = "owner_product_key" if name == "ports" else "product_key"
        rows: dict[str, list[dict]] = defaultdict(list)
        for row in source[name]:
            rows[row[owner]].append(row)
        grouped[name] = rows
    peers = {row["product_key"]: row for row in source["peer_products"]}
    peer_ports = {row["port_key"]: row for row in source["peer_ports"]}
    source_refs = {key: index for index, key in enumerate(sorted(peer_ports), start=1)}
    assert len(source_refs) < 2147483647
    dispositions: dict[str, list[dict]] = defaultdict(list)
    for row in source["source_dispositions"]:
        dispositions[row["source_disposition_key"]].append(row)
    connections: dict[str, list[dict]] = defaultdict(list)
    for row in source["connections"]:
        connections[row["port_a_key"]].append(row)
        connections[row["port_b_key"]].append(row)

    records = []
    for product in sorted(source["products"], key=lambda row: row["product_key"]):
        assert product["product_kind"] == "CROSS"
        key = product["product_key"]
        reasons: list[str] = []
        blockers: list[dict] = []
        disposition = _one(dispositions[product["source_disposition_key"]], "source_disposition", reasons)
        if (disposition is None or disposition.get("source_entity_key") != product.get("source_entity_key")
            or disposition.get("ingestion_disposition") != "accepted"
            or disposition.get("modeling_disposition") != "modeled"):
            reasons.append("source_not_modeled_or_identity_conflict")
        origin = _one(grouped["product_origins"][key], "product_origin", reasons)
        origin_point = _point(origin, "origin_") if origin else None
        if (origin is None or origin.get("origin_key") != product.get("origin_key")
            or origin.get("source_event_key") != product.get("source_event_key")
            or origin.get("origin_status") != "known" or not origin.get("authority_key")
            or not all(_finite(value) for value in origin_point)):
            reasons.append("origin_missing_or_conflicting")
        ports = grouped["ports"][key]
        if len(ports) != 4:
            reasons.append(f"cross_port_count_{len(ports)}")
        sides: dict[str, dict] = {}
        for role, field in SIDES:
            cross_port = _one([row for row in ports if row["port_role"] == role], f"{field}_port", reasons)
            if cross_port is None:
                continue
            if (cross_port.get("source_event_key") != product.get("source_event_key")
                or cross_port.get("role_status") != "neutral"
                or cross_port.get("role_authority") != "stable_source_identity_only"
                or not cross_port.get("authority_key")):
                reasons.append(f"{field}_role_or_identity_conflict")
            join = _one(connections[cross_port["port_key"]], f"{field}_connection", reasons)
            if join is None:
                continue
            peer_key = join["port_b_key"] if join["port_a_key"] == cross_port["port_key"] else join["port_a_key"]
            peer_port = peer_ports.get(peer_key)
            if peer_port is None:
                reasons.append(f"{field}_peer_port_missing")
                continue
            if (join.get("connection_scope") != "source_gas_pairing"
                or join.get("fact_status") != "source_business_pairing"
                or join.get("authority_key") != cross_port.get("authority_key")):
                reasons.append(f"{field}_connection_authority_conflict")
            peer = peers.get(peer_port["owner_product_key"])
            if peer is None or peer.get("product_kind") != "PIPE":
                reasons.append(f"{field}_peer_not_pipe")
                continue
            for identity in ("pipe_piece_key", "source_path_key", "source_side", "segment_key"):
                if peer_port.get(identity) != cross_port.get(identity):
                    reasons.append(f"{field}_{identity}_conflict")
            vertices = sorted(grouped["peer_axis_vertices"][peer["product_key"]],
                              key=lambda row: row["sequence_ordinal"])
            if len(vertices) < 2 or [row["sequence_ordinal"] for row in vertices] != list(range(1, len(vertices)+1)):
                reasons.append(f"{field}_finite_axis_missing")
                continue
            if peer_port["source_side"] == "start":
                a, b, retained_side = vertices[0], vertices[1], "source_forward"
            elif peer_port["source_side"] == "end":
                a, b, retained_side = vertices[-1], vertices[-2], "source_backward"
            else:
                reasons.append(f"{field}_source_side_unknown")
                continue
            a_point, b_point = _point(a), _point(b)
            if not all(_finite(value) for value in a_point + b_point):
                reasons.append(f"{field}_finite_window_nonfinite")
                continue
            if not (_close(a_point, _point(cross_port)) and _close(a_point, _point(peer_port))):
                reasons.append(f"{field}_junction_point_conflict")
            if (a.get("source_entity_key") != peer.get("source_entity_key")
                or b.get("source_entity_key") != peer.get("source_entity_key")
                or a.get("source_segment_ordinal") != peer_port.get("source_segment_ordinal")
                or a.get("source_path_key") != peer_port.get("source_path_key")
                or b.get("source_path_key") != peer_port.get("source_path_key")):
                reasons.append(f"{field}_axis_source_identity_conflict")
            length = math.dist(a_point, b_point)
            if not math.isfinite(length) or length <= 0:
                reasons.append(f"{field}_finite_window_length_invalid")
                continue
            direction = [(b_point[i]-a_point[i])/length for i in range(3)]
            retained = [cross_port.get(f"retained_direction_{axis}") for axis in "xyz"]
            if (cross_port.get("retained_side") != retained_side
                or peer_port.get("retained_side") != retained_side
                or not all(_finite(value) for value in retained)
                or not _close(retained, direction)):
                reasons.append(f"{field}_retained_direction_conflict")
            diameter = cross_port.get("outer_diameter_m")
            peer_geometry = _one(grouped["peer_geometry_inputs"][peer["product_key"]],
                                 f"{field}_peer_geometry", reasons)
            if (not _finite(diameter) or diameter <= 0
                or cross_port.get("diameter_status") not in ("known", "inferred")
                or peer_port.get("outer_diameter_m") != diameter
                or peer_port.get("diameter_status") != cross_port.get("diameter_status")
                or not cross_port.get("diameter_authority")):
                reasons.append(f"{field}_outer_diameter_missing_or_conflict")
            if (peer_geometry is None or peer_geometry.get("input_kind") != "PIPE_SOURCE_PATH_FACTS"
                or peer_geometry.get("status") != "known"
                or peer_geometry.get("outer_diameter_m") != diameter
                or peer_geometry.get("diameter_authority") != cross_port.get("diameter_authority")):
                reasons.append(f"{field}_peer_geometry_diameter_conflict")
            sides[field] = {
                "a_m": a_point, "b_m": b_point, "outer_diameter_m": diameter,
                "source_marker": "cad_exact",
                "source_ref": source_refs[peer_port["port_key"]],
                "finite_window_length_m": length,
                "source_identity": {
                    "cross_port_key": cross_port["port_key"], "pipe_port_key": peer_port["port_key"],
                    "pipe_product_key": peer["product_key"], "pipe_source_entity_key": peer["source_entity_key"],
                    "source_path_key": peer_port["source_path_key"], "source_segment_key": peer_port["segment_key"],
                    "source_segment_ordinal": peer_port["source_segment_ordinal"],
                    "role_status": cross_port["role_status"], "role_authority": cross_port["role_authority"],
                    "axis_vertex_a_key": a["axis_vertex_key"], "axis_vertex_b_key": b["axis_vertex_key"],
                    "diameter_authority": cross_port["diameter_authority"],
                },
            }
            for issue in grouped["peer_unresolved_items"][peer["product_key"]]:
                if issue.get("resolution_status") == "open" and "family_geometry" in (issue.get("blocked_actions") or []):
                    blockers.append({"source": "peer_pipe", "side": field, "issue": issue})
        if len(sides) == 4 and len({side["source_identity"]["pipe_port_key"] for side in sides.values()}) != 4:
            reasons.append("cross_sides_use_duplicate_pipe_port")
        for issue in grouped["unresolved_items"][key]:
            if issue.get("resolution_status") == "open" and "family_geometry" in (issue.get("blocked_actions") or []):
                blockers.append({"source": "cross", "issue": issue})
        reasons = sorted(set(reasons))
        ready = not reasons and len(sides) == 4
        records.append({"product_key": key, "source_entity_key": product["source_entity_key"],
                        "source_event_key": product["source_event_key"], "product_origin_m": origin_point,
                        "sides": sides, "input_ready": ready, "input_readiness_reasons": reasons,
                        "family_geometry_allowed": ready and not blockers,
                        "family_geometry_blockers": blockers, "source_disposition": disposition})
    return records


def source_components(source: Mapping[str, Sequence[dict]]) -> dict:
    assert all(row["product_kind"] == "CROSS" for row in source["products"])
    graph = _source_components(source)
    for edge in graph["edges"]:
        edge["cross_port_key"] = edge.pop("transition_port_key")
    assert len(graph["edges"]) == 4*len(source["products"])
    graph["rule"] = graph["rule"].replace("two-sided", "four-sided")
    return graph


def provider_request(record: Mapping[str, object], family_version_alias: str) -> dict:
    """Consume freshly projected windows: A faces the product and B points away."""
    if not record["input_ready"] or not record["family_geometry_allowed"]:
        raise ValueError("CROSS source facts do not allow a Family Geometry request")
    if any("retained_end" in side for side in record["sides"].values()):
        raise ValueError("Reproject source windows with A toward the product and B outward")
    conditions = [{"interface_key": field, "condition": {
        "kind": "finite_segment", "start_m": record["sides"][field]["a_m"],
        "end_m": record["sides"][field]["b_m"],
        "outer_diameter_m": record["sides"][field]["outer_diameter_m"], "direction_relation": "same"}}
        for _, field in SIDES]
    invocation = {
                  "public_parameters": {}, "product_origin": record["product_origin_m"],
                  "length_unit": {}, "external_constraints": conditions}
    request = {"direct_session_alias": "direct_001", "mode": "single",
               "attachment_kind": "self_geometry", "source": {"source_kind": "published", "family_version_alias": family_version_alias},
               "invocation_json": json.dumps(invocation, ensure_ascii=False, separators=(",", ":"), allow_nan=False)}
    validate_product_realization_preview_provider_input(request)
    return request


def synthetic_requests(scenarios: Sequence[dict], family_version_alias: str) -> list[dict]:
    keys = ("c1_four_diameter_groups", "c2_nonorthogonal_placed", "c3_coincident_ports")
    output = []
    for raw in scenarios:
        key = raw["case_key"]
        if key not in keys:
            continue
        invocation = raw["invocation"]
        targets = {item["interface_key"]: item["condition"] for item in invocation["external_constraints"]}
        record = {"product_origin_m": invocation["product_origin"], "input_ready": True,
                  "family_geometry_allowed": True,
                  "sides": {field: {"a_m": targets[field]["start_m"], "b_m": targets[field]["end_m"],
                                    "outer_diameter_m": targets[field]["outer_diameter_m"]}
                            for _, field in SIDES}}
        request = provider_request(record, family_version_alias)
        invocation = json.loads(request["invocation_json"])
        output.append({"case": key, "source_kind": "approved_synthetic_not_cad_fact",
                       "source_facts": record, "provider_request": request,
                       "input_sha256": hashlib.sha256(json.dumps(invocation, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                       "provider_request_sha256": hashlib.sha256(json.dumps(request,
                           ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
    assert [row["case"] for row in output] == list(keys)
    return output
