from __future__ import annotations

import ast
from pathlib import Path

import pyarrow as pa

from tests.process_support.gas_modeling_input_closure import (
    arrow_schema_for_domain,
    load_component_catalog,
    load_closure_tables,
    load_modeling_input_schema,
    validate_exact_modeling_input_arrow_tables,
    validate_exact_modeling_input_submission,
)


TESTS_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = TESTS_ROOT / "fixtures/gas_agent_ab/common/modeling_inputs.schema.json"
CATALOG_PATH = TESTS_ROOT / "fixtures/gas_agent_ab/common/component_catalog.yaml"
FIXTURE_PATH = TESTS_ROOT / "fixtures/gas_modeling_input_closure/synthetic/exact_complete.json"
VALIDATOR_PATH = TESTS_ROOT / "process_support/gas_modeling_input_closure.py"
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
BASE_SEMANTIC_HELPERS = {
    "_validate_axis_vertices",
    "_validate_source_lineage",
    "_validate_geometry_coverage",
    "_validate_ports_and_connections",
    "_validate_system_membership",
    "_validate_materials",
    "_validate_properties",
    "_validate_spatial_assignments",
    "_validate_piperuns",
}
EXACT_ENTRY_HELPERS = {
    "_validate_exact_source_contract",
    "_validate_exact_product_contract",
    "_validate_exact_geometry_contract",
    "_validate_exact_port_contract",
    "_validate_exact_connection_geometry",
    "_validate_exact_material_contract",
    "_validate_exact_property_contract",
    "_validate_exact_spatial_contract",
    "_validate_exact_piperun_contract",
    "_validate_exact_unresolved_contract",
    "_validate_exact_ifc_enum_contract",
    "_validate_exact_package_realization_contract",
}
EXACT_SEMANTIC_HELPERS = EXACT_ENTRY_HELPERS | {
    "_validate_exact_catalog_resources",
    "_validate_exact_bend_resource",
    "_validate_exact_tee_resource",
    "_validate_exact_saddle_resource",
    "_validate_exact_meter_resource",
}


def test_exact_schema_is_experiment_only_and_machine_complete() -> None:
    schema = load_modeling_input_schema(SCHEMA_PATH)

    assert schema["schema_kind"] == "arrow_relational_submission_contract"
    assert schema["scope"] == "experiment_only"
    assert schema["status"] == "exact_submission_contract"
    assert schema["public_ifc_mcp_contract"] is False
    assert schema["allow_extra_domains"] is False
    assert schema["allow_extra_columns"] is False
    assert set(schema["domains"]) == EXPECTED_DOMAINS

    for domain, spec in schema["domains"].items():
        names = [column["name"] for column in spec["columns"]]
        assert len(names) == len(set(names)), domain
        assert spec["primary_key"] in names
        assert all(column["arrow_type"] in {"string", "int32", "int64", "float64", "bool"} for column in spec["columns"])
        assert arrow_schema_for_domain(schema, domain).names == names


