"""Fail-closed loaders for reviewed SYSTEM geometry seed resources."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from CAD2BIM_backend.domain.system_geometry_seed import SystemGeometrySeedManifest


_RESOURCE_PACKAGE = "CAD2BIM_backend.infrastructure.geometry_seed.resources"
_MANIFEST_NAME = "manifest.json"
_MAX_MANIFEST_BYTES = 1024 * 1024
_DEFAULT_MAX_SOURCE_BYTES = 100 * 1024 * 1024


class SystemGeometrySeedLoadError(RuntimeError):
    """A package or neutral-fixture manifest cannot be trusted."""


@dataclass(frozen=True, slots=True)
class LoadedSystemGeometrySeed:
    manifest: SystemGeometrySeedManifest
    sources: Mapping[str, bytes]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SystemGeometrySeedLoadError(
                f"system geometry seed JSON contains duplicate key: {key}"
            )
        result[key] = value
    return result


def _parse_manifest(content: bytes) -> SystemGeometrySeedManifest:
    if len(content) > _MAX_MANIFEST_BYTES:
        raise SystemGeometrySeedLoadError("system geometry seed manifest is too large")
    try:
        payload = json.loads(content.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        return SystemGeometrySeedManifest.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise SystemGeometrySeedLoadError("system geometry seed manifest is invalid") from exc


def _load(
    manifest_content: bytes,
    read_source: Callable[[str], bytes],
    *,
    max_source_bytes: int,
) -> LoadedSystemGeometrySeed:
    if max_source_bytes < 1:
        raise ValueError("max_source_bytes must be positive")
    manifest = _parse_manifest(manifest_content)
    sources: dict[str, bytes] = {}
    for entry in manifest.resources:
        content = read_source(entry.source_path)
        if len(content) > max_source_bytes:
            raise SystemGeometrySeedLoadError(
                f"system geometry seed source is too large: {entry.seed_key}"
            )
        sources[entry.source_path] = content
    return LoadedSystemGeometrySeed(manifest=manifest, sources=sources)


def _safe_parts(relative_path: str) -> tuple[str, ...]:
    path = PurePosixPath(relative_path)
    if (
        path.is_absolute()
        or relative_path != path.as_posix()
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SystemGeometrySeedLoadError(
            "system geometry seed source path is not safe and relative"
        )
    return path.parts


def _read_directory_file(root: Path, relative_path: str, *, limit: int) -> bytes:
    parts = _safe_parts(relative_path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    descriptors: list[int] = []
    try:
        descriptor = os.open(root, directory_flags)
        descriptors.append(descriptor)
        for part in parts[:-1]:
            descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        file_descriptor = os.open(parts[-1], flags, dir_fd=descriptor)
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(file_descriptor)
            raise SystemGeometrySeedLoadError(
                "system geometry seed source must be a regular file"
            )
        with os.fdopen(file_descriptor, "rb") as handle:
            content = handle.read(limit + 1)
        if len(content) > limit:
            raise SystemGeometrySeedLoadError("system geometry seed file is too large")
        return content
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
        raise SystemGeometrySeedLoadError(
            "system geometry seed source is missing or unsafe"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def load_system_geometry_seed_directory(
    root: str | Path,
    *,
    max_source_bytes: int = _DEFAULT_MAX_SOURCE_BYTES,
) -> LoadedSystemGeometrySeed:
    """Load a neutral fixture root with fixed ``manifest.json`` and no symlinks."""

    root_path = Path(root)
    if root_path.is_symlink():
        raise SystemGeometrySeedLoadError("system geometry seed root must not be a symlink")
    try:
        resolved_root = root_path.resolve(strict=True)
    except OSError as exc:
        raise SystemGeometrySeedLoadError("system geometry seed root is unavailable") from exc
    if not resolved_root.is_dir():
        raise SystemGeometrySeedLoadError("system geometry seed root must be a directory")
    manifest_content = _read_directory_file(
        resolved_root,
        _MANIFEST_NAME,
        limit=_MAX_MANIFEST_BYTES,
    )
    return _load(
        manifest_content,
        lambda relative_path: _read_directory_file(
            resolved_root,
            relative_path,
            limit=max_source_bytes,
        ),
        max_source_bytes=max_source_bytes,
    )


def _read_traversable_file(root: Traversable, relative_path: str, *, limit: int) -> bytes:
    parts = _safe_parts(relative_path)
    current = root
    for part in parts:
        current = current.joinpath(part)
    if not current.is_file():
        raise SystemGeometrySeedLoadError("packaged system geometry seed source is unavailable")
    content = current.read_bytes()
    if len(content) > limit:
        raise SystemGeometrySeedLoadError("packaged system geometry seed file is too large")
    return content


def load_production_system_geometry_seed(
    *,
    max_source_bytes: int = _DEFAULT_MAX_SOURCE_BYTES,
) -> LoadedSystemGeometrySeed:
    """Read only the fixed manifest and sources embedded in this installed package."""

    root = files(_RESOURCE_PACKAGE)
    if isinstance(root, Path):
        if root.is_symlink():
            raise SystemGeometrySeedLoadError(
                "packaged system geometry seed root must not be a symlink"
            )
        try:
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise SystemGeometrySeedLoadError(
                "packaged system geometry seed root is unavailable"
            ) from exc
        manifest_content = _read_directory_file(
            resolved_root,
            _MANIFEST_NAME,
            limit=_MAX_MANIFEST_BYTES,
        )
        return _load(
            manifest_content,
            lambda relative_path: _read_directory_file(
                resolved_root,
                relative_path,
                limit=max_source_bytes,
            ),
            max_source_bytes=max_source_bytes,
        )
    manifest_content = _read_traversable_file(root, _MANIFEST_NAME, limit=_MAX_MANIFEST_BYTES)
    return _load(
        manifest_content,
        lambda relative_path: _read_traversable_file(
            root,
            relative_path,
            limit=max_source_bytes,
        ),
        max_source_bytes=max_source_bytes,
    )


__all__ = [
    "LoadedSystemGeometrySeed",
    "SystemGeometrySeedLoadError",
    "load_production_system_geometry_seed",
    "load_system_geometry_seed_directory",
]
