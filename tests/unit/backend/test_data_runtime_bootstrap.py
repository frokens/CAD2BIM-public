from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, call

import pytest

from CAD2BIM_backend.domain.identity import ConfiguredLocalSubject
from CAD2BIM_mcp.direct import bootstrap as direct_bootstrap


def test_direct_runtime_composes_only_postgres_owned_resources(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from CAD2BIM_backend.application.data_runtime_service import (
        OperationInspection, _bounded_error_message,
    )
    from CAD2BIM_backend.application.exact_payload_resolver import ExactParquetPayload

    database = SimpleNamespace(session_factory=object())
    repository = object()
    identity_directory = object()
    storage = object()
    artifacts = object()
    application = object()
    data_query_attempt_service = object()
    code_discovery_service = object()
    query_service = object()
    query_python_service = object()
    json_service = object()
    query_sql_service = object()
    query_snapshots = object()
    payload_resolver = object()
    transform_worker = object()
    python_transform_worker = object()
    transform_service = object()
    cad_measurement_service = object()
    ifc_model_service = object()
    ifc_trusted_baseline_service = object()
    ifc_geometry_basis_service = object()
    ifc_project_service = object()
    ifc_spatial_structure_service = object()
    geometry_resource_service = object()
    geometry_catalog_service = object()
    geometry_source_service = object()
    geometry_preview_service = object()
    geometry_query_service = object()
    geometry_authoring_service = SimpleNamespace()
    geometry_authoring_source = object()
    product_family_asset_service = object()
    product_family_catalog_service = object()
    product_family_authoring_service = SimpleNamespace()
    product_realization_input_service = object()
    product_realization_preview_service = object()
    product_realization_runtime = object()
    product_realization_host_extractor = object()
    exact_artifact_reader = object()
    geometry_runtime = object()
    ifc_revision_runtime = object()
    ifc_write_coordinator = object()
    ifc_revision_reader = object()
    ifc_revision_writer = object()
    ifc_query_worker = object()
    ifc_query_service = object()
    source_patch_adapter = object()
    ifc_geometry_basis_compiler = object()
    ifc_project_compiler = object()
    ifc_spatial_structure_compiler = object()
    ifc_codec = object()
    cursor_codec = object()
    sessions = object()
    resolver = object()
    python_worker_attestation = object()
    settings = SimpleNamespace(
        database_url="postgresql+psycopg://runtime:secret@db/cad2bim",
        storage_root=tmp_path / "storage",
        max_upload_bytes=100 * 1024 * 1024,
    )
    config = SimpleNamespace(
        subject=ConfiguredLocalSubject(subject="local-user"),
        data_query_cursor_key="cursor-key-not-exposed",
        python_worker_executable=tmp_path / "m2c-python",
        python_worker_attestation=python_worker_attestation,
        host_python_max_concurrent_workers=2,
        backend_settings=Mock(return_value=settings),
    )
    database_factory = Mock(return_value=database)
    repository_factory = Mock(return_value=repository)
    identity_factory = Mock(return_value=identity_directory)
    storage_factory = Mock(return_value=storage)
    artifact_factory = Mock(return_value=artifacts)
    service_factory = Mock(return_value=application)
    data_query_attempt_service_factory = Mock(return_value=data_query_attempt_service)
    code_discovery_service_factory = Mock(return_value=code_discovery_service)
    sessions_factory = Mock(return_value=sessions)
    resolver_factory = Mock(return_value=resolver)
    query_service_factory = Mock(return_value=query_service)
    query_python_service_factory = Mock(return_value=query_python_service)
    json_service_factory = Mock(return_value=json_service)
    query_sql_service_factory = Mock(return_value=query_sql_service)
    query_snapshots_factory = Mock(return_value=query_snapshots)
    payload_resolver_factory = Mock(return_value=payload_resolver)
    transform_worker_factory = Mock(return_value=transform_worker)
    python_transform_worker_factory = Mock(return_value=python_transform_worker)
    transform_service_factory = Mock(return_value=transform_service)
    cad_measurement_service_factory = Mock(return_value=cad_measurement_service)
    ifc_model_service_factory = Mock(return_value=ifc_model_service)
    ifc_trusted_baseline_service_factory = Mock(return_value=ifc_trusted_baseline_service)
    ifc_geometry_basis_service_factory = Mock(return_value=ifc_geometry_basis_service)
    ifc_project_service_factory = Mock(return_value=ifc_project_service)
    ifc_spatial_structure_service_factory = Mock(return_value=ifc_spatial_structure_service)
    geometry_resource_service_factory = Mock(return_value=geometry_resource_service)
    geometry_catalog_service_factory = Mock(return_value=geometry_catalog_service)
    geometry_source_service_factory = Mock(return_value=geometry_source_service)
    geometry_preview_service_factory = Mock(return_value=geometry_preview_service)
    geometry_query_service_factory = Mock(return_value=geometry_query_service)
    geometry_authoring_service_factory = Mock(return_value=geometry_authoring_service)
    geometry_authoring_source_factory = Mock(return_value=geometry_authoring_source)
    product_family_asset_service_factory = Mock(return_value=product_family_asset_service)
    product_family_catalog_service_factory = Mock(return_value=product_family_catalog_service)
    product_family_authoring_service_factory = Mock(return_value=product_family_authoring_service)
    product_realization_input_service_factory = Mock(return_value=product_realization_input_service)
    product_realization_preview_service_factory = Mock(
        return_value=product_realization_preview_service
    )
    product_realization_runtime_factory = Mock(return_value=product_realization_runtime)
    product_realization_host_extractor_factory = Mock(
        return_value=product_realization_host_extractor
    )
    exact_artifact_reader_factory = Mock(return_value=exact_artifact_reader)
    geometry_runtime_factory = Mock(return_value=geometry_runtime)
    ifc_revision_runtime_factory = Mock(return_value=ifc_revision_runtime)
    ifc_write_coordinator_factory = Mock(return_value=ifc_write_coordinator)
    ifc_revision_reader_factory = Mock(return_value=ifc_revision_reader)
    ifc_revision_writer_factory = Mock(return_value=ifc_revision_writer)
    ifc_query_worker_factory = Mock(return_value=ifc_query_worker)
    ifc_query_service_factory = Mock(return_value=ifc_query_service)
    source_patch_adapter_factory = Mock(return_value=source_patch_adapter)
    ifc_geometry_basis_compiler_factory = Mock(return_value=ifc_geometry_basis_compiler)
    ifc_project_compiler_factory = Mock(return_value=ifc_project_compiler)
    ifc_spatial_structure_compiler_factory = Mock(return_value=ifc_spatial_structure_compiler)
    ifc_codec_factory = Mock(return_value=ifc_codec)
    cursor_codec_factory = Mock(return_value=cursor_codec)
    service_module = ModuleType("CAD2BIM_backend.application.data_runtime_service")
    service_module.DataRuntimeService = service_factory
    service_module.DirectSessionService = sessions_factory
    service_module.DirectScopeResolver = resolver_factory
    service_module.OperationInspection = OperationInspection
    service_module._bounded_error_message = _bounded_error_message
    data_query_attempt_service_module = ModuleType(
        "CAD2BIM_backend.application.data_query_attempt_service"
    )
    data_query_attempt_service_module.DataQueryAttemptService = data_query_attempt_service_factory
    code_discovery_service_module = ModuleType("CAD2BIM_backend.application.code_discovery_service")
    code_discovery_service_module.CodeDiscoveryService = code_discovery_service_factory
    query_service_module = ModuleType("CAD2BIM_backend.application.data_query_service")
    query_service_module.DataQueryService = query_service_factory
    query_python_service_module = ModuleType(
        "CAD2BIM_backend.application.data_query_python_service"
    )
    query_python_service_module.DataQueryPythonService = query_python_service_factory
    json_service_module = ModuleType("CAD2BIM_backend.application.data_json_service")
    json_service_module.DataJsonService = json_service_factory
    query_sql_service_module = ModuleType("CAD2BIM_backend.application.data_query_sql_service")
    query_sql_service_module.DataQuerySqlService = query_sql_service_factory
    transform_service_module = ModuleType("CAD2BIM_backend.application.data_transform_service")
    transform_service_module.DataTransformService = transform_service_factory
    cad_measurement_service_module = ModuleType(
        "CAD2BIM_backend.application.cad_measurement_service"
    )
    cad_measurement_service_module.CadMeasurementService = cad_measurement_service_factory
    ifc_model_service_module = ModuleType("CAD2BIM_backend.application.ifc_model_service")
    ifc_model_service_module.IfcModelService = ifc_model_service_factory
    ifc_trusted_baseline_module = ModuleType("CAD2BIM_backend.application.ifc_trusted_baseline_service")
    ifc_trusted_baseline_module.IfcTrustedBaselineService = ifc_trusted_baseline_service_factory
    ifc_geometry_basis_service_module = ModuleType(
        "CAD2BIM_backend.application.ifc_geometry_basis_service"
    )
    ifc_geometry_basis_service_module.IfcGeometryBasisService = ifc_geometry_basis_service_factory
    ifc_project_service_module = ModuleType("CAD2BIM_backend.application.ifc_project_service")
    ifc_project_service_module.IfcProjectService = ifc_project_service_factory
    ifc_spatial_structure_service_module = ModuleType(
        "CAD2BIM_backend.application.ifc_spatial_structure_service"
    )
    ifc_spatial_structure_service_module.IfcSpatialStructureService = (
        ifc_spatial_structure_service_factory
    )
    geometry_resource_service_module = ModuleType(
        "CAD2BIM_backend.application.geometry_resource_service"
    )
    geometry_resource_service_module.GeometryResourceService = geometry_resource_service_factory
    geometry_catalog_service_module = ModuleType(
        "CAD2BIM_backend.application.geometry_catalog_service"
    )
    geometry_catalog_service_module.GeometryCatalogService = geometry_catalog_service_factory
    geometry_source_service_module = ModuleType(
        "CAD2BIM_backend.application.geometry_source_service"
    )
    geometry_source_service_module.GeometrySourceService = geometry_source_service_factory
    geometry_preview_service_module = ModuleType(
        "CAD2BIM_backend.application.geometry_preview_service"
    )
    geometry_preview_service_module.GeometryPreviewService = geometry_preview_service_factory
    geometry_query_service_module = ModuleType("CAD2BIM_backend.application.geometry_query_service")
    geometry_query_service_module.GeometryQueryService = geometry_query_service_factory
    geometry_authoring_service_module = ModuleType(
        "CAD2BIM_backend.application.geometry_authoring_service"
    )
    geometry_authoring_service_module.GeometryAuthoringService = geometry_authoring_service_factory
    product_family_asset_service_module = ModuleType(
        "CAD2BIM_backend.application.product_family_asset_service"
    )
    product_family_asset_service_module.ProductFamilyAssetService = (
        product_family_asset_service_factory
    )
    product_family_catalog_service_module = ModuleType(
        "CAD2BIM_backend.application.product_family_catalog_service"
    )
    product_family_catalog_service_module.ProductFamilyCatalogService = (
        product_family_catalog_service_factory
    )
    product_family_authoring_service_module = ModuleType(
        "CAD2BIM_backend.application.product_family_authoring_service"
    )
    product_family_authoring_service_module.ProductFamilyAuthoringService = (
        product_family_authoring_service_factory
    )
    product_realization_input_service_module = ModuleType(
        "CAD2BIM_backend.application.product_realization_input_service"
    )
    product_realization_input_service_module.ProductRealizationInputService = (
        product_realization_input_service_factory
    )
    product_realization_preview_service_module = ModuleType(
        "CAD2BIM_backend.application.product_realization_preview_service"
    )
    product_realization_preview_service_module.ProductRealizationPreviewService = (
        product_realization_preview_service_factory
    )
    exact_artifact_reader_module = ModuleType("CAD2BIM_backend.application.exact_artifact_reader")
    exact_artifact_reader_module.ExactArtifactReader = exact_artifact_reader_factory
    ifc_revision_writer_module = ModuleType("CAD2BIM_backend.application.ifc_revision_writer")
    ifc_revision_writer_module.IfcRevisionWriter = ifc_revision_writer_factory
    ifc_revision_reader_module = ModuleType("CAD2BIM_backend.application.ifc_revision_reader")
    ifc_revision_reader_module.IfcRevisionReader = ifc_revision_reader_factory
    ifc_write_coordinator_module = ModuleType("CAD2BIM_backend.application.ifc_write_coordinator")
    ifc_write_coordinator_module.IfcWriteCoordinator = ifc_write_coordinator_factory
    ifc_query_service_module = ModuleType("CAD2BIM_backend.application.ifc_query_service")
    ifc_query_service_module.IfcQueryService = ifc_query_service_factory
    ifc_codec_module = ModuleType("CAD2BIM_backend.infrastructure.ifc.codec")
    ifc_codec_module.IfcOpenShellModelCodec = ifc_codec_factory
    ifc_codec_module.MAX_IFC_REVISION_BYTES = 512 * 1024 * 1024
    ifc_geometry_basis_module = ModuleType("CAD2BIM_backend.infrastructure.ifc.geometry_basis")
    ifc_geometry_basis_module.IfcOpenShellGeometryBasisCompiler = (
        ifc_geometry_basis_compiler_factory
    )
    ifc_project_module = ModuleType("CAD2BIM_backend.infrastructure.ifc.project")
    ifc_project_module.IfcOpenShellProjectCompiler = ifc_project_compiler_factory
    ifc_spatial_structure_module = ModuleType(
        "CAD2BIM_backend.infrastructure.ifc.spatial_structure"
    )
    ifc_spatial_structure_module.IfcOpenShellSpatialStructureCompiler = (
        ifc_spatial_structure_compiler_factory
    )
    product_realization_host_module = ModuleType(
        "CAD2BIM_backend.infrastructure.ifc.product_realization_host"
    )
    product_realization_host_module.IfcOpenShellProductRealizationHostFactExtractor = (
        product_realization_host_extractor_factory
    )
    resolver_module = ModuleType("CAD2BIM_backend.application.exact_payload_resolver")
    resolver_module.ExactPayloadResolver = payload_resolver_factory
    resolver_module.ExactParquetPayload = ExactParquetPayload
    worker_module = ModuleType("CAD2BIM_backend.infrastructure.data_transform")
    worker_module.SubprocessDuckDBDataTransformWorker = transform_worker_factory
    worker_module.SubprocessPythonArrowDataTransformWorker = python_transform_worker_factory
    cursor_module = ModuleType("CAD2BIM_backend.infrastructure.data_query")
    cursor_module.CursorCodec = cursor_codec_factory
    snapshot_module = ModuleType("CAD2BIM_backend.infrastructure.data_query.snapshot_cache")
    snapshot_module.QuerySnapshotCache = query_snapshots_factory
    geometry_runtime_module = ModuleType("CAD2BIM_backend.infrastructure.geometry_runtime")
    geometry_runtime_module.OcpCadQueryGeometryRuntime = geometry_runtime_factory
    geometry_codec = SimpleNamespace(close=AsyncMock())
    codec_factory = Mock(return_value=geometry_codec)
    codec_module = ModuleType("CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution")
    codec_module.OcpGeometryCodecExecutor = codec_factory
    codec_module.GEOMETRY_CODEC_BUDGET = object()
    monkeypatch.setitem(sys.modules, codec_module.__name__, codec_module)
    ifc_revision_runtime_module = ModuleType("CAD2BIM_backend.infrastructure.ifc_revision_runtime")
    ifc_revision_runtime_module.IfcOpenShellRevisionRuntime = ifc_revision_runtime_factory
    product_realization_runtime_module = ModuleType(
        "CAD2BIM_backend.infrastructure.product_realization_runtime"
    )
    product_realization_runtime_module.OcpCadQueryProductRealizationRuntime = (
        product_realization_runtime_factory
    )
    ifc_query_worker_module = ModuleType("CAD2BIM_backend.infrastructure.ifc_query")
    ifc_query_worker_module.IfcOpenShellQueryWorker = ifc_query_worker_factory
    source_text_module = ModuleType("CAD2BIM_backend.infrastructure.source_text")
    source_text_module.__path__ = []
    source_text_module.TextSourcePatchAdapter = source_patch_adapter_factory
    source_patch_module = ModuleType("CAD2BIM_backend.infrastructure.source_text.source_patch_adapter")
    source_patch_module.TextSourcePatchAdapter = source_patch_adapter_factory
    geometry_authoring_source_module = ModuleType(
        "CAD2BIM_backend.infrastructure.source_text.geometry_authoring"
    )
    geometry_authoring_source_module.SourceTextGeometryAuthoringSource = (
        geometry_authoring_source_factory
    )

    monkeypatch.setitem(sys.modules, service_module.__name__, service_module)
    monkeypatch.setitem(
        sys.modules,
        code_discovery_service_module.__name__,
        code_discovery_service_module,
    )
    monkeypatch.setitem(
        sys.modules,
        data_query_attempt_service_module.__name__,
        data_query_attempt_service_module,
    )
    monkeypatch.setitem(sys.modules, query_service_module.__name__, query_service_module)
    monkeypatch.setitem(
        sys.modules,
        query_python_service_module.__name__,
        query_python_service_module,
    )
    monkeypatch.setitem(sys.modules, json_service_module.__name__, json_service_module)
    monkeypatch.setitem(
        sys.modules,
        query_sql_service_module.__name__,
        query_sql_service_module,
    )
    monkeypatch.setitem(sys.modules, transform_service_module.__name__, transform_service_module)
    monkeypatch.setitem(
        sys.modules,
        cad_measurement_service_module.__name__,
        cad_measurement_service_module,
    )
    monkeypatch.setitem(sys.modules, ifc_model_service_module.__name__, ifc_model_service_module)
    monkeypatch.setitem(sys.modules, ifc_trusted_baseline_module.__name__, ifc_trusted_baseline_module)
    monkeypatch.setitem(
        sys.modules,
        ifc_geometry_basis_service_module.__name__,
        ifc_geometry_basis_service_module,
    )
    monkeypatch.setitem(
        sys.modules,
        ifc_project_service_module.__name__,
        ifc_project_service_module,
    )
    monkeypatch.setitem(
        sys.modules,
        ifc_spatial_structure_service_module.__name__,
        ifc_spatial_structure_service_module,
    )
    monkeypatch.setitem(
        sys.modules,
        geometry_resource_service_module.__name__,
        geometry_resource_service_module,
    )
    monkeypatch.setitem(
        sys.modules, geometry_catalog_service_module.__name__, geometry_catalog_service_module
    )
    monkeypatch.setitem(
        sys.modules, geometry_source_service_module.__name__, geometry_source_service_module
    )
    monkeypatch.setitem(
        sys.modules, geometry_preview_service_module.__name__, geometry_preview_service_module
    )
    monkeypatch.setitem(
        sys.modules, geometry_query_service_module.__name__, geometry_query_service_module
    )
    monkeypatch.setitem(
        sys.modules,
        geometry_authoring_service_module.__name__,
        geometry_authoring_service_module,
    )
    monkeypatch.setitem(
        sys.modules,
        product_family_asset_service_module.__name__,
        product_family_asset_service_module,
    )
    monkeypatch.setitem(
        sys.modules,
        product_family_catalog_service_module.__name__,
        product_family_catalog_service_module,
    )
    monkeypatch.setitem(
        sys.modules,
        product_family_authoring_service_module.__name__,
        product_family_authoring_service_module,
    )
    monkeypatch.setitem(
        sys.modules,
        product_realization_input_service_module.__name__,
        product_realization_input_service_module,
    )
    monkeypatch.setitem(
        sys.modules,
        product_realization_preview_service_module.__name__,
        product_realization_preview_service_module,
    )
    monkeypatch.setitem(
        sys.modules,
        exact_artifact_reader_module.__name__,
        exact_artifact_reader_module,
    )
    monkeypatch.setitem(
        sys.modules,
        ifc_revision_writer_module.__name__,
        ifc_revision_writer_module,
    )
    monkeypatch.setitem(
        sys.modules,
        ifc_revision_reader_module.__name__,
        ifc_revision_reader_module,
    )
    monkeypatch.setitem(
        sys.modules,
        ifc_write_coordinator_module.__name__,
        ifc_write_coordinator_module,
    )
    monkeypatch.setitem(
        sys.modules,
        ifc_query_service_module.__name__,
        ifc_query_service_module,
    )
    monkeypatch.setitem(sys.modules, ifc_codec_module.__name__, ifc_codec_module)
    monkeypatch.setitem(
        sys.modules,
        ifc_geometry_basis_module.__name__,
        ifc_geometry_basis_module,
    )
    monkeypatch.setitem(sys.modules, ifc_project_module.__name__, ifc_project_module)
    monkeypatch.setitem(
        sys.modules,
        ifc_spatial_structure_module.__name__,
        ifc_spatial_structure_module,
    )
    monkeypatch.setitem(
        sys.modules,
        product_realization_host_module.__name__,
        product_realization_host_module,
    )
    monkeypatch.setitem(sys.modules, resolver_module.__name__, resolver_module)
    monkeypatch.setitem(sys.modules, worker_module.__name__, worker_module)
    monkeypatch.setitem(sys.modules, cursor_module.__name__, cursor_module)
    monkeypatch.setitem(sys.modules, snapshot_module.__name__, snapshot_module)
    monkeypatch.setitem(sys.modules, geometry_runtime_module.__name__, geometry_runtime_module)
    monkeypatch.setitem(
        sys.modules,
        ifc_revision_runtime_module.__name__,
        ifc_revision_runtime_module,
    )
    monkeypatch.setitem(
        sys.modules,
        product_realization_runtime_module.__name__,
        product_realization_runtime_module,
    )
    monkeypatch.setitem(
        sys.modules,
        ifc_query_worker_module.__name__,
        ifc_query_worker_module,
    )
    monkeypatch.setitem(sys.modules, source_text_module.__name__, source_text_module)
    monkeypatch.setitem(sys.modules, source_patch_module.__name__, source_patch_module)
    monkeypatch.setitem(
        sys.modules,
        geometry_authoring_source_module.__name__,
        geometry_authoring_source_module,
    )
    monkeypatch.setattr(direct_bootstrap, "create_database", database_factory)
    monkeypatch.setattr(
        direct_bootstrap,
        "PostgresControlPlaneRepository",
        repository_factory,
    )
    monkeypatch.setattr(
        direct_bootstrap,
        "PostgresIdentityDirectory",
        identity_factory,
    )
    monkeypatch.setattr(direct_bootstrap, "LocalStorage", storage_factory)
    monkeypatch.setattr(direct_bootstrap, "ArtifactService", artifact_factory)

    resources = direct_bootstrap.create_direct_runtime_resources(config)

    assert resources.application is application
    assert resources.code_discovery_service is code_discovery_service
    assert resources.query_service is query_service
    assert resources.query_python_service is query_python_service
    assert resources.json_service is json_service
    assert resources.query_sql_service is query_sql_service
    assert resources.query_snapshots is query_snapshots
    assert resources.transform_service is transform_service
    assert resources.cad_measurement_service is cad_measurement_service
    assert resources.ifc_model_service is ifc_model_service
    assert resources.ifc_geometry_basis_service is ifc_geometry_basis_service
    assert resources.ifc_project_service is ifc_project_service
    assert resources.ifc_spatial_structure_service is ifc_spatial_structure_service
    assert resources.ifc_query_service is ifc_query_service
    assert resources.products_read_service._data is application
    assert resources.products_read_service._repository is repository
    assert resources.products_read_service._payloads is payload_resolver
    assert resources.products_read_service._writer is ifc_revision_writer
    assert resources.products_read_service._runtime is ifc_revision_runtime
    assert resources.geometry_resource_service is geometry_resource_service
    assert resources.geometry_catalog_service is geometry_catalog_service
    assert resources.geometry_source_service is geometry_source_service
    assert resources.geometry_preview_service is geometry_preview_service
    assert resources.geometry_query_service is geometry_query_service
    assert resources.geometry_authoring_service is geometry_authoring_service
    assert resources.product_family_catalog_service is product_family_catalog_service
    assert resources.product_family_authoring_service is product_family_authoring_service
    assert resources.product_realization_input_service is product_realization_input_service
    assert resources.product_realization_preview_service is product_realization_preview_service
    assert resources.product_realization_runtime is product_realization_runtime
    assert resources.geometry_runtime is geometry_runtime
    assert resources.ifc_revision_runtime is ifc_revision_runtime
    assert resources.ifc_write_coordinator is ifc_write_coordinator
    assert resources.ifc_query_worker is ifc_query_worker
    assert resources.transform_worker is transform_worker
    assert resources.python_transform_worker is python_transform_worker
    assert resources.python_worker_attestation is python_worker_attestation
    assert resources.database is database
    assert resources.storage is storage
    database_factory.assert_called_once_with(settings)
    repository_factory.assert_called_once_with(database.session_factory)
    identity_factory.assert_called_once_with(database.session_factory)
    storage_factory.assert_called_once_with(
        tmp_path / "storage",
        max_size_bytes=512 * 1024 * 1024,
    )
    artifact_factory.assert_called_once_with(repository, storage)
    ifc_codec_factory.assert_called_once_with()
    capacity = geometry_runtime_factory.call_args.kwargs["capacity"]
    assert capacity.identity[3] == 2
    ifc_revision_reader_factory.assert_called_once_with(repository, storage, ifc_revision_runtime)
    ifc_revision_runtime_factory.assert_called_once_with(capacity=capacity)
    from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
        bind_tenant_execution_context, current_tenant_execution_context,
    )
    ifc_write_coordinator_factory.assert_called_once_with(
        get_execution_context=current_tenant_execution_context,
        bind_execution_context=bind_tenant_execution_context,
    )
    ifc_revision_writer_factory.assert_called_once_with(
        repository,
        storage,
        ifc_revision_reader,
        ifc_revision_runtime,
    )
    ifc_model_service_factory.assert_called_once_with(
        repository,
        storage,
        ifc_codec,
        ifc_revision_writer,
        ifc_revision_runtime,
        ifc_write_coordinator,
    )
    ifc_geometry_basis_compiler_factory.assert_not_called()
    ifc_geometry_basis_service_factory.assert_called_once_with(
        ifc_revision_writer,
        ifc_revision_runtime,
        ifc_write_coordinator,
    )
    ifc_project_compiler_factory.assert_not_called()
    ifc_project_service_factory.assert_called_once_with(
        ifc_revision_writer,
        ifc_revision_runtime,
        ifc_write_coordinator,
    )
    service_factory.assert_called_once_with(repository, artifacts, ifc_repository=repository)
    cursor_codec_factory.assert_called_once_with(config.data_query_cursor_key)
    ifc_spatial_structure_compiler_factory.assert_not_called()
    ifc_spatial_structure_service_factory.assert_called_once_with(
        ifc_revision_writer,
        cursor_codec,
        ifc_revision_runtime,
        ifc_write_coordinator,
    )
    geometry_resource_service_factory.assert_called_once_with(
        repository, storage, geometry_runtime=geometry_runtime, source_bundle_packer=ANY,
    )
    capacity = geometry_runtime_factory.call_args.kwargs["capacity"]
    geometry_runtime_factory.assert_called_once_with(
        capacity=capacity, codec_executor=geometry_codec
    )
    codec_factory.assert_called_once_with(
        budget=codec_module.GEOMETRY_CODEC_BUDGET, capacity=capacity
    )
    assert resources.geometry_codec is geometry_codec
    geometry_catalog_service_factory.assert_called_once_with(
        geometry_resource_service, cursor_codec
    )
    geometry_source_service_factory.assert_called_once_with(
        geometry_catalog_service, geometry_resource_service
    )
    publication = geometry_authoring_service._publication
    assert product_family_authoring_service._publication is publication
    drafts, inspections = publication._drafts, publication._inspections
    assert inspections._repository is repository
    assert inspections._family_catalog is product_family_catalog_service
    geometry_preview_service_factory.assert_called_once_with(
        geometry_catalog_service,
        geometry_resource_service,
        geometry_runtime,
        product_family_catalog_service,
        inspections,
        family_assets=product_family_asset_service,
    )
    geometry_query_service_factory.assert_called_once_with(
        repository,
        artifacts,
        geometry_resource_service,
        geometry_runtime,
        cursor_codec,
        inspections=inspections,
        family_catalog=product_family_catalog_service,
        family_assets=product_family_asset_service,
    )
    geometry_authoring_service_factory.assert_called_once_with(
        repository,
        geometry_resource_service,
        geometry_authoring_source,
        source_patch=source_patch_adapter,
        drafts=drafts,
        catalog=geometry_catalog_service,
    )
    geometry_authoring_source_factory.assert_called_once_with()
    product_family_asset_service_factory.assert_called_once_with(
        storage,
        geometry_authoring_source,
    )
    product_family_catalog_service_factory.assert_called_once_with(
        repository,
        geometry_resource_service,
        geometry_catalog_service,
        product_family_asset_service,
        cursor_codec,
    )
    product_realization_runtime_factory.assert_called_once_with(
        capacity=capacity, codec_executor=geometry_codec
    )
    product_realization_input_service_factory.assert_called_once_with(
        product_family_asset_service,
        geometry_resource_service,
        runtime=product_realization_runtime,
        family_repository=repository,
    )
    product_family_authoring_service_factory.assert_called_once_with(
        repository,
        artifacts,
        product_family_catalog_service,
        product_family_asset_service,
        product_realization_input_service,
        geometry_authoring_source,
        source_patch=source_patch_adapter,
        drafts=drafts,
        geometry_authoring=geometry_authoring_service,
    )
    product_realization_host_extractor_factory.assert_not_called()
    product_realization_preview_service_factory.assert_called_once_with(
        product_family_catalog_service,
        product_realization_input_service,
        product_realization_runtime,
        geometry_preview_service,
        inspections,
    )
    query_service_factory.assert_called_once_with(repository, artifacts, cursor_codec)
    payload_resolver_factory.assert_called_once_with(repository, artifacts)
    ifc_query_worker_factory.assert_called_once_with(capacity=capacity)
    assert source_patch_adapter_factory.call_args_list == [call(), call()]
    ifc_query_service_factory.assert_called_once_with(
        repository,
        artifacts,
        payload_resolver,
        ifc_revision_reader,
        ifc_query_worker,
        cursor_codec,
        source_patch_adapter,
    )
    data_query_attempt_service_factory.assert_called_once_with(
        repository,
        artifacts,
        cursor_codec,
        source_patch_adapter,
        query_snapshots,
    )
    code_discovery_service_factory.assert_called_once_with(
        application,
        geometry_resource_service,
        geometry_catalog_service,
        geometry_source_service,
        geometry_query_service,
        ifc_query_service,
        geometry_authoring_service,
        cursor_codec,
        data_query_attempts=data_query_attempt_service,
        product_family_catalog=product_family_catalog_service,
        product_family_assets=product_family_asset_service,
        exact_artifacts=exact_artifact_reader,
        source_bundles=geometry_authoring_source,
        product_family_authoring=product_family_authoring_service,
    )
    exact_artifact_reader_factory.assert_called_once_with(artifacts)
    json_service_factory.assert_called_once_with(payload_resolver, artifacts, cursor_codec)
    sql_capacity = transform_worker_factory.call_args.kwargs["capacity"]
    assert sql_capacity is not capacity
    transform_worker_factory.assert_called_once_with(capacity=sql_capacity)
    python_transform_worker_factory.assert_called_once_with(
        python_executable=str(config.python_worker_executable), capacity=capacity
    )
    query_snapshots_factory.assert_called_once_with()
    query_sql_service_factory.assert_called_once_with(
        payload_resolver,
        transform_worker,
        cursor_codec,
        query_snapshots,
        data_query_attempt_service,
    )
    query_python_service_factory.assert_called_once_with(
        payload_resolver,
        python_transform_worker,
        cursor_codec,
        query_snapshots,
        data_query_attempt_service,
    )
    transform_service_factory.assert_called_once_with(
        repository,
        artifacts,
        payload_resolver,
        transform_worker,
        python_worker=python_transform_worker,
        source_patch=source_patch_adapter,
        ifc_revisions=ifc_revision_reader,
        ifc_worker=ifc_query_worker,
    )
    ifc_trusted_baseline_service_factory.assert_called_once_with(
        ifc_revision_writer, artifacts, ifc_write_coordinator,
    )
    assert resources.ifc_trusted_baseline_service is ifc_trusted_baseline_service
    cad_measurement_service_factory.assert_called_once_with(
        repository,
        artifacts,
        payload_resolver,
        worker=cad_measurement_service_factory.call_args.kwargs["worker"],
    )
    sessions_factory.assert_called_once_with(
        repository,
        identity_directory,
        config.subject.subject,
        resources.scope_binder,
    )
    resolver_factory.assert_called_once_with(
        repository,
        identity_directory,
        config.subject.subject,
        resources.scope_binder,
    )


