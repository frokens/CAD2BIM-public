"""FastMCP registrations for source-first read-only IFC investigation."""

from __future__ import annotations

import json

from fastmcp import Context, FastMCP
from pydantic import ValidationError

from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.ifc.invocation import server_ifc_invocation_key
from CAD2BIM_mcp.direct.ifc.query.application import IfcQueryProviderApplication
from CAD2BIM_mcp.direct.ifc.query.contracts import (
    DirectIfcQueryPythonInput,
    IfcQueryExactInput,
    IfcQueryPythonResult,
    IfcQuerySourceEdit,
    validate_ifc_query_input,
)
from CAD2BIM_mcp.direct.provider_schema import provider_reference_schema
from CAD2BIM_mcp.direct.tool_support import (
    DIRECT_SESSION_ALIAS as _DIRECT_ALIAS,
    invalid_provider_input as _invalid,
    register_provider_document,
    register_provider_tool as _register,
)
from CAD2BIM_mcp.tool_docs import ifc_query as _tool_docs


_QUERY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": False,
}


def register_ifc_query_python(
    server: FastMCP,
    application: IfcQueryProviderApplication,
) -> None:
    async def ifc_query_python(
        direct_session_alias: _DIRECT_ALIAS,
        ctx: Context,
        mode: str,
        inputs: tuple[IfcQueryExactInput, ...] = (),
        code: str | None = None,
        parent_attempt_alias: str | None = None,

        edits: tuple[IfcQuerySourceEdit, ...] | None = None,
        page_size: int = 100,
        cursor: str | None = None,
    ) -> IfcQueryPythonResult:
        try:
            value: dict[str, object] = {
                "direct_session_alias": direct_session_alias,
                "mode": mode,
            }
            if mode == "fresh":
                value.update(inputs=inputs, code=code, page_size=page_size)
            elif mode == "repair":
                value.update(
                    parent_attempt_alias=parent_attempt_alias,

                    edits=edits,
                    page_size=page_size,
                )
            elif mode == "cursor":
                value["cursor"] = cursor
            request = validate_ifc_query_input(value)
            return await application.query_ifc_python(
                request,
                request_identity=server_ifc_invocation_key(ctx),
            )
        except ValidationError as exc:
            raise _invalid(
                "Use one complete fresh, repair, or cursor IFC-query branch", exc
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    register_provider_document(
        server,
        tool_name="ifc_query_python",
        document_name="program_api",
        description=_tool_docs.IFC_QUERY_PROGRAM_RESOURCE_DESCRIPTION,
        document={
            "usage": _tool_docs.IFC_QUERY_PROGRAM_USAGE,
            "input_binding_schema": provider_reference_schema(
                IfcQueryExactInput,
                mode="validation",
            ),
            "return_contract": _tool_docs.IFC_QUERY_RETURN_CONTRACT,
            "examples": {
                "ifc_only": json.loads(_tool_docs.IFC_QUERY_FRESH_EXAMPLE),
            },
        },
        schema_roots=(("input_binding_schema",),),
    )
    _register(
        server,
        ifc_query_python,
        name="ifc_query_python",
        compact_input=True,
        input_type=DirectIfcQueryPythonInput,
        output_type=IfcQueryPythonResult,
        annotations=_QUERY_ANNOTATIONS,
        description=(_tool_docs.IFC_QUERY_PYTHON_DESCRIPTION),
    )


IFC_QUERY_TOOL_REGISTRARS = (register_ifc_query_python,)


__all__ = [
    "IFC_QUERY_TOOL_REGISTRARS",
    "register_ifc_query_python",
]
