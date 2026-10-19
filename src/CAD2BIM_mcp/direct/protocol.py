"""Application protocol consumed by the Direct MCP transport."""

from __future__ import annotations

from typing import Protocol

from CAD2BIM_backend.domain.cad_measurement import (
    CadAnalyzeToleranceCandidatesCommand,
    CadComputeMeasurementsCommand,
    CadToleranceAnalysisResult,
)
from CAD2BIM_backend.domain.data_json import JsonReadQuery, JsonReadResult
from CAD2BIM_backend.domain.data_query import (
    CadEntitiesQuery,
    CadEntityQuery,
    CadGetEntityResult,
    CadQueryEntitiesResult,
    CadQueryXdataResult,
    CadXdataQuery,
    DataQueryRowsResult,
    DataRowsQuery,
)
from CAD2BIM_backend.domain.data_query_python import DataQueryPythonQuery, DataQueryPythonResult
from CAD2BIM_mcp.direct.catalog.contracts import DirectDataDescribePackageResult
from CAD2BIM_backend.domain.data_query_sql import DataQuerySqlQuery, DataQuerySqlResult
from CAD2BIM_backend.domain.data_transform import DataTransformCommand
from CAD2BIM_backend.domain.ifc_official_docs import (
    IfcOfficialDocsReadQuery,
    IfcOfficialDocsReadResult,
    IfcOfficialDocsSearchQuery,
    IfcOfficialDocsSearchResult,
)
from CAD2BIM_backend.domain.ifc_geometry_basis import ConfigureIfcGeometryBasisCommand
from CAD2BIM_backend.domain.ifc_project import UpdateIfcProjectCommand
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    IfcSpatialStructureListQuery,
)
from CAD2BIM_backend.domain.data_runtime import CriticalOperationKind
from CAD2BIM_mcp.direct.transform.contracts import (
    DataRepairTransformToolResult,
    DataTransformToolResult,
)
from CAD2BIM_mcp.direct.code.application import CodeProviderApplication
from CAD2BIM_mcp.direct.catalog.contracts import (
    DirectPackageListResult,
    PackageDescriptionResult,
)
from CAD2BIM_mcp.direct.session.contracts import (
    DirectSessionResult,
)
from CAD2BIM_mcp.direct.ifc.model.contracts import (
    IfcModelConfigureGeometryBasisResult,
    IfcModelInfoResult,
    IfcModelInitializeResult,
    IfcRepresentationContextCatalogResult,
)
from CAD2BIM_mcp.direct.ifc.project.contracts import IfcProjectUpdateResult
from CAD2BIM_mcp.direct.ifc.spatial.contracts import (
    IfcSpatialStructureApplyResult,
    IfcSpatialStructureListResult,
)
from CAD2BIM_mcp.direct.operation.contracts import (
    OperationGetResult,
    OperationListResult,
)
from CAD2BIM_mcp.direct.transform.repair_inputs import DirectRepairTransformInput
from CAD2BIM_mcp.direct.ifc.geometry.application import GeometryProviderApplication
from CAD2BIM_mcp.direct.ifc.geometry.authoring_application import (
    GeometryAuthoringProviderApplication,
)
from CAD2BIM_mcp.direct.ifc.product_family.application import (
    ProductFamilyProviderApplication,
)
from CAD2BIM_mcp.direct.ifc.product_family.write_application import (
    ProductFamilyWriteProviderApplication,
)
from CAD2BIM_mcp.direct.ifc.product_family.preview_application import (
    ProductRealizationPreviewProviderApplication,
)
from CAD2BIM_mcp.direct.ifc.query.application import IfcQueryProviderApplication
from CAD2BIM_mcp.direct.ifc.products.contracts import DirectProductsApplyInput, IfcProductsApplyResult, DirectProductsReadInput, IfcProductsReadResult, DirectProductsEditInput, IfcProductsEditResult
from CAD2BIM_mcp.direct.operation.contracts import IfcProductsRecoveryListResult


