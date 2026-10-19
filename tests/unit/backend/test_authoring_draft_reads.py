"""Info reads expose editable business fields, while Code owns source views."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest
from pydantic import TypeAdapter

from CAD2BIM_backend.application.geometry_authoring_service import GeometryAuthoringService
from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringService
from CAD2BIM_backend.domain.authoring_draft_content import GeometryDraftContent, FamilyDraftContent
from CAD2BIM_backend.internal_authoring_contracts import GeometryInfoInput, FamilyInfoInput
from CAD2BIM_mcp.direct.ifc.geometry.contracts import DirectIfcGeometryResourceInfoInput, IfcGeometryResourceInfoResult
from CAD2BIM_mcp.direct.ifc.product_family.contracts import DirectIfcProductFamilyInfoInput, IfcProductFamilyInfoResult
from CAD2BIM_mcp.runner.authoring_tools import _geometry_info
from tests.unit.backend.test_product_realization_preview_service import _request, _scope


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_kind", ["mapped_model", "modeling_function"])
async def test_geometry_draft_info_projects_current_generation_without_artifact_or_source_text(resource_kind):
    service = GeometryAuthoringService.__new__(GeometryAuthoringService)
    service._drafts = SimpleNamespace(save_navigation=AsyncMock(return_value={"operation_ref": "cur.saved", "operation_alias": "op_003"}))
    schema = {"type":"object", "properties":{"length_m":{"type":"number", "minimum":0.1, "maximum":1.0, "default":0.5}}, "required":["length_m"], "additionalProperties":False}
    content = GeometryDraftContent(display_name="Editable shape", usage_instructions="Use METRE parameters",
        source_files=({"path": "main.py", "source": "def build(ctx):\n    return ctx\n"},),
        input_metadata={"resource_kind":resource_kind, "parameter_schema":schema if resource_kind == "modeling_function" else None})
    service.read_draft = AsyncMock(return_value=(SimpleNamespace(generation=3), content))
    result = await service.draft_info(_scope(), draft_ref="cur.geometry-draft")
    assert result.edit_token == 3 and result.source_files == ("main.py",)
    assert result.resource_kind == resource_kind
    assert (json.loads(result.parameter_schema_json) if result.parameter_schema_json is not None else None) == (schema if resource_kind == "modeling_function" else None)
    assert set(result.model_dump()).isdisjoint({"snapshot", "artifact_id", "source", "content_digest"})
    assert _geometry_info(result.model_dump(mode="json")).info == result
    assert IfcGeometryResourceInfoResult(info=result).info == result


@pytest.mark.asyncio
async def test_family_draft_info_preserves_business_contract_and_draft_dependency_reference():
    from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope
    owner = ResourceOwnerScope(scope_kind="task", tenant_id=_scope().identity.tenant_id, task_id=_scope().identity.task_id)
    service = ProductFamilyAuthoringService.__new__(ProductFamilyAuthoringService)
    service._drafts = SimpleNamespace(save_navigation=AsyncMock(return_value={"operation_ref": "cur.saved", "operation_alias": "op_004"}),
        reference=lambda scope, draft: "cur.geometry-draft")
    service._geometry_authoring = SimpleNamespace(_repository=SimpleNamespace(get_geometry_draft=AsyncMock(
        return_value=SimpleNamespace(root_id="geometry-root", owner_scope=owner))))
    content = FamilyDraftContent(display_name="Editable family", purpose="Assembly", usage_instructions="Use exact interfaces",
        source_files=({"path": "main.py", "source": "def realize(ctx):\n    return ctx\n"},),
        definition={"family_contract": _request().family_contract, "geometry_bindings": [{
            "binding_key": "body_geometry", "source": {"source_kind": "draft", "resource_id": "geometry-root"},
            "expected_resource_kind": "mapped_model", "geometry_purpose": "shape_fragment"}]})
    service.read_draft = AsyncMock(return_value=(SimpleNamespace(generation=4, owner_scope=owner), content))
    result = await service.draft_info(_scope(), draft_ref="cur.family-draft")
    assert result.edit_token == 4 and result.geometry_bindings[0].source.draft_ref == "cur.geometry-draft"
    assert content.definition.geometry_bindings[0].source.resource_id == "geometry-root"
    assert '"body_shape"' in result.contract_json
    assert IfcProductFamilyInfoResult(info=result).info == result


@pytest.mark.parametrize("internal,direct", [(GeometryInfoInput, DirectIfcGeometryResourceInfoInput),
                                            (FamilyInfoInput, DirectIfcProductFamilyInfoInput)])
def test_info_draft_branch_is_shared_and_closed(internal, direct):
    value = {"target_kind": "draft", "draft_ref": "cur.draft"}
    assert TypeAdapter(internal).validate_python(value).draft_ref == "cur.draft"
    assert direct.model_validate({"direct_session_alias": "direct_001", "target": value}).target.draft_ref == "cur.draft"
    with pytest.raises(ValueError):
        TypeAdapter(internal).validate_python({**value, "resource_alias": "cur.published"})


@pytest.mark.asyncio
@pytest.mark.parametrize("interface_source", ["built", "declared", "conflicting"])
async def test_family_draft_preparation_freezes_mapped_inputs_without_executing_them(interface_source):
    import hashlib
    from CAD2BIM_backend.domain.data_runtime import ArtifactVersionRef
    from CAD2BIM_backend.domain.geometry_runtime import GeometryBuildFixedRequest
    from CAD2BIM_backend.domain.preview_cases import PreviewSourceSnapshot
    from CAD2BIM_backend.infrastructure.source_text.geometry_authoring import SourceTextGeometryAuthoringSource
    from tests.unit.backend.test_product_realization_preview_service import _geometry, _model_interfaces
    from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope
    owner = ResourceOwnerScope(scope_kind="task", tenant_id=_scope().identity.tenant_id, task_id=_scope().identity.task_id)
    service = ProductFamilyAuthoringService.__new__(ProductFamilyAuthoringService)
    service._drafts = SimpleNamespace(resolve_reference=lambda scope, reference, **kwargs: "geometry",
        reference=lambda scope, draft: "cur.geometry")
    contract = _request().family_contract.model_dump(mode="json", by_alias=True)
    option = dict(contract["geometry_fragments"][0]["binding_options"][0])
    option["binding_key"] = "same_root_second_binding"
    contract["geometry_fragments"][0]["binding_options"].append(option)
    content = FamilyDraftContent(display_name="Editable family", purpose="Assembly", usage_instructions="Use exact interfaces",
        source_files=({"path": "main.py", "source": "def realize(ctx):\n    return ctx\n"},),
        definition={"family_contract": contract, "geometry_bindings": [{
            "binding_key": "body_geometry", "source": {"source_kind": "draft", "resource_id": "geometry"},
            "expected_resource_kind": "mapped_model", "geometry_purpose": "shape_fragment"}, {
            "binding_key": "same_root_second_binding", "source": {"source_kind": "draft", "resource_id": "geometry"},
            "expected_resource_kind": "mapped_model", "geometry_purpose": "shape_fragment"}]})
    artifact = ArtifactVersionRef(artifact_id="draft", version_id="snapshot", sha256="a" * 64)
    service.read_draft = AsyncMock(return_value=(SimpleNamespace(root_id="family", snapshot=artifact, owner_scope=owner), content))
    geometry_source = PreviewSourceSnapshot(source_kind="geometry_sample", source_type="draft",
        source_id="geometry", source_ref="cur.geometry", snapshot_id="snapshot", content_digest="b" * 64,
        title="Geometry", draft_artifact=artifact)
    expected_interfaces = _model_interfaces()
    declared_interfaces = () if interface_source == "built" else expected_interfaces
    built_interfaces = () if interface_source == "declared" else expected_interfaces
    if interface_source == "conflicting":
        built_interfaces = (expected_interfaces[0].model_copy(update={"local_frame":
            expected_interfaces[0].local_frame.model_copy(update={"translation_m": (0.5, 0., 0.)})}),)
    service._geometry_authoring = SimpleNamespace(_repository=SimpleNamespace(get_geometry_draft=AsyncMock(
        return_value=SimpleNamespace(root_id="geometry", owner_scope=owner))), prepare_draft_preview=AsyncMock(return_value={
        "source": geometry_source, "build_request": GeometryBuildFixedRequest(source_bundle=b"mapped source"),
        "interfaces": declared_interfaces, "dependencies": ()}))
    service._source_bundles = SourceTextGeometryAuthoringSource()
    from CAD2BIM_backend.application.product_realization_input_service import ProductRealizationInputService
    service._realization_inputs = SimpleNamespace(normalize_contract_defaults=lambda contract, schemas: contract,
        prepare_definition_requests=ProductRealizationInputService.prepare_definition_requests)
    prepared = await service.prepare_draft_preview(_scope(), draft_ref="cur.family", invocations=(_request().invocation,))
    assert prepared["requests"] is None
    service._geometry_authoring.prepare_draft_preview.assert_awaited_once()
    assert len(prepared["dependencies"]) == 1
    runtime = SimpleNamespace(build_fixed=AsyncMock(return_value=SimpleNamespace(geometry=_geometry(),
        interfaces=built_interfaces, source_bundle_sha256=hashlib.sha256(b"mapped source").hexdigest())))
    if interface_source == "conflicting":
        with pytest.raises(ValueError, match="Declared Geometry interfaces differ from the actual fixed build"):
            await prepared["request_loader"](runtime)
        runtime.build_fixed.assert_awaited_once()
        return
    requests = await prepared["request_loader"](runtime)
    assert len(requests) == 1 and requests[0].geometry_bindings[0].geometry == _geometry()
    assert requests[0].geometry_bindings[1].geometry == _geometry()
    assert all(binding.interfaces == expected_interfaces for binding in requests[0].geometry_bindings)
    runtime.build_fixed.assert_awaited_once()
