from __future__ import annotations

import json
import math
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pyarrow as pa
import pytest

from CAD2BIM_backend.domain.geometry_frame import rigid_transform_from_axis_reference


SOURCE = Path(__file__).parents[2] / "scenarios" / "family_baseline_input" / "transformation.py"
SOURCE_NAMESPACE = "0" * 64


def test_full_source_satisfies_controlled_data_transform_contract() -> None:
    from CAD2BIM_backend.infrastructure.data_transform.capabilities import (
        PYTHON_ARROW_APPROVED_LIBRARIES,
        PYTHON_ARROW_SAFE_STDLIB,
    )
    from CAD2BIM_backend.infrastructure.worker_runtime.python_user_code import compile_entry

    compile_entry(SOURCE.read_text(), "transform", allowed_import_roots={
        *PYTHON_ARROW_APPROVED_LIBRARIES, *PYTHON_ARROW_SAFE_STDLIB,
    })


def _module():
    spec = spec_from_file_location("family_baseline_input_transformation", SOURCE)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table(rows: list[dict[str, object]]) -> pa.Table:
    return pa.Table.from_pylist(rows)


def _writer_issue(key, product, subject, code, affected, actions=(), *, status="open",
                  component=None):
    return {"unresolved_item_key": key, "issue_code": code, "subject_key": subject,
            "product_key": product, "component_key": component, "affected_field": affected,
            "resolution_status": status, "blocked_actions": list(actions),
            "authority_key": "writer:source", "evidence_json": "{}", "message": "source issue"}


def _inputs(*, reverse: bool = False, chunked: bool = False) -> dict[str, pa.Table]:
    products = [
        {
            "product_key": "source:bend",
            "product_kind": "BEND",
            "source_disposition_key": "disp:bend",
            "assembly_parent_product_key": None,
            "origin_kind": "source",
            "name": "Turning reducer",
            "geometry_strategy": "AXIS_SWEEP",
            "component_resource_key": None,
        },
        {
            "product_key": "source:pipe",
            "product_kind": "PIPE",
            "source_disposition_key": "disp:pipe",
            "assembly_parent_product_key": None,
            "origin_kind": "source",
            "name": "Pipe",
            "geometry_strategy": "AXIS_SWEEP",
            "component_resource_key": None,
        },
    ]
    ports = [
        {
            "port_key": "port:b1",
            "owner_product_key": "source:bend",
            "sequence_ordinal": 1,
            "port_role": "INLET",
            "system_type": "GAS",
            "diameter_status": "known",
            "outer_diameter_m": 0.2,
            "direction_x": -1.0,
            "direction_y": 0.0,
            "direction_z": 0.0,
        },
        {
            "port_key": "port:b2",
            "owner_product_key": "source:bend",
            "sequence_ordinal": 2,
            "port_role": "OUTLET",
            "system_type": "GAS",
            "diameter_status": "known",
            "outer_diameter_m": 0.1,
            "direction_x": 0.0,
            "direction_y": 1.0,
            "direction_z": 0.0,
        },
        {
            "port_key": "port:p1",
            "owner_product_key": "source:pipe",
            "sequence_ordinal": 1,
            "port_role": "END_A",
            "system_type": "GAS",
            "diameter_status": "known",
            "outer_diameter_m": 0.2,
            "direction_x": 1.0,
            "direction_y": 0.0,
            "direction_z": 0.0,
        },
    ]
    source_dispositions = [
        {
            "source_disposition_key": "disp:bend",
            "source_entity_key": "entity:bend",
            "source_business_id": "business:bend",
            "source_layer": "fittings",
            "ingestion_disposition": "accepted",
            "modeling_disposition": "modeled",
            "source_entity_type": "INSERT",
        },
        {
            "source_disposition_key": "disp:pipe",
            "source_entity_key": "entity:pipe",
            "source_business_id": "business:pipe",
            "source_layer": "pipes",
            "ingestion_disposition": "accepted",
            "modeling_disposition": "modeled",
            "source_entity_type": "LINE",
        },
    ]
    values = {
        "products": _table(products),
        "ports": _table(ports),
        "connections": _table(
            [
                {
                    "connection_key": "connection:1",
                    "port_a_key": "port:b1",
                    "port_b_key": "port:p1",
                }
            ]
        ),
        "geometry_inputs": _table(
            [
                {
                    "product_key": "source:bend",
                    "sequence_ordinal": 1,
                    "origin_x_m": 1.0,
                    "origin_y_m": 2.0,
                    "origin_z_m": 3.0,
                    "axis_x": 1.0,
                    "axis_y": 0.0,
                    "axis_z": 0.0,
                    "ref_direction_x": 0.0,
                    "ref_direction_y": 1.0,
                    "ref_direction_z": 0.0,
                    "length_m": 0.4,
                    "width_m": None,
                    "height_m": None,
                    "radius_m": 0.2,
                    "outer_diameter_m": 0.2,
                    "inner_diameter_m": None,
                    "trim_start_m": 0.0,
                    "trim_end_m": 0.0,
                    "geometry_kind": "AXIS_SWEEP",
                }
            ]
        ),
        "product_origins": _table([
            {"origin_key": "origin:source:bend", "product_key": "source:bend",
             "origin_x_m": 1.0, "origin_y_m": 2.0, "origin_z_m": 3.0,
             "origin_status": "known", "authority_key": "writer:source:bend"},
            {"origin_key": "origin:source:pipe", "product_key": "source:pipe",
             "origin_x_m": 0.0, "origin_y_m": 0.0, "origin_z_m": 0.0,
             "origin_status": "known", "authority_key": "writer:source:pipe"},
        ]),
        "axis_vertices": _table(
            [
                {
                    "axis_vertex_key": "vertex:1",
                    "product_key": "source:bend",
                    "sequence_ordinal": 1,
                    "x_m": 1.0,
                    "y_m": 2.0,
                    "z_m": 3.0,
                },
                {
                    "axis_vertex_key": "vertex:2",
                    "product_key": "source:bend",
                    "sequence_ordinal": 2,
                    "x_m": 1.2,
                    "y_m": 2.2,
                    "z_m": 3.0,
                },
            ]
        ),
        "property_values": _table(
            [
                {
                    "property_value_key": "property:1",
                    "owner_key": "source:bend",
                    "property_name": "tyqxh",
                    "value_string": "TR-20-10",
                    "value_identifier": None,
                    "value_number": None,
                }
            ]
        ),
        "source_dispositions": _table(source_dispositions),
        "unresolved_items": _table(
            [
                _writer_issue("unresolved:other", "source:bend", "source:bend",
                              "OTHER", "other")
            ]
        ),
        "product_instances": _table(
            [
                {
                    "product_key": "source:bend",
                    "source_entity_key": "entity:bend",
                    "material": "steel",
                    "pressure_normalized": "medium",
                },
                {
                    "product_key": "source:pipe",
                    "source_entity_key": "entity:pipe",
                    "material": "steel",
                    "pressure_normalized": "medium",
                },
            ]
        ),
    }
    if reverse:
        return {
            name: pa.Table.from_pylist(list(reversed(table.to_pylist())))
            for name, table in values.items()
        }
    if chunked:
        return {
            name: (
                pa.concat_tables([table.slice(0, 1), table.slice(1)])
                if table.num_rows > 1
                else table
            )
            for name, table in values.items()
        }
    return values


