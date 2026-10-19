from __future__ import annotations

import pytest

from CAD2BIM_backend.domain.text_source import SourceEdit
from CAD2BIM_backend.infrastructure.source_text import (
    SourceBundleAddMember,
    SourceBundleDeleteMember,
    SourceBundleFilePatch,
    SourceBundleLiteralEdit,
    SourceBundleReplaceMember,
    SourcePatchError,
    SourcePatchFailure,
    apply_python_source_bundle_changes,
    apply_source_bundle_patch,
    inspect_source_bundle,
    pack_source_bundle,
)
from CAD2BIM_backend.infrastructure.source_text.geometry_authoring import (
    SourceTextGeometryAuthoringSource,
)
from CAD2BIM_backend.ports.geometry_authoring import (
    GeometryAuthoringSourceError,
    GeometryAuthoringSourceFailure,
)


_MEMBER_BUDGET = 65_536
_TOTAL_BUDGET = 262_144
_MEMBER_COUNT_BUDGET = 64


def test_bundle_patch_uses_whole_bundle_cas_and_preserves_untouched_members() -> None:
    original = pack_source_bundle(
        {
            "main.py": b"from .shape import make\n\ndef build(ctx):\n    return make(1.0)\n",
            "shape.py": b"def make(length):\n    return length\n",
        }
    )
    exact = inspect_source_bundle(original)

    patched = apply_source_bundle_patch(
        original,
        exact.sha256,
        (
            SourceBundleFilePatch(
                path="shape.py",
                edits=(SourceEdit(old_text="return length", new_text="return length * 2"),),
            ),
        ),
    )

    result = inspect_source_bundle(patched.content)
    assert patched.sha256 == result.sha256
    assert result.read("main.py") == exact.read("main.py")
    assert result.read_text("shape.py") == "def make(length):\n    return length * 2\n"


def test_bundle_patch_rejects_stale_sha_and_cannot_add_delete_or_patch_binary() -> None:
    original = pack_source_bundle({"main.py": b"old\n", "raw.bin": b"\x00\xff"})
    exact = inspect_source_bundle(original)
    edit = (SourceEdit(old_text="old", new_text="new"),)

    with pytest.raises(SourcePatchError) as stale:
        apply_source_bundle_patch(
            original,
            "0" * 64,
            (SourceBundleFilePatch(path="main.py", edits=edit),),
        )
    assert stale.value.failure is SourcePatchFailure.STALE_SOURCE

    with pytest.raises(ValueError, match="existing"):
        apply_source_bundle_patch(
            original,
            exact.sha256,
            (SourceBundleFilePatch(path="added.py", edits=edit),),
        )
    with pytest.raises(ValueError, match="UTF-8 text"):
        apply_source_bundle_patch(
            original,
            exact.sha256,
            (SourceBundleFilePatch(path="raw.bin", edits=edit),),
        )


def test_bundle_patch_rejects_duplicate_paths_and_aggregate_edit_overflow() -> None:
    original = pack_source_bundle({"main.py": b"old\n"})
    exact = inspect_source_bundle(original)
    patch = SourceBundleFilePatch(
        path="main.py",
        edits=(SourceEdit(old_text="old", new_text="new"),),
    )
    with pytest.raises(ValueError, match="unique"):
        apply_source_bundle_patch(original, exact.sha256, (patch, patch))

    edits = tuple(SourceEdit(old_text=str(index), new_text=f"x{index}") for index in range(17))
    content = pack_source_bundle(
        {"main.py": " ".join(str(index) for index in range(17)).encode("utf-8")}
    )
    with pytest.raises(ValueError, match="at most 16 edits"):
        apply_source_bundle_patch(
            content,
            inspect_source_bundle(content).sha256,
            (SourceBundleFilePatch(path="main.py", edits=edits),),
        )


def test_python_bundle_changes_atomically_edit_add_replace_delete_and_pack_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = pack_source_bundle(
        {
            "main.py": b"from .solver import solve\n\ndef realize(ctx):\n    return solve(1.0)\n",
            "obsolete.py": b"VALUE = 'obsolete'\n",
            "solver.py": b"def solve(value):\n    return value\n",
        }
    )
    exact = inspect_source_bundle(original)

    import CAD2BIM_backend.infrastructure.source_text.source_bundle_patch as mutation_module

    original_pack = mutation_module.pack_source_bundle
    pack_calls = 0

    def counting_pack(members):
        nonlocal pack_calls
        pack_calls += 1
        return original_pack(members)

    monkeypatch.setattr(mutation_module, "pack_source_bundle", counting_pack)
    patched = apply_python_source_bundle_changes(
        original,
        exact.sha256,
        (
            SourceBundleLiteralEdit(
                path="main.py",
                edits=(SourceEdit(old_text="solve(1.0)", new_text="solve(2.0)"),),
            ),
            SourceBundleAddMember(path="shared/numeric.py", source="EPSILON = 1e-9\n"),
            SourceBundleReplaceMember(
                path="solver.py",
                source="def solve(value):\n    return value * 2\n",
            ),
            SourceBundleDeleteMember(
                path="obsolete.py",
            ),
        ),
        maximum_members=_MEMBER_COUNT_BUDGET,
        maximum_member_bytes=_MEMBER_BUDGET,
        maximum_total_bytes=_TOTAL_BUDGET,
    )

    result = inspect_source_bundle(patched.content)
    assert pack_calls == 1
    assert patched.sha256 == result.sha256
    assert tuple(member.path for member in result.members) == (
        "main.py",
        "shared/numeric.py",
        "solver.py",
    )
    assert "solve(2.0)" in result.read_text("main.py")
    assert result.read_text("shared/numeric.py") == "EPSILON = 1e-9\n"
    assert "value * 2" in result.read_text("solver.py")
    unchanged = inspect_source_bundle(original)
    assert "solve(1.0)" in unchanged.read_text("main.py")
    assert "obsolete" in unchanged.read_text("obsolete.py")


