"""Direct tool registrations owned by this capability family."""

from __future__ import annotations

from typing import Annotated, Literal

from fastmcp import FastMCP
from CAD2BIM_backend.domain.text_source import TextSourceView
from pydantic import Field, TypeAdapter

from CAD2BIM_backend.domain.data_runtime import CriticalOperationKind

from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.protocol import DirectDataMcpApplication
from CAD2BIM_mcp.direct.tool_support import register_provider_tool
from CAD2BIM_mcp.direct.operation.contracts import OperationGetResult, OperationListResult, IfcProductsRecoveryListResult
from CAD2BIM_mcp.direct.operation.inputs import DirectOperationListRequest
from CAD2BIM_mcp.direct.transform.common_inputs import (
    DirectOperationGetInput,
    DirectOperationListInput,
    DirectoryPageSize,
    PositiveInt64,
)
from CAD2BIM_mcp.tool_docs import operation as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


def register_operation_list(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    async def operation_list(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_ALIAS_SHORT_DESCRIPTION,
            ),
        ],
        before_sequence: Annotated[
            PositiveInt64 | None,
            Field(description=(_tool_docs.OPERATION_LIST_BEFORE_SEQUENCE_DESCRIPTION)),
        ] = None,
        page_size: Annotated[
            DirectoryPageSize,
            Field(description=_tool_docs.OPERATION_LIST_PAGE_SIZE_DESCRIPTION),
        ] = 20,
        operation_kind: Annotated[
            CriticalOperationKind | None,
            Field(description=(_tool_docs.OPERATION_LIST_OPERATION_KIND_DESCRIPTION)),
        ] = None,
        tool_name: str | None = None,
        outcome: Literal["succeeded", "failed"] | None = None,
        mode: str = "catalog",
        cursor: str | None = None,
    ) -> OperationListResult | IfcProductsRecoveryListResult:
        try:
            if mode == "ifc_products_recovery":
                return await application.list_ifc_products_recovery_operations(
                    direct_session_alias, tool_name=tool_name, cursor=cursor, page_size=page_size)
            request = DirectOperationListInput(
                direct_session_alias=direct_session_alias,
                before_sequence=before_sequence,
                page_size=page_size,
                operation_kind=operation_kind,
                tool_name=tool_name, outcome=outcome,
            )
            return await application.list_operations(
                request.direct_session_alias,
                request.before_sequence,
                request.page_size,
                request.operation_kind,
                tool_name=request.tool_name, outcome=request.outcome,
            )
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    register_provider_tool(
        server, operation_list, name="operation_list",
        description=_tool_docs.OPERATION_LIST_DESCRIPTION,
        input_type=DirectOperationListRequest, output_type=OperationListResult | IfcProductsRecoveryListResult,
        compact_input=True,
        compact_output=True,
    )


def register_operation_get(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    async def operation_get(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_ALIAS_SHORT_DESCRIPTION,
            ),
        ],
        operation_alias: str | None = None,
        case_key: str | None = None,
        after_case_key: str | None = None,
        page_size: int = 1,
        mode: Literal["operation", "operation_reference", "inspection", "transform_input", "preview_report"] = "operation",
        operation_ref: str | None = None,
        inspection_ref: str | None = None,
        content_kind: str | None = None,
        input_kind: Literal["package_description", "output_schema", "metadata", "inline_schema", "inline_rows", "parameter", "inline_text"] | None = None,
        payload_name: str | None = None,
        view: TextSourceView | None = None,
        section: str | None = None, start_index: int | None = None, item_count: int | None = None,
        input_name: str | None = None, start_column: int | None = None, column_count: int | None = None,
        start_row: int | None = None, row_count: int | None = None, select: list[str] | None = None,
        parameter_name: str | None = None, row_index: int | None = None, column_name: str | None = None,
        start_character: int | None = None, character_count: int | None = None,
    ) -> OperationGetResult:
        try:
            values = {"direct_session_alias": direct_session_alias, "mode": mode}
            if mode == "transform_input":
                values.update(operation_alias=operation_alias, input_kind=input_kind)
                values.update({key: value for key, value in dict(payload_name=payload_name, view=view,
                    section=section, start_index=start_index, item_count=item_count, input_name=input_name,
                    start_column=start_column, column_count=column_count, start_row=start_row, row_count=row_count,
                    select=select, parameter_name=parameter_name, row_index=row_index, column_name=column_name,
                    start_character=start_character, character_count=character_count).items() if value is not None})
            elif mode == "preview_report":
                values.update(operation_ref=operation_ref, content_kind=content_kind)
                values.update({key:value for key,value in dict(case_key=case_key,
                    start_character=start_character, character_count=character_count).items() if value is not None})
            elif mode == "inspection":
                values["inspection_ref"] = inspection_ref
            else:
                values.update(case_key=case_key, after_case_key=after_case_key, page_size=page_size)
                values["operation_ref" if mode == "operation_reference" else "operation_alias"] = (
                    operation_ref if mode == "operation_reference" else operation_alias)
            request = TypeAdapter(DirectOperationGetInput).validate_python(values)
            return await application.get_operation(
                request.direct_session_alias,
                selection=request,
            )
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    register_provider_tool(
        server, operation_get, name="operation_get",
        description=_tool_docs.OPERATION_GET_DESCRIPTION,
        input_type=DirectOperationGetInput, output_type=OperationGetResult,
        compact_input=True,
        compact_output=True,
    )
