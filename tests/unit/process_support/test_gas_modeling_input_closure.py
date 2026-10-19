from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

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


def _profile() -> dict[str, Any]:
    return load_closure_profile(PROFILE_PATH)


def _tables() -> dict[str, list[dict[str, Any]]]:
    return load_closure_tables(FIXTURE_PATH)


def _codes(tables: dict[str, Any]) -> frozenset[str]:
    return validate_modeling_input_closure(tables, _profile()).issue_codes


def _row(tables: dict[str, Any], domain: str, key_field: str, key: str) -> dict[str, Any]:
    return next(row for row in tables[domain] if row[key_field] == key)


def test_minimal_complete_fixture_passes_preflight_without_external_data() -> None:
    report = validate_modeling_input_closure(_tables(), _profile())

    assert report.passed
    assert report.issues == ()


def test_structure_rejects_missing_domain_duplicate_key_and_broken_foreign_key() -> None:
    missing = _tables()
    del missing["ports"]
    assert "DOMAIN_MISSING" in _codes(missing)

    duplicate = _tables()
    duplicate["products"].append(deepcopy(duplicate["products"][0]))
    assert "PRIMARY_KEY_DUPLICATE" in _codes(duplicate)

    broken = _tables()
    broken["axis_vertices"][0]["product_key"] = "product:absent"
    assert "FOREIGN_KEY_UNKNOWN" in _codes(broken)


def test_source_origin_product_requires_a_source_disposition() -> None:
    missing_lineage = _tables()
    product = next(
        row for row in missing_lineage["products"] if row["origin_kind"] == "source"
    )
    product["source_disposition_key"] = None

    assert "SOURCE_PRODUCT_DISPOSITION_MISSING" in _codes(missing_lineage)


def test_geometry_rejects_nonfinite_coordinates_and_nonunit_directions() -> None:
    nonfinite = _tables()
    nonfinite["geometry_inputs"][0]["origin_x_m"] = float("nan")
    assert "NUMBER_NOT_FINITE" in _codes(nonfinite)

    nonunit = _tables()
    nonunit["ports"][0]["direction_x"] = 2.0
    assert "VECTOR_NOT_UNIT" in _codes(nonunit)


def test_assembly_and_system_parent_cycles_are_rejected() -> None:
    assembly = _tables()
    _row(assembly, "products", "product_key", "product:valve-001")[
        "assembly_parent_product_key"
    ] = "product:valve-body-001"
    assert "ASSEMBLY_CYCLE" in _codes(assembly)

    system = _tables()
    _row(system, "systems", "system_key", "system:gas")["parent_system_key"] = (
        "system:gas-low"
    )
    assert "SYSTEM_CYCLE" in _codes(system)


def test_leaf_system_rules_allow_root_membership_but_reject_two_leaf_memberships() -> None:
    root_membership = _tables()
    root_membership["system_members"].append(
        {
            "system_member_key": "system-member:root:pipe-start",
            "system_key": "system:gas",
            "member_kind": "port",
            "member_key": "port:pipe-001:start",
        }
    )
    assert validate_modeling_input_closure(root_membership, _profile()).passed

    two_leaf = deepcopy(root_membership)
    two_leaf["systems"].append(
        {
            "system_key": "system:gas-trial",
            "parent_system_key": "system:gas",
            "system_role": "GAS_TRIAL",
        }
    )
    two_leaf["system_members"].append(
        {
            "system_member_key": "system-member:trial:pipe-start",
            "system_key": "system:gas-trial",
            "member_kind": "port",
            "member_key": "port:pipe-001:start",
        }
    )
    assert "PORT_LEAF_SYSTEM_CARDINALITY" in _codes(two_leaf)

    invalid_not_applicable = _tables()
    _row(invalid_not_applicable, "products", "product_key", "product:valve-body-001")[
        "system_membership_requirement"
    ] = "not_applicable"
    assert "PRODUCT_SYSTEM_NOT_APPLICABLE" in _codes(invalid_not_applicable)

    valid_not_applicable = _tables()
    _row(valid_not_applicable, "products", "product_key", "product:valve-body-001")[
        "system_membership_requirement"
    ] = "not_applicable"
    valid_not_applicable["system_members"] = [
        row
        for row in valid_not_applicable["system_members"]
        if not (
            row["member_kind"] == "product"
            and row["member_key"] == "product:valve-body-001"
        )
    ]
    assert validate_modeling_input_closure(valid_not_applicable, _profile()).passed


def test_port_state_and_external_connection_cardinality_are_closed() -> None:
    missing_reason = _tables()
    _row(missing_reason, "ports", "port_key", "port:pipe-001:start")[
        "open_reason_code"
    ] = None
    assert "OPEN_PORT_REASON_MISSING" in _codes(missing_reason)

    multiply_connected = _tables()
    multiply_connected["connections"].append(
        {
            "connection_key": "connection:duplicate",
            "port_a_key": "port:pipe-001:end",
            "port_b_key": "port:valve-001:a",
            "connection_scope": "accepted_external_physical",
        }
    )
    assert "PORT_MULTIPLY_CONNECTED" in _codes(multiply_connected)

    internal = _tables()
    internal["connections"].append(
        {
            "connection_key": "connection:internal-valve",
            "port_a_key": "port:valve-001:a",
            "port_b_key": "port:valve-001:b",
            "connection_scope": "accepted_external_physical",
        }
    )
    assert "CONNECTION_INTERNAL_PRODUCT" in _codes(internal)


def test_material_property_and_spatial_rows_keep_typed_meaning() -> None:
    material = _tables()
    material["materials_styles"] = [
        row
        for row in material["materials_styles"]
        if row["material_style_key"] != "material-binding:valve-body-001"
    ]
    assert "PRODUCT_MATERIAL_BINDING_MISSING" in _codes(material)

    prop = _tables()
    prop["property_values"][0]["owner_key"] = "product:absent"
    assert "PROPERTY_OWNER_UNKNOWN" in _codes(prop)

    group = _tables()
    site = _row(group, "spatial_nodes", "spatial_node_key", "spatial:site")
    site["node_kind"] = "GROUP"
    assert {
        "GROUP_HAS_SPATIAL_PARENT",
        "GROUP_USED_FOR_CONTAINMENT",
    }.issubset(_codes(group))


def test_piperun_definition_member_and_unresolved_rows_are_not_name_only() -> None:
    mismatch = _tables()
    member = next(
        row for row in mismatch["piperun_members"] if row["row_kind"] == "member"
    )
    member["homogeneity_key"] = "STEEL|0.050|GAS_LOW"
    assert "PIPERUN_HOMOGENEITY_MISMATCH" in _codes(mismatch)

    no_definition = _tables()
    no_definition["piperun_members"] = [
        row for row in no_definition["piperun_members"] if row["row_kind"] == "member"
    ]
    assert "PIPERUN_DEFINITION_MISSING" in _codes(no_definition)

    unresolved = _tables()
    unresolved["unresolved_items"][0]["issue_code"] = ""
    assert "FIELD_EMPTY" in _codes(unresolved)


def test_current_name_only_m2c_shape_is_explicitly_insufficient() -> None:
    legacy = _tables()
    for product in legacy["products"]:
        product.pop("ifc_class")
        product.pop("system_membership_requirement")
    for port in legacy["ports"]:
        port.pop("connection_state")
        port.pop("flow_direction_authority")
    for row in legacy["materials_styles"]:
        row.pop("row_kind")

    report = validate_modeling_input_closure(legacy, _profile())

    assert not report.passed
    assert "FIELD_MISSING" in report.issue_codes
