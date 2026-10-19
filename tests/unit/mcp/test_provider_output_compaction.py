from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from enum import Enum
from itertools import product
import json
from typing import Literal
from unittest.mock import Mock

from fastmcp.utilities.json_schema import dereference_refs
from fastmcp import Client
from jsonschema import Draft202012Validator
from mcp.types import ListToolsResult
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from referencing import Registry, Resource
import pytest

from CAD2BIM_backend.domain.cad_measurement import CadToleranceAnalysisResult
from CAD2BIM_backend.domain.data_query import CadQueryXdataResult
from CAD2BIM_mcp.direct.ifc.geometry.authoring_contracts import IfcGeometryResourceWriteResult
from CAD2BIM_mcp.direct.ifc.geometry.contracts import IfcGeometryPreviewOutcomeResult, IfcGeometryResourceInfoResult
from CAD2BIM_mcp.direct.ifc.product_family.contracts import IfcProductFamilyInfoResult
from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import IfcProductRealizationPreviewResult
from CAD2BIM_mcp.direct.code.contracts import DirectCodeReadResponse
from CAD2BIM_mcp.direct.operation.contracts import OperationGetResult, OperationListResult, IfcProductsRecoveryListResult
from CAD2BIM_mcp.direct.ifc.products.contracts import IfcProductsApplyResult, IfcProductsEditResult
from CAD2BIM_mcp.direct.provider_schema import (
    _compact_closed_unions,
    compact_provider_input_schema,
    provider_output_schema,
    provider_reference_schema,
    share_provider_reference_types,
    share_provider_input_nodes,
)
from CAD2BIM_mcp.direct.tool_support import provider_input_schema
from CAD2BIM_mcp.direct.references import REFERENCE_TOOL_NAMES, reference_schema_projection
from CAD2BIM_mcp.tool_docs.common import COMMON_TYPES_RESOURCE_URI


async def _read_contract(server, uri):
    """Read the actual registered document in-process, without starting a transport."""
    return json.loads((await server.read_resource(uri)).contents[0].content)


async def _read_shared_contracts(server):
    from CAD2BIM_mcp.tool_docs.common import COMMON_PREVIEW_TYPES_RESOURCE_URI
    return {uri: await _read_contract(server, uri)
            for uri in (COMMON_TYPES_RESOURCE_URI, COMMON_PREVIEW_TYPES_RESOURCE_URI)}


def _contract_registry(documents):
    """Only explicitly read resources are resolvable; no network retrieval."""
    return Registry().with_resources((uri, Resource.from_contents(document))
                                     for uri, document in documents.items())


def _restore_shared_schema(schema, documents):
    """Restore local definitions for shape assertions; validation uses the URI registry."""
    pending, imported = set(), {}
    def reference(value, owner):
        uri, _, fragment = value.partition("#")
        uri = uri or owner
        if uri not in documents or not fragment.startswith("/$defs/"):
            return value
        name = fragment.removeprefix("/$defs/").replace("~1", "/").replace("~0", "~")
        pending.add((uri, name))
        return "#/$defs/" + name.replace("~", "~0").replace("/", "~1")
    def walk(node, owner=None):
        if not isinstance(node, dict):
            return deepcopy(node)
        result = deepcopy(node)
        if "$ref" in result:
            result["$ref"] = reference(result["$ref"], owner)
        if "discriminator" in result and "mapping" in result["discriminator"]:
            result["discriminator"]["mapping"] = {k: reference(v, owner) for k, v in result["discriminator"]["mapping"].items()}
        for key in ("properties", "$defs", "patternProperties", "dependentSchemas"):
            if key in result:
                result[key] = {k: walk(v, owner) for k, v in result[key].items()}
        for key in ("items", "additionalProperties", "propertyNames", "contains", "not", "if", "then", "else"):
            if isinstance(result.get(key), dict):
                result[key] = walk(result[key], owner)
        for key in ("oneOf", "anyOf", "allOf", "prefixItems"):
            if key in result:
                result[key] = [walk(v, owner) for v in result[key]]
        return result
    restored = walk(schema)
    while pending:
        uri, name = pending.pop()
        if name not in imported:
            imported[name] = None
            imported[name] = walk(documents[uri]["$defs"][name], uri)
    if imported:
        restored.setdefault("$defs", {}).update(imported)
    return restored


