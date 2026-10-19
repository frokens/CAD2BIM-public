"""Direct tool registrations owned by this capability family."""

from __future__ import annotations

from typing import Annotated

from fastmcp import Context, FastMCP
from fastmcp.tools.function_tool import FunctionTool
from pydantic import Field, ValidationError

from CAD2BIM_backend.domain.data_query_sql import DataQuerySqlResult
from CAD2BIM_backend.domain.data_query_python import (
    DataQueryPythonResult,
)
from CAD2BIM_backend.domain.data_json import JsonReadResult

from CAD2BIM_backend.domain.data_query import (
    DataQueryRowsResult,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.server_support import _validated_query
from CAD2BIM_mcp.direct.protocol import DirectDataMcpApplication
from CAD2BIM_mcp.direct.provider_schema import (
    _apply_query_cursor_xor_schema,
    _object_union_output_schema,
    compact_provider_input_schema,
)
from CAD2BIM_mcp.direct.query.sql_inputs import (
    DirectDataQuerySqlInput,
    QuerySqlContinuationInput,
    QuerySqlFirstPageInput,
    QuerySqlQuery,
    QuerySqlRepairInput,
    QuerySqlSourceEdit,
    to_domain_data_query_sql_query,
    validate_direct_data_query_sql_input,
)
from CAD2BIM_mcp.direct.query.json_inputs import (
    DirectDataReadJsonInput,
    DirectJsonContinuationInput,
    JsonChildrenToolQuery,
    JsonRecordsToolQuery,
    JsonValueToolQuery,
    to_domain_json_query,
    validate_direct_data_read_json_input,
)
from CAD2BIM_mcp.direct.query.python_inputs import (
    DirectDataQueryPythonInput,
    QueryPythonContinuationInput,
    QueryPythonFirstPageInput,
    QueryPythonQuery,
    QueryPythonRepairInput,
    QueryPythonSourceEdit,
    to_domain_data_query_python_query,
    validate_direct_data_query_python_input,
)
from CAD2BIM_backend.domain.text_source import SourceEdit
from CAD2BIM_mcp.direct.invocation import server_direct_invocation_key
from CAD2BIM_mcp.direct.tool_support import (
    provider_input_schema,
    register_provider_document,
)
from CAD2BIM_mcp.direct.query.cad_inputs import (
    DataRowsToolQuery,
    to_domain_data_rows_query,
)
from CAD2BIM_mcp.tool_docs import data_query as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


def _closed_mode_schema(
    type_: object, *, omit_generated_titles: bool = False
) -> dict[str, object]:
    schema = provider_input_schema(
        type_,
        omit_generated_titles=omit_generated_titles,
    )
    branches = schema.pop("anyOf", None)
    if isinstance(branches, list):
        schema["oneOf"] = branches
    return schema


def register_data_query_sql(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    async def data_query_sql(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_ALIAS_SHORT_DESCRIPTION,
            ),
        ],
        ctx: Context,
        mode: str = Field(default=None),
        query: Annotated[
            QuerySqlQuery | None,
            Field(description=(_tool_docs.DATA_QUERY_SQL_QUERY_DESCRIPTION)),
        ] = None,
        parent_attempt_alias: str | None = None,

        edits: tuple[QuerySqlSourceEdit, ...] | None = None,
        page_size: int = 100,
        cursor: Annotated[
            str | None,
            Field(
                pattern=r"^cur\..+$",
                max_length=16_384,
                description=(_tool_docs.DATA_QUERY_SQL_CURSOR_DESCRIPTION),
            ),
        ] = None,
    ) -> DataQuerySqlResult:
        try:
            value: dict[str, object] = {
                "direct_session_alias": direct_session_alias,
            }
            if mode is not None:
                value["mode"] = mode
            if query is not None:
                value["query"] = query
            if parent_attempt_alias is not None:
                value["parent_attempt_alias"] = parent_attempt_alias

            if edits is not None:
                value["edits"] = edits
            if mode == "repair":
                value["page_size"] = page_size
            if cursor is not None:
                value["cursor"] = cursor
            request = validate_direct_data_query_sql_input(value)
            if isinstance(request, QuerySqlFirstPageInput):
                domain_query = to_domain_data_query_sql_query(request.query)
                return await application.query_sql(
                    request.direct_session_alias,
                    domain_query,
                    None,
                    request_identity=server_direct_invocation_key(ctx),
                )
            if isinstance(request, QuerySqlRepairInput):
                return await application.repair_query_sql(
                    request.direct_session_alias,
                    request.parent_attempt_alias,

                    tuple(
                        SourceEdit(old_text=item.old_text, new_text=item.new_text)
                        for item in request.edits
                    ),
                    request.page_size,
                    request_identity=server_direct_invocation_key(ctx),
                )
            if not isinstance(request, QuerySqlContinuationInput):
                raise AssertionError("unexpected data_query_sql Provider branch")
            return await application.query_sql(
                request.direct_session_alias,
                None,
                request.cursor,
                request_identity=server_direct_invocation_key(ctx),
            )
        except ValidationError as exc:
            raise _tool_error(
                DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "Select exactly one complete data_query_sql mode: fresh, repair, or cursor",
                    phase="preflight",
                )
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    data_query_sql_tool = FunctionTool.from_function(
        data_query_sql,
        name="data_query_sql",
        description=(_tool_docs.DATA_QUERY_SQL_DESCRIPTION),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False},
    )
    data_query_sql_tool.parameters = _closed_mode_schema(
        DirectDataQuerySqlInput,
        omit_generated_titles=True,
    )
    server.add_tool(data_query_sql_tool)


