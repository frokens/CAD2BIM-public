from __future__ import annotations

from pathlib import Path
from typing import Any

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
FIXTURE_PATH = TESTS_ROOT / "fixtures/gas_modeling_input_closure/synthetic/exact_complete.json"
CATALOG_PATH = TESTS_ROOT / "fixtures/gas_agent_ab/common/component_catalog.yaml"


def _schema() -> dict[str, Any]:
    return load_modeling_input_schema(SCHEMA_PATH)


def _tables() -> dict[str, list[dict[str, Any]]]:
    return load_closure_tables(FIXTURE_PATH)


def _catalog() -> dict[str, Any]:
    return load_component_catalog(CATALOG_PATH)


def _codes(tables: dict[str, Any]) -> frozenset[str]:
    return validate_exact_modeling_input_submission(tables, _schema()).issue_codes


def _catalog_codes(tables: dict[str, Any]) -> frozenset[str]:
    return validate_exact_modeling_input_submission(
        tables,
        _schema(),
        component_catalog=_catalog(),
    ).issue_codes


def _row(tables: dict[str, Any], domain: str, key_field: str, key: str) -> dict[str, Any]:
    return next(row for row in tables[domain] if row[key_field] == key)


def test_exact_contract_rejects_extra_column_wrong_type_and_nonnullable_null() -> None:
    extra = _tables()
    extra["products"][0]["invented"] = "not in contract"
    assert "FIELD_UNDECLARED" in _codes(extra)

    wrong_type = _tables()
    wrong_type["ports"][0]["sequence_ordinal"] = "zero"
    assert "FIELD_TYPE_INVALID" in _codes(wrong_type)

    null = _tables()
    null["products"][0]["geometry_strategy"] = None
    assert "FIELD_NULL" in _codes(null)


def test_exact_geometry_strategy_and_resource_binding_fail_closed() -> None:
    strategy = _tables()
    _row(strategy, "products", "product_key", "product:valve-body-001")["geometry_strategy"] = "TEMPLATE_COMPOSITE"
    assert {"COMPONENT_RESOURCE_MISSING", "GEOMETRY_STRATEGY_INPUT_MISSING"}.issubset(_codes(strategy))

    template = _tables()
    body = _row(template, "products", "product_key", "product:valve-body-001")
    body["geometry_strategy"] = "TEMPLATE_COMPOSITE"
    body["component_resource_key"] = "component:valve-body"
    geometry = _row(template, "geometry_inputs", "geometry_input_key", "geometry:valve-body-001")
    geometry["geometry_kind"] = "TEMPLATE_MESH"
    geometry["resource_key"] = "component:valve-body"
    geometry["resource_digest"] = None
    geometry["scale_x"] = geometry["scale_y"] = geometry["scale_z"] = 1.0
    assert "GEOMETRY_RESOURCE_BINDING_MISSING" in _codes(template)

    fitting = _tables()
    fitting_product = _row(fitting, "products", "product_key", "product:valve-body-001")
    fitting_product["geometry_strategy"] = "PARAMETRIC_FITTING"
    fitting_geometry = _row(
        fitting, "geometry_inputs", "geometry_input_key", "geometry:valve-body-001"
    )
    fitting_geometry["geometry_kind"] = "PARAMETRIC_FITTING"
    assert "GEOMETRY_RESOURCE_BINDING_MISSING" in _codes(fitting)

    saddle = _tables()
    saddle_product = _row(saddle, "products", "product_key", "product:valve-body-001")
    saddle_product["geometry_strategy"] = "SERVICE_SADDLE"
    saddle_geometry = _row(
        saddle, "geometry_inputs", "geometry_input_key", "geometry:valve-body-001"
    )
    saddle_geometry["geometry_kind"] = "SERVICE_SADDLE"
    assert "GEOMETRY_RESOURCE_BINDING_MISSING" in _codes(saddle)


def test_exact_axis_ports_and_connection_geometry_must_agree() -> None:
    endpoint = _tables()
    _row(endpoint, "ports", "port_key", "port:pipe-001:end")["x_m"] = 0.9
    codes = _codes(endpoint)
    assert {"AXIS_ENDPOINT_PORT_MISMATCH", "CONNECTED_PORT_POSITION_MISMATCH"}.issubset(codes)

    direction = _tables()
    valve_port = _row(direction, "ports", "port_key", "port:valve-body-001:a")
    valve_port["direction_x"] = 1.0
    assert "CONNECTED_PORT_DIRECTION_MISMATCH" in _codes(direction)


