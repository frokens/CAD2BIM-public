from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from fastmcp import Client, FastMCP
from jsonschema import Draft202012Validator
from pydantic import BaseModel, TypeAdapter, ValidationError

from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.product_realization import ProductRealizationStatus
from CAD2BIM_mcp.runner.authoring_contracts import ProductRealizationPreviewInput
from CAD2BIM_backend.domain.product_family import ProductFamilyGeometryPurpose, ProductFamilyVersionAssetRole
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole, GeometryResourceKind
from CAD2BIM_backend.domain.geometry_catalog import (
    GeometryCatalogAssetSummary, GeometryCatalogResourceSummary,
    GeometryCatalogRootInfo, GeometryCatalogVersionInfo,
    GeometryCatalogVersionSummary, GeometrySourceMember, GeometrySourceReadResult,
)
from CAD2BIM_backend.internal_authoring_api import create_internal_authoring_router
from CAD2BIM_mcp.runner.authoring_tools import (
    _family_preview, _geometry_info, _preview_tool_result, register_runner_authoring_tools,
)
from CAD2BIM_mcp.runner.authoring_contracts import (
    GeometryPreviewInput, GeometryWriteInput, FamilyWriteInput,
    authoring_domain_request,
)
from CAD2BIM_mcp.runner.references import project_runner_tool_declaration
from tests.unit.mcp.test_direct_ifc_product_realization_preview_provider import _outcome


NAMES = {
    "ifc_geometry_resource_list", "ifc_geometry_resource_info",
    "ifc_geometry_resource_source_read", "ifc_geometry_resource_write",
    "ifc_geometry_preview", "ifc_geometry_query_python",
    "ifc_product_family_list", "ifc_product_family_info",
    "ifc_product_family_write", "ifc_product_realization_preview",
}




