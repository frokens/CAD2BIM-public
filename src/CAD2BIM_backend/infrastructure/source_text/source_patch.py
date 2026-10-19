"""Fail-closed exact source patching for accepted source code.

Textum performs the character-level patch computation.  The Host owns every
editing invariant: unique literal matching, non-overlapping ranges, byte budgets,
and post-patch verification. The caller owns the actual base identity/CAS.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Sequence

from textum import Patch, PatchSet

from CAD2BIM_backend.domain.text_source import SourceEdit


MAX_EDIT_COUNT = 16
MAX_EDIT_TEXT_BYTES = 16 * 1024
MAX_AGGREGATE_EDIT_BYTES = 32 * 1024
MAX_SOURCE_BYTES = 64 * 1024


class SourcePatchFailure(StrEnum):
    """Small internal failure vocabulary for the application-layer mapper."""

    STALE_SOURCE = "stale_source"
    MATCH_NOT_FOUND = "match_not_found"
    MATCH_NOT_UNIQUE = "match_not_unique"
    OVERLAP = "overlap"
    BUDGET_EXCEEDED = "budget_exceeded"
    ENGINE_FAILED = "engine_failed"


class SourcePatchError(RuntimeError):
    """A bounded patch failure that never contains a private scratch path."""

    def __init__(self, failure: SourcePatchFailure, message: str, *, edit_index: int | None = None) -> None:
        super().__init__(message)
        self.failure = failure
        self.edit_index = edit_index


@dataclass(frozen=True, slots=True)
class _LocatedEdit:
    start: int
    end: int
    replacement: str
    edit: SourceEdit
    index: int


@dataclass(frozen=True, slots=True)
class _LinePatch:
    start_line: int
    end_line: int
    replacement: str


def apply_text_edits(
    source: str,
    edits: Sequence[SourceEdit],
    *,
    maximum_source_bytes: int,
) -> str:
    """Apply literal edits; the caller owns base identity and final content rules."""

    if not 1 <= len(edits) <= MAX_EDIT_COUNT:
        raise _budget_error(f"A repair must contain 1..{MAX_EDIT_COUNT} edits")

    aggregate_bytes = 0
    located: list[_LocatedEdit] = []
    for index, edit in enumerate(edits, 1):
        old_bytes = _encoded_size(edit.old_text)
        new_bytes = _encoded_size(edit.new_text)
        if not edit.old_text:
            raise _budget_error("Repair old_text must not be empty")
        if old_bytes > MAX_EDIT_TEXT_BYTES or new_bytes > MAX_EDIT_TEXT_BYTES:
            raise _budget_error("Each repair text is limited to 16,384 UTF-8 bytes")
        aggregate_bytes += old_bytes + new_bytes
        if aggregate_bytes > MAX_AGGREGATE_EDIT_BYTES:
            raise _budget_error("Repair edits exceed 32,768 aggregate UTF-8 bytes")
        try:
            start = _unique_literal_offset(source, edit.old_text)
        except SourcePatchError as exc:
            raise SourcePatchError(exc.failure, f"edit {index}: {exc}", edit_index=index) from None
        located.append(
            _LocatedEdit(
                start=start,
                end=start + len(edit.old_text),
                replacement=edit.new_text,
                edit=edit,
                index=index,
            )
        )

    _reject_overlaps(located)
    patched = _apply_with_textum(source, located)
    expected = _apply_oracle(source, located)
    if patched != expected:
        raise SourcePatchError(
            SourcePatchFailure.ENGINE_FAILED,
            "The text patch engine returned an unexpected result",
        )
    if not patched:
        raise _budget_error("Repaired source must not be empty")
    if _encoded_size(patched) > maximum_source_bytes:
        raise _budget_error(f"Repaired source exceeds {maximum_source_bytes:,} UTF-8 bytes")
    return patched


def _unique_literal_offset(source: str, needle: str) -> int:
    """Find one literal occurrence, including matches that overlap each other."""

    first = source.find(needle)
    if first < 0:
        raise SourcePatchError(
            SourcePatchFailure.MATCH_NOT_FOUND,
            "Repair old_text was not found in the source",
        )
    if source.find(needle, first + 1) >= 0:
        raise SourcePatchError(
            SourcePatchFailure.MATCH_NOT_UNIQUE,
            "Repair old_text occurs more than once in the source",
        )
    return first


def _reject_overlaps(edits: Sequence[_LocatedEdit]) -> None:
    ordered = sorted(edits, key=lambda item: (item.start, item.end))
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.start < previous.end:
            raise SourcePatchError(
                SourcePatchFailure.OVERLAP,
                f"edit {current.index}: overlaps edit {previous.index} in the original text",
                edit_index=current.index,
            )


def _apply_with_textum(source: str, edits: Sequence[_LocatedEdit]) -> str:
    try:
        with tempfile.TemporaryDirectory(prefix="cad2bim-source-patch-") as temporary:
            path = Path(temporary) / "source.txt"
            # Textum line ranges require an addressable boundary after the
            # final line.  Add one private sentinel newline when the exact
            # Artifact omits it, then remove only that sentinel afterwards.
            has_sentinel = not source.endswith("\n")
            engine_source = source + "\n" if has_sentinel else source
            original = engine_source.encode("utf-8")
            path.write_bytes(original)
            patch_set = PatchSet()
            # Textum 0.3.0's literal target helper cannot reliably locate a
            # multi-line literal even after the Host has proved it unique.
            # Convert the already validated character edits to local line
            # ranges instead.  Edits touching the same line range are merged
            # so PatchSet still receives non-overlapping patches.
            for item in _compile_line_patches(engine_source, edits):
                patch_set.add(
                    Patch.from_line_range(
                        file=str(path),
                        start_line=item.start_line,
                        end_line=item.end_line,
                        replacement=item.replacement,
                    )
                )
            results = patch_set.apply_to_files()
            if path.read_bytes() != original or set(results) != {str(path)}:
                raise RuntimeError("patch engine violated the private scratch contract")
            patched = results[str(path)]
            if not isinstance(patched, str):
                raise RuntimeError("patch engine returned non-text output")
            if has_sentinel:
                if not patched.endswith("\n"):
                    raise RuntimeError("patch engine lost the private line sentinel")
                patched = patched[:-1]
            return patched
    except SourcePatchError:
        raise
    except Exception as exc:
        raise SourcePatchError(
            SourcePatchFailure.ENGINE_FAILED,
            "The text patch engine failed",
        ) from exc


def _compile_line_patches(
    source: str,
    edits: Sequence[_LocatedEdit],
) -> tuple[_LinePatch, ...]:
    """Compile exact character edits into non-overlapping Textum line ranges."""

    grouped: list[tuple[int, int, list[_LocatedEdit]]] = []
    for edit in sorted(edits, key=lambda item: (item.start, item.end)):
        line_start = source.rfind("\n", 0, edit.start) + 1
        next_newline = source.find("\n", edit.end)
        line_end = len(source) if next_newline < 0 else next_newline + 1
        if grouped and line_start < grouped[-1][1]:
            previous_start, previous_end, previous_edits = grouped[-1]
            grouped[-1] = (
                previous_start,
                max(previous_end, line_end),
                [*previous_edits, edit],
            )
        else:
            grouped.append((line_start, line_end, [edit]))

    patches: list[_LinePatch] = []
    for start, end, group_edits in grouped:
        replacement = source[start:end]
        for edit in sorted(group_edits, key=lambda item: item.start, reverse=True):
            relative_start = edit.start - start
            relative_end = edit.end - start
            replacement = (
                replacement[:relative_start]
                + edit.replacement
                + replacement[relative_end:]
            )
        original_segment = source[start:end]
        start_line = source.count("\n", 0, start)
        line_count = original_segment.count("\n")
        if not original_segment.endswith("\n"):
            line_count += 1
        patches.append(
            _LinePatch(
                start_line=start_line,
                end_line=start_line + line_count,
                replacement=replacement,
            )
        )
    return tuple(patches)


def _apply_oracle(source: str, edits: Sequence[_LocatedEdit]) -> str:
    result = source
    for item in sorted(edits, key=lambda value: value.start, reverse=True):
        result = result[: item.start] + item.replacement + result[item.end :]
    return result


def _encoded_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _budget_error("Repair text must be valid UTF-8") from exc


def _budget_error(message: str) -> SourcePatchError:
    return SourcePatchError(SourcePatchFailure.BUDGET_EXCEEDED, message)


__all__ = [
    "MAX_AGGREGATE_EDIT_BYTES",
    "MAX_EDIT_COUNT",
    "MAX_EDIT_TEXT_BYTES",
    "MAX_SOURCE_BYTES",
    "SourcePatchFailure",
    "SourceEdit",
    "SourcePatchError",
    "apply_text_edits",
]
