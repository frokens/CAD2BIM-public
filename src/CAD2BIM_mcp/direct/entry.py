"""Isolated stdio entry point for the Direct CAD/Data MCP."""

from __future__ import annotations

import asyncio

from CAD2BIM_backend.application.ifc_official_docs_service import IfcOfficialDocsService
from CAD2BIM_backend.infrastructure.ifc_official_docs import BuildingSmartIfcDocsClient
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.bootstrap import create_direct_runtime_resources
from CAD2BIM_mcp.direct.config import DirectMcpConfig
from CAD2BIM_mcp.direct.server import create_direct_mcp


async def run_direct_stdio(config: DirectMcpConfig) -> None:
    """Verify owned resources, serve stdio, and always release the database pool."""

    resources = create_direct_runtime_resources(config)
    ifc_docs_client = BuildingSmartIfcDocsClient(proxy=config.ifc_docs_proxy)
    try:
        await resources.verify_ready()
        server = create_direct_mcp(
            DirectApplicationFacade(
                resources.application,
                resources.sessions,
                resources.scope_resolver,
                resources.scope_binder,
                query_service=resources.query_service,
                query_python_service=resources.query_python_service,
                query_sql_service=resources.query_sql_service,
                json_service=resources.json_service,
                transform_service=resources.transform_service,
                cad_measurement_service=resources.cad_measurement_service,
                ifc_official_docs_service=IfcOfficialDocsService(ifc_docs_client),
                ifc_model_service=resources.ifc_model_service,
                products_apply_service=resources.products_apply_service,
                products_edit_service=resources.products_edit_service,
                products_read_service=resources.products_read_service,
                ifc_geometry_basis_service=resources.ifc_geometry_basis_service,
                ifc_project_service=resources.ifc_project_service,
                ifc_spatial_structure_service=resources.ifc_spatial_structure_service,
                ifc_query_service=resources.ifc_query_service,
                geometry_resource_service=resources.geometry_resource_service,
                geometry_catalog_service=resources.geometry_catalog_service,
                geometry_source_service=resources.geometry_source_service,
                geometry_preview_service=resources.geometry_preview_service,
                geometry_query_service=resources.geometry_query_service,
                geometry_inspection_service=resources.geometry_inspection_service,
                geometry_authoring_service=resources.geometry_authoring_service,
                product_family_catalog_service=resources.product_family_catalog_service,
                product_family_authoring_service=resources.product_family_authoring_service,
                product_realization_preview_service=resources.product_realization_preview_service,
                code_discovery_service=resources.code_discovery_service,
                reference_service=resources.reference_service,
            ),
        )
        await server.run_async(transport="stdio", show_banner=False)
    finally:
        try:
            await ifc_docs_client.aclose()
        finally:
            await resources.close()


def cli() -> None:
    """Load only explicit process environment and run the Direct stdio profile."""

    config = DirectMcpConfig.from_environ()
    try:
        asyncio.run(run_direct_stdio(config))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    cli()


__all__ = ["cli", "run_direct_stdio"]
