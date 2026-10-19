"""Product-read contract boundaries; these tests do not parse or execute IFC."""

import pytest
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.domain.ifc_products_read import (
    IfcApplyProductMatch,
    IfcProductReadFacts,
    IfcProductsReadFacts,
    IfcProductsReadError,
    IfcProductsReadGroups,
    IfcProductsReadQuery,
    IfcProductsReadRuntimeRequest,
    IfcProductsReadRuntimeResult,
    IfcReadPlacement,
    IfcReadPropertyValue,
    IfcReadPropertySet,
    IfcReadLocalObject,
    IfcReadText,
)


GUID = "0" * 22


def _found(**groups):
    return IfcProductReadFacts.model_validate({
        "global_id": GUID,
        "status": "found",
        "match_count": 1,
        "basic": {
            "ifc_class": "IfcElementAssembly",
            "placement": {"status": "unsupported"},
            "aggregate_parent": {"status": "empty"},
            "direct_child_count": 2,
        },
        **groups,
    })


def test_applied_defaults_and_current_selection_are_closed_independent_views():
    adapter = TypeAdapter(IfcProductsReadQuery)
    applied = adapter.validate_python({"mode": "from_apply", "product_keys": ["p1"]})
    assert applied.view == "applied"
    with pytest.raises(ValidationError):
        adapter.validate_python({"mode": "from_apply", "product_keys": ["p1"], "groups": {}})
    current = adapter.validate_python({"mode": "from_apply", "view": "current", "product_keys": ["p1"], "groups": {"ports": {"mode": "names"}}})
    assert current.groups.ports.mode == "names"
    with pytest.raises(ValidationError):
        adapter.validate_python({"mode": "from_ifc", "global_ids": [GUID], "operation_id": "hidden"})


def test_selected_objects_are_nonempty_unique_and_bounded():
    for ids in ([], [GUID, GUID], [str(index).zfill(22) for index in range(33)], ["bad-guid"]):
        with pytest.raises(ValidationError):
            IfcProductsReadRuntimeRequest(global_ids=ids)
    for keys in ([], ["p", "p"], [str(index) for index in range(33)]):
        with pytest.raises(ValidationError):
            TypeAdapter(IfcProductsReadQuery).validate_python({"mode": "from_apply", "product_keys": keys})


def test_names_discovery_and_known_property_selection_preserve_source_and_set():
    groups = IfcProductsReadGroups.model_validate({"properties": {"mode": "values", "selectors": [
        {"source": "instance", "set_kind": "pset", "set_name": "Pset_Custom", "property_names": ["Pressure"]},
        {"source": "type", "set_kind": "qto", "set_name": "Qto_Custom", "property_names": ["Length"]},
    ]}})
    assert groups.properties.selectors[1].source == "type"
    assert IfcProductsReadGroups.model_validate({"properties": {"mode": "names"}}).properties.sources == ("instance", "type")
    for selection in (
        {"mode": "values", "selectors": []},
        {"mode": "names", "selectors": []},
        {"mode": "names", "sources": []},
        {"mode": "names", "sources": ["type", "type"]},
    ):
        with pytest.raises(ValidationError):
            IfcProductsReadGroups.model_validate({"properties": selection})


def test_material_and_port_discovery_return_actual_selectable_identity_kinds():
    selected = IfcProductsReadGroups.model_validate({
        "materials": {"mode": "values", "selectors": [{"source": "instance", "set_name": "WallLayers", "member_names": ["Concrete", "Insulation"]}]},
        "ports": {"mode": "values", "global_ids": [GUID]},
    })
    assert selected.materials.selectors[0].member_names == ("Concrete", "Insulation")
    for group, value in (
        ("materials", {"mode": "values", "member_step_ids": [19]}),
        ("materials", {"mode": "values", "selectors": [{"source": "instance", "set_name": "WallLayers", "member_names": ["Concrete", "Concrete"]}]}),
        ("materials", {"mode": "values", "names": ["steel"]}),
        ("ports", {"mode": "values", "global_ids": []}),
        ("ports", {"mode": "names", "path": "ConnectedTo[0]"}),
    ):
        with pytest.raises(ValidationError):
            IfcProductsReadGroups.model_validate({group: value})


def test_exact_selector_names_round_trip_separately_from_truncated_display():
    exact_set = "set_" + "s" * 2500
    exact_property = "property_" + "p" * 700
    exact_member = "material_" + "m" * 900
    discovered = IfcReadPropertySet.model_validate({
        "object": {"step_id": 9, "ifc_class": "IfcPropertySet"},
        "source": "instance", "set_kind": "pset", "selector_name": exact_set,
        "name": {"value": exact_set[:2048], "truncated": True, "original_length": len(exact_set)},
        "properties": [{"object": {"step_id": 10, "ifc_class": "IfcPropertySingleValue"}, "selector_name": exact_property}],
    })
    selection = IfcProductsReadGroups.model_validate({"properties": {"mode": "values", "selectors": [{
        "source": discovered.source, "set_kind": discovered.set_kind,
        "set_name": discovered.selector_name, "property_names": [discovered.properties[0].selector_name],
    }]}, "materials": {"mode": "values", "selectors": [{"source": "type", "set_name": exact_set, "member_names": [exact_member]}]}})
    assert selection.properties.selectors[0].set_name == exact_set
    assert selection.properties.selectors[0].property_names == (exact_property,)
    assert selection.materials.selectors[0].member_names == (exact_member,)
    assert discovered.object.model_dump() == {"step_id": 9, "ifc_class": "IfcPropertySet"}
    with pytest.raises(ValidationError):
        IfcReadLocalObject(step_id=9, ifc_class="IfcPropertySet", selector_name=exact_set)
    with pytest.raises(ValidationError):
        IfcProductsReadGroups.model_validate({"properties": {"mode": "values", "selectors": [{
            "source": "instance", "set_kind": "pset", "set_name": discovered.name.model_dump(), "property_names": [exact_property],
        }]}})


