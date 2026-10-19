"""FastMCP registration for IFC spatial-structure reads and writes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.tools.function_tool import FunctionTool
from fastmcp.utilities.json_schema import dereference_refs
from pydantic import Field, ValidationError

from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.ifc.spatial.contracts import (
    DirectIfcSpatialListContinuationInput,
    DirectIfcSpatialListFirstPageInput,
    DirectIfcSpatialListInput,
    IfcGlobalIdInput,
    IfcSpatialClassInput,
    IfcSpatialOperationInput,
    IfcSpatialStructureApplyResult,
    IfcSpatialStructureListResult,
    to_domain_spatial_apply_command,
    to_domain_spatial_list_query,
    validate_direct_spatial_list_input,
)
from CAD2BIM_mcp.direct.ifc.invocation import server_ifc_invocation_key
from CAD2BIM_mcp.direct.provider_schema import compact_provider_input_schema
from CAD2BIM_mcp.direct.tool_support import provider_input_schema
from CAD2BIM_mcp.tool_docs import ifc_spatial as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs

if TYPE_CHECKING:
    from CAD2BIM_mcp.direct.protocol import DirectDataMcpApplication
else:
    DirectDataMcpApplication = Any


def register_ifc_spatial_structure_apply(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    async def ifc_spatial_structure_apply(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_TARGET_RUN_ALIAS_DESCRIPTION,
            ),
        ],
        ctx: Context,
        operations: Annotated[
            tuple[IfcSpatialOperationInput, ...],
            Field(
                min_length=1,
                max_length=256,
                description=_tool_docs.IFC_SPATIAL_STRUCTURE_APPLY_OPERATIONS_DESCRIPTION,
            ),
        ],
    ) -> IfcSpatialStructureApplyResult:
        try:
            command = to_domain_spatial_apply_command(operations)
            return await application.apply_ifc_spatial_structure(
                direct_session_alias,
                command,
                idempotency_key=server_ifc_invocation_key(ctx),
            )
        except ValidationError as exc:
            raise _tool_error(
                DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "The spatial batch must contain only complete, unambiguous legal operations",
                    phase="preflight",
                )
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    tool = FunctionTool.from_function(
        ifc_spatial_structure_apply,
        name="ifc_spatial_structure_apply",
        description=_tool_docs.IFC_SPATIAL_STRUCTURE_APPLY_DESCRIPTION,
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    )
    # Materialize the same discriminated input projection used by FastMCP's
    # existing list-tools middleware before factoring its required class tags.
    tool.parameters = compact_provider_input_schema(dereference_refs(tool.parameters))
    server.add_tool(tool)


def register_ifc_spatial_structure_list(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    async def ifc_spatial_structure_list(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_TARGET_RUN_ALIAS_DESCRIPTION,
            ),
        ],
        spatial_classes: Annotated[
            tuple[IfcSpatialClassInput, ...],
            Field(
                max_length=4,
                description=(_tool_docs.IFC_SPATIAL_STRUCTURE_LIST_SPATIAL_CLASSES_DESCRIPTION),
            ),
        ] = (),
        global_ids: Annotated[
            tuple[IfcGlobalIdInput, ...],
            Field(
                max_length=256,
                description=_tool_docs.DIRECT_IFC_SPATIAL_LIST_FIRST_PAGE_INPUT_GLOBAL_IDS_DESCRIPTION,
            ),
        ] = (),
        parent_global_id: Annotated[
            str | None,
            Field(
                pattern=r"^[0-9A-Za-z_$]{22}$",
                description=_tool_docs.IFC_SPATIAL_STRUCTURE_LIST_QUERY_INPUT_PARENT_GLOBAL_ID_DESCRIPTION,
            ),
        ] = None,
        page_size: Annotated[
            int,
            Field(
                strict=True,
                ge=1,
                le=256,
                description=_tool_docs.IFC_SPATIAL_STRUCTURE_LIST_PAGE_SIZE_DESCRIPTION,
            ),
        ] = 64,
        cursor: Annotated[
            str | None,
            Field(
                pattern=r"^cur\..+$",
                max_length=16_384,
                description=(_tool_docs.IFC_SPATIAL_STRUCTURE_LIST_CURSOR_DESCRIPTION),
            ),
        ] = None,
    ) -> IfcSpatialStructureListResult:
        try:
            value: dict[str, object] = {"direct_session_alias": direct_session_alias}
            if cursor is not None:
                value["cursor"] = cursor
            else:
                value.update(
                    {
                        "spatial_classes": spatial_classes,
                        "global_ids": global_ids,
                        "parent_global_id": parent_global_id,
                        "page_size": page_size,
                    }
                )
            request = validate_direct_spatial_list_input(value)
            if isinstance(request, DirectIfcSpatialListContinuationInput):
                return await application.list_ifc_spatial_structure(
                    request.direct_session_alias,
                    None,
                    request.cursor,
                )
            if not isinstance(request, DirectIfcSpatialListFirstPageInput):
                raise AssertionError("unexpected IFC spatial-list Provider branch")
            return await application.list_ifc_spatial_structure(
                request.direct_session_alias,
                to_domain_spatial_list_query(request),
                None,
            )
        except ValidationError as exc:
            raise _tool_error(
                DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "Submit either first-page filters or one unchanged spatial cursor",
                    phase="preflight",
                )
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    tool = FunctionTool.from_function(
        ifc_spatial_structure_list,
        name="ifc_spatial_structure_list",
        description=(_tool_docs.IFC_SPATIAL_STRUCTURE_LIST_DESCRIPTION),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    tool.parameters = provider_input_schema(
        DirectIfcSpatialListInput, omit_generated_titles=True,
    )
    server.add_tool(tool)


__all__ = [
    "register_ifc_spatial_structure_apply",
    "register_ifc_spatial_structure_list",
]