def register_data_query_python(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    async def data_query_python(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_ALIAS_SHORT_DESCRIPTION,
            ),
        ],
        ctx: Context,
        mode: str = Field(default=None),
        query: Annotated[
            QueryPythonQuery | None,
            Field(description=(_tool_docs.DATA_QUERY_PYTHON_QUERY_DESCRIPTION)),
        ] = None,
        parent_attempt_alias: str | None = None,

        edits: tuple[QueryPythonSourceEdit, ...] | None = None,
        page_size: int = 100,
        cursor: Annotated[
            str | None,
            Field(
                pattern=r"^cur\..+$",
                max_length=16_384,
                description=(_tool_docs.DATA_QUERY_PYTHON_CURSOR_DESCRIPTION),
            ),
        ] = None,
    ) -> DataQueryPythonResult:
        try:
            value: dict[str, object] = {
                "direct_session_alias": direct_session_alias,
            }
            if mode is not None:
                value["mode"] = mode
            if query is not None:
                value["query"] = query.model_dump(mode="python")
            if parent_attempt_alias is not None:
                value["parent_attempt_alias"] = parent_attempt_alias

            if edits is not None:
                value["edits"] = edits
            if mode == "repair":
                value["page_size"] = page_size
            if cursor is not None:
                value["cursor"] = cursor
            request = validate_direct_data_query_python_input(value)
            if isinstance(request, QueryPythonFirstPageInput):
                return await application.query_python(
                    request.direct_session_alias,
                    to_domain_data_query_python_query(request.query),
                    None,
                    request_identity=server_direct_invocation_key(ctx),
                )
            if isinstance(request, QueryPythonRepairInput):
                return await application.repair_query_python(
                    request.direct_session_alias,
                    request.parent_attempt_alias,

                    tuple(
                        SourceEdit(old_text=item.old_text, new_text=item.new_text)
                        for item in request.edits
                    ),
                    request.page_size,
                    request_identity=server_direct_invocation_key(ctx),
                )
            if not isinstance(request, QueryPythonContinuationInput):
                raise AssertionError("unexpected data_query_python Provider branch")
            return await application.query_python(
                request.direct_session_alias,
                None,
                request.cursor,
                request_identity=server_direct_invocation_key(ctx),
            )
        except ValidationError as exc:
            raise _tool_error(
                DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "Select exactly one complete data_query_python mode: fresh, repair, or cursor",
                    phase="preflight",
                )
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    data_query_python_tool = FunctionTool.from_function(
        data_query_python,
        name="data_query_python",
        description=(_tool_docs.DATA_QUERY_PYTHON_DESCRIPTION),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False},
    )
    data_query_python_tool.parameters = _closed_mode_schema(
        DirectDataQueryPythonInput,
        omit_generated_titles=True,
    )
    register_provider_document(
        server,
        tool_name="data_query_python",
        document_name="program_api",
        description=_tool_docs.DATA_QUERY_PYTHON_PROGRAM_RESOURCE_DESCRIPTION,
        document=_tool_docs.data_query_python_program_document(),
    )
    server.add_tool(data_query_python_tool)


