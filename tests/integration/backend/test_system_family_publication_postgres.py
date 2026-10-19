"""One real PostgreSQL SYSTEM copy, recovery, and cross-tenant build witness."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftService
from CAD2BIM_backend.application.authoring_publication_service import AuthoringPublicationService
from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionService
from CAD2BIM_backend.domain.data_runtime import AuthoringPreviewSelection
from CAD2BIM_backend.domain.preview_cases import GeometryPreviewCase, GeometryCaseExpectation, FamilyPreviewCase, FamilyCaseExpectation
from CAD2BIM_backend.application.data_runtime_service import DirectScopeResolver, DirectSessionService
from CAD2BIM_backend.application.geometry_authoring_service import GeometryAuthoringService
from CAD2BIM_backend.application.geometry_catalog_service import GeometryCatalogService
from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService
from CAD2BIM_backend.application.product_family_asset_service import ProductFamilyAssetService
from CAD2BIM_backend.application.product_family_authoring_service import (
    ProductFamilyAuthoringApplicationError,
    ProductFamilyAuthoringService,
)
from CAD2BIM_backend.application.product_family_catalog_service import ProductFamilyCatalogService
from CAD2BIM_backend.application.product_realization_input_service import ProductRealizationInputService
from CAD2BIM_backend.application.product_realization_preview_service import ProductRealizationPreviewService
from CAD2BIM_backend.application.system_family_publication_service import (
    CatalogSystemFamilySource,
    SystemFamilyPublicationService,
)
from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.data_runtime import CriticalOperationStatus, ProductFamilyWriteOperationDetail
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole, GeometryResourceKind
from CAD2BIM_backend.domain.identity import ConfiguredLocalSubject, TenantActorKind
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyVersionAssetRole,
    product_family_content_digest,
)
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationStatus,
    ProductRealizationValidationScenario,
    product_geometry_binding_digest,
)
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScopeKind
from CAD2BIM_backend.domain.system_family_publication import SystemFamilyPublicationCommand
from CAD2BIM_backend.infrastructure.data_query.cursor_codec import CursorCodec
from CAD2BIM_backend.infrastructure.geometry_runtime.adapter import OcpCadQueryGeometryRuntime
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactModel,
    ArtifactVersionModel,
    CriticalOperationModel,
    DataCatalogEntryModel,
    GeometryResourceModel,
    GeometryResourceVersionModel,
    ProductFamilyGeometryBindingModel,
    ProductFamilyModel,
    ProductFamilyVersionAssetModel,
    ProductFamilyVersionModel,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from CAD2BIM_backend.infrastructure.product_realization_runtime import OcpCadQueryProductRealizationRuntime
from CAD2BIM_backend.infrastructure.source_text.geometry_authoring import SourceTextGeometryAuthoringSource
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from CAD2BIM_backend.infrastructure.worker_runtime.capacity import HostWorkerCapacity
from CAD2BIM_backend.ports.identity import IdentityAccessDenied
from tests.integration.backend.test_geometry_resource_postgres import (
    _cleanup_geometry_rows,
    _create_runtime,
    _create_test_runtime_logins,
    _drop_test_runtime_logins,
    _runtime_database_url,
)
from tests.integration.backend.test_product_family_postgres import _cleanup_family_rows
from tests.unit.backend.product_family_fixtures import current_family_document


pytestmark = [pytest.mark.integration, pytest.mark.network]
TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL", "").strip()
_FAMILY_SOURCE = """def construct(ctx):
    return {
        'status': 'SOLVED',
        'components': [{'component_key': 'body'}],
        'fragments': [{
            'fragment_key': 'body_shape',
            'binding_key': 'body_geometry',
        }],
    }

def realize(ctx):
    return construct(ctx)

def standard_model(ctx):
    return construct(ctx)