def test_legal_long_product_keys_are_preserved_in_request_and_result():
    key = "product_" + "p" * 3000
    query = TypeAdapter(IfcProductsReadQuery).validate_python({"mode": "from_apply", "product_keys": [key]})
    match = IfcApplyProductMatch(status="not_committed", product_key=key, report_match_count=1)
    facts = IfcProductsReadFacts(view="applied", items=({"product_key": key, "apply_match": match},))
    assert query.product_keys == (key,)
    assert facts.items[0].product_key == facts.items[0].apply_match.product_key == key


def test_unrequested_empty_and_unsupported_groups_remain_distinct():
    fact = _found(materials={"status": "empty"}, ports={"status": "unsupported"})
    assert fact.structure is None
    assert fact.materials.status == "empty"
    assert fact.ports.status == "unsupported"
    assert fact.basic.placement.translation_m is None
    assert fact.basic.direct_child_count == 2


def test_failed_location_and_unresolved_placement_cannot_claim_concrete_facts():
    for change in ({"status": "not_found", "match_count": 0}, {"status": "ambiguous", "match_count": 2}):
        with pytest.raises(ValidationError):
            IfcProductReadFacts.model_validate({**_found().model_dump(), **change})
    with pytest.raises(ValidationError):
        IfcReadPlacement(status="found", translation_m=(0, 0, 0))
    with pytest.raises(ValidationError):
        IfcReadPlacement(status="unsupported", translation_m=(0, 0, 0))
    with pytest.raises(ValidationError):
        IfcReadPlacement(status="found", translation_m=(float("nan"), 0, 0), rotation=((1, 0, 0), (0, 1, 0), (0, 0, 1)))


def test_property_values_preserve_primitives_lists_units_and_unsupported_state():
    value = IfcReadPropertyValue(status="found", value_type="IfcPressureMeasure", scalar=32.5, unit={"ifc_class": "IfcSIUnit"})
    assert value.scalar == 32.5
    assert IfcReadPropertyValue(status="found", values=(False, 2, "tag")).values == (False, 2, "tag")
    assert IfcReadPropertyValue(status="unsupported", value_type="IfcComplexProperty").scalar is None
    for payload in (
        {"status": "found", "scalar": {"invented": "json"}},
        {"status": "found", "values": list(range(129))},
        {"status": "unsupported", "scalar": 3},
        {"status": "found", "scalar": 1, "values": [1]},
        {"status": "found", "scalar": "x" * 2049},
        {"status": "found", "scalar": "short", "truncated": True},
    ):
        with pytest.raises(ValidationError):
            IfcReadPropertyValue.model_validate(payload)


def test_text_truncation_is_explicit_and_consistent():
    assert IfcReadText(value="abc", original_length=7, truncated=True).truncated
    for payload in ({"value": "abc", "original_length": 7}, {"value": "abc", "original_length": 3, "truncated": True}):
        with pytest.raises(ValidationError):
            IfcReadText.model_validate(payload)


def test_collections_and_serialized_response_have_independent_bounds():
    with pytest.raises(ValidationError):
        _found(properties={"status": "found", "types": [{"global_id": GUID, "ifc_class": "IfcTypeProduct"}] * 129})
    long_text = {"value": "字" * 2048, "original_length": 2048}
    many_sets = [{"object": {"step_id": index + 1, "ifc_class": "IfcPropertySet"}, "source": "instance", "set_kind": "pset", "name": long_text} for index in range(45)]
    fact = _found(properties={"status": "found", "sets": many_sets})
    for construct in (
        lambda: IfcProductsReadRuntimeResult(products=(fact,)),
        lambda: IfcProductsReadFacts(view="current", items=({"global_id": GUID,
            "apply_match": IfcApplyProductMatch(status="not_checked"), "current": fact},)),
    ):
        with pytest.raises(IfcProductsReadError, match="narrow the selected products") as caught:
            construct()
        assert caught.value.code == "IFC_PRODUCTS_READ_LIMIT_EXCEEDED"


def test_applied_results_cannot_embed_current_ifc_facts():
    with pytest.raises(ValidationError, match="applied view"):
        IfcProductsReadFacts(view="applied", items=({"product_key": "p1", "apply_match": IfcApplyProductMatch(status="not_checked"), "current": _found()},))


def test_nested_contract_schema_has_no_open_objects_or_unbounded_arrays():
    schemas = (IfcProductsReadRuntimeRequest.model_json_schema(), IfcProductsReadRuntimeResult.model_json_schema())

    def check(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            if value.get("type") == "array":
                assert "maxItems" in value
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    for schema in schemas:
        check(schema)
