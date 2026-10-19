"""Shared Direct Provider projection for exact UTF-8 source views.

Each capability remains responsible for resolving and authorizing its own exact
source fact.  This module only owns the closed Provider view vocabulary and the
deterministic conversion to and from the existing text-source Application Core.
"""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import Field, field_validator

from CAD2BIM_backend.domain.text_source import (
    TextSourceFullResult,
    TextSourceLinesResult,
    TextSourceSearchResult,
    TextSourceView,
    TextSourceViewResult,
)
from CAD2BIM_mcp.direct.contract_base import DirectContract
from CAD2BIM_mcp.tool_docs import source as _tool_docs


MAX_SOURCE_VIEW_BYTES = 65_536


def _utf8_bounded(value: str, *, maximum: int, label: str) -> str:
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds {maximum} UTF-8 bytes")
    return value


class FullSourceView(DirectContract):
    view_mode: Literal["full"] = Field(
        description=_tool_docs.FULL_SOURCE_VIEW_VIEW_MODE_DESCRIPTION
    )


class LinesSourceView(DirectContract):
    view_mode: Literal["lines"] = Field(
        description=_tool_docs.LINES_SOURCE_VIEW_VIEW_MODE_DESCRIPTION
    )
    start_line: int = Field(
        strict=True,
        ge=1,
        le=MAX_SOURCE_VIEW_BYTES,
        description=_tool_docs.LINES_SOURCE_VIEW_START_LINE_DESCRIPTION,
    )
    line_count: int = Field(
        strict=True,
        ge=1,
        le=200,
        description=_tool_docs.LINES_SOURCE_VIEW_LINE_COUNT_DESCRIPTION,
    )


class SearchSourceView(DirectContract):
    view_mode: Literal["search"] = Field(
        description=_tool_docs.SEARCH_SOURCE_VIEW_VIEW_MODE_DESCRIPTION
    )
    text: str = Field(
        min_length=1,
        max_length=MAX_SOURCE_VIEW_BYTES,
        description=(_tool_docs.SEARCH_SOURCE_VIEW_TEXT_DESCRIPTION),
    )
    context_lines: int = Field(
        default=3,
        strict=True,
        ge=0,
        le=20,
        description=_tool_docs.SEARCH_SOURCE_VIEW_CONTEXT_LINES_DESCRIPTION,
    )
    max_matches: int = Field(
        default=20,
        strict=True,
        ge=1,
        le=50,
        description=_tool_docs.SEARCH_SOURCE_VIEW_MAX_MATCHES_DESCRIPTION,
    )

    @field_validator("text")
    @classmethod
    def validate_search_text(cls, value: str) -> str:
        return _utf8_bounded(value, maximum=MAX_SOURCE_VIEW_BYTES, label="search text")


SourceViewInput: TypeAlias = FullSourceView | LinesSourceView | SearchSourceView


def to_text_source_view(value: SourceViewInput) -> TextSourceView:
    if isinstance(value, FullSourceView):
        return TextSourceView(view_mode="full")
    if isinstance(value, LinesSourceView):
        return TextSourceView(
            view_mode="lines",
            start_line=value.start_line,
            line_count=value.line_count,
        )
    if isinstance(value, SearchSourceView):
        return TextSourceView(
            view_mode="search",
            text=value.text,
            context_lines=value.context_lines,
            max_matches=value.max_matches,
        )
    raise TypeError("unsupported source view input")


def project_text_source_view(value: TextSourceViewResult) -> dict[str, object]:
    if isinstance(value, TextSourceFullResult):
        return {
            "view_mode": "full",
            "source_sha256": value.sha256,
            "total_lines": value.total_lines,
            "text": value.text,
            "returned_start_line": 1,
            "returned_line_count": value.total_lines,
            "match_count": 0,
            "truncated": False,
        }
    if isinstance(value, TextSourceLinesResult):
        return {
            "view_mode": "lines",
            "source_sha256": value.sha256,
            "total_lines": value.total_lines,
            "text": "".join(line.text for line in value.lines),
            "returned_start_line": value.start_line,
            "returned_line_count": value.returned_line_count,
            "match_count": 0,
            "truncated": value.end_line < value.total_lines,
        }
    if isinstance(value, TextSourceSearchResult):
        text = "\n".join(
            f"[match {index} lines {match.excerpt_start_line}-{match.excerpt_end_line}]\n"
            f"{match.excerpt}"
            for index, match in enumerate(value.matches, start=1)
        )
        return {
            "view_mode": "search",
            "source_sha256": value.sha256,
            "total_lines": value.total_lines,
            "text": text,
            "returned_start_line": (value.matches[0].excerpt_start_line if value.matches else None),
            "returned_line_count": sum(
                match.excerpt_end_line - match.excerpt_start_line + 1 for match in value.matches
            ),
            "match_count": value.total_match_count,
            "truncated": not value.complete,
        }
    raise TypeError("unsupported source view result")


__all__ = [
    "FullSourceView",
    "LinesSourceView",
    "MAX_SOURCE_VIEW_BYTES",
    "SearchSourceView",
    "SourceViewInput",
    "project_text_source_view",
    "to_text_source_view",
]