def _assembly_inputs() -> dict[str, pa.Table]:
    values = _inputs()

    def append(name: str, *rows: dict[str, object]) -> None:
        values[name] = pa.Table.from_pylist(values[name].to_pylist() + list(rows))

    append(
        "products",
        {
            "product_key": "source:regulator",
            "product_kind": "REGULATOR_ASSEMBLY",
            "source_disposition_key": "disp:regulator",
            "assembly_parent_product_key": None,
            "origin_kind": "source",
            "name": "Regulator",
            "geometry_strategy": "ASSEMBLY",
            "component_resource_key": None,
        },
        {
            "product_key": "source:regulator:body",
            "product_kind": "REGULATOR_BODY",
            "source_disposition_key": None,
            "assembly_parent_product_key": "source:regulator",
            "origin_kind": "assembly",
            "name": "Body",
            "geometry_strategy": "BODY",
            "component_resource_key": "BODY_RESOURCE",
        },
        {
            "product_key": "source:regulator:bend_a",
            "product_kind": "REGULATOR_BEND",
            "source_disposition_key": None,
            "assembly_parent_product_key": "source:regulator",
            "origin_kind": "assembly",
            "name": "Connector bend",
            "geometry_strategy": "BEND",
            "component_resource_key": "BEND_RESOURCE",
        },
    )
    append(
        "ports",
        {
            "port_key": "port:regulator:body",
            "owner_product_key": "source:regulator:body",
            "sequence_ordinal": 1,
            "port_role": "INTERNAL_BODY",
            "connection_state": "connected",
            "diameter_status": "known",
            "outer_diameter_m": 0.05,
            "direction_x": 1.0,
            "direction_y": 0.0,
            "direction_z": 0.0,
        },
        {
            "port_key": "port:regulator:bend:internal",
            "owner_product_key": "source:regulator:bend_a",
            "sequence_ordinal": 1,
            "port_role": "INTERNAL_BEND",
            "connection_state": "connected",
            "diameter_status": "known",
            "outer_diameter_m": 0.05,
            "direction_x": -1.0,
            "direction_y": 0.0,
            "direction_z": 0.0,
        },
        {
            "port_key": "port:regulator:bend:external",
            "owner_product_key": "source:regulator:bend_a",
            "sequence_ordinal": 2,
            "port_role": "EXTERNAL_BEND",
            "connection_state": "connected",
            "diameter_status": "known",
            "outer_diameter_m": 0.15,
            "direction_x": 0.0,
            "direction_y": 1.0,
            "direction_z": 0.0,
        },
        {
            "port_key": "port:p2",
            "owner_product_key": "source:pipe",
            "sequence_ordinal": 2,
            "port_role": "END",
            "connection_state": "connected",
            "diameter_status": "known",
            "outer_diameter_m": 0.15,
            "direction_x": 0.0,
            "direction_y": -1.0,
            "direction_z": 0.0,
        },
    )
    append(
        "connections",
        {
            "connection_key": "connection:regulator:internal",
            "port_a_key": "port:regulator:body",
            "port_b_key": "port:regulator:bend:internal",
        },
        {
            "connection_key": "connection:regulator:external",
            "port_a_key": "port:regulator:bend:external",
            "port_b_key": "port:p2",
        },
    )
    append(
        "source_dispositions",
        {
            "source_disposition_key": "disp:regulator",
            "source_entity_key": "entity:regulator",
            "source_business_id": "business:regulator",
            "source_layer": "regulators",
            "ingestion_disposition": "accepted",
            "modeling_disposition": "modeled",
            "source_entity_type": "INSERT",
        },
    )
    append(
        "product_instances",
        {
            "product_key": "source:regulator",
            "source_entity_key": "entity:regulator",
            "material": "steel",
            "pressure_normalized": "medium",
        },
    )
    return values


def test_corrected_bend_category_is_not_reinterpreted_from_port_diameters() -> None:
    module = _module()
    outputs = module.compile_tables(_inputs(), SOURCE_NAMESPACE)
    products = outputs["expected_products"].to_pylist()
    transition = next(row for row in products if row["source_product_key"] == "source:bend")
    assert transition["category"] == "BEND"
    assert transition["turn_angle_deg"] == 90.0
    assert transition["diameter_min_m"] == 0.1
    assert transition["diameter_max_m"] == 0.2
    relations = [
        row
        for row in outputs["product_sources"].to_pylist()
        if row["product_key"] == transition["product_key"]
    ]
    assert {(row["source_identity"], row["relation_role"]) for row in relations} == {
        ("entity:entity:bend", "direct"),
        ("entity:entity:pipe", "adjacent_topology"),
    }


