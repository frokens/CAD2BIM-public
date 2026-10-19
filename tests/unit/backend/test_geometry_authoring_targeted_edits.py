"""Geometry content edits resolve into the existing atomic draft snapshot."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.application.geometry_authoring_service import GeometryAuthoringApplicationError
from CAD2BIM_backend.domain.geometry_authoring import (
    GeometryDraftWriteCommand, GeometryParameterSchemaChange, resolve_geometry_parameter_schema,
)
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole, GeometryResourceKind
from tests.unit.backend.test_authoring_draft_service import geometry_create, setup_services


def _schema_change(value):
    return TypeAdapter(GeometryParameterSchemaChange).validate_python(value)


def test_parameter_edits_preserve_root_and_required_and_reject_invalid_root_default():
    schema = geometry_create()["source"]["parameter_schema"]["value"]
    schema["properties"]["length"] = {"type": "number", "minimum": 0, "default": 0.5}
    schema.update(description="Authoritative units", default={"diameter": 0.1, "length": 0.5})
    original = deepcopy(schema)
    result = resolve_geometry_parameter_schema(schema, _schema_change({"action": "edit", "set": {
        "diameter": {"schema": {"type": "number", "minimum": 0.05, "default": 0.1}, "required": False},
        "length": {"schema": {"type": "number", "minimum": 0, "default": 1}, "required": True}}}))
    assert schema == original
    assert result["description"] == schema["description"] and result["default"] == schema["default"]
    assert result["required"] == ["length"]
    with pytest.raises(ValueError, match="default violates"):
        resolve_geometry_parameter_schema(result, _schema_change({"action": "edit", "remove": ["diameter"]}))
    without_root_default = {key: value for key, value in result.items() if key != "default"}
    removed = resolve_geometry_parameter_schema(without_root_default,
        _schema_change({"action": "edit", "remove": ["length"]}))
    assert removed["required"] == [] and set(removed["properties"]) == {"diameter"}


@pytest.mark.parametrize("change", [
    {"action": "edit"},
    {"action": "edit", "remove": ["diameter", "diameter"]},
    {"action": "edit", "set": {"diameter": {"schema": {"type": "number"}, "required": True}}, "remove": ["diameter"]},
    {"action": "edit", "set": {"diameter": {"schema": {"type": "number"}}}},
    {"action": "set", "value": {"type": "object", "properties": {}, "required": [], "additionalProperties": False, "$defs": {}}},
])
def test_parameter_change_contract_rejects_ambiguous_or_unsupported_inputs(change):
    with pytest.raises(ValidationError):
        _schema_change(change)


async def test_geometry_edit_merges_usage_schema_and_source_in_one_snapshot():
    scope, repo, geometry, _, _ = setup_services()
    created = await geometry.save_draft_from_aliases(scope, declaration=geometry_create(), request_identity="create")
    reference = geometry.draft_reference(scope, created.draft)
    declaration = {"mode": "update", "draft_ref": reference, "expected_edit_token": 1,
        "usage_instructions": {"action": "edit", "edits": [{"old_text": "metres", "new_text": "metres (米)"}]},
        "parameter_schema": {"action": "edit", "set": {
            "diameter": {"schema": {"type": "number", "minimum": 0.05, "maximum": 1, "default": 0.1}, "required": False},
            "length": {"schema": {"type": "number", "minimum": 0, "default": 1}, "required": True}}},
        "source_changes": [{"change_kind": "literal_edit", "path": "main.py",
            "edits": [{"old_text": "return None", "new_text": "return None  # targeted revision"}]}]}
    saved = await geometry.save_draft_from_aliases(scope, declaration=declaration, request_identity="edit")
    _, content = await geometry.read_draft(scope, draft_ref=reference)
    assert saved.draft.generation == 2
    assert content.usage_instructions == "Use dimensions in metres (米)."
    assert content.input_metadata.parameter_schema["required"] == ["length"]
    assert "targeted revision" in next(item.source for item in content.source_files if item.path == "main.py")
    assert not repo.versions and not repo.store_calls
    assert await geometry.save_draft_from_aliases(scope, declaration=declaration, request_identity="edit") == saved


@pytest.mark.parametrize("change,diagnostic", [
    ({"usage_instructions": {"action": "edit", "edits": [
        {"old_text": "metres", "new_text": "米"}, {"old_text": "missing text", "new_text": "replacement"}]}},
        {"field": "usage_instructions", "edit_index": 2}),
    ({"parameter_schema": {"action": "edit", "remove": ["unknown"]}},
        {"field": "parameter_schema", "parameter_key": "unknown"}),
    ({"usage_instructions": {"action": "set", "value": " \n "}}, {"field": "usage_instructions"}),
    ({"interfaces": []}, {"field": "interfaces"}),
])
async def test_geometry_edit_failure_is_preflight_and_leaves_original_snapshot(change, diagnostic):
    scope, repo, geometry, _, _ = setup_services()
    created = await geometry.save_draft_from_aliases(scope, declaration=geometry_create(), request_identity="create")
    with pytest.raises(GeometryAuthoringApplicationError) as error:
        await geometry.save_draft_from_aliases(scope, request_identity="invalid", declaration={
            "mode": "update", "draft_ref": geometry.draft_reference(scope, created.draft), "expected_edit_token": 1, **change})
    assert error.value.phase == "preflight" and error.value.code == "GEOMETRY_DRAFT_INPUT_INVALID"
    assert all(error.value.diagnostic[key] == value for key, value in diagnostic.items())
    assert repo.drafts[created.draft.root_id] == created.draft and len(repo.operations) == 1


async def _published_geometry(geometry, *, usage="Exact version instructions."):
    declaration = geometry_create()
    packed = geometry._source_bundles.pack({item["path"]: item["source"].encode() for item in declaration["source"]["source_files"]})
    schema = json.dumps(declaration["source"]["parameter_schema"]["value"]).encode()
    bundle = SimpleNamespace(resource=SimpleNamespace(resource_id="geometry-published",
        resource_kind=GeometryResourceKind.MODELING_FUNCTION, display_name="Selected root name", owner_scope=object()),
        version=SimpleNamespace(resource_version_id="geometry-exact-v1", manifest={"usage_instructions": usage,
            "interfaces": [{"from_build_output": True}]}))
    geometry._catalog = SimpleNamespace(resolve_exact_version=AsyncMock(return_value=bundle))
    geometry._resources.read_exact_asset = AsyncMock(side_effect=lambda context, **kwargs:
        (None, packed.content) if kwargs["asset_role"] is GeometryResourceAssetRole.SOURCE_BUNDLE else (None, schema))


async def test_published_create_inherits_selected_root_name_and_exact_version_usage():
    scope, _, geometry, _, _ = setup_services()
    await _published_geometry(geometry)
    created = await geometry.save_draft_from_aliases(scope, request_identity="published", declaration={"mode": "create",
        "source": {"source_kind": "published", "resource_version_alias": "cur.exact"}})
    _, content = await geometry.read_draft(scope, draft_ref=geometry.draft_reference(scope, created.draft))
    assert content.display_name == "Selected root name" and content.usage_instructions == "Exact version instructions."
    assert content.interfaces == () and content.published_base.resource_version_id == "geometry-exact-v1"
    geometry._catalog.resolve_exact_version.assert_awaited_once_with(scope.identity, "cur.exact")


async def test_published_create_requires_set_when_exact_version_usage_is_missing():
    scope, repo, geometry, _, _ = setup_services()
    await _published_geometry(geometry, usage=None)
    declaration = {"mode": "create", "source": {"source_kind": "published", "resource_version_alias": "cur.exact"}}
    with pytest.raises(GeometryAuthoringApplicationError, match="action=set"):
        await geometry.save_draft_from_aliases(scope, request_identity="missing", declaration=declaration)
    assert not repo.drafts and not repo.operations
    created = await geometry.save_draft_from_aliases(scope, request_identity="replacement", declaration={**declaration,
        "usage_instructions": {"action": "set", "value": "Explicit complete instructions."}})
    _, content = await geometry.read_draft(scope, draft_ref=geometry.draft_reference(scope, created.draft))
    assert content.usage_instructions == "Explicit complete instructions." and content.display_name == "Selected root name"


def test_inline_create_requires_metadata_and_set_but_published_create_preserves_omission():
    adapter = TypeAdapter(GeometryDraftWriteCommand)
    inline = geometry_create()
    for invalid in ({key: value for key, value in inline.items() if key != "display_name"},
                    {**inline, "usage_instructions": {"action": "edit", "edits": [{"old_text": "old", "new_text": "new"}]}},
                    {**inline, "source": {**inline["source"], "interfaces": []}}):
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid)
    published = {"mode": "create", "source": {"source_kind": "published", "resource_version_alias": "cur.exact"}}
    assert adapter.validate_python(published).model_dump(exclude_unset=True) == published
    with pytest.raises(ValidationError):
        adapter.validate_python({**published, "usage_instructions": None})