@pytest.mark.parametrize("early_close_failure", [False, True])
async def test_direct_runtime_readiness_and_close_are_owned(
    monkeypatch, tmp_path: Path, early_close_failure: bool
) -> None:
    database = SimpleNamespace(verify_ready=AsyncMock(), dispose=AsyncMock())
    storage = SimpleNamespace(verify_ready=AsyncMock())
    worker = SimpleNamespace(verify_ready=AsyncMock(), close=AsyncMock())
    python_worker = SimpleNamespace(verify_ready=AsyncMock(), close=AsyncMock())
    cad_measurement_service = SimpleNamespace(close=AsyncMock())
    snapshots = SimpleNamespace(close=AsyncMock())
    geometry_runtime = SimpleNamespace(verify_ready=AsyncMock(), close=AsyncMock())
    geometry_codec = SimpleNamespace(close=AsyncMock())
    product_realization_runtime = SimpleNamespace(
        verify_ready=AsyncMock(),
        close=AsyncMock(),
    )
    ifc_query_worker = SimpleNamespace(verify_ready=AsyncMock(), close=AsyncMock())
    ifc_revision_runtime = SimpleNamespace(verify_ready=AsyncMock(), close=AsyncMock())
    write_coordinator = SimpleNamespace(close=AsyncMock())
    attestation = object()
    environment_verifier = Mock()
    monkeypatch.setattr(
        direct_bootstrap,
        "verify_attested_environment",
        environment_verifier,
    )
    resources = direct_bootstrap.DirectRuntimeResources(
        application=object(),
        query_service=object(),
        query_python_service=object(),
        json_service=object(),
        query_sql_service=object(),
        query_snapshots=snapshots,
        transform_service=object(),
        cad_measurement_service=cad_measurement_service,
        ifc_model_service=object(),
        ifc_trusted_baseline_service=object(),
        ifc_geometry_basis_service=object(),
        ifc_project_service=object(),
        ifc_spatial_structure_service=object(),
        ifc_query_service=object(),
        ifc_revision_runtime=ifc_revision_runtime,
        ifc_write_coordinator=write_coordinator,
        geometry_resource_service=object(),
        geometry_catalog_service=object(),
        geometry_source_service=object(),
        geometry_preview_service=object(),
        geometry_inspection_service=object(),
        geometry_query_service=object(),
        geometry_authoring_service=object(),
        product_family_catalog_service=object(),
        product_family_authoring_service=object(),
        product_realization_input_service=object(),
        product_realization_preview_service=object(),
        product_realization_runtime=product_realization_runtime,
        geometry_runtime=geometry_runtime,
        geometry_codec=geometry_codec,
        ifc_query_worker=ifc_query_worker,
        transform_worker=worker,
        python_transform_worker=python_worker,
        sessions=object(),
        scope_resolver=object(),
        scope_binder=lambda _context: None,
        database=database,
        storage=storage,
        python_worker_executable=tmp_path / "data-python-worker" / "bin" / "python",
        python_worker_attestation=attestation,
    )

    await resources.verify_ready()
    async def close_revision():
        write_coordinator.close.assert_awaited_once_with()
    ifc_revision_runtime.close.side_effect = close_revision
    if early_close_failure:
        geometry_runtime.close.side_effect = RuntimeError("early close failed")
        with pytest.raises(RuntimeError, match="early close failed"):
            await resources.close()
    else:
        await resources.close()
    geometry_codec.close.assert_awaited_once_with()

    environment_verifier.assert_called_once_with(
        tmp_path / "data-python-worker" / "bin" / "python",
        attestation,
    )
    database.verify_ready.assert_awaited_once_with()
    storage.verify_ready.assert_awaited_once_with()
    cad_measurement_service.close.assert_awaited_once_with()
    worker.verify_ready.assert_not_awaited()
    python_worker.verify_ready.assert_not_awaited()
    geometry_runtime.verify_ready.assert_not_awaited()
    product_realization_runtime.verify_ready.assert_not_awaited()
    ifc_query_worker.verify_ready.assert_not_awaited()
    ifc_revision_runtime.verify_ready.assert_not_awaited()
    snapshots.close.assert_awaited_once_with()
    worker.close.assert_awaited_once_with()
    python_worker.close.assert_awaited_once_with()
    geometry_runtime.close.assert_awaited_once_with()
    product_realization_runtime.close.assert_awaited_once_with()
    ifc_query_worker.close.assert_awaited_once_with()
    ifc_revision_runtime.close.assert_awaited_once_with()
    database.dispose.assert_awaited_once_with()

    environment_verifier.reset_mock(side_effect=True)
    environment_verifier.side_effect = RuntimeError("attested closure drifted")
    database.verify_ready.reset_mock()
    with pytest.raises(RuntimeError, match="attested closure drifted"):
        await resources.verify_ready()
    database.verify_ready.assert_not_awaited()

    environment_verifier.reset_mock(side_effect=True)
    database.verify_ready.side_effect = RuntimeError(
        "database failed: postgresql://user:secret@private-host/private-database"
    )
    storage.verify_ready.reset_mock()
    with pytest.raises(RuntimeError, match="DATABASE_READINESS_FAILED") as database_failure:
        await resources.verify_ready()
    assert "login permissions and Alembic schema revision" in str(database_failure.value)
    assert "secret" not in str(database_failure.value)
    assert "private-host" not in str(database_failure.value)
    assert database_failure.value.__suppress_context__ is True
    storage.verify_ready.assert_not_awaited()