def test_standalone_riser_bends_join_bend_denominator_without_changing_riser_kind() -> None:
    inputs = _inputs()
    products = inputs["products"].to_pylist()
    for ordinal in (91, 101):
        products.append({**products[0], "product_key": f"source:riser-bend:{ordinal}",
                         "product_kind": "RISER_BEND", "source_disposition_key": None})
    products.append({**products[1], "product_key": "source:riser",
                     "product_kind": "RISER", "source_disposition_key": None})
    inputs["products"] = pa.Table.from_pylist(products)

    outputs = _module().compile_tables(inputs, SOURCE_NAMESPACE)
    categories = {row["source_product_key"]: row["category"]
                  for row in outputs["expected_products"].to_pylist()}
    assert categories["source:bend"] == "BEND"
    assert categories["source:riser-bend:91"] == categories["source:riser-bend:101"] == "BEND"
    assert categories["source:riser"] == "RISER"
    summary = {row["category"]: row["product_count"]
               for row in outputs["category_summary"].to_pylist()}
    assert summary["BEND"] == 3 and summary["RISER"] == 1
    assert sum(summary.values()) == 5
    assert {row["disposition"] for row in outputs["product_reconciliation"].to_pylist()
            if row["source_product_kind"] == "RISER_BEND"} == {"root_included"}


def test_corrected_transition_category_is_preserved() -> None:
    module = _module()
    inputs = _inputs()
    rows = inputs["products"].to_pylist()
    rows[0]["product_kind"] = "TRANSITION"
    inputs["products"] = pa.Table.from_pylist(rows)
    products = module.compile_tables(inputs, SOURCE_NAMESPACE)["expected_products"].to_pylist()
    transition = next(row for row in products if row["source_product_key"] == "source:bend")
    assert transition["category"] == "TRANSITION"


def test_output_keys_and_partitions_are_stable_under_input_reordering() -> None:
    module = _module()
    first = module.compile_tables(_inputs(), SOURCE_NAMESPACE)
    reordered = module.compile_tables(_inputs(reverse=True), SOURCE_NAMESPACE)
    for name in first:
        assert first[name].equals(reordered[name])


def test_output_is_stable_under_arrow_batch_boundaries() -> None:
    module = _module()
    first = module.compile_tables(_inputs(), SOURCE_NAMESPACE)
    chunked = module.compile_tables(_inputs(chunked=True), SOURCE_NAMESPACE)
    for name in first:
        assert first[name].equals(chunked[name])


def _physical_pair_inputs(*, reverse=False):
    inputs = _inputs(reverse=reverse)
    products = inputs["products"].to_pylist()
    for product in products:
        product["product_kind"] = "PIPE"
    inputs["products"] = pa.Table.from_pylist(products)
    ports = inputs["ports"].to_pylist()
    for port in ports:
        if port["port_key"] == "port:p1":
            port.update(direction_x=math.cos(0.01), direction_y=math.sin(0.01))
    inputs["ports"] = pa.Table.from_pylist(ports)
    connection = inputs["connections"].to_pylist()[0]
    connection.update(connection_reason="coincident_straight_through",
        connection_direction_source="derived_shared_axis_from_physical_port_pair",
        connection_direction_a_x=-math.cos(0.005), connection_direction_a_y=-math.sin(0.005),
        connection_direction_a_z=0.0, connection_direction_b_x=math.cos(0.005),
        connection_direction_b_y=math.sin(0.005), connection_direction_b_z=0.0)
    inputs["connections"] = pa.Table.from_pylist([connection])
    return inputs


def test_physical_and_connection_axes_project_separately_with_stable_identity():
    module = _module()
    inputs = _physical_pair_inputs()
    output = module.compile_tables(inputs, SOURCE_NAMESPACE)
    reordered = module.compile_tables(_physical_pair_inputs(reverse=True), SOURCE_NAMESPACE)
    for name in output:
        assert output[name].equals(reordered[name])
    reversed_pair = _physical_pair_inputs()
    relation, = reversed_pair["connections"].to_pylist()
    relation["port_a_key"], relation["port_b_key"] = relation["port_b_key"], relation["port_a_key"]
    for part in "xyz":
        a, b = "connection_direction_a_"+part, "connection_direction_b_"+part
        relation[a], relation[b] = relation[b], relation[a]
    reversed_pair["connections"] = pa.Table.from_pylist([relation])
    opposite_storage_order = module.compile_tables(reversed_pair, SOURCE_NAMESPACE)
    for name in output:
        assert output[name].equals(opposite_storage_order[name])
    ports = {p["port_key"]:p for p in inputs["ports"].to_pylist()}
    pair, = inputs["connections"].to_pylist()
    for row in output["product_port_relations"].to_pylist():
        assert [row["direction_"+a] for a in "xyz"] == [ports[row["source_port_key"]]["direction_"+a] for a in "xyz"]
        if row["connection_key"]:
            side = "a" if row["source_port_key"] == pair["port_a_key"] else "b"
            assert [row["connection_direction_"+a] for a in "xyz"] == [pair["connection_direction_"+side+"_"+a] for a in "xyz"]
        else:
            assert row["connection_direction_source"] is None
            assert row["connection_direction_x"] is None
    # Removing a relation's derived direction changes reusable input content, not product/port identity.
    pair["connection_reason"] = "physical_pair_without_derived_axis"
    for key in list(pair):
        if key.startswith("connection_direction_"):
            pair[key] = None
    inputs["connections"] = pa.Table.from_pylist([pair])
    plain = module.compile_tables(inputs, SOURCE_NAMESPACE)
    for table, key in (("expected_products", "product_key"), ("product_port_relations", "port_relation_key")):
        assert output[table][key].to_pylist() == plain[table][key].to_pylist()
    assert not output["product_combinations"].equals(plain["product_combinations"])


@pytest.mark.parametrize("field,value", [
    ("connection_direction_a_z", None), ("connection_direction_b_z", float("inf")),
    ("connection_direction_a_x", -2.0), ("connection_direction_b_x", -1.0),
    ("connection_direction_source", "wrong_source"), ("connection_reason", "unrelated"),
])
def test_transform_rejects_invalid_derived_direction_instead_of_using_physical_axis(field, value):
    inputs = _physical_pair_inputs()
    pair, = inputs["connections"].to_pylist()
    pair[field] = value
    inputs["connections"] = pa.Table.from_pylist([pair])
    with pytest.raises(ValueError, match="connection direction"):
        _module().compile_tables(inputs, SOURCE_NAMESPACE)


