"""Shared validation helpers for Direct tool registration."""

from typing import Any

from pydantic import ValidationError

from CAD2BIM_mcp.direct.errors import DirectApplicationError


def _validated_query(value: Any, converter: Any, label: str) -> Any:
    if value is None:
        return None
    try:
        return converter(value)
    except ValidationError as exc:
        raise DirectApplicationError(
            "INVALID_ARGUMENT",
            f"The {label} query does not match a supported input shape",
        ) from exc
