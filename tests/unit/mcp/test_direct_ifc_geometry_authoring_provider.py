"""Geometry draft entry regressions; publication is a separate capability."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftError
from CAD2BIM_backend.domain.authoring_draft import AuthoringDraftChanged
from CAD2BIM_backend.ports.geometry_authoring import GeometryAuthoringSourceError, GeometryAuthoringSourceFailure
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.geometry.authoring_application import IfcGeometryAuthoringApplicationMixin
from CAD2BIM_mcp.direct.ifc.geometry.authoring_contracts import validate_geometry_resource_write_input
from CAD2BIM_mcp.direct.ifc.geometry.authoring_tools import register_ifc_geometry_resource_write
from CAD2BIM_mcp.tool_docs.ifc_geometry import GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE

_USAGE = "Test Geometry usage instructions."
_DIAMETER_SCHEMA_JSON = json.dumps({"type": "object", "additionalProperties": False,
    "required": [], "properties": {"diameter_m": {"type": "number", "minimum": 0.01,
    "maximum": 1.0, "default": 0.1}}})


def test_update_source_change_uses_draft_cas_without_repeating_member_sha():
    request = validate_geometry_resource_write_input({"direct_session_alias": "direct_001",
        "mode": "update", "draft_ref": "cur.draft", "expected_edit_token": 2,
        "source_changes": [{"change_kind": "replace_member", "source_path": "main.py",
                            "source": "def build(ctx): return ctx.geometry('body')"}]})
    declaration = request.to_domain_declaration()
    assert declaration["source_changes"][0]["path"] == "main.py"
    assert declaration["source_changes"][0]["change_kind"] == "replace_member"
    assert declaration["expected_edit_token"] == 2
    assert "parameter_schema" not in declaration
    assert "geometry_inputs" not in declaration
    with pytest.raises(ValidationError):
        validate_geometry_resource_write_input({**request.model_dump(exclude_unset=True), "parameter_schema_json": None})


@pytest.mark.parametrize("error,code,diagnostic", [
    (AuthoringDraftError("AUTHORING_DRAFT_CHANGED", "Read the current draft before updating"), "AUTHORING_DRAFT_CHANGED", None),
    (AuthoringDraftChanged("Read current Info before updating"), "DRAFT_CHANGED", None),
    (GeometryAuthoringSourceError(GeometryAuthoringSourceFailure.MATCH_NOT_FOUND, "old_text did not match"),
     "GEOMETRY_SOURCE_MATCH_NOT_FOUND", {"failure": "match_not_found"}),
])
async def test_draft_cas_error_remains_actionable_at_direct_boundary(error, code, diagnostic):
    from contextlib import nullcontext
    class Probe(IfcGeometryAuthoringApplicationMixin):
        _resolve_scope = AsyncMock(return_value=SimpleNamespace(identity=object()))
        _scope_binder = staticmethod(lambda _: nullcontext())
    async def changed(_):
        raise error
    with pytest.raises(DirectApplicationError) as caught:
        await Probe()._authoring_call("direct_001", "ifc_geometry_resource_write", changed)
    assert caught.value.code == code
    assert caught.value.phase == "preflight"
    assert caught.value.diagnostic == diagnostic


async def test_geometry_provider_has_closed_create_sources_and_text_actions():
    server = FastMCP("geometry-edits")
    register_ifc_geometry_resource_write(server, object())
    tool = next(item for item in await server.list_tools() if item.name == "ifc_geometry_resource_write")
    validator = Draft202012Validator(tool.parameters)
    inline = json.loads(GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE)
    published = {"direct_session_alias": "direct_001", "mode": "create",
        "source": {"source_kind": "published", "resource_version_alias": "cur.exact"}}
    for value in (inline, published):
        assert validator.is_valid(value)
        validate_geometry_resource_write_input(value)
    for invalid in ({key: value for key, value in inline.items() if key != "usage_instructions"},
                    {**published, "display_name": None}, {**published, "usage_instructions": None},
                    {**published, "usage_instructions": {"action": "edit", "edits": [{"old_text": "a", "new_text": "b"}]}},
                    {**inline, "usage_instructions": {"action": "set", "value": "Use metres.", "edits": []}},
                    {**inline, "source": {**inline["source"], "interfaces": []}}):
        assert not validator.is_valid(invalid)
        with pytest.raises(ValidationError):
            validate_geometry_resource_write_input(invalid)

    def check(schema):
        if isinstance(schema, dict):
            assert not set(schema) & {"allOf", "if", "then", "else", "not"}
            assert schema.get("type") != "unknown"
            for item in schema.values():
                check(item)
        elif isinstance(schema, list):
            for item in schema:
                check(item)
    check(tool.parameters)


def test_geometry_parameter_json_action_keeps_original_final_schema_capacity():
    from CAD2BIM_backend.domain.canonical import canonical_json
    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False, "description": ""}
    schema["description"] = "x" * (16384 - len(canonical_json(schema).encode()))
    wrapped = canonical_json({"action": "set", "value": schema})
    assert len(canonical_json(schema).encode()) == 16384 and len(wrapped.encode()) > 16384
    value = json.loads(GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE)
    value["source"]["parameter_schema_json"] = wrapped
    assert validate_geometry_resource_write_input(value).to_domain_declaration()["source"]["parameter_schema"]["value"] == schema
    for invalid_json in ("{", json.dumps({"action": "edit", "remove": ["diameter"]}), json.dumps(schema)):
        with pytest.raises(ValidationError):
            validate_geometry_resource_write_input({**value, "source": {**value["source"], "parameter_schema_json": invalid_json}})


def test_geometry_parameter_edit_projects_schema_alias_without_omitted_fields():
    change = {"action": "edit", "set": {"diameter": {"schema": {"type": "number"}, "required": True}}, "remove": []}
    declaration = validate_geometry_resource_write_input({"direct_session_alias": "direct_001", "mode": "update",
        "draft_ref": "cur.draft", "expected_edit_token": 1, "parameter_schema_json": json.dumps(change)}).to_domain_declaration()
    assert declaration["parameter_schema"] == change
    assert set(declaration) == {"mode", "draft_ref", "expected_edit_token", "parameter_schema"}
