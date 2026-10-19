"""FastMCP registrations for owner-backed code discovery and reading."""

from __future__ import annotations

from fastmcp import FastMCP
from pydantic import ValidationError

from CAD2BIM_mcp.direct.code.application import CodeProviderApplication
from CAD2BIM_mcp.direct.code.contracts import (
    DirectCodeListInput,
    DirectCodeListResult,
    DirectCodeReadInput,
    DirectCodeReadResponse,
    validate_code_list_input,
    validate_code_read_input,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.tool_support import invalid_provider_input, register_provider_tool
from CAD2BIM_mcp.tool_docs import code as _tool_docs


def register_code_list(
    server: FastMCP,
    application: CodeProviderApplication,
) -> None:
    async def code_list(
        direct_session_alias: str,
        mode: str,
        query: object | None = None,
        cursor: str | None = None,
    ) -> DirectCodeListResult:
        try:
            value: dict[str, object] = {
                "direct_session_alias": direct_session_alias,
                "mode": mode,
            }
            if mode == "first":
                value["query"] = query
            elif mode == "cursor":
                value["cursor"] = cursor
            return await application.list_code(validate_code_list_input(value))
        except ValidationError as exc:
            raise invalid_provider_input(
                "Use one complete first-page code-kind query or unchanged cursor branch", exc
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    register_provider_tool(
        server,
        code_list,
        name="code_list",
        input_type=DirectCodeListInput,
        compact_input=True,
        output_type=DirectCodeListResult,
        description=(_tool_docs.CODE_LIST_DESCRIPTION),
    )


def register_code_read(
    server: FastMCP,
    application: CodeProviderApplication,
) -> None:
    async def code_read(
        direct_session_alias: str,
        source_form: str | None = None,
        code_locator: str | None = None,
        view: object | None = None,
        source_path: str | None = None,
        mode: str | None = None,
        reads: object | None = None,
    ) -> DirectCodeReadResponse:
        try:
            value: dict[str, object] = {
                "direct_session_alias": direct_session_alias,
                "source_form": source_form,
                "code_locator": code_locator,
                "view": view,
            }
            if mode == "batch":
                value = {
                    "direct_session_alias": direct_session_alias,
                    "mode": mode,
                    "reads": reads,
                }
            elif source_form == "bundle":
                value["source_path"] = source_path
            return await application.read_code(validate_code_read_input(value))
        except ValidationError as exc:
            raise invalid_provider_input(
                "Use a complete single_text or bundle read, or mode=batch with 1-8 reads", exc
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    register_provider_tool(
        server,
        code_read,
        name="code_read",
        input_type=DirectCodeReadInput,
        compact_input=True,
        output_type=DirectCodeReadResponse,
        description=(_tool_docs.CODE_READ_DESCRIPTION),
    )


CODE_TOOL_REGISTRARS = (register_code_list, register_code_read)

__all__ = ["CODE_TOOL_REGISTRARS", "register_code_list", "register_code_read"]
