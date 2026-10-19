from __future__ import annotations

from pathlib import Path

from tests.process_support.gas_modeling_input_closure import (
    load_closure_profile,
    load_closure_tables,
    validate_modeling_input_closure,
)


TESTS_ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = TESTS_ROOT / "fixtures/gas_modeling_input_closure/closure_profile.yaml"
FIXTURE_PATH = (
    TESTS_ROOT / "fixtures/gas_modeling_input_closure/synthetic/minimal_complete.json"
)
EXPECTED_DOMAINS = {
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
}


def test_closure_profile_is_an_explicit_test_only_minimum_preflight() -> None:
    profile = load_closure_profile(PROFILE_PATH)

    assert profile["profile_id"] == "gas_modeling_input_closure_minimum_preflight_v1"
    assert profile["scope"] == "test_only"
    assert profile["status"] == "minimum_preflight"
    assert profile["public_ifc_mcp_contract"] is False
    assert "not a public IFC MCP input contract" in profile["purpose"]
    assert set(profile["domains"]) == EXPECTED_DOMAINS
    assert profile["allow_extra_domains"] is False


def test_profile_preserves_definition_binding_and_group_semantics() -> None:
    profile = load_closure_profile(PROFILE_PATH)
    domains = profile["domains"]

    assert domains["system_members"]["unique_together"] == [
        ["system_key", "member_kind", "member_key"]
    ]
    assert domains["materials_styles"]["enum_fields"]["row_kind"] == [
        "definition",
        "binding",
        "style",
        "layer_binding",
    ]
    assert {"owner_kind", "owner_key", "pset_name", "status"}.issubset(
        domains["property_values"]["required_fields"]
    )
    assert domains["spatial_nodes"]["enum_fields"]["node_kind"] == [
        "PROJECT",
        "SITE",
        "GROUP",
    ]
    assert domains["piperun_members"]["enum_fields"]["row_kind"] == [
        "run_definition",
        "member",
    ]
    assert domains["connections"]["enum_fields"]["connection_scope"] == [
        "accepted_external_physical"
    ]


def test_profile_and_fixture_contain_no_copied_golden_answer() -> None:
    combined = PROFILE_PATH.read_text(encoding="utf-8") + FIXTURE_PATH.read_text(
        encoding="utf-8"
    )

    for forbidden in (
        "ad00c6a0e0c9b731ea6cdb4f87ecc1aab4bfd3f47abe8f597560dc3108691436",
        "988fb614a8849c3b547830be2942310b075124d5401b5f6a0a15ab40c9c0df11",
        "49990",
        "23012",
        "23323",
        "7695",
    ):
        assert forbidden not in combined


def test_synthetic_fixture_exactly_matches_and_passes_the_profile() -> None:
    profile = load_closure_profile(PROFILE_PATH)
    tables = load_closure_tables(FIXTURE_PATH)

    assert set(tables) == EXPECTED_DOMAINS
    assert validate_modeling_input_closure(tables, profile).passed