def _unsupported_schema(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            key in {"allOf", "if", "then", "else", "not"} or _unsupported_schema(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_unsupported_schema(item) for item in value)
    return False


def _unknown_schema_type(value: object) -> bool:
    if isinstance(value, dict):
        return value.get("type") == "unknown" or any(_unknown_schema_type(item) for item in value.values())
    if isinstance(value, list):
        return any(_unknown_schema_type(item) for item in value)
    return False


def _schema_node(schema, value):
    while "$ref" in value:
        value = schema["$defs"][value["$ref"].removeprefix("#/$defs/")]
    return value














def test_runner_preview_projects_image_only_when_backend_authorizes_one() -> None:
    class Result(BaseModel):
        status: str

    solved = _preview_tool_result(Result(status="SOLVED"), b"\x89PNG\r\n")
    assert len(solved.content) == 2
    assert "image_base64" not in solved.structured_content
    ambiguous = _preview_tool_result(Result(status="AMBIGUOUS"), None)
    assert len(ambiguous.content) == 1


async def test_runner_family_list_preserves_rejected_page_navigation(monkeypatch) -> None:
    from CAD2BIM_backend.domain.product_family_catalog import (
        ProductFamilyCatalogListResult, ProductFamilyCatalogRejection,
    )

    rejected = ProductFamilyCatalogListResult(
        families=(), returned_count=0, next_cursor="cur.next-page",
        rejections=(ProductFamilyCatalogRejection(
            family_version_ref="cur.rejected-version", version_no=1,
            code="PRODUCT_FAMILY_CONTRACT_INVALID", field_path="interfaces.0.position_rule.mode",
            reason="Input should be 'fixed'",
        ),),
    )
    backend = SimpleNamespace(call_authoring=AsyncMock(return_value=rejected.model_dump(mode="json")))
    monkeypatch.setattr("CAD2BIM_mcp.runner.authoring_tools.request_domain_credential", lambda: "credential")
    server = FastMCP("Rejected Family discovery")
    register_runner_authoring_tools(server, backend)
    async with Client(server) as client:
        result = await client.call_tool("ifc_product_family_list", {"request": {}})
    public = result.structured_content
    assert public["families"] == [] and public["complete"] is False
    assert public["next_cursor"] == "cur.next-page"
    assert public["rejections"][0]["family_version_alias"] == "cur.rejected-version"
    assert "family_version_ref" not in public["rejections"][0]


@pytest.mark.parametrize("kind", tuple(GeometryResourceKind))
async def test_runner_geometry_info_projects_committed_domain_record_without_internal_ids(kind) -> None:
    from tests.unit.backend.test_geometry_resource_service import _copy_source
    from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
    from CAD2BIM_backend.domain.geometry_resource import geometry_resource_content_digest
    from CAD2BIM_backend.domain.geometry_runtime import GeometrySummary, GeometryBounds, GeometryTopologySummary
    bundle, _ = _copy_source(kind)
    manifest = {**bundle.version.manifest, "unit": "METRE", "local_frame": {"handedness": "right"}}
    if kind is GeometryResourceKind.MAPPED_MODEL:
        manifest.update(geometry_format="occt_brep_v1", geometry_summary=GeometrySummary(
            format="occt_brep_v1", unit="METRE", surface_area_m2=6.0,
            bounds=GeometryBounds(minimum_m=(0, 0, 0), maximum_m=(1, 1, 1)),
            topology=GeometryTopologySummary(solid_count=1, shell_count=1, face_count=6, wire_count=6,
                edge_count=12, vertex_count=8, triangle_count=0, boundary_edge_count=0)).model_dump(mode="json"))
    digest = sha256_digest(manifest)
    assets = tuple(item.model_copy(update={"sha256": digest, "size_bytes": len(canonical_json(manifest).encode())})
                   if item.asset_role is GeometryResourceAssetRole.MANIFEST else item for item in bundle.assets
                   if kind is GeometryResourceKind.MAPPED_MODEL or item.asset_role is not GeometryResourceAssetRole.SAMPLE_OUTPUT)
    bundle = bundle.model_copy(update={"assets": assets, "version": bundle.version.model_copy(update={"manifest": manifest,
        "manifest_digest": digest, "content_digest": geometry_resource_content_digest(
            resource_kind=bundle.resource.resource_kind, manifest_digest=digest, assets=assets)})})
    version = GeometryCatalogVersionSummary(
        resource_version_ref="cur.opaque-version-alias", version_no=bundle.version.version_no,
        origin_kind=bundle.version.origin_kind, content_digest=bundle.version.content_digest,
        manifest_digest=bundle.version.manifest_digest, source_available=True,
        preview_available=kind is GeometryResourceKind.MODELING_FUNCTION, created_at=bundle.version.created_at,
    )
    root = GeometryCatalogResourceSummary(
        resource_root_ref="cur.opaque-root-alias", resource_kind=bundle.resource.resource_kind,
        owner_scope=bundle.resource.owner_scope.scope_kind,
        display_name=bundle.resource.display_name, latest_version=version,
        created_at=bundle.resource.created_at,
    )
    root_info = GeometryCatalogRootInfo(
        resource=root, recent_versions=(version,), recent_versions_complete=True,
    )
    root_result = _geometry_info({
        "catalog": root_info.model_dump(mode="json"),
        "version_bundles": [bundle.model_dump(mode="json")],
    }).model_dump(mode="json")
    source_asset = next(
        asset for asset in bundle.assets if asset.asset_role.value == "source_bundle"
    )
    source = GeometrySourceReadResult(
        resource_version_ref=version.resource_version_ref,
        bundle_sha256=source_asset.sha256,
        members=(GeometrySourceMember(
            source_path="main.py", size_bytes=32,
            sha256=hashlib.sha256(b"def build(ctx):\n    return shape\n").hexdigest(),
            text_available=True,
        ),),
    )
    version_info = GeometryCatalogVersionInfo(
        resource_root_ref=root.resource_root_ref,
        resource_kind=root.resource_kind, owner_scope=root.owner_scope,
        display_name=root.display_name, version=version,
        manifest=bundle.version.manifest, lineage=bundle.version.lineage,
        assets=tuple(GeometryCatalogAssetSummary.model_validate(
            asset.model_dump(exclude={"resource_version_id", "created_at"}))
                     for asset in bundle.assets),
    )
    version_result = _geometry_info({
        "catalog": version_info.model_dump(mode="json"),
        "bundle": bundle.model_dump(mode="json"),
        "source": source.model_dump(mode="json"),
        "parameter_schema_json": canonical_json({"type": "object"}) if kind is GeometryResourceKind.MODELING_FUNCTION else None,
        "geometry_inputs": [],
        "publication": None,
    }).model_dump(mode="json")
    for public in (root_result, version_result):
        encoded = json.dumps(public)
        assert "operation_id" not in encoded
        assert "resource_id" not in encoded
        assert "resource_version_id" not in encoded
        assert "lineage" not in encoded
        assert "manifest" not in encoded
    assert version_result["info"]["resource_version_alias"] == "cur.opaque-version-alias"
    assert version_result["info"]["source_files"][0]["source_path"] == "main.py"
    if kind is GeometryResourceKind.MODELING_FUNCTION:
        assert version_result["info"]["parameter_schema_json"] == '{"type":"object"}'
        assert version_result["info"]["geometry_format"] is None
        assert version_result["info"]["bbox"] is None
        assert version_result["info"]["topology"] is None
        assert version_result["info"]["preview_available"] is True


def test_runner_family_preview_projects_real_domain_outcomes_without_internal_ids() -> None:
    request = TypeAdapter(ProductRealizationPreviewInput).validate_python({
        "mode": "single", "source": {"source_kind": "published", "family_version_alias": "cur.family_version_ref_001"},
        "attachment_kind": "self_geometry", "invocation_json": json.dumps({"product_origin": [0, 0, 0], "length_unit": {"name": "METRE"}}),
    })
    for status in ProductRealizationStatus:
        outcome = _outcome(status)
        payload = {
            "realization": outcome.realization.model_dump(mode="json"),
            "preview": None,
        }
        if outcome.preview is not None:
            payload["preview"] = outcome.preview.model_dump(mode="json", exclude={"content"})
            payload["image_base64"] = base64.b64encode(outcome.preview.content).decode()
        result, image = _family_preview(payload, request)
        public = json.dumps(result.model_dump(mode="json"))
        assert "family_version_id" not in public
        assert "geometry_resource_version_id" not in public
        assert "operation_id" not in public
        assert "lineage" not in public
        assert image == (None if outcome.preview is None else outcome.preview.content)


@pytest.mark.parametrize("kind", ("finite_segment", "fixed_point", "infinite_axis"))
def test_runner_family_preview_reuses_the_same_installation_targets_as_direct(kind) -> None:
    from tests.unit.mcp.test_direct_ifc_product_realization_preview_provider import _self_geometry_provider_request
    from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import validate_product_realization_preview_provider_input
    from CAD2BIM_mcp.runner.tool_projection import runner_public_request

    provider = _self_geometry_provider_request(kind)
    direct = validate_product_realization_preview_provider_input(provider)
    public = runner_public_request("ifc_product_realization_preview", provider)
    request = TypeAdapter(ProductRealizationPreviewInput).validate_python(public["request"])
    domain = authoring_domain_request("ifc_product_realization_preview", request)
    assert domain.invocation.external_constraints == direct.external_constraints
    assert domain.invocation.public_parameters == direct.public_parameters
    assert "direct_session_alias" not in request.model_dump()


async def test_authoring_geometry_list_uses_parent_authorization_and_identity() -> None:
    identity = TenantExecutionContext(
        tenant_id="tenant-1", principal_id="principal-1", task_id="task-1",
        actor_kind=TenantActorKind.RUNNER, command_id="action-1",
    )
    authorized_names = []
    bound = []

    async def authorized(_request, *, tool_name):
        authorized_names.append(tool_name)
        return SimpleNamespace(owner_id="tenant-1", run_id="run-1"), identity

    @contextmanager
    def bind_identity(value):
        bound.append(value)
        yield

    class Catalog:
        async def list(self, context, query, cursor):
            assert context == identity and query.page_size == 20 and cursor is None
            return SimpleNamespace(model_dump=lambda **_kwargs: {"resources": [], "returned_count": 0, "next_cursor": None})

    authoring = SimpleNamespace(geometry_catalog=Catalog())
    app = FastAPI()
    app.include_router(create_internal_authoring_router(
        authorized=authorized, bind_identity=bind_identity,
        authoring=authoring, data_runtime=SimpleNamespace(),
    ), prefix="/internal/domain")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/internal/domain/authoring/ifc_geometry_resource_list", json={})
    assert response.status_code == 200, response.text
    assert authorized_names == ["ifc_geometry_resource_list"]
    assert bound == [identity]
    assert response.json()["resources"] == []


@pytest.mark.parametrize("failure", [None, "resolve", "repair"])
async def test_runner_geometry_query_repair_uses_exact_parent_and_existing_service(failure):
    from CAD2BIM_backend.application.geometry_query_service import GeometryQueryApplicationError, GeometryQueryService
    from CAD2BIM_backend.domain.geometry_query import GeometryQueryPage

    identity = TenantExecutionContext(tenant_id="tenant-1", principal_id="principal-1", task_id="task-1",
        actor_kind=TenantActorKind.RUNNER, command_id="saved-repair")
    authorized_names = []

    async def authorized(_request, *, tool_name):
        authorized_names.append(tool_name)
        return SimpleNamespace(owner_id="tenant-1", run_id="run-1", action_id="saved-repair"), identity

    service = AsyncMock(spec_set=GeometryQueryService)
    service.resolve_attempt_alias.return_value = "original-parent-id"
    service.repair.return_value = GeometryQueryPage(
        attempt_alias="cur.repaired-attempt", target_kind="geometry_inspection",
        inspection_ref="cur.original-inspection", selection={"selection_kind": "whole"},
        coordinate_space={"coordinate_space": "result"}, source_sha256="b" * 64,
        geometry_sha256="c" * 64, result_bytes=2, total_count=0, returned_count=0, results=(),
    )
    if failure is not None:
        owner_error = GeometryQueryApplicationError(
            "GEOMETRY_QUERY_ATTEMPT_SCOPE_MISMATCH" if failure == "resolve" else "GEOMETRY_QUERY_INVALID",
            "Original parent rejected" if failure == "resolve" else "Original repaired source failed",
            phase="preflight" if failure == "resolve" else "execution",
            attempt_alias=None if failure == "resolve" else "cur.repaired-attempt",
        )
        getattr(service, "resolve_attempt_alias" if failure == "resolve" else "repair").side_effect = owner_error
    app = FastAPI()
    app.include_router(create_internal_authoring_router(authorized=authorized, bind_identity=lambda _: nullcontext(),
        authoring=SimpleNamespace(geometry_query=service), data_runtime=SimpleNamespace()), prefix="/internal/domain")
    body = {
        "mode": "repair", "parent_attempt_alias": "cur.original-parent",
        "edits": [{"old_text": f"solids[0].isInside(({x}, 0.0, z), 1e-9, True)",
                   "new_text": f"solids[0].isInside(({x}, 0.0, z), 1e-9)"} for x in ("0.0", "0.039", "0.050")],
        "page_size": 100,
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        response = await client.post("/internal/domain/authoring/ifc_geometry_query_python", json=body)
    assert authorized_names == ["ifc_geometry_query_python"]
    scope, parent_alias = service.resolve_attempt_alias.await_args.args
    assert scope.identity == identity and scope.run_id == "run-1"
    assert parent_alias == body["parent_attempt_alias"]
    service.fresh.assert_not_called()
    service.fresh_inspection.assert_not_called()
    service.fresh_family_standard_model.assert_not_called()
    if failure == "resolve":
        service.repair.assert_not_called()
    else:
        actual_scope, parent_id, edits, action_id, page_size = service.repair.await_args.args
        assert actual_scope == scope and parent_id == "original-parent-id"
        assert action_id == "saved-repair" and page_size == 100
        assert [{"old_text": edit.old_text, "new_text": edit.new_text} for edit in edits] == body["edits"]
    if failure is None:
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["parent_attempt_alias"] == body["parent_attempt_alias"]
        assert result["attempt_alias"] == "cur.repaired-attempt"
        assert result["target"] == {"target_kind": "geometry_inspection", "inspection_ref": "cur.original-inspection",
            "selection": {"selection_kind": "whole"}, "coordinate_space": {"coordinate_space": "result"}}
    else:
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == owner_error.code and detail["phase"] == owner_error.phase
        assert detail["attempt_alias"] == owner_error.attempt_alias


async def test_runner_geometry_query_fresh_resource_resolves_exact_version_through_existing_service():
    from CAD2BIM_backend.application.geometry_query_service import GeometryQueryService
    from CAD2BIM_backend.domain.geometry_query import GeometryQueryPage

    identity = TenantExecutionContext(tenant_id="tenant-1", principal_id="principal-1", task_id="task-1",
        actor_kind=TenantActorKind.RUNNER, command_id="saved-query")

    async def authorized(_request, *, tool_name):
        assert tool_name == "ifc_geometry_query_python"
        return SimpleNamespace(owner_id="tenant-1", run_id="run-1", action_id="saved-query"), identity

    service = AsyncMock(spec_set=GeometryQueryService)
    service.fresh.return_value = GeometryQueryPage(attempt_alias="cur.query-attempt",
        resource_version_id="exact-version", source_sha256="b" * 64, geometry_sha256="c" * 64,
        result_bytes=2, total_count=0, returned_count=0, results=())
    bundle = SimpleNamespace(resource=SimpleNamespace(resource_id="exact-resource"),
        version=SimpleNamespace(resource_version_id="exact-version"))
    catalog = SimpleNamespace(resolve_exact_version=AsyncMock(return_value=bundle), version_ref=Mock(return_value="cur.exact-version"))
    resources = SimpleNamespace(get_version_by_id=AsyncMock(return_value=bundle))
    app = FastAPI()
    app.include_router(create_internal_authoring_router(authorized=authorized, bind_identity=lambda _: nullcontext(),
        authoring=SimpleNamespace(geometry_query=service, geometry_catalog=catalog, geometry_resources=resources),
        data_runtime=SimpleNamespace()), prefix="/internal/domain")
    body = {"mode": "fresh", "target": {"target_kind": "geometry_resource_version", "resource_version_alias": "cur.exact-version"},
        "code": "def query(ctx):\n    ctx.emit({})\n", "page_size": 17}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        response = await client.post("/internal/domain/authoring/ifc_geometry_query_python", json=body)
    assert response.status_code == 200, response.text
    catalog.resolve_exact_version.assert_awaited_once_with(identity, "cur.exact-version")
    args = service.fresh.await_args.args
    assert args[0].identity == identity and args[0].run_id == "run-1"
    assert args[1:] == ("exact-resource", "exact-version", body["code"], "saved-query", 17)
    assert response.json()["target"] == body["target"]


@pytest.mark.parametrize("kind", ["geometry", "family"])
async def test_runner_write_http_saves_draft_through_shared_service_and_maps_cas(kind):
    from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftError
    identity = TenantExecutionContext(tenant_id="tenant-1", principal_id="principal-1", task_id="task-1",
        actor_kind=TenantActorKind.RUNNER, command_id="saved-write")
    access = SimpleNamespace(owner_id="tenant-1", run_id="run-1", action_id="saved-write")
    async def authorized(_request, *, tool_name):
        return access, identity
    @contextmanager
    def bind_identity(_):
        yield
    committed = SimpleNamespace(draft=SimpleNamespace(generation=2), operation=SimpleNamespace(catalog_entry=SimpleNamespace(alias="op_001")))
    service = SimpleNamespace(save_draft_from_aliases=AsyncMock(return_value=committed), draft_reference=Mock(return_value="cur.draft"))
    app = FastAPI()
    app.include_router(create_internal_authoring_router(authorized=authorized, bind_identity=bind_identity,
        authoring=SimpleNamespace(geometry_authoring=service, family_authoring=service), data_runtime=SimpleNamespace()), prefix="/internal/domain")
    request = {"mode": "update", "draft_ref": "cur.draft", "expected_edit_token": 1, "display_name": "Updated"}
    tool = "ifc_geometry_resource_write" if kind == "geometry" else "ifc_product_family_write"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/internal/domain/authoring/" + tool, json=request)
        assert response.status_code == 200, response.text
        assert response.json()["edit_token"] == 2 and response.json()["runtime_checked"] is False
        assert service.save_draft_from_aliases.await_args.kwargs == {"declaration": request, "request_identity": "saved-write"}
        service.save_draft_from_aliases.side_effect = AuthoringDraftError("AUTHORING_DRAFT_CHANGED", "Read the current draft")
        response = await client.post("/internal/domain/authoring/" + tool, json=request)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "AUTHORING_DRAFT_CHANGED"


@pytest.mark.parametrize("name", ["ifc_geometry_resource_write", "ifc_product_family_write"])
async def test_registered_runner_write_preserves_omitted_fields(monkeypatch, name):
    monkeypatch.setattr("CAD2BIM_mcp.runner.authoring_tools.request_domain_credential", lambda: "credential")
    backend = SimpleNamespace(call_authoring=AsyncMock(return_value={
        "draft_ref":"cur.draft", "edit_token":2, "operation_alias":"op_001",
        "saved":True, "runtime_checked":False,
    }))
    server = FastMCP("runner-omission")
    register_runner_authoring_tools(server, backend)
    request = {"mode":"update", "draft_ref":"cur.draft", "expected_edit_token":1,
        "display_name":"Only this field changes"}
    async with Client(server) as client:
        result = await client.call_tool(name, {"request":request})
    assert not result.is_error
    assert backend.call_authoring.await_args.kwargs["body"] == request


async def test_runner_keeps_actionable_literal_edit_diagnostic(monkeypatch):
    from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringApplicationError
    from CAD2BIM_backend.internal_authoring_api import _domain_error
    from CAD2BIM_mcp.runner.backend_client import BackendDomainError

    diagnostic = {"field":"usage_instructions", "failure":"match_not_found", "edit_index":2}
    projected = _domain_error(ProductFamilyAuthoringApplicationError(
        "PRODUCT_FAMILY_INPUT_INVALID", "Original text was not found", diagnostic=diagnostic))
    assert projected.detail["diagnostic"] == diagnostic
    backend = SimpleNamespace(call_authoring=AsyncMock(side_effect=BackendDomainError(
        projected.status_code, projected.detail)))
    monkeypatch.setattr("CAD2BIM_mcp.runner.authoring_tools.request_domain_credential", lambda: "credential")
    server = FastMCP("runner-edit-error")
    register_runner_authoring_tools(server, backend)
    async with Client(server) as client:
        result = await client.call_tool("ifc_product_family_write", {"request":{
            "mode":"update", "draft_ref":"cur.draft", "expected_edit_token":1,
            "usage_instructions":{"action":"edit", "edits":[{"old_text":"Missing", "new_text":"New"}]}}},
            raise_on_error=False)
    assert result.is_error
    message = " ".join(item.text for item in result.content if hasattr(item,"text"))
    assert json.loads(message.split("; diagnostic=", 1)[1]) == diagnostic
    assert "phase=preflight" in message


@pytest.mark.parametrize("name,contract,public_request,domain_field", [
    ("ifc_geometry_preview", GeometryPreviewInput,
     {"mode": "single", "source": {"source_kind": "draft", "draft_ref": "cur.draft"},
      "sample_parameters_json": '{"radius_m":0.2}', "retain": True}, "parameters"),
    ("ifc_product_realization_preview", ProductRealizationPreviewInput,
     {"mode": "single", "attachment_kind": "standard_model", "source": {"source_kind": "draft", "draft_ref": "cur.draft"},
      "invocation_json": '{"public_parameters":{},"product_origin":[0,0,0],"length_unit":{"name":"METRE"}}', "retain": True}, "invocation"),
    ("ifc_geometry_resource_write", GeometryWriteInput,
     {"mode": "update", "draft_ref": "cur.draft", "expected_edit_token": 2,
      "source_changes": [{"change_kind": "add_member", "path": "helper.py", "source": "VALUE = 1"}]}, "source_changes"),
    ("ifc_product_family_write", FamilyWriteInput,
     {"mode": "update", "draft_ref": "cur.draft", "expected_edit_token": 2,
      "source_changes_json": '{"changes":[{"change_kind":"add_member","path":"helper.py","source":"VALUE = 1"}]}'}, "source_changes"),
])
def test_runner_public_documents_convert_to_existing_domain(name, contract, public_request, domain_field):
    public = TypeAdapter(contract).validate_python(public_request)
    domain = authoring_domain_request(name, public).model_dump(mode="json", exclude_unset=True)
    assert domain_field in domain and domain["mode"] == public_request["mode"]
    if domain_field == "source_changes":
        assert domain[domain_field][0]["path"] == "helper.py"
    assert "direct_session_alias" not in json.dumps(domain)


@pytest.mark.parametrize("contract,invalid", [
    (GeometryWriteInput, {"mode":"update", "draft_ref":"cur.draft", "expected_edit_token":1,
         "source_changes":[{"change_kind":"add_member", "source_path":"helper.py", "source":"VALUE = 1"}]}),
    (FamilyWriteInput, {"mode":"update", "draft_ref":"cur.draft", "expected_edit_token":1,
         "definition":{"action":"edit", "geometry_bindings":{"remove":["one"]}}}),
    (ProductRealizationPreviewInput, {"mode":"single", "source":{"source_kind":"draft", "draft_ref":"cur.draft"},
         "invocation":{"product_origin":[0,0,0], "length_unit":{"name":"METRE"}}}),
])
def test_runner_rejects_replaced_public_shapes(contract, invalid):
    with pytest.raises(ValidationError):
        TypeAdapter(contract).validate_python(invalid)


async def test_runner_public_declarations_share_bootstrap_projection_and_multimodal_result_contract():
    from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
    from langchain_core.utils.function_calling import convert_to_openai_tool

    server = FastMCP("public-contract")
    register_runner_authoring_tools(server, SimpleNamespace())
    tools = {tool.name: project_runner_tool_declaration(tool) for tool in await server.list_tools(run_middleware=False)}
    for name, tool in tools.items():
        schema = convert_to_openai_tool(convert_mcp_tool_to_langchain_tool(object(), tool.to_mcp_tool()))["function"]["parameters"]
        assert not _unsupported_schema(schema) and not _unknown_schema_type(schema)
        assert "direct_session_alias" not in json.dumps(schema)
        assert project_runner_tool_declaration(tool).parameters == tool.parameters
    family_schema = tools["ifc_product_family_write"].parameters
    fields = json.dumps(family_schema)
    assert '"definition_json"' in fields and '"source_changes_json"' in fields
    for name in ("ifc_geometry_preview", "ifc_product_realization_preview"):
        assert tools[name].output_schema is None
        assert f"cad2bim://contracts/{name}/result" in tools[name].description


def test_runner_projection_preserves_public_defaults():
    from CAD2BIM_mcp.runner.authoring_contracts import GeometryListInput, FamilyListInput
    for name, contract in (("ifc_geometry_resource_list", GeometryListInput), ("ifc_product_family_list", FamilyListInput)):
        assert authoring_domain_request(name, TypeAdapter(contract).validate_python({})).query.page_size == 50
    request = TypeAdapter(ProductRealizationPreviewInput).validate_python({
        "mode": "single", "attachment_kind": "standard_model", "source": {"source_kind": "draft", "draft_ref": "cur.draft"},
        "invocation_json": '{"product_origin":[0,0,0],"length_unit":{"name":"METRE"}}',
    })
    assert authoring_domain_request("ifc_product_realization_preview", request).mode == "single"
