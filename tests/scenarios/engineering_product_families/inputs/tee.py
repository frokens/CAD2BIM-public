"""Project exact TEE and three adjacent PIPE windows without solving products."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence

from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import (
    validate_product_realization_preview_provider_input,
)
from tests.scenarios.engineering_product_families.inputs.bend import _close, _finite, _one, _point
from tests.scenarios.engineering_product_families.inputs.transition import source_components as _source_components


ROLES = (("MAIN_A", "side_a"), ("MAIN_B", "side_b"), ("BRANCH", "side_c"))
INDEPENDENT_AUTHORITY = "approved_unique_closest_to_180_degree_source_direction_pair"
KNOWN_MAIN_AUTHORITY = "same_source_path_same_station_through_direction"
KNOWN_BRANCH_AUTHORITY = "independent_branch_source_path"


def project_tee_rows(source: Mapping[str, Sequence[dict]]) -> list[dict]:
    by_product: dict[str, dict[str, list[dict]]] = {}
    for name in ("product_origins", "ports", "unresolved_items", "peer_geometry_inputs",
                 "peer_axis_vertices", "peer_unresolved_items"):
        key_name = "owner_product_key" if name == "ports" else "product_key"
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in source[name]:
            grouped[row[key_name]].append(row)
        by_product[name] = grouped
    peers = {row["product_key"]: row for row in source["peer_products"]}
    peer_ports = {row["port_key"]: row for row in source["peer_ports"]}
    # The public integer reference is a deterministic, injective projection of
    # exact peer-port identity in this writer snapshot; original keys remain below.
    source_refs = {key: number for number, key in enumerate(sorted(peer_ports), start=1)}
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
        key = product["product_key"]
        reasons: list[str] = []
        blockers: list[dict] = []
        assert product["product_kind"] == "TEE"
        disposition = _one(dispositions[product["source_disposition_key"]], "source_disposition", reasons)
        if disposition is None or disposition.get("source_entity_key") != product.get("source_entity_key"):
            reasons.append("source_disposition_identity_conflict")
        if (disposition is None or disposition.get("ingestion_disposition") != "accepted"
            or disposition.get("modeling_disposition") != "modeled"):
            reasons.append("source_not_modeled")
        origin = _one(by_product["product_origins"][key], "product_origin", reasons)
        origin_point = _point(origin, "origin_") if origin else None
        if (origin is None or origin.get("origin_key") != product.get("origin_key")
            or origin.get("source_event_key") != product.get("source_event_key")
            or origin.get("origin_status") != "known" or not origin.get("authority_key")
            or not all(_finite(value) for value in origin_point)):
            reasons.append("origin_missing_or_conflicting")
        ports = by_product["ports"][key]
        if len(ports) != 3:
            reasons.append(f"tee_port_count_{len(ports)}")
        role_authority = {row["port_role"]: row.get("role_authority") for row in ports}
        if (len(role_authority) == 3 and set(role_authority.values()) == {INDEPENDENT_AUTHORITY}):
            role_mode = "three_independent_unique_closest_pair"
            family_roles = {role: "independent" for role, _ in ROLES}
        elif (role_authority.get("MAIN_A") == role_authority.get("MAIN_B") == KNOWN_MAIN_AUTHORITY
              and role_authority.get("BRANCH") == KNOWN_BRANCH_AUTHORITY):
            role_mode = "known_main_same_source_path"
            family_roles = {"MAIN_A": "main_a", "MAIN_B": "main_b", "BRANCH": "branch"}
        else:
            role_mode = "unsupported"
            family_roles = {}
            reasons.append("role_authority_not_selected_tee_rule")

        sides: dict[str, dict] = {}
        for role, field in ROLES:
            tee_port = _one([row for row in ports if row["port_role"] == role], f"{field}_port", reasons)
            if tee_port is None:
                continue
            if (tee_port.get("source_event_key") != product.get("source_event_key")
                or tee_port.get("role_status") != "known" or not tee_port.get("role_authority")
                or not tee_port.get("authority_key")):
                reasons.append(f"{field}_role_or_identity_conflict")
            join = _one(connections[tee_port["port_key"]], f"{field}_connection", reasons)
            peer_port = None
            if join is not None:
                peer_key = join["port_b_key"] if join["port_a_key"] == tee_port["port_key"] else join["port_a_key"]
                peer_port = peer_ports.get(peer_key)
                if (join.get("connection_scope") != "source_gas_pairing"
                    or join.get("fact_status") != "source_business_pairing"
                    or join.get("authority_key") != tee_port.get("authority_key")):
                    reasons.append(f"{field}_connection_authority_conflict")
            if peer_port is None:
                reasons.append(f"{field}_peer_port_missing")
                continue
            peer = peers.get(peer_port["owner_product_key"])
            if peer is None or peer.get("product_kind") != "PIPE":
                reasons.append(f"{field}_peer_not_pipe")
                continue
            if (peer_port.get("pipe_piece_key") != tee_port.get("pipe_piece_key")
                or peer_port.get("source_path_key") != tee_port.get("source_path_key")
                or peer_port.get("source_side") != tee_port.get("source_side")
                or peer_port.get("segment_key") != tee_port.get("segment_key")):
                reasons.append(f"{field}_source_segment_identity_conflict")
            segment = sorted(by_product["peer_axis_vertices"][peer["product_key"]],
                             key=lambda row: row["sequence_ordinal"])
            if len(segment) < 2 or [row["sequence_ordinal"] for row in segment] != list(range(1, len(segment) + 1)):
                reasons.append(f"{field}_finite_axis_missing")
                continue
            if peer_port["source_side"] == "start":
                a, b, expected_retained = segment[0], segment[1], "source_forward"
            elif peer_port["source_side"] == "end":
                a, b, expected_retained = segment[-1], segment[-2], "source_backward"
            else:
                reasons.append(f"{field}_source_side_unknown")
                continue
            a_point, b_point = _point(a), _point(b)
            p_tee, p_pipe = _point(tee_port), _point(peer_port)
            if not all(_finite(value) for value in a_point + b_point + p_tee + p_pipe):
                reasons.append(f"{field}_nonfinite_finite_window")
                continue
            if not (_close(a_point, p_tee) and _close(a_point, p_pipe)):
                reasons.append(f"{field}_junction_point_conflict")
            if a.get("source_entity_key") != peer.get("source_entity_key") or b.get("source_entity_key") != peer.get("source_entity_key"):
                reasons.append(f"{field}_axis_source_identity_conflict")
            if (a.get("source_segment_ordinal") != peer_port.get("source_segment_ordinal")
                or a.get("source_path_key") != peer_port.get("source_path_key")
                or b.get("source_path_key") != peer_port.get("source_path_key")):
                reasons.append(f"{field}_axis_segment_identity_conflict")
            length = math.dist(a_point, b_point)
            if not math.isfinite(length) or length <= 0:
                reasons.append(f"{field}_finite_window_length_invalid")
                continue
            direction = [(b_point[index]-a_point[index])/length for index in range(3)]
            retained = [tee_port.get(f"retained_direction_{axis}") for axis in "xyz"]
            if (tee_port.get("retained_side") != expected_retained
                or peer_port.get("retained_side") != expected_retained
                or not all(_finite(value) for value in retained)
                or not _close(retained, direction)):
                reasons.append(f"{field}_retained_direction_conflict")
            diameter = tee_port.get("outer_diameter_m")
            peer_geometry = _one(by_product["peer_geometry_inputs"][peer["product_key"]],
                                 f"{field}_peer_geometry", reasons)
            if (not _finite(diameter) or diameter <= 0
                or tee_port.get("diameter_status") not in ("known", "inferred")
                or peer_port.get("outer_diameter_m") != diameter
                or peer_port.get("diameter_status") != tee_port.get("diameter_status")
                or not tee_port.get("diameter_authority")):
                reasons.append(f"{field}_outer_diameter_missing_or_conflict")
            if (peer_geometry is None or peer_geometry.get("input_kind") != "PIPE_SOURCE_PATH_FACTS"
                or peer_geometry.get("status") != "known"
                or peer_geometry.get("outer_diameter_m") != diameter
                or peer_geometry.get("diameter_authority") != tee_port.get("diameter_authority")):
                reasons.append(f"{field}_peer_geometry_diameter_conflict")
            sides[field] = {
                "a_m": a_point, "b_m": b_point, "outer_diameter_m": diameter,
                "role": family_roles.get(role), "source_marker": "cad_exact",
                "source_ref": source_refs[peer_port["port_key"]], "finite_window_length_m": length,
                "source_identity": {
                    "tee_port_key": tee_port["port_key"], "pipe_port_key": peer_port["port_key"],
                    "pipe_product_key": peer["product_key"], "pipe_source_entity_key": peer["source_entity_key"],
                    "source_path_key": peer_port["source_path_key"], "source_segment_key": peer_port["segment_key"],
                    "source_segment_ordinal": peer_port["source_segment_ordinal"],
                    "role_status": tee_port["role_status"], "role_authority": tee_port["role_authority"],
                    "axis_vertex_a_key": a["axis_vertex_key"], "axis_vertex_b_key": b["axis_vertex_key"],
                    "diameter_authority": tee_port["diameter_authority"],
                },
            }
            for issue in by_product["peer_unresolved_items"][peer["product_key"]]:
                if issue.get("resolution_status") == "open" and "family_geometry" in (issue.get("blocked_actions") or []):
                    blockers.append({"source": "peer_pipe", "side": field, "issue": issue})
        if len(sides) == 3 and len({side["source_identity"]["pipe_port_key"] for side in sides.values()}) != 3:
            reasons.append("tee_sides_use_duplicate_pipe_port")
        for issue in by_product["unresolved_items"][key]:
            if issue.get("resolution_status") == "open" and "family_geometry" in (issue.get("blocked_actions") or []):
                blockers.append({"source": "tee", "issue": issue})
        reasons = sorted(set(reasons))
        ready = not reasons and len(sides) == 3
        records.append({
            "product_key": key, "source_entity_key": product["source_entity_key"],
            "source_event_key": product["source_event_key"], "product_origin_m": origin_point,
            "role_mode": role_mode, "sides": sides, "input_ready": ready,
            "input_readiness_reasons": reasons, "family_geometry_allowed": ready and not blockers,
            "family_geometry_blockers": blockers, "source_disposition": disposition,
        })
    return records


def source_components(source: Mapping[str, Sequence[dict]]) -> dict:
    assert all(row["product_kind"] == "TEE" for row in source["products"])
    graph = _source_components(source)
    for edge in graph["edges"]:
        edge["tee_port_key"] = edge.pop("transition_port_key")
    assert len(graph["edges"]) == 3*len(source["products"])
    graph["rule"] = graph["rule"].replace("two-sided", "three-sided")
    return graph


def provider_request(record: Mapping[str, object], family_version_alias: str) -> dict:
    """Consume freshly projected windows: A faces the product and B points away."""
    if not record["input_ready"] or not record["family_geometry_allowed"]:
        raise ValueError("TEE source facts do not allow a Family Geometry request")
    if any("retained_end" in side for side in record["sides"].values()):
        raise ValueError("Reproject source windows with A toward the product and B outward")
    values, conditions = {}, []
    for field in ("side_a", "side_b", "side_c"):
        side = record["sides"][field]
        values[f"{field}_role"] = side["role"]
        conditions.append({"interface_key": field, "condition": {
            "kind": "finite_segment", "start_m": side["a_m"], "end_m": side["b_m"],
            "outer_diameter_m": side["outer_diameter_m"], "direction_relation": "same"}})
    invocation = {
                  "public_parameters": values, "product_origin": record["product_origin_m"],
                  "length_unit": {}, "external_constraints": conditions}
    request = {"direct_session_alias": "direct_001", "mode": "single",
               "attachment_kind": "self_geometry", "source": {"source_kind": "published", "family_version_alias": family_version_alias},
               "invocation_json": json.dumps(invocation, ensure_ascii=False, separators=(",", ":"), allow_nan=False)}
    validate_product_realization_preview_provider_input(request)
    return request


def synthetic_requests(scenarios: Sequence[dict], family_version_alias: str) -> list[dict]:
    keys = ("t1_known_straight", "t2_known_curved", "t3_independent_curved", "t4_tied_main_pair")
    output = []
    for raw in scenarios:
        key = raw["case_key"]
        if key not in keys:
            continue
        invocation = raw["invocation"]
        values = invocation["public_parameters"]
        targets = {item["interface_key"]: item["condition"] for item in invocation["external_constraints"]}
        record = {"product_origin_m": invocation["product_origin"], "input_ready": True,
                  "family_geometry_allowed": True,
                  "sides": {field: {"a_m": targets[field]["start_m"], "b_m": targets[field]["end_m"],
                                    "outer_diameter_m": targets[field]["outer_diameter_m"],
                                    "role": values[f"{field}_role"]}
                            for field in ("side_a", "side_b", "side_c")}}
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