def test_shared_reference_types_preserve_recursive_graph_discriminators_and_literal_data():
    literal = {"$ref": "https://invalid.example/instance-data", "type": "business-value"}
    node = {"type": "object", "additionalProperties": False,
            "properties": {"kind": {"const": "node", "type": "string"},
                           "next": {"anyOf": [{"$ref": "#/$defs/Node"}, {"type": "null"}]},
                           "$ref": {"type": "string"}}, "required": ["kind", "next"],
            "examples": [literal], "default": literal}
    original = {"oneOf": [{"$ref": "#/$defs/Node"}], "$defs": {"Node": node},
                "discriminator": {"propertyName": "kind", "mapping": {"node": "#/$defs/Node"}}}
    before = deepcopy(original)
    projected, common = share_provider_reference_types([original, deepcopy(original)], common_uri=COMMON_TYPES_RESOURCE_URI)
    assert original == before
    assert projected[0]["discriminator"]["mapping"]["node"] == COMMON_TYPES_RESOURCE_URI + "#/$defs/Node"
    assert common[COMMON_TYPES_RESOURCE_URI]["$defs"]["Node"] == node
    assert _restore_shared_schema(projected[0], common) == original
    validator = Draft202012Validator(projected[0], registry=_contract_registry(common))
    assert validator.is_valid({"kind": "node", "next": {"kind": "node", "next": None}})
    assert not validator.is_valid({"kind": "node", "next": {"kind": "wrong", "next": None}})


def test_shared_reference_types_separate_generation_modes_and_business_types():
    class Amount(BaseModel):
        value: Decimal
    class Envelope(BaseModel):
        amount: Amount
    validation = provider_reference_schema(Envelope, mode="validation")
    serialization = provider_reference_schema(Envelope, mode="serialization")
    assert validation != serialization
    projected, common = share_provider_reference_types(
        [validation, validation, serialization, serialization], common_uri=COMMON_TYPES_RESOURCE_URI,
    )
    assert len(common[COMMON_TYPES_RESOURCE_URI]["$defs"]) == 2
    assert projected[0] == projected[1] and projected[2] == projected[3]
    assert projected[0] != projected[2]
    for index, original in enumerate([validation, validation, serialization, serialization]):
        old, new = Draft202012Validator(original), Draft202012Validator(projected[index], registry=_contract_registry(common))
        for value in ({"amount": {"value": 1}}, {"amount": {"value": "1.25"}}, {"amount": {"value": []}}):
            assert old.is_valid(value) == new.is_valid(value)
    one = {"$ref": "#/$defs/Length", "$defs": {"Length": {"type": "number"}}}
    other = {"$ref": "#/$defs/Angle", "$defs": {"Angle": {"type": "number"}}}
    result, definitions = share_provider_reference_types([one, other], common_uri=COMMON_TYPES_RESOURCE_URI)
    assert result == [one, other] and not definitions[COMMON_TYPES_RESOURCE_URI]["$defs"]


def test_shared_reference_types_reject_unknown_dependencies_without_retrieval():
    from jsonschema.exceptions import _WrappedReferencingError
    schema = {"$ref": "https://invalid.example/schema"}
    with pytest.raises(ValueError, match="local definition"):
        share_provider_reference_types([schema], common_uri=COMMON_TYPES_RESOURCE_URI)
    with pytest.raises(_WrappedReferencingError):
        Draft202012Validator(schema, registry=Registry()).validate({})