def test_transform_entry_reads_the_writer_product_origins_input() -> None:
    class Context:
        def __init__(self):
            self.inputs = _inputs()
            self.read_names = []
            self.outputs = {}

        def table(self, name):
            self.read_names.append(name)
            return self.inputs[name]

        def param(self, name):
            return {"source_cad_sha256": SOURCE_NAMESPACE,
                    "source_m4_package_id": "writer",
                    "source_product_package_id": "product",
                    "source_cad_package_id": "cad"}[name]

        def write_table(self, name, table):
            self.outputs[name] = table

    context = Context()
    _module().transform(context)
    assert len(context.read_names) == len(set(context.read_names)) == 10
    assert "product_origins" in context.read_names
    assert context.outputs["expected_products"].num_rows == 2


def test_twelve_category_denominator_is_explicit_even_when_empty() -> None:
    module = _module()
    summary = module.compile_tables(_inputs(), SOURCE_NAMESPACE)["category_summary"].to_pylist()
    assert [row["category"] for row in summary] == list(module.CATEGORIES)
    assert sum(row["product_count"] for row in summary) == 2


def test_missing_diameter_and_conflicting_model_values_are_diagnosed() -> None:
    module = _module()
    inputs = _inputs()
    ports = inputs["ports"].to_pylist()
    ports[-1]["diameter_status"] = "unknown"
    ports[-1]["outer_diameter_m"] = None
    inputs["ports"] = pa.Table.from_pylist(ports)
    properties = inputs["property_values"].to_pylist()
    properties.append(
        {**properties[0], "property_value_key": "property:2", "value_string": "TR-ALT"}
    )
    inputs["property_values"] = pa.Table.from_pylist(properties)
    issues = module.compile_tables(inputs, SOURCE_NAMESPACE)["input_issues"].to_pylist()
    assert {row["issue_code"] for row in issues} >= {
        "MISSING_REQUIRED_DIAMETER",
        "CONFLICTING_MODEL_VALUES",
    }


def test_source_content_namespace_prevents_cross_source_product_key_collision() -> None:
    module = _module()
    first = module.compile_tables(_inputs(), "0" * 64)["expected_products"]
    second = module.compile_tables(_inputs(), "1" * 64)["expected_products"]
    first_keys = {row["product_key"] for row in first.to_pylist()}
    second_keys = {row["product_key"] for row in second.to_pylist()}
    assert first_keys.isdisjoint(second_keys)


def test_assembly_internal_and_external_port_diameters_remain_correlated() -> None:
    module = _module()
    outputs = module.compile_tables(_assembly_inputs(), SOURCE_NAMESPACE)
    regulator = next(
        row
        for row in outputs["expected_products"].to_pylist()
        if row["source_product_key"] == "source:regulator"
    )
    assert regulator["root_source_port_count"] == 0
    assert regulator["component_source_port_count"] == 3
    assert regulator["observed_external_interface_count"] == 1
    assert regulator["diameter_min_m"] == regulator["diameter_max_m"] == 0.15
    relations = [
        row
        for row in outputs["product_port_relations"].to_pylist()
        if row["product_key"] == regulator["product_key"]
    ]
    assert {
        (row["source_port_key"], row["interface_scope"], row["outer_diameter_m"])
        for row in relations
    } >= {
        ("port:regulator:body", "component_internal", 0.05),
        ("port:regulator:bend:internal", "component_internal", 0.05),
        ("port:regulator:bend:external", "component_external", 0.15),
    }
    reconciliation = outputs["product_reconciliation"].to_pylist()
    assert len(reconciliation) == len(_assembly_inputs()["products"])


def test_full_source_axis_is_preserved_in_order_without_body_trim_invention() -> None:
    module = _module()
    inputs = _inputs()
    vertices = inputs["axis_vertices"].to_pylist()
    vertices.append(
        {
            "axis_vertex_key": "vertex:3",
            "product_key": "source:bend",
            "sequence_ordinal": 3,
            "x_m": 1.4,
            "y_m": 2.4,
            "z_m": 3.1,
        }
    )
    inputs["axis_vertices"] = pa.Table.from_pylist(vertices)

    outputs = module.compile_tables(inputs, SOURCE_NAMESPACE)
    bend = next(
        row
        for row in outputs["expected_products"].to_pylist()
        if row["source_product_key"] == "source:bend"
    )
    points = [
        row
        for row in outputs["product_path_points"].to_pylist()
        if row["product_key"] == bend["product_key"]
    ]
    assert bend["path_vertex_count"] == 3
    assert [(row["sequence_ordinal"], row["x_m"], row["y_m"], row["z_m"]) for row in points] == [
        (1, 1.0, 2.0, 3.0),
        (2, 1.2, 2.2, 3.0),
        (3, 1.4, 2.4, 3.1),
    ]
    expected_local = [(0.0, 0.0, 0.0), (0.2, 0.0, 0.2), (0.4, 0.1, 0.4)]
    for row, expected in zip(points, expected_local, strict=True):
        assert (
            row["local_x_m"],
            row["local_y_m"],
            row["local_z_m"],
        ) == pytest.approx(expected)
    assert {row["path_semantics"] for row in points} == {"source_axis"}
    assert json.loads(bend["parameters_json"])["path_semantics"] == (
        "source_axis_not_trimmed_body_path"
    )


def test_non_axial_m4_projection_round_trips_through_domain_frame_order() -> None:
    module = _module()
    inputs = _inputs()
    geometry = inputs["geometry_inputs"].to_pylist()
    geometry[0].update(
        axis_x=0.6,
        axis_y=0.8,
        axis_z=0.0,
        ref_direction_x=-0.8,
        ref_direction_y=0.6,
        ref_direction_z=0.0,
    )
    inputs["geometry_inputs"] = pa.Table.from_pylist(geometry)
    vertices = inputs["axis_vertices"].to_pylist()
    vertices[1].update(x_m=-1.0, y_m=6.0, z_m=6.0)
    inputs["axis_vertices"] = pa.Table.from_pylist(vertices)

    points = module.compile_tables(inputs, SOURCE_NAMESPACE)["product_path_points"].to_pylist()
    projected = next(row for row in points if row["sequence_ordinal"] == 2)
    m4_local = (
        projected["local_x_m"],
        projected["local_y_m"],
        projected["local_z_m"],
    )
    assert m4_local == pytest.approx((2.0, 3.0, 4.0))

    frame = rigid_transform_from_axis_reference((1.0, 2.0, 3.0), (0.6, 0.8, 0.0), (-0.8, 0.6, 0.0))
    domain_local = (m4_local[2], m4_local[1], m4_local[0])
    round_trip = tuple(
        frame.translation_m[index]
        + sum(frame.rotation[index][axis] * domain_local[axis] for axis in range(3))
        for index in range(3)
    )
    assert round_trip == pytest.approx((-1.0, 6.0, 6.0))

    m4_local_direction = (0.2, 0.3, 0.4)
    domain_local_direction = (
        m4_local_direction[2],
        m4_local_direction[1],
        m4_local_direction[0],
    )
    world_direction = tuple(
        sum(frame.rotation[index][axis] * domain_local_direction[axis] for axis in range(3))
        for index in range(3)
    )
    assert world_direction == pytest.approx((-0.2, 0.4, 0.3))


