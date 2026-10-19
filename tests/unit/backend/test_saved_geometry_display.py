from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.geometry_catalog_service import GeometryCatalogService
from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionApplicationError, GeometryInspectionService
from CAD2BIM_backend.application.catalog_scope import catalog_scope_digest
from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService
from CAD2BIM_backend.application.product_family_catalog_service import ProductFamilyCatalogService
from CAD2BIM_backend.application.saved_geometry_display_service import SavedGeometryDisplayService
from CAD2BIM_backend.application.task_run_service import TaskRunService
from CAD2BIM_backend.domain.control_plane import CreateTaskRequest, StartRunRequest, ResourceNotFoundError
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogIntegrityError, GeometryCatalogReferenceError
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogReferenceError
from CAD2BIM_backend.domain.geometry_query import GeometryQueryArtifactRef
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry, CanonicalGeometryFormat, GeometryDisplayMesh, GeometryPreview, GeometryRenderScene,
)
from CAD2BIM_backend.domain.identity import AuthenticatedSubject, TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.memory import InMemoryControlPlaneRepository
from CAD2BIM_backend.infrastructure.data_query.cursor_codec import CursorCodec
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context, current_tenant_execution_context
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from CAD2BIM_backend.saved_geometry_display_api import create_saved_geometry_display_router
from tests.unit.backend.test_opaque_reference_service import MemoryReferenceIndex


async def _setup(tmp_path):
    repository = InMemoryControlPlaneRepository()
    task = await repository.create_task("tenant", CreateTaskRequest(title="saved geometry"))
    context = TenantExecutionContext(tenant_id="tenant", principal_id="reader", task_id=task.task_id,
        actor_kind=TenantActorKind.USER, command_id="display")
    with bind_tenant_execution_context(context):
        run = await repository.create_run("tenant", task.task_id,
            StartRunRequest(module="component", stage_key="component", idempotency_key="start"),
            execution_protocol_factory=lambda: ExecutionProtocolBinding(bundle_id="component", module="component", stage_key="component"))
    scope = InvocationScope(identity=context.model_copy(update={"actor_kind": TenantActorKind.RUNNER}),
        owner_id="tenant", run_id=run.run_id,
        execution_mode=run.execution_mode, catalog_frontier=None)
    artifacts = ArtifactService(repository, LocalStorage(tmp_path / "assets"))
    payload = b"saved immutable BREP owner test"
    digest = hashlib.sha256(payload).hexdigest()
    png = GeometryPreview(width_px=720, height_px=480, payload=b"saved PNG", sha256=hashlib.sha256(b"saved PNG").hexdigest())
    stl = GeometryDisplayMesh(payload=b"s" * 84, sha256=hashlib.sha256(b"s" * 84).hexdigest(), geometry_sha256=digest)
    runtime = SimpleNamespace(render=AsyncMock(return_value=png), export_stl=AsyncMock(return_value=stl),
        release_execution=Mock(), build_fixed=AsyncMock(side_effect=AssertionError("author execution is forbidden")))
    bundle = SimpleNamespace(resource=SimpleNamespace(resource_kind=GeometryResourceKind.MAPPED_MODEL,
        resource_id="geometry", display_name="保存构件"), version=SimpleNamespace(resource_version_id="geometry-exact"))
    catalog = SimpleNamespace(resolve_exact_version=AsyncMock(return_value=bundle))
    resources = SimpleNamespace(read_exact_asset=AsyncMock(return_value=(SimpleNamespace(
        asset_format=CanonicalGeometryFormat.OCCT_BREP_V1.value,
        content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type, sha256=digest), payload)))
    display = SavedGeometryDisplayService(artifacts=artifacts, geometry_runtime=runtime,
        geometry_catalog=catalog, geometry_resources=resources, family_catalog=None, family_assets=None, inspections=None)
    return display, artifacts, runtime, catalog, scope, context, task, run


