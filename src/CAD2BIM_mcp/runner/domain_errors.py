"""Bounded diagnostics shared by authenticated Runner domain tools."""

from __future__ import annotations

import re

from fastmcp.exceptions import ToolError

from CAD2BIM_mcp.direct.errors import _bounded_diagnostic_json
from CAD2BIM_mcp.runner.backend_client import BackendDomainError


_DOMAIN_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,119}\Z")
_ERROR_PHASES = frozenset({
    "read", "preflight", "input_validation", "code_validation", "execution", "output_validation",
    "publication", "ifc_validation", "storage_verification", "commit_fence", "reconcile",
})
_OPERATION_ALIAS = re.compile(r"op_[0-9]{3,}\Z")
_ATTEMPT_ALIAS = re.compile(r"cur\.[A-Za-z0-9._~-]{1,1024}\Z")
_OPERATION_REFERENCE = re.compile(r"(?:cur\.[A-Za-z0-9._~-]{1,16380}|or_[A-Za-z0-9_-]{22})\Z")
_GENERIC_ERROR = (
    "BACKEND_DOMAIN_FAILURE; phase=read; retryable=false; operation_status=unknown; message=Domain request failed"
)


def _bounded_text(value: str, maximum: int) -> str:
    normalized = " ".join(value.split())
    return normalized.encode("utf-8")[:maximum].decode("utf-8", errors="ignore")


def backend_domain_tool_error(error: BackendDomainError) -> ToolError:
    """Project the approved domain diagnostic without exposing HTTP or SQL details."""

    detail = error.detail
    if not isinstance(detail, dict):
        return ToolError(_GENERIC_ERROR)
    code = detail.get("code")
    message = detail.get("message")
    phase = detail.get("phase")
    retryable = detail.get("retryable")
    if (
        not isinstance(code, str) or _DOMAIN_CODE.fullmatch(code) is None
        or not isinstance(message, str) or not message.strip()
        or phase not in _ERROR_PHASES or type(retryable) is not bool
    ):
        return ToolError(_GENERIC_ERROR)
    metadata = [code, f"phase={phase}", f"retryable={str(retryable).lower()}"]
    operation_alias = detail.get("operation_alias")
    if isinstance(operation_alias, str) and _OPERATION_ALIAS.fullmatch(operation_alias):
        metadata.append(f"operation_alias={operation_alias}")
    operation_ref = detail.get("operation_ref")
    has_reference = isinstance(operation_ref, str) and _OPERATION_REFERENCE.fullmatch(operation_ref)
    if has_reference:
        metadata.append(f"operation_ref={operation_ref}")
    attempt_alias = detail.get("attempt_alias")
    if isinstance(attempt_alias, str) and _ATTEMPT_ALIAS.fullmatch(attempt_alias):
        metadata.append(f"attempt_alias={attempt_alias}")
    has_original = has_reference or (
        isinstance(operation_alias, str) and _OPERATION_ALIAS.fullmatch(operation_alias)
    )
    status = detail.get("operation_status")
    rejected = (error.status_code in {409, 422} and status == "not_started"
        and retryable is False and not has_original and attempt_alias is None)
    settled = (error.status_code == 409 and has_original
        and status in {"prepared", "indeterminate", "succeeded", "failed"})
    if not rejected and not settled:
        status = "unknown"
    metadata.append(f"operation_status={status}")
    diagnostic = detail.get("diagnostic")
    suffix = (
        "; diagnostic=" + _bounded_diagnostic_json(diagnostic, 1_200)
        if isinstance(diagnostic, dict) and diagnostic else ""
    )
    prefix = "; ".join(metadata) + "; message="
    budget = max(0, 2_000 - len((prefix + suffix).encode("utf-8")))
    return ToolError(prefix + _bounded_text(message, budget) + suffix)


__all__ = ["backend_domain_tool_error"]
