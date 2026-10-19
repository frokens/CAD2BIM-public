"""Server-only logical identities for Direct IFC write invocations."""

from __future__ import annotations

from typing import Any

from CAD2BIM_mcp.direct.invocation import server_direct_invocation_key


def server_ifc_invocation_key(context: Any) -> str:
    """Bind one write to its MCP session and request without exposing either value.

    JSON-RPC request IDs are unique only within one MCP session and may be reused
    after a stdio server restart.  The session namespace prevents a new logical
    invocation from replaying an unrelated durable Operation that happened to use
    the same transport-local request number.
    """

    return server_direct_invocation_key(context)


__all__ = ["server_ifc_invocation_key"]
