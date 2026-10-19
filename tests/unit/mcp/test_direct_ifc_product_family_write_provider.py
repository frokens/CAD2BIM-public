"""Family draft definition and partial update consumer tests."""
import json
from collections.abc import Mapping, Sequence
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from CAD2BIM_mcp.direct.ifc.product_family.write_contracts import validate_product_family_write_provider_input, IfcProductFamilyWriteResult
from CAD2BIM_mcp.tool_docs.ifc_product_family import PRODUCT_FAMILY_REALIZE_SOURCE_EXAMPLE
_FORBIDDEN_SCHEMA_KEYS = {"allOf", "if", "then", "else", "not"}
_INTERNAL_NAMES = {"tenant_id", "task_id", "run_id", "storage_path", "artifact_id", "artifact_version_id"}
_GEOMETRY_ALIAS = "cur.geometry"
_USAGE = "Family usage instructions."
_SHA = "a" * 64

def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


def _assert_provider_safe_schema(schema: Mapping[str, object]) -> None:
    Draft202012Validator.check_schema(schema)
    for node in _walk(schema):
        if isinstance(node, Mapping):
            assert node != {}
            assert node.get("type") != "unknown"
            assert not (_FORBIDDEN_SCHEMA_KEYS & set(node))
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False
    serialized = json.dumps(schema, sort_keys=True).lower()
    assert '"additionalproperties": {}' not in serialized
    for name in _INTERNAL_NAMES:
        assert f'"{name}"' not in serialized


def _contract() -> dict[str, object]:
    return {
        "product_structure_kind": "single_product",
        "components": [{"component_key": "body", "ifc_class": "IfcValve"}],
        "geometry_fragments": [
            {
                "fragment_key": "body_shape",
                "owner_component_key": "body",
                "binding_options": [{"binding_key": "body_geometry", "parameter_sources": {"overrides": {}}}],
            }
        ],
        "standard_model": {},
        "public_parameters": [
            {
                "parameter_kind": "number",
                "parameter_key": "diameter_m",
                "unit": "metre",
                "minimum": 0.01,
                "maximum": 2.0,
                "default_value": 0.2,
            }
        ],
    }


def _scenario() -> dict[str, object]:
    return {
        "scenario_key": "nominal",
        "expected_status": "AMBIGUOUS",
        "product_origin": [0, 0, 0],
        "length_unit": {"name": "METRE"},
        "public_parameters": {"diameter_m": 0.2},
    }


def _publication_scenarios() -> list[dict[str, object]]:
    common = _scenario()
    return [
        {
            **common,
            "scenario_key": "nominal",
            "realization_kind": "standard_model",
            "expected_status": "SOLVED",
        },
        {
            **common,
            "scenario_key": "standard_placed",
            "realization_kind": "standard_model",
            "expected_status": "SOLVED",
            "product_origin": [2, 0, 0],
        },
        {
            **common,
            "scenario_key": "self_geometry",
            "realization_kind": "self_geometry",
            "expected_status": "SOLVED",
        },
    ]



def _binding():
    return {"binding_key": "body_geometry", "source": {"source_kind": "published", "resource_version_alias": _GEOMETRY_ALIAS},
            "expected_resource_kind": "mapped_model", "geometry_purpose": "product_component"}


def _source_files():
    return [{"source_path": "main.py", "source": PRODUCT_FAMILY_REALIZE_SOURCE_EXAMPLE}]


def _definition_json():
    return json.dumps({"family_contract": _contract(), "geometry_bindings": [_binding()]})


def _result():
    return IfcProductFamilyWriteResult(draft_ref="cur.family-draft", edit_token=1, operation_alias="op_101")


def test_family_definition_accepts_draft_binding_without_test_selection():
    definition = json.loads(_definition_json())
    definition["geometry_bindings"][0]["source"] = {"source_kind": "draft", "draft_ref": "cur.geometry-draft"}
    request = {"direct_session_alias": "direct_001", "mode": "create", "display_name": "Draft Family",
        "purpose": "Test partial authoring", "usage_instructions": {"action": "set", "value": _USAGE},
        "source": {"source_kind": "inline", "source_files": _source_files(), "definition_json": json.dumps({"action": "set", "value": definition})}}
    result = validate_product_family_write_provider_input(request).to_domain_declaration()
    assert result["source"]["definition"]["value"]["geometry_bindings"][0]["source"]["source_kind"] == "draft"
    assert result["source"]["definition"]["value"]["family_contract"]["standard_model"] == {}
    definition["validation_scenarios"] = _publication_scenarios()
    from CAD2BIM_mcp.direct.tool_support import ProviderJsonDocumentError
    with pytest.raises(ProviderJsonDocumentError):
        validate_product_family_write_provider_input({**request, "source": {**request["source"], "definition_json": json.dumps({"action": "set", "value": definition})}})


