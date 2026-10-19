"""Materialize the exact ArtifactVersion byte closure into a Seed directory."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import AsyncIterator
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from CAD2BIM_backend.ports.storage import StorageKey, StoragePort

from tests.process_support.ifc_golden_seed.closure import SeedClosure, SeedExportScope
from tests.process_support.ifc_golden_seed.core import MAX_OBJECT_BYTES


class ArtifactInventoryError(ValueError):
    """An exact artifact is missing, corrupt, or unsafe to materialize."""


class ArtifactReader(Protocol):
    def iter_chunks(
        self,
        *,
        artifact_id: str,
        version_id: str,
        version_no: int,
    ) -> AsyncIterator[bytes]: ...


@dataclass(frozen=True, slots=True)
class MappingArtifactReader:
    """Small deterministic reader for synthetic tests."""

    objects: Mapping[tuple[str, str], bytes]
    chunk_size: int = 64 * 1024

    async def iter_chunks(
        self,
        *,
        artifact_id: str,
        version_id: str,
        version_no: int,
    ) -> AsyncIterator[bytes]:
        del version_no
        try:
            content = self.objects[(artifact_id, version_id)]
        except KeyError as exc:
            raise ArtifactInventoryError("exact artifact bytes are unavailable") from exc
        if self.chunk_size < 1:
            raise ArtifactInventoryError("synthetic artifact chunk size must be positive")
        for offset in range(0, len(content), self.chunk_size):
            yield bytes(content[offset : offset + self.chunk_size])


@dataclass(frozen=True, slots=True)
class StoragePortArtifactReader:
    storage: StoragePort
    scope: SeedExportScope

    def iter_chunks(
        self,
        *,
        artifact_id: str,
        version_id: str,
        version_no: int,
    ) -> AsyncIterator[bytes]:
        del version_id
        return self.storage.iter_chunks(
            StorageKey(
                owner_id=self.scope.owner_id,
                task_id=self.scope.task_id,
                object_id=artifact_id,
                version=version_no,
            ),
            chunk_size=1024 * 1024,
        )


@dataclass(frozen=True, slots=True)
class ArtifactInventoryResult:
    inventory: tuple[dict[str, object], ...]
    object_paths: Mapping[tuple[str, str], str]


def _real_directory(path: Path) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ArtifactInventoryError("Seed staging directory is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ArtifactInventoryError("Seed staging root must be a real directory")
    return path.absolute()


def _object_relative_path(artifact_id: str, version_id: str, sha256: str) -> str:
    identity = hashlib.sha256(f"{artifact_id}\x00{version_id}".encode()).hexdigest()[:24]
    return f"objects/artifacts/{identity}/{sha256}"


async def _stream_exclusive(
    root: Path,
    relative_path: str,
    chunks: AsyncIterator[bytes],
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    relative = PurePosixPath(relative_path)
    target = root.joinpath(*relative.parts)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = -1
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            async for chunk in chunks:
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise ArtifactInventoryError("artifact stream yielded a non-byte chunk")
                content = bytes(chunk)
                total += len(content)
                if total > expected_size or total > MAX_OBJECT_BYTES:
                    raise ArtifactInventoryError("artifact stream exceeded its declared limit")
                digest.update(content)
                stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if total != expected_size:
            raise ArtifactInventoryError("artifact byte size does not match the database row")
        if digest.hexdigest() != expected_sha256:
            raise ArtifactInventoryError("artifact SHA-256 does not match the database row")
    except FileExistsError as exc:
        raise ArtifactInventoryError("refusing to overwrite a Seed object") from exc
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise


async def materialize_artifact_inventory(
    closure: SeedClosure,
    reader: ArtifactReader,
    seed_root: Path,
) -> ArtifactInventoryResult:
    """Read, verify, and exclusively write every exact reachable ArtifactVersion."""

    root = _real_directory(seed_root)
    versions = {
        (row["artifact_id"], row["version_id"]): row for row in closure.artifact_versions
    }
    inventory: list[dict[str, object]] = []
    object_paths: dict[tuple[str, str], str] = {}
    for reachable in closure.artifact_reachability:
        exact = (reachable.artifact_id, reachable.version_id)
        version = versions.get(exact)
        if version is None:
            raise ArtifactInventoryError("artifact reachability lacks its exact version row")
        version_no = version.get("version_no")
        expected_size = version.get("size_bytes")
        expected_sha = version.get("sha256")
        media_type = version.get("content_type")
        filename = version.get("filename")
        if (
            isinstance(version_no, bool)
            or not isinstance(version_no, int)
            or version_no < 1
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or expected_size > MAX_OBJECT_BYTES
            or not isinstance(expected_sha, str)
            or len(expected_sha) != 64
            or not isinstance(media_type, str)
            or not isinstance(filename, str)
            or not filename
        ):
            raise ArtifactInventoryError("artifact version metadata is invalid")
        relative_path = _object_relative_path(*exact, expected_sha)
        await _stream_exclusive(
            root,
            relative_path,
            reader.iter_chunks(
                artifact_id=reachable.artifact_id,
                version_id=reachable.version_id,
                version_no=version_no,
            ),
            expected_size=expected_size,
            expected_sha256=expected_sha,
        )
        object_paths[exact] = relative_path
        inventory.append(
            {
                "path": relative_path,
                "filename": filename,
                "media_type": media_type,
                "size": expected_size,
                "sha256": expected_sha,
                "source_artifact_id": reachable.artifact_id,
                "source_artifact_version_id": reachable.version_id,
                "reverse_references": list(reachable.reverse_references),
            }
        )
    return ArtifactInventoryResult(
        inventory=tuple(sorted(inventory, key=lambda item: str(item["path"]))),
        object_paths=object_paths,
    )


__all__ = [
    "ArtifactInventoryError",
    "ArtifactInventoryResult",
    "ArtifactReader",
    "MappingArtifactReader",
    "StoragePortArtifactReader",
    "materialize_artifact_inventory",
]
