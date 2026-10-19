"""Deterministic PIPE input projection from exact writer rows; no geometry solving."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence

from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import (
    validate_product_realization_preview_provider_input,
)


def _finite_number(value: object) -> bool:
    return type(value) in (float, int) and math.isfinite(value)


def _one(rows: list[dict], label: str, reasons: list[str]) -> dict | None:
    if len(rows) != 1:
        reasons.append(f"{label}_count_{len(rows)}")
        return None
    return rows[0]


def add_family_geometry_admission(record: dict) -> dict:
    """Derive this PIPE's own geometry action admission from retained source facts."""
    blockers = []
    if not record["input_ready"]:
        blockers.append({
            "kind": "input_not_ready",
            "input_readiness_reasons": list(record["input_readiness_reasons"]),
        })
    for issue in record["unresolved_items"]:
        if issue["resolution_status"] == "open" and "family_geometry" in issue["blocked_actions"]:
            blockers.append({
                "kind": "source_action_blocked",
                "source_member": "unresolved_items",
                "unresolved_item_key": issue["unresolved_item_key"],
                "issue_code": issue["issue_code"],
                "affected_field": issue["affected_field"],
                "blocked_actions": list(issue["blocked_actions"]),
                "authority_key": issue["authority_key"],
            })
    record["family_geometry_allowed"] = not blockers
    record["family_geometry_blockers"] = blockers
    return record


