"""One neutral Data Application graph shared by Direct and Runner composition roots."""

from __future__ import annotations

from dataclasses import dataclass

from CAD2BIM_backend.application.code_discovery_service import CodeDiscoveryService
from CAD2BIM_backend.application.data_json_service import DataJsonService
from CAD2BIM_backend.application.data_query_attempt_service import DataQueryAttemptService
from CAD2BIM_backend.application.data_query_python_service import DataQueryPythonService
from CAD2BIM_backend.application.data_query_service import DataQueryService
from CAD2BIM_backend.application.data_query_sql_service import DataQuerySqlService
from CAD2BIM_backend.application.data_runtime_service import DataRuntimeService
from CAD2BIM_backend.application.data_transform_service import DataTransformService
from CAD2BIM_backend.application.exact_artifact_reader import ExactArtifactReader
from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService
from CAD2BIM_backend.infrastructure.data_transform import (
    SubprocessDuckDBDataTransformWorker,
    SubprocessPythonArrowDataTransformWorker,
)


@dataclass(frozen=True, slots=True)
class DataApplicationResources:
    references: OpaqueReferenceService
    runtime: DataRuntimeService
    query: DataQueryService
    query_sql: DataQuerySqlService
    query_python: DataQueryPythonService
    json: DataJsonService
    transform: DataTransformService
    code_discovery: CodeDiscoveryService
    attempts: DataQueryAttemptService
    transform_worker: SubprocessDuckDBDataTransformWorker
    python_transform_worker: SubprocessPythonArrowDataTransformWorker


def create_data_application_resources(
    *,
    repository,
    artifacts,
    cursor_codec,
    payload_resolver,
    source_patch_adapter,
    query_snapshots,
    sql_capacity,
    python_capacity,
    python_worker_executable: str,
    geometry_resources,
    geometry_catalog,
    geometry_sources,
    geometry_queries,
    ifc_queries,
    geometry_authoring,
    product_family_authoring,
    product_family_catalog,
    product_family_assets,
    source_bundles,
    ifc_revisions=None,
    ifc_worker=None,
) -> DataApplicationResources:
    """Create services over caller-owned persistence, workers and snapshot lifetime."""

    query = DataQueryService(repository, artifacts, cursor_codec)
    runtime = DataRuntimeService(repository, artifacts, ifc_repository=repository, queries=query)
    attempts = DataQueryAttemptService(
        repository, artifacts, cursor_codec, source_patch_adapter, query_snapshots
    )
    code_discovery = CodeDiscoveryService(
        runtime,
        geometry_resources,
        geometry_catalog,
        geometry_sources,
        geometry_queries,
        ifc_queries,
        geometry_authoring,
        cursor_codec,
        data_query_attempts=attempts,
        product_family_catalog=product_family_catalog,
        product_family_assets=product_family_assets,
        exact_artifacts=ExactArtifactReader(artifacts),
        source_bundles=source_bundles,
        product_family_authoring=product_family_authoring,
    )
    json = DataJsonService(payload_resolver, artifacts, cursor_codec)
    transform_worker = SubprocessDuckDBDataTransformWorker(capacity=sql_capacity)
    python_transform_worker = SubprocessPythonArrowDataTransformWorker(
        python_executable=python_worker_executable,
        capacity=python_capacity,
    )
    query_sql = DataQuerySqlService(
        payload_resolver, transform_worker, cursor_codec, query_snapshots, attempts
    )
    query_python = DataQueryPythonService(
        payload_resolver, python_transform_worker, cursor_codec, query_snapshots, attempts
    )
    transform = DataTransformService(
        repository, artifacts, payload_resolver, transform_worker,
        python_worker=python_transform_worker,
        source_patch=source_patch_adapter,
        ifc_revisions=ifc_revisions,
        ifc_worker=ifc_worker,
    )
    return DataApplicationResources(
        references=OpaqueReferenceService(repository, cursor_codec),
        runtime=runtime,
        query=query,
        query_sql=query_sql,
        query_python=query_python,
        json=json,
        transform=transform,
        code_discovery=code_discovery,
        attempts=attempts,
        transform_worker=transform_worker,
        python_transform_worker=python_transform_worker,
    )


__all__ = ["DataApplicationResources", "create_data_application_resources"]
