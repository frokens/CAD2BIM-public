from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from CAD2BIM_backend.application.code_discovery_service import CodeDiscoveryService, CodeKind
from CAD2BIM_backend.application.geometry_catalog_service import GeometryCatalogService
from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionService
from CAD2BIM_backend.application.geometry_source_service import GeometrySourceService
from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService
from CAD2BIM_backend.application.product_family_catalog_service import ProductFamilyCatalogService
from CAD2BIM_backend.application.task_run_service import TaskRunService
from CAD2BIM_backend.domain.control_plane import CreateTaskRequest, StartRunRequest
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.geometry_catalog import GeometryResourceVersionPage
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole, GeometryResourceKind, GeometryResourcePage
from CAD2BIM_backend.domain.identity import AuthenticatedSubject, TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyListEntry, ProductFamilyPage, ProductFamilyVersionAssetRole, ProductFamilyVersionPage,
)
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyProgramSource
from CAD2BIM_backend.domain.resource_scope import ResourceDeletionError, ResourceOwnerScope, ResourceOwnerScopeKind
from CAD2BIM_backend.infrastructure.memory import InMemoryControlPlaneRepository
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context, current_tenant_execution_context
from CAD2BIM_backend.infrastructure.source_text.geometry_authoring import SourceTextGeometryAuthoringSource
from CAD2BIM_backend.library_api import create_library_router
from CAD2BIM_backend.saved_geometry_display_api import authorized_component_read_scope
from tests.unit.backend.test_opaque_reference_service import MemoryReferenceIndex, reference_codec
from tests.unit.backend.test_product_family_catalog_service import _family_bundle, _geometry, SOURCE_BUNDLE


async def _setup():
    repository = InMemoryControlPlaneRepository()
    task = await repository.create_task("tenant-1", CreateTaskRequest(title="Published library"))
    user = TenantExecutionContext(tenant_id="tenant-1", principal_id="principal-1", task_id=task.task_id,
        actor_kind=TenantActorKind.USER, command_id="library")
    with bind_tenant_execution_context(user):
        run = await repository.create_run("tenant-1", task.task_id,
            StartRunRequest(module="component", stage_key="component", idempotency_key="start"),
            execution_protocol_factory=lambda: ExecutionProtocolBinding(bundle_id="component", module="component", stage_key="component"))
    identity_directory = SimpleNamespace(resolve_authenticated_subject=AsyncMock(
        return_value=user.model_copy(update={"task_id": None})))
    task_runs = TaskRunService(repository=repository, identity_directory=identity_directory,
        execution_protocol_factory=lambda *_: run.execution_protocol, bind_identity=bind_tenant_execution_context)
    geometry = _geometry()
    family = _family_bundle()
    family = family.model_copy(update={"family": family.family.model_copy(update={"owner_scope":
        ResourceOwnerScope(scope_kind=ResourceOwnerScopeKind.TENANT, tenant_id="tenant-1")})})

    def owned(result):
        def read(identity, **_):
            assert current_tenant_execution_context() == identity
            assert identity.tenant_id == "tenant-1"
            return result
        return AsyncMock(side_effect=read)

    source_asset = next(asset for asset in geometry.assets if asset.asset_role is GeometryResourceAssetRole.SOURCE_BUNDLE)
    geometry_resources = SimpleNamespace(
        delete_resource=owned(True),
        list_ready_resources=owned(GeometryResourcePage(resources=(geometry.resource,), complete=True)),
        get_latest_version=owned(geometry), get_resource=owned(geometry.resource),
        list_versions=owned(GeometryResourceVersionPage(versions=(geometry,), complete=True)),
        get_exact_version=owned(geometry), get_version_by_id=owned(geometry),
        read_exact_asset=owned((source_asset, SOURCE_BUNDLE)))
    family_source = ProductFamilyProgramSource(family=family.family,
        family_version_id=family.version.family_version_id, version_no=family.version.version_no,
        created_at=family.version.created_at,
        source_asset=next(asset for asset in family.assets if asset.asset_role is ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE))
    family_repository = SimpleNamespace(
        delete_product_family=owned(True),
        list_product_families=owned(ProductFamilyPage(entries=(ProductFamilyListEntry(
            family=family.family, latest_version=family),), complete=True)),
        get_product_family=owned(family.family), get_latest_product_family_version=owned(family),
        list_product_family_versions=owned(ProductFamilyVersionPage(versions=(family,), complete=True)),
        get_exact_product_family_version=owned(family), get_product_family_program_source=owned(family_source))
    codec = reference_codec()
    geometry_catalog = GeometryCatalogService(geometry_resources, codec)
    geometry_source = GeometrySourceService(geometry_catalog, geometry_resources)
    family_assets = SimpleNamespace(verify_bundle=AsyncMock(), read_program_source=AsyncMock(return_value=SOURCE_BUNDLE))
    family_catalog = ProductFamilyCatalogService(family_repository, geometry_resources,
        geometry_catalog, family_assets, codec)
    codes = CodeDiscoveryService(None, geometry_resources, geometry_catalog, geometry_source,
        None, None, None, codec, product_family_catalog=family_catalog,
        product_family_assets=family_assets, source_bundles=SourceTextGeometryAuthoringSource())
    references = OpaqueReferenceService(MemoryReferenceIndex(), codec)
    now = datetime.now(UTC)
    subject = AuthenticatedSubject(issuer="https://identity.test", subject="principal-1", audience="cad2bim",
        authenticated_at=now, token_expires_at=now + timedelta(minutes=5))
    app = FastAPI()
    app.state.library_deletes = {"geometry": geometry_resources.delete_resource,
                                "families": family_repository.delete_product_family}
    app.state.saved_display = SimpleNamespace(definition_source=AsyncMock(return_value=None))
    app.include_router(create_library_router(authenticator=SimpleNamespace(authenticate=lambda _: subject),
        task_runs=task_runs, identity_directory=identity_directory, geometry_catalog=geometry_catalog,
        family_catalog=family_catalog, code_discovery=codes, references=references,
        saved_display=app.state.saved_display,
        bind_identity=bind_tenant_execution_context))
    params = {"task_id": task.task_id, "run_id": run.run_id}
    return app, params, task_runs, codes, references, geometry_catalog, geometry, user


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,items,root_field", [
    ("geometry", "resources", "resource_root_ref"), ("families", "families", "family_root_ref"),
])
async def test_library_deletes_whole_root_with_authenticated_scope_and_no_run(kind, items, root_field):
    app, params, *_ = await _setup()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test",
                                 headers={"authorization": "Bearer valid"}) as client:
        for selected_scope in ({}, params):
            page = await client.get(f"/library/{kind}", params=selected_scope)
            root = page.json()[items][0]
            response = await client.delete(f"/library/{kind}",
                params={**selected_scope, "reference": root[root_field]})
            assert response.status_code == 204, response.text
            assert not response.content
        assert app.state.library_deletes[kind].await_count == 2
        assert app.state.library_deletes[kind].await_args_list[0].args[0].actor_kind is TenantActorKind.USER
        assert app.state.library_deletes[kind].await_args_list[1].args[0].actor_kind is TenantActorKind.RUNNER
        assert (await client.delete(f"/library/{kind}", params={"reference": "unavailable"})).status_code == 404
        assert (await client.delete(f"/library/{kind}", params={**params, "task_id": "another-task", "reference": root[root_field]})).status_code == 404
    assert app.state.library_deletes[kind].await_count == 2
    assert current_tenant_execution_context() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", [
    ("LIBRARY_SYSTEM_RESOURCE", 403), ("LIBRARY_GEOMETRY_IN_USE", 409),
    ("LIBRARY_PUBLICATION_PENDING", 409),
])
async def test_library_deletion_returns_specific_rejection(code, status):
    app, params, *_ = await _setup()
    app.state.library_deletes["geometry"].side_effect = ResourceDeletionError(code, "Deletion is blocked")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test",
                                 headers={"authorization": "Bearer valid"}) as client:
        page = await client.get("/library/geometry", params=params)
        root = page.json()["resources"][0]["resource_root_ref"]
        response = await client.delete("/library/geometry", params={**params, "reference": root})
        assert response.status_code == status
        assert response.json()["detail"] == {"code": code, "message": "Deletion is blocked"}