def test_family_metadata_update_omits_definition_and_source():
    request = validate_product_family_write_provider_input({"direct_session_alias": "direct_001",
        "mode": "update", "draft_ref": "cur.family-draft", "expected_edit_token": 1, "purpose": "Updated purpose"})
    declaration = request.to_domain_declaration()
    assert "definition" not in declaration and "source_changes" not in declaration
    with pytest.raises(ValidationError):
        validate_product_family_write_provider_input({**request.model_dump(exclude_unset=True), "definition_json": None})


@pytest.mark.asyncio
async def test_actual_family_provider_schema_has_separate_closed_create_branches_and_typed_usage():
    from unittest.mock import Mock
    from fastmcp import FastMCP
    from CAD2BIM_mcp.direct.ifc.product_family.write_tools import register_ifc_product_family_write
    server = FastMCP("Family input test")
    register_ifc_product_family_write(server, Mock())
    tool = await server.get_tool("ifc_product_family_write")
    _assert_provider_safe_schema(tool.parameters)
    validator = Draft202012Validator(tool.parameters)
    published = {"direct_session_alias": "direct_001", "mode": "create", "source": {
        "source_kind": "published", "family_version_alias": "cur.family"}}
    validator.validate(published)
    assert not validator.is_valid({**published, "usage_instructions": "old bare usage"})
    assert not validator.is_valid({**published, "usage_instructions": None})
    inline = {**published, "source": {"source_kind": "inline", "source_files": _source_files(),
        "definition_json": json.dumps({"action": "set", "value": json.loads(_definition_json())})}}
    assert not validator.is_valid(inline)
    validator.validate({**inline, "display_name": "Family", "purpose": "Inspect component",
        "usage_instructions": {"action": "set", "value": _USAGE}})
    update = {"direct_session_alias": "direct_001", "mode": "update", "draft_ref": "cur.family-draft", "expected_edit_token": 1,
        "usage_instructions": {"action": "edit", "edits": [{"old_text": "old", "new_text": "new"}]}}
    validator.validate(update)
    assert tool._reference_documents["source.definition_json"][0] != tool._reference_documents["definition_json"][0]


@pytest.mark.asyncio
async def test_family_reference_projection_covers_set_and_edit_bindings_without_touching_data():
    from CAD2BIM_mcp.direct.ifc.product_family.write_contracts import product_family_definition_reference_document
    from CAD2BIM_mcp.direct.references import project_reference_fields, reference_schema_projection
    from unittest.mock import AsyncMock
    for create in (True, False):
        schema, _ = product_family_definition_reference_document(create=create)
        _, paths = reference_schema_projection(schema)
        assert ("value", "geometry_bindings", "*", "source", "resource_version_alias") in paths
        document = {"action": "set", "value": {"family_contract": {"standard_model": {
            "public_parameters": {"draft_ref": "gv_" + "d" * 22}}}, "geometry_bindings": [_binding()]}}
        translator = AsyncMock(return_value="cur.expanded")
        result = await project_reference_fields(document, paths, translator)
        assert result["value"]["geometry_bindings"][0]["source"]["resource_version_alias"] == "cur.expanded"
        assert result["value"]["family_contract"]["standard_model"]["public_parameters"]["draft_ref"] == "gv_" + "d" * 22
        translator.assert_awaited_once_with(_GEOMETRY_ALIAS, ("gv",))
        if not create:
            assert ("geometry_bindings", "set", "*", "source", "draft_ref") in paths
            document = {"action": "edit", "geometry_bindings": {"set": [{**_binding(), "source": {"source_kind": "draft", "draft_ref": "gd_" + "a" * 22}}]}}
            translator = AsyncMock(return_value="cur.expanded-draft")
            result = await project_reference_fields(document, paths, translator)
            assert result["geometry_bindings"]["set"][0]["source"]["draft_ref"] == "cur.expanded-draft"
            translator.assert_awaited_once_with("gd_" + "a" * 22, ("gd", "fd"))


