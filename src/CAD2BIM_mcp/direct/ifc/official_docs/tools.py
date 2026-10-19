"""FastMCP registrations for official buildingSMART documentation."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field, ValidationError

from CAD2BIM_backend.domain.ifc_official_docs import (
    IfcOfficialDocsReadQuery,
    IfcOfficialDocsReadResult,
    IfcOfficialDocsSearchQuery,
    IfcOfficialDocsSearchResult,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.protocol import DirectDataMcpApplication
from CAD2BIM_mcp.tool_docs import ifc_official_docs as _tool_docs


def register_ifc_search_official_docs(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    @server.tool(
        name="ifc_search_official_docs",
        description=(_tool_docs.IFC_SEARCH_OFFICIAL_DOCS_DESCRIPTION),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    async def ifc_search_official_docs(
        query: Annotated[
            str,
            Field(
                min_length=1,
                max_length=1_000,
                description=_tool_docs.IFC_SEARCH_OFFICIAL_DOCS_QUERY_DESCRIPTION,
            ),
        ],
        max_results: Annotated[
            int,
            Field(
                strict=True,
                ge=1,
                le=5,
                description=_tool_docs.IFC_SEARCH_OFFICIAL_DOCS_MAX_RESULTS_DESCRIPTION,
            ),
        ] = 3,
    ) -> IfcOfficialDocsSearchResult:
        try:
            request = IfcOfficialDocsSearchQuery(query=query, max_results=max_results)
            return await application.search_ifc_official_docs(request)
        except ValidationError as exc:
            raise _tool_error(
                DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "The official IFC documentation search request is invalid",
                )
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None


def register_ifc_read_official_doc(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    @server.tool(
        name="ifc_read_official_doc",
        description=(_tool_docs.IFC_READ_OFFICIAL_DOC_DESCRIPTION),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    async def ifc_read_official_doc(
        url: Annotated[
            str,
            Field(
                min_length=1,
                max_length=2_048,
                description=_tool_docs.IFC_READ_OFFICIAL_DOC_URL_DESCRIPTION,
            ),
        ],
        focus: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=1_000,
                description=(_tool_docs.IFC_READ_OFFICIAL_DOC_FOCUS_DESCRIPTION),
            ),
        ] = None,
    ) -> IfcOfficialDocsReadResult:
        try:
            request = IfcOfficialDocsReadQuery(url=url, focus=focus)
            return await application.read_ifc_official_doc(request)
        except ValidationError as exc:
            raise _tool_error(
                DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "The official IFC documentation read request is invalid",
                )
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None
