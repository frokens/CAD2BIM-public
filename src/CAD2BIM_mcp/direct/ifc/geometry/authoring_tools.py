"""FastMCP registration for TASK geometry-resource writes."""

from __future__ import annotations

import json

from fastmcp import Context, FastMCP
from pydantic import ValidationError

from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.ifc.geometry.authoring_application import (
    GeometryAuthoringProviderApplication,
)
from CAD2BIM_mcp.direct.ifc.geometry.authoring_contracts import (
    DirectIfcGeometryResourceWriteInput,
    IfcGeometryDraftCreateSource,
    IfcGeometryPublishedDraftSource,
    IfcGeometryAuthoringInput,
    IfcGeometrySourceChange,
    IfcGeometryResourceWriteResult,
    validate_geometry_resource_write_input,
)
from CAD2BIM_backend.domain.geometry_interface import (
    GeometryModelInterface,
    GeometryModelInterfaces,
)
from CAD2BIM_backend.domain.text_source import TextChange
from CAD2BIM_mcp.direct.ifc.invocation import server_ifc_invocation_key
from CAD2BIM_mcp.direct.provider_schema import provider_reference_schema
from CAD2BIM_mcp.direct.tool_support import (
    DIRECT_SESSION_ALIAS,
    invalid_provider_input,
    register_provider_document,
    register_provider_tool,
)
from CAD2BIM_mcp.tool_docs.ifc_geometry import (
    GEOMETRY_AUTHORING_PROGRAM_RESOURCE_DESCRIPTION,
    GEOMETRY_AUTHORING_PROGRAM_USAGE,
    GEOMETRY_AUTHORING_EXECUTION_AND_DIAGNOSTIC_GUIDANCE,
    GEOMETRY_BUILD_RETURN_CONTRACT,
    GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE,
    GEOMETRY_DRAFT_UPDATE_EXAMPLE,
    IFC_GEOMETRY_RESOURCE_WRITE_DESCRIPTION,
)


_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
}


def register_ifc_geometry_resource_write(
    server: FastMCP,
    application: GeometryAuthoringProviderApplication,
) -> None:
    async def ifc_geometry_resource_write(
        direct_session_alias: DIRECT_SESSION_ALIAS,
        ctx: Context,
        mode: str,
        display_name: str | None = None,
        usage_instructions: TextChange | None = None,
        source: IfcGeometryDraftCreateSource | IfcGeometryPublishedDraftSource | None = None,
        draft_ref: str | None = None,
        expected_edit_token: int | None = None,
        source_changes: tuple[IfcGeometrySourceChange, ...] | None = None,
        geometry_inputs: tuple[IfcGeometryAuthoringInput, ...] | None = None,
        parameter_schema_json: str | None = None,
        interfaces: GeometryModelInterfaces | None = None,
    ) -> IfcGeometryResourceWriteResult:
        try:
            value: dict[str, object] = {
                "direct_session_alias": direct_session_alias, "mode": mode,
            }
            values = {
                "display_name": display_name, "usage_instructions": usage_instructions,
                "source": source, "draft_ref": draft_ref,
                "expected_edit_token": expected_edit_token, "source_changes": source_changes,
                "geometry_inputs": geometry_inputs, "parameter_schema_json": parameter_schema_json,
                "interfaces": interfaces,
            }
            value.update({key: item for key, item in values.items() if item is not None})
            request = validate_geometry_resource_write_input(value)
            return await application.write_ifc_geometry_resource(
                request,
                request_identity=server_ifc_invocation_key(ctx),
            )
        except ValidationError as exc:
            raise invalid_provider_input(
                "Use one complete create or update draft branch",
                exc,
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    register_provider_document(
        server,
        tool_name="ifc_geometry_resource_write",
        document_name="program_api",
        description=GEOMETRY_AUTHORING_PROGRAM_RESOURCE_DESCRIPTION,
        document={
            "usage": GEOMETRY_AUTHORING_PROGRAM_USAGE,
            "execution_diagnostics": GEOMETRY_AUTHORING_EXECUTION_AND_DIAGNOSTIC_GUIDANCE,
            "interface_schema": provider_reference_schema(
                GeometryModelInterface,
                mode="validation",
            ),
            "return_contract": GEOMETRY_BUILD_RETURN_CONTRACT,
            "examples": {
                "published_create": {"direct_session_alias": "direct_001", "mode": "create",
                    "source": {"source_kind": "published", "resource_version_alias": "cur.<exact-version>"}},
                "modeling_function_create": json.loads(
                    GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE
                ),
                "draft_update": GEOMETRY_DRAFT_UPDATE_EXAMPLE,
            },
        },
        schema_roots=(("interface_schema",),),
    )
    register_provider_tool(
        server,
        ifc_geometry_resource_write,
        name="ifc_geometry_resource_write",
        compact_input=True,
        compact_output=True,
        input_type=DirectIfcGeometryResourceWriteInput,
        output_type=IfcGeometryResourceWriteResult,
        annotations=_WRITE_ANNOTATIONS,
        description=IFC_GEOMETRY_RESOURCE_WRITE_DESCRIPTION,
    )


IFC_GEOMETRY_AUTHORING_TOOL_REGISTRARS = (register_ifc_geometry_resource_write,)


__all__ = [
    "IFC_GEOMETRY_AUTHORING_TOOL_REGISTRARS",
    "register_ifc_geometry_resource_write",
]
