"""Stable Direct application and provider error projection."""

from __future__ import annotations

import json
from typing import Literal

from fastmcp.exceptions import ToolError



class DirectApplicationError(RuntimeError):
    """Expected application failure safe to expose as a bounded tool error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: Literal[
            "read",
            "preflight",
            "code_validation",
            "execution",
            "output_validation",
            "publication",
            "ifc_validation",
            "storage_verification",
            "commit_fence",
            "reconcile",
        ] = "read",
        retryable: bool = False,
        operation_alias: str | None = None,
        operation_ref: str | None = None,
        operation_status: Literal["prepared", "indeterminate", "succeeded", "failed", "unknown"] | None = None,
        diagnostic: dict[str, object] | None = None,
    ) -> None:
        super().__init__(_bounded_utf8_text(message, 1_700))
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.operation_alias = operation_alias
        self.operation_ref = operation_ref
        self.operation_status = operation_status
        self.diagnostic = diagnostic


def _tool_error(error: DirectApplicationError) -> ToolError:
    phase = getattr(error, "phase", "preflight")
    retryable = bool(getattr(error, "retryable", False))
    metadata = [
        error.code,
        f"phase={phase}",
        f"retryable={'true' if retryable else 'false'}",
    ]
    operation_alias = getattr(error, "operation_alias", None)
    if operation_alias is not None:
        metadata.append(f"operation_alias={operation_alias}")
    for name in ("operation_ref", "operation_status"):
        value = getattr(error, name, None)
        if value is not None:
            metadata.append(f"{name}={value}")
    diagnostic = getattr(error, "diagnostic", None)
    diagnostic_suffix = ""
    if isinstance(diagnostic, dict) and diagnostic:
        diagnostic_suffix = f"; diagnostic={_bounded_diagnostic_json(diagnostic, 1_200)}"
    prefix = f"{'; '.join(metadata)}; message="
    reserved = len((prefix + diagnostic_suffix).encode("utf-8"))
    message_budget = max(0, 2_000 - reserved)
    return ToolError(f"{prefix}{_bounded_utf8_text(str(error), message_budget)}{diagnostic_suffix}")


def _bounded_diagnostic_json(value: dict[str, object], maximum: int) -> str:
    """Keep compiler coordinates structured while bounding potentially long source lines."""

    safe = dict(value)
    while True:
        rendered = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        encoded = rendered.encode("utf-8")
        if len(encoded) <= maximum:
            return rendered
        strings = [(key, item) for key, item in safe.items() if isinstance(item, str) and item]
        if not strings:
            return '{"diagnostic_truncated":true}'
        key, item = max(strings, key=lambda pair: len(pair[1].encode("utf-8")))
        excess = len(encoded) - maximum
        target = max(0, len(item.encode("utf-8")) - excess - 32)
        safe[key] = item.encode("utf-8")[:target].decode("utf-8", errors="ignore")
        safe[f"{key}_truncated"] = True


def _bounded_utf8_text(value: str, maximum: int) -> str:
    if maximum <= 0:
        return ""
    normalized = " ".join(value.split())
    encoded = normalized.encode("utf-8")
    if len(encoded) <= maximum:
        return normalized
    return encoded[:maximum].decode("utf-8", errors="ignore")


__all__ = ["DirectApplicationError"]
