"""Deterministic RISER source projection and action admission; no solving."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence

from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import (
    validate_product_realization_preview_provider_input,
)


SERIAL_RISER_ENTITIES = frozenset({"entrec_00000001", "entrec_00000002"})
SOURCE_MEMBERS = ("products", "source_dispositions", "geometry_inputs", "axis_vertices",
                  "product_origins", "ports", "property_values", "unresolved_items", "connections")


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _positive(value: object) -> bool:
    return _finite(value) and value > 0


def _point(row: dict | None, fields: tuple[str, str, str]) -> list | None:
    return [row.get(field) for field in fields] if row is not None else None


def _one(rows: Sequence[dict], label: str, reasons: list[str]) -> dict | None:
    if len(rows) != 1:
        reasons.append(f"{label}_count_{len(rows)}")
        return None
    return rows[0]


def _near(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-7)


def _project_one(source: Mapping[str, Sequence[dict]]) -> dict:
    reasons: list[str] = []
    product = _one(source["products"], "product", reasons)
    if product is None:
        raise ValueError("one RISER product is required for projection")
    key = product["product_key"]
    if product["product_kind"] != "RISER":
        raise ValueError("projection source product is not RISER")
    geometry = _one(source["geometry_inputs"], "geometry_input", reasons)
    origin = _one(source["product_origins"], "product_origin", reasons)
    disposition = _one(source["source_dispositions"], "source_disposition", reasons)
    by_role: dict[str, list[dict]] = defaultdict(list)
    for vertex in source["axis_vertices"]:
        by_role[vertex["axis_role"]].append(vertex)
    base = _one(by_role["riser_base"], "riser_base", reasons)
    top = _one(by_role["riser_top"], "riser_top", reasons)
    if len(source["axis_vertices"]) != 2:
        reasons.append("unexpected_axis_vertex_count")
    by_port_role: dict[str, list[dict]] = defaultdict(list)
    for port in source["ports"]:
        by_port_role[port["port_role"]].append(port)
    bottom_port = _one(by_port_role["BOTTOM"], "bottom_port", reasons)
    top_port = _one(by_port_role["TOP"], "top_port", reasons)
    if len(source["ports"]) != 2:
        reasons.append("unexpected_port_count")

    if geometry is None or geometry.get("input_kind") != "RISER_SOURCE_AND_APPROVED_HEIGHT_FACTS" or geometry.get("product_key") != key:
        reasons.append("riser_geometry_source_identity_invalid")
    if geometry is not None and (geometry.get("origin_key") != product.get("origin_key")
                                 or geometry.get("source_path_key") is not None
                                 or geometry.get("pipe_piece_key") is not None):
        reasons.append("riser_geometry_origin_or_path_conflict")
    if origin is None or (origin.get("origin_key") != product.get("origin_key")
                          or origin.get("product_key") != key
                          or origin.get("source_entity_key") != product.get("source_entity_key")
                          or origin.get("source_event_key") != product.get("source_event_key")):
        reasons.append("origin_source_identity_conflict")
    if disposition is None or (disposition.get("source_disposition_key") != product.get("source_disposition_key")
                                or disposition.get("source_entity_key") != product.get("source_entity_key")):
        reasons.append("source_disposition_identity_conflict")
    if disposition is None or disposition.get("ingestion_disposition") != "accepted" or disposition.get("modeling_disposition") != "modeled":
        reasons.append("source_not_modeled")

    bottom = _point(base, ("x_m", "y_m", "z_m"))
    upper = _point(top, ("x_m", "y_m", "z_m"))
    origin_point = _point(origin, ("origin_x_m", "origin_y_m", "origin_z_m"))
    if base is None or (base.get("sequence_ordinal") != 1
                        or base.get("product_key") != key
                        or base.get("source_entity_key") != product.get("source_entity_key")
                        or not base.get("authority_key")):
        reasons.append("base_source_role_or_authority_invalid")
    if origin is None or origin.get("origin_status") != "known" or not origin.get("authority_key") or not all(_finite(v) for v in origin_point):
        reasons.append("origin_missing_or_invalid")
    if base is not None and origin is not None and base.get("authority_key") != origin.get("authority_key"):
        reasons.append("base_origin_authority_conflict")
    if bottom is None or not all(_finite(v) for v in bottom):
        reasons.append("source_base_missing_or_invalid")
    if top is None or (top.get("sequence_ordinal") != 2
                       or top.get("product_key") != key
                       or top.get("source_entity_key") != product.get("source_entity_key")
                       or not top.get("authority_key")):
        reasons.append("top_source_role_or_authority_invalid")
    if upper is None or not all(_finite(v) for v in upper):
        reasons.append("source_top_missing_or_invalid")
    height = product.get("height_m")
    height_authority = product.get("height_authority")
    if not _positive(height) or not height_authority:
        reasons.append("height_missing_or_invalid")
    if geometry is None or geometry.get("height_m") != height or geometry.get("height_authority") != height_authority:
        reasons.append("height_or_authority_conflict")
    if top is not None and top.get("authority_key") != height_authority:
        reasons.append("top_height_authority_conflict")
    direction = None
    axis_distance = None
    if bottom is not None and upper is not None and all(_finite(v) for v in bottom + upper):
        delta = [upper[i] - bottom[i] for i in range(3)]
        axis_distance = math.hypot(*delta)
        if not math.isfinite(axis_distance):
            reasons.append("axis_distance_technically_unrepresentable")
            axis_distance = None
        elif axis_distance <= 0:
            reasons.append("axis_direction_missing_or_invalid")
        else:
            direction = [v / axis_distance for v in delta]
            if _positive(height) and not _near(axis_distance, height):
                reasons.append("axis_height_conflict")

    diameter = geometry.get("outer_diameter_m") if geometry else None
    diameter_status = geometry.get("diameter_status") if geometry else None
    diameter_authority = geometry.get("diameter_authority") if geometry else None
    if not _positive(diameter) or diameter_status not in ("known", "inferred") or not diameter_authority:
        reasons.append("outer_diameter_missing_or_invalid")
    for label, port in (("bottom", bottom_port), ("top", top_port)):
        if port is None or port.get("owner_product_key") != key or port.get("access_semantics") != "riser_vertical_endpoint":
            reasons.append(f"{label}_port_source_identity_invalid")
        if port is None or (port.get("outer_diameter_m") != diameter
                            or port.get("diameter_status") != diameter_status
                            or port.get("diameter_authority") != diameter_authority):
            reasons.append(f"{label}_port_diameter_conflict")
    if geometry is not None and geometry.get("status") != "known":
        reasons.append("geometry_input_not_known")

    inferred_property_rows = [row for row in source["property_values"]
        if row.get("pset_name") == "Pset_CAD2BIM_Derivation"
        and row.get("property_name") in {"InferredOuterDiameter", "InferredOuterDiameterSourcePort"}]
    inferred_properties = {row["property_name"]: row for row in inferred_property_rows}
    if len(inferred_property_rows) != len(inferred_properties):
        reasons.append("duplicate_diameter_inference_property")
    resolved_inference = [row for row in source["unresolved_items"]
        if row.get("issue_code") == "RISER_OUTER_DIAMETER_INFERRED"
        and row.get("resolution_status") == "resolved_by_authorized_inference"]
    inference_evidence = None
    if diameter_status == "inferred":
        issue = _one(resolved_inference, "resolved_diameter_inference", reasons)
        if issue is not None:
            try:
                inference_evidence = json.loads(issue["evidence_json"])
            except (TypeError, ValueError):
                reasons.append("diameter_inference_evidence_invalid")
            if (issue.get("authority_key") != diameter_authority
                    or issue.get("affected_field") != "outer_diameter_m"
                    or issue.get("product_key") != key):
                reasons.append("diameter_inference_authority_conflict")
        if inference_evidence is not None and (
            inference_evidence.get("outer_diameter_m") != diameter
            or inference_evidence.get("diameter_status") != "inferred"
            or inference_evidence.get("authority") != "m4-user-decisions-20260920"
            or not inference_evidence.get("endpoint_key")
            or not inference_evidence.get("host_atomic_edge_key")):
            reasons.append("diameter_inference_evidence_conflict")
        if inferred_properties:
            pair = inferred_properties
            if set(pair) != {"InferredOuterDiameter", "InferredOuterDiameterSourcePort"} or (
                pair["InferredOuterDiameter"].get("value_number") != diameter
                or pair["InferredOuterDiameterSourcePort"].get("status") != "inferred"
                or pair["InferredOuterDiameter"].get("status") != "inferred"
                or pair["InferredOuterDiameter"].get("source_key") != pair["InferredOuterDiameterSourcePort"].get("source_key")
            ):
                reasons.append("diameter_inference_property_pair_conflict")
    elif resolved_inference or inferred_properties:
        reasons.append("unexpected_diameter_inference_evidence")

    explicit_wall = [row for row in source["property_values"]
                     if row.get("property_name") in {"wall_thickness_m", "WallThicknessM"}]
    section = {"known_wall_thickness_m": []}
    if explicit_wall:
        wall = _one(explicit_wall, "explicit_wall_thickness", reasons)
        if wall is not None:
            if wall.get("unit") in {"metre", "METRE", "m"} and _positive(wall.get("value_number")):
                section["known_wall_thickness_m"] = [wall["value_number"]]
            else:
                reasons.append("explicit_wall_thickness_unit_or_value_invalid")

    unresolved = [dict(row) for row in source["unresolved_items"]]
    for issue in unresolved:
        if issue.get("resolution_status") == "open" and issue.get("affected_field") == "outer_diameter_m":
            reasons.append("open_outer_diameter_issue")
    reasons = sorted(set(reasons))
    blockers = []
    if reasons:
        blockers.append({"kind": "input_not_ready", "input_readiness_reasons": reasons})
    for issue in unresolved:
        if issue.get("resolution_status") == "open" and "family_geometry" in (issue.get("blocked_actions") or []):
            blockers.append({"kind": "source_action_blocked", "source_member": "unresolved_items",
                "unresolved_item_key": issue.get("unresolved_item_key"),
                "issue_code": issue.get("issue_code"), "subject_key": issue.get("subject_key"),
                "affected_field": issue.get("affected_field"),
                "resolution_status": issue.get("resolution_status"),
                "blocked_actions": issue.get("blocked_actions"),
                "authority_key": issue.get("authority_key")})
    return {
        "product_key": key, "source_entity_key": product.get("source_entity_key"),
        "source_event_key": product.get("source_event_key"),
        "source_disposition": {field: disposition.get(field) for field in
            ("ingestion_disposition", "modeling_disposition", "reason_code")} if disposition else None,
        "world_bottom_m": bottom, "source_top_m": upper,
        "direction": direction, "direction_source": "riser_base_to_riser_top" if direction else None,
        "source_axis_distance_m": axis_distance, "height_m": height,
        "height_authority": height_authority, "outer_diameter_m": diameter,
        "diameter_status": diameter_status, "diameter_authority": diameter_authority,
        "diameter_inference_evidence": inference_evidence,
        "inferred_property_pair_present": bool(inferred_properties),
        "product_origin_m": origin_point, "section": section,
        "planned_pipe_sections": product.get("planned_pipe_sections"),
        "port_position_statuses": {role: port.get("position_status") if port else None
            for role, port in (("BOTTOM", bottom_port), ("TOP", top_port))},
        "unresolved_items": unresolved,
        "input_readiness_reasons": reasons, "input_ready": not reasons,
        "family_geometry_blockers": blockers, "family_geometry_allowed": not blockers,
        "source_rows": {name: list(source.get(name, [])) for name in SOURCE_MEMBERS},
    }


def project_riser_rows(tables: Mapping[str, Sequence[dict]]) -> list[dict]:
    """Keep every RISER product in the denominator, including incomplete ones."""
    products = [row for row in tables["products"] if row["product_kind"] == "RISER"]
    keys = [row["product_key"] for row in products]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate RISER product identity")
    by_member: dict[str, dict[str, list[dict]]] = {}
    for name, field in (("geometry_inputs", "product_key"), ("axis_vertices", "product_key"),
                        ("product_origins", "product_key"), ("ports", "owner_product_key"),
                        ("unresolved_items", "product_key")):
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in tables[name]:
            grouped[row[field]].append(row)
        by_member[name] = grouped
    dispositions = defaultdict(list)
    for row in tables["source_dispositions"]:
        dispositions[row["source_disposition_key"]].append(row)
    properties = defaultdict(list)
    for row in tables["property_values"]:
        properties[row["owner_key"]].append(row)
    connections = defaultdict(list)
    port_owner = {row["port_key"]: row["owner_product_key"] for row in tables["ports"]}
    for row in tables.get("connections", []):
        for field in ("port_a_key", "port_b_key"):
            owner = port_owner.get(row[field])
            if owner is not None:
                connections[owner].append(row)
    records = []
    for product in sorted(products, key=lambda item: item["product_key"]):
        key = product["product_key"]
        ports = by_member["ports"].get(key, [])
        source = {
            "products": [product],
            "source_dispositions": dispositions.get(product["source_disposition_key"], []),
            "geometry_inputs": by_member["geometry_inputs"].get(key, []),
            "axis_vertices": by_member["axis_vertices"].get(key, []),
            "product_origins": by_member["product_origins"].get(key, []),
            "ports": ports,
            "property_values": properties.get(key, []) + [row for port in ports for row in properties.get(port["port_key"], [])],
            "unresolved_items": by_member["unresolved_items"].get(key, []),
            "connections": connections.get(key, []),
        }
        records.append(_project_one(source))
    return records


def _request_document(record: Mapping[str, object], family_version_alias: str, direct_session_alias: str) -> dict:
    direction = record["direction"]
    norm = math.hypot(*direction)
    axis = [value / norm for value in direction]
    constraints = [{"interface_key": key, "condition": {
        "kind": "fixed_point", "point_m": point, "axis": outward,
        "direction_relation": "opposed",
        "outer_diameter_m": record["outer_diameter_m"],
    }} for key, point, outward in (
        ("bottom", record["world_bottom_m"], axis),
        ("top", record["source_top_m"], [-value for value in axis]),
    )]
    invocation = {
        "public_parameters": {"section": record["section"]},
        "product_origin": record["product_origin_m"], "length_unit": {},
        "external_constraints": constraints}
    return {"direct_session_alias": direct_session_alias, "mode": "single",
        "attachment_kind": "self_geometry", "source": {"source_kind": "published", "family_version_alias": family_version_alias},
        "invocation_json": json.dumps(invocation, ensure_ascii=False, separators=(",", ":"), allow_nan=False)}


def validate_provider_structure(record: Mapping[str, object], family_version_alias: str, direct_session_alias: str) -> None:
    if not record["input_ready"]:
        raise ValueError("RISER source input is not structurally ready")
    validate_product_realization_preview_provider_input(
        _request_document(record, family_version_alias, direct_session_alias))


def provider_request(record: Mapping[str, object], family_version_alias: str, direct_session_alias: str) -> dict:
    """Recompute the local action gate from retained exact rows before preparing a call."""
    canonical = _project_one(record["source_rows"])
    for field in ("world_bottom_m", "source_top_m", "direction", "height_m", "outer_diameter_m",
                  "product_origin_m", "section", "input_ready", "input_readiness_reasons",
                  "family_geometry_allowed", "family_geometry_blockers"):
        if record[field] != canonical[field]:
            raise ValueError(f"RISER {field} differs from retained source facts")
    if not canonical["input_ready"] or not canonical["family_geometry_allowed"]:
        raise ValueError("RISER source blocks family_geometry request")
    request = _request_document(canonical, family_version_alias, direct_session_alias)
    validate_product_realization_preview_provider_input(request)
    return request


def synthetic_requests(family_version_alias: str, direct_session_alias: str) -> list[dict]:
    cases = (("R1", [0, 0, 0], [0, 0, 1], 0.40, 0.02, [0, 0, 0]),
             ("R2", [20, 10, 2], [0, 0, 1], 3, 0.25, [19, 9, 1]),
             ("R3", [0, 0, 0], [0, 0, 1], 0, 0.10, [0, 0, 0]))
    output = []
    for case, bottom, direction, height, diameter, origin in cases:
        record = {"world_bottom_m": bottom, "direction": direction, "height_m": height,
                  "source_top_m": [bottom[i] + height * direction[i] / math.hypot(*direction) for i in range(3)],
                  "outer_diameter_m": diameter, "section": {"known_wall_thickness_m": []},
                  "product_origin_m": origin}
        request = _request_document(record, family_version_alias, direct_session_alias)
        validate_product_realization_preview_provider_input(request)
        output.append({"case": case, "source_kind": "synthetic_target_case_not_cad_fact",
            "provider_request": request, "normalization": "current Provider DTO parsed; no solve or result",
            "R3_zero_height_intentional": case == "R3"})
    return output


def select_audit_partitions(records: Sequence[dict]) -> dict:
    by_key = {row["product_key"]: row for row in records}
    categories = {
        "ordinary": [row for row in records if row["diameter_status"] == "known"
                     and not any(issue["resolution_status"] == "open" for issue in row["unresolved_items"])
                     and row["source_entity_key"] not in SERIAL_RISER_ENTITIES],
        "approved_diameter_inference": [row for row in records if row["diameter_status"] == "inferred"],
        "independent_host_pending": [row for row in records if any(
            issue["resolution_status"] == "open" and issue["affected_field"] == "host_pairing"
            for issue in row["unresolved_items"])],
        "approved_serial_riser": [row for row in records if row["source_entity_key"] in SERIAL_RISER_ENTITIES],
        "input_not_ready": [row for row in records if not row["input_ready"]],
        "family_geometry_blocked": [row for row in records if not row["family_geometry_allowed"]],
    }
    representatives = []
    selected_by_category = {}
    for name, matches in categories.items():
        picked = min((row["product_key"] for row in matches), default=None)
        selected_by_category[name] = picked
        if picked is not None and picked not in representatives:
            representatives.append(picked)
    # Include both approved serial identities even if one is the category witness.
    for row in categories["approved_serial_riser"]:
        if row["product_key"] not in representatives:
            representatives.append(row["product_key"])
    holdout = sorted(key for key in by_key if key not in representatives
                     and int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 20 == 0)
    remainder = sorted(set(by_key) - set(representatives) - set(holdout))
    assert len(representatives) + len(holdout) + len(remainder) == len(records)
    return {"selection_rule": "category minima plus both serial IDs; SHA-256 product key modulo 20 holdout",
        "category_counts": {name: len(matches) for name, matches in categories.items()},
        "category_witnesses": selected_by_category,
        "representative": representatives, "holdout_unexecuted": holdout, "remainder": remainder}
