"""Shared bounded full/lines/literal-search projection for exact source text."""

from __future__ import annotations

import bisect
import hashlib
from dataclasses import dataclass

from CAD2BIM_backend.domain.text_source import (
    TextSourceFullResult,
    TextSourceLine,
    TextSourceLinesResult,
    TextSourceSearchMatch,
    TextSourceSearchResult,
    TextSourceView,
    TextSourceViewResult,
)


class TextSourceViewError(ValueError):
    """A source or requested projection is invalid or outside its budget."""


@dataclass(frozen=True, slots=True)
class TextSourceViewPolicy:
    maximum_source_bytes: int = 512 * 1024
    maximum_full_bytes: int = 64 * 1024
    maximum_projected_bytes: int = 96 * 1024

    def __post_init__(self) -> None:
        if min(
            self.maximum_source_bytes,
            self.maximum_full_bytes,
            self.maximum_projected_bytes,
        ) < 1:
            raise ValueError("text source view budgets must be positive")


class TextSourceViewService:
    """Project one already-authorized exact UTF-8 source without changing it."""

    def __init__(self, policy: TextSourceViewPolicy | None = None) -> None:
        self._policy = policy or TextSourceViewPolicy()

    def view(self, source: str, request: TextSourceView) -> TextSourceViewResult:
        if "\x00" in source:
            raise TextSourceViewError("source text must not contain NUL")
        encoded = source.encode("utf-8")
        if len(encoded) > self._policy.maximum_source_bytes:
            raise TextSourceViewError("source text exceeds its read budget")
        lines = source.splitlines(keepends=True) or [source]
        common = {
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "size_bytes": len(encoded),
            "total_lines": len(lines),
        }
        if request.view_mode == "full":
            if len(encoded) > self._policy.maximum_full_bytes:
                raise TextSourceViewError(
                    "source is too large for full view; use lines or search"
                )
            return TextSourceFullResult(**common, text=source)

        if request.view_mode == "lines":
            assert request.start_line is not None and request.line_count is not None
            if request.start_line > len(lines):
                raise TextSourceViewError(
                    f"start_line {request.start_line} exceeds total_lines {len(lines)}"
                )
            selected = lines[
                request.start_line - 1 : request.start_line - 1 + request.line_count
            ]
            if len("".join(selected).encode("utf-8")) > self._policy.maximum_projected_bytes:
                raise TextSourceViewError("selected source lines exceed the response budget")
            return TextSourceLinesResult(
                **common,
                start_line=request.start_line,
                end_line=request.start_line + len(selected) - 1,
                returned_line_count=len(selected),
                lines=tuple(
                    TextSourceLine(line_number=request.start_line + index, text=text)
                    for index, text in enumerate(selected)
                ),
            )

        assert request.text is not None
        offsets: list[int] = []
        cursor = 0
        while True:
            found = source.find(request.text, cursor)
            if found < 0:
                break
            offsets.append(found)
            cursor = found + 1

        line_starts = [0]
        for line in lines[:-1]:
            line_starts.append(line_starts[-1] + len(line))
        projected: list[TextSourceSearchMatch] = []
        used_excerpt_bytes = 0
        for offset in offsets[: request.max_matches]:
            line_index = bisect.bisect_right(line_starts, offset) - 1
            end_offset = offset + len(request.text)
            end_line_index = bisect.bisect_right(
                line_starts, max(offset, end_offset - 1)
            ) - 1
            excerpt_start = max(0, line_index - request.context_lines)
            excerpt_end = min(len(lines), end_line_index + request.context_lines + 1)
            excerpt = "".join(lines[excerpt_start:excerpt_end])
            excerpt_bytes = len(excerpt.encode("utf-8"))
            if used_excerpt_bytes + excerpt_bytes > self._policy.maximum_projected_bytes:
                break
            used_excerpt_bytes += excerpt_bytes
            projected.append(
                TextSourceSearchMatch(
                    line=line_index + 1,
                    column=offset - line_starts[line_index] + 1,
                    end_line=end_line_index + 1,
                    end_column=end_offset - line_starts[end_line_index] + 1,
                    excerpt_start_line=excerpt_start + 1,
                    excerpt_end_line=excerpt_end,
                    excerpt=excerpt,
                )
            )
        return TextSourceSearchResult(
            **common,
            total_match_count=len(offsets),
            returned_count=len(projected),
            complete=len(projected) == len(offsets),
            matches=tuple(projected),
        )


__all__ = ["TextSourceViewError", "TextSourceViewPolicy", "TextSourceViewService"]
