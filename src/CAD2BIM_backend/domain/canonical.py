"""Shared RFC 8785 canonical JSON and stable digest helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel
import rfc8785


def canonical_value(value: Any) -> Any:
    """Convert supported domain values to deterministic JSON data."""

    if isinstance(value, BaseModel):
        return canonical_value(value.model_dump(mode="python"))
    if isinstance(value, Enum):
        return canonical_value(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("canonical JSON mapping keys must be strings")
        return {key: canonical_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [canonical_value(item) for item in value]
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
        raise ValueError("canonical JSON does not permit NaN or infinity")
    return value


def canonical_json(value: Any) -> str:
    """Render the bounded domain through RFC 8785 JCS."""

    try:
        return rfc8785.dumps(canonical_value(value)).decode("utf-8")
    except rfc8785.CanonicalizationError as exc:
        raise ValueError("value is outside the RFC 8785 canonical JSON domain") from exc


def sha256_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(kind: str, value: Any) -> str:
    return f"{kind}_{sha256_digest(value)[:32]}"


__all__ = ["canonical_json", "canonical_value", "sha256_digest", "stable_id"]