@pytest.mark.parametrize("during_startup", [False, True])
async def test_backend_cleans_both_database_pools_after_one_dispose_fails(during_startup, monkeypatch):
    from dataclasses import fields
    from CAD2BIM_backend import bootstrap

    api_db = SimpleNamespace(verify_ready=AsyncMock(), dispose=AsyncMock())
    runner_db = SimpleNamespace(verify_ready=AsyncMock(),
        dispose=AsyncMock(side_effect=RuntimeError("runner pool cleanup failed")))
    if during_startup:
        runner_db.verify_ready.side_effect = RuntimeError("startup unavailable")
        databases = iter((api_db, runner_db))
        monkeypatch.setattr(bootstrap, "create_database", lambda _settings: next(databases))
        with pytest.raises(RuntimeError, match="runner pool cleanup failed"):
            await bootstrap.open_backend_runtime(api_settings=object(),
                runner_settings=SimpleNamespace(database_url="postgresql+psycopg://local/test"),
                public_key_pem=b"", issuer="test", audience="test", execution_protocol=None,
                graph_builder=None, runner_id="test", state_access_secret=b"test")
    else:
        runtime = bootstrap.BackendRuntime(**{field.name: None for field in fields(bootstrap.BackendRuntime)})
        runtime.api_database, runtime.runner_database = api_db, runner_db
        runtime.graph_runtime = SimpleNamespace(close=AsyncMock())
        with pytest.raises(RuntimeError, match="runner pool cleanup failed"):
            await runtime.close()
    runner_db.dispose.assert_awaited_once_with()
    api_db.dispose.assert_awaited_once_with()