def test_exact_axis_only_representation_preserves_unknown_diameter_without_body() -> None:
    tables = _tables()
    product = _row(tables, "products", "product_key", "product:pipe-001")
    product["geometry_strategy"] = "AXIS_ONLY"
    product["representation_strategy"] = "AXIS"
    product["representation_quality"] = "PLACEHOLDER"
    geometry = _row(
        tables, "geometry_inputs", "geometry_input_key", "geometry:pipe-001"
    )
    geometry["geometry_kind"] = "AXIS_ONLY"
    geometry["geometry_role"] = "SOURCE_AXIS_WITH_BODY_SUPPRESSED"
    geometry["representation_role"] = "AXIS"
    geometry["outer_diameter_m"] = None
    for port in tables["ports"]:
        if port["owner_product_key"] == "product:pipe-001":
            port["diameter_status"] = "unknown"
            port["outer_diameter_m"] = None
    assert validate_exact_modeling_input_submission(tables, _schema()).passed

    end_port = _row(tables, "ports", "port_key", "port:pipe-001:end")
    original_x = end_port["x_m"]
    end_port["x_m"] = original_x + 0.1
    assert "AXIS_ENDPOINT_PORT_MISMATCH" in _codes(tables)
    end_port["x_m"] = original_x

    geometry["outer_diameter_m"] = 0.032
    assert "AXIS_ONLY_DIAMETER_FORBIDDEN" in _codes(tables)


def test_representation_suppressed_is_a_strict_nonassembly_no_geometry_state() -> None:
    tables = _tables()
    product = _row(
        tables, "products", "product_key", "product:degenerate-fitting-001"
    )
    geometry = _row(
        tables,
        "geometry_inputs",
        "geometry_input_key",
        "geometry:degenerate-fitting-001",
    )
    assert product["assembly_role"] == "not_assembly"
    assert product["representation_strategy"] == "NONE"
    assert product["representation_quality"] == "NONE"
    assert geometry["geometry_kind"] == "NO_GEOMETRY"
    assert geometry["representation_role"] == "NONE"
    assert validate_exact_modeling_input_submission(tables, _schema()).passed

    body = _tables()
    _row(body, "geometry_inputs", "geometry_input_key", "geometry:degenerate-fitting-001")[
        "outer_diameter_m"
    ] = 0.1
    assert "NO_GEOMETRY_PARAMETER_FORBIDDEN" in _codes(body)

    axis = _tables()
    axis["axis_vertices"].append(
        {
            "axis_vertex_key": "axis:degenerate:0",
            "product_key": "product:degenerate-fitting-001",
            "sequence_ordinal": 0,
            "x_m": 2.0,
            "y_m": 0.0,
            "z_m": 0.0,
        }
    )
    assert "SUPPRESSED_AXIS_FORBIDDEN" in _codes(axis)


def test_representation_suppressed_requires_exact_open_blocking_unresolved() -> None:
    missing = _tables()
    missing["unresolved_items"] = [
        row
        for row in missing["unresolved_items"]
        if row["subject_key"] != "product:degenerate-fitting-001"
    ]
    assert "SUPPRESSED_PRODUCT_UNRESOLVED_MISSING" in _codes(missing)

    wrong_severity = _tables()
    issue = next(
        row
        for row in wrong_severity["unresolved_items"]
        if row["subject_key"] == "product:degenerate-fitting-001"
    )
    issue["severity"] = "warning"
    assert "SUPPRESSED_PRODUCT_UNRESOLVED_MISSING" in _codes(wrong_severity)

    wrong_status = _tables()
    issue = next(
        row
        for row in wrong_status["unresolved_items"]
        if row["subject_key"] == "product:degenerate-fitting-001"
    )
    issue["resolution_status"] = "accepted_unknown"
    assert "SUPPRESSED_PRODUCT_UNRESOLVED_MISSING" in _codes(wrong_status)