@pytest.mark.asyncio
async def test_display_pair_binds_same_saved_source_and_reuses_exact_task_artifacts_after_restart(tmp_path):
    display, artifacts, runtime, catalog, scope, *_ = await _setup(tmp_path)
    result = await display.project(scope, "geometry_resource_version", "authorized-exact-version")
    geometry = runtime.render.await_args.args[0].geometry
    assert runtime.export_stl.await_args.args[0] == geometry
    assert result["geometry_sha256"] == geometry.sha256
    assert result["unit"] == "METRE"
    for format in ("png", "stl"):
        item = result[format]
        downloaded = await artifacts.download_version(scope.owner_id, item["artifact_id"], item["version_id"])
        assert downloaded.artifact.version_metadata["geometry_sha256"] == geometry.sha256
        assert item["content_url"].startswith(f"/tasks/{scope.identity.task_id}/artifacts/")
        assert "storage_key" not in item and "path" not in item
    # Reconstruct the reader, as after a backend restart. The original Artifact
    # identity is recovered without a new render, geometry author or repository.
    restored = SavedGeometryDisplayService(artifacts=artifacts, geometry_runtime=runtime,
        geometry_catalog=catalog, geometry_resources=display._geometry_resources,
        family_catalog=None, family_assets=None, inspections=None)
    assert await restored.read(scope, "geometry_resource_version", "authorized-exact-version") == result
    assert await restored.project(scope, "geometry_resource_version", "authorized-exact-version") == result
    assert runtime.render.await_count == runtime.export_stl.await_count == 1
    runtime.build_fixed.assert_not_awaited()
    catalog.resolve_exact_version.assert_awaited_with(scope.identity, "authorized-exact-version")


@pytest.mark.asyncio
async def test_failed_stl_does_not_publish_partial_pair_and_retry_reuses_original_png(tmp_path):
    display, artifacts, runtime, _catalog, scope, *_ = await _setup(tmp_path)
    original = runtime.export_stl.return_value
    runtime.export_stl.side_effect = ValueError("actual meshing failure")
    with pytest.raises(ValueError, match="actual meshing failure"):
        await display.project(scope, "geometry_resource_version", "authorized-exact-version")
    assert await display.read(scope, "geometry_resource_version", "authorized-exact-version") is None
    runtime.export_stl.side_effect = None
    runtime.export_stl.return_value = original
    result = await display.project(scope, "geometry_resource_version", "authorized-exact-version")
    assert await display.read(scope, "geometry_resource_version", "authorized-exact-version") == result
    assert runtime.render.await_count == 1
    assert runtime.export_stl.await_count == 2


@pytest.mark.asyncio
async def test_definition_source_selects_saved_normalized_defaults_without_executing(tmp_path):
    display, _, runtime, catalog, scope, *_ = await _setup(tmp_path)
    catalog.resolve_exact_version.return_value.resource.resource_kind = GeometryResourceKind.MODELING_FUNCTION
    schema = {"type": "object", "properties": {"length_m": {"type": "number", "default": 4}},
        "required": ["length_m"], "additionalProperties": False}
    display._geometry_resources.read_exact_asset.return_value = (SimpleNamespace(
        asset_format="json_schema", content_type="application/schema+json"), json.dumps(schema).encode())
    source = {"source_kind": "geometry_inspection", "source_ref": "exact-saved-inspection",
        "case_key": "nominal", "parameters": {"length_m": 4}}
    display._inspections = SimpleNamespace(saved_version_display_source=AsyncMock(return_value=source))
    assert await display.definition_source(scope, "definition-version") == source
    display._inspections.saved_version_display_source.assert_awaited_once_with(
        scope, catalog.resolve_exact_version.return_value, {"length_m": 4})
    runtime.render.assert_not_awaited()
    runtime.build_fixed.assert_not_awaited()
    with pytest.raises(ValueError, match="no saved canonical entity"):
        await display.project(scope, "geometry_resource_version", "definition-version")