@pytest.mark.asyncio
async def test_library_deletion_requires_authentication_and_root_reference():
    app, params, *_ = await _setup()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test") as client:
        assert (await client.delete("/library/geometry", params={"reference": "unavailable"})).status_code == 401
        client.headers["authorization"] = "Bearer valid"
        page = await client.get("/library/geometry", params=params)
        version = page.json()["resources"][0]["latest_version"]["resource_version_ref"]
        assert (await client.delete("/library/geometry", params={**params, "reference": version})).status_code == 404
    app.state.library_deletes["geometry"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,items,root_field,version_field,prefix", [
    ("geometry", "resources", "resource_root_ref", "resource_version_ref", "gv"),
    ("families", "families", "family_root_ref", "family_version_ref", "fv"),
])
async def test_library_reads_published_catalog_exact_code_and_display_source(kind, items, root_field, version_field, prefix):
    app, params, *_ = await _setup()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test",
                                 headers={"authorization": "Bearer valid"}) as client:
        page = await client.get(f"/library/{kind}", params=params)
        assert page.status_code == 200, page.text
        root = page.json()[items][0]
        root_info = await client.get(f"/library/{kind}/info", params={**params, "reference": root[root_field]})
        assert root_info.status_code == 200, root_info.text
        assert root_info.json()["code"] is root_info.json()["display_source"] is None
        exact = root["latest_version"][version_field]
        assert exact.startswith(prefix + "_") and len(exact) == 25
        detail = await client.get(f"/library/{kind}/info", params={**params, "reference": exact})
        assert detail.status_code == 200, detail.text
        assert detail.json()["display_source"]["source_ref"] == exact
        code = detail.json()["code"]
        assert code["code_locator"].startswith("sc_") and len(code["code_locator"]) == 25
        assert code["owner_alias"] == exact
        read = await client.post("/library/code", params=params,
            json={"code_locator": code["code_locator"], "source_path": "main.py", "view": {"view_mode": "full"}})
        assert read.status_code == 200, read.text
        assert read.json()["code_locator"] == code["code_locator"]
        assert read.json()["view"]["text"] == "def realize(ctx):\n    return None\n"
        assert read.json()["member_sha256"] == code["members"][0]["sha256"]
        assert "storage_key" not in detail.text and "storage_key" not in read.text
    assert current_tenant_execution_context() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_library_definition_info_selects_one_saved_inspection_or_explicit_empty_state(available):
    app, params, task_runs, _, _, catalog, *_ = await _setup()
    original_info = catalog.info

    async def definition_info(identity, reference):
        result = await original_info(identity, reference)
        return result.model_copy(update={"resource_kind": GeometryResourceKind.MODELING_FUNCTION})

    catalog.info = definition_info
    if available:
        now = datetime.now(UTC)
        subject = AuthenticatedSubject(issuer="https://identity.test", subject="principal-1", audience="cad2bim",
            authenticated_at=now, token_expires_at=now + timedelta(minutes=5))
        scope = await authorized_component_read_scope(task_runs, subject, params["task_id"], params["run_id"])
        exact = GeometryInspectionService(None, None, reference_codec()).reference(scope,
            SimpleNamespace(artifact_id="saved-case", version_id="saved-case-version", sha256="a" * 64))
        app.state.saved_display.definition_source.return_value = {"source_kind": "geometry_inspection",
            "source_ref": exact, "case_key": "default_case", "parameters": {"length_m": 4},
            "title": "Geometry · default_case（默认参数）"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test",
                                 headers={"authorization": "Bearer valid"}) as client:
        page = await client.get("/library/geometry", params=params)
        version = page.json()["resources"][0]["latest_version"]["resource_version_ref"]
        response = await client.get("/library/geometry/info", params={**params, "reference": version})
        assert response.status_code == 200, response.text
        document = response.json()
        if available:
            assert document["display_source"]["source_kind"] == "geometry_inspection"
            assert document["display_source"]["source_ref"].startswith("gi_")
            assert document["display_source"]["case_key"] == "default_case"
            assert document["display_message"] is None
        else:
            assert document["display_source"] is None
            assert document["display_message"] == "参数化几何，尚无该参数的保存模型。"
    assert app.state.saved_display.definition_source.await_count == 1


