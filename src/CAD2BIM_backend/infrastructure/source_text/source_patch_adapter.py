"""Adapter from the neutral patch Port to the existing Textum-backed engine."""

from __future__ import annotations

from collections.abc import Sequence

from CAD2BIM_backend.domain.text_source import SourceEdit
from CAD2BIM_backend.infrastructure.source_text.source_patch import (
    SourcePatchError,
    apply_text_edits,
)
from CAD2BIM_backend.ports.source_patch import (
    SourcePatchPortError,
)


class TextSourcePatchAdapter:
    def apply_text(
        self,
        source: str,
        edits: Sequence[SourceEdit],
        *,
        maximum_source_bytes: int,
    ) -> str:
        try:
            return apply_text_edits(source, edits, maximum_source_bytes=maximum_source_bytes)
        except SourcePatchError as exc:
            raise SourcePatchPortError(exc.failure.value, str(exc), edit_index=exc.edit_index) from None



__all__ = ["TextSourcePatchAdapter"]
