"""Project CONNECTOR source facts through the accepted finite two-port walk."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from tests.scenarios.engineering_product_families.inputs.bend import project_bend_rows
from tests.scenarios.engineering_product_families.inputs.transition import (
    provider_request as _two_port_provider_request,
    source_components as _two_port_source_components,
)


def project_connector_rows(source: Mapping[str, Sequence[dict]]) -> list[dict]:
    assert all(row["product_kind"] == "CONNECTOR" for row in source["products"])
    proxy = {**source, "products": [{**row, "product_kind": "BEND"} for row in source["products"]]}
    records = project_bend_rows(proxy)
    assert len(records) == len(source["products"])
    for record in records:
        record["input_readiness_reasons"] = [reason.replace("bend_port", "connector_port")
                                             for reason in record["input_readiness_reasons"]]
        for side in record["sides"].values():
            identity = side["source_identity"]
            for old, new in (("bend_port_key", "connector_port_key"),
                             ("bend_role_status", "connector_role_status"),
                             ("bend_role_authority", "connector_role_authority")):
                identity[new] = identity.pop(old)
        for blocker in record["family_geometry_blockers"]:
            if blocker["source"] == "bend":
                blocker["source"] = "connector"
    return records


def source_components(source: Mapping[str, Sequence[dict]]) -> dict:
    assert all(row["product_kind"] == "CONNECTOR" for row in source["products"])
    graph = _two_port_source_components(source)
    for edge in graph["edges"]:
        edge["connector_port_key"] = edge.pop("transition_port_key")
    return graph


def provider_request(record: Mapping[str, object], family_version_alias: str) -> dict:
    if not record["input_ready"] or not record["family_geometry_allowed"]:
        raise ValueError("CONNECTOR source facts do not allow a Family Geometry request")
    return _two_port_provider_request(record, family_version_alias)


def synthetic_requests(scenarios: Sequence[dict], family_version_alias: str) -> list[dict]:
    keys = ("k1_direct_s_sleeve", "k2_true_one_degree", "k3_full_sixty_degree")
    output = []
    for raw in scenarios:
        key = raw["scenario_key"]
        if key not in keys:
            continue
        targets = {item["interface_key"]: item["condition"] for item in raw["external_constraints"]}
        record = {"product_origin_m": raw["product_origin"], "input_ready": True,
                  "family_geometry_allowed": True,
                  "sides": {side: {
                      "a_m": targets[side]["start_m"], "b_m": targets[side]["end_m"],
                      "outer_diameter_m": targets[side]["outer_diameter_m"],
                  } for side in ("side_a", "side_b")}}
        request = provider_request(record, family_version_alias)
        invocation = json.loads(request["invocation_json"])
        output.append({
            "case": key, "source_kind": "approved_synthetic_not_cad_fact",
            "side_source_ids": {side: f"synthetic:{key}:{side}" for side in ("side_a", "side_b")},
            "source_facts": record, "provider_request": request,
            "input_sha256": hashlib.sha256(json.dumps(invocation, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "provider_request_sha256": hashlib.sha256(json.dumps(request, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        })
    assert [row["case"] for row in output] == list(keys)
    return output