@pytest.mark.asyncio
async def test_registered_shared_contracts_preserve_internal_reference_document_authority():
    from CAD2BIM_mcp.direct.ifc.product_family.write_contracts import (
        product_family_definition_reference_document, product_family_definition_resource_document,
    )
    from CAD2BIM_mcp.tool_docs.ifc_product_family import PRODUCT_FAMILY_DEFINITION_DOCUMENT_EXAMPLE
    server = create_direct_mcp(Mock())
    common = await _read_shared_contracts(server)
    document = await _read_contract(server, "cad2bim://contracts/ifc_product_family_write/definition_json")
    from CAD2BIM_mcp.tool_docs.common import COMMON_PREVIEW_TYPES_RESOURCE_URI
    primary = common[COMMON_TYPES_RESOURCE_URI]
    preview = common[COMMON_PREVIEW_TYPES_RESOURCE_URI]
    assert set(primary["$defs"]).isdisjoint(preview["$defs"])
    assert COMMON_PREVIEW_TYPES_RESOURCE_URI not in json.dumps(primary["$defs"])
    assert COMMON_PREVIEW_TYPES_RESOURCE_URI not in json.dumps(document)
    assert "JsonValue" in primary["$defs"]
    assert "PreviewCaseReuse" in preview["$defs"]
    public = document["create_definition_input_schema"]
    tool = await server.get_tool("ifc_product_family_write")
    original, maximum = product_family_definition_reference_document()
    assert tool._reference_documents["definition_json"] == (original, maximum)
    assert tool._reference_documents["source.definition_json"] == product_family_definition_reference_document(create=True)
    assert COMMON_TYPES_RESOURCE_URI not in json.dumps(original)
    for key, schema in product_family_definition_resource_document().items():
        projected = reference_schema_projection(schema)[0]
        assert _restore_shared_schema(document[key], common) == projected
    assert not hasattr(server, "_provider_contract_documents")
    validator = Draft202012Validator(public, registry=_contract_registry(common))
    complete_input = {"action":"set", "value":PRODUCT_FAMILY_DEFINITION_DOCUMENT_EXAMPLE}
    validator.validate(complete_input)
    assert not validator.is_valid({**complete_input, "unknown": True})
from CAD2BIM_mcp.direct.server import create_direct_mcp
from CAD2BIM_mcp.direct.transform.contracts import DataTransformWriteToolResult