class DirectDataMcpApplication(
    GeometryProviderApplication,
    GeometryAuthoringProviderApplication,
    ProductFamilyProviderApplication,
    ProductFamilyWriteProviderApplication,
    ProductRealizationPreviewProviderApplication,
    IfcQueryProviderApplication,
    CodeProviderApplication,
    Protocol,
):
    async def apply_ifc_products(self, request: DirectProductsApplyInput, *, request_identity: str) -> IfcProductsApplyResult: ...
    async def edit_ifc_products(self, request: DirectProductsEditInput, *, request_identity: str) -> IfcProductsEditResult: ...
    async def read_ifc_products(self, request: DirectProductsReadInput) -> IfcProductsReadResult: ...

    async def list_ifc_products_recovery_operations(self, direct_session_alias: str, *,
        tool_name: str, cursor: str | None = None, page_size: int = 20) -> IfcProductsRecoveryListResult: ...

    async def open_direct_session(self, session_name: str) -> DirectSessionResult: ...

    async def initialize_ifc_model(
        self,
        direct_session_alias: str,
        project_name: str,
    ) -> IfcModelInitializeResult: ...

    async def get_ifc_model_info(
        self,
        direct_session_alias: str,
    ) -> IfcModelInfoResult: ...

    async def configure_ifc_geometry_basis(
        self,
        direct_session_alias: str,
        command: ConfigureIfcGeometryBasisCommand,
        *,
        idempotency_key: str,
    ) -> IfcModelConfigureGeometryBasisResult: ...

    async def list_ifc_representation_contexts(
        self,
        direct_session_alias: str,
    ) -> IfcRepresentationContextCatalogResult: ...

    async def update_ifc_project(
        self,
        direct_session_alias: str,
        command: UpdateIfcProjectCommand,
        *,
        idempotency_key: str,
    ) -> IfcProjectUpdateResult: ...


    async def apply_ifc_spatial_structure(
        self,
        direct_session_alias: str,
        command: ApplyIfcSpatialStructureCommand,
        *,
        idempotency_key: str,
    ) -> IfcSpatialStructureApplyResult: ...

    async def list_ifc_spatial_structure(
        self,
        direct_session_alias: str,
        query: IfcSpatialStructureListQuery | None,
        cursor: str | None,
    ) -> IfcSpatialStructureListResult: ...

    async def list_data_packages(
        self,
        direct_session_alias: str,
        before_sequence: int | None,
        page_size: int,
    ) -> DirectPackageListResult: ...

    async def list_operations(
        self,
        direct_session_alias: str,
        before_sequence: int | None,
        page_size: int,
        operation_kind: CriticalOperationKind | None = None,
        *, tool_name: str | None = None, outcome: str | None = None,
    ) -> OperationListResult: ...

    async def get_operation(
        self,
        direct_session_alias: str,
        operation_alias: str | None = None,
        *, case_key: str | None = None, after_case_key: str | None = None, page_size: int = 1,
        selection: object | None = None,
    ) -> OperationGetResult: ...

    async def transform_data(
        self,
        direct_session_alias: str,
        command: DataTransformCommand,
        *,
        request_identity: str,
    ) -> DataTransformToolResult: ...

    async def repair_transform(
        self,
        request: DirectRepairTransformInput,
        *,
        request_identity: str,
    ) -> DataRepairTransformToolResult: ...

    async def analyze_tolerance_candidates(
        self,
        direct_session_alias: str,
        command: CadAnalyzeToleranceCandidatesCommand,
    ) -> CadToleranceAnalysisResult: ...

    async def compute_measurements(
        self,
        direct_session_alias: str,
        command: CadComputeMeasurementsCommand,
    ) -> DataTransformToolResult: ...

    async def query_sql(
        self,
        direct_session_alias: str,
        query: DataQuerySqlQuery | None,
        cursor: str | None,
        *,
        request_identity: str | None = None,
    ) -> DataQuerySqlResult: ...

    async def repair_query_sql(
        self,
        direct_session_alias: str,
        parent_attempt_alias: str,

        edits,
        page_size: int,
        *,
        request_identity: str,
    ) -> DataQuerySqlResult: ...

    async def query_python(
        self,
        direct_session_alias: str,
        query: DataQueryPythonQuery | None,
        cursor: str | None,
        *,
        request_identity: str | None = None,
    ) -> DataQueryPythonResult: ...

    async def repair_query_python(
        self,
        direct_session_alias: str,
        parent_attempt_alias: str,

        edits,
        page_size: int,
        *,
        request_identity: str,
    ) -> DataQueryPythonResult: ...

    async def read_json(
        self,
        direct_session_alias: str,
        query: JsonReadQuery | None,
        cursor: str | None,
    ) -> JsonReadResult: ...

    async def search_ifc_official_docs(
        self,
        query: IfcOfficialDocsSearchQuery,
    ) -> IfcOfficialDocsSearchResult: ...

    async def read_ifc_official_doc(
        self,
        query: IfcOfficialDocsReadQuery,
    ) -> IfcOfficialDocsReadResult: ...

    async def get_package_description(
        self,
        direct_session_alias: str,
        package_alias: str,
    ) -> PackageDescriptionResult: ...

    async def describe_package(
        self,
        direct_session_alias: str,
        package_alias: str,
        payload_name: str | None = None,
    ) -> DirectDataDescribePackageResult: ...

    async def query_rows(
        self,
        direct_session_alias: str,
        query: DataRowsQuery | None,
        cursor: str | None,
    ) -> DataQueryRowsResult: ...

    async def query_entities(
        self,
        direct_session_alias: str,
        query: CadEntitiesQuery | None,
        cursor: str | None,
    ) -> CadQueryEntitiesResult: ...

    async def get_entity(
        self,
        direct_session_alias: str,
        query: CadEntityQuery | None,
        cursor: str | None,
    ) -> CadGetEntityResult: ...

    async def query_xdata(
        self,
        direct_session_alias: str,
        query: CadXdataQuery | None,
        cursor: str | None,
    ) -> CadQueryXdataResult: ...


__all__ = ["DirectDataMcpApplication"]
