"""Tenant authoring on the real merged migrations and non-superuser roles.

Tests use the existing Component Run route, Application factory and storage.
They never enable MCP, launch a model, or modify the caller's demo database.
"""

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
import os
from unittest.mock import AsyncMock
from uuid import uuid4

from alembic import command
import psycopg
from psycopg import sql
import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftError, AuthoringDraftService
from CAD2BIM_backend.application.geometry_authoring_service import GeometryAuthoringApplicationError
from CAD2BIM_backend.application.geometry_source_service import GeometrySourceService
from CAD2BIM_backend.domain.control_plane import ExecutionMode, StartRunRequest
from CAD2BIM_backend.domain.data_runtime import CriticalOperationKind, CriticalOperationStatus
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogListQuery
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole
from CAD2BIM_backend.domain.identity import TenantActorKind
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.preview_cases import GeometryPreviewCase, GeometryCaseExpectation
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogListQuery
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScopeKind
from CAD2BIM_backend.ifc_application_resources import create_authoring_application_resources
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.postgres.database import EXPECTED_SCHEMA_REVISION
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactVersionModel, CriticalOperationModel, GeometryResourceModel,
    GeometryResourceVersionModel, GeometryResourceVersionAssetModel,
    GeometryResourceVersionInputModel, ProductFamilyGeometryBindingModel,
    ProductFamilyModel, ProductFamilyVersionModel, RunModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository_product_family_authoring import (
    PostgresProductFamilyAuthoringRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    bind_tenant_execution_context, set_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from CAD2BIM_backend.infrastructure.worker_runtime import HostWorkerCapacity
from tests.integration.backend.test_geometry_query_postgres import (
    _Login, _create_logins, _drop_logins, _open_context, _runtime, _runtime_url,
    _sync_url, _task_owner_and_run,
)
from tests.integration.backend.test_geometry_resource_migrations import _isolated_database
from tests.unit.backend.product_family_fixtures import current_family_contract

pytestmark = [pytest.mark.integration, pytest.mark.network]
DATABASE_URL = os.environ.get("CAD2BIM_TEST_DATABASE_URL", "").strip()
_BOX_SOURCE = "import cadquery as cq\ndef build(ctx):\n    return cq.Workplane('XY').box(0.1,0.1,0.1)\n"
_FAMILY_SOURCE = """def construct(ctx):
    return {'status':'SOLVED','components':[{'component_key':'body'}],
            'fragments':[{'fragment_key':'body_shape','binding_key':'body_geometry'}]}
def realize(ctx):
    return construct(ctx)
def standard_model(ctx):
    return construct(ctx)
"""


def test_component_head_really_upgrades_through_tool_branch_and_tenant_authoring():
    with _isolated_database("component_authoring") as (config, engine):
        command.upgrade(config, "20261002_0097")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261002_0097"
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == EXPECTED_SCHEMA_REVISION
            assert connection.scalar(text("SELECT to_regprocedure('cad2bim_authoring_publication_fence(text,text)')"))
            assert connection.scalar(text("SELECT to_regprocedure('cad2bim_guard_authoring_publication_operation()')"))
            assert connection.scalar(text("SELECT to_regprocedure('cad2bim_observed_run_identity(text,text)')")) is not None


@dataclass
class _Environment:
    admin: object
    mcp: object
    runner: object
    api: object
    resources: object
    storage: LocalStorage
    runner_url: str = field(repr=False)


def _resources(repository, storage, tmp_path):
    return create_authoring_application_resources(
        repository=repository, storage=storage, artifacts=ArtifactService(repository, storage),
        cursor_codec=CursorCodec(base64.urlsafe_b64encode(b"t" * 32).rstrip(b"=").decode()),
        capacity=HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path / "capacity"),
    )