def test_duplicate_axis_sequence_is_rejected_instead_of_silently_reordered() -> None:
    module = _module()
    inputs = _inputs()
    vertices = inputs["axis_vertices"].to_pylist()
    vertices[1]["sequence_ordinal"] = 1
    inputs["axis_vertices"] = pa.Table.from_pylist(vertices)
    with pytest.raises(ValueError, match="sequence_ordinal must be unique"):
        module.compile_tables(inputs, SOURCE_NAMESPACE)


def test_approved_regulator_components_separate_source_evidence_from_design_requirement() -> None:
    module = _module()
    outputs = module.compile_tables(_assembly_inputs(), SOURCE_NAMESPACE)
    regulator = next(
        row
        for row in outputs["expected_products"].to_pylist()
        if row["source_product_key"] == "source:regulator"
    )
    components = {
        row["semantic_role"]: row
        for row in outputs["expected_components"].to_pylist()
        if row["parent_product_key"] == regulator["product_key"]
    }
    assert set(components) == {"body", "left_elbow", "right_elbow"}
    assert components["body"]["source_component_product_key"] == "source:regulator:body"
    assert components["left_elbow"]["source_component_product_key"] == ("source:regulator:bend_a")
    assert components["right_elbow"]["source_component_product_key"] is None
    assert components["right_elbow"]["evidence_status"] == (
        "approved_design_required_source_component_absent"
    )


def test_service_saddle_main_ports_are_host_datums_and_branch_remains_gas() -> None:
    module = _module()
    inputs = _inputs()

    def append(name: str, *rows: dict[str, object]) -> None:
        inputs[name] = pa.Table.from_pylist(inputs[name].to_pylist() + list(rows))

    append(
        "products",
        {
            "product_key": "source:saddle",
            "product_kind": "SERVICE_SADDLE",
            "source_disposition_key": "disp:saddle",
            "assembly_parent_product_key": None,
            "origin_kind": "source",
            "name": "Service saddle",
            "geometry_strategy": "PARAMETRIC",
            "component_resource_key": None,
        },
    )
    append(
        "ports",
        *[
            {
                "port_key": f"port:saddle:{role.lower()}",
                "owner_product_key": "source:saddle",
                "sequence_ordinal": ordinal,
                "port_role": role,
                "system_type": "GAS",
                "diameter_status": "known",
                "outer_diameter_m": diameter,
                "direction_x": direction[0],
                "direction_y": direction[1],
                "direction_z": direction[2],
            }
            for ordinal, (role, diameter, direction) in enumerate(
                (
                    ("MAIN_A", 0.2, (-1.0, 0.0, 0.0)),
                    ("MAIN_B", 0.2, (1.0, 0.0, 0.0)),
                    ("BRANCH", 0.05, (0.0, 1.0, 0.0)),
                ),
                start=1,
            )
        ],
    )
    append(
        "source_dispositions",
        {
            "source_disposition_key": "disp:saddle",
            "source_entity_key": "entity:saddle",
            "source_business_id": "business:saddle",
            "source_layer": "saddles",
            "ingestion_disposition": "accepted",
            "modeling_disposition": "modeled",
            "source_entity_type": "INSERT",
        },
    )
    append(
        "product_instances",
        {
            "product_key": "source:saddle",
            "source_entity_key": "entity:saddle",
            "material": "steel",
            "pressure_normalized": "medium",
        },
    )

    outputs = module.compile_tables(inputs, SOURCE_NAMESPACE)
    saddle = next(
        row
        for row in outputs["expected_products"].to_pylist()
        if row["source_product_key"] == "source:saddle"
    )
    relations = {
        row["port_role"]: row
        for row in outputs["product_port_relations"].to_pylist()
        if row["product_key"] == saddle["product_key"]
    }
    assert saddle["host_datum_count"] == 2
    assert saddle["gas_interface_count"] == 1
    assert relations["MAIN_A"]["family_interface_kind"] == "HOST_DATUM"
    assert relations["MAIN_B"]["family_interface_kind"] == "HOST_DATUM"
    assert relations["BRANCH"]["family_interface_kind"] == "GAS"
    assert {row["source_system_type"] for row in relations.values()} == {"GAS"}


def test_port_scoped_unresolved_item_propagates_exact_identity_and_action_effects() -> None:
    module = _module()
    inputs = _inputs()
    unresolved = inputs["unresolved_items"].to_pylist()[0]
    unresolved.update(
        {
            "unresolved_item_key": "unresolved:physical:b1",
            "subject_key": "port:b1",
            "issue_code": "UNRESOLVED_PHYSICAL_INTERFACE",
            "affected_field": "external_interface",
            "blocked_actions": ["external_physical_interface_resolution"],
            "authority_key": "authority:physical:b1",
        }
    )
    inputs["unresolved_items"] = pa.Table.from_pylist([unresolved])
    outputs = module.compile_tables(inputs, SOURCE_NAMESPACE)
    issue = next(
        row
        for row in outputs["input_issues"].to_pylist()
        if row["source_unresolved_item_key"] == "unresolved:physical:b1"
    )
    assert issue["source_product_key"] == "source:bend"
    assert issue["source_subject_kind"] is None
    assert issue["source_subject_key"] == "port:b1"
    assert issue["source_authority_key"] == "authority:physical:b1"
    assert issue["candidate_count"] is None
    assert issue["blocks_family_geometry"] is False
    assert json.loads(issue["blocked_actions_json"]) == ["external_physical_interface_resolution"]