def _request_document(record: Mapping[str, object], family_version_alias: str, direct_session_alias: str) -> dict:
    """Assemble the current Provider shape; this alone grants no action permission."""
    points = record["world_points_m"]
    constraints = []
    for key, point, left, right, sign in (
        ("inlet", points[0], points[0], points[1], 1),
        ("outlet", points[-1], points[-2], points[-1], -1),
    ):
        delta = [sign * (right[i] - left[i]) for i in range(3)]
        norm = math.hypot(*delta)
        # A coincident source edge stays intact for the solver's zero_segment
        # rejection. The fixed datum itself still needs a nonzero axis.
        axis = [value / norm for value in delta] if norm else [1.0, 0.0, 0.0]
        constraints.append({"interface_key": key, "condition": {
            "kind": "fixed_point", "point_m": point, "axis": axis,
            "direction_relation": "opposed",
            "outer_diameter_m": record["outer_diameter_m"],
        }})
    invocation = {
        "public_parameters": {
            "middle_points_m": points[1:-1],
            "section": record["section"],
        },
        "product_origin": record["product_origin_m"],
        "length_unit": {},
        "external_constraints": constraints,
    }
    return {
        "direct_session_alias": direct_session_alias,
        "mode": "single",
        "attachment_kind": "self_geometry",
        "source": {"source_kind": "published", "family_version_alias": family_version_alias},
        "invocation_json": json.dumps(invocation, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
    }


def validate_provider_structure(record: Mapping[str, object], family_version_alias: str, direct_session_alias: str) -> None:
    """Check a complete input's DTO shape without creating an admitted request."""
    if not record["input_ready"]:
        raise ValueError("PIPE input is not structurally ready")
    validate_product_realization_preview_provider_input(
        _request_document(record, family_version_alias, direct_session_alias)
    )


def provider_request(record: Mapping[str, object], family_version_alias: str, direct_session_alias: str) -> dict:
    """Build an executable Provider request only when source action admission allows it."""
    if not record["input_ready"]:
        raise ValueError("PIPE input is not ready for a Provider request")
    admission = add_family_geometry_admission(dict(record))
    if (record["family_geometry_allowed"] != admission["family_geometry_allowed"]
            or record["family_geometry_blockers"] != admission["family_geometry_blockers"]):
        raise ValueError("PIPE action admission differs from retained source facts")
    if not admission["family_geometry_allowed"]:
        raise ValueError("PIPE source blocks family_geometry action")
    request = _request_document(record, family_version_alias, direct_session_alias)
    validate_product_realization_preview_provider_input(request)
    return request


def project_pipe_rows(tables: Mapping[str, Sequence[dict]]) -> list[dict]:
    """Preserve every source vertex and explicit null while preparing one row per PIPE product."""
    products = [row for row in tables["products"] if row["product_kind"] == "PIPE"]
    grouped: dict[str, dict[str, list[dict]]] = {}
    for name, key in (
        ("axis_vertices", "product_key"),
        ("geometry_inputs", "product_key"),
        ("product_origins", "product_key"),
        ("unresolved_items", "product_key"),
        ("connection_segments", "pipe_piece_key"),
        ("ports", "owner_product_key"),
    ):
        index: dict[str, list[dict]] = defaultdict(list)
        for row in tables[name]:
            if row.get(key) is not None:
                index[row[key]].append(row)
        grouped[name] = index
    dispositions = {row["source_disposition_key"]: row for row in tables["source_dispositions"]}
    result = []
    seen_product_keys: set[str] = set()
    for product in sorted(products, key=lambda row: row["product_key"]):
        key = product["product_key"]
        if key in seen_product_keys:
            raise ValueError(f"duplicate PIPE product identity: {key}")
        seen_product_keys.add(key)
        reasons: list[str] = []
        geometry = _one(grouped["geometry_inputs"].get(key, []), "geometry_input", reasons)
        origin = _one(grouped["product_origins"].get(key, []), "product_origin", reasons)
        vertices = sorted(grouped["axis_vertices"].get(key, []), key=lambda row: (row["sequence_ordinal"], row["axis_vertex_key"]))
        ordinals = [row["sequence_ordinal"] for row in vertices]
        if len(vertices) < 2:
            reasons.append("source_path_fewer_than_two_points")
        if ordinals != list(range(1, len(vertices) + 1)):
            reasons.append("source_path_sequence_invalid")
        if any(not all(_finite_number(row[axis]) for axis in ("x_m", "y_m", "z_m")) for row in vertices):
            reasons.append("source_path_coordinate_missing_or_invalid")
        if len(vertices) > 128:
            reasons.append("parameter_capacity_over_128_points")
        if geometry is not None and (
            geometry["source_path_key"] != product["source_path_key"]
            or geometry["pipe_piece_key"] != key
            or geometry["origin_key"] != product["origin_key"]
            or geometry["input_kind"] != "PIPE_SOURCE_PATH_FACTS"
        ):
            reasons.append("geometry_source_identity_conflict")
        if any(row["product_key"] != key or row["source_path_key"] != product["source_path_key"]
               or row["source_entity_key"] != product["source_entity_key"] for row in vertices):
            reasons.append("vertex_source_identity_conflict")
        od = geometry["outer_diameter_m"] if geometry else None
        od_status = geometry["diameter_status"] if geometry else None
        if od_status != "known" or not _finite_number(od) or od <= 0:
            reasons.append("outer_diameter_missing_or_invalid")
        segments = grouped["connection_segments"].get(key, [])
        if geometry is not None and any(
            row["source_path_key"] != product["source_path_key"]
            or (row["diameter_status"] == "known" and _finite_number(row["outer_diameter_m"])
                and _finite_number(od) and row["outer_diameter_m"] != od)
            for row in segments
        ):
            reasons.append("segment_source_or_diameter_conflict")
        origin_value = [origin[axis] for axis in ("origin_x_m", "origin_y_m", "origin_z_m")] if origin else None
        if origin is None or origin["origin_key"] != product["origin_key"] or origin["source_path_key"] != product["source_path_key"]:
            reasons.append("origin_source_identity_conflict")
        if origin is None or origin["origin_status"] != "known" or not all(_finite_number(value) for value in origin_value):
            reasons.append("origin_missing_or_pending")
        disposition = dispositions.get(product["source_disposition_key"])
        if disposition is None or disposition["source_entity_key"] != product["source_entity_key"]:
            reasons.append("source_disposition_identity_conflict")
        if disposition is not None and disposition["modeling_disposition"] != "modeled":
            reasons.append("source_not_modeled")
        planned = product["planned_pipe_sections"]
        planned_ports = [row for row in grouped["ports"].get(key, []) if row["pipe_section_key"] is not None]
        segment_by_key = {row["segment_key"]: row for row in segments}
        planned_context = []
        for section in planned:
            source_segment = segment_by_key.get(section["source_segment_key"])
            section_ports = [row for row in planned_ports if row["pipe_section_key"] == section["section_key"]]
            if source_segment is None:
                reasons.append("planned_section_source_segment_missing")
            planned_context.append({
                "section": section,
                "source_segment": source_segment,
                "ports": [{
                    "port_key": row["port_key"], "port_role": row["port_role"],
                    "access_semantics": row["access_semantics"],
                    "position_status": row["position_status"],
                    "world_position_m": [row[axis] for axis in ("x_m", "y_m", "z_m")],
                    "source_segment_ordinal": row["source_segment_ordinal"],
                    "source_parameter": row["source_parameter"],
                    "segment_key": row["segment_key"],
                    "outer_diameter_m": row["outer_diameter_m"],
                    "diameter_status": row["diameter_status"],
                    "diameter_authority": row["diameter_authority"],
                } for row in section_ports],
            })
        unresolved = [{
            "unresolved_item_key": row["unresolved_item_key"],
            "issue_code": row["issue_code"],
            "subject_key": row["subject_key"],
            "affected_field": row["affected_field"],
            "resolution_status": row["resolution_status"],
            "blocked_actions": row["blocked_actions"],
            "authority_key": row["authority_key"],
        } for row in grouped["unresolved_items"].get(key, [])]
        for row in unresolved:
            if row["resolution_status"] == "open" and row["affected_field"] == "outer_diameter_m":
                if "outer_diameter_missing_or_invalid" not in reasons:
                    reasons.append("outer_diameter_missing_or_invalid")
        points = [[row["x_m"], row["y_m"], row["z_m"]] for row in vertices]
        record = {
            "product_key": key,
            "source_entity_key": product["source_entity_key"],
            "source_path_key": product["source_path_key"],
            "source_disposition_key": product["source_disposition_key"],
            "source_disposition": {
                "ingestion_disposition": disposition["ingestion_disposition"],
                "modeling_disposition": disposition["modeling_disposition"],
                "reason_code": disposition["reason_code"],
            } if disposition else None,
            "geometry_input_key": geometry["geometry_input_key"] if geometry else None,
            "geometry_input_status": geometry["status"] if geometry else None,
            "world_points_m": points,
            "source_vertices": [{
                "axis_vertex_key": row["axis_vertex_key"],
                "sequence_ordinal": row["sequence_ordinal"],
                "source_segment_ordinal": row["source_segment_ordinal"],
                "source_parameter": row["source_parameter"],
                "geometry_element_key": row["geometry_element_key"],
                "authority_key": row["authority_key"],
            } for row in vertices],
            "outer_diameter_m": od,
            "diameter_status": od_status,
            "diameter_authority": geometry["diameter_authority"] if geometry else None,
            "source_segment_keys": [row["segment_key"] for row in segments],
            "product_origin_m": origin_value,
            "origin_key": origin["origin_key"] if origin else None,
            "origin_status": origin["origin_status"] if origin else None,
            "origin_authority": origin["authority_key"] if origin else None,
            "section": {"known_wall_thickness_m": []},
            "wall_thickness_evidence": "no confirmed wall-thickness field in selected writer inputs",
            "planned_pipe_sections": planned,
            "planned_section_context": planned_context,
            "unresolved_items": unresolved,
            "input_ready": not reasons,
            "input_readiness_reasons": reasons,
            "final_retained_sections_ready": None if not planned else all(
                section["window_status"] == "resolved" for section in planned
            ),
            "final_retained_sections_blockers": [
                "pending_geometry_window" if section["window_status"] == "pending_geometry"
                else f"window_status:{section['window_status']}"
                for section in planned
            ],
            "geometry_validation_status": "not_run",
        }
        result.append(add_family_geometry_admission(record))
    return result


def select_audit_partitions(records: Sequence[dict]) -> dict[str, list[str]]:
    """Choose finite witnesses and held-out sources before any geometry result exists."""
    ready = [row for row in records if row["input_ready"] and not row["planned_pipe_sections"]]
    ordered = sorted(ready, key=lambda row: row["product_key"])
    two = next(row for row in ordered if len(row["world_points_m"]) == 2)
    bent = next(row for row in ordered if len(row["world_points_m"]) >= 3)
    longest = min(ready, key=lambda row: (-len(row["world_points_m"]), row["product_key"]))
    planned = sorted((row for row in records if row["planned_pipe_sections"]), key=lambda row: row["product_key"])
    missing = sorted((row for row in records if not row["input_ready"]), key=lambda row: row["product_key"])
    representatives = list(dict.fromkeys([two["product_key"], bent["product_key"], longest["product_key"]]
                                         + [row["product_key"] for row in planned + missing]))
    other = [row for row in ready if row["product_key"] not in representatives]
    holdout = sorted(other, key=lambda row: hashlib.sha256(
        (row["source_entity_key"] + "|" + row["product_key"]).encode("utf-8")
    ).hexdigest())[:12]
    holdout_keys = [row["product_key"] for row in holdout]
    selected = set(representatives + holdout_keys)
    remainder = [row["product_key"] for row in records if row["product_key"] not in selected]
    return {"representative": representatives, "holdout_unexecuted": holdout_keys, "remainder": remainder}