def register_data_read_json(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    async def data_read_json(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_ALIAS_SHORT_DESCRIPTION,
            ),
        ],
        query: Annotated[
            JsonValueToolQuery | JsonChildrenToolQuery | JsonRecordsToolQuery | None,
            Field(description=(_tool_docs.DATA_READ_JSON_QUERY_DESCRIPTION)),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(
                pattern=r"^cur\..+$",
                max_length=16_384,
                description=(_tool_docs.DATA_READ_JSON_CURSOR_DESCRIPTION),
            ),
        ] = None,
    ) -> JsonReadResult:
        try:
            value: dict[str, object] = {"direct_session_alias": direct_session_alias}
            if query is not None:
                # FastMCP has already materialized the nested Provider DTO.  Re-enter the
                # closed root-union validator with plain data so Pydantic does not reject a
                # valid branch merely because the nested model class differs.
                value["query"] = query.model_dump(mode="python")
            if cursor is not None:
                value["cursor"] = cursor
            request = validate_direct_data_read_json_input(value)
            if isinstance(request, DirectJsonContinuationInput):
                return await application.read_json(
                    request.direct_session_alias,
                    None,
                    request.cursor,
                )
            return await application.read_json(
                request.direct_session_alias,
                to_domain_json_query(request.query),
                None,
            )
        except ValidationError as exc:
            raise _tool_error(
                DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "Exactly one complete data_read_json query or cursor is required",
                    phase="preflight",
                )
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    data_read_json_tool = FunctionTool.from_function(
        data_read_json,
        name="data_read_json",
        description=(_tool_docs.DATA_READ_JSON_DESCRIPTION),
        output_schema=_object_union_output_schema(JsonReadResult),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    data_read_json_tool.parameters = provider_input_schema(
        DirectDataReadJsonInput,
        omit_generated_titles=True,
    )
    server.add_tool(data_read_json_tool)


def register_data_query_rows(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    async def data_query_rows(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                description=_common_tool_docs.DIRECT_SESSION_ALIAS_SHORT_DESCRIPTION,
            ),
        ],
        query: Annotated[
            DataRowsToolQuery | None,
            Field(description=(_tool_docs.DATA_QUERY_ROWS_QUERY_DESCRIPTION)),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(
                pattern=r"^cur\..+$",
                max_length=16_384,
                description=(_tool_docs.DATA_QUERY_ROWS_CURSOR_DESCRIPTION),
            ),
        ] = None,
    ) -> DataQueryRowsResult:
        try:
            domain_query = _validated_query(
                query,
                to_domain_data_rows_query,
                "data_query_rows",
            )
            return await application.query_rows(direct_session_alias, domain_query, cursor)
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    data_query_rows_tool = FunctionTool.from_function(
        data_query_rows,
        name="data_query_rows",
        description=(_tool_docs.DATA_QUERY_ROWS_DESCRIPTION),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    _apply_query_cursor_xor_schema(data_query_rows_tool)
    data_query_rows_tool.parameters = compact_provider_input_schema(
        data_query_rows_tool.parameters
    )
    server.add_tool(data_query_rows_tool)
