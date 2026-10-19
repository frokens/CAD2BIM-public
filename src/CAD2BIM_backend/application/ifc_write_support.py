"""Small shared helpers for durable non-initial IFC write use cases."""

from __future__ import annotations

from collections.abc import Mapping


def require_ifc_idempotency_key(value: str) -> str:
    """Validate one server-issued logical invocation identity."""

    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 255:
        raise ValueError(
            "idempotency_key must be a non-empty string of at most 255 characters"
        )
    return value


def bind_failed_operation(
    error: Exception,
    *,
    operation_alias: str,
    diagnostic: Mapping[str, object] | None = None,
) -> Exception:
    """Attach the already-persisted safe failure identity to its business error."""

    error.operation_alias = operation_alias  # type: ignore[attr-defined]
    if diagnostic:
        error.diagnostic = dict(diagnostic)  # type: ignore[attr-defined]
    return error


__all__ = ["bind_failed_operation", "require_ifc_idempotency_key"]
