"""Direct tool registrations owned by this capability family."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field


from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.protocol import DirectDataMcpApplication
from CAD2BIM_mcp.direct.session.contracts import (
    DirectSessionResult,
)
from CAD2BIM_mcp.tool_docs import session as _tool_docs


def register_runtime_open_direct_session(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    @server.tool(
        name="runtime_open_direct_session",
        description=(_tool_docs.RUNTIME_OPEN_DIRECT_SESSION_DESCRIPTION),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    )
    async def runtime_open_direct_session(
        session_name: Annotated[
            str,
            Field(
                min_length=1,
                max_length=100,
                description=_tool_docs.RUNTIME_OPEN_DIRECT_SESSION_SESSION_NAME_DESCRIPTION,
            ),
        ],
    ) -> DirectSessionResult:
        try:
            return await application.open_direct_session(session_name)
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None