"""


@dataclass
class _Services:
    resources: GeometryResourceService
    geometry_authoring: GeometryAuthoringService
    family_assets: ProductFamilyAssetService
    family_catalog: ProductFamilyCatalogService
    family_authoring: ProductFamilyAuthoringService
    builds: ProductRealizationPreviewService
    preview: GeometryPreviewService


def _services(repository, storage, geometry_runtime, realization_runtime) -> _Services:
    sources = SourceTextGeometryAuthoringSource()
    artifacts = ArtifactService(repository, storage)
    resources = GeometryResourceService(
        repository, storage, geometry_runtime=geometry_runtime,
        source_bundle_packer=lambda members: sources.pack(members).content,
    )
    cursor = CursorCodec(base64.urlsafe_b64encode(b"s" * 32).rstrip(b"=").decode())
    geometry_catalog = GeometryCatalogService(resources, cursor)
    family_assets = ProductFamilyAssetService(storage, sources)
    family_catalog = ProductFamilyCatalogService(
        repository, resources, geometry_catalog, family_assets, cursor,
    )
    inputs = ProductRealizationInputService(
        family_assets, resources, runtime=realization_runtime, family_repository=repository,
    )
    drafts = AuthoringDraftService(artifacts, repository, cursor)
    inspection = GeometryInspectionService(artifacts, repository, cursor)
    preview = GeometryPreviewService(geometry_catalog, resources, geometry_runtime, family_catalog, inspection)
    from CAD2BIM_backend.infrastructure.source_text.source_patch_adapter import TextSourcePatchAdapter
    source_patch = TextSourcePatchAdapter()
    geometry_authoring = GeometryAuthoringService(repository, resources, sources,
        source_patch=source_patch, drafts=drafts, catalog=geometry_catalog)
    family_authoring = ProductFamilyAuthoringService(repository, artifacts, family_catalog, family_assets,
        inputs, sources, source_patch=source_patch, drafts=drafts, geometry_authoring=geometry_authoring)
    publisher = AuthoringPublicationService(repository, drafts, geometry_authoring, family_authoring, inspection,
        geometry_runtime=geometry_runtime, realization_runtime=realization_runtime)
    geometry_authoring._publication = family_authoring._publication = publisher
    return _Services(resources=resources, geometry_authoring=geometry_authoring, family_assets=family_assets,
        family_catalog=family_catalog, family_authoring=family_authoring, preview=preview,
        builds=ProductRealizationPreviewService(family_catalog, inputs, realization_runtime, preview, inspection))


async def _direct_scope(runtime, subject: str, session_name: str) -> InvocationScope:
    sessions = DirectSessionService(
        runtime.repository, runtime.identity_directory, subject, bind_tenant_execution_context,
    )
    resolver = DirectScopeResolver(
        runtime.repository, runtime.identity_directory, subject, bind_tenant_execution_context,
    )
    opened = await sessions.open_direct_session(session_name)
    return await resolver.resolve(
        opened.binding.direct_session_alias, command_id=f"system-copy-read-{uuid4().hex}",
    )


class _InterruptAfterGeometryCommit:
    """Interrupt only after the real Geometry repository and Storage readback."""

    def __init__(self, resources) -> None:
        self.resources = resources
        self.committed = None

    def __getattr__(self, name):
        return getattr(self.resources, name)

    async def publish_candidate(self, *args, **kwargs):
        committed = await self.resources.publish_candidate(*args, **kwargs)
        self.committed = committed
        raise asyncio.CancelledError("dependency committed before Family publication")


class _LoseFamilyCommitResponse:
    """Lose a result after a real PostgreSQL success, including its execution fence."""

    def __init__(self, repository) -> None:
        self.repository = repository
        self.lost_responses = 0

    def __getattr__(self, name):
        return getattr(self.repository, name)

    async def commit_product_family_authoring_success(self, *args, **kwargs):
        committed = await self.repository.commit_product_family_authoring_success(*args, **kwargs)
        if not self.lost_responses:
            self.lost_responses += 1
            raise RuntimeError("SYSTEM Family commit succeeded before its response was lost")
        return committed


class _UnavailableOriginalSource:
    """Recovery must use the saved Operation even when its original endpoint is gone."""

    def __init__(self, source) -> None:
        self.source = source
        self.read_calls: list[str] = []

    def __getattr__(self, name):
        return getattr(self.source, name)

    async def read_family(self, **_kwargs):
        self.read_calls.append("family")
        raise AssertionError("original-request recovery must not reread its unavailable source")

    async def read_geometry(self, **_kwargs):
        self.read_calls.append("geometry")
        raise AssertionError("original-request recovery must not reread its unavailable source")


async def _family_bytes(assets, bundle) -> dict[ProductFamilyVersionAssetRole, bytes]:
    return {asset.asset_role: await assets.read_exact_asset(bundle, asset) for asset in bundle.assets}


async def _geometry_bytes(resources, context, bundle) -> dict[GeometryResourceAssetRole, bytes]:
    result = {}
    for asset in bundle.assets:
        observed = await resources.read_exact_asset(
            context, resource_id=bundle.resource.resource_id,
            resource_version_id=bundle.version.resource_version_id, asset_role=asset.asset_role,
        )
        assert observed is not None
        result[asset.asset_role] = observed[1]
    return result


async def _copy_operation_id(admin, task_id: str) -> str:
    async with admin.connect() as connection:
        ids = (await connection.scalars(
            select(CriticalOperationModel.operation_id).where(
                CriticalOperationModel.task_id == task_id,
                CriticalOperationModel.operation_detail_json["mode"].as_string() == "system_copy",
            )
        )).all()
    assert len(ids) == 1
    return ids[0]


async def _assert_private_dependency_rejected(runtime, scope, target, source_geometry) -> None:
    """Use the curator login for a complete transaction whose sole dependency is private."""
    family_id = f"system-private-guard-{uuid4().hex}"
    version_id = f"system-private-version-{uuid4().hex}"
    binding = target.geometry_bindings[0].model_copy(update={
        "family_version_id": version_id,
        "geometry_resource_id": source_geometry.resource.resource_id,
        "geometry_resource_version_id": source_geometry.version.resource_version_id,
        "geometry_content_digest": source_geometry.version.content_digest,
    })
    assets = tuple(asset.model_copy(update={"family_version_id": version_id}) for asset in target.assets)
    version = target.version
    digest = product_family_content_digest(
        contract_digest=version.contract_digest, program_entry=version.program_entry,
        geometry_bindings=(binding,), assets=assets,
        usage_instructions=version.usage_instructions,
    )
    with pytest.raises(DBAPIError) as rejected:
        with bind_tenant_execution_context(scope.identity):
            async with runtime.sessions() as session, session.begin():
                await session.execute(insert(ProductFamilyModel).values(
                    family_id=family_id, owner_scope="SYSTEM",
                    owner_tenant_id=None, owner_task_id=None,
                ))
                await session.execute(insert(ProductFamilyVersionModel).values(
                    family_version_id=version_id, family_id=family_id, version_no=1,
                    parent_family_version_id=None, display_name=version.display_name,
                    purpose=version.purpose, usage_instructions=version.usage_instructions,
                    family_contract_json=version.family_contract.model_dump(mode="json"),
                    contract_digest=version.contract_digest, content_digest=digest,
                    program_entry=version.program_entry,
                ))
                await session.execute(insert(ProductFamilyGeometryBindingModel).values(
                    family_version_id=version_id, binding_key=binding.binding_key,
                    geometry_resource_id=binding.geometry_resource_id,
                    geometry_resource_version_id=binding.geometry_resource_version_id,
                    geometry_content_digest=binding.geometry_content_digest,
                    expected_resource_kind=binding.expected_resource_kind.value,
                    geometry_purpose=binding.geometry_purpose.value,
                ))
                await session.execute(insert(ProductFamilyVersionAssetModel), [{
                    "family_version_id": version_id, "asset_role": asset.asset_role.value,
                    "asset_format": asset.asset_format, "filename": asset.filename,
                    "content_type": asset.content_type, "size_bytes": asset.size_bytes,
                    "sha256": asset.sha256,
                } for asset in assets])
    # The curator cannot SELECT TASK Geometry. The existing invoker guard therefore
    # rejects its exact-parent JOIN before the narrower-owner check (which is 23514).
    assert getattr(rejected.value.orig, "sqlstate", None) == "23503"
    # Preserve only bounded failure evidence; no source program, bytes, or DSN.
    diagnostic = getattr(rejected.value.orig, "diag", None)
    assert diagnostic.message_primary == "ProductFamily binding does not resolve exact parents"
    print("SYSTEM_PRIVATE_DEPENDENCY_REJECTED_SQLSTATE=23503")
    print(f"SYSTEM_PRIVATE_DEPENDENCY_MESSAGE={getattr(diagnostic, 'message_primary', None)}")
    print(f"SYSTEM_PRIVATE_DEPENDENCY_CONSTRAINT={getattr(diagnostic, 'constraint_name', None)}")
    with bind_tenant_execution_context(scope.identity):
        assert await runtime.repository.get_product_family(scope.identity, family_id=family_id) is None


async def _cleanup_owned_case(admin, task_ids: set[str]) -> None:
    if not task_ids:
        return
    family_ids: set[str] = set()
    geometry_ids: set[str] = set()
    async with admin.begin() as connection:
        operations = (await connection.execute(select(
            CriticalOperationModel.operation_id, CriticalOperationModel.target_product_family_id,
            CriticalOperationModel.target_geometry_resource_id, CriticalOperationModel.operation_detail_json,
        ).where(CriticalOperationModel.task_id.in_(task_ids)))).mappings().all()
        for operation in operations:
            if operation["target_product_family_id"]:
                family_ids.add(operation["target_product_family_id"])
            if operation["target_geometry_resource_id"]:
                geometry_ids.add(operation["target_geometry_resource_id"])
            if operation["operation_detail_json"].get("mode") == "system_copy":
                detail = ProductFamilyWriteOperationDetail.model_validate(operation["operation_detail_json"])
                geometry_ids.update(item.target_version.resource.resource_id
                                    for item in detail.system_copy.geometry_copies)
        await connection.execute(delete(DataCatalogEntryModel).where(DataCatalogEntryModel.task_id.in_(task_ids)))
        await connection.execute(delete(CriticalOperationModel).where(CriticalOperationModel.task_id.in_(task_ids)))
    await _cleanup_family_rows(admin, family_ids)
    await _cleanup_geometry_rows(admin, geometry_ids)
    async with admin.begin() as connection:
        await connection.execute(update(ArtifactModel).where(ArtifactModel.task_id.in_(task_ids))
                                 .values(current_version_id=None))
        await connection.execute(delete(ArtifactVersionModel).where(ArtifactVersionModel.task_id.in_(task_ids)))
        await connection.execute(delete(ArtifactModel).where(ArtifactModel.task_id.in_(task_ids)))


@pytest.mark.asyncio
async def test_system_copy_is_exact_atomic_recoverable_and_public_across_tenants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    suffix = uuid4().hex
    logins = _create_test_runtime_logins(TEST_DATABASE_URL)
    admin = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    source_url = _runtime_database_url(TEST_DATABASE_URL, logins["mcp"])
    curator_url = _runtime_database_url(TEST_DATABASE_URL, logins["curator"])
    source_runtime = _create_runtime(source_url)
    curator_runtime = _create_runtime(curator_url)
    runtimes = [source_runtime, curator_runtime]
    tasks: set[str] = set()
    capacity = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path / "capacity")
    geometry_runtime = OcpCadQueryGeometryRuntime(capacity=capacity)
    realization_runtime = OcpCadQueryProductRealizationRuntime(capacity=capacity)
    storage = LocalStorage(tmp_path / "storage", max_size_bytes=64 * 1024 * 1024)
    try:
        await source_runtime.database.verify_ready()
        await curator_runtime.database.verify_ready()
        async with curator_runtime.engine.connect() as connection:
            role = (await connection.execute(text(
                "SELECT rolsuper, rolbypassrls, "
                "pg_has_role(current_user, 'cad2bim_system_curator', 'MEMBER') AS curator, "
                "pg_has_role(current_user, 'cad2bim_mcp_runtime', 'MEMBER') AS mcp, "
                "pg_has_role(current_user, 'cad2bim_runner_runtime', 'MEMBER') AS runner "
                "FROM pg_roles WHERE rolname = current_user"
            ))).mappings().one()
        assert dict(role) == {"rolsuper": False, "rolbypassrls": False, "curator": True,
                              "mcp": False, "runner": False}

        subject = f"system-source-{suffix}"
        source_scope = await _direct_scope(source_runtime, subject, f"source-{suffix}")
        same_tenant_scope = await _direct_scope(source_runtime, subject, f"new-task-{suffix}")
        other_tenant_scope = await _direct_scope(source_runtime, f"other-{suffix}", f"other-{suffix}")
        tasks.update(scope.identity.task_id for scope in
                     (source_scope, same_tenant_scope, other_tenant_scope))
        assert same_tenant_scope.identity.tenant_id == source_scope.identity.tenant_id
        assert same_tenant_scope.identity.task_id != source_scope.identity.task_id
        assert other_tenant_scope.identity.tenant_id != source_scope.identity.tenant_id

        curator_context = await curator_runtime.identity_directory.resolve_configured_local_subject(
            ConfiguredLocalSubject(subject=f"system-curator-{suffix}"), command_id=f"curator-{suffix}",
        )
        curator_context = curator_context.model_copy(update={"actor_kind": TenantActorKind.SYSTEM_CURATOR})
        management_scope = await curator_runtime.repository.open_system_family_publication_scope(
            curator_context, publication_name=f"system-copy-{suffix}",
        )
        tasks.add(management_scope.identity.task_id)
        assert management_scope.identity.actor_kind is TenantActorKind.SYSTEM_CURATOR
        assert management_scope.execution_mode is ExecutionMode.DIRECT_MCP

        source_services = _services(source_runtime.repository, storage, geometry_runtime, realization_runtime)
        with bind_tenant_execution_context(source_scope.identity):
            geometry_result = await source_services.geometry_authoring.save_draft_from_aliases(source_scope,
                request_identity=f"system-source-geometry-{suffix}", declaration={"mode":"create",
                "display_name":"Hollow ring", "usage_instructions":{"action":"set","value":"Fixed hollow ring; use the actual inlet and outlet frames."},
                "source":{"source_kind":"mapped_model", "source_files":[{"path":"main.py","source":
                    "import cadquery as cq\ndef build(ctx):\n"
                    "    ctx.define_interface(key='ring_outer', origin_m=(0,0,0), axis=(0,0,-1), reference_direction=(1,0,0), profile={'kind':'circular','diameter_m':0.10})\n"
                    "    ctx.define_interface(key='ring_join', origin_m=(0,0,0.02), axis=(0,0,1), reference_direction=(1,0,0), profile={'kind':'circular','diameter_m':0.10})\n"
                    "    return cq.Workplane('XY').circle(0.05).circle(0.04).extrude(0.02)\n"}]}})
            geometry_ref = source_services.geometry_authoring.draft_reference(source_scope,geometry_result.draft)
            prepared = await source_services.geometry_authoring.prepare_draft_preview(source_scope,draft_ref=geometry_ref)
            checked = await source_services.preview.preview_cases(source_scope, **prepared,
                cases=(GeometryPreviewCase(case_key="fixed",expectation=GeometryCaseExpectation()),),
                retain_for_inspection=True,request_identity="check-source-geometry")
            gp = await source_services.geometry_authoring.publish_draft(source_scope,
                draft_ref=geometry_ref,expected_edit_token=1,
                preview_examples=(AuthoringPreviewSelection(operation_alias=checked.operation_alias),),request_identity="publish-source-geometry")
            source_geometry = gp.geometry_versions[0]
            saved = await source_services.family_authoring.save_draft_from_aliases(source_scope,
                request_identity="save-source-family",declaration={"mode":"create", "display_name":"Exact SYSTEM copy source",
                "purpose":"One real shared SYSTEM dependency witness.", "usage_instructions":{"action":"set","value":"Use its exact Geometry binding."},
                "source":{"source_kind":"inline", "source_files":[{"path":"main.py","source":_FAMILY_SOURCE}],
                "definition":{"action":"set","value":{"family_contract":current_family_document(standard=True),"geometry_bindings":[{
                    "binding_key":"body_geometry","source":{"source_kind":"published",
                    "resource_version_alias":source_services.geometry_authoring._catalog.version_ref(source_scope.identity,source_geometry)},
                    "expected_resource_kind":"mapped_model","geometry_purpose":"product_component"}]}}}})
            family_ref = source_services.family_authoring.draft_reference(source_scope,saved.draft)
            scenarios = tuple(ProductRealizationValidationScenario(scenario_key=key,realization_kind=kind,
                public_parameters={},expected_status=ProductRealizationStatus.SOLVED,product_origin=origin,length_unit={"name":"METRE"})
                for key,kind,origin in (("nominal","standard_model",(0,0,0)),("placed","standard_model",(2,3,4)),("self","self_geometry",(2,3,4))))
            cases = tuple(FamilyPreviewCase(case_key=x.scenario_key,invocation=x.to_invocation(),expectation=FamilyCaseExpectation(expected_status="SOLVED")) for x in scenarios)
            prepared = await source_services.family_authoring.prepare_draft_preview(source_scope,draft_ref=family_ref,
                invocations=tuple(x.invocation for x in cases))
            checked = await source_services.builds.preview_cases(source_scope,**prepared,cases=cases,
                nominal_scenario_key="nominal",retain_for_inspection=True,request_identity="check-source-family")
            source_result = await source_services.family_authoring.publish_draft(source_scope,
                draft_ref=family_ref,expected_edit_token=1,preview_examples=(AuthoringPreviewSelection(operation_alias=checked.operation_alias),),
                request_identity="publish-source-family")
            original_geometry_bytes = await _geometry_bytes(source_services.resources, source_scope.identity, source_geometry)
        source_family = source_result.family_version
        original_source_operation = source_result.operation
        with bind_tenant_execution_context(source_scope.identity):
            original_family_bytes = await _family_bytes(source_services.family_assets, source_family)
            original_manifest, original_brep = await source_services.family_assets.standard_model_assets(source_family)

        command = SystemFamilyPublicationCommand(
            request_id=f"system-copy-{suffix}", source_family_id=source_family.family.family_id,
            source_family_version_id=source_family.version.family_version_id,
        )
        source = CatalogSystemFamilySource(
            source_scope.identity, source_services.family_catalog,
            source_services.family_assets, source_services.resources,
            bind_identity=bind_tenant_execution_context,
        )
        target_services = _services(curator_runtime.repository, storage, geometry_runtime, realization_runtime)
        publication = SystemFamilyPublicationService(
            curator_runtime.repository, target_services.family_authoring, target_services.resources, source,
            bind_identity=bind_tenant_execution_context,
        )
        runtime_calls: list[str] = []

        def forbidden_execution(*_args, **_kwargs):
            runtime_calls.append("runtime execution")
            raise AssertionError("SYSTEM scope copy must not re-execute or rebuild any source")

        with monkeypatch.context() as pure_copy:
            for runtime, names in (
                (realization_runtime, ("realize", "realize_batch", "realize_geometry",
                                       "precheck_batch", "instantiate_batch")),
                (geometry_runtime, ("normalize", "reopen", "build_fixed", "validate_function", "query", "render")),
            ):
                for name in names:
                    pure_copy.setattr(runtime, name, forbidden_execution)

            # An ordinary login cannot turn its own Task into SYSTEM authority.
            ordinary = SystemFamilyPublicationService(
                source_runtime.repository, source_services.family_authoring, source_services.resources, source,
                bind_identity=bind_tenant_execution_context,
            )
            with pytest.raises(IdentityAccessDenied):
                await ordinary.publish(source_scope, command)
            with pytest.raises(IdentityAccessDenied):
                await source_runtime.repository.open_system_family_publication_scope(
                    source_scope.identity.model_copy(update={
                        "actor_kind": TenantActorKind.SYSTEM_CURATOR, "task_id": None,
                    }),
                    publication_name=f"ordinary-login-denied-{suffix}",
                )
            inaccessible = CatalogSystemFamilySource(
                other_tenant_scope.identity, source_services.family_catalog,
                source_services.family_assets, source_services.resources,
                bind_identity=bind_tenant_execution_context,
            )
            with pytest.raises((ProductFamilyAuthoringApplicationError, IdentityAccessDenied)):
                await SystemFamilyPublicationService(
                    curator_runtime.repository, target_services.family_authoring,
                    target_services.resources, inaccessible,
                    bind_identity=bind_tenant_execution_context,
                ).publish(management_scope, command)

            # Damage only this test's owned payload, leaving its immutable metadata untouched.
            source_asset = next(asset for asset in source_family.assets
                                if asset.asset_role is ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE)
            family_payload = storage._logical_directory(source_services.family_assets.candidate_storage_key(
                source_family.family, source_asset,
            )) / "payload"
            geometry_asset = next(asset for asset in source_geometry.assets
                                  if asset.asset_role is GeometryResourceAssetRole.CANONICAL_GEOMETRY)
            geometry_payload = storage._logical_directory(source_services.resources.resource_storage_key(
                source_geometry, geometry_asset,
            )) / "payload"
            for payload in (family_payload, geometry_payload):
                original_payload = payload.read_bytes()
                for fault in ("missing", "corrupt"):
                    try:
                        if fault == "missing":
                            payload.unlink()
                        else:
                            payload.write_bytes(original_payload + b"corrupt")
                        with pytest.raises(ProductFamilyAuthoringApplicationError) as invalid:
                            await publication.publish(management_scope, command)
                        assert invalid.value.code == "SYSTEM_PUBLICATION_SOURCE_INVALID"
                        assert invalid.value.phase == "preflight"
                        assert invalid.value.retryable is False
                    finally:
                        payload.write_bytes(original_payload)

            interrupted_resources = _InterruptAfterGeometryCommit(target_services.resources)
            with pytest.raises(asyncio.CancelledError):
                await SystemFamilyPublicationService(
                    curator_runtime.repository, target_services.family_authoring,
                    interrupted_resources, source,
                    bind_identity=bind_tenant_execution_context,
                ).publish(management_scope, command)
            assert interrupted_resources.committed is not None
            with bind_tenant_execution_context(management_scope.identity):
                operation_id = await _copy_operation_id(admin, management_scope.identity.task_id)
                interrupted = await curator_runtime.repository.get_product_family_authoring_operation_state(
                    management_scope, operation_id=operation_id,
                )
                assert interrupted is not None
                assert interrupted.status is CriticalOperationStatus.INDETERMINATE
                saved_plan = interrupted.detail.system_copy
                assert saved_plan is not None and len(saved_plan.geometry_copies) == 1
                assert saved_plan.source_owner_scope == source_family.family.owner_scope
                assert saved_plan.geometry_copies[0].source_owner_scope == source_geometry.resource.owner_scope
                assert await curator_runtime.repository.get_exact_product_family_version(
                    management_scope.identity, family_id=interrupted.target_product_family_id,
                    family_version_id=interrupted.detail.version_draft.family_version_id,
                ) is None

            restarted = _create_runtime(curator_url)
            runtimes.append(restarted)
            await restarted.database.verify_ready()
            loss = _LoseFamilyCommitResponse(restarted.repository)
            restarted_services = _services(loss, storage, geometry_runtime, realization_runtime)
            unavailable_source = _UnavailableOriginalSource(source)
            committed = await SystemFamilyPublicationService(
                loss, restarted_services.family_authoring, restarted_services.resources, unavailable_source,
                bind_identity=bind_tenant_execution_context,
            ).publish(management_scope, command)
            assert loss.lost_responses == 1
            reopened = _create_runtime(curator_url)
            runtimes.append(reopened)
            await reopened.database.verify_ready()
            reopened_services = _services(reopened.repository, storage, geometry_runtime, realization_runtime)
            replayed = await SystemFamilyPublicationService(
                reopened.repository, reopened_services.family_authoring, reopened_services.resources, unavailable_source,
                bind_identity=bind_tenant_execution_context,
            ).publish(management_scope, command)
            assert replayed == committed
            assert committed.operation.operation.operation_id == operation_id
            assert committed.operation.operation.status is CriticalOperationStatus.SUCCEEDED
            assert committed.operation.operation.detail.system_copy == saved_plan
            for changed in (
                command.model_copy(update={"source_family_version_id": f"different-source-{suffix}"}),
                command.model_copy(update={"source_family_id": f"different-source-family-{suffix}"}),
            ):
                with pytest.raises(ProductFamilyAuthoringApplicationError) as conflict:
                    await SystemFamilyPublicationService(
                        reopened.repository, reopened_services.family_authoring,
                        reopened_services.resources, unavailable_source,
                        bind_identity=bind_tenant_execution_context,
                    ).publish(management_scope, changed)
                assert conflict.value.code == "PRODUCT_FAMILY_IDEMPOTENCY_CONFLICT"
            assert unavailable_source.read_calls == []
            assert runtime_calls == []

        target = committed.family_version
        assert target.family.owner_scope.scope_kind is ResourceOwnerScopeKind.SYSTEM
        assert target.family.family_id != source_family.family.family_id
        assert target.version.family_version_id != source_family.version.family_version_id
        assert target.version.version_no == 1 and target.version.parent_family_version_id is None
        assert target.version.family_contract == source_family.version.family_contract
        for name in ("program_entry", "usage_instructions"):
            assert getattr(target.version, name) == getattr(source_family.version, name)
        with bind_tenant_execution_context(management_scope.identity):
            target_bytes = await _family_bytes(reopened_services.family_assets, target)
            target_manifest, target_brep = await reopened_services.family_assets.standard_model_assets(target)
        for role in (ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE,
                     ProductFamilyVersionAssetRole.STANDARD_MODEL_GEOMETRY):
            assert target_bytes[role] == original_family_bytes[role]
        assert target_brep == original_brep
        assert target_manifest == original_manifest.model_copy(update={
            "family_version_id": target.version.family_version_id,
            "binding_digest": product_geometry_binding_digest(
                (binding.binding_key, binding.expected_resource_kind, binding.geometry_content_digest)
                for binding in target.geometry_bindings
            ),
        })
        copied_geometry = saved_plan.geometry_copies[0].target_version
        assert copied_geometry.resource.resource_id == interrupted_resources.committed.resource.resource_id
        assert copied_geometry.version.resource_version_id == interrupted_resources.committed.version.resource_version_id
        assert copied_geometry.resource.owner_scope.scope_kind is ResourceOwnerScopeKind.SYSTEM
        with bind_tenant_execution_context(management_scope.identity):
            assert await _geometry_bytes(reopened_services.resources, management_scope.identity, copied_geometry) == original_geometry_bytes
        async with admin.connect() as connection:
            assert await connection.scalar(select(func.count()).select_from(GeometryResourceModel).where(
                GeometryResourceModel.resource_id == copied_geometry.resource.resource_id,
            )) == 1
            assert await connection.scalar(select(func.count()).select_from(GeometryResourceVersionModel).where(
                GeometryResourceVersionModel.resource_id == copied_geometry.resource.resource_id,
            )) == 1

        for read_scope in (same_tenant_scope, other_tenant_scope):
            with bind_tenant_execution_context(read_scope.identity):
                restored = await source_runtime.repository.get_exact_product_family_version(
                    read_scope.identity, family_id=target.family.family_id,
                    family_version_id=target.version.family_version_id,
                )
                assert restored == target
                assert await source_runtime.repository.get_exact_product_family_version(
                    read_scope.identity, family_id=source_family.family.family_id,
                    family_version_id=source_family.version.family_version_id,
                ) is None
                assert await source_services.resources.get_exact_version(
                    read_scope.identity, resource_id=copied_geometry.resource.resource_id,
                    resource_version_id=copied_geometry.version.resource_version_id,
                ) is not None
        with bind_tenant_execution_context(other_tenant_scope.identity):
            request, execution, preview = await source_services.builds.build(
                other_tenant_scope, family_version=target, invocation=scenarios[-1].to_invocation(),
            )
        assert preview is None
        assert execution.realization.proposal.status is ProductRealizationStatus.SOLVED
        assert request.source_bundle == original_family_bytes[ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE]
        assert len(execution.realization.instance_receipts) == 1
        receipt = execution.realization.instance_receipts[0]
        assert receipt.fragment_key == "body_shape" and receipt.binding_key == "body_geometry"
        assert receipt.normalized_parameters == {}
        assert receipt.interfaces and {item.interface_key for item in receipt.interfaces} == {"ring_outer", "ring_join"}
        assert execution.instance_geometries is not None
        assert execution.instance_geometries[receipt.fragment_key].sha256 == receipt.geometry_sha256
        assert request.geometry_bindings[0].geometry_content_digest == target.geometry_bindings[0].geometry_content_digest
        assert request.geometry_bindings[0].resource_kind is GeometryResourceKind.MAPPED_MODEL
        assert target.geometry_bindings[0].expected_resource_kind is GeometryResourceKind.MAPPED_MODEL
        assert request.geometry_bindings[0].geometry is None
        assert execution.instance_geometries[receipt.fragment_key].payload == original_geometry_bytes[GeometryResourceAssetRole.CANONICAL_GEOMETRY]

        await _assert_private_dependency_rejected(reopened, management_scope, target, source_geometry)
        with bind_tenant_execution_context(source_scope.identity):
            assert await source_runtime.repository.get_exact_product_family_version(
                source_scope.identity, family_id=source_family.family.family_id,
                family_version_id=source_family.version.family_version_id,
            ) == source_family
            assert await source_services.resources.get_exact_version(
                source_scope.identity, resource_id=source_geometry.resource.resource_id,
                resource_version_id=source_geometry.version.resource_version_id,
            ) == source_geometry
            assert await _geometry_bytes(source_services.resources, source_scope.identity, source_geometry) == original_geometry_bytes
            assert await source_runtime.repository.get_operation(
                source_scope, operation_id=original_source_operation.operation.operation_id,
            ) == original_source_operation
            assert await source_runtime.repository.get_geometry_authoring_operation(
                source_scope, operation_id=geometry_result.operation.operation.operation_id,
            ) == geometry_result.operation
            assert await _family_bytes(source_services.family_assets, source_family) == original_family_bytes
    finally:
        await realization_runtime.close()
        await geometry_runtime.close()
        for runtime in reversed(runtimes):
            await runtime.close()
        try:
            await _cleanup_owned_case(admin, tasks)
        finally:
            await admin.dispose()
            _drop_test_runtime_logins(TEST_DATABASE_URL, logins)
