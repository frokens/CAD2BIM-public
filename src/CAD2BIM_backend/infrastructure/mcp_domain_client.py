"""Runner adapter for one saved domain tool call over the protected MCP."""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.types import CallToolResult, TextContent

from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.domain.runtime_execution import (
    DATA_PYTHON_EXECUTION,
    IFC_QUERY_EXECUTION,
    IFC_REVISION_EXECUTION,
    IFC_REVISION_INVOCATION_EXECUTION,
    RUNTIME_RESPONSE_MARGIN_SECONDS,
)
from CAD2BIM_backend.infrastructure.runner_domain_tokens import RunnerDomainTokenCodec


_LOGGER = logging.getLogger(__name__)
_ERROR_PHASES = frozenset({
    "read", "preflight", "input_validation", "code_validation", "execution", "output_validation",
    "publication", "ifc_validation", "storage_verification", "commit_fence", "reconcile",
})


class RunnerDomainOutcomeUnknown(RuntimeError):
    """The saved action must remain pending until its domain outcome is resolved."""


class RunnerDomainMcpClient:
    def __init__(self, *, mcp_url: str, codec: RunnerDomainTokenCodec) -> None:
        if not mcp_url:
            raise ValueError("Runner domain MCP URL is required")
        self._mcp_url = mcp_url
        self._codec = codec

    @property
    def mcp_url(self) -> str:
        return self._mcp_url

    async def call_tool(
        self,
        *,
        grant: RunnerCheckpointGrant,
        action_id: str,
        mcp_tool_name: str,
        domain_tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float | None = None,
        effect: Literal["READ", "WRITE"] = "WRITE",
    ) -> CallToolResult:
        if timeout_seconds is None:
            policy = (
                IFC_REVISION_INVOCATION_EXECUTION if domain_tool_name in {
                    "ifc_products_apply", "ifc_products_edit",
                } else
                IFC_REVISION_EXECUTION if domain_tool_name in {
                    "ifc_model_info", "ifc_representation_context_list", "ifc_spatial_structure_list", "ifc_products_read",
                } else
                IFC_QUERY_EXECUTION if domain_tool_name == "ifc_query_python" else
                DATA_PYTHON_EXECUTION if domain_tool_name == "data_derive" else None
            )
            if policy is not None:
                # Cover the protected Backend hop's existing response margin
                # plus transport return; do not change any execution policy.
                timeout_seconds = policy.total_seconds + 2 * RUNTIME_RESPONSE_MARGIN_SECONDS
        token = self._codec.issue(
            grant, action_id=action_id, tool_name=domain_tool_name
        )
        transport = StreamableHttpTransport(
            self._mcp_url,
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            async with Client(transport, timeout=timeout_seconds) as client:
                result = await client.call_tool_mcp(mcp_tool_name, arguments, timeout=timeout_seconds)
        except Exception as exc:
            _LOGGER.warning(
                "Runner domain outcome unconfirmed run=%s action=%s tool=%s exception_type=%s",
                getattr(getattr(grant, "scope", None), "run_id", None), action_id,
                domain_tool_name, type(exc).__name__,
            )
            raise RunnerDomainOutcomeUnknown(
                "Domain outcome is unconfirmed; retain the saved action identity and pending call"
            ) from exc
        if result.isError and effect != "READ":
            # The protected server's input diagnostic is emitted before dispatch.
            # Backend errors include operation_status, even when their code is
            # INVALID_ARGUMENT. An explicit Application proof of non-admission or
            # a persisted failure settles the saved call; phase alone does not.
            settled = False
            diagnostic = (None, None, None)
            for content in result.content:
                if not isinstance(content, TextContent):
                    continue
                header, separator, _message = content.text.partition("; message=")
                parts = header.split("; ")
                metadata = dict(part.split("=", 1) for part in parts[1:] if "=" in part)
                if separator:
                    code = parts[0] if re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", parts[0]) else None
                    phase = metadata.get("phase")
                    status = metadata.get("operation_status")
                    diagnostic = (
                        code, phase if phase in _ERROR_PHASES else None,
                        status if status in {"unknown", "not_started", "failed"} else None,
                    )
                input_rejected = (
                    separator and parts[0] == "INVALID_ARGUMENT"
                    and metadata.get("phase") == "preflight"
                    and "operation_status" not in metadata and "operation_alias" not in metadata
                    and "operation_ref" not in metadata
                )
                failed = separator and metadata.get("operation_status") == "failed" and (
                    metadata.get("operation_alias") or metadata.get("operation_ref")
                )
                rejected = (separator and metadata.get("operation_status") == "not_started"
                    and metadata.get("retryable") == "false" and "operation_alias" not in metadata
                    and "operation_ref" not in metadata
                    and "attempt_alias" not in metadata)
                settled = bool(input_rejected or failed or rejected)
                if settled:
                    break
            if not settled:
                _LOGGER.warning(
                    "Runner domain outcome unconfirmed run=%s action=%s tool=%s "
                    "code=%s phase=%s operation_status=%s exception_type=RunnerDomainOutcomeUnknown",
                    getattr(getattr(grant, "scope", None), "run_id", None), action_id,
                    domain_tool_name, *diagnostic,
                )
                raise RunnerDomainOutcomeUnknown(
                    "Domain outcome is unconfirmed; retain the saved action identity and pending call"
                )
        return result


__all__ = ["RunnerDomainMcpClient", "RunnerDomainOutcomeUnknown"]