@pytest_asyncio.fixture
async def environment(tmp_path):
    if not DATABASE_URL:
        pytest.skip("isolated migrated CAD2BIM_TEST_DATABASE_URL is required")
    logins = _create_logins(DATABASE_URL)
    suffix = uuid4().hex[:12]
    logins["runner"] = _Login(f"component_runner_{suffix}", "cad2bim_runner_runtime", f"component-{suffix}")
    with psycopg.connect(_sync_url(DATABASE_URL)) as connection:
        login = logins["runner"]
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}").format(
                sql.Identifier(login.name), sql.Literal(login.password)))
        connection.execute(sql.SQL("GRANT {} TO {}").format(
            sql.Identifier(login.privilege), sql.Identifier(login.name)))
    admin = create_async_engine(DATABASE_URL)
    mcp, runner, api = (_runtime(_runtime_url(DATABASE_URL, logins[key])) for key in ("mcp", "runner", "api"))
    storage = LocalStorage(tmp_path / "storage")
    env = _Environment(admin, mcp, runner, api, _resources(runner.repository, storage, tmp_path), storage,
        _runtime_url(DATABASE_URL, logins["runner"]))
    try:
        async with runner.engine.connect() as connection:
            assert (await connection.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one() == (False, False)
        yield env
    finally:
        await env.resources.close()
        for runtime in (env.mcp, env.runner, env.api):
            await runtime.close()
        await admin.dispose()
        _drop_logins(DATABASE_URL, logins)


async def _scope(env, *, subject, name, component=True):
    identity = await _open_context(env.mcp, subject=subject, session_name=name)
    owner, setup_run = await _task_owner_and_run(env.admin, identity)
    if not component:
        return InvocationScope(identity=identity, owner_id=owner, run_id=setup_run,
            execution_mode=ExecutionMode.DIRECT_MCP, catalog_frontier=None)
    async with env.admin.begin() as connection:
        await connection.execute(update(RunModel).where(RunModel.run_id == setup_run).values(status="completed"))
    identity = identity.model_copy(update={"actor_kind": TenantActorKind.RUNNER})
    with bind_tenant_execution_context(identity):
        run = await env.runner.repository.create_run(owner, identity.task_id,
            StartRunRequest(module="component", stage_key="component", idempotency_key=f"component-{uuid4().hex}"),
            execution_protocol_factory=lambda: ExecutionProtocolBinding(
                bundle_id="component", module="component", stage_key="component"))
    return InvocationScope(identity=identity, owner_id=owner, run_id=run.run_id,
        execution_mode=ExecutionMode.GRAPH, catalog_frontier=None)


def _geometry(*, name="Tenant box", source=_BOX_SOURCE, inputs=(), function=False):
    source_value = {"source_kind": "modeling_function" if function else "mapped_model",
        "source_files": [{"path": "main.py", "source": source}], "geometry_inputs": list(inputs)}
    if function:
        source_value["source_files"].append({"path": "interfaces.py", "source": "def evaluate(ctx):\n    pass\n"})
    if function:
        source_value["parameter_schema"] = {"action": "set", "value": {
            "type": "object", "additionalProperties": False, "required": [], "properties": {}}}
    return {"mode": "create", "display_name": name,
        "usage_instructions": {"action": "set", "value": "Dimensions are in metres. Original guidance."},
        "source": source_value}


def _family(geometry_ref):
    return {"mode": "create", "display_name": "Tenant fitting", "purpose": "Tenant authoring integration",
        "usage_instructions": {"action": "set", "value": "Use the fixed 0.1 metre definition."},
        "source": {"source_kind": "inline", "source_files": [{"path": "main.py", "source": _FAMILY_SOURCE}],
            "definition": {"action": "set", "value": {
                "family_contract": current_family_contract(standard=True).model_dump(mode="json", by_alias=True),
                "geometry_bindings": [{"binding_key": "body_geometry", "source": {
                    "source_kind": "draft", "draft_ref": geometry_ref},
                    "expected_resource_kind": "mapped_model", "geometry_purpose": "shape_fragment"}]}}}}


async def _count_versions(env, geometry_id, family_id):
    async with env.admin.connect() as connection:
        geometry = (await connection.execute(select(GeometryResourceVersionModel.resource_version_id)
            .where(GeometryResourceVersionModel.resource_id == geometry_id))).scalars().all()
        family = (await connection.execute(select(ProductFamilyVersionModel.family_version_id)
            .where(ProductFamilyVersionModel.family_id == family_id))).scalars().all()
        return geometry, family


@pytest.mark.asyncio
async def test_tenant_current_snapshot_cross_task_cas_and_api_read_only(environment):
    env = environment
    subject = f"tenant-snapshot-{uuid4().hex}"
    first = await _scope(env, subject=subject, name="First Component")
    second = await _scope(env, subject=subject, name="Second Component")
    private = await _scope(env, subject=subject, name="Private Direct", component=False)
    foreign = await _scope(env, subject=f"foreign-{uuid4().hex}", name="Foreign Component")
    assert first.identity.tenant_id == second.identity.tenant_id != foreign.identity.tenant_id
    with bind_tenant_execution_context(first.identity):
        saved = await env.resources.geometry_authoring.save_draft_from_aliases(first,
            declaration=_geometry(), request_identity="tenant-create")
        assert saved.draft.owner_scope.scope_kind is ResourceOwnerScopeKind.TENANT
    with bind_tenant_execution_context(private.identity):
        hidden = await env.resources.geometry_authoring.save_draft_from_aliases(private,
            declaration=_geometry(name="Task private box"), request_identity="private-create")
        assert hidden.draft.owner_scope.scope_kind is ResourceOwnerScopeKind.TASK
    with bind_tenant_execution_context(second.identity):
        draft = await env.runner.repository.get_geometry_draft(second, resource_id=saved.draft.root_id)
        assert draft == saved.draft
        reference = env.resources.geometry_authoring.draft_reference(second, draft)
        assert (await env.resources.geometry_authoring.read_draft(second, draft_ref=reference))[1].source_files[0].source == _BOX_SOURCE
        updated = await env.resources.geometry_authoring.save_draft_from_aliases(second, request_identity="cross-task-edit",
            declaration={"mode": "update", "draft_ref": reference, "expected_edit_token": 1,
                "usage_instructions": {"action": "edit", "edits": [{
                    "old_text": "Original guidance.", "new_text": "Shared guidance."}]}})
        assert updated.draft.generation == 2
        with pytest.raises(GeometryAuthoringApplicationError) as stale:
            await env.resources.geometry_authoring.save_draft_from_aliases(second, request_identity="stale-edit",
                declaration={"mode": "update", "draft_ref": reference, "expected_edit_token": 1, "display_name": "Stale"})
        assert stale.value.code == "GEOMETRY_DRAFT_CONFLICT"
        async with env.api.sessions() as session:
            await set_tenant_execution_context(session, second.identity)
            assert await session.get(ArtifactVersionModel, updated.draft.snapshot.version_id) is not None
            assert await session.get(ArtifactVersionModel, saved.draft.snapshot.version_id) is None
            assert await session.get(ArtifactVersionModel, hidden.draft.snapshot.version_id) is None
        for table in (GeometryResourceModel, ProductFamilyModel):
            async with env.api.sessions() as session, session.begin():
                await set_tenant_execution_context(session, second.identity)
                with pytest.raises(DBAPIError) as denied:
                    await session.execute(update(table).values(draft_generation=2))
                assert denied.value.orig.sqlstate == "42501"
    with bind_tenant_execution_context(first.identity):
        current = await env.runner.repository.get_geometry_draft(first, resource_id=saved.draft.root_id)
        assert current == updated.draft
    with bind_tenant_execution_context(foreign.identity):
        assert await env.runner.repository.get_geometry_draft(foreign, resource_id=saved.draft.root_id) is None
        async with env.runner.sessions() as session:
            await set_tenant_execution_context(session, foreign.identity)
            assert await session.get(ArtifactVersionModel, updated.draft.snapshot.version_id) is None
        async with env.runner.sessions() as session, session.begin():
            await set_tenant_execution_context(session, foreign.identity)
            with pytest.raises(DBAPIError) as denied:
                await session.execute(insert(GeometryResourceModel).values(resource_id=f"forbidden-{uuid4().hex}",
                    resource_kind="mapped_model", owner_scope="TENANT", owner_tenant_id=first.identity.tenant_id,
                    owner_task_id=None, display_name="Forbidden foreign root", created_at=datetime.now(UTC)))
            assert denied.value.orig.sqlstate == "42501"


@pytest.mark.asyncio
async def test_tenant_atomic_publication_cross_task_fence_exact_reuse_and_reopen(environment, tmp_path, monkeypatch):
    env = environment
    subject = f"tenant-publication-{uuid4().hex}"
    first = await _scope(env, subject=subject, name="Author Component")
    second = await _scope(env, subject=subject, name="Consumer Component")
    foreign = await _scope(env, subject=f"foreign-publication-{uuid4().hex}", name="Foreign Component")
    with bind_tenant_execution_context(first.identity):
        geometry = await env.resources.geometry_authoring.save_draft_from_aliases(first,
            declaration=_geometry(), request_identity="save-geometry")
        geometry_ref = env.resources.geometry_authoring.draft_reference(first, geometry.draft)
        family = await env.resources.family_authoring.save_draft_from_aliases(first,
            declaration=_family(geometry_ref), request_identity="save-family")
        family_ref = env.resources.family_authoring.draft_reference(first, family.draft)
        assert family.draft.owner_scope == geometry.draft.owner_scope
        original_insert = PostgresProductFamilyAuthoringRepositoryMixin._insert_product_family_bundle

        async def duplicate_family_insert(cls, session, bundle):
            await original_insert(session, bundle)
            return await original_insert(session, bundle)

        with monkeypatch.context() as rejected:
            rejected.setattr(PostgresProductFamilyAuthoringRepositoryMixin,
                "_insert_product_family_bundle", classmethod(duplicate_family_insert))
            with pytest.raises(AuthoringDraftError) as failed:
                await env.resources.family_authoring.publish_draft(first, draft_ref=family_ref,
                    expected_edit_token=1, request_identity="publish-rollback")
        assert failed.value.code == "AUTHORING_PUBLICATION_COMMIT_REJECTED"
        assert await _count_versions(env, geometry.draft.root_id, family.draft.root_id) == ([], [])
        with monkeypatch.context() as unclaimed:
            unclaimed.setattr(env.runner.repository, "claim_authoring_execution", AsyncMock(return_value=None))
            with pytest.raises(AuthoringDraftError) as pending:
                await env.resources.family_authoring.publish_draft(first, draft_ref=family_ref,
                    expected_edit_token=1, request_identity="publish-original")
        assert pending.value.code == "PUBLICATION_RECONCILIATION_REQUIRED"
        operation_id = AuthoringDraftService.operation_identity(first, "ifc_authoring_publish", "publish-original")
        held = await env.runner.repository.get_authoring_execution_state(first, operation_id,
            operation_kind=CriticalOperationKind.AUTHORING_PUBLISH)
        assert held.operation.status is CriticalOperationStatus.PREPARED
    with bind_tenant_execution_context(second.identity):
        same_geometry = await env.runner.repository.get_geometry_draft(second, resource_id=geometry.draft.root_id)
        other_geometry_ref = env.resources.geometry_authoring.draft_reference(second, same_geometry)
        async with env.runner.sessions() as session:
            await set_tenant_execution_context(session, second.identity)
            assert await session.get(CriticalOperationModel, operation_id) is None
            assert await session.scalar(text("SELECT cad2bim_authoring_publication_fence('geometry',:root)"),
                {"root": geometry.draft.root_id}) is True
        with monkeypatch.context() as no_build:
            no_build.setattr(env.resources.geometry_runtime, "build_fixed", AsyncMock(side_effect=AssertionError("fenced root cannot build")))
            with pytest.raises(AuthoringDraftError) as fenced:
                await env.resources.geometry_authoring.publish_draft(second, draft_ref=other_geometry_ref,
                    expected_edit_token=1, request_identity="other-task-publish")
        assert fenced.value.code == "PUBLICATION_RECONCILIATION_REQUIRED"
        assert fenced.value.operation_alias is None
    with bind_tenant_execution_context(first.identity):
        result = await env.resources.family_authoring.publish_draft(first, draft_ref=family_ref,
            expected_edit_token=1, request_identity="publish-original")
        assert result.operation.operation.operation_id == operation_id
        assert result.family_version.version.version_no == 1
        assert len(result.geometry_versions) == 1
        replay = await env.resources.family_authoring.publish_draft(first, draft_ref=family_ref,
            expected_edit_token=1, request_identity="publish-original")
        assert replay.operation == result.operation
    with bind_tenant_execution_context(second.identity):
        same_family = await env.runner.repository.get_product_family_draft(second, family_id=family.draft.root_id)
        other_family_ref = env.resources.family_authoring.draft_reference(second, same_family)
        assert (await env.resources.family_authoring.read_draft(second, draft_ref=other_family_ref))[1].source_files[0].source == _FAMILY_SOURCE
        with monkeypatch.context() as no_rebuild:
            no_rebuild.setattr(env.resources.geometry_runtime, "build_fixed", AsyncMock(side_effect=AssertionError("exact closure must reuse Geometry")))
            no_rebuild.setattr(env.resources.realization_runtime, "realize_geometry", AsyncMock(side_effect=AssertionError("exact closure must reuse Family")))
            reused = await env.resources.family_authoring.publish_draft(second, draft_ref=other_family_ref,
                expected_edit_token=1, request_identity="reuse-exact-closure")
        assert reused.geometry_versions == result.geometry_versions and reused.family_version == result.family_version
        assert reused.operation.operation.task_id == second.identity.task_id
        assert reused.operation.operation.operation_id != operation_id
        geometries = await env.resources.geometry_catalog.list(second.identity, GeometryCatalogListQuery(), None)
        families = await env.resources.family_catalog.list(second.identity, ProductFamilyCatalogListQuery(), None)
        assert len(geometries.resources) == len(families.families) == 1
        geometry_alias = geometries.resources[0].latest_version.resource_version_ref
        assert (await env.resources.geometry_catalog.resolve_exact_version(second.identity, geometry_alias)) == result.geometry_versions[0]
        source_view = await GeometrySourceService(env.resources.geometry_catalog,
            env.resources.geometry_resources).read_source(second.identity, geometry_alias, "main.py", None)
        assert source_view.view.text == _BOX_SOURCE
        exact = result.geometry_versions[0]
        _, brep = await env.resources.geometry_resources.read_exact_asset(second.identity,
            resource_id=exact.resource.resource_id, resource_version_id=exact.version.resource_version_id,
            asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY)
        assert brep
        prepared = await env.resources.geometry_authoring.prepare_draft_preview(second, draft_ref=other_geometry_ref)
        checked = await env.resources.geometry_preview.preview_cases(second, **prepared,
            cases=(GeometryPreviewCase(case_key="nominal", expectation=GeometryCaseExpectation()),),
            retain_for_inspection=True, request_identity="cross-task-preview")
        assert checked.report.checks_passed and checked.report.cases[0].inspection_ref
        function = await env.resources.geometry_authoring.save_draft_from_aliases(second,
            declaration=_geometry(name="Tenant input consumer", function=True, source="def build(ctx):\n    return ctx.geometry('base')\n",
                inputs=({"input_key": "base", "resource_version_alias": geometry_alias},)), request_identity="save-dependent-function")
        function_ref = env.resources.geometry_authoring.draft_reference(second, function.draft)
        function_result = await env.resources.geometry_authoring.publish_draft(second, draft_ref=function_ref,
            expected_edit_token=1, request_identity="publish-dependent-function")
        async with env.api.sessions() as session:
            await set_tenant_execution_context(session, second.identity)
            assert await session.get(ProductFamilyModel, family.draft.root_id) is not None
            assert await session.get(ProductFamilyVersionModel, result.family_version.version.family_version_id) is not None
            assert (await session.scalars(select(GeometryResourceVersionAssetModel).where(
                GeometryResourceVersionAssetModel.resource_version_id == exact.version.resource_version_id))).all()
            assert await session.get(GeometryResourceVersionInputModel,
                (function_result.geometry_versions[0].version.resource_version_id, "base")) is not None
            assert await session.get(CriticalOperationModel, operation_id) is None
            for table in (GeometryResourceModel, GeometryResourceVersionModel, GeometryResourceVersionAssetModel,
                          GeometryResourceVersionInputModel, ProductFamilyModel, ProductFamilyVersionModel,
                          ProductFamilyGeometryBindingModel):
                assert (await session.execute(text("SELECT has_table_privilege(current_user,:table,'SELECT'),"
                    "has_table_privilege(current_user,:table,'INSERT')"), {"table": table.__tablename__})).one() == (True, False)
        # Construct a currently visible TASK Geometry with the same validated
        # content. Both dependency refusals therefore isolate owner width.
        task_root, task_version = f"task-geometry-{uuid4().hex}", f"task-version-{uuid4().hex}"
        async with env.admin.connect() as connection:
            version_row = dict((await connection.execute(select(GeometryResourceVersionModel.__table__).where(
                GeometryResourceVersionModel.resource_version_id == exact.version.resource_version_id))).mappings().one())
            asset_rows = [dict(item) for item in (await connection.execute(select(GeometryResourceVersionAssetModel.__table__).where(
                GeometryResourceVersionAssetModel.resource_version_id == exact.version.resource_version_id))).mappings()]
            binding_row = dict((await connection.execute(select(ProductFamilyGeometryBindingModel.__table__).where(
                ProductFamilyGeometryBindingModel.family_version_id == result.family_version.version.family_version_id))).mappings().one())
        version_row.update(resource_id=task_root, resource_version_id=task_version)
        async with env.runner.sessions() as session, session.begin():
            await set_tenant_execution_context(session, second.identity)
            await session.execute(insert(GeometryResourceModel).values(resource_id=task_root, resource_kind="mapped_model",
                owner_scope="TASK", owner_tenant_id=second.identity.tenant_id, owner_task_id=second.identity.task_id,
                display_name="Task scoped dependency", created_at=datetime.now(UTC)))
            await session.execute(insert(GeometryResourceVersionModel).values(**version_row))
            for asset_row in asset_rows:
                asset_row["resource_version_id"] = task_version
                await session.execute(insert(GeometryResourceVersionAssetModel).values(**asset_row))
        binding_row.update(binding_key="narrow_dependency", geometry_resource_id=task_root,
            geometry_resource_version_id=task_version)
        async with env.runner.sessions() as session, session.begin():
            await set_tenant_execution_context(session, second.identity)
            with pytest.raises(DBAPIError) as narrow_family:
                await session.execute(insert(ProductFamilyGeometryBindingModel).values(**binding_row))
            assert narrow_family.value.orig.sqlstate == "23514"
            assert "narrower" in str(narrow_family.value.orig)
        async with env.runner.sessions() as session, session.begin():
            await set_tenant_execution_context(session, second.identity)
            with pytest.raises(DBAPIError) as narrow_input:
                await session.execute(insert(GeometryResourceVersionInputModel).values(
                    resource_version_id=function_result.geometry_versions[0].version.resource_version_id,
                    input_key="narrow_dependency", target_resource_version_id=task_version))
            assert narrow_input.value.orig.sqlstate == "42501"
    with bind_tenant_execution_context(foreign.identity):
        async with env.api.sessions() as session:
            await set_tenant_execution_context(session, foreign.identity)
            assert await session.get(GeometryResourceModel, geometry.draft.root_id) is None
            assert await session.get(GeometryResourceVersionModel, exact.version.resource_version_id) is None
            assert await session.get(ProductFamilyModel, family.draft.root_id) is None
            assert await session.get(ProductFamilyVersionModel, result.family_version.version.family_version_id) is None
    assert await _count_versions(env, geometry.draft.root_id, family.draft.root_id) == (
        [result.geometry_versions[0].version.resource_version_id], [result.family_version.version.family_version_id])
    await env.resources.close()
    await env.runner.close()
    env.runner = _runtime(env.runner_url)
    env.resources = _resources(env.runner.repository, LocalStorage(tmp_path / "storage"), tmp_path)
    with bind_tenant_execution_context(second.identity):
        reopened = await env.resources.geometry_catalog.resolve_exact_version(second.identity, geometry_alias)
        assert reopened == exact
        assert (await env.resources.family_authoring.read_draft(second, draft_ref=other_family_ref))[0] == same_family
    with bind_tenant_execution_context(first.identity):
        recovered = await env.resources.family_authoring.publish_draft(first, draft_ref=family_ref,
            expected_edit_token=1, request_identity="publish-original")
        assert recovered.operation == result.operation
    print({"evidence": "tenant_authoring", "geometry_root": geometry.draft.root_id,
        "geometry_version": exact.version.resource_version_id, "family_root": family.draft.root_id,
        "family_version": result.family_version.version.family_version_id, "original_task": first.identity.task_id,
        "consumer_task": second.identity.task_id, "original_operation": operation_id,
        "consumer_operation": reused.operation.operation.operation_id})