def test_writer_serial_issues_use_exact_product_owner_and_keep_pending_actions() -> None:
    inputs = _inputs()
    inputs["unresolved_items"] = _table([
        _writer_issue(
            "issue:PENDING_PLANNED_PIPE_SECTION_GEOMETRY:source:pipe:planned_pipe_sections",
            "source:pipe", "source:pipe", "PENDING_PLANNED_PIPE_SECTION_GEOMETRY",
            "planned_pipe_sections",
            ["family_geometry", "interface_profile", "ifc_network_binding"],
        ),
        _writer_issue(
            "issue:PENDING_SERIAL_INTERFACE_CONSTRAINTS:event:reg:external_constraints",
            "source:bend", "event:reg", "PENDING_SERIAL_INTERFACE_CONSTRAINTS",
            "external_constraints", ["external_constraint_window_materialization",
                                     "external_physical_interface_resolution",
                                     "ifc_network_binding"],
        ),
    ])
    outputs = _module().compile_tables(inputs, SOURCE_NAMESPACE)
    products = {row["source_product_key"]: row
                for row in outputs["expected_products"].to_pylist()}
    issues = {row["issue_code"]: row for row in outputs["input_issues"].to_pylist()
              if row["source_unresolved_item_key"]}
    planned = issues["PENDING_PLANNED_PIPE_SECTION_GEOMETRY"]
    assert planned["source_product_key"] == "source:pipe"
    assert planned["product_key"] == products["source:pipe"]["product_key"]
    assert planned["source_subject_key"] == "source:pipe"
    assert planned["affected_field"] == "planned_pipe_sections"
    assert planned["status"] == "open" and planned["blocks_family_geometry"]
    assert json.loads(planned["blocked_actions_json"]) == [
        "family_geometry", "interface_profile", "ifc_network_binding"]
    assert products["source:pipe"]["readiness_status"] == "blocked_required_input"
    serial = issues["PENDING_SERIAL_INTERFACE_CONSTRAINTS"]
    assert serial["source_product_key"] == "source:bend"
    assert serial["product_key"] == products["source:bend"]["product_key"]
    assert serial["source_subject_key"] == "event:reg"
    assert serial["affected_field"] == "external_constraints"
    assert serial["status"] == "open" and not serial["blocks_family_geometry"]
    assert products["source:bend"]["readiness_status"] == "ready_for_family_authoring_input"


def test_writer_unresolved_issue_rejects_unknown_exact_product_owner() -> None:
    inputs = _inputs()
    inputs["unresolved_items"] = _table([
        _writer_issue("issue:unknown-owner", "source:absent", "source:pipe",
                      "PENDING_PLANNED_PIPE_SECTION_GEOMETRY", "planned_pipe_sections",
                      ["family_geometry"]),
    ])
    with pytest.raises(ValueError, match="exact Writer product owner"):
        _module().compile_tables(inputs, SOURCE_NAMESPACE)


def test_writer_product_origin_is_independent_of_geometry_input_frame() -> None:
    inputs = _inputs()
    geometry = inputs["geometry_inputs"].to_pylist()
    for axis in "xyz":
        geometry[0][f"origin_{axis}_m"] = None
        geometry[0][f"axis_{axis}"] = None
        geometry[0][f"ref_direction_{axis}"] = None
    inputs["geometry_inputs"] = _table(geometry)
    outputs = _module().compile_tables(inputs, SOURCE_NAMESPACE)
    bend = next(row for row in outputs["expected_products"].to_pylist()
                if row["source_product_key"] == "source:bend")
    assert [bend[f"origin_{axis}_m"] for axis in "xyz"] == [1.0, 2.0, 3.0]
    evidence = next(row for row in outputs["product_parameter_evidence"].to_pylist()
                    if row["product_key"] == bend["product_key"]
                    and row["parameter_key"] == "product_origin_m")
    assert evidence["authority_member"] == "product_origins"
    assert evidence["authority_key"] == "origin:source:bend"
    assert evidence["status"] == "known"
    assert not any(row["product_key"] == bend["product_key"]
                   and row["parameter_key"] == "product_frame"
                   for row in outputs["product_parameter_evidence"].to_pylist())


def test_lineage_and_source_denominator_use_exact_package_authorities() -> None:
    module = _module()
    inputs = _inputs()
    source_rows = inputs["source_dispositions"].to_pylist()
    source_rows.append(
        {
            "source_disposition_key": "disp:display-only",
            "source_entity_key": "entity:display-only",
            "source_business_id": "business:display-only",
            "source_layer": "display",
            "ingestion_disposition": "accepted",
            "modeling_disposition": "non_product",
            "source_entity_type": "TEXT",
            "reason_code": "display_duplicate_unique_identity_geometry",
        }
    )
    inputs["source_dispositions"] = pa.Table.from_pylist(source_rows)
    outputs = module.compile_tables(
        inputs,
        SOURCE_NAMESPACE,
        "data_package_m4",
        "data_package_product",
        "data_package_cad",
    )
    reconciled = outputs["source_disposition_reconciliation"].to_pylist()
    assert len(reconciled) == len(source_rows)
    display = next(row for row in reconciled if row["source_entity_key"] == "entity:display-only")
    assert display["input_disposition"] == "preserved_non_product_source"
    lineage = outputs["input_lineage"].to_pylist()
    assert {row["source_package_id"] for row in lineage} == {
        "data_package_m4",
        "data_package_product",
        "data_package_cad",
    }
    authorities = {row["authority"] for row in outputs["cad_source_denominator"].to_pylist()}
    assert authorities == {"data_package_m4/source_dispositions"}


