"""Server-only logical identities shared by Direct durable invocations."""

from __future__ import annotations

import hashlib
from typing import Any

from CAD2BIM_mcp.direct.errors import DirectApplicationError


def server_direct_invocation_key(context: Any) -> str:
    """Namespace one transport request without exposing transport identities."""

    try:
        raw_session_id = context.session_id
        raw_request_id = context.request_id
    except (AttributeError, RuntimeError):
        raw_session_id = None
        raw_request_id = None
    session_id = "" if raw_session_id is None else str(raw_session_id).strip()
    request_id = "" if raw_request_id is None else str(raw_request_id).strip()
    if not session_id or not request_id:
        raise DirectApplicationError(
            "SYSTEM_PAUSED",
            "The server could not establish a stable Direct invocation identity",
            phase="preflight",
            retryable=False,
        )
    digest = hashlib.sha256(
        # Preserve the original stable namespace used by existing IFC writes.
        b"cad2bim-direct-ifc-invocation-v1\0"
        + session_id.encode("utf-8")
        + b"\0"
        + request_id.encode("utf-8")
    ).hexdigest()
    return f"mcp:{digest}"


__all__ = ["server_direct_invocation_key"]