@pytest.mark.asyncio
async def test_definition_without_defaults_is_empty_but_invalid_schema_is_integrity_error(tmp_path):
    display, _, runtime, catalog, scope, *_ = await _setup(tmp_path)
    catalog.resolve_exact_version.return_value.resource.resource_kind = GeometryResourceKind.MODELING_FUNCTION
    schema = {"type": "object", "properties": {"length_m": {"type": "number"}},
        "required": ["length_m"], "additionalProperties": False}
    asset = SimpleNamespace(asset_format="json_schema", content_type="application/schema+json")
    display._geometry_resources.read_exact_asset.return_value = asset, json.dumps(schema).encode()
    display._inspections = SimpleNamespace(saved_version_display_source=AsyncMock())
    assert await display.definition_source(scope, "definition-version") is None
    display._inspections.saved_version_display_source.assert_not_awaited()
    schema["properties"]["length_m"]["default"] = "invalid number"
    display._geometry_resources.read_exact_asset.return_value = asset, json.dumps(schema).encode()
    with pytest.raises(GeometryCatalogIntegrityError, match="default violates its schema"):
        await display.definition_source(scope, "definition-version")
    runtime.build_fixed.assert_not_awaited()


@pytest.mark.asyncio
async def test_display_api_authenticates_run_and_rejects_unavailable_or_foreign_scope(tmp_path):
    display, _artifacts, runtime, _catalog, scope, context, task, run = await _setup(tmp_path)
    now = datetime.now(UTC)
    subject = AuthenticatedSubject(issuer="https://identity.test", subject="reader", audience="cad2bim",
        authenticated_at=now, token_expires_at=now + timedelta(minutes=5))
    task_runs = SimpleNamespace(authorized_run_context=AsyncMock(return_value=(context, run)),
        checkpoint_observation_context=AsyncMock(side_effect=_artifacts._repository.get_observed_run))
    codec = CursorCodec(base64.urlsafe_b64encode(b"d" * 32).rstrip(b"=").decode())
    references = OpaqueReferenceService(MemoryReferenceIndex(), codec)
    app = FastAPI()
    app.include_router(create_saved_geometry_display_router(authenticator=SimpleNamespace(authenticate=lambda _token: subject),
        task_runs=task_runs, display=display, references=references, bind_identity=bind_tenant_execution_context))
    path = f"/tasks/{task.task_id}/runs/{run.run_id}/display"
    body = {"source_kind": "geometry_resource_version",
        "source_ref": GeometryCatalogService(None, codec).version_ref(scope.identity, _catalog.resolve_exact_version.return_value)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test") as client:
        assert (await client.post(path, json=body)).status_code == 401
        client.headers["authorization"] = "Bearer valid"
        assert (await client.get(path, params=body)).status_code == 404
        response = await client.post(path, json=body)
        assert response.status_code == 200
        assert (await client.get(path, params=body)).json() == response.json()
        task_runs.authorized_run_context.side_effect = ResourceNotFoundError("foreign Task")
        assert (await client.post(path, json=body)).status_code == 404
        assert runtime.render.await_count == runtime.export_stl.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind,short_kind", [("geometry_resource_version", "gv"),
    ("family_standard_model", "fv"), ("geometry_inspection", "gi")])
