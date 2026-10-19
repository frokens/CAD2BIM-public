"""Whole-bundle CAS mutation for exact source bundles."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from CAD2BIM_backend.domain.text_source import (
    SourceBundleAddMember,
    SourceBundleChange,
    SourceBundleDeleteMember,
    SourceBundleLiteralEdit,
    SourceBundleReplaceMember,
    SourceEdit,
)
from CAD2BIM_backend.infrastructure.source_text.source_bundle import (
    MAX_SOURCE_BUNDLE_MEMBERS,
    MAX_SOURCE_BUNDLE_TOTAL_BYTES,
    SourceBundleError,
    SourceBundleMemberNotFoundError,
    SourceBundleMemberNotTextError,
    inspect_source_bundle,
    normalize_source_bundle_path,
    pack_source_bundle,
)
from CAD2BIM_backend.infrastructure.source_text.source_patch import (
    MAX_AGGREGATE_EDIT_BYTES,
    MAX_EDIT_COUNT,
    MAX_SOURCE_BYTES,
    SourcePatchError,
    SourcePatchFailure,
    apply_text_edits,
)


MAX_BUNDLE_PATCH_FILES = 16
MAX_SOURCE_BUNDLE_CHANGES = MAX_SOURCE_BUNDLE_MEMBERS


@dataclass(frozen=True, slots=True)
class SourceBundleFilePatch:
    path: str
    edits: tuple[SourceEdit, ...]


@dataclass(frozen=True, slots=True)
class PatchedSourceBundle:
    content: bytes
    sha256: str


def apply_python_source_bundle_changes(
    content: bytes,
    expected_sha256: str,
    changes: tuple[SourceBundleChange, ...],
    *,
    maximum_member_bytes: int,
    maximum_total_bytes: int,
    maximum_members: int = MAX_SOURCE_BUNDLE_MEMBERS,
) -> PatchedSourceBundle:
    """Atomically mutate one exact all-Python bundle and pack the result once.

    Policy-owned byte limits are supplied by the caller so this neutral mechanism
    does not become a second budget authority for Geometry or ProductFamily.
    """

    _validate_mutation_budget(
        maximum_members=maximum_members,
        maximum_member_bytes=maximum_member_bytes,
        maximum_total_bytes=maximum_total_bytes,
    )
    bundle = inspect_source_bundle(content)
    if bundle.sha256 != expected_sha256:
        raise SourcePatchError(
            SourcePatchFailure.STALE_SOURCE,
            "The source bundle has changed since it was inspected",
        )
    if not 1 <= len(changes) <= MAX_SOURCE_BUNDLE_CHANGES:
        raise SourceBundleError(
            f"a source bundle mutation must contain 1..{MAX_SOURCE_BUNDLE_CHANGES} changes"
        )

    paths = tuple(_require_python_path(change.path) for change in changes)
    if len(paths) != len(set(paths)):
        raise SourceBundleError("source bundle mutation target paths must be unique")
    _validate_literal_edit_budget(changes)

    members: dict[str, bytes] = {}
    for member in bundle.members:
        path = _require_python_path(member.path)
        source = _read_python_member(bundle, path)
        encoded = _encode_nonempty_source(source, path=path)
        _require_member_budget(encoded, maximum_member_bytes=maximum_member_bytes)
        members[path] = encoded
    _require_member_count(members, maximum_members=maximum_members)
    _require_total_budget(members, maximum_total_bytes=maximum_total_bytes)

    for change in changes:
        path = change.path
        if isinstance(change, SourceBundleAddMember):
            if path in members:
                raise SourceBundleError("source bundle add target already exists")
            members[path] = _encode_nonempty_source(change.source, path=path)
        elif isinstance(change, SourceBundleReplaceMember):
            _require_existing_member(path, members)
            members[path] = _encode_nonempty_source(change.source, path=path)
        elif isinstance(change, SourceBundleDeleteMember):
            _require_existing_member(path, members)
            del members[path]
        elif isinstance(change, SourceBundleLiteralEdit):
            _require_existing_member(path, members)
            source = members[path].decode("utf-8")
            patched = apply_text_edits(source, change.edits, maximum_source_bytes=MAX_SOURCE_BYTES)
            members[path] = patched.encode("utf-8")
        else:  # pragma: no cover - closed local union
            raise TypeError("unsupported source bundle change")

    entry = members.get("main.py")
    if entry is None:
        raise SourceBundleError("source bundle mutation must retain exactly one main.py")
    _decode_nonempty_source(entry, path="main.py")
    for path, member_content in members.items():
        _require_member_budget(member_content, maximum_member_bytes=maximum_member_bytes)
        _decode_nonempty_source(member_content, path=path)
    _require_member_count(members, maximum_members=maximum_members)
    _require_total_budget(members, maximum_total_bytes=maximum_total_bytes)

    patched_content = pack_source_bundle(members)
    return PatchedSourceBundle(
        content=patched_content,
        sha256=hashlib.sha256(patched_content).hexdigest(),
    )


def _validate_mutation_budget(
    *,
    maximum_members: int,
    maximum_member_bytes: int,
    maximum_total_bytes: int,
) -> None:
    if (
        isinstance(maximum_members, bool)
        or not isinstance(maximum_members, int)
        or not 1 <= maximum_members <= MAX_SOURCE_BUNDLE_MEMBERS
    ):
        raise SourceBundleError("source bundle member-count budget is invalid")
    if (
        isinstance(maximum_member_bytes, bool)
        or not isinstance(maximum_member_bytes, int)
        or not 1 <= maximum_member_bytes <= MAX_SOURCE_BYTES
    ):
        raise SourceBundleError("source member byte budget is invalid")
    if (
        isinstance(maximum_total_bytes, bool)
        or not isinstance(maximum_total_bytes, int)
        or not 1 <= maximum_total_bytes <= MAX_SOURCE_BUNDLE_TOTAL_BYTES
    ):
        raise SourceBundleError("source bundle total byte budget is invalid")


def _require_python_path(path: str) -> str:
    normalized = normalize_source_bundle_path(path)
    if not normalized.endswith(".py"):
        raise SourceBundleError("mutable source bundle members must use .py paths")
    return normalized


def _read_python_member(bundle, path: str) -> str:
    try:
        return bundle.read_text(path)
    except SourceBundleMemberNotTextError as exc:
        raise SourceBundleError("mutable source bundle members must contain UTF-8 text") from exc


def _encode_nonempty_source(source: str, *, path: str) -> bytes:
    if not isinstance(source, str):
        raise SourceBundleError("source bundle member content must be text")
    try:
        content = source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SourceBundleError("source bundle member content must be valid UTF-8") from exc
    if not content or "\x00" in source:
        raise SourceBundleError(f"source bundle member {path!r} must be nonempty UTF-8 text")
    return content


def _decode_nonempty_source(content: bytes, *, path: str) -> str:
    try:
        source = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceBundleError("source bundle member content must be valid UTF-8") from exc
    if not source or "\x00" in source:
        raise SourceBundleError(f"source bundle member {path!r} must be nonempty UTF-8 text")
    return source


def _require_member_budget(content: bytes, *, maximum_member_bytes: int) -> None:
    if len(content) > maximum_member_bytes:
        raise SourceBundleError("source bundle member exceeds the caller-owned byte budget")


def _require_member_count(members: dict[str, bytes], *, maximum_members: int) -> None:
    if len(members) > maximum_members:
        raise SourceBundleError("source bundle exceeds the caller-owned member-count budget")


def _require_total_budget(
    members: dict[str, bytes],
    *,
    maximum_total_bytes: int,
) -> None:
    if sum(len(content) for content in members.values()) > maximum_total_bytes:
        raise SourceBundleError("source bundle exceeds the caller-owned total byte budget")


def _require_existing_member(path: str, members: dict[str, bytes]) -> None:
    if path not in members:
        raise SourceBundleError("source bundle change target does not exist")


def _validate_literal_edit_budget(changes: tuple[SourceBundleChange, ...]) -> None:
    edits = tuple(
        edit
        for change in changes
        if isinstance(change, SourceBundleLiteralEdit)
        for edit in change.edits
    )
    if any(not change.edits for change in changes if isinstance(change, SourceBundleLiteralEdit)):
        raise SourceBundleError("a source bundle literal change requires at least one edit")
    if len(edits) > MAX_EDIT_COUNT:
        raise SourceBundleError(f"a source bundle mutation accepts at most {MAX_EDIT_COUNT} edits")
    try:
        aggregate_bytes = sum(
            len(edit.old_text.encode("utf-8")) + len(edit.new_text.encode("utf-8"))
            for edit in edits
        )
    except (AttributeError, UnicodeEncodeError) as exc:
        raise SourcePatchError(
            SourcePatchFailure.BUDGET_EXCEEDED,
            "Source bundle edit text must be valid UTF-8",
        ) from exc
    if aggregate_bytes > MAX_AGGREGATE_EDIT_BYTES:
        raise SourcePatchError(
            SourcePatchFailure.BUDGET_EXCEEDED,
            "Source bundle edits exceed 32,768 aggregate UTF-8 bytes",
        )


def apply_source_bundle_patch(
    content: bytes,
    expected_sha256: str,
    file_patches: tuple[SourceBundleFilePatch, ...],
) -> PatchedSourceBundle:
    """Patch existing text files after one exact whole-bundle SHA check."""

    bundle = inspect_source_bundle(content)
    if bundle.sha256 != expected_sha256:
        raise SourcePatchError(
            SourcePatchFailure.STALE_SOURCE,
            "The source bundle has changed since it was inspected",
        )
    if not 1 <= len(file_patches) <= MAX_BUNDLE_PATCH_FILES:
        raise SourceBundleError(f"a bundle repair must modify 1..{MAX_BUNDLE_PATCH_FILES} files")
    paths = tuple(patch.path for patch in file_patches)
    if len(paths) != len(set(paths)):
        raise SourceBundleError("bundle repair paths must be unique")
    if sum(len(patch.edits) for patch in file_patches) > MAX_EDIT_COUNT:
        raise SourceBundleError(f"a bundle repair accepts at most {MAX_EDIT_COUNT} edits")
    try:
        aggregate_edit_bytes = sum(
            len(edit.old_text.encode("utf-8")) + len(edit.new_text.encode("utf-8"))
            for patch in file_patches
            for edit in patch.edits
        )
    except UnicodeEncodeError as exc:
        raise SourcePatchError(
            SourcePatchFailure.BUDGET_EXCEEDED,
            "Repair text must be valid UTF-8",
        ) from exc
    if aggregate_edit_bytes > MAX_AGGREGATE_EDIT_BYTES:
        raise SourcePatchError(
            SourcePatchFailure.BUDGET_EXCEEDED,
            "Bundle repair edits exceed 32,768 aggregate UTF-8 bytes",
        )

    members = {member.path: bundle.read(member.path) for member in bundle.members}
    for file_patch in file_patches:
        try:
            source = bundle.read_text(file_patch.path)
        except SourceBundleMemberNotFoundError as exc:
            raise SourceBundleError(
                "bundle repair may modify only an existing source file"
            ) from exc
        except SourceBundleMemberNotTextError as exc:
            raise SourceBundleError(
                "bundle repair may modify only an existing UTF-8 text file"
            ) from exc
        patched = apply_text_edits(source, file_patch.edits, maximum_source_bytes=MAX_SOURCE_BYTES)
        members[file_patch.path] = patched.encode("utf-8")

    patched_content = pack_source_bundle(members)
    patched_bundle = inspect_source_bundle(patched_content)
    return PatchedSourceBundle(
        content=patched_content,
        sha256=patched_bundle.sha256,
    )


__all__ = [
    "MAX_BUNDLE_PATCH_FILES",
    "MAX_SOURCE_BUNDLE_CHANGES",
    "PatchedSourceBundle",
    "SourceBundleAddMember",
    "SourceBundleChange",
    "SourceBundleDeleteMember",
    "SourceBundleFilePatch",
    "SourceBundleLiteralEdit",
    "SourceBundleReplaceMember",
    "apply_python_source_bundle_changes",
    "apply_source_bundle_patch",
]
