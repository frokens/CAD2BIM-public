"""Independent FastMCP profile for the approved Direct CAD/Data runtime."""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP
from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService

from CAD2BIM_mcp.direct.protocol import DirectDataMcpApplication
from CAD2BIM_mcp.direct.session.tools import register_runtime_open_direct_session
from CAD2BIM_mcp.direct.ifc.model.tools import register_ifc_model_initialize
from CAD2BIM_mcp.direct.ifc.model.tools import register_ifc_model_info
from CAD2BIM_mcp.direct.ifc.model.tools import register_ifc_model_configure_geometry_basis
from CAD2BIM_mcp.direct.ifc.model.tools import register_ifc_representation_context_list
from CAD2BIM_mcp.direct.ifc.project.tools import register_ifc_project_update
from CAD2BIM_mcp.direct.ifc.spatial.tools import (
    register_ifc_spatial_structure_apply,
    register_ifc_spatial_structure_list,
)
from CAD2BIM_mcp.direct.catalog.tools import register_data_package_list
from CAD2BIM_mcp.direct.operation.tools import register_operation_list
from CAD2BIM_mcp.direct.operation.tools import register_operation_get
from CAD2BIM_mcp.direct.code import CODE_TOOL_REGISTRARS
from CAD2BIM_mcp.direct.cad.tools import register_cad_analyze_tolerance_candidates
from CAD2BIM_mcp.direct.cad.tools import register_cad_compute_measurements
from CAD2BIM_mcp.direct.transform.tools import register_data_transform
from CAD2BIM_mcp.direct.query.tools import register_data_query_sql
from CAD2BIM_mcp.direct.query.tools import register_data_query_python
from CAD2BIM_mcp.direct.ifc.official_docs.tools import register_ifc_search_official_docs
from CAD2BIM_mcp.direct.ifc.official_docs.tools import register_ifc_read_official_doc
from CAD2BIM_mcp.direct.query.tools import register_data_read_json
from CAD2BIM_mcp.direct.catalog.tools import register_data_get_package_description
from CAD2BIM_mcp.direct.catalog.tools import register_data_describe_package
from CAD2BIM_mcp.direct.query.tools import register_data_query_rows
from CAD2BIM_mcp.direct.cad.tools import register_cad_query_entities
from CAD2BIM_mcp.direct.cad.tools import register_cad_get_entity
from CAD2BIM_mcp.direct.cad.tools import register_cad_query_xdata
from CAD2BIM_mcp.direct.ifc.geometry import (
    IFC_GEOMETRY_AUTHORING_TOOL_REGISTRARS,
    IFC_GEOMETRY_TOOL_REGISTRARS,
)
from CAD2BIM_mcp.direct.ifc.query import IFC_QUERY_TOOL_REGISTRARS
from CAD2BIM_mcp.direct.ifc.product_family import IFC_PRODUCT_FAMILY_TOOL_REGISTRARS
from CAD2BIM_mcp.direct.ifc.products.tools import register_ifc_products_apply, register_ifc_products_read, register_ifc_products_edit
from CAD2BIM_mcp.direct.ifc.authoring_publish import register_ifc_authoring_publish
from CAD2BIM_mcp.direct.tool_support import (
    DirectProviderInputSchemaMiddleware, DirectToolInputErrorMiddleware, register_shared_provider_documents,
)
from CAD2BIM_mcp.direct.references import DirectReferenceMiddleware


ToolRegistrar = Callable[[FastMCP, DirectDataMcpApplication], None]

# This tuple is the one authoritative public registration order.
_TOOL_REGISTRARS: tuple[ToolRegistrar, ...] = (
    register_runtime_open_direct_session,
    register_ifc_model_initialize,
    register_ifc_model_info,
    register_ifc_model_configure_geometry_basis,
    register_ifc_representation_context_list,
    register_ifc_project_update,
    register_ifc_spatial_structure_list,
    register_ifc_spatial_structure_apply,
    register_data_package_list,
    register_operation_list,
    register_operation_get,
    *CODE_TOOL_REGISTRARS,
    register_cad_analyze_tolerance_candidates,
    register_cad_compute_measurements,
    register_data_transform,
    register_data_query_sql,
    register_data_query_python,
    register_ifc_search_official_docs,
    register_ifc_read_official_doc,
    register_data_read_json,
    register_data_get_package_description,
    register_data_describe_package,
    register_data_query_rows,
    register_cad_query_entities,
    register_cad_get_entity,
    register_cad_query_xdata,
    *IFC_GEOMETRY_TOOL_REGISTRARS,
    *IFC_GEOMETRY_AUTHORING_TOOL_REGISTRARS,
    *IFC_PRODUCT_FAMILY_TOOL_REGISTRARS,
    register_ifc_products_apply,
    register_ifc_products_edit,
    register_ifc_products_read,
    register_ifc_authoring_publish,
    *IFC_QUERY_TOOL_REGISTRARS,
)


def create_direct_mcp(
    application: DirectDataMcpApplication,
) -> FastMCP:
    """Register the frozen Direct provider surface in its authoritative order."""

    server = FastMCP(
        name="CAD2BIM Direct Data MCP",
        # Constructor middleware wraps the default dereferencer; its returned
        # declarations can retain local input references without changing tools.
        middleware=[DirectProviderInputSchemaMiddleware()],
        # The MCP SDK's validator renders invalid values in its public error.
        # Direct runs the same registered Schema behind its bounded middleware.
        strict_input_validation=False,
        mask_error_details=True,
    )
    server.add_middleware(DirectToolInputErrorMiddleware())
    references = getattr(application, "reference_service", None)
    if isinstance(references, OpaqueReferenceService):
        server.add_middleware(DirectReferenceMiddleware(
            references, application._scope_resolver, application._scope_binder,
        ))
    server._provider_contract_documents = []
    for registrar in _TOOL_REGISTRARS:
        registrar(server, application)
    register_shared_provider_documents(server)
    return server
