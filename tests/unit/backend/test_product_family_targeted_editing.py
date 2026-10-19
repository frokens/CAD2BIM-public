"""Family business-key merges and atomic draft preflight checks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringApplicationError
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.product_family import ProductFamilyVersionAssetRole
from CAD2BIM_backend.domain.product_family_authoring import FamilyDraftDefinitionEdit, FamilyDraftWriteCommand
from tests.unit.backend.product_family_fixtures import current_family_document
from tests.unit.backend.test_authoring_draft_service import geometry_create, setup_services


async def _create_family():
    scope, repository, geometry, family, publication = setup_services()
    saved_geometry = await geometry.save_draft_from_aliases(scope, declaration=geometry_create(), request_identity="geometry")
    binding = {"binding_key": "body_geometry", "source": {"source_kind": "draft",
        "draft_ref": geometry.draft_reference(scope, saved_geometry.draft)},
        "expected_resource_kind": "modeling_function", "geometry_purpose": "product_component"}
    declaration = {"mode": "create", "display_name": "Family", "purpose": "Inspect component",
        "usage_instructions": {"action": "set", "value": "Use metres.\n尺寸由合同限定。"},
        "source": {"source_kind": "inline", "source_files": [{"path": "main.py", "source":
            "def realize(ctx):\n    return None\ndef standard_model(ctx):\n    return None\n"}],
            "definition": {"action": "set", "value": {"family_contract": current_family_document(), "geometry_bindings": [binding]}}}}
    saved = await family.save_draft_from_aliases(scope, declaration=declaration, request_identity="family")
    return scope, repository, geometry, family, saved, declaration


@pytest.mark.asyncio
async def test_write_compares_frozen_sources_then_inherits_them_for_source_or_usage_only(monkeypatch):
    scope, repository, geometry, family, saved, declaration = await _create_family()
    reference = family.draft_reference(scope, saved.draft)
    _, original = await family.read_draft(scope, draft_ref=reference)
    sources = original.definition.family_contract.geometry_fragments[0].binding_options[0].parameter_sources
    assert sources.default_source is None and sources.overrides["diameter"].value == 0.1
    operations = dict(repository.operations)
    with pytest.raises(ProductFamilyAuthoringApplicationError) as caught:
        await family.save_draft_from_aliases(scope, request_identity="equivalent-definition", declaration={
            "mode": "update", "draft_ref": reference, "expected_edit_token": 1,
            "definition": declaration["source"]["definition"]})
    assert caught.value.code == "DRAFT_UNCHANGED" and repository.operations == operations
    monkeypatch.setattr(geometry, "read_draft", AsyncMock(
        side_effect=AssertionError("Unchanged static Geometry dependency was read")))
    usage = await family.save_draft_from_aliases(scope, request_identity="usage-only", declaration={
        "mode": "update", "draft_ref": reference, "expected_edit_token": 1,
        "usage_instructions": {"action": "set", "value": "Use the same frozen parameter sources."}})
    source = await family.save_draft_from_aliases(scope, request_identity="source-only", declaration={
        "mode": "update", "draft_ref": reference, "expected_edit_token": usage.draft.generation,
        "source_changes": [{"change_kind": "literal_edit", "path": "main.py", "edits": [{
            "old_text": "def realize(ctx):", "new_text": "# Same numeric contract.\ndef realize(ctx):"}]}]})
    _, content = await family.read_draft(scope, draft_ref=reference)
    assert source.draft.generation == 3 and content.definition == original.definition
    geometry.read_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_family_merges_parameter_binding_and_mapping_before_full_validation():
    scope, repository, _, family, saved, declaration = await _create_family()
    original = declaration["source"]["definition"]["value"]
    binding = {**original["geometry_bindings"][0], "binding_key": "new_geometry"}
    fragment = {**original["family_contract"]["geometry_fragments"][0], "binding_options": [{
        "binding_key": "new_geometry", "parameter_sources": {"overrides": {"diameter": {"source_kind": "public_parameter", "parameter_key": "diameter"}}}}]}
    change = {"action": "edit", "geometry_bindings": {"set": [binding], "remove": ["body_geometry"]},
        "family_contract": {"public_parameters": {"set": [{"parameter_kind": "number", "parameter_key": "diameter", "unit": "metre", "minimum": 0.01, "maximum": 1}]},
            "geometry_fragments": {"set": [fragment]}}}
    reference = family.draft_reference(scope, saved.draft)
    updated = await family.save_draft_from_aliases(scope, request_identity="joint-update", declaration={
        "mode": "update", "draft_ref": reference, "expected_edit_token": 1, "definition": change,
        "usage_instructions": {"action": "edit", "edits": [{"old_text": "Use metres.", "new_text": "Use metres for diameter."},
            {"old_text": "尺寸由合同限定。", "new_text": "尺寸由合同限定。\n直径按实际输入。"}]}})
    _, content = await family.read_draft(scope, draft_ref=reference)
    assert updated.draft.generation == 2
    assert content.usage_instructions == "Use metres for diameter.\n尺寸由合同限定。\n直径按实际输入。"
    assert content.definition.family_contract.geometry_fragments[0].binding_options[0].binding_key == "new_geometry"
    assert content.definition.geometry_bindings[0].binding_key == "new_geometry"
    assert content.display_name == "Family" and content.source_files[0].source == declaration["source"]["source_files"][0]["source"]
    operations_before = dict(repository.operations)
    with pytest.raises(ProductFamilyAuthoringApplicationError) as captured:
        await family.save_draft_from_aliases(scope, request_identity="dangling-delete", declaration={
            "mode": "update", "draft_ref": reference, "expected_edit_token": 2,
            "definition": {"action": "edit", "family_contract": {"public_parameters": {"remove": ["diameter"]}}}})
    assert captured.value.phase == "preflight" and captured.value.code == "PRODUCT_FAMILY_INPUT_INVALID"
    assert captured.value.diagnostic["field"].startswith("definition")
    assert all(key in str(captured.value) for key in ("body_shape", "new_geometry", "diameter"))
    from CAD2BIM_backend.internal_authoring_api import _domain_error
    from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
    from CAD2BIM_mcp.runner.backend_client import BackendDomainError
    from CAD2BIM_mcp.runner.domain_errors import backend_domain_tool_error
    direct = _tool_error(DirectApplicationError(captured.value.code, str(captured.value), phase=captured.value.phase,
        diagnostic=captured.value.diagnostic))
    http_error = _domain_error(captured.value)
    runner = backend_domain_tool_error(BackendDomainError(http_error.status_code, http_error.detail))
    for projected in (direct, runner):
        assert all(key in str(projected) for key in ("preflight", "definition", "body_shape", "new_geometry", "diameter"))
    assert repository.drafts[saved.draft.root_id] == updated.draft
    assert repository.operations == operations_before


@pytest.mark.asyncio
@pytest.mark.parametrize("change,expected", [
    ({"action": "edit", "edits": [{"old_text": "missing anchor", "new_text": "replacement"}]}, "1"),
    ({"action": "edit", "edits": [{"old_text": "Use metres.", "new_text": ""}, {"old_text": "尺寸由合同限定。", "new_text": ""}]}, "usage_instructions"),
])
async def test_invalid_usage_edits_keep_snapshot_and_operation_unchanged(change, expected):
    scope, repository, _, family, saved, _ = await _create_family()
    operations_before = dict(repository.operations)
    with pytest.raises(ProductFamilyAuthoringApplicationError) as captured:
        await family.save_draft_from_aliases(scope, request_identity="invalid-usage", declaration={
            "mode": "update", "draft_ref": family.draft_reference(scope, saved.draft), "expected_edit_token": 1,
            "usage_instructions": change})
    assert captured.value.phase == "preflight" and expected in str(captured.value)
    if expected == "1":
        assert captured.value.diagnostic["edit_index"] == 1
    assert repository.drafts[saved.draft.root_id] == saved.draft
    assert repository.operations == operations_before


@pytest.mark.asyncio
async def test_missing_collection_removal_and_exact_binding_kind_fail_preflight():
    scope, repository, _, family, saved, declaration = await _create_family()
    reference = family.draft_reference(scope, saved.draft)
    with pytest.raises(ProductFamilyAuthoringApplicationError, match="remove\\[1\\].*missing.*absent") as captured:
        await family.save_draft_from_aliases(scope, request_identity="missing-removal", declaration={
            "mode": "update", "draft_ref": reference, "expected_edit_token": 1,
            "definition": {"action": "edit", "geometry_bindings": {"remove": ["absent"]}}})
    assert captured.value.phase == "preflight"
    binding = {**declaration["source"]["definition"]["value"]["geometry_bindings"][0], "expected_resource_kind": "mapped_model"}
    with pytest.raises(ProductFamilyAuthoringApplicationError) as captured:
        await family.save_draft_from_aliases(scope, request_identity="wrong-kind", declaration={
            "mode": "update", "draft_ref": reference, "expected_edit_token": 1,
            "definition": {"action": "edit", "geometry_bindings": {"set": [binding]}}})
    assert captured.value.code == "BINDINGS_INVALID" and captured.value.phase == "preflight"
    assert captured.value.diagnostic["binding_key"] == "body_geometry"
    fragment = declaration["source"]["definition"]["value"]["family_contract"]["geometry_fragments"][0]
    with pytest.raises(ProductFamilyAuthoringApplicationError) as captured:
        await family.save_draft_from_aliases(scope, request_identity="missing-binding", declaration={
            "mode": "update", "draft_ref": reference, "expected_edit_token": 1,
            "definition": {"action": "edit", "family_contract": {"geometry_fragments": {"set": [{
                **fragment, "binding_options": [{"binding_key": "missing_geometry", "parameter_sources": {"overrides": {}}}]}]}}}})
    assert captured.value.phase == "preflight" and captured.value.diagnostic["missing_binding_keys"] == ["missing_geometry"]
    assert captured.value.diagnostic["undeclared_binding_keys"] == ["body_geometry"]
    assert "missing_geometry" in str(captured.value) and "body_geometry" in str(captured.value)
    assert repository.drafts[saved.draft.root_id] == saved.draft


@pytest.mark.asyncio
@pytest.mark.parametrize("source_usage", ["Exact selected version usage.", None])
@pytest.mark.parametrize("same_owner_draft", [False, True])
async def test_published_create_inherits_exact_version_metadata_and_requires_missing_usage(source_usage, same_owner_draft):
    scope, repository, geometry, family, saved, declaration = await _create_family()
    definition = declaration["source"]["definition"]["value"]
    packed = family._source_bundles.pack({item["path"]: item["source"].encode() for item in declaration["source"]["source_files"]})
    bundle = SimpleNamespace(family=SimpleNamespace(family_id="selected-family", owner_scope=None),
        version=SimpleNamespace(family_version_id="selected-version", display_name="Exact version name", purpose="Exact version purpose",
            usage_instructions=source_usage, family_contract=definition["family_contract"]),
        assets=(SimpleNamespace(asset_role=ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE),),
        geometry_bindings=(SimpleNamespace(binding_key="body_geometry", geometry_resource_id="geometry-root", geometry_resource_version_id="selected-geometry",
            expected_resource_kind=GeometryResourceKind.MODELING_FUNCTION, geometry_purpose="product_component"),))
    if same_owner_draft:
        from CAD2BIM_backend.domain.control_plane import ConflictError
        bundle.family = repository.roots[saved.draft.root_id]
        repository.commit_product_family_draft = AsyncMock(side_effect=ConflictError(
            "the draft changed; read its current edit token before saving"))
    family._family_catalog = SimpleNamespace(resolve_exact_version=AsyncMock(return_value=bundle))
    family._family_assets = SimpleNamespace(read_exact_asset=AsyncMock(return_value=packed.content))
    exact_geometry = SimpleNamespace(resource=SimpleNamespace(resource_kind=GeometryResourceKind.MODELING_FUNCTION,
        resource_id="geometry-root", owner_scope=repository.drafts[next(iter(repository.drafts))].owner_scope), version=SimpleNamespace(resource_version_id="selected-geometry"))
    schema_body = b'{"type":"object","additionalProperties":false,"properties":{"diameter":{"type":"number","minimum":0.01,"maximum":1,"default":0.1}}}'
    geometry._resources = SimpleNamespace(get_exact_version=AsyncMock(return_value=exact_geometry),
        read_exact_asset=AsyncMock(return_value=(None, schema_body)))
    geometry._catalog = SimpleNamespace(version_ref=Mock(return_value="cur.exact-geometry"), resolve_exact_version=AsyncMock(return_value=exact_geometry))
    request = {"mode": "create", "source": {"source_kind": "published", "family_version_alias": "cur.selected-family"}}
    operations_before = dict(repository.operations)
    if source_usage is None:
        with pytest.raises(ProductFamilyAuthoringApplicationError, match="action=set") as captured:
            await family.save_draft_from_aliases(scope, declaration=request, request_identity="published-missing-usage")
        assert captured.value.phase == "preflight" and repository.operations == operations_before
        request["usage_instructions"] = {"action": "set", "value": "Explicit valid replacement."}
    if same_owner_draft:
        with pytest.raises(ProductFamilyAuthoringApplicationError) as captured:
            await family.save_draft_from_aliases(scope, declaration=request, request_identity="published")
        assert captured.value.code == "DRAFT_CHANGED" and captured.value.phase == "preflight"
        assert not captured.value.retryable and captured.value.operation_alias is None
        assert repository.drafts[saved.draft.root_id] == saved.draft
        assert repository.operations == operations_before
        commit = repository.commit_product_family_draft.await_args.kwargs
        assert commit["family"].family_id == saved.draft.root_id and commit["expected_generation"] is None
        snapshot = await family.read_draft_snapshot(scope, commit["snapshot"])
        assert snapshot.published_base.family_version_id == "selected-version"
        return
    result = await family.save_draft_from_aliases(scope, declaration=request, request_identity="published")
    _, content = await family.read_draft(scope, draft_ref=family.draft_reference(scope, result.draft))
    assert content.display_name == "Exact version name" and content.purpose == "Exact version purpose"
    assert content.usage_instructions == (source_usage or "Explicit valid replacement.")
    assert content.published_base.family_version_id == "selected-version"
    family._family_catalog.resolve_exact_version.assert_awaited_with(scope.identity, "cur.selected-family")


def test_business_key_merges_preserve_order_and_standard_model_set_clear():
    from CAD2BIM_backend.domain.product_family_authoring import FamilyDraftDefinition
    previous = FamilyDraftDefinition.model_validate({"family_contract": current_family_document(public_parameters=[
        {"parameter_kind": "number", "parameter_key": key, "required": False, "default_value": 1} for key in ("first", "second")]),
        "geometry_bindings": [{"binding_key": "body_geometry", "source": {"source_kind": "published", "resource_version_alias": "cur.geometry"},
            "expected_resource_kind": "modeling_function", "geometry_purpose": "product_component"}]})
    edit = FamilyDraftDefinitionEdit.model_validate({"action": "edit", "family_contract": {
        "public_parameters": {"set": [{"parameter_kind": "number", "parameter_key": "second", "required": False, "default_value": 2},
            {"parameter_kind": "number", "parameter_key": "third", "required": False, "default_value": 3}]},
        "standard_model": {"action": "set", "value": {"public_parameters": {"third": 3}}}}})
    merged = edit.merge(previous)
    assert tuple(item.parameter_key for item in merged.family_contract.public_parameters) == ("first", "second", "third")
    assert tuple(item.default_value for item in merged.family_contract.public_parameters) == (1, 2, 3)
    cleared = FamilyDraftDefinitionEdit.model_validate({"action": "edit", "family_contract": {"standard_model": {"action": "clear"}}}).merge(merged)
    assert cleared.family_contract.standard_model is None and previous.family_contract.standard_model is None


@pytest.mark.parametrize("change", [
    {"set": [{"parameter_kind": "number", "parameter_key": "x"}] * 2},
    {"remove": ["x", "x"]},
    {"set": [{"parameter_kind": "number", "parameter_key": "x"}], "remove": ["x"]},
])
def test_family_collection_edits_reject_duplicates_and_conflicting_keys(change):
    with pytest.raises(ValidationError):
        FamilyDraftDefinitionEdit.model_validate({"action": "edit", "family_contract": {"public_parameters": change}})


def test_family_published_and_inline_create_are_distinct_closed_branches():
    adapter = TypeAdapter(FamilyDraftWriteCommand)
    parsed = adapter.validate_python({"mode": "create", "source": {"source_kind": "published", "family_version_alias": "cur.family"}})
    assert parsed.model_dump(exclude_unset=True) == {"mode": "create", "source": {"source_kind": "published", "family_version_alias": "cur.family"}}
    with pytest.raises(ValidationError):
        adapter.validate_python({"mode": "create", "usage_instructions": {"action": "edit", "edits": [{"old_text": "x", "new_text": "y"}]},
            "source": {"source_kind": "published", "family_version_alias": "cur.family"}})
    with pytest.raises(ValidationError):
        adapter.validate_python({"mode": "create", "source": {"source_kind": "inline", "source_files": [], "definition": {"action": "edit"}}})


@pytest.mark.asyncio
async def test_invalid_update_reports_its_business_field_instead_of_other_create_branch():
    scope, _, _, family, saved, _ = await _create_family()
    with pytest.raises(ProductFamilyAuthoringApplicationError) as captured:
        await family.save_draft_from_aliases(scope, request_identity="invalid-command", declaration={
            "mode": "update", "draft_ref": family.draft_reference(scope, saved.draft), "expected_edit_token": 1,
            "definition": {"action": "edit", "geometry_bindings": None}})
    assert captured.value.phase == "preflight"
    assert captured.value.diagnostic["field"] == "definition.edit.geometry_bindings"
