"""Composition facade for Direct capability-family application adapters."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import TYPE_CHECKING, Callable, ContextManager
from uuid import uuid4

from CAD2BIM_mcp.direct.cad.application import CadApplicationMixin
from CAD2BIM_mcp.direct.catalog.application import CatalogApplicationMixin
from CAD2BIM_mcp.direct.code.application import CodeApplicationMixin
from CAD2BIM_mcp.direct.ifc.model.application import IfcModelApplicationMixin
from CAD2BIM_mcp.direct.ifc.geometry.application import IfcGeometryApplicationMixin
from CAD2BIM_mcp.direct.ifc.authoring_publish import AuthoringPublishApplicationMixin
from CAD2BIM_mcp.direct.ifc.geometry.authoring_application import (
    IfcGeometryAuthoringApplicationMixin,
)
from CAD2BIM_mcp.direct.ifc.product_family.application import (
    IfcProductFamilyApplicationMixin,
)
from CAD2BIM_mcp.direct.ifc.product_family.write_application import (
    IfcProductFamilyWriteApplicationMixin,
)
from CAD2BIM_mcp.direct.ifc.product_family.preview_application import (
    IfcProductRealizationPreviewApplicationMixin,
)
from CAD2BIM_mcp.direct.ifc.project.application import IfcProjectApplicationMixin
from CAD2BIM_mcp.direct.ifc.products.application import IfcProductsApplyApplicationMixin, IfcProductsReadApplicationMixin, IfcProductsEditApplicationMixin
from CAD2BIM_mcp.direct.ifc.spatial.application import (
    IfcSpatialStructureApplicationMixin,
)
from CAD2BIM_mcp.direct.ifc.official_docs.application import (
    IfcOfficialDocsApplicationMixin,
)
from CAD2BIM_mcp.direct.ifc.query.application import IfcQueryApplicationMixin
from CAD2BIM_mcp.direct.operation.application import OperationApplicationMixin
from CAD2BIM_mcp.direct.query.application import QueryApplicationMixin
from CAD2BIM_mcp.direct.session.application import SessionApplicationMixin
from CAD2BIM_mcp.direct.transform.application import TransformApplicationMixin
from CAD2BIM_mcp.direct.errors import DirectApplicationError

if TYPE_CHECKING:
    from CAD2BIM_backend.application.ifc_products_read_service import IfcProductsReadService
    from CAD2BIM_backend.application.product_apply_service import IfcProductsApplyService
    from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService
    from CAD2BIM_backend.application.code_discovery_service import CodeDiscoveryService
    from CAD2BIM_backend.application.cad_measurement_service import CadMeasurementService
    from CAD2BIM_backend.application.data_json_service import DataJsonService
    from CAD2BIM_backend.application.data_query_python_service import DataQueryPythonService
    from CAD2BIM_backend.application.data_query_service import DataQueryService
    from CAD2BIM_backend.application.data_query_sql_service import DataQuerySqlService
    from CAD2BIM_backend.application.data_runtime_service import (
        DataRuntimeService,
        DirectScopeResolver,
        DirectSessionService,
    )
    from CAD2BIM_backend.application.data_transform_service import DataTransformService
    from CAD2BIM_backend.application.ifc_model_service import IfcModelService
    from CAD2BIM_backend.application.ifc_project_service import IfcProjectService
    from CAD2BIM_backend.application.ifc_spatial_structure_service import (
        IfcSpatialStructureService,
    )
    from CAD2BIM_backend.application.ifc_geometry_basis_service import (
        IfcGeometryBasisService,
    )
    from CAD2BIM_backend.application.geometry_catalog_service import GeometryCatalogService
    from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
    from CAD2BIM_backend.application.geometry_query_service import GeometryQueryService
    from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionService
    from CAD2BIM_backend.application.geometry_authoring_service import GeometryAuthoringService
    from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService
    from CAD2BIM_backend.application.geometry_source_service import GeometrySourceService
    from CAD2BIM_backend.application.product_family_catalog_service import (
        ProductFamilyCatalogService,
    )
    from CAD2BIM_backend.application.product_family_authoring_service import (
        ProductFamilyAuthoringService,
    )
    from CAD2BIM_backend.application.product_realization_preview_service import (
        ProductRealizationPreviewService,
    )
    from CAD2BIM_backend.application.ifc_official_docs_service import IfcOfficialDocsService
    from CAD2BIM_backend.application.ifc_query_service import IfcQueryService
    from CAD2BIM_backend.domain.identity import TenantExecutionContext

_READ_TIMEOUT_SECONDS = 10.0


class DirectApplicationFacade(
    IfcProductsEditApplicationMixin,
    IfcProductsReadApplicationMixin,
    IfcProductsApplyApplicationMixin,
    AuthoringPublishApplicationMixin,
    SessionApplicationMixin,
    CodeApplicationMixin,
    IfcModelApplicationMixin,
    IfcGeometryApplicationMixin,
    IfcGeometryAuthoringApplicationMixin,
    IfcProductFamilyApplicationMixin,
    IfcProductFamilyWriteApplicationMixin,
    IfcProductRealizationPreviewApplicationMixin,
    IfcProjectApplicationMixin,
    IfcSpatialStructureApplicationMixin,
    IfcOfficialDocsApplicationMixin,
    IfcQueryApplicationMixin,
    CatalogApplicationMixin,
    OperationApplicationMixin,
    TransformApplicationMixin,
    CadApplicationMixin,
    QueryApplicationMixin,
):
    """Bind a Direct session scope and project shared services to MCP."""

    def __init__(
        self,
        application: DataRuntimeService,
        sessions: DirectSessionService,
        scope_resolver: DirectScopeResolver,
        scope_binder: Callable[[TenantExecutionContext], ContextManager[None]] = (
            lambda _context: nullcontext()
        ),
        *,
        query_service: DataQueryService | None = None,
        query_python_service: DataQueryPythonService | None = None,
        query_sql_service: DataQuerySqlService | None = None,
        json_service: DataJsonService | None = None,
        transform_service: DataTransformService | None = None,
        cad_measurement_service: CadMeasurementService | None = None,
        ifc_official_docs_service: IfcOfficialDocsService | None = None,
        ifc_model_service: IfcModelService | None = None,
        ifc_geometry_basis_service: IfcGeometryBasisService | None = None,
        ifc_project_service: IfcProjectService | None = None,
        ifc_spatial_structure_service: IfcSpatialStructureService | None = None,
        ifc_query_service: IfcQueryService | None = None,
        geometry_resource_service: GeometryResourceService | None = None,
        geometry_catalog_service: GeometryCatalogService | None = None,
        geometry_source_service: GeometrySourceService | None = None,
        geometry_preview_service: GeometryPreviewService | None = None,
        geometry_query_service: GeometryQueryService | None = None,
        geometry_inspection_service: GeometryInspectionService | None = None,
        geometry_authoring_service: GeometryAuthoringService | None = None,
        product_family_catalog_service: ProductFamilyCatalogService | None = None,
        product_family_authoring_service: ProductFamilyAuthoringService | None = None,
        product_realization_preview_service: ProductRealizationPreviewService | None = None,
        code_discovery_service: CodeDiscoveryService | None = None,
        reference_service: OpaqueReferenceService | None = None,
        products_apply_service: IfcProductsApplyService | None = None,
        products_read_service: IfcProductsReadService | None = None,
        products_edit_service=None,
    ) -> None:
        self._application = application
        self._sessions = sessions
        self._scope_resolver = scope_resolver
        self._scope_binder = scope_binder
        self._query_service = query_service
        self._query_python_service = query_python_service
        self._query_sql_service = query_sql_service
        self._json_service = json_service
        self._transform_service = transform_service
        self._cad_measurement_service = cad_measurement_service
        self._ifc_official_docs_service = ifc_official_docs_service
        self._ifc_model_service = ifc_model_service
        self._ifc_geometry_basis_service = ifc_geometry_basis_service
        self._ifc_project_service = ifc_project_service
        self._ifc_spatial_structure_service = ifc_spatial_structure_service
        self._ifc_query_service = ifc_query_service
        self._geometry_resource_service = geometry_resource_service
        self._geometry_catalog_service = geometry_catalog_service
        self._geometry_source_service = geometry_source_service
        self._geometry_preview_service = geometry_preview_service
        self._geometry_query_service = geometry_query_service
        self._geometry_inspection_service = geometry_inspection_service
        self._geometry_authoring_service = geometry_authoring_service
        self._product_family_catalog_service = product_family_catalog_service
        self._product_family_authoring_service = product_family_authoring_service
        self._product_realization_preview_service = product_realization_preview_service
        self._code_discovery_service = code_discovery_service
        self.reference_service = reference_service
        self._products_apply_service = products_apply_service
        self._products_read_service = products_read_service
        self._products_edit_service = products_edit_service

    async def _run_runtime_read(
        self,
        direct_session_alias: str,
        action: str,
        operation,
        *,
        timeout_seconds: float = _READ_TIMEOUT_SECONDS,
        timeout_code: str = "QUERY_TIMEOUT",
        timeout_message: str = "The 10-second Direct read boundary expired",
    ):
        try:
            async with asyncio.timeout(timeout_seconds):
                scope = await self._resolve_scope(direct_session_alias, action)
                with self._scope_binder(scope.identity):
                    return await operation(scope)
        except TimeoutError:
            raise DirectApplicationError(
                timeout_code,
                timeout_message,
                phase="read",
                retryable=False,
            ) from None

    async def _resolve_scope(self, alias: str, action: str):
        return await self._scope_resolver.resolve(
            alias,
            command_id=f"command_direct_{action}_{uuid4().hex}",
        )
