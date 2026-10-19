"""Deterministically compile a bounded Package catalog synopsis from Markdown."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass


MAX_DESCRIPTION_BYTES = 65_536
MAX_DESCRIPTION_TITLE_CHARACTERS = 300
MAX_SUMMARY_CHARACTERS = 1_000
MAX_SECTION_TITLE_CHARACTERS = 160
MAX_SECTION_COUNT = 16

REQUIRED_SECTION_TITLES = (
    "Summary",
    "Intended use",
    "Contents",
    "Method",
    "Scope and completeness",
    "Known limitations",
)

_ATX_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})(?:[ \t]+|$)(.*)$")
_SETEXT_UNDERLINE = re.compile(r"^[ \t]{0,3}(=+|-+)[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_INLINE_CODE = re.compile(r"(`+)(.*?)\1", re.DOTALL)
_INLINE_IMAGE = re.compile(r"!\[([^]]*)\]\([^)]*\)")
_INLINE_LINK = re.compile(r"\[([^]]+)\]\([^)]*\)")
_REFERENCE_LINK = re.compile(r"\[([^]]+)\]\[[^]]*\]")
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_AUTOLINK = re.compile(r"<((?:https?://|mailto:)[^>]+)>")
_BLOCK_PREFIX = re.compile(r"^[ \t]*(?:(?:>[ \t]*)+|[-+*][ \t]+|\d+[.)][ \t]+)")
_WHITESPACE = re.compile(r"\s+")


class PackageSynopsisError(ValueError):
    """The Package description cannot produce the required catalog projection."""


@dataclass(frozen=True, slots=True)
class PackageSynopsis:
    """Small immutable projection stored beside one exact description artifact."""

    description_title: str
    summary: str
    section_titles: tuple[str, ...]


@dataclass(slots=True)
class _Heading:
    level: int
    title: str
    body_lines: list[str]


class PackageSynopsisCompiler:
    """Compile the M2a H1, Summary and ordered H2 directory."""

    def compile(self, description_markdown: str) -> PackageSynopsis:
        if not isinstance(description_markdown, str):
            raise PackageSynopsisError("description.md must be text")
        if "\x00" in description_markdown:
            raise PackageSynopsisError("description.md must not contain NUL")
        try:
            byte_count = len(description_markdown.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise PackageSynopsisError("description.md must be valid UTF-8 text") from exc
        if byte_count == 0 or byte_count > MAX_DESCRIPTION_BYTES:
            raise PackageSynopsisError(
                f"description.md must contain 1..{MAX_DESCRIPTION_BYTES} UTF-8 bytes"
            )

        headings = _parse_headings(_HTML_COMMENT.sub("", description_markdown))
        h1 = [heading for heading in headings if heading.level == 1]
        if len(h1) != 1:
            raise PackageSynopsisError("description.md must contain exactly one H1")
        description_title = _visible_text(h1[0].title)
        _require_length(
            description_title,
            "H1",
            MAX_DESCRIPTION_TITLE_CHARACTERS,
        )

        h2 = [heading for heading in headings if heading.level == 2]
        if not h2 or len(h2) > MAX_SECTION_COUNT:
            raise PackageSynopsisError(
                f"description.md must contain 1..{MAX_SECTION_COUNT} H2 sections"
            )

        section_titles: list[str] = []
        for heading in h2:
            title = _visible_text(heading.title)
            _require_length(title, "H2 title", MAX_SECTION_TITLE_CHARACTERS)
            if title in section_titles:
                raise PackageSynopsisError(f"duplicate H2 section: {title}")
            section_titles.append(title)

        if section_titles[0] != REQUIRED_SECTION_TITLES[0]:
            raise PackageSynopsisError("Summary must be the first H2 section")

        required_positions: list[int] = []
        by_title = {title: heading for title, heading in zip(section_titles, h2)}
        for required in REQUIRED_SECTION_TITLES:
            heading = by_title.get(required)
            if heading is None:
                raise PackageSynopsisError(f"required H2 section is missing: {required}")
            required_positions.append(section_titles.index(required))
            if not _visible_text("\n".join(heading.body_lines)):
                raise PackageSynopsisError(f"required H2 section is empty: {required}")
        if required_positions != sorted(required_positions):
            raise PackageSynopsisError("required H2 sections are out of order")

        summary = _visible_text("\n".join(by_title["Summary"].body_lines))
        _require_length(summary, "Summary", MAX_SUMMARY_CHARACTERS)
        return PackageSynopsis(
            description_title=description_title,
            summary=summary,
            section_titles=tuple(section_titles),
        )


def _parse_headings(markdown: str) -> list[_Heading]:
    lines = markdown.splitlines()
    headings: list[_Heading] = []
    active_h2: _Heading | None = None
    fence_character: str | None = None
    fence_length = 0
    index = 0

    while index < len(lines):
        line = lines[index]
        fence = _FENCE.match(line)
        if fence is not None:
            marker = fence.group(1)
            if fence_character is None:
                fence_character = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_character and len(marker) >= fence_length:
                fence_character = None
                fence_length = 0
            index += 1
            continue

        if fence_character is not None:
            if active_h2 is not None:
                active_h2.body_lines.append(line)
            index += 1
            continue

        level: int | None = None
        raw_title = ""
        atx = _ATX_HEADING.match(line)
        if atx is not None:
            level = len(atx.group(1))
            raw_title = re.sub(r"[ \t]+#+[ \t]*$", "", atx.group(2))
        elif index + 1 < len(lines) and line.strip():
            setext = _SETEXT_UNDERLINE.match(lines[index + 1])
            if setext is not None:
                level = 1 if setext.group(1).startswith("=") else 2
                raw_title = line
                index += 1

        if level is not None:
            heading = _Heading(level=level, title=raw_title, body_lines=[])
            headings.append(heading)
            if level == 2:
                active_h2 = heading
            elif level > 2 and active_h2 is not None:
                active_h2.body_lines.append(raw_title)
            index += 1
            continue

        if active_h2 is not None:
            active_h2.body_lines.append(line)
        index += 1

    return headings


def _visible_text(markdown_fragment: str) -> str:
    text = _INLINE_CODE.sub(lambda match: match.group(2), markdown_fragment)
    text = _INLINE_IMAGE.sub(lambda match: match.group(1), text)
    text = _INLINE_LINK.sub(lambda match: match.group(1), text)
    text = _REFERENCE_LINK.sub(lambda match: match.group(1), text)
    text = _AUTOLINK.sub(lambda match: match.group(1), text)
    text = _HTML_TAG.sub(" ", text)
    text = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~])", r"\1", text)
    text = re.sub(r"(?:\*\*|__|~~)", "", text)
    text = re.sub(r"(?<!\w)[*_]|[*_](?!\w)", "", text)
    text = "\n".join(_BLOCK_PREFIX.sub("", line) for line in text.splitlines())
    return _WHITESPACE.sub(" ", html.unescape(text)).strip()


def _require_length(value: str, label: str, maximum: int) -> None:
    if not value or len(value) > maximum:
        raise PackageSynopsisError(f"{label} must contain 1..{maximum} characters")


__all__ = [
    "MAX_DESCRIPTION_BYTES",
    "PackageSynopsis",
    "PackageSynopsisCompiler",
    "PackageSynopsisError",
    "REQUIRED_SECTION_TITLES",
]