async def test_display_authorized_user_reads_actual_runner_signed_reference_for_only_its_task(tmp_path, source_kind, short_kind):
    display, artifacts, runtime, catalog, scope, context, task, run = await _setup(tmp_path)
    codec = CursorCodec(base64.urlsafe_b64encode(b"d" * 32).rstrip(b"=").decode())
    foreign_identity = scope.identity.model_copy(update={"task_id": "another-task"})
    user_scope = scope.model_copy(update={"identity": context})
    if source_kind == "geometry_resource_version":
        bundle = catalog.resolve_exact_version.return_value

        async def get_exact_version(identity, **locator):
            assert current_tenant_execution_context() == identity
            assert identity.actor_kind is TenantActorKind.RUNNER
            assert locator == {"resource_id": "geometry", "resource_version_id": "geometry-exact"}
            return bundle

        actual_catalog = GeometryCatalogService(SimpleNamespace(get_exact_version=get_exact_version), codec)
        source_ref = actual_catalog.version_ref(scope.identity, bundle)
        foreign_ref = actual_catalog.version_ref(foreign_identity, bundle)
        user_ref = actual_catalog.version_ref(context, bundle)
        with pytest.raises(GeometryCatalogReferenceError):
            await actual_catalog.resolve_exact_version(context, source_ref)
        display._geometry_catalog = actual_catalog
    elif source_kind == "family_standard_model":
        bundle = SimpleNamespace(family=SimpleNamespace(family_id="family"), version=SimpleNamespace(
            family_version_id="family-exact", display_name="Saved family",
            family_contract=SimpleNamespace(standard_model=object())))

        async def get_exact_family(identity, **locator):
            assert current_tenant_execution_context() == identity
            assert identity.actor_kind is TenantActorKind.RUNNER
            assert locator == {"family_id": "family", "family_version_id": "family-exact"}
            return bundle

        actual_catalog = ProductFamilyCatalogService(
            repository=SimpleNamespace(get_exact_product_family_version=get_exact_family),
            geometry_resources=None, geometry_catalog=None, family_assets=None, cursor_codec=codec)
        source_ref = actual_catalog.version_ref(scope.identity, bundle)
        foreign_ref = actual_catalog.version_ref(foreign_identity, bundle)
        user_ref = actual_catalog.version_ref(context, bundle)
        with pytest.raises(ProductFamilyCatalogReferenceError):
            await actual_catalog.resolve_exact_version(context, source_ref, verify_assets=False)
        asset, payload = await display._geometry_resources.read_exact_asset(scope.identity)
        manifest = SimpleNamespace(geometry_sha256=asset.sha256, geometry_summary=None)
        display._family_catalog = actual_catalog
        display._family_assets = SimpleNamespace(standard_model_assets=AsyncMock(return_value=(manifest, payload)))
    else:
        inspection = GeometryInspectionService(artifacts, None, codec)
        reference = GeometryQueryArtifactRef(artifact_id="inspection", version_id="exact-inspection", sha256="1" * 64)
        asset, payload = await display._geometry_resources.read_exact_asset(scope.identity)
        loaded = SimpleNamespace(artifact=reference,
            geometry=CanonicalGeometry(format=CanonicalGeometryFormat(asset.asset_format),
                content_type=asset.content_type, payload=payload, sha256=asset.sha256),
            manifest=SimpleNamespace(title="Saved result", case_key="nominal",
                scene=GeometryRenderScene(geometry_sha256=asset.sha256, coordinate_space="geometry_coordinates")))

        async def read_artifact(invocation, exact_reference):
            assert current_tenant_execution_context() == invocation.identity
            assert invocation.identity.actor_kind is TenantActorKind.RUNNER
            assert exact_reference == reference
            return loaded

        inspection.read_artifact = AsyncMock(side_effect=read_artifact)
        source_ref = inspection.reference(scope, reference)
        foreign_ref = inspection.reference(scope.model_copy(update={"identity": foreign_identity}), reference)
        user_ref = inspection.reference(user_scope, reference)
        with pytest.raises(GeometryInspectionApplicationError, match="invalid or unavailable"):
            await inspection.read(user_scope, source_ref)
        display._inspections = inspection

    index = MemoryReferenceIndex()
    references = OpaqueReferenceService(index, codec)
    short_ref = await references.shorten(scope.identity, source_ref, (short_kind,))
    foreign_short = await references.shorten(foreign_identity, foreign_ref, (short_kind,))
    user_short = await references.shorten(context, user_ref, (short_kind,))
    root_ref = codec.encode({"domain": "geometry_catalog_reference", "reference_kind": "resource_root",
        "scope": catalog_scope_digest(scope.identity), "resource_id": "geometry"})
    wrong_kind = await references.shorten(scope.identity, root_ref, ("gr",))
    expand = references.expand
    shorten = references.shorten

    async def bound_expand(identity, value, kinds):
        assert current_tenant_execution_context() == identity
        assert catalog_scope_digest(identity) == catalog_scope_digest(scope.identity)
        return await expand(identity, value, kinds)

    references.expand = AsyncMock(side_effect=bound_expand)

    async def bound_shorten(identity, value, kinds):
        assert current_tenant_execution_context() == identity
        assert catalog_scope_digest(identity) == catalog_scope_digest(scope.identity)
        return await shorten(identity, value, kinds)

    references.shorten = AsyncMock(side_effect=bound_shorten)

    now = datetime.now(UTC)
    subject = AuthenticatedSubject(issuer="https://identity.test", subject="reader", audience="cad2bim",
        authenticated_at=now, token_expires_at=now + timedelta(minutes=5))
    task_runs = TaskRunService(repository=artifacts._repository,
        identity_directory=SimpleNamespace(resolve_authenticated_subject=AsyncMock(
            return_value=context.model_copy(update={"task_id": None}))),
        execution_protocol_factory=lambda *_: run.execution_protocol,
        bind_identity=bind_tenant_execution_context)
    observed = task_runs.checkpoint_observation_context

    async def observe(run_id, thread_id):
        assert current_tenant_execution_context() is None
        assert (run_id, thread_id) == (run.run_id, run.thread_id)
        return await observed(run_id, thread_id)

    task_runs.checkpoint_observation_context = AsyncMock(side_effect=observe)
    app = FastAPI()
    app.include_router(create_saved_geometry_display_router(
        authenticator=SimpleNamespace(authenticate=lambda _token: subject), task_runs=task_runs,
        display=display, references=references, bind_identity=bind_tenant_execution_context))
    path = f"/tasks/{task.task_id}/runs/{run.run_id}/display"
    body = {"source_kind": source_kind, "source_ref": short_ref}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test",
                                 headers={"authorization": "Bearer valid"}) as client:
        response = await client.post(path, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["source"]["source_ref"] == short_ref
        assert (await client.get(path, params=body)).json() == response.json()
        sealed_body = {**body, "source_ref": source_ref}
        assert (await client.get(path, params=sealed_body)).json() == response.json()
        assert (await client.post(path, json=sealed_body)).json() == response.json()
        # A restarted navigation service still reads the same exact manifest and pair.
        expand = OpaqueReferenceService(index, codec).expand
        assert (await client.get(path, params=body)).json() == response.json()
        assert "principal_id" not in response.json() and "actor_kind" not in response.json()
        for invalid in (foreign_ref, foreign_short, user_ref, user_short, root_ref, wrong_kind, short_ref[:-1]):
            assert (await client.get(path, params={**body, "source_ref": invalid})).status_code == 404
            assert (await client.post(path, json={**body, "source_ref": invalid})).status_code == 404
        assert (await client.post(f"/tasks/another-task/runs/{run.run_id}/display", json=body)).status_code == 404
    with bind_tenant_execution_context(scope.identity):
        saved = await display.read(scope, source_kind, source_ref)
    assert saved["source"]["source_ref"] == source_ref
    assert saved == {**response.json(), "source": {**response.json()["source"], "source_ref": source_ref}}
    assert current_tenant_execution_context() is None
    assert runtime.render.await_count == runtime.export_stl.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [{"actor_kind": TenantActorKind.USER},
    {"task_id": "another-task"}, {"principal_id": "another-principal"}, {"tenant_id": "another-tenant"}])
async def test_display_rejects_observer_identity_that_does_not_match_saved_run(tmp_path, changed):
    display, _artifacts, runtime, _catalog, scope, context, task, run = await _setup(tmp_path)
    task_runs = SimpleNamespace(authorized_run_context=AsyncMock(return_value=(context, run)),
        checkpoint_observation_context=AsyncMock(return_value=(scope.identity.model_copy(update=changed), run)))
    app = FastAPI()
    app.include_router(create_saved_geometry_display_router(authenticator=SimpleNamespace(authenticate=lambda _: "verified"),
        task_runs=task_runs, display=display,
        references=OpaqueReferenceService(MemoryReferenceIndex(), CursorCodec(base64.urlsafe_b64encode(b"d" * 32).rstrip(b"=").decode())),
        bind_identity=bind_tenant_execution_context))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test",
                                 headers={"authorization": "Bearer valid"}) as client:
        response = await client.post(f"/tasks/{task.task_id}/runs/{run.run_id}/display",
            json={"source_kind": "geometry_resource_version", "source_ref": "unread-source"})
        assert response.status_code == 404
    runtime.render.assert_not_awaited()
    runtime.export_stl.assert_not_awaited()
