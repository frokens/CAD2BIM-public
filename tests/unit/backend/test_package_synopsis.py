from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from CAD2BIM_backend.application.package_synopsis import (
    MAX_DESCRIPTION_BYTES,
    PackageSynopsisCompiler,
    PackageSynopsisError,
)


def _description(
    *,
    title: str = "Gas topology candidates",
    summary: str = "Builds **candidate connections** from [exact L0 facts](https://example.test).",
    extra: str = "",
) -> str:
    return f"""# {title}
## Summary
{summary}
## Intended use
Support later topology review.
{extra}## Contents
One candidate table.
## Method
Deterministic SQL over bound payloads.
## Scope and completeness
All selected source rows are represented.
## Known limitations
Candidates are not approved IFC connections.
"""


def test_compiles_visible_text_and_ordered_section_directory() -> None:
    result = PackageSynopsisCompiler().compile(
        _description(extra="## Parameters\nUses `snap_distance_m`.\n")
    )

    assert result.description_title == "Gas topology candidates"
    assert result.summary == "Builds candidate connections from exact L0 facts."
    assert result.section_titles == (
        "Summary",
        "Intended use",
        "Parameters",
        "Contents",
        "Method",
        "Scope and completeness",
        "Known limitations",
    )

    with pytest.raises(FrozenInstanceError):
        result.summary = "changed"  # type: ignore[misc]


def test_setext_headings_are_supported_and_fenced_headings_are_not_structural() -> None:
    markdown = (
        _description()
        .replace(
            "# Gas topology candidates",
            "Gas topology candidates\n=======================",
        )
        .replace(
            "## Summary",
            "Summary\n-------",
        )
        .replace(
            "Support later topology review.",
            "```markdown\n## Not a section\n```\nSupport later topology review.",
        )
    )

    result = PackageSynopsisCompiler().compile(markdown)

    assert result.description_title == "Gas topology candidates"
    assert "Not a section" not in result.section_titles


def test_summary_includes_visible_nested_heading_text() -> None:
    result = PackageSynopsisCompiler().compile(
        _description(summary="### Decision\nUse exact source facts.")
    )

    assert result.summary == "Decision Use exact source facts."


@pytest.mark.parametrize(
    ("markdown", "message"),
    [
        (_description().replace("# Gas topology candidates\n", ""), "exactly one H1"),
        (_description() + "\n# Another title\n", "exactly one H1"),
        (_description().replace("# Gas topology candidates", "#   "), "H1 must contain"),
        (_description().replace("## Contents\nOne candidate table.\n", ""), "is missing: Contents"),
        (
            _description().replace("## Method", "## Contents\nDuplicate.\n## Method"),
            "duplicate H2 section: Contents",
        ),
        (
            _description().replace("## Contents\nOne candidate table.\n", "## Contents\n"),
            "is empty: Contents",
        ),
        (
            _description()
            .replace(
                "## Intended use\nSupport later topology review.\n",
                "",
            )
            .replace(
                "## Contents\nOne candidate table.\n",
                "## Contents\nOne candidate table.\n## Intended use\nSupport later topology review.\n",
            ),
            "out of order",
        ),
        ("## Extra\nBody.\n" + _description(), "Summary must be the first H2"),
        (
            _description().replace("## Method", "##   \nIgnored body.\n## Method"),
            "H2 title must contain",
        ),
        (_description().replace("## Summary", "## Summary\x00"), "must not contain NUL"),
    ],
)
def test_rejects_invalid_required_structure(markdown: str, message: str) -> None:
    with pytest.raises(PackageSynopsisError, match=message):
        PackageSynopsisCompiler().compile(markdown)


def test_rejects_summary_over_1000_characters_without_truncation() -> None:
    with pytest.raises(PackageSynopsisError, match="Summary must contain 1..1000"):
        PackageSynopsisCompiler().compile(_description(summary="x" * 1_001))


def test_rejects_title_and_section_title_over_character_limits() -> None:
    with pytest.raises(PackageSynopsisError, match="H1 must contain 1..300"):
        PackageSynopsisCompiler().compile(_description(title="x" * 301))

    markdown = _description(extra=f"## {'x' * 161}\nOptional body.\n")
    with pytest.raises(PackageSynopsisError, match="H2 title must contain 1..160"):
        PackageSynopsisCompiler().compile(markdown)


def test_rejects_more_than_16_h2_sections() -> None:
    extras = "".join(f"## Extra {index}\nBody.\n" for index in range(11))
    with pytest.raises(PackageSynopsisError, match="1..16 H2 sections"):
        PackageSynopsisCompiler().compile(_description(extra=extras))


def test_description_limit_is_utf8_bytes_not_characters() -> None:
    oversized = _description() + ("\u754c" * (MAX_DESCRIPTION_BYTES // 3))
    assert len(oversized) < MAX_DESCRIPTION_BYTES

    with pytest.raises(PackageSynopsisError, match="UTF-8 bytes"):
        PackageSynopsisCompiler().compile(oversized)


def test_exact_65536_byte_description_is_accepted() -> None:
    base = _description()
    markdown = base + (" " * (MAX_DESCRIPTION_BYTES - len(base.encode("utf-8"))))

    result = PackageSynopsisCompiler().compile(markdown)

    assert result.description_title == "Gas topology candidates"
