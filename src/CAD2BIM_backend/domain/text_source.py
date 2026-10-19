"""Bounded, exact views over trusted UTF-8 source text."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TextSourceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class SourceEdit:
    """One entry-neutral exact literal replacement request."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    old_text: str
    new_text: str


class TextSet(TextSourceContract):
    """Complete text input; the content owner validates its final business rules."""

    action: Literal["set"]
    value: str = Field(min_length=1, max_length=65_536)


class TextEdit(TextSourceContract):
    """Literal edits against one unchanged base, shared by content owners."""

    action: Literal["edit"]
    edits: tuple[SourceEdit, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_edits(self) -> "TextEdit":
        total = 0
        for index, edit in enumerate(self.edits, 1):
            if not edit.old_text:
                raise ValueError(f"edit {index}: old_text must not be empty")
            for text in (edit.old_text, edit.new_text):
                if "\x00" in text:
                    raise ValueError(f"edit {index}: text must not contain NUL")
                try:
                    size = len(text.encode("utf-8", errors="strict"))
                except UnicodeEncodeError as exc:
                    raise ValueError(f"edit {index}: text must be valid UTF-8") from exc
                if size > 16_384:
                    raise ValueError(f"edit {index}: text exceeds 16,384 UTF-8 bytes")
                total += size
        if total > 32_768:
            raise ValueError("edits exceed 32,768 aggregate UTF-8 bytes")
        return self


TextChange: TypeAlias = Annotated[TextSet | TextEdit, Field(discriminator="action")]


@dataclass(frozen=True, slots=True)
class SourceBundleLiteralEdit:
    """Patch one existing UTF-8 Python member by exact literal replacement."""

    path: str
    edits: tuple[SourceEdit, ...]
    change_kind: Literal["literal_edit"] = field(default="literal_edit", kw_only=True)


@dataclass(frozen=True, slots=True)
class SourceBundleAddMember:
    """Add one previously absent UTF-8 Python member."""

    path: str
    source: str
    change_kind: Literal["add_member"] = field(default="add_member", kw_only=True)


@dataclass(frozen=True, slots=True)
class SourceBundleReplaceMember:
    """Replace one existing UTF-8 Python member of the selected source bundle."""

    path: str
    source: str
    change_kind: Literal["replace_member"] = field(default="replace_member", kw_only=True)


@dataclass(frozen=True, slots=True)
class SourceBundleDeleteMember:
    """Delete one existing UTF-8 Python member of the selected source bundle."""

    path: str
    change_kind: Literal["delete_member"] = field(default="delete_member", kw_only=True)


SourceBundleChange: TypeAlias = Annotated[
    SourceBundleLiteralEdit
    | SourceBundleAddMember
    | SourceBundleReplaceMember
    | SourceBundleDeleteMember,
    Field(discriminator="change_kind"),
]


class TextSourceView(TextSourceContract):
    """One closed source-view request shared by persisted source consumers."""

    view_mode: Literal["full", "lines", "search"]
    start_line: int | None = Field(default=None, ge=1)
    line_count: int | None = Field(default=None, ge=1, le=200)
    text: str | None = Field(default=None, min_length=1, max_length=4096)
    context_lines: int = Field(default=3, ge=0, le=20)
    max_matches: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def validate_branch(self) -> "TextSourceView":
        if self.view_mode == "full":
            if self.start_line is not None or self.line_count is not None or self.text is not None:
                raise ValueError("full source view does not accept lines or search fields")
        elif self.view_mode == "lines":
            if self.start_line is None or self.line_count is None or self.text is not None:
                raise ValueError("lines source view requires only start_line and line_count")
        elif self.text is None or self.start_line is not None or self.line_count is not None:
            raise ValueError("search source view requires text and no line range")
        if "\x00" in (self.text or ""):
            raise ValueError("source search text must not contain NUL")
        return self


class TextSourceLine(TextSourceContract):
    line_number: int = Field(ge=1)
    text: str


class TextSourceSearchMatch(TextSourceContract):
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    end_line: int = Field(ge=1)
    end_column: int = Field(ge=1)
    excerpt_start_line: int = Field(ge=1)
    excerpt_end_line: int = Field(ge=1)
    excerpt: str


class TextSourceFullResult(TextSourceContract):
    view_mode: Literal["full"] = "full"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    total_lines: int = Field(ge=1)
    text: str


class TextSourceLinesResult(TextSourceContract):
    view_mode: Literal["lines"] = "lines"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    total_lines: int = Field(ge=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    returned_line_count: int = Field(ge=1, le=200)
    lines: tuple[TextSourceLine, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_lines(self) -> "TextSourceLinesResult":
        if self.returned_line_count != len(self.lines):
            raise ValueError("returned_line_count must equal lines length")
        if self.end_line != self.start_line + self.returned_line_count - 1:
            raise ValueError("source line range is inconsistent")
        return self


class TextSourceSearchResult(TextSourceContract):
    view_mode: Literal["search"] = "search"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    total_lines: int = Field(ge=1)
    total_match_count: int = Field(ge=0)
    returned_count: int = Field(ge=0, le=50)
    complete: bool
    matches: tuple[TextSourceSearchMatch, ...] = Field(max_length=50)

    @model_validator(mode="after")
    def validate_matches(self) -> "TextSourceSearchResult":
        if self.returned_count != len(self.matches):
            raise ValueError("returned_count must equal matches length")
        if self.complete != (self.returned_count == self.total_match_count):
            raise ValueError("complete must identify whether every literal match was returned")
        return self


TextSourceViewResult = TextSourceFullResult | TextSourceLinesResult | TextSourceSearchResult


__all__ = [
    "SourceBundleAddMember",
    "SourceBundleChange",
    "SourceBundleDeleteMember",
    "SourceBundleLiteralEdit",
    "SourceBundleReplaceMember",
    "SourceEdit",
    "TextSet",
    "TextEdit",
    "TextChange",
    "TextSourceFullResult",
    "TextSourceLine",
    "TextSourceLinesResult",
    "TextSourceSearchMatch",
    "TextSourceSearchResult",
    "TextSourceView",
    "TextSourceViewResult",
]
