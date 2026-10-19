"""Application-facing contract for authenticated opaque continuation tokens."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class OpaqueCursorCodecError(ValueError):
    """An opaque continuation could not be encoded or authenticated."""


class OpaqueCursorCodecPort(Protocol):
    """Encode and decode bounded server-owned cursor claims."""

    def encode(self, payload: Mapping[str, Any]) -> str: ...

    def decode(self, token: str) -> dict[str, Any]: ...


__all__ = ["OpaqueCursorCodecError", "OpaqueCursorCodecPort"]
