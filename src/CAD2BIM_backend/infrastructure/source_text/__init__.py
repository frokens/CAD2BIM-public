"""Shared exact-source text infrastructure with child-safe lazy imports."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "PatchedSourceBundle",
    "SourceBundle",
    "SourceBundleError",
    "SourceBundleMember",
    "SourceBundleMemberNotFoundError",
    "SourceBundleMemberNotTextError",
    "SourceBundleAddMember",
    "SourceBundleChange",
    "SourceBundleDeleteMember",
    "SourceBundleFilePatch",
    "SourceBundleLiteralEdit",
    "SourceBundleReplaceMember",
    "SourceEdit",
    "SourcePatchError",
    "SourcePatchFailure",
    "TextSourcePatchAdapter",
    "apply_text_edits",
    "apply_python_source_bundle_changes",
    "apply_source_bundle_patch",
    "inspect_source_bundle",
    "pack_source_bundle",
]


_EXPORT_MODULES = {
    "SourceBundle": ".source_bundle",
    "SourceBundleError": ".source_bundle",
    "SourceBundleMember": ".source_bundle",
    "SourceBundleMemberNotFoundError": ".source_bundle",
    "SourceBundleMemberNotTextError": ".source_bundle",
    "inspect_source_bundle": ".source_bundle",
    "pack_source_bundle": ".source_bundle",
    "SourceEdit": ".source_patch",
    "SourcePatchError": ".source_patch",
    "SourcePatchFailure": ".source_patch",
    "apply_text_edits": ".source_patch",
    "TextSourcePatchAdapter": ".source_patch_adapter",
    "PatchedSourceBundle": ".source_bundle_patch",
    "SourceBundleAddMember": ".source_bundle_patch",
    "SourceBundleChange": ".source_bundle_patch",
    "SourceBundleDeleteMember": ".source_bundle_patch",
    "SourceBundleFilePatch": ".source_bundle_patch",
    "SourceBundleLiteralEdit": ".source_bundle_patch",
    "SourceBundleReplaceMember": ".source_bundle_patch",
    "apply_python_source_bundle_changes": ".source_bundle_patch",
    "apply_source_bundle_patch": ".source_bundle_patch",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    return getattr(import_module(module_name, __name__), name)