def test_writer_inferred_riser_size_keeps_authority_and_open_physical_host():
    inputs=_inputs()
    products=inputs["products"].to_pylist();products[0]["product_kind"]="RISER";inputs["products"]=_table(products)
    authority="authorized_unique_unsnapped_endpoint_od_inference:event:riser"
    ports=inputs["ports"].to_pylist()
    for index,p in enumerate(ports[:2]):
        p.update(outer_diameter_m=.032,diameter_status="inferred",diameter_authority=authority,
                 port_role="BOTTOM" if index==0 else "TOP",connection_state="open")
    inputs["ports"]=_table(ports);inputs["connections"]=pa.table({})
    geometry=inputs["geometry_inputs"].to_pylist()
    geometry[0].update(outer_diameter_m=.032,diameter_status="inferred",diameter_authority=authority,
                       height_m=1.5,height_authority="approved:riser-trial-height")
    inputs["geometry_inputs"]=_table(geometry)
    inference=_writer_issue("inferred-riser-od", "source:bend", "entity:bend",
                            "RISER_OUTER_DIAMETER_INFERRED", "outer_diameter_m",
                            status="resolved_by_authorized_inference")
    inference["authority_key"]=authority
    inference["evidence_json"]=json.dumps({"outer_diameter_m":.032,
        "business_connection_accepted":True,"physical_connection_accepted":False,
        "host_source_entity_key":"entity:host","endpoint_key":"endpoint:host:start"})
    inputs["unresolved_items"]=_table([inference,
        _writer_issue("open-host", "source:bend", "source:bend", "UNRESOLVED_DEVICE_HOST",
                      "host_pairing", ["external_business_host_resolution"]),
        _writer_issue("open-interface", "source:bend", "port:b1", "UNRESOLVED_PHYSICAL_INTERFACE",
                      "external_interface", ["external_physical_interface_resolution"]),
    ])
    result=_module().compile_tables(inputs,SOURCE_NAMESPACE)
    riser=next(r for r in result["expected_products"].to_pylist() if r["category"]=="RISER")
    assert riser["diameter_min_m"]==.032 and riser["family_geometry_status"]=="pending_family_geometry"
    assert riser["external_action_status"]=="external_business_resolution_required"
    old=next(r for r in result["input_issues"].to_pylist() if r["source_unresolved_item_key"]=="inferred-riser-od")
    assert old["status"]=="resolved_by_authorized_inference" and not old["blocks_family_geometry"]
    assert old["issue_code"]=="RISER_OUTER_DIAMETER_INFERRED" and json.loads(old["blocked_actions_json"])==[]
    size=next(r for r in result["product_parameter_evidence"].to_pylist() if r["parameter_key"]=="outer_diameter_m")
    assert size["authority_type"]=="authorized_inference" and size["status"]=="inferred"
    assert size["authority_member"]=="unresolved_items" and size["authority_key"]=="inferred-riser-od"
    details=json.loads(size["details"])
    assert details["diameter_authority"]==authority
    assert json.loads(details["writer_evidence_json"])["physical_connection_accepted"] is False
    summary=next(r for r in result["category_summary"].to_pylist() if r["category"]=="RISER")
    assert summary["unresolved_record_count"]==2 and summary["missing_diameter_count"]==0


def test_device_host_issue_requires_only_external_business_resolution() -> None:
    module = _module()
    inputs = _inputs()
    unresolved = inputs["unresolved_items"].to_pylist()[0]
    unresolved.update(
        {
            "unresolved_item_key": "unresolved:host:bend",
            "subject_key": "source:bend",
            "issue_code": "UNRESOLVED_DEVICE_HOST",
            "affected_field": "host_pairing",
            "blocked_actions": ["external_business_host_resolution"],
        }
    )
    inputs["unresolved_items"] = pa.Table.from_pylist([unresolved])
    outputs = module.compile_tables(inputs, SOURCE_NAMESPACE)
    bend = next(
        row
        for row in outputs["expected_products"].to_pylist()
        if row["source_product_key"] == "source:bend"
    )
    issue = next(
        row
        for row in outputs["input_issues"].to_pylist()
        if row["source_unresolved_item_key"] == "unresolved:host:bend"
    )
    assert bend["family_geometry_status"] == "pending_family_geometry"
    assert bend["self_apply_input_status"] == "ready_for_self_geometry_and_apply_input"
    assert bend["external_action_status"] == "external_business_resolution_required"
    assert issue["blocks_family_geometry"] is False
    assert json.loads(issue["blocked_actions_json"]) == ["external_business_host_resolution"]


def test_anode_structure_is_approved_but_historical_ifc_child_class_is_not_ratified() -> None:
    module = _module()
    inputs = _inputs()

    def append(name: str, row: dict[str, object]) -> None:
        inputs[name] = pa.Table.from_pylist(inputs[name].to_pylist() + [row])

    append(
        "products",
        {
            "product_key": "source:anode",
            "product_kind": "ANODE",
            "source_disposition_key": "disp:anode",
            "assembly_parent_product_key": None,
            "origin_kind": "source",
            "name": "Anode",
            "geometry_strategy": "ASSEMBLY",
            "component_resource_key": None,
        },
    )
    append(
        "source_dispositions",
        {
            "source_disposition_key": "disp:anode",
            "source_entity_key": "entity:anode",
            "source_business_id": "business:anode",
            "source_layer": "anodes",
            "ingestion_disposition": "accepted",
            "modeling_disposition": "modeled",
            "source_entity_type": "INSERT",
        },
    )
    append(
        "product_instances",
        {
            "product_key": "source:anode",
            "source_entity_key": "entity:anode",
            "material": "mixed",
            "pressure_normalized": None,
        },
    )
    outputs = module.compile_tables(inputs, SOURCE_NAMESPACE)
    anode = next(
        row
        for row in outputs["expected_products"].to_pylist()
        if row["source_product_key"] == "source:anode"
    )
    components = [
        row
        for row in outputs["expected_components"].to_pylist()
        if row["parent_product_key"] == anode["product_key"]
    ]
    assert {row["semantic_role"] for row in components} == {
        "cast_body",
        "core",
        "exit_seal",
        "lead_jacket",
        "lead_conductor",
    }
    assert {row["ifc_class"] for row in components} == {"IfcDiscreteAccessory"}
    assert {row["ifc_class_status"] for row in components} == {"historical_unratified"}
    assert {row["evidence_status"] for row in components} == {
        "approved_design_required_source_component_absent"
    }


