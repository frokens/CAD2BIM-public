"""Entry-neutral boundary for exact literal source patching."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from CAD2BIM_backend.domain.text_source import SourceEdit


class SourcePatchPortError(RuntimeError):
    """Stable patch failure category independent of the concrete engine."""

    def __init__(self, failure: str, message: str, *, edit_index: int | None = None) -> None:
        super().__init__(message)
        self.failure = failure
        self.edit_index = edit_index


class SourcePatchPort(Protocol):
    def apply_text(
        self,
        source: str,
        edits: Sequence[SourceEdit],
        *,
        maximum_source_bytes: int,
    ) -> str: ...



__all__ = ["SourcePatchPort", "SourcePatchPortError"]
