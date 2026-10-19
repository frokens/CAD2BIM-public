from __future__ import annotations

import importlib.metadata

import pytest

from CAD2BIM_backend.infrastructure.source_text import source_patch
from CAD2BIM_backend.infrastructure.source_text.source_patch import (
    MAX_EDIT_COUNT,
    MAX_EDIT_TEXT_BYTES,
    MAX_SOURCE_BYTES,
    SourcePatchFailure,
    SourceEdit,
    SourcePatchError,
    apply_text_edits,
)


def _edit(old: str, new: str) -> SourceEdit:
    return SourceEdit(old_text=old, new_text=new)


def _rejected(
    source: str,
    edits: list[SourceEdit],
    expected: SourcePatchFailure,
) -> SourcePatchError:
    with pytest.raises(SourcePatchError) as caught:
        apply_text_edits(source, edits, maximum_source_bytes=MAX_SOURCE_BYTES)
    assert caught.value.failure is expected
    return caught.value


def test_textum_dependency_is_the_locked_runtime_version() -> None:
    assert importlib.metadata.version("textum") == "0.3.0"


def test_patch_preserves_unicode_tabs_crlf_and_missing_final_newline() -> None:
    original = "def transform(ctx):\r\n\tvalue = '旧值'\r\n\tctx.log(value)"

    result = apply_text_edits(original, [_edit("旧值", "新值"), _edit("ctx.log(value)", "ctx.log(value + '!')")], maximum_source_bytes=MAX_SOURCE_BYTES)

    assert result == "def transform(ctx):\r\n\tvalue = '新值'\r\n\tctx.log(value + '!')"
    assert not result.endswith("\n")


def test_patch_replaces_a_realistic_multiline_python_block() -> None:
    old = '''        add_frame(key,x,y,z,r["host_tangent_x"],r["host_tangent_y"],r["host_tangent_z"],0.0,0.0,1.0)
        node=target=r["target_key"] if r["target_kind"]=="quantized_endpoint" else "cut_"+r["host_atomic_edge_key"]+"_"+str(round(float(r["host_parameter"]),12))
        p0=add_port(key,1,"SIDE_A",x,y,z,-r["host_tangent_x"],-r["host_tangent_y"],-r["host_tangent_z"],od,node)
        p1=add_port(key,2,"SIDE_B",x,y,z,r["host_tangent_x"],r["host_tangent_y"],r["host_tangent_z"],od,node)'''
    new = '''        he=edge[r["host_atomic_edge_key"]]
        htx,hty,htz=he["unit_x"],he["unit_y"],he["unit_z"]
        add_frame(key,x,y,z,htx,hty,htz,0.0,0.0,1.0)
        node=target=r["target_key"] if r["target_kind"]=="quantized_endpoint" else "cut_"+r["host_atomic_edge_key"]+"_"+str(round(float(r["host_parameter"]),12))
        p0=add_port(key,1,"SIDE_A",x,y,z,-htx,-hty,-htz,od,node)
        p1=add_port(key,2,"SIDE_B",x,y,z,htx,hty,htz,od,node)'''
    source = "def build():\n" + old + "\n    return p0, p1\n"

    result = apply_text_edits(source, [_edit(old, new)], maximum_source_bytes=MAX_SOURCE_BYTES)

    assert result == "def build():\n" + new + "\n    return p0, p1\n"


def test_patch_merges_multiple_edits_on_the_same_line() -> None:
    source = "before OLD and LATER after\n"

    result = apply_text_edits(source, [_edit("OLD", "first"), _edit("LATER", "second")], maximum_source_bytes=MAX_SOURCE_BYTES)

    assert result == "before first and second after\n"


def test_patch_is_independent_of_edit_submission_order() -> None:
    source = "before OLD middle LATER after\n"
    forward = [_edit("OLD", "expanded"), _edit("LATER", "x")]

    first = apply_text_edits(source, forward, maximum_source_bytes=MAX_SOURCE_BYTES)
    second = apply_text_edits(source, list(reversed(forward)), maximum_source_bytes=MAX_SOURCE_BYTES)

    assert first == second
    assert first == "before expanded middle x after\n"


