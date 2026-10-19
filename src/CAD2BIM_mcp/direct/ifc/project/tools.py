"""FastMCP registration for IFC Project business-metadata updates."""

from __future__ import annotations

from typing import Annotated

from fastmcp import Context, FastMCP
from pydantic import Field, ValidationError

from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.ifc.project.contracts import (
    IfcNullableProjectFieldInput,
    IfcProjectPatchInput,
    IfcProjectUpdateResult,
    to_domain_project_update_command,
)
from CAD2BIM_mcp.direct.ifc.invocation import server_ifc_invocation_key
from CAD2BIM_mcp.direct.protocol import DirectDataMcpApplication
from CAD2BIM_mcp.tool_docs import ifc_model as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


def register_ifc_project_update(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    @server.tool(
        name="ifc_project_update",
        description=(_tool_docs.IFC_PROJECT_UPDATE_DESCRIPTION),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    )
    async def ifc_project_update(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_TARGET_RUN_ALIAS_DESCRIPTION,
            ),
        ],
        ctx: Context,
        patch: Annotated[
            IfcProjectPatchInput,
            Field(description=(_tool_docs.IFC_PROJECT_UPDATE_PATCH_DESCRIPTION)),
        ],
        clear_fields: Annotated[
            tuple[IfcNullableProjectFieldInput, ...],
            Field(
                max_length=4,
                description=(_tool_docs.IFC_PROJECT_UPDATE_CLEAR_FIELDS_DESCRIPTION),
            ),
        ] = (),
    ) -> IfcProjectUpdateResult:
        try:
            command = to_domain_project_update_command(patch, clear_fields)
            return await application.update_ifc_project(
                direct_session_alias,
                command,
                idempotency_key=server_ifc_invocation_key(ctx),
            )
        except ValidationError as exc:
            raise _tool_error(
                DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "The Project patch must contain one unambiguous legal change",
                    phase="preflight",
                )
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None


__all__ = ["register_ifc_project_update"]