def test_exact_assembly_inherits_spatial_container_without_duplicate_containment() -> None:
    duplicate = _tables()
    duplicate["spatial_assignments"].append(
        {
            "spatial_assignment_key": "spatial-assignment:valve-body",
            "spatial_node_key": "spatial:site",
            "product_key": "product:valve-body-001",
            "assignment_kind": "spatial_containment",
            "relation_kind": "IfcRelContainedInSpatialStructure",
            "assignment_reason": "INVALID_DUPLICATE_CHILD_CONTAINMENT",
        }
    )
    assert "PRODUCT_SPATIAL_CONTAINMENT_NOT_APPLICABLE" in _codes(duplicate)

    orphan = _tables()
    child = _row(orphan, "products", "product_key", "product:valve-body-001")
    child["assembly_parent_product_key"] = None
    assert {"ASSEMBLY_COMPONENT_PARENT_INVALID", "SPATIAL_INHERITANCE_WITHOUT_ASSEMBLY"}.intersection(_codes(orphan))


def test_exact_property_spatial_relation_and_piperun_split_are_explicit() -> None:
    property_value = _tables()
    property_value["property_values"][0]["value_string"] = "100"
    assert "PROPERTY_TYPED_VALUE_INVALID" in _codes(property_value)

    spatial = _tables()
    spatial["spatial_assignments"][0]["relation_kind"] = "IfcRelAssignsToGroup"
    assert "SPATIAL_RELATION_KIND_INVALID" in _codes(spatial)

    split = _tables()
    definition = next(row for row in split["piperun_members"] if row["row_kind"] == "run_definition")
    definition["requires_split"] = True
    assert "PIPERUN_SPLIT_REASON_MISSING" in _codes(split)


def test_exact_unresolved_subject_and_material_realization_are_closed() -> None:
    unresolved = _tables()
    unresolved["unresolved_items"][0]["subject_key"] = "product:absent"
    assert "UNRESOLVED_SUBJECT_UNKNOWN" in _codes(unresolved)

    material = _tables()
    definition = next(row for row in material["materials_styles"] if row["row_kind"] == "definition")
    definition["roughness"] = None
    assert "MATERIAL_REALIZATION_INCOMPLETE" in _codes(material)


def test_package_realization_cardinality_trim_selectors_and_frames_are_public() -> None:
    cardinality = _tables()
    duplicate = dict(cardinality["geometry_inputs"][0])
    duplicate["geometry_input_key"] = "geometry:pipe-001:duplicate"
    duplicate["sequence_ordinal"] = 1
    cardinality["geometry_inputs"].append(duplicate)
    assert "GEOMETRY_INPUT_CARDINALITY_INVALID" in _codes(cardinality)

    trim = _tables()
    geometry = _row(trim, "geometry_inputs", "geometry_input_key", "geometry:pipe-001")
    geometry["trim_start_m"] = 0.6
    geometry["trim_end_m"] = 0.4
    assert "AXIS_TRIM_EXCEEDS_LENGTH" in _codes(trim)

    systems = _tables()
    systems["systems"][1]["parent_system_key"] = None
    assert "SITE_SERVICE_SYSTEM_CARDINALITY" in _codes(systems)

    sites = _tables()
    sites["spatial_nodes"][1]["object_type"] = "NOT_ENGINEERING_SITE"
    assert "SITE_SERVICE_SITE_CARDINALITY" in _codes(sites)

    frame = _tables()
    _row(frame, "ports", "port_key", "port:pipe-001:start")["local_y_m"] = 0.1
    assert "PORT_LOCAL_WORLD_POSITION_MISMATCH" in _codes(frame)


def test_ifc_enums_are_rejected_before_the_ifc_runtime() -> None:
    product = _tables()
    product["products"][0]["predefined_type"] = "NOT_A_REAL_ENUM"
    assert "IFC_PRODUCT_PREDEFINED_TYPE_INVALID" in _codes(product)

    port = _tables()
    port["ports"][0]["system_type"] = "NOT_A_REAL_ENUM"
    assert "IFC_PORT_SYSTEM_TYPE_INVALID" in _codes(port)

    system = _tables()
    system["systems"][0]["predefined_type"] = "NOT_A_REAL_ENUM"
    assert "IFC_SYSTEM_PREDEFINED_TYPE_INVALID" in _codes(system)


