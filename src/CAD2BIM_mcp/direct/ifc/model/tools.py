"""FastMCP registrations for durable IFC model capabilities."""

from __future__ import annotations

from typing import Annotated

from fastmcp import Context, FastMCP
from pydantic import Field, ValidationError

from CAD2BIM_backend.domain.ifc_geometry_basis import MAX_IFC_CONTEXT_OPERATIONS

from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.ifc.invocation import server_ifc_invocation_key
from CAD2BIM_mcp.direct.ifc.model.contracts import (
    IfcContextOperationInput,
    IfcCoordinateReferenceChangeInput,
    IfcModelConfigureGeometryBasisResult,
    IfcModelInfoResult,
    IfcModelInitializeResult,
    IfcProjectUnitChangeInput,
    IfcRepresentationContextCatalogResult,
    to_domain_geometry_basis_command,
)
from CAD2BIM_mcp.direct.protocol import DirectDataMcpApplication
from CAD2BIM_mcp.tool_docs import ifc_model as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


def register_ifc_model_initialize(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    @server.tool(
        name="ifc_model_initialize",
        description=(_tool_docs.IFC_MODEL_INITIALIZE_DESCRIPTION),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    )
    async def ifc_model_initialize(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_TARGET_RUN_ALIAS_DESCRIPTION,
            ),
        ],
        project_name: Annotated[
            str,
            Field(
                min_length=1,
                max_length=255,
                description=(_tool_docs.IFC_MODEL_INITIALIZE_PROJECT_NAME_DESCRIPTION),
            ),
        ],
    ) -> IfcModelInitializeResult:
        try:
            return await application.initialize_ifc_model(
                direct_session_alias,
                project_name,
            )
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None


def register_ifc_model_info(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    @server.tool(
        name="ifc_model_info",
        description=(_tool_docs.IFC_MODEL_INFO_DESCRIPTION),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    async def ifc_model_info(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_TARGET_RUN_ALIAS_DESCRIPTION,
            ),
        ],
    ) -> IfcModelInfoResult:
        try:
            return await application.get_ifc_model_info(direct_session_alias)
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None


def register_ifc_model_configure_geometry_basis(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    @server.tool(
        name="ifc_model_configure_geometry_basis",
        description=(_tool_docs.IFC_MODEL_CONFIGURE_GEOMETRY_BASIS_DESCRIPTION),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    )
    async def ifc_model_configure_geometry_basis(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_TARGET_RUN_ALIAS_DESCRIPTION,
            ),
        ],
        ctx: Context,
        project_unit_change: Annotated[
            IfcProjectUnitChangeInput | None,
            Field(
                description=(
                    _tool_docs.IFC_MODEL_CONFIGURE_GEOMETRY_BASIS_PROJECT_UNIT_CHANGE_DESCRIPTION
                )
            ),
        ] = None,
        coordinate_reference_change: Annotated[
            IfcCoordinateReferenceChangeInput | None,
            Field(
                description=(
                    _tool_docs.IFC_MODEL_CONFIGURE_GEOMETRY_BASIS_COORDINATE_REFERENCE_CHANGE_DESCRIPTION
                )
            ),
        ] = None,
        context_operations: Annotated[
            tuple[IfcContextOperationInput, ...],
            Field(
                max_length=MAX_IFC_CONTEXT_OPERATIONS,
                description=(
                    _tool_docs.IFC_MODEL_CONFIGURE_GEOMETRY_BASIS_CONTEXT_OPERATIONS_DESCRIPTION
                ),
            ),
        ] = (),
    ) -> IfcModelConfigureGeometryBasisResult:
        try:
            command = to_domain_geometry_basis_command(
                project_unit_change,
                coordinate_reference_change,
                context_operations,
            )
            return await application.configure_ifc_geometry_basis(
                direct_session_alias,
                command,
                idempotency_key=server_ifc_invocation_key(ctx),
            )
        except ValidationError as exc:
            raise _tool_error(
                DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "The geometry basis request is invalid or contains no explicit change",
                    phase="preflight",
                )
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None


def register_ifc_representation_context_list(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    @server.tool(
        name="ifc_representation_context_list",
        description=(_tool_docs.IFC_REPRESENTATION_CONTEXT_LIST_DESCRIPTION),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    async def ifc_representation_context_list(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_TARGET_RUN_ALIAS_DESCRIPTION,
            ),
        ],
    ) -> IfcRepresentationContextCatalogResult:
        try:
            return await application.list_ifc_representation_contexts(direct_session_alias)
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None