def test_distinct_instances_preserve_their_complete_geometry_inputs() -> None:
    module = _module()
    inputs = _inputs()

    products = inputs["products"].to_pylist()
    products.append(
        {**products[0], "product_key": "source:bend2", "source_disposition_key": "disp:bend2"}
    )
    inputs["products"] = pa.Table.from_pylist(products)

    ports = inputs["ports"].to_pylist()
    for row in ports[:2]:
        clone = dict(row)
        clone["port_key"] = row["port_key"].replace("port:b", "port:bend2:")
        clone["owner_product_key"] = "source:bend2"
        clone["direction_x"], clone["direction_y"] = -row["direction_y"], row["direction_x"]
        ports.append(clone)
    inputs["ports"] = pa.Table.from_pylist(ports)

    dispositions = inputs["source_dispositions"].to_pylist()
    dispositions.append(
        {
            **dispositions[0],
            "source_disposition_key": "disp:bend2",
            "source_entity_key": "entity:bend2",
            "source_business_id": "business:bend2",
        }
    )
    inputs["source_dispositions"] = pa.Table.from_pylist(dispositions)

    instances = inputs["product_instances"].to_pylist()
    instances.append(
        {
            **instances[0],
            "product_key": "source:bend2",
            "source_entity_key": "entity:bend2",
        }
    )
    inputs["product_instances"] = pa.Table.from_pylist(instances)

    properties = inputs["property_values"].to_pylist()
    properties.append(
        {
            **properties[0],
            "property_value_key": "property:bend2:model",
            "owner_key": "source:bend2",
        }
    )
    inputs["property_values"] = pa.Table.from_pylist(properties)

    geometry = inputs["geometry_inputs"].to_pylist()
    geometry.append(
        {
            **geometry[0],
            "product_key": "source:bend2",
            "origin_x_m": 101.0,
            "origin_y_m": 202.0,
        }
    )
    inputs["geometry_inputs"] = pa.Table.from_pylist(geometry)

    origins = inputs["product_origins"].to_pylist()
    origins.append({**origins[0], "origin_key": "origin:source:bend2",
                    "product_key": "source:bend2", "origin_x_m": 101.0,
                    "origin_y_m": 202.0})
    inputs["product_origins"] = pa.Table.from_pylist(origins)

    vertices = inputs["axis_vertices"].to_pylist()
    vertices.extend(
        {
            **row,
            "axis_vertex_key": row["axis_vertex_key"].replace("vertex:", "vertex:bend2:"),
            "product_key": "source:bend2",
            "x_m": row["x_m"] + 100.0,
            "y_m": row["y_m"] + 200.0,
        }
        for row in vertices[:2]
    )
    inputs["axis_vertices"] = pa.Table.from_pylist(vertices)

    combinations = module.compile_tables(inputs, SOURCE_NAMESPACE)[
        "product_combinations"
    ].to_pylist()
    selected = {
        row["source_product_key"]: row
        for row in combinations
        if row["source_product_key"] in {"source:bend", "source:bend2"}
    }
    first = json.loads(selected["source:bend"]["combination_json"])
    second = json.loads(selected["source:bend2"]["combination_json"])
    assert selected["source:bend"]["product_key"] != selected["source:bend2"]["product_key"]
    assert second["product_frame"]["origin_m"][:2] == [101.0, 202.0]
    assert second["path"][0][:2] == [first["path"][0][0] + 100.0, first["path"][0][1] + 200.0]
    assert (
        selected["source:bend"]["combination_json"]
        != (selected["source:bend2"]["combination_json"])
    )


def test_singleton_category_is_not_claimed_as_an_independent_holdout() -> None:
    module = _module()
    combinations = module.compile_tables(_inputs(), SOURCE_NAMESPACE)[
        "product_combinations"
    ].to_pylist()
    for row in combinations:
        roles = set(json.loads(row["selection_roles_json"]))
        if row["feasibility_status"] != "blocked_source_input":
            assert roles & {
                "representative_development",
                "representative_only_no_independent_holdout",
            }
        assert "execution_holdout_candidate" not in roles


def test_runtime_candidate_requires_fixture_domain_and_untrimmed_source_axis() -> None:
    module = _module()
    inputs = _inputs()
    ports = inputs["ports"].to_pylist()
    ports.append(
        {
            **ports[-1],
            "port_key": "port:p2",
            "sequence_ordinal": 2,
            "port_role": "END_B",
            "direction_x": -1.0,
        }
    )
    inputs["ports"] = pa.Table.from_pylist(ports)
    geometry = inputs["geometry_inputs"].to_pylist()
    geometry.append(
        {
            **geometry[0],
            "product_key": "source:pipe",
            "origin_x_m": 0.0,
            "origin_y_m": 0.0,
            "origin_z_m": 0.0,
            "outer_diameter_m": 0.1,
            "trim_start_m": 0.0,
            "trim_end_m": 0.0,
            "length_m": 2.0,
        }
    )
    inputs["geometry_inputs"] = pa.Table.from_pylist(geometry)
    vertices = inputs["axis_vertices"].to_pylist()
    vertices.extend(
        [
            {
                "axis_vertex_key": "vertex:pipe:1",
                "product_key": "source:pipe",
                "sequence_ordinal": 1,
                "x_m": 0.0,
                "y_m": 0.0,
                "z_m": 0.0,
            },
            {
                "axis_vertex_key": "vertex:pipe:2",
                "product_key": "source:pipe",
                "sequence_ordinal": 2,
                "x_m": 2.0,
                "y_m": 0.0,
                "z_m": 0.0,
            },
        ]
    )
    inputs["axis_vertices"] = pa.Table.from_pylist(vertices)

    combinations = module.compile_tables(inputs, SOURCE_NAMESPACE)[
        "product_combinations"
    ].to_pylist()
    pipe = next(row for row in combinations if row["source_product_key"] == "source:pipe")
    assert "runtime_input_candidate" in json.loads(pipe["selection_roles_json"])

    geometry[-1]["trim_end_m"] = 0.1
    inputs["geometry_inputs"] = pa.Table.from_pylist(geometry)
    combinations = module.compile_tables(inputs, SOURCE_NAMESPACE)[
        "product_combinations"
    ].to_pylist()
    pipe = next(row for row in combinations if row["source_product_key"] == "source:pipe")
    assert "runtime_input_candidate" not in json.loads(pipe["selection_roles_json"])