def test_python_bundle_changes_reject_stale_bundle_without_packing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = pack_source_bundle({"main.py": b"def realize(ctx):\n    return 1\n"})

    import CAD2BIM_backend.infrastructure.source_text.source_bundle_patch as mutation_module

    def fail_pack(_members):
        raise AssertionError("failed mutation must not pack a candidate")

    monkeypatch.setattr(mutation_module, "pack_source_bundle", fail_pack)
    with pytest.raises(SourcePatchError) as stale_bundle:
        apply_python_source_bundle_changes(
            original,
            "0" * 64,
            (SourceBundleAddMember(path="helper.py", source="VALUE = 1\n"),),
            maximum_member_bytes=_MEMBER_BUDGET,
            maximum_total_bytes=_TOTAL_BUDGET,
        )
    assert stale_bundle.value.failure is SourcePatchFailure.STALE_SOURCE

    assert tuple(member.path for member in inspect_source_bundle(original).members) == ("main.py",)


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        (
            (
                SourceBundleAddMember(path="helper.py", source="VALUE = 1\n"),
                SourceBundleReplaceMember(
                    path="helper.py",
                    source="VALUE = 2\n",
                ),
            ),
            "target paths must be unique",
        ),
        (
            (SourceBundleAddMember(path="main.py", source="VALUE = 1\n"),),
            "already exists",
        ),
        (
            (
                SourceBundleReplaceMember(
                    path="missing.py",
                    source="VALUE = 1\n",
                ),
            ),
            "does not exist",
        ),
        (
            (
                SourceBundleDeleteMember(
                    path="missing.py",
                ),
            ),
            "does not exist",
        ),
        (
            (SourceBundleAddMember(path="../escape.py", source="VALUE = 1\n"),),
            "normalized relative POSIX",
        ),
        (
            (SourceBundleAddMember(path="notes.txt", source="text\n"),),
            "must use .py paths",
        ),
        (
            (SourceBundleAddMember(path="empty.py", source=""),),
            "nonempty UTF-8 text",
        ),
        (
            (SourceBundleAddMember(path="binary.py", source="\x00"),),
            "nonempty UTF-8 text",
        ),
    ),
)
def test_python_bundle_changes_reject_invalid_or_conflicting_targets(
    changes,
    message: str,
) -> None:
    original = pack_source_bundle({"main.py": b"def realize(ctx):\n    return 1\n"})
    with pytest.raises(ValueError, match=message):
        apply_python_source_bundle_changes(
            original,
            inspect_source_bundle(original).sha256,
            changes,
            maximum_member_bytes=_MEMBER_BUDGET,
            maximum_total_bytes=_TOTAL_BUDGET,
        )


def test_python_bundle_changes_require_closed_nonempty_python_bundle() -> None:
    original = pack_source_bundle(
        {
            "main.py": b"def realize(ctx):\n    return 1\n",
            "helper.py": b"VALUE = 1\n",
        }
    )
    exact = inspect_source_bundle(original)

    with pytest.raises(ValueError, match="retain exactly one main.py"):
        apply_python_source_bundle_changes(
            original,
            exact.sha256,
            (SourceBundleDeleteMember(path="main.py"),),
            maximum_member_bytes=_MEMBER_BUDGET,
            maximum_total_bytes=_TOTAL_BUDGET,
        )

    binary = pack_source_bundle(
        {"main.py": b"def realize(ctx):\n    return 1\n", "payload.py": b"\x00\xff"}
    )
    with pytest.raises(ValueError, match="UTF-8 text"):
        apply_python_source_bundle_changes(
            binary,
            inspect_source_bundle(binary).sha256,
            (SourceBundleAddMember(path="helper.py", source="VALUE = 1\n"),),
            maximum_member_bytes=_MEMBER_BUDGET,
            maximum_total_bytes=_TOTAL_BUDGET,
        )

    non_python = pack_source_bundle(
        {"main.py": b"def realize(ctx):\n    return 1\n", "README.md": b"text\n"}
    )
    with pytest.raises(ValueError, match="must use .py paths"):
        apply_python_source_bundle_changes(
            non_python,
            inspect_source_bundle(non_python).sha256,
            (SourceBundleAddMember(path="helper.py", source="VALUE = 1\n"),),
            maximum_member_bytes=_MEMBER_BUDGET,
            maximum_total_bytes=_TOTAL_BUDGET,
        )


