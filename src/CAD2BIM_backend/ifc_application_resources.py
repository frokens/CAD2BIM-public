"""Shared IFC Application composition used by Direct and Runner runtimes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from CAD2BIM_backend.application.artifact_service import ArtifactService
    from CAD2BIM_backend.application.exact_payload_resolver import ExactPayloadResolver
    from CAD2BIM_backend.application.geometry_authoring_service import GeometryAuthoringService
    from CAD2BIM_backend.application.geometry_catalog_service import GeometryCatalogService
    from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
    from CAD2BIM_backend.application.geometry_query_service import GeometryQueryService
    from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionService
    from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService
    from CAD2BIM_backend.application.geometry_source_service import GeometrySourceService
    from CAD2BIM_backend.application.ifc_geometry_basis_service import IfcGeometryBasisService
    from CAD2BIM_backend.application.ifc_model_service import IfcModelService
    from CAD2BIM_backend.application.ifc_project_service import IfcProjectService
    from CAD2BIM_backend.application.ifc_query_service import IfcQueryService
    from CAD2BIM_backend.application.ifc_revision_reader import IfcRevisionReader
    from CAD2BIM_backend.application.ifc_revision_writer import IfcRevisionWriter
    from CAD2BIM_backend.application.ifc_spatial_structure_service import IfcSpatialStructureService
    from CAD2BIM_backend.application.ifc_trusted_baseline_service import IfcTrustedBaselineService
    from CAD2BIM_backend.application.ifc_write_coordinator import IfcWriteCoordinator
    from CAD2BIM_backend.application.product_family_asset_service import ProductFamilyAssetService
    from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringService
    from CAD2BIM_backend.application.product_family_catalog_service import ProductFamilyCatalogService
    from CAD2BIM_backend.application.product_realization_input_service import ProductRealizationInputService
    from CAD2BIM_backend.application.product_realization_preview_service import ProductRealizationPreviewService
    from CAD2BIM_backend.application.product_apply_service import IfcProductsApplyService
    from CAD2BIM_backend.application.product_edit_service import IfcProductsEditService
    from CAD2BIM_backend.application.ifc_products_read_service import IfcProductsReadService
    from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftService
    from CAD2BIM_backend.application.authoring_publication_service import AuthoringPublicationService
    from CAD2BIM_backend.infrastructure.data_query import CursorCodec
    from CAD2BIM_backend.infrastructure.geometry_runtime import OcpCadQueryGeometryRuntime
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution import OcpGeometryCodecExecutor
    from CAD2BIM_backend.infrastructure.ifc_query import IfcOpenShellQueryWorker
    from CAD2BIM_backend.infrastructure.ifc_revision_runtime import IfcOpenShellRevisionRuntime
    from CAD2BIM_backend.infrastructure.postgres.repository import PostgresControlPlaneRepository
    from CAD2BIM_backend.infrastructure.product_realization_runtime import OcpCadQueryProductRealizationRuntime
    from CAD2BIM_backend.infrastructure.source_text import TextSourcePatchAdapter
    from CAD2BIM_backend.infrastructure.source_text.geometry_authoring import SourceTextGeometryAuthoringSource
    from CAD2BIM_backend.infrastructure.storage import LocalStorage
    from CAD2BIM_backend.infrastructure.worker_runtime import HostWorkerCapacity


@dataclass(slots=True)
class IfcApplicationResources:
    model: IfcModelService
    trusted_baseline: IfcTrustedBaselineService
    geometry_basis: IfcGeometryBasisService
    project: IfcProjectService
    spatial_structure: IfcSpatialStructureService
    query: IfcQueryService
    revision_reader: IfcRevisionReader
    revision_writer: IfcRevisionWriter
    write_coordinator: IfcWriteCoordinator
    revision_runtime: IfcOpenShellRevisionRuntime
    query_worker: IfcOpenShellQueryWorker
    products_apply: IfcProductsApplyService | None = None
    products_edit: IfcProductsEditService | None = None
    products_read: IfcProductsReadService | None = None

    async def close(self) -> None:
        await self.write_coordinator.close()
        try:
            await self.query_worker.close()
        finally:
            await self.revision_runtime.close()


@dataclass(slots=True)
class AuthoringApplicationResources:
    """One shared Geometry/Family Application graph for Direct or Runner."""

    geometry_codec: OcpGeometryCodecExecutor
    geometry_runtime: OcpCadQueryGeometryRuntime
    source_bundles: SourceTextGeometryAuthoringSource
    geometry_resources: GeometryResourceService
    geometry_catalog: GeometryCatalogService
    geometry_source: GeometrySourceService
    geometry_preview: GeometryPreviewService
    geometry_query: GeometryQueryService
    geometry_inspection: GeometryInspectionService
    geometry_authoring: GeometryAuthoringService
    family_assets: ProductFamilyAssetService
    family_catalog: ProductFamilyCatalogService
    family_authoring: ProductFamilyAuthoringService
    realization_inputs: ProductRealizationInputService
    realization_preview: ProductRealizationPreviewService
    realization_runtime: OcpCadQueryProductRealizationRuntime

    async def close(self) -> None:
        try:
            await self.geometry_runtime.close()
        finally:
            try:
                await self.realization_runtime.close()
            finally:
                await self.geometry_codec.close()


async def resolve_authoring_resource_owner(repository, scope):
    """Use the saved server-selected Component Run route, never public metadata."""
    from CAD2BIM_backend.domain.control_plane import ExecutionMode, ExecutionProtocolStatus
    from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope, ResourceOwnerScopeKind
    from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftError

    kind = ResourceOwnerScopeKind.TASK
    if scope.execution_mode is ExecutionMode.GRAPH:
        run = await repository.get_run_with_execution_identity(scope.owner_id, scope.run_id)
        if (run is None or run.owner_id != scope.owner_id or run.task_id != scope.identity.task_id
            or run.tenant_id != scope.identity.tenant_id or run.principal_id != scope.identity.principal_id
            or run.execution_mode is not ExecutionMode.GRAPH):
            raise AuthoringDraftError("AUTHORING_SCOPE_INVALID", "The authenticated authoring Run is unavailable")
        if run.module == "component":
            if (run.execution_protocol_status is not ExecutionProtocolStatus.PINNED
                or run.execution_protocol is None or run.execution_protocol.module != "component"
                or run.execution_protocol.stage_key != run.stage_key):
                raise AuthoringDraftError("AUTHORING_SCOPE_INVALID", "Component authoring requires its server-selected Run route")
            kind = ResourceOwnerScopeKind.TENANT
    return ResourceOwnerScope(scope_kind=kind, tenant_id=scope.identity.tenant_id,
        task_id=scope.identity.task_id if kind is ResourceOwnerScopeKind.TASK else None)


def create_authoring_application_resources(
    *,
    repository: PostgresControlPlaneRepository,
    storage: LocalStorage,
    artifacts: ArtifactService,
    cursor_codec: CursorCodec,
    capacity: HostWorkerCapacity,
) -> AuthoringApplicationResources:
    """Compose existing authoring owners once for both authenticated entries."""

    from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftService
    from CAD2BIM_backend.application.authoring_publication_service import AuthoringPublicationService

    from CAD2BIM_backend.application.geometry_authoring_service import GeometryAuthoringService
    from CAD2BIM_backend.application.geometry_catalog_service import GeometryCatalogService
    from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
    from CAD2BIM_backend.application.geometry_query_service import GeometryQueryService
    from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionService
    from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService
    from CAD2BIM_backend.application.geometry_source_service import GeometrySourceService
    from CAD2BIM_backend.application.product_family_asset_service import ProductFamilyAssetService
    from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringService
    from CAD2BIM_backend.application.product_family_catalog_service import ProductFamilyCatalogService
    from CAD2BIM_backend.application.product_realization_input_service import ProductRealizationInputService
    from CAD2BIM_backend.application.product_realization_preview_service import ProductRealizationPreviewService
    from CAD2BIM_backend.infrastructure.geometry_runtime import OcpCadQueryGeometryRuntime
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution import (
        GEOMETRY_CODEC_BUDGET,
        OcpGeometryCodecExecutor,
    )
    from CAD2BIM_backend.infrastructure.product_realization_runtime import OcpCadQueryProductRealizationRuntime
    from CAD2BIM_backend.infrastructure.source_text.geometry_authoring import SourceTextGeometryAuthoringSource
    from CAD2BIM_backend.infrastructure.source_text.source_patch_adapter import TextSourcePatchAdapter

    codec = OcpGeometryCodecExecutor(budget=GEOMETRY_CODEC_BUDGET, capacity=capacity)
    geometry_runtime = OcpCadQueryGeometryRuntime(capacity=capacity, codec_executor=codec)
    source_bundles = SourceTextGeometryAuthoringSource()
    source_patch = TextSourcePatchAdapter()
    geometry_resources = GeometryResourceService(
        repository,
        storage,
        geometry_runtime=geometry_runtime,
        source_bundle_packer=lambda members: source_bundles.pack(members).content,
    )
    geometry_catalog = GeometryCatalogService(geometry_resources, cursor_codec)
    family_assets = ProductFamilyAssetService(storage, source_bundles)
    family_catalog = ProductFamilyCatalogService(
        repository, geometry_resources, geometry_catalog, family_assets, cursor_codec,
    )
    realization_runtime = OcpCadQueryProductRealizationRuntime(
        capacity=capacity, codec_executor=codec,
    )
    realization_inputs = ProductRealizationInputService(
        family_assets, geometry_resources, runtime=realization_runtime,
        family_repository=repository,
    )
    geometry_inspection = GeometryInspectionService(artifacts, repository, cursor_codec, family_catalog=family_catalog)
    geometry_preview = GeometryPreviewService(
        geometry_catalog, geometry_resources, geometry_runtime, family_catalog, geometry_inspection,
        family_assets=family_assets,
    )
    drafts = AuthoringDraftService(artifacts, repository, cursor_codec,
        owner_resolver=lambda scope: resolve_authoring_resource_owner(repository, scope))
    geometry_authoring = GeometryAuthoringService(repository, geometry_resources,
        source_bundles, source_patch=source_patch, drafts=drafts, catalog=geometry_catalog)
    family_authoring = ProductFamilyAuthoringService(repository, artifacts, family_catalog, family_assets,
        realization_inputs, source_bundles, source_patch=source_patch, drafts=drafts, geometry_authoring=geometry_authoring)
    publication = AuthoringPublicationService(repository, drafts, geometry_authoring, family_authoring, geometry_inspection,
        geometry_runtime=geometry_runtime, realization_runtime=realization_runtime)
    geometry_authoring._publication = publication
    family_authoring._publication = publication
    return AuthoringApplicationResources(
        geometry_codec=codec,
        geometry_runtime=geometry_runtime,
        source_bundles=source_bundles,
        geometry_resources=geometry_resources,
        geometry_catalog=geometry_catalog,
        geometry_source=GeometrySourceService(geometry_catalog, geometry_resources),
        geometry_preview=geometry_preview,
        geometry_inspection=geometry_inspection,
        geometry_query=GeometryQueryService(
            repository, artifacts, geometry_resources, geometry_runtime, cursor_codec,
            inspections=geometry_inspection,
            family_catalog=family_catalog, family_assets=family_assets,
        ),
        geometry_authoring=geometry_authoring,
        family_assets=family_assets,
        family_catalog=family_catalog,
        family_authoring=family_authoring,
        realization_inputs=realization_inputs,
        realization_preview=ProductRealizationPreviewService(
            family_catalog, realization_inputs, realization_runtime, geometry_preview, geometry_inspection,
        ),
        realization_runtime=realization_runtime,
    )


def create_system_family_publication_service(
    *, repository: PostgresControlPlaneRepository, storage: LocalStorage,
    artifacts: ArtifactService, cursor_codec: CursorCodec, source,
    bind_identity,
):
    """Compose Backend copy owners without allocating an authoring Runtime."""
    from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService
    from CAD2BIM_backend.application.geometry_catalog_service import GeometryCatalogService
    from CAD2BIM_backend.application.product_family_asset_service import ProductFamilyAssetService
    from CAD2BIM_backend.application.product_family_catalog_service import ProductFamilyCatalogService
    from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringService
    from CAD2BIM_backend.application.product_realization_input_service import ProductRealizationInputService
    from CAD2BIM_backend.application.system_family_publication_service import SystemFamilyPublicationService
    from CAD2BIM_backend.infrastructure.source_text.geometry_authoring import SourceTextGeometryAuthoringSource
    from CAD2BIM_backend.infrastructure.source_text.source_patch_adapter import TextSourcePatchAdapter

    bundles = SourceTextGeometryAuthoringSource()
    geometry = GeometryResourceService(repository, storage)
    geometry_catalog = GeometryCatalogService(geometry, cursor_codec)
    assets = ProductFamilyAssetService(storage, bundles)
    catalog = ProductFamilyCatalogService(repository, geometry, geometry_catalog, assets, cursor_codec)
    inputs = ProductRealizationInputService(assets, geometry, runtime=None, family_repository=repository)
    authoring = ProductFamilyAuthoringService(repository, artifacts, catalog, assets, inputs, bundles,
        source_patch=TextSourcePatchAdapter())
    return SystemFamilyPublicationService(repository, authoring, geometry, source, bind_identity=bind_identity)


def create_ifc_application_resources(
    *,
    repository: PostgresControlPlaneRepository,
    storage: LocalStorage,
    artifacts: ArtifactService,
    payloads: ExactPayloadResolver,
    cursor_codec: CursorCodec,
    source_patches: TextSourcePatchAdapter,
    capacity: HostWorkerCapacity,
) -> IfcApplicationResources:
    """Compose the existing IFC writer and query services without an entry identity."""

    from CAD2BIM_backend.application.ifc_geometry_basis_service import IfcGeometryBasisService
    from CAD2BIM_backend.application.ifc_model_service import IfcModelService
    from CAD2BIM_backend.application.ifc_project_service import IfcProjectService
    from CAD2BIM_backend.application.ifc_query_service import IfcQueryService
    from CAD2BIM_backend.application.ifc_revision_reader import IfcRevisionReader
    from CAD2BIM_backend.application.ifc_revision_writer import IfcRevisionWriter
    from CAD2BIM_backend.application.ifc_spatial_structure_service import IfcSpatialStructureService
    from CAD2BIM_backend.application.ifc_trusted_baseline_service import IfcTrustedBaselineService
    from CAD2BIM_backend.application.ifc_write_coordinator import IfcWriteCoordinator
    from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
    from CAD2BIM_backend.infrastructure.ifc_query import IfcOpenShellQueryWorker
    from CAD2BIM_backend.infrastructure.ifc_revision_runtime import IfcOpenShellRevisionRuntime
    from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
        bind_tenant_execution_context, current_tenant_execution_context,
    )

    revision_runtime = IfcOpenShellRevisionRuntime(capacity=capacity)
    revision_reader = IfcRevisionReader(repository, storage, revision_runtime)
    write_coordinator = IfcWriteCoordinator(
        get_execution_context=current_tenant_execution_context,
        bind_execution_context=bind_tenant_execution_context,
    )
    revision_writer = IfcRevisionWriter(
        repository, storage, revision_reader, revision_runtime
    )
    query_worker = IfcOpenShellQueryWorker(capacity=capacity)
    return IfcApplicationResources(
        trusted_baseline=IfcTrustedBaselineService(revision_writer, artifacts, write_coordinator),
        model=IfcModelService(
            repository, storage, IfcOpenShellModelCodec(), revision_writer,
            revision_runtime, write_coordinator,
        ),
        geometry_basis=IfcGeometryBasisService(
            revision_writer, revision_runtime, write_coordinator
        ),
        project=IfcProjectService(revision_writer, revision_runtime, write_coordinator),
        spatial_structure=IfcSpatialStructureService(
            revision_writer, cursor_codec, revision_runtime, write_coordinator
        ),
        query=IfcQueryService(
            repository, artifacts, payloads, revision_reader, query_worker,
            cursor_codec, source_patches,
        ),
        revision_reader=revision_reader,
        revision_writer=revision_writer,
        write_coordinator=write_coordinator,
        revision_runtime=revision_runtime,
        query_worker=query_worker,
    )


def connect_product_apply_service(*, ifc, authoring, repository, payloads, storage):
    """Wire the product slice to the same composed writer, resources and worker pool."""
    from CAD2BIM_backend.application.product_apply_service import IfcProductsApplyService

    ifc.products_apply = IfcProductsApplyService(
        families=repository, data=repository, payloads=payloads,
        inputs=authoring.realization_inputs, realization_runtime=authoring.realization_runtime,
        revision_runtime=ifc.revision_runtime, writer=ifc.revision_writer,
        coordinator=ifc.write_coordinator, storage=storage,
    )
    return ifc.products_apply


def connect_product_edit_service(*, ifc, authoring, repository, payloads, storage, references):
    """Connect product edits to the same exact resources, runtime and Writer."""
    from CAD2BIM_backend.application.product_edit_service import IfcProductsEditService

    ifc.products_edit = IfcProductsEditService(
        families=repository, data=repository, payloads=payloads,
        inputs=authoring.realization_inputs, realization_runtime=authoring.realization_runtime,
        revision_runtime=ifc.revision_runtime, writer=ifc.revision_writer,
        coordinator=ifc.write_coordinator, storage=storage,
        family_catalog=authoring.family_catalog,
        references=references,
    )
    return ifc.products_edit


def connect_product_read_service(*, ifc, data_runtime, repository, payloads):
    """Connect selected reads to the existing Operation, exact Payload and IFC owners."""
    from CAD2BIM_backend.application.ifc_products_read_service import IfcProductsReadService

    ifc.products_read = IfcProductsReadService(
        data_runtime=data_runtime, repository=repository, payloads=payloads,
        writer=ifc.revision_writer, runtime=ifc.revision_runtime)
    return ifc.products_read


__all__ = [
    "AuthoringApplicationResources",
    "IfcApplicationResources",
    "create_authoring_application_resources",
    "create_ifc_application_resources",
    "connect_product_apply_service",
    "connect_product_edit_service",
]