_COMPACT_RESULTS = {
    "operation_get": OperationGetResult,
    "operation_list": OperationListResult | IfcProductsRecoveryListResult,
    "ifc_products_apply": IfcProductsApplyResult,
    "ifc_products_edit": IfcProductsEditResult,
    "ifc_product_realization_preview": IfcProductRealizationPreviewResult,
    "cad_query_xdata": CadQueryXdataResult,
    "cad_analyze_tolerance_candidates": CadToleranceAnalysisResult,
    "data_derive": DataTransformWriteToolResult,
    "ifc_geometry_resource_info": IfcGeometryResourceInfoResult,
    "ifc_geometry_preview": IfcGeometryPreviewOutcomeResult,
    "ifc_geometry_resource_write": IfcGeometryResourceWriteResult,
    "ifc_product_family_info": IfcProductFamilyInfoResult,
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _expand_shared_input(schema):
    """Independent local-reference oracle; annotation values remain literal data."""
    definitions = schema.get("$defs", {})

    def expand(node, active=()):
        if not isinstance(node, dict):
            return deepcopy(node)
        if "$ref" in node:
            assert set(node) == {"$ref"}
            ref = node["$ref"]
            assert ref.startswith("#/$defs/") and ref not in active
            return expand(definitions[ref.removeprefix("#/$defs/")], (*active, ref))
        result = deepcopy(node)
        for key in ("properties", "patternProperties"):
            if key in node:
                result[key] = {name: expand(child, active) for name, child in node[key].items()}
        for key in ("items", "additionalProperties", "propertyNames"):
            if isinstance(node.get(key), dict):
                result[key] = expand(node[key], active)
        for key in ("anyOf", "oneOf", "prefixItems"):
            if key in node:
                result[key] = [expand(child, active) for child in node[key]]
        return result

    return expand({key: value for key, value in schema.items() if key != "$defs"})


@pytest.mark.parametrize("union", ["oneOf", "anyOf"])
def test_shared_inputs_preserve_complete_nodes_branch_multiplicity_and_literal_data(union):
    literal = {"$ref": "business-value", "description": {"type": "literal"}}
    leaf = {"type": "string", "description": "完整字段说明" * 35, "minLength": 2}
    branch = {"type": "object", "additionalProperties": False,
              "properties": {name: deepcopy(leaf) for name in ("description", "type", "$ref")},
              "required": ["description"], "default": literal,
              "examples": [literal], "enum": [literal], "const": literal}
    source = {union: [deepcopy(branch), deepcopy(branch),
                      {**deepcopy(branch), "description": "different"}]}
    before = deepcopy(source)
    result = share_provider_input_nodes(source)
    assert source == before
    assert _expand_shared_input(result) == source
    assert len(_canonical(result).encode()) < len(_canonical(source).encode())
    assert share_provider_input_nodes(source) == result == share_provider_input_nodes(result)
    assert any("#/$defs/" in _canonical(node) for node in result["$defs"].values())
    Draft202012Validator.check_schema(result)


@pytest.mark.parametrize("difference", [
    {"description": "different"}, {"default": 4}, {"required": []},
    {"additionalProperties": True}, {"enum": [{"value": 4}]},
])
def test_shared_inputs_keep_unequal_constraints(difference):
    branch = {"type": "object", "description": "long repeated contract " * 30,
              "properties": {"value": {"type": "integer", "minimum": 0}},
              "required": ["value"], "additionalProperties": False, "default": {"value": 3}}
    source = {"anyOf": [branch, deepcopy(branch), {**deepcopy(branch), **difference}]}
    result = share_provider_input_nodes(source)
    assert result["anyOf"][0] == result["anyOf"][1] != result["anyOf"][2]
    assert _expand_shared_input(result) == source
    for value in ({"value": 3}, {"value": -1}, {}, {"value": 3, "extra": True}):
        assert Draft202012Validator(source).is_valid(value) == Draft202012Validator(result).is_valid(value)


@pytest.mark.parametrize("schema", [
    {"properties": {"a": {"type": "string"}, "b": {"type": "string"}}},
    {"$defs": {"Node": {"$ref": "#/$defs/Node"}}, "$ref": "#/$defs/Node"},
    {"$ref": "https://example.invalid/schema"},
    {"$id": "urn:scoped", "properties": {}},
    {"$recursiveAnchor": True, "type": "object"},
    {"allOf": [{"type": "object"}]},
    {"items": [{"type": "string"}]},
])
def test_shared_inputs_preserve_nonprofitable_or_unsupported_documents(schema):
    before = deepcopy(schema)
    assert share_provider_input_nodes(schema) == schema == before


def _without_titles(value):
    if isinstance(value, list):
        return [_without_titles(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: _without_titles(item) for key, item in value.items() if key != "title"}


def _literal_expansion(schema):
    """Independent parity oracle: enumerate each union's finite required tags.

    This compares every other Schema keyword exactly, including descriptions,
    defaults, bounds, closedness, arrays and optional fields. It does not infer
    equivalence from a shorter string or the compactor's own merge decisions.
    """
    if isinstance(schema, list):
        return [_literal_expansion(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {key: _literal_expansion(value) for key, value in schema.items()}
    for union in ("anyOf", "oneOf"):
        if union not in result:
            continue
        expanded = []
        for branch in result[union]:
            properties = branch.get("properties", {}) if isinstance(branch, dict) else {}
            keys = [key for key, value in properties.items() if (
                key in branch.get("required", ())
                and value.get("type") == "string" and "enum" in value
            )]
            alternatives = [properties[key]["enum"] for key in keys]
            for combination in product(*alternatives):
                item = deepcopy(branch)
                for key, literal in zip(keys, combination, strict=True):
                    item["properties"][key].pop("enum")
                    item["properties"][key]["const"] = literal
                expanded.append(item)
        result[union] = sorted(expanded, key=_canonical)
    return result


@pytest.mark.parametrize("result_type", _COMPACT_RESULTS.values())
def test_output_compaction_preserves_all_schema_assertions_and_reference_paths(result_type):
    original = TypeAdapter(result_type).json_schema(mode="serialization")
    original["type"] = "object"
    original, original_paths = reference_schema_projection(original)
    compact, compact_paths = reference_schema_projection(provider_output_schema(result_type, compact=True))
    expected = dereference_refs(original)
    assert original_paths == compact_paths
    assert _literal_expansion(_without_titles(expected)) == _literal_expansion(_without_titles(compact))
    Draft202012Validator.check_schema(compact)
    assert len(_canonical(compact)) <= len(_canonical(expected))


@pytest.mark.parametrize("result_type", [IfcGeometryPreviewOutcomeResult, IfcProductRealizationPreviewResult])
def test_static_result_definitions_preserve_expanded_constraints_and_reference_paths(result_type):
    document, document_paths = reference_schema_projection(
        provider_output_schema(result_type, compact=True, preserve_references=True)
    )
    expanded, expanded_paths = reference_schema_projection(
        provider_output_schema(result_type, compact=True)
    )
    assert document.get("$defs")
    assert document_paths == expanded_paths
    Draft202012Validator.check_schema(document)
    assert _literal_expansion(dereference_refs(document)) == _literal_expansion(expanded)


def _provider_schema_nodes(node):
    if not isinstance(node, dict):
        return
    yield node
    for key in ("properties", "$defs", "patternProperties"):
        for child in node.get(key, {}).values():
            yield from _provider_schema_nodes(child)
    for key in ("items", "additionalProperties", "propertyNames"):
        yield from _provider_schema_nodes(node.get(key))
    for key in ("oneOf", "anyOf", "allOf", "prefixItems"):
        for child in node.get(key, ()):
            yield from _provider_schema_nodes(child)


@pytest.mark.asyncio
async def test_compaction_survives_final_fastmcp_middleware_without_input_changes():
    server = create_direct_mcp(Mock())
    common = await _read_shared_contracts(server)
    final = {tool.name: tool for tool in await server.list_tools()}
    for name, result_type in _COMPACT_RESULTS.items():
        registered = await server.get_tool(name)
        multimodal = name in {"ifc_geometry_preview", "ifc_product_realization_preview"}
        expected, paths = reference_schema_projection(provider_output_schema(
            result_type, compact=True, preserve_references=multimodal,
        ))
        if multimodal:
            assert registered.output_schema is None and final[name].output_schema is None
            document = await _read_contract(server, f"cad2bim://contracts/{name}/result")
            assert "$defs" not in document and "properties" not in document
            assert document["shared_semantics"] == "cad2bim://contracts/common/preview#preview_results"
            assert "cases" in document
            assert "optional field guidance" in final[name].description
        else:
            assert registered.output_schema == expected == final[name].output_schema
            assert '"$ref"' not in _canonical(expected)
            assert '"$defs"' not in _canonical(expected)
        if hasattr(registered, "_reference_output_paths"):
            assert registered._reference_output_paths == paths
        assert dereference_refs(registered.parameters) == _expand_shared_input(final[name].parameters)
        assert all(node.get("type") != "unknown" for node in _provider_schema_nodes(expected))
    # Output-only types no longer leak into the shared input/reference catalogue.
    shared_names = {key for doc in common.values() for key in doc["$defs"]}
    assert not {"PreviewCaseBatchResult", "PreviewCaseResultCard", "IfcPreviewPanelResult"} & shared_names


@pytest.mark.parametrize("compact_output", [False, True])
def test_automatic_titles_are_omitted_but_explicit_metadata_and_instance_data_survive(compact_output):
    class Kind(str, Enum):
        FIRST = "first"
        SECOND = "second"

    class Explicit(BaseModel):
        model_config = ConfigDict(extra="forbid", title="Public response")
        kind: Kind = Field(title="Business kind", description="Copy the exact kind")
        title: str
        fact: Literal["title"] = "title"
        metadata: dict[str, str] = Field(
            default={"title": "keep default data"},
            examples=[{"title": "keep example data"}],
            json_schema_extra={"const": {"title": "keep const data"},
                               "enum": [{"title": "keep enum data"}]},
        )

    class Automatic(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: int
        kind: Kind

    compact = provider_output_schema(Explicit, compact=compact_output)
    assert compact["title"] == "Public response"
    assert compact["properties"]["kind"]["title"] == "Business kind"
    assert compact["properties"]["kind"]["description"] == "Copy the exact kind"
    assert "title" in compact["properties"]
    assert compact["properties"]["fact"]["const"] == "title"
    assert compact["properties"]["metadata"]["default"] == {"title": "keep default data"}
    assert compact["properties"]["metadata"]["examples"] == [{"title": "keep example data"}]
    assert compact["properties"]["metadata"]["const"] == {"title": "keep const data"}
    assert compact["properties"]["metadata"]["enum"] == [{"title": "keep enum data"}]
    automatic = provider_output_schema(Automatic, compact=compact_output)
    assert "title" not in automatic
    assert "title" not in automatic["properties"]["value"]
    enum_schema = (automatic["properties"]["kind"] if compact_output else automatic["$defs"]["Kind"])
    assert enum_schema["title"] == "Kind"


def _assert_only_schema_titles_removed(before, after):
    """Compare schema positions exactly; instance-valued keywords stay opaque."""
    assert set(before) - set(after) <= {"title"}
    assert not set(after) - set(before)
    removed = int("title" in before and "title" not in after)
    for key, value in after.items():
        original = before[key]
        if key in {"properties", "$defs", "patternProperties"}:
            assert original.keys() == value.keys()
            removed += sum(_assert_only_schema_titles_removed(original[name], child)
                           for name, child in value.items())
        elif key in {"items", "additionalProperties", "propertyNames"} and isinstance(value, dict):
            removed += _assert_only_schema_titles_removed(original, value)
        elif key in {"anyOf", "oneOf", "allOf", "prefixItems"}:
            assert len(original) == len(value)
            removed += sum(_assert_only_schema_titles_removed(left, right)
                           for left, right in zip(original, value, strict=True))
        else:
            assert original == value, key
    return removed


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_reference_generation_preserves_mode_root_references_tuples_and_dto(mode):
    class Reference(BaseModel):
        amount: Decimal
        pair: tuple[int, str]
        metadata: dict[str, str] = {"title": "literal default"}

    dto = list[Reference]
    original = TypeAdapter(dto).json_schema(mode=mode)
    saved = deepcopy(original)
    result = provider_reference_schema(dto, mode=mode)
    assert result["type"] == "array"
    assert result["items"] == {"$ref": "#/$defs/Reference"}
    assert "prefixItems" in result["$defs"]["Reference"]["properties"]["pair"]
    assert _assert_only_schema_titles_removed(original, result) > 0
    assert provider_reference_schema(dto, mode=mode) == result
    assert TypeAdapter(dto).json_schema(mode=mode) == saved
    assert provider_reference_schema(dto, mode="validation") != provider_reference_schema(dto, mode="serialization")
    Draft202012Validator.check_schema(result)


@pytest.mark.asyncio
async def test_noncompact_output_removes_only_annotations_through_registered_projection():
    original = TypeAdapter(DirectCodeReadResponse).json_schema(mode="serialization")
    original["type"] = "object"
    projected = provider_output_schema(DirectCodeReadResponse)
    assert projected.get("$defs") and projected.get("anyOf")
    assert _assert_only_schema_titles_removed(original, projected) > 0
    saved = deepcopy(projected)
    expected, paths = reference_schema_projection(projected)
    assert projected == saved
    assert reference_schema_projection(expected) == (expected, paths)
    assert provider_output_schema(DirectCodeReadResponse) == projected
    server = create_direct_mcp(Mock())
    registered = await server.get_tool("code_read")
    assert registered.output_schema == expected
    async with Client(server) as client:
        final = {tool.name: tool for tool in await client.list_tools()}
    assert final["code_read"].outputSchema == dereference_refs(expected)


@pytest.mark.parametrize("union", ["anyOf", "oneOf"])
def test_input_factoring_preserves_required_field_matching_and_overlapping_branches(union):
    def branch(minimum, maximum):
        return {
            "type": "object", "additionalProperties": False,
            "properties": {"value": {"type": "integer", "minimum": minimum, "maximum": maximum},
                           "scope": {"type": "string", "const": "same"}},
            "required": ["value", "scope"],
        }

    original = {union: [branch(1, 3), branch(2, 4), branch(2, 4)]}
    saved = deepcopy(original)
    projected = compact_provider_input_schema(original)
    before, after = Draft202012Validator(original), Draft202012Validator(projected)
    assert original == saved
    assert compact_provider_input_schema(projected) == projected
    assert len(_canonical(projected)) < len(_canonical(original))
    for value in (None, True, "2", 0, 1, 2, 3, 4, 5):
        for payload in ({"value": value, "scope": "same"}, {"value": value},
                        {"value": value, "scope": "same", "extra": 1}):
            assert before.is_valid(payload) == after.is_valid(payload)
    assert before.is_valid({"scope": "same"}) == after.is_valid({"scope": "same"}) is False
    # The repeated second branch still contributes its original matching count.
    assert after.is_valid({"value": 4, "scope": "same"}) is (union == "anyOf")


@pytest.mark.parametrize("difference", ["optional", "second_field", "description", "default", "required", "reference_annotation", "discriminator"])
def test_input_factoring_keeps_non_equivalent_or_unsupported_branches(difference):
    first = _tagged("first")
    second = _tagged("second")
    if difference == "optional":
        first["required"].remove("tag")
        second["required"].remove("tag")
    elif difference == "second_field":
        second["properties"]["other"]["const"] = "different"
    elif difference == "description":
        second["description"] = "Only the second branch permits this mode"
    elif difference == "default":
        second["properties"]["value"]["default"] = 2
    elif difference == "required":
        second["required"].remove("value")
    elif difference == "reference_annotation":
        second["properties"]["value"]["x-reference-kind"] = "package"
    else:
        first["discriminator"] = second["discriminator"] = {"propertyName": "tag"}
    schema = {"oneOf": [first, second]}
    assert compact_provider_input_schema(schema) == schema


def test_input_generator_removes_only_proven_generated_titles_and_preserves_data():
    class Authored(BaseModel):
        model_config = ConfigDict(extra="forbid", title="Exact authored contract")
        title: str = Field(title="Business title", description="A business title")
        metadata: dict[str, str] = Field(default={"title": "literal"})

    class Generated(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: int

    plain = provider_input_schema(Authored)
    generated = provider_input_schema(Generated, omit_generated_titles=True)
    selected = provider_input_schema(Authored, omit_generated_titles=True)
    assert selected["title"] == plain["title"] == "Exact authored contract"
    assert selected["properties"]["title"] == plain["properties"]["title"]
    assert selected["properties"]["metadata"]["default"] == {"title": "literal"}
    assert "title" not in generated and "title" not in generated["properties"]["value"]
    assert compact_provider_input_schema(selected) == selected


def test_input_factoring_preserves_reference_paths_and_literal_title_data():
    first, second = _tagged("first"), _tagged("second")
    for branch in (first, second):
        branch["properties"]["title"] = {"type": "string", "title": "Authored label"}
        branch["properties"]["resource_alias"] = {"type": "string", "pattern": r"^cur\..+$"}
        branch["examples"] = [{"title": "business", "enum": [{"title": "literal"}]}]
    original = {"$defs": {"first": first, "second": second},
                "anyOf": [{"$ref": "#/$defs/first"}, {"$ref": "#/$defs/second"}]}
    projected = compact_provider_input_schema(original)
    assert reference_schema_projection(original)[1] == reference_schema_projection(projected)[1]
    common = projected["anyOf"][0]
    assert common["properties"]["title"] == first["properties"]["title"]
    assert common["examples"] == first["examples"]
    assert compact_provider_input_schema(projected) == projected


def test_input_oneof_does_not_hide_multiple_matches_from_an_overlapping_outside_branch():
    first = {"type": "object", "additionalProperties": False,
             "properties": {"value": {"type": "integer", "minimum": 1}, "other": {"type": "integer"}},
             "required": ["value", "other"]}
    second = deepcopy(first)
    second["properties"]["value"]["minimum"] = 2
    outside = deepcopy(first)
    outside["properties"]["other"]["minimum"] = 0
    outside["description"] = "A distinct overlapping branch"
    schema = {"oneOf": [first, second, outside]}
    projected = compact_provider_input_schema(schema)
    assert projected == schema
    assert not Draft202012Validator(projected).is_valid({"value": 2, "other": 1})


@pytest.mark.parametrize("schema", [
    {"$defs": {"node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/node"}}}}, "$ref": "#/$defs/node"},
    {"$ref": "https://invalid.example/schema"},
    {"$ref": "#/$defs/missing"},
    {"$defs": {"limited": {"type": "string", "minLength": 5}}, "$ref": "#/$defs/limited", "minLength": 1},
    {"type": "object", "default": {"$ref": "file:///private/data", "title": "literal"}},
])
def test_input_factoring_preserves_unsupported_reference_documents(schema):
    saved = deepcopy(schema)
    assert compact_provider_input_schema(schema) == saved
    assert schema == saved


@pytest.mark.parametrize("container", ["contains", "x-instance-data"])
def test_input_dereferencing_does_not_visit_literal_refs_in_unsupported_containers(container):
    literal = {"$ref": "#/$defs/literal"}
    schema = {"type": "array", "$defs": {"literal": {"type": "string"}},
              container: {"const": literal}}
    projected = compact_provider_input_schema(schema)
    assert projected == schema
    payload = [literal]
    assert Draft202012Validator(schema).is_valid(payload)
    assert Draft202012Validator(projected).is_valid(payload)


@pytest.mark.asyncio
async def test_actual_registered_input_schemas_survive_final_sdk_projection():
    server = create_direct_mcp(Mock())
    registered = {tool.name: tool for tool in await server.list_tools()}
    wire = ListToolsResult(tools=[tool.to_mcp_tool() for tool in registered.values()])
    sdk = ListToolsResult.model_validate_json(wire.model_dump_json(by_alias=True))
    assert sdk == wire
    final = {tool.name: tool for tool in sdk.tools}
    assert registered.keys() == final.keys()

    def inspect_schema(node):
        if not isinstance(node, dict):
            return
        assert not ({"allOf", "if", "then", "else", "not"} & node.keys())
        assert node.get("type") != "unknown"
        for key in ("properties", "$defs", "patternProperties"):
            for child in node.get(key, {}).values():
                inspect_schema(child)
        for key in ("items", "additionalProperties", "propertyNames"):
            inspect_schema(node.get(key))
        for key in ("anyOf", "oneOf", "prefixItems"):
            for child in node.get(key, ()):
                inspect_schema(child)

    for name, tool in final.items():
        assert tool.inputSchema == registered[name].parameters, name
        Draft202012Validator.check_schema(tool.inputSchema)
        inspect_schema(tool.inputSchema)
        original = await server.get_tool(name)
        assert _expand_shared_input(tool.inputSchema) == dereference_refs(original.parameters), name
        if name in REFERENCE_TOOL_NAMES:
            assert original._reference_input_paths == reference_schema_projection(tool.inputSchema)[1], name


def _tagged(tag, *, required=True, second="same", **tag_metadata):
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "tag": {"type": "string", "const": tag, **tag_metadata},
            "other": {"type": "string", "const": second},
            "value": {"type": "integer", "minimum": 1, "maximum": 3},
        },
        "required": ["tag", "other", "value"] if required else ["other", "value"],
    }


@pytest.mark.parametrize("union", ["anyOf", "oneOf"])
def test_merged_tags_accept_exact_original_values_and_reject_invalid_payloads(union):
    original = {union: [_tagged("a"), _tagged("b"), _tagged("c")]}
    unchanged = deepcopy(original)
    compact = _compact_closed_unions(original)
    assert original == unchanged
    assert len(compact[union]) == 1
    before, after = Draft202012Validator(original), Draft202012Validator(compact)
    for tag, other, number in product(["a", "b", "c", "d", None, 1], ["same", "different"], [0, 1, 3, 4, "1", True]):
        payload = {"tag": tag, "other": other, "value": number}
        assert before.is_valid(payload) == after.is_valid(payload)
    for payload in ({"other": "same", "value": 2}, {"tag": "a", "other": "same", "value": 2, "extra": 0}):
        assert not before.is_valid(payload) and not after.is_valid(payload)


@pytest.mark.parametrize("variants", [
    [_tagged("a", second="x"), _tagged("b", second="y")],
    [_tagged("a", required=False), _tagged("b", required=False)],
    [_tagged("a", description="First"), _tagged("b", description="Second")],
    [_tagged("a", default="a"), _tagged("b", default="b")],
    [_tagged("a"), _tagged("a")],
    [{**_tagged("a"), "additionalProperties": True}, {**_tagged("b"), "additionalProperties": True}],
])
def test_non_equivalent_or_overlapping_branches_are_preserved(variants):
    original = {"oneOf": variants}
    assert _compact_closed_unions(original) == original


def test_compactor_does_not_rewrite_arbitrary_default_or_example_data():
    payload = {"oneOf": [_tagged("a"), _tagged("b")]}
    original = {"type": "object", "default": payload, "examples": [payload]}
    assert _compact_closed_unions(original) == original