def test_python_bundle_changes_enforce_member_and_total_policy_budgets() -> None:
    original = pack_source_bundle({"main.py": b"1234"})
    exact = inspect_source_bundle(original)

    with pytest.raises(ValueError, match="member exceeds"):
        apply_python_source_bundle_changes(
            original,
            exact.sha256,
            (SourceBundleAddMember(path="helper.py", source="1"),),
            maximum_member_bytes=3,
            maximum_total_bytes=10,
        )

    with pytest.raises(ValueError, match="total byte budget"):
        apply_python_source_bundle_changes(
            original,
            exact.sha256,
            (SourceBundleAddMember(path="helper.py", source="56"),),
            maximum_member_bytes=4,
            maximum_total_bytes=5,
        )

    with pytest.raises(ValueError, match="member-count"):
        apply_python_source_bundle_changes(
            original,
            exact.sha256,
            (SourceBundleAddMember(path="helper.py", source="1"),),
            maximum_members=1,
            maximum_member_bytes=4,
            maximum_total_bytes=10,
        )

    with pytest.raises(ValueError, match="1..64 changes"):
        apply_python_source_bundle_changes(
            original,
            exact.sha256,
            (),
            maximum_member_bytes=_MEMBER_BUDGET,
            maximum_total_bytes=_TOTAL_BUDGET,
        )


def test_python_bundle_literal_changes_preserve_exact_edit_failures() -> None:
    original = pack_source_bundle(
        {"main.py": b"def realize(ctx):\n    value = 1\n    value = 1\n    return value\n"}
    )
    exact = inspect_source_bundle(original)

    with pytest.raises(SourcePatchError) as multiple:
        apply_python_source_bundle_changes(
            original,
            exact.sha256,
            (
                SourceBundleLiteralEdit(
                    path="main.py",
                    edits=(SourceEdit(old_text="value = 1", new_text="value = 2"),),
                ),
            ),
            maximum_member_bytes=_MEMBER_BUDGET,
            maximum_total_bytes=_TOTAL_BUDGET,
        )
    assert multiple.value.failure is SourcePatchFailure.MATCH_NOT_UNIQUE

    with pytest.raises(SourcePatchError) as missing:
        apply_python_source_bundle_changes(
            original,
            exact.sha256,
            (
                SourceBundleLiteralEdit(
                    path="main.py",
                    edits=(SourceEdit(old_text="missing", new_text="replacement"),),
                ),
            ),
            maximum_member_bytes=_MEMBER_BUDGET,
            maximum_total_bytes=_TOTAL_BUDGET,
        )
    assert missing.value.failure is SourcePatchFailure.MATCH_NOT_FOUND

    overlapping = pack_source_bundle({"main.py": b"abcdef"})
    with pytest.raises(SourcePatchError) as overlap:
        apply_python_source_bundle_changes(
            overlapping,
            inspect_source_bundle(overlapping).sha256,
            (
                SourceBundleLiteralEdit(
                    path="main.py",
                    edits=(
                        SourceEdit(old_text="abc", new_text="A"),
                        SourceEdit(old_text="cde", new_text="B"),
                    ),
                ),
            ),
            maximum_member_bytes=_MEMBER_BUDGET,
            maximum_total_bytes=_TOTAL_BUDGET,
        )
    assert overlap.value.failure is SourcePatchFailure.OVERLAP


def test_geometry_authoring_source_mutate_exposes_neutral_changes_and_safe_failures() -> None:
    source = SourceTextGeometryAuthoringSource()
    original = pack_source_bundle({"main.py": b"def realize(ctx):\n    return 1\n"})
    exact = inspect_source_bundle(original)

    mutated = source.mutate(
        original,
        exact.sha256,
        (SourceBundleAddMember(path="helper.py", source="VALUE = 2\n"),),
        maximum_members=2,
        maximum_member_bytes=_MEMBER_BUDGET,
        maximum_total_bytes=_TOTAL_BUDGET,
    )
    inspected = inspect_source_bundle(mutated.content)
    assert mutated.sha256 == inspected.sha256
    assert inspected.read_text("helper.py") == "VALUE = 2\n"

    with pytest.raises(GeometryAuthoringSourceError) as stale:
        source.mutate(
            original,
            "0" * 64,
            (SourceBundleAddMember(path="helper.py", source="VALUE = 2\n"),),
            maximum_members=2,
            maximum_member_bytes=_MEMBER_BUDGET,
            maximum_total_bytes=_TOTAL_BUDGET,
        )
    assert stale.value.failure is GeometryAuthoringSourceFailure.STALE_SOURCE
