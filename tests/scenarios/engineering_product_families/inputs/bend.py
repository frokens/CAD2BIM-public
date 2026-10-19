"""Project exact BEND source and adjacent PIPE windows into public inputs; never solve."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import (
    validate_product_realization_preview_provider_input,
)


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _point(row: dict, prefix: str = "") -> list[float | None]:
    return [row.get(f"{prefix}{axis}_m") for axis in "xyz"]


def _close(a: Sequence[float], b: Sequence[float]) -> bool:
    return all(math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-7) for x, y in zip(a, b))


def _one(rows: Sequence[dict], label: str, reasons: list[str]) -> dict | None:
    if len(rows) != 1:
        reasons.append(f"{label}_count_{len(rows)}")
        return None
    return rows[0]


def project_bend_rows(source: Mapping[str, Sequence[dict]]) -> list[dict]:
    by_product: dict[str, dict[str, list[dict]]] = {}
    for name in ("product_origins", "ports", "unresolved_items", "peer_geometry_inputs",
                 "peer_axis_vertices", "peer_unresolved_items"):
        key_name = "owner_product_key" if name == "ports" else "product_key"
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in source[name]:
            grouped[row[key_name]].append(row)
        by_product[name] = grouped
    peer_products = {row["product_key"]: row for row in source["peer_products"]}
    peer_ports = {row["port_key"]: row for row in source["peer_ports"]}
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
        if product["product_kind"] != "BEND":
            reasons.append("product_kind_not_bend")
        disposition = _one(dispositions[product["source_disposition_key"]], "source_disposition", reasons)
        if disposition is None or disposition.get("source_entity_key") != product.get("source_entity_key"):
            reasons.append("source_disposition_identity_conflict")
        if disposition is None or disposition.get("ingestion_disposition") != "accepted" or disposition.get("modeling_disposition") != "modeled":
            reasons.append("source_not_modeled")
        origin = _one(by_product["product_origins"][key], "product_origin", reasons)
        origin_point = _point(origin, "origin_") if origin else None
        if (origin is None or origin.get("origin_key") != product.get("origin_key")
            or origin.get("source_event_key") != product.get("source_event_key")
            or origin.get("origin_status") != "known" or not origin.get("authority_key")
            or not all(_finite(v) for v in origin_point)):
            reasons.append("origin_missing_or_conflicting")
        ports = by_product["ports"][key]
        if len(ports) != 2:
            reasons.append(f"bend_port_count_{len(ports)}")
        sides: dict[str, dict] = {}
        for role, field in (("SIDE_A", "side_a"), ("SIDE_B", "side_b")):
            bend_port = _one([row for row in ports if row["port_role"] == role], f"{field}_port", reasons)
            if bend_port is None:
                continue
            if (bend_port.get("source_event_key") != product.get("source_event_key")
                or bend_port.get("role_authority") != "stable_source_identity_only"
                or bend_port.get("role_status") not in ("neutral", "known")
                or not bend_port.get("authority_key")):
                reasons.append(f"{field}_role_or_identity_conflict")
            join = _one(connections[bend_port["port_key"]], f"{field}_connection", reasons)
            peer_port = None
            if join is not None:
                peer_key = join["port_b_key"] if join["port_a_key"] == bend_port["port_key"] else join["port_a_key"]
                peer_port = peer_ports.get(peer_key)
                if (join.get("connection_scope") != "source_gas_pairing"
                    or join.get("fact_status") != "source_business_pairing"
                    or join.get("authority_key") != bend_port.get("authority_key")):
                    reasons.append(f"{field}_connection_authority_conflict")
            if peer_port is None:
                reasons.append(f"{field}_peer_port_missing")
                continue
            peer = peer_products.get(peer_port["owner_product_key"])
            if peer is None or peer.get("product_kind") != "PIPE":
                reasons.append(f"{field}_peer_not_pipe")
                continue
            if (peer_port.get("pipe_piece_key") != bend_port.get("pipe_piece_key")
                or peer_port.get("source_path_key") != bend_port.get("source_path_key")
                or peer_port.get("source_side") != bend_port.get("source_side")
                or peer_port.get("segment_key") != bend_port.get("segment_key")):
                reasons.append(f"{field}_source_segment_identity_conflict")
            segment = sorted(by_product["peer_axis_vertices"][peer["product_key"]],
                             key=lambda row: row["sequence_ordinal"])
            if len(segment) < 2 or [row["sequence_ordinal"] for row in segment] != list(range(1, len(segment) + 1)):
                reasons.append(f"{field}_finite_axis_missing")
                continue
            if peer_port["source_side"] == "start":
                a, b = segment[0], segment[1]
                expected_retained = "source_forward"
            elif peer_port["source_side"] == "end":
                a, b = segment[-1], segment[-2]
                expected_retained = "source_backward"
            else:
                reasons.append(f"{field}_source_side_unknown")
                continue
            a_point, b_point = _point(a), _point(b)
            p_bend, p_pipe = _point(bend_port), _point(peer_port)
            if not all(_finite(v) for v in a_point + b_point + p_bend + p_pipe):
                reasons.append(f"{field}_nonfinite_finite_window")
                continue
            if not (_close(a_point, p_bend) and _close(a_point, p_pipe)):
                reasons.append(f"{field}_junction_point_conflict")
            if b.get("source_entity_key") != peer.get("source_entity_key") or a.get("source_entity_key") != peer.get("source_entity_key"):
                reasons.append(f"{field}_axis_source_identity_conflict")
            if (a.get("source_segment_ordinal") != peer_port.get("source_segment_ordinal")
                or a.get("source_path_key") != peer_port.get("source_path_key")
                or b.get("source_path_key") != peer_port.get("source_path_key")):
                reasons.append(f"{field}_axis_segment_identity_conflict")
            length = math.dist(a_point, b_point)
            if not math.isfinite(length) or length <= 0:
                reasons.append(f"{field}_finite_window_length_invalid")
                continue
            actual_direction = [(b_point[i] - a_point[i]) / length for i in range(3)]
            retained = [bend_port.get(f"retained_direction_{axis}") for axis in "xyz"]
            if (bend_port.get("retained_side") != expected_retained
                or peer_port.get("retained_side") != expected_retained
                or not all(_finite(v) for v in retained)
                or not _close(retained, actual_direction)):
                reasons.append(f"{field}_retained_direction_conflict")
            diameter = bend_port.get("outer_diameter_m")
            peer_geometry = _one(by_product["peer_geometry_inputs"][peer["product_key"]], f"{field}_peer_geometry", reasons)
            if (not _finite(diameter) or diameter <= 0
                or bend_port.get("diameter_status") not in ("known", "inferred")
                or peer_port.get("outer_diameter_m") != diameter
                or peer_port.get("diameter_status") != bend_port.get("diameter_status")
                or not bend_port.get("diameter_authority")):
                reasons.append(f"{field}_outer_diameter_missing_or_conflict")
            if (peer_geometry is None or peer_geometry.get("input_kind") != "PIPE_SOURCE_PATH_FACTS"
                or peer_geometry.get("status") != "known"
                or peer_geometry.get("outer_diameter_m") != diameter
                or peer_geometry.get("diameter_authority") != bend_port.get("diameter_authority")):
                reasons.append(f"{field}_peer_geometry_diameter_conflict")
            sides[field] = {"a_m": a_point, "b_m": b_point, "outer_diameter_m": diameter,
                "source_marker": "cad_exact",
                "finite_window_length_m": length, "source_identity": {
                    "bend_port_key": bend_port["port_key"], "pipe_port_key": peer_port["port_key"],
                    "pipe_product_key": peer["product_key"], "pipe_source_entity_key": peer["source_entity_key"],
                    "source_path_key": peer_port["source_path_key"],
                    "source_segment_key": peer_port["segment_key"],
                    "source_segment_ordinal": peer_port["source_segment_ordinal"],
                    "bend_role_status": bend_port["role_status"],
                    "bend_role_authority": bend_port["role_authority"],
                    "axis_vertex_a_key": a["axis_vertex_key"], "axis_vertex_b_key": b["axis_vertex_key"],
                    "diameter_authority": bend_port["diameter_authority"]}}
            for issue in by_product["peer_unresolved_items"][peer["product_key"]]:
                if issue.get("resolution_status") == "open" and "family_geometry" in (issue.get("blocked_actions") or []):
                    blockers.append({"source": "peer_pipe", "side": field, "issue": issue})
        if len(sides) == 2 and sides["side_a"]["source_identity"]["pipe_port_key"] == sides["side_b"]["source_identity"]["pipe_port_key"]:
            reasons.append("both_roles_use_same_pipe_port")
        for issue in by_product["unresolved_items"][key]:
            if issue.get("resolution_status") == "open" and "family_geometry" in (issue.get("blocked_actions") or []):
                blockers.append({"source": "bend", "issue": issue})
        reasons = sorted(set(reasons))
        input_ready = not reasons
        records.append({"product_key": key, "source_entity_key": product["source_entity_key"],
            "source_event_key": product["source_event_key"],
            "product_origin_m": origin_point, "sides": sides,
            "input_ready": input_ready, "input_readiness_reasons": reasons,
            "family_geometry_allowed": input_ready and not blockers,
            "family_geometry_blockers": blockers,
            "source_disposition": disposition})
    return records


def provider_request(record: Mapping[str, object], family_version_alias: str,
                     direct_session_alias: str = "direct_001") -> dict:
    """Consume freshly projected windows: A faces the product and B points away."""
    if not record["input_ready"] or not record["family_geometry_allowed"]:
        raise ValueError("BEND source facts do not allow a Family Geometry request")
    sides = record["sides"]
    if any("retained_end" in side for side in sides.values()):
        raise ValueError("Reproject source windows with A toward the product and B outward")
    targets = []
    for field in ("side_a", "side_b"):
        side = sides[field]
        targets.append({"interface_key": field, "condition": {
            "kind": "finite_segment", "start_m": side["a_m"], "end_m": side["b_m"],
            "outer_diameter_m": side["outer_diameter_m"], "direction_relation": "same"}})
    invocation = {
        "public_parameters": {}, "product_origin": record["product_origin_m"],
        "length_unit": {}, "external_constraints": targets}
    request = {"direct_session_alias": direct_session_alias, "mode": "single",
        "attachment_kind": "self_geometry", "source": {
            "source_kind": "published", "family_version_alias": family_version_alias},
        "invocation_json": json.dumps(invocation, ensure_ascii=False, separators=(",", ":"), allow_nan=False)}
    validate_product_realization_preview_provider_input(request)
    return request


def select_audit_partitions(records: Sequence[dict]) -> dict:
    entities = sorted({record["source_entity_key"] for record in records})
    representative = entities[:6]
    holdout = entities[6:18]
    remainder = entities[18:]
    assert len(representative) + len(holdout) + len(remainder) == len(entities)
    return {"rule": "lexicographic source_entity_key before any Family solve; all event products of an entity stay together",
        "representative_source_entities": representative,
        "holdout_source_entities": holdout,
        "remainder_source_entities": remainder,
        "source_entity_count": len(entities), "product_count": len(records)}


def synthetic_requests(family_version_alias: str) -> list[dict]:
    scenarios = json.loads((Path(__file__).resolve().parents[1] /
        "families/bend/preview_cases.json").read_text(encoding="utf-8"))["cases"]["items"]
    by_key = {row["case_key"]: row for row in scenarios}
    cases = (("B1", "b1_direct", 0), ("B2", "b2_right_angle_unequal", 90),
             ("B3", "b3_150_degrees", 150))
    output = []
    for case, key, angle_deg in cases:
        raw = by_key[key]
        targets = {item["interface_key"]: item["condition"]
                   for item in raw["invocation"]["external_constraints"]}
        record = {"product_origin_m": raw["invocation"]["product_origin"], "input_ready": True,
            "family_geometry_allowed": True,
            "sides": {field: {"a_m": targets[field]["start_m"], "b_m": targets[field]["end_m"],
                              "outer_diameter_m": targets[field]["outer_diameter_m"]}
                      for field in ("side_a", "side_b")}}
        request = provider_request(record, family_version_alias)
        output.append({"case": case, "angle_deg": angle_deg,
            "side_source_ids": [f"synthetic:{case}:side_a", f"synthetic:{case}:side_b"],
            "source_kind": "synthetic_target_case_not_cad_fact",
            "provider_request": request,
            "provider_request_sha256": hashlib.sha256(json.dumps(request,ensure_ascii=False,sort_keys=True,separators=(",", ":")).encode()).hexdigest(),
            "normalization": "current Provider DTO parsed; no Family solve or result"})
    return output