def test_every_semantic_validator_code_is_disclosed_in_the_common_schema() -> None:
    """Prevent any future evaluator rule from remaining hidden from either arm."""

    tree = ast.parse(VALIDATOR_PATH.read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    base_calls = {
        node.func.id
        for node in ast.walk(functions["validate_modeling_input_closure"])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("_validate_")
        and node.func.id != "_validate_parent_graph"
    }
    exact_calls = {
        node.func.id
        for node in ast.walk(functions["validate_exact_modeling_input_submission"])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("_validate_exact_")
        and node.func.id != "_validate_exact_scalar"
    }
    assert base_calls == BASE_SEMANTIC_HELPERS
    assert exact_calls == EXACT_ENTRY_HELPERS

    validator_codes: set[str] = set()
    for function_name in BASE_SEMANTIC_HELPERS | EXACT_SEMANTIC_HELPERS:
        for node in ast.walk(functions[function_name]):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "add"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                validator_codes.add(node.args[0].value)

    schema = load_modeling_input_schema(SCHEMA_PATH)
    constraints = schema["semantic_constraints"]
    rules = constraints["rules"]
    assert constraints["format"] == "cad2bim-semantic-constraints/v1"
    assert len(rules) == 18
    assert len({rule["id"] for rule in rules}) == len(rules)
    assert all(rule["domains"] and rule["requirements"] and rule["summary"] for rule in rules)

    disclosed_codes = [code for rule in rules for code in rule["error_codes"]]
    assert len(disclosed_codes) == len(set(disclosed_codes))
    assert set(disclosed_codes) == validator_codes


def test_pilot_01_semantic_failures_are_directly_locatable_in_public_rules() -> None:
    """The eleven non-structural Arm-A pilot failures must not require guessing."""

    pilot_codes = {
        "AXIS_ONLY_DIAMETER_FORBIDDEN",
        "GEOMETRY_STRATEGY_INPUT_MISSING",
        "MATERIAL_DEFINITION_INCOMPLETE",
        "MATERIAL_DEFINITION_SHAPE",
        "MATERIAL_DEFINITION_UNKNOWN",
        "MATERIAL_REALIZATION_INCOMPLETE",
        "MATERIAL_STYLE_OWNER",
        "PROPERTY_NUMBER_INVALID",
        "PROPERTY_UNKNOWN_DETAILS_MISSING",
        "SUPPRESSED_GEOMETRY_CARDINALITY",
        "SUPPRESSED_PRODUCT_UNRESOLVED_MISSING",
    }
    rules = load_modeling_input_schema(SCHEMA_PATH)["semantic_constraints"]["rules"]
    rule_by_code = {
        code: rule
        for rule in rules
        for code in rule["error_codes"]
    }

    assert pilot_codes <= set(rule_by_code)
    for code in pilot_codes:
        rule = rule_by_code[code]
        assert rule["kind"]
        assert rule["domains"]
        assert rule["requirements"]
        assert rule["summary"]


def test_exact_schema_exposes_all_package_only_realization_inputs() -> None:
    schema = load_modeling_input_schema(SCHEMA_PATH)
    products = {column["name"] for column in schema["domains"]["products"]["columns"]}
    geometry = {column["name"] for column in schema["domains"]["geometry_inputs"]["columns"]}
    ports = {column["name"] for column in schema["domains"]["ports"]["columns"]}
    materials = {column["name"] for column in schema["domains"]["materials_styles"]["columns"]}
    spatial = {column["name"] for column in schema["domains"]["spatial_assignments"]["columns"]}
    piperuns = {column["name"] for column in schema["domains"]["piperun_members"]["columns"]}

    assert {
        "ifc_class", "predefined_type", "object_type", "tag", "assembly_role",
        "geometry_strategy", "representation_strategy", "component_resource_key",
    }.issubset(products)
    assert {
        "resource_key", "resource_digest", "origin_x_m", "axis_x", "ref_direction_x",
        "port_1_key", "outer_diameter_m", "trim_start_m", "length_m", "radius_m", "scale_x",
    }.issubset(geometry)
    assert schema["domains"]["geometry_inputs"]["resource_binding_required_for"] == [
        "PARAMETRIC_FITTING",
        "TEMPLATE_MESH",
        "METER_MESH",
        "SERVICE_SADDLE",
    ]
    product_strategy = next(
        column
        for column in schema["domains"]["products"]["columns"]
        if column["name"] == "geometry_strategy"
    )["enum"]
    geometry_kind = next(
        column
        for column in schema["domains"]["geometry_inputs"]["columns"]
        if column["name"] == "geometry_kind"
    )["enum"]
    assert "REPRESENTATION_SUPPRESSED" in product_strategy
    assert "NO_GEOMETRY" in geometry_kind
    assert {
        "system_key", "local_x_m", "local_direction_x", "x_m", "direction_x",
        "open_reason_code", "outer_diameter_m",
    }.issubset(ports)
    assert {"red", "green", "blue", "transparency", "roughness", "metallic", "curve_width_m"}.issubset(materials)
    assert "relation_kind" in spatial
    assert {"source_run_key", "boundary_reason", "homogeneity_key", "requires_split", "split_reason"}.issubset(piperuns)


def test_exact_schema_and_public_catalog_share_one_unit_contract() -> None:
    schema = load_modeling_input_schema(SCHEMA_PATH)
    catalog = load_component_catalog(CATALOG_PATH)
    resource_rule = next(
        rule
        for rule in schema["semantic_constraints"]["rules"]
        if rule["id"] == "resources.catalog_realization"
    )

    assert resource_rule["requirements"]["catalog_coordinate_contract"] == {
        key: catalog["coordinate_contract"][key]
        for key in (
            "exact_submission_length_unit",
            "canonical_parametric_length_unit",
            "mapped_template_reference_length_unit",
        )
    }
    assert catalog["realization_contract"]["comparison_tolerance_m"] == schema[
        "connection_position_tolerance_m"
    ]


def test_exact_fixture_passes_rows_and_materialized_arrow_tables() -> None:
    schema = load_modeling_input_schema(SCHEMA_PATH)
    rows = load_closure_tables(FIXTURE_PATH)

    assert validate_exact_modeling_input_submission(rows, schema).passed
    tables = {
        domain: pa.Table.from_pylist(rows[domain], schema=arrow_schema_for_domain(schema, domain))
        for domain in schema["domains"]
    }
    assert validate_exact_modeling_input_arrow_tables(tables, schema).passed


def test_exact_schema_and_fixture_contain_no_golden_answer() -> None:
    combined = SCHEMA_PATH.read_text(encoding="utf-8") + FIXTURE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "ad00c6a0e0c9b731ea6cdb4f87ecc1aab4bfd3f47abe8f597560dc3108691436",
        "988fb614a8849c3b547830be2942310b075124d5401b5f6a0a15ab40c9c0df11",
        "18848",
        "49990",
        "23012",
        "23323",
        "7695",
    ):
        assert forbidden not in combined
