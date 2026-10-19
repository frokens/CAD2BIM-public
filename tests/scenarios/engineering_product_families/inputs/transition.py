"""Project TRANSITION source facts using the accepted finite two-port source walk."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence

from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import (
    validate_product_realization_preview_provider_input,
)
from tests.scenarios.engineering_product_families.inputs.bend import project_bend_rows


def project_transition_rows(source: Mapping[str, Sequence[dict]]) -> list[dict]:
    """Reuse the identical source/axis/OD walk; retain TRANSITION identities in output."""
    assert all(row["product_kind"] == "TRANSITION" for row in source["products"])
    proxy = {**source, "products": [{**row, "product_kind": "BEND"} for row in source["products"]]}
    records = project_bend_rows(proxy)
    assert len(records) == len(source["products"])
    for record in records:
        record["input_readiness_reasons"] = [reason.replace("bend_port", "transition_port")
                                              for reason in record["input_readiness_reasons"]]
        for side in record["sides"].values():
            identity = side["source_identity"]
            for old, new in (("bend_port_key", "transition_port_key"),
                             ("bend_role_status", "transition_role_status"),
                             ("bend_role_authority", "transition_role_authority")):
                identity[new] = identity.pop(old)
        for blocker in record["family_geometry_blockers"]:
            if blocker["source"] == "bend":
                blocker["source"] = "transition"
    return records


def provider_request(record: Mapping[str, object], family_version_alias: str,
                     direct_session_alias: str = "direct_001") -> dict:
    """Consume freshly projected windows: A faces the product and B points away."""
    if not record["input_ready"] or not record["family_geometry_allowed"]:
        raise ValueError("TRANSITION source facts do not allow a Family Geometry request")
    if any("retained_end" in side for side in record["sides"].values()):
        raise ValueError("Reproject source windows with A toward the product and B outward")
    targets = []
    for field in ("side_a", "side_b"):
        side = record["sides"][field]
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


def source_components(source: Mapping[str, Sequence[dict]]) -> dict:
    """Freeze product--peer source-entity connected components before any Family solve."""
    products = {row["product_key"]: row for row in source["products"]}
    transition_ports = {row["port_key"]: row for row in source["ports"]}
    peer_ports = {row["port_key"]: row for row in source["peer_ports"]}
    peer_products = {row["product_key"]: row for row in source["peer_products"]}
    graph: dict[str, set[str]] = defaultdict(set)
    edges = []
    for key in products:
        graph["product:" + key]
    for connection in source["connections"]:
        a, b = connection["port_a_key"], connection["port_b_key"]
        transition_key, peer_key = (a, b) if a in transition_ports else (b, a)
        assert transition_key in transition_ports and peer_key in peer_ports
        transition_port, peer_port = transition_ports[transition_key], peer_ports[peer_key]
        assert connection["connection_scope"] == "source_gas_pairing"
        assert connection["fact_status"] == "source_business_pairing"
        product_key = transition_port["owner_product_key"]
        peer_product = peer_products[peer_port["owner_product_key"]]
        assert product_key in products and peer_product["product_kind"] == "PIPE"
        source_key = peer_product["source_entity_key"]
        p, s = "product:" + product_key, "source:" + source_key
        graph[p].add(s)
        graph[s].add(p)
        edges.append({"product_key": product_key, "transition_port_key": transition_key,
                      "peer_port_key": peer_key, "peer_pipe_product_key": peer_product["product_key"],
                      "peer_source_entity_key": source_key,
                      "connection_key": connection["connection_key"]})
    seen, components = set(), []
    for start in sorted(graph):
        if start in seen:
            continue
        waiting, members = [start], []
        seen.add(start)
        while waiting:
            current = waiting.pop()
            members.append(current)
            for adjacent in sorted(graph[current]):
                if adjacent not in seen:
                    seen.add(adjacent)
                    waiting.append(adjacent)
        product_keys = sorted(item[len("product:"):] for item in members if item.startswith("product:"))
        source_keys = sorted(item[len("source:"):] for item in members if item.startswith("source:"))
        identity = json.dumps({"products": product_keys, "source_entities": source_keys},
                              sort_keys=True, separators=(",", ":"))
        component_sha256 = hashlib.sha256(identity.encode()).hexdigest()
        # Deterministic identity buckets yield counts from actual graph components.
        bucket = int(component_sha256[:2], 16)
        partition = "representative" if bucket < 8 else "holdout" if bucket < 24 else "remainder"
        components.append({"component_sha256": component_sha256, "partition": partition,
                           "product_keys": product_keys, "source_entity_keys": source_keys})
    assert sum(len(item["product_keys"]) for item in components) == len(products)
    assert sum(len(item["source_entity_keys"]) for item in components) == len({
        edge["peer_source_entity_key"] for edge in edges})
    return {"rule": "two-sided source_gas_pairing product--peer source entity graph; SHA-256 component identity first byte 0..7 representative, 8..23 holdout, 24..255 remainder",
            "edges": edges, "components": components}


def synthetic_requests(scenarios: Sequence[dict], family_version_alias: str) -> list[dict]:
    output = []
    for raw in scenarios:
        key = raw["scenario_key"]
        if key not in ("x1_direct_six_to_one", "x2_45_degree_six_to_one", "x3_short_window"):
            continue
        invocation = {
                      "public_parameters": raw["public_parameters"], "product_origin": raw["product_origin"],
                      "length_unit": {}, "external_constraints": raw["external_constraints"]}
        request = {"direct_session_alias": "direct_001", "mode": "single",
                   "attachment_kind": "self_geometry", "source": {
                       "source_kind": "published", "family_version_alias": family_version_alias},
                   "invocation_json": json.dumps(invocation, ensure_ascii=False, separators=(",", ":"), allow_nan=False)}
        validate_product_realization_preview_provider_input(request)
        input_digest = hashlib.sha256(json.dumps(invocation, ensure_ascii=False,
                                                  sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        output.append({"case": key, "source_kind": "approved_synthetic_not_cad_fact",
                       "provider_request": request, "input_sha256": input_digest,
                       "provider_request_sha256": hashlib.sha256(json.dumps(
                           request, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode()).hexdigest()})
    assert len(output) == 3
    return output