@pytest.mark.asyncio
async def test_library_without_run_uses_actual_user_scope_without_fabricating_execution():
    app, params, task_runs, codes, *_ = await _setup()
    task_runs.checkpoint_observation_context = AsyncMock(side_effect=AssertionError("no Run may be fabricated"))
    codes.published_program_card = AsyncMock(side_effect=AssertionError("no Run code invocation"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test",
                                 headers={"authorization": "Bearer valid"}) as client:
        for scope in ({}, {"task_id": params["task_id"]}):
            page = await client.get("/library/families", params=scope)
            assert page.status_code == 200, page.text
            version = page.json()["families"][0]["latest_version"]["family_version_ref"]
            assert version.startswith("fv_" if scope else "cur_v1.")
            detail = await client.get("/library/families/info", params={**scope, "reference": version})
            assert detail.status_code == 200, detail.text
            assert detail.json()["code"] is detail.json()["display_source"] is None
            assert (await client.post("/library/code", params=scope,
                json={"code_locator": "unavailable", "view": {"view_mode": "full"}})).status_code == 409
    task_runs.checkpoint_observation_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_library_rejects_foreign_reference_wrong_type_and_non_library_code_before_source_read():
    app, params, task_runs, codes, references, catalog, geometry, _user = await _setup()
    saved_scope = await authorized_component_read_scope(task_runs, "verified", **params)
    identity = saved_scope.identity
    foreign = catalog.version_ref(identity.model_copy(update={"task_id": "another-task"}), geometry)
    other_code = codes._locator(saved_scope, CodeKind.GEOMETRY_QUERY_ATTEMPT,
        "geometry_query_attempt", attempt_alias="unavailable")
    with bind_tenant_execution_context(identity):
        other_code = await references.shorten(identity, other_code, ("sc",))
    codes._geometry_queries = SimpleNamespace(resolve_attempt_alias=AsyncMock(side_effect=AssertionError("not library code")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test") as client:
        assert (await client.get("/library/geometry", params=params)).status_code == 401
        client.headers["authorization"] = "Bearer valid"
        assert (await client.get("/library/geometry", params={"run_id": params["run_id"]})).status_code == 422
        assert (await client.get("/library/geometry", params={**params, "task_id": "another-task"})).status_code == 404
        assert (await client.get("/library/geometry/info", params={**params, "reference": foreign})).status_code == 404
        family = (await client.get("/library/families", params=params)).json()["families"][0]
        assert (await client.get("/library/geometry/info", params={**params,
            "reference": family["latest_version"]["family_version_ref"]})).status_code == 404
        assert (await client.post("/library/code", params=params, json={"code_locator": other_code,
            "source_path": "main.py", "view": {"view_mode": "full"}})).status_code == 404
    codes._geometry_queries.resolve_attempt_alias.assert_not_awaited()