def test_patch_allows_exact_deletion_but_not_an_empty_final_source() -> None:
    source = "head\nREMOVE\ntail"
    result = apply_text_edits(source, [_edit("REMOVE\n", "")], maximum_source_bytes=MAX_SOURCE_BYTES)
    assert result == "head\ntail"

    rejected = _rejected("all", [_edit("all", "")], SourcePatchFailure.BUDGET_EXCEEDED)
    assert "must not be empty" in str(rejected)


@pytest.mark.parametrize(
    ("source", "old_text", "failure"),
    [
        ("abc", "missing", SourcePatchFailure.MATCH_NOT_FOUND),
        ("old x old", "old", SourcePatchFailure.MATCH_NOT_UNIQUE),
        ("aaa", "aa", SourcePatchFailure.MATCH_NOT_UNIQUE),
    ],
)
def test_patch_rejects_zero_repeated_and_overlapping_literal_matches(
    source: str,
    old_text: str,
    failure: SourcePatchFailure,
) -> None:
    _rejected(source, [_edit(old_text, "new")], failure)


def test_patch_rejects_overlapping_edit_ranges_from_the_original_source() -> None:
    _rejected(
        "abcdef",
        [_edit("abcd", "x"), _edit("cdef", "y")],
        SourcePatchFailure.OVERLAP,
    )


@pytest.mark.parametrize(
    "edits",
    [
        [],
        [_edit(str(index), "x") for index in range(MAX_EDIT_COUNT + 1)],
        [_edit("x" * (MAX_EDIT_TEXT_BYTES + 1), "y")],
        [_edit("x", "y" * (MAX_EDIT_TEXT_BYTES + 1))],
        [
            _edit("first", "x" * MAX_EDIT_TEXT_BYTES),
            _edit("second", "y" * MAX_EDIT_TEXT_BYTES),
        ],
    ],
)
def test_patch_rejects_edit_count_and_text_budgets(edits: list[SourceEdit]) -> None:
    source = " ".join(item.old_text for item in edits) or "source"
    _rejected(source, edits, SourcePatchFailure.BUDGET_EXCEEDED)


def test_patch_rejects_a_final_source_over_sixty_four_kib() -> None:
    source = ("a" * (MAX_SOURCE_BYTES - 1)) + "x"
    _rejected(
        source,
        [_edit("x", "xx")],
        SourcePatchFailure.BUDGET_EXCEEDED,
    )


def test_text_edit_uses_own_content_budget_without_source_digest() -> None:
    from CAD2BIM_backend.infrastructure.source_text.source_patch_adapter import TextSourcePatchAdapter
    from CAD2BIM_backend.ports.source_patch import SourcePatchPortError

    original = "a" * (96 * 1024) + " unique end"
    adapter = TextSourcePatchAdapter()
    edits = [_edit("unique end", "changed end")]
    result = adapter.apply_text(original, edits, maximum_source_bytes=128 * 1024)
    assert result == "a" * (96 * 1024) + " changed end"
    with pytest.raises(SourcePatchPortError, match="65,536"):
        adapter.apply_text(original, edits, maximum_source_bytes=64 * 1024)


@pytest.mark.parametrize("source,edits,index", [
    ("alpha beta", [_edit("alpha", "x"), _edit("absent", "y")], 2),
    ("alpha alpha", [_edit("alpha", "x")], 1),
    ("abcdef", [_edit("abcd", "x"), _edit("cdef", "y")], 2),
])
def test_text_edit_failure_identifies_input_edit(source, edits, index) -> None:
    from CAD2BIM_backend.infrastructure.source_text.source_patch_adapter import TextSourcePatchAdapter
    from CAD2BIM_backend.ports.source_patch import SourcePatchPortError

    with pytest.raises(SourcePatchPortError) as failed:
        TextSourcePatchAdapter().apply_text(source, edits, maximum_source_bytes=32_768)
    assert failed.value.edit_index == index
    assert f"edit {index}" in str(failed.value)


def test_patch_maps_engine_exceptions_without_leaking_private_paths(monkeypatch) -> None:
    class FailingPatchSet:
        def add(self, patch) -> None:
            pass

        def apply_to_files(self):
            raise RuntimeError("private path /tmp/secret/source.py")

    monkeypatch.setattr(source_patch, "PatchSet", FailingPatchSet)
    rejected = _rejected("old", [_edit("old", "new")], SourcePatchFailure.ENGINE_FAILED)

    assert str(rejected) == "The text patch engine failed"
    assert "/tmp" not in str(rejected)
