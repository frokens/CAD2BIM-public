"""Composition root for the PostgreSQL-backed Direct runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.identity import ConfiguredLocalSubject
from CAD2BIM_backend.infrastructure.postgres.database import Database, create_database
from CAD2BIM_backend.infrastructure.postgres.identity_directory import (
    PostgresIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.postgres.repository import (
    PostgresControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from CAD2BIM_backend.ifc_application_resources import (
    create_authoring_application_resources,
    create_ifc_application_resources,
    connect_product_apply_service,
    connect_product_edit_service,
    connect_product_read_service,
)
from CAD2BIM_backend.infrastructure.data_transform.python_worker_environment import (
    PythonWorkerEnvironmentAttestation,
    verify_attested_environment,
)

if TYPE_CHECKING:
    from CAD2BIM_backend.application.code_discovery_service import CodeDiscoveryService
    from CAD2BIM_backend.application.data_runtime_service import (
        DataRuntimeService,
        DirectScopeResolver,
        DirectSessionService,
        ScopeBinder,
    )
    from CAD2BIM_backend.application.data_query_service import DataQueryService
    from CAD2BIM_backend.application.data_query_python_service import DataQueryPythonService
    from CAD2BIM_backend.application.data_query_sql_service import DataQuerySqlService
    from CAD2BIM_backend.application.data_json_service import DataJsonService
    from CAD2BIM_backend.application.data_transform_service import DataTransformService
    from CAD2BIM_backend.application.cad_measurement_service import CadMeasurementService
    from CAD2BIM_backend.application.ifc_model_service import IfcModelService
    from CAD2BIM_backend.application.ifc_query_service import IfcQueryService
    from CAD2BIM_backend.application.ifc_geometry_basis_service import (
        IfcGeometryBasisService,
    )
    from CAD2BIM_backend.application.ifc_project_service import IfcProjectService
    from CAD2BIM_backend.application.ifc_trusted_baseline_service import IfcTrustedBaselineService
    from CAD2BIM_backend.application.ifc_spatial_structure_service import (
        IfcSpatialStructureService,
    )
    from CAD2BIM_backend.application.ifc_write_coordinator import IfcWriteCoordinator
    from CAD2BIM_backend.application.geometry_resource_service import (
        GeometryResourceService,
    )
    from CAD2BIM_backend.application.geometry_catalog_service import GeometryCatalogService
    from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
    from CAD2BIM_backend.application.geometry_query_service import GeometryQueryService
    from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionService
    from CAD2BIM_backend.application.geometry_authoring_service import GeometryAuthoringService
    from CAD2BIM_backend.application.geometry_source_service import GeometrySourceService
    from CAD2BIM_backend.application.product_family_catalog_service import (
        ProductFamilyCatalogService,
    )
    from CAD2BIM_backend.application.product_family_authoring_service import (
        ProductFamilyAuthoringService,
    )
    from CAD2BIM_backend.application.product_realization_input_service import (
        ProductRealizationInputService,
    )
    from CAD2BIM_backend.application.product_apply_service import IfcProductsApplyService
    from CAD2BIM_backend.application.ifc_products_read_service import IfcProductsReadService
    from CAD2BIM_backend.application.product_realization_preview_service import (
        ProductRealizationPreviewService,
    )
    from CAD2BIM_backend.infrastructure.geometry_runtime import OcpCadQueryGeometryRuntime
    from CAD2BIM_backend.infrastructure.ifc_query import IfcOpenShellQueryWorker
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution import (
        OcpGeometryCodecExecutor,
    )
    from CAD2BIM_backend.infrastructure.ifc_revision_runtime import (
        IfcOpenShellRevisionRuntime,
    )
    from CAD2BIM_backend.infrastructure.product_realization_runtime import (
        OcpCadQueryProductRealizationRuntime,
    )
    from CAD2BIM_backend.infrastructure.data_query.snapshot_cache import QuerySnapshotCache
    from CAD2BIM_backend.infrastructure.data_transform import (
        SubprocessDuckDBDataTransformWorker,
        SubprocessPythonArrowDataTransformWorker,
    )


class DirectRuntimeBootstrapConfig(Protocol):
    subject: ConfiguredLocalSubject
    data_query_cursor_key: str
    python_worker_executable: Path
    python_worker_attestation: PythonWorkerEnvironmentAttestation
    host_python_max_concurrent_workers: int

    def backend_settings(self) -> Settings: ...


@dataclass(slots=True)
class DirectRuntimeResources:
    """Resources owned by one Direct MCP process."""

    application: DataRuntimeService
    query_service: DataQueryService
    query_python_service: DataQueryPythonService
    json_service: DataJsonService
    query_sql_service: DataQuerySqlService
    query_snapshots: QuerySnapshotCache
    transform_service: DataTransformService
    cad_measurement_service: CadMeasurementService
    ifc_model_service: IfcModelService
    ifc_trusted_baseline_service: IfcTrustedBaselineService
    ifc_geometry_basis_service: IfcGeometryBasisService
    ifc_project_service: IfcProjectService
    ifc_spatial_structure_service: IfcSpatialStructureService
    ifc_query_service: IfcQueryService
    ifc_revision_runtime: IfcOpenShellRevisionRuntime
    ifc_write_coordinator: IfcWriteCoordinator
    geometry_resource_service: GeometryResourceService
    geometry_catalog_service: GeometryCatalogService
    geometry_source_service: GeometrySourceService
    geometry_preview_service: GeometryPreviewService
    geometry_query_service: GeometryQueryService
    geometry_inspection_service: GeometryInspectionService
    geometry_authoring_service: GeometryAuthoringService
    product_family_catalog_service: ProductFamilyCatalogService
    product_family_authoring_service: ProductFamilyAuthoringService
    product_realization_input_service: ProductRealizationInputService
    product_realization_preview_service: ProductRealizationPreviewService
    product_realization_runtime: OcpCadQueryProductRealizationRuntime
    geometry_runtime: OcpCadQueryGeometryRuntime
    geometry_codec: OcpGeometryCodecExecutor
    ifc_query_worker: IfcOpenShellQueryWorker
    transform_worker: SubprocessDuckDBDataTransformWorker
    python_transform_worker: SubprocessPythonArrowDataTransformWorker
    sessions: DirectSessionService
    scope_resolver: DirectScopeResolver
    scope_binder: ScopeBinder
    database: Database
    storage: LocalStorage
    python_worker_executable: Path
    python_worker_attestation: PythonWorkerEnvironmentAttestation
    code_discovery_service: CodeDiscoveryService | None = None
    reference_service: OpaqueReferenceService | None = None
    products_apply_service: IfcProductsApplyService | None = None
    products_edit_service: object | None = None
    products_read_service: IfcProductsReadService | None = None

    async def verify_ready(self) -> None:
        """Check process prerequisites without running business capability samples."""
        await asyncio.to_thread(
            verify_attested_environment,
            self.python_worker_executable,
            self.python_worker_attestation,
        )
        try:
            await self.database.verify_ready()
        except Exception:
            raise RuntimeError(
                "DATABASE_READINESS_FAILED: verify the configured PostgreSQL service, "
                "login permissions and Alembic schema revision; recover the database "
                "before restarting Direct MCP. Data Python Worker rebuild is not required "
                "for this failure."
            ) from None
        await self.storage.verify_ready()

    async def close(self) -> None:
        # Writes may outlive a disconnected caller. Drain their owner before
        # closing the workers or database used to reconcile the original action.
        await self.ifc_write_coordinator.close()
        try:
            await self.query_snapshots.close()
        finally:
            try:
                await self.ifc_revision_runtime.close()
            finally:
                try:
                    await self.ifc_query_worker.close()
                finally:
                    try:
                        await self.geometry_runtime.close()
                    finally:
                        try:
                            await self.product_realization_runtime.close()
                        finally:
                            try:
                                await self.geometry_codec.close()
                            finally:
                                try:
                                    await self.python_transform_worker.close()
                                finally:
                                    try:
                                        await self.cad_measurement_service.close()
                                    finally:
                                        try:
                                            await self.transform_worker.close()
                                        finally:
                                            await self.database.dispose()


def create_direct_runtime_resources(
    config: DirectRuntimeBootstrapConfig,
) -> DirectRuntimeResources:
    """Build the durable Direct runtime without a development-memory fallback."""

    # Keep configuration and packaging imports independent from heavy CAD dependencies.
    from CAD2BIM_backend.application.data_runtime_service import (
        DirectScopeResolver,
        DirectSessionService,
    )
    from CAD2BIM_backend.data_application_resources import create_data_application_resources
    from CAD2BIM_backend.application.cad_measurement_service import CadMeasurementService
    from CAD2BIM_backend.application.exact_payload_resolver import ExactPayloadResolver
    from CAD2BIM_backend.infrastructure.ifc.codec import MAX_IFC_REVISION_BYTES
    from CAD2BIM_backend.infrastructure.data_query import CursorCodec
    from CAD2BIM_backend.infrastructure.data_query.snapshot_cache import QuerySnapshotCache
    from CAD2BIM_backend.infrastructure.worker_runtime import HostWorkerCapacity
    from CAD2BIM_backend.infrastructure.worker_runtime.capacity import default_capacity_root
    from CAD2BIM_backend.infrastructure.cad_measurement.supervised_worker import SubprocessCadMeasurementWorker
    from CAD2BIM_backend.infrastructure.source_text import TextSourcePatchAdapter
    from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
        bind_tenant_execution_context,
    )

    settings = config.backend_settings()
    if not settings.database_url:
        raise ValueError("CAD2BIM_DATABASE_URL is required for Direct MCP")
    database = create_database(settings)
    repository = PostgresControlPlaneRepository(database.session_factory)
    identity_directory = PostgresIdentityDirectory(database.session_factory)
    storage = LocalStorage(
        settings.storage_root,
        max_size_bytes=max(settings.max_upload_bytes, MAX_IFC_REVISION_BYTES),
    )
    artifacts = ArtifactService(repository, storage)
    geometry_capacity = HostWorkerCapacity(
        max_concurrent_workers=config.host_python_max_concurrent_workers
    )
    sql_capacity = HostWorkerCapacity(max_concurrent_workers=2, capacity_root=default_capacity_root(pool="sql"), resource_pool="sql")
    cad_capacity = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=default_capacity_root(pool="cad"), resource_pool="cad")
    cursor_codec = CursorCodec(config.data_query_cursor_key)
    payload_resolver = ExactPayloadResolver(repository, artifacts)
    source_patch_adapter = TextSourcePatchAdapter()
    ifc = create_ifc_application_resources(
        repository=repository,
        storage=storage,
        artifacts=artifacts,
        payloads=payload_resolver,
        cursor_codec=cursor_codec,
        source_patches=source_patch_adapter,
        capacity=geometry_capacity,
    )
    ifc_revision_runtime = ifc.revision_runtime
    ifc_write_coordinator = ifc.write_coordinator
    ifc_model_service = ifc.model
    ifc_geometry_basis_service = ifc.geometry_basis
    ifc_project_service = ifc.project
    query_snapshots = QuerySnapshotCache()
    ifc_spatial_structure_service = ifc.spatial_structure
    # Both entries own the same Geometry/Family graph and native Worker pool.
    authoring = create_authoring_application_resources(
        repository=repository,
        storage=storage,
        artifacts=artifacts,
        cursor_codec=cursor_codec,
        capacity=geometry_capacity,
    )
    connect_product_apply_service(ifc=ifc, authoring=authoring, repository=repository,
                                 payloads=payload_resolver, storage=storage)
    geometry_codec = authoring.geometry_codec
    geometry_runtime = authoring.geometry_runtime
    source_bundles = authoring.source_bundles
    geometry_resource_service = authoring.geometry_resources
    geometry_catalog_service = authoring.geometry_catalog
    product_family_asset_service = authoring.family_assets
    product_family_catalog_service = authoring.family_catalog
    product_realization_runtime = authoring.realization_runtime
    product_realization_input_service = authoring.realization_inputs
    product_family_authoring_service = authoring.family_authoring
    geometry_source_service = authoring.geometry_source
    geometry_preview_service = authoring.geometry_preview
    product_realization_preview_service = authoring.realization_preview
    geometry_query_service = authoring.geometry_query
    geometry_authoring_service = authoring.geometry_authoring
    ifc_query_worker = ifc.query_worker
    ifc_query_service = ifc.query
    data = create_data_application_resources(
        repository=repository,
        artifacts=artifacts,
        cursor_codec=cursor_codec,
        payload_resolver=payload_resolver,
        source_patch_adapter=source_patch_adapter,
        query_snapshots=query_snapshots,
        sql_capacity=sql_capacity,
        python_capacity=geometry_capacity,
        python_worker_executable=str(config.python_worker_executable),
        geometry_resources=geometry_resource_service,
        geometry_catalog=geometry_catalog_service,
        geometry_sources=geometry_source_service,
        geometry_queries=geometry_query_service,
        ifc_queries=ifc_query_service,
        ifc_revisions=ifc.revision_reader,
        ifc_worker=ifc.query_worker,
        geometry_authoring=geometry_authoring_service,
        product_family_authoring=product_family_authoring_service,
        product_family_catalog=product_family_catalog_service,
        product_family_assets=product_family_asset_service,
        source_bundles=source_bundles,
    )
    connect_product_read_service(ifc=ifc, data_runtime=data.runtime,
        repository=repository, payloads=payload_resolver)
    connect_product_edit_service(ifc=ifc, authoring=authoring, repository=repository,
        payloads=payload_resolver, storage=storage, references=data.references)
    cad_measurement_service = CadMeasurementService(
        repository,
        artifacts,
        payload_resolver,
        worker=SubprocessCadMeasurementWorker(capacity=cad_capacity),
    )
    sessions = DirectSessionService(
        repository,
        identity_directory,
        config.subject.subject,
        bind_tenant_execution_context,
    )
    scope_resolver = DirectScopeResolver(
        repository,
        identity_directory,
        config.subject.subject,
        bind_tenant_execution_context,
    )
    return DirectRuntimeResources(
        application=data.runtime,
        code_discovery_service=data.code_discovery,
        query_service=data.query,
        query_python_service=data.query_python,
        json_service=data.json,
        query_sql_service=data.query_sql,
        query_snapshots=query_snapshots,
        transform_service=data.transform,
        cad_measurement_service=cad_measurement_service,
        ifc_model_service=ifc_model_service,
        ifc_trusted_baseline_service=ifc.trusted_baseline,
        products_apply_service=ifc.products_apply,
        products_edit_service=ifc.products_edit,
        products_read_service=ifc.products_read,
        ifc_geometry_basis_service=ifc_geometry_basis_service,
        ifc_project_service=ifc_project_service,
        ifc_spatial_structure_service=ifc_spatial_structure_service,
        ifc_query_service=ifc_query_service,
        ifc_revision_runtime=ifc_revision_runtime,
        ifc_write_coordinator=ifc_write_coordinator,
        geometry_resource_service=geometry_resource_service,
        geometry_catalog_service=geometry_catalog_service,
        geometry_source_service=geometry_source_service,
        geometry_preview_service=geometry_preview_service,
        geometry_query_service=geometry_query_service,
        geometry_inspection_service=authoring.geometry_inspection,
        geometry_authoring_service=geometry_authoring_service,
        product_family_catalog_service=product_family_catalog_service,
        product_family_authoring_service=product_family_authoring_service,
        product_realization_input_service=product_realization_input_service,
        product_realization_preview_service=product_realization_preview_service,
        product_realization_runtime=product_realization_runtime,
        geometry_runtime=geometry_runtime,
        geometry_codec=geometry_codec,
        ifc_query_worker=ifc_query_worker,
        transform_worker=data.transform_worker,
        python_transform_worker=data.python_transform_worker,
        sessions=sessions,
        scope_resolver=scope_resolver,
        scope_binder=bind_tenant_execution_context,
        reference_service=data.references,
        database=database,
        storage=storage,
        python_worker_executable=config.python_worker_executable,
        python_worker_attestation=config.python_worker_attestation,
    )


__all__ = [
    "DirectRuntimeBootstrapConfig",
    "DirectRuntimeResources",
    "create_direct_runtime_resources",
]