def test_family_edit_decoder_preserves_nested_omission_and_rejects_old_bare_definition():
    request = {"direct_session_alias": "direct_001", "mode": "update", "draft_ref": "cur.family-draft", "expected_edit_token": 1,
        "definition_json": json.dumps({"action": "edit", "geometry_bindings": {"set": [_binding()]}})}
    declaration = validate_product_family_write_provider_input(request).to_domain_declaration()
    assert declaration["definition"] == {"action": "edit", "geometry_bindings": {"set": [_binding()]}}
    from CAD2BIM_mcp.direct.tool_support import ProviderJsonDocumentError
    with pytest.raises(ProviderJsonDocumentError):
        validate_product_family_write_provider_input({**request, "definition_json": _definition_json()})


@pytest.mark.asyncio
async def test_family_direct_adapter_retains_merge_preflight_error_and_key():
    from contextlib import nullcontext
    from unittest.mock import AsyncMock
    from CAD2BIM_mcp.direct.errors import DirectApplicationError
    from CAD2BIM_mcp.direct.ifc.product_family.write_application import IfcProductFamilyWriteApplicationMixin
    from tests.unit.backend.test_product_family_targeted_editing import _create_family
    scope, _, _, family, saved, _ = await _create_family()
    app = IfcProductFamilyWriteApplicationMixin()
    app._product_family_authoring_service = family
    app._resolve_scope = AsyncMock(return_value=scope)
    app._scope_binder = lambda identity: nullcontext()
    request = validate_product_family_write_provider_input({"direct_session_alias": "direct_001", "mode": "update",
        "draft_ref": family.draft_reference(scope, saved.draft), "expected_edit_token": 1,
        "definition_json": json.dumps({"action": "edit", "geometry_bindings": {"remove": ["absent_binding"]}})})
    with pytest.raises(DirectApplicationError) as captured:
        await app.write_ifc_product_family(request, request_identity="adapter-invalid-edit")
    assert captured.value.code == "PRODUCT_FAMILY_INPUT_INVALID" and captured.value.phase == "preflight"
    assert captured.value.diagnostic["field"] == "definition"
    assert "absent_binding" in str(captured.value) and "remove[1]" in str(captured.value)


@pytest.mark.asyncio
async def test_family_draft_commit_conflict_reaches_direct_and_runner_diagnostics():
    from contextlib import nullcontext
    from unittest.mock import AsyncMock
    from CAD2BIM_backend.domain.control_plane import ConflictError
    from CAD2BIM_backend.internal_authoring_api import _domain_error
    from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringApplicationError
    from CAD2BIM_mcp.direct.errors import DirectApplicationError
    from CAD2BIM_mcp.direct.ifc.product_family.write_application import IfcProductFamilyWriteApplicationMixin
    from tests.unit.backend.test_product_family_targeted_editing import _create_family

    scope, repository, _, family, saved, _ = await _create_family()
    repository.commit_product_family_draft = AsyncMock(side_effect=ConflictError(
        "the draft changed; read its current edit token before saving"))
    request = validate_product_family_write_provider_input({"direct_session_alias": "direct_001", "mode": "update",
        "draft_ref": family.draft_reference(scope, saved.draft), "expected_edit_token": 1, "display_name": "Changed name"})
    with pytest.raises(ProductFamilyAuthoringApplicationError) as shared:
        await family.save_draft_from_aliases(scope, declaration=request.to_domain_declaration(), request_identity="runner-conflict")
    runner_error = _domain_error(shared.value)
    assert runner_error.status_code == 409 and runner_error.detail["code"] == "DRAFT_CHANGED"
    assert runner_error.detail["phase"] == "preflight" and runner_error.detail["retryable"] is False

    app = IfcProductFamilyWriteApplicationMixin()
    app._product_family_authoring_service = family
    app._resolve_scope = AsyncMock(return_value=scope)
    app._scope_binder = lambda identity: nullcontext()
    with pytest.raises(DirectApplicationError) as direct:
        await app.write_ifc_product_family(request, request_identity="direct-conflict")
    assert direct.value.code == "DRAFT_CHANGED" and direct.value.phase == "preflight"
    assert not direct.value.retryable and direct.value.operation_alias is None
    assert repository.drafts[saved.draft.root_id] == saved.draft
    repository.commit_product_family_draft.side_effect = ValueError("invalid commit result")
    with pytest.raises(DirectApplicationError) as invalid:
        await app.write_ifc_product_family(request, request_identity="invalid-result")
    assert invalid.value.code == "PRODUCT_FAMILY_WRITE_INTEGRITY_ERROR"
    assert invalid.value.phase == "output_validation"