def test_public_catalog_drives_resource_binding_and_straight_geometry() -> None:
    tables = _tables()
    product = _row(tables, "products", "product_key", "product:valve-body-001")
    product["ifc_class"] = "IfcPipeFitting"
    product["predefined_type"] = "CONNECTOR"
    product["geometry_strategy"] = "PARAMETRIC_FITTING"
    product["component_resource_key"] = "PARAMETRIC_STRAIGHT_CONNECTOR"
    geometry = _row(
        tables, "geometry_inputs", "geometry_input_key", "geometry:valve-body-001"
    )
    resource = next(
        row
        for row in _catalog()["resources"]
        if row["resource_key"] == "PARAMETRIC_STRAIGHT_CONNECTOR"
    )
    geometry["geometry_kind"] = "PARAMETRIC_FITTING"
    geometry["resource_key"] = resource["resource_key"]
    geometry["resource_digest"] = resource["resource_digest"]
    assert not {
        "RESOURCE_BINDING_UNKNOWN",
        "RESOURCE_STRATEGY_MISMATCH",
        "RESOURCE_PORT_CARDINALITY_INVALID",
        "PRODUCT_RESOURCE_BINDING_MISMATCH",
        "STRAIGHT_CONNECTOR_INTERFACE_COLLAPSED",
    }.intersection(_catalog_codes(tables))

    bad_digest = _tables()
    bad_product = _row(
        bad_digest, "products", "product_key", "product:valve-body-001"
    )
    bad_product.update(product)
    bad_geometry = _row(
        bad_digest,
        "geometry_inputs",
        "geometry_input_key",
        "geometry:valve-body-001",
    )
    bad_geometry.update(geometry)
    bad_geometry["resource_digest"] = "0" * 64
    assert "RESOURCE_BINDING_UNKNOWN" in _catalog_codes(bad_digest)

    collapsed = tables
    _row(collapsed, "ports", "port_key", "port:valve-body-001:b")["local_x_m"] = -0.1
    assert "STRAIGHT_CONNECTOR_INTERFACE_COLLAPSED" in _catalog_codes(collapsed)


def test_arrow_validator_rejects_physical_type_drift() -> None:
    schema = _schema()
    rows = _tables()
    arrow_tables = {
        domain: pa.Table.from_pylist(rows[domain], schema=arrow_schema_for_domain(schema, domain))
        for domain in schema["domains"]
    }
    product_schema = arrow_tables["products"].schema
    fields = list(product_schema)
    index = product_schema.get_field_index("name")
    fields[index] = pa.field("name", pa.large_string(), nullable=False)
    arrow_tables["products"] = pa.Table.from_arrays(
        [arrow_tables["products"].column(name) for name in product_schema.names],
        schema=pa.schema(fields),
    )

    assert "ARROW_SCHEMA_MISMATCH" in validate_exact_modeling_input_arrow_tables(
        arrow_tables, schema
    ).issue_codes


def test_arrow_physical_nullable_metadata_does_not_override_logical_rows() -> None:
    schema = _schema()
    rows = _tables()

    physically_nullable = {}
    for domain in schema["domains"]:
        expected = arrow_schema_for_domain(schema, domain)
        physical_schema = pa.schema(
            [pa.field(field.name, field.type, nullable=True) for field in expected]
        )
        physically_nullable[domain] = pa.Table.from_pylist(
            rows[domain], schema=physical_schema
        )
    report = validate_exact_modeling_input_arrow_tables(physically_nullable, schema)
    assert report.passed

    required_null_rows = _tables()
    required_null_rows["products"][0]["name"] = None
    expected = arrow_schema_for_domain(schema, "products")
    nullable_products = pa.schema(
        [pa.field(field.name, field.type, nullable=True) for field in expected]
    )
    physically_nullable["products"] = pa.Table.from_pylist(
        required_null_rows["products"], schema=nullable_products
    )
    report = validate_exact_modeling_input_arrow_tables(physically_nullable, schema)
    assert "ARROW_SCHEMA_MISMATCH" not in report.issue_codes
    assert "FIELD_NULL" in report.issue_codes
