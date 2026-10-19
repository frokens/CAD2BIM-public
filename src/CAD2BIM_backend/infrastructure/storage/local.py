"""Async local-disk implementation of :mod:`CAD2BIM_backend.ports.storage`."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import stat as stat_module
import time
import unicodedata
import weakref
from collections.abc import AsyncIterable, AsyncIterator, Collection
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from CAD2BIM_backend.ports.storage import (
    ProductFamilyVersionStorageKey,
    ResourceStorageKey,
    StorageConflictError,
    StorageKey,
    StorageObjectKey,
    StorageObjectNotFoundError,
    StorageValidationError,
    StoredObject,
    VerifiedStorageRead,
)


DEFAULT_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/dxf",
        "application/json",
        "application/vnd.cad2bim.authoring-draft+json",
        "application/schema+json",
        "application/octet-stream",
        "application/pdf",
        "application/sql",
        "application/vnd.apache.parquet",
        "application/vnd.cad2bim.indexed-triangle-mesh+json",
        "application/vnd.opencascade.brep",
        "application/x-tar",
        "application/x-ifc",
        "application/x-step",
        "application/zip",
        "application/vnd.cad2bim.geometry-inspection+zip",
        "image/jpeg",
        "image/png",
        "image/vnd.dxf",
        "model/ifc",
        "model/step",
        "model/stl",
        "text/csv",
        "text/markdown",
        "text/plain",
        "text/x-python",
    }
)

_UNSAFE_FILENAME = re.compile(r"[\x00-\x1f\x7f:*?\"<>|]")
_STAGING_GRACE_SECONDS = 60 * 60
_STORAGE_METADATA_MAX_BYTES = 64 * 1024


def sanitize_filename(filename: str) -> str:
    """Return a portable display filename, rejecting path syntax outright."""

    if not isinstance(filename, str) or not filename:
        raise StorageValidationError("filename must be a non-empty string")
    if "\x00" in filename:
        raise StorageValidationError("filename must not contain NUL")
    if "/" in filename or "\\" in filename:
        raise StorageValidationError("filename must not contain path separators")
    normalized = unicodedata.normalize("NFKC", filename).strip()
    if normalized in {"", ".", ".."} or normalized.startswith("."):
        raise StorageValidationError("filename is not a regular visible name")
    sanitized = _UNSAFE_FILENAME.sub("_", normalized).rstrip(". ")
    if not sanitized:
        raise StorageValidationError("filename is empty after sanitization")
    return sanitized[:255]


class LocalStorage:
    """Logical-key storage with atomic publication and no network dependency."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_size_bytes: int = 100 * 1024 * 1024,
        allowed_content_types: Collection[str] = DEFAULT_ALLOWED_CONTENT_TYPES,
    ) -> None:
        if max_size_bytes < 1:
            raise ValueError("max_size_bytes must be positive")
        root_path = Path(root).expanduser()
        root_path.mkdir(parents=True, exist_ok=True)
        self._root = root_path.resolve(strict=True)
        if not self._root.is_dir():
            raise ValueError("storage root must be a directory")
        root_info = self._root.stat()
        self._root_identity = (root_info.st_dev, root_info.st_ino)
        self._max_size_bytes = max_size_bytes
        self._allowed_content_types = frozenset(
            item.strip().lower() for item in allowed_content_types
        )
        self._locks: weakref.WeakValueDictionary[tuple[str, ...], asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._locks_guard = asyncio.Lock()

    async def write(
        self,
        key: StorageObjectKey,
        *,
        filename: str,
        content_type: str,
        chunks: AsyncIterable[bytes],
    ) -> StoredObject:
        safe_name = sanitize_filename(filename)
        media_type = self._validate_content_type(content_type)
        lock = await self._lock_for(key)
        async with lock:
            parent, directory = await asyncio.to_thread(self._prepare_parent, key)
            token = uuid4().hex
            staging = parent / f".tmp-{self._lock_identity(key)[-1]}-{token}"
            await asyncio.to_thread(staging.mkdir, 0o700)
            payload = staging / "payload"
            metadata_path = staging / "metadata.json"
            descriptor: int | None = None
            try:
                descriptor = await asyncio.to_thread(self._open_exclusive, payload)
                digest = hashlib.sha256()
                size = 0
                async for chunk in chunks:
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise StorageValidationError("storage chunks must be bytes")
                    data = bytes(chunk)
                    size += len(data)
                    if size > self._max_size_bytes:
                        raise StorageValidationError(
                            f"payload exceeds {self._max_size_bytes} bytes"
                        )
                    digest.update(data)
                    if data:
                        await asyncio.to_thread(self._write_all, descriptor, data)
                await asyncio.to_thread(os.fsync, descriptor)
                await asyncio.to_thread(os.close, descriptor)
                descriptor = None

                checksum = digest.hexdigest()
                created_at = datetime.now(timezone.utc)
                result = StoredObject(
                    key=key,
                    filename=safe_name,
                    content_type=media_type,
                    size_bytes=size,
                    sha256=checksum,
                    created_at=created_at,
                )
                await asyncio.to_thread(self._write_metadata, metadata_path, result)
                await asyncio.to_thread(self._fsync_directory, staging)

                existing = await asyncio.to_thread(self._load_verified_optional, key, directory)
                if existing is not None:
                    return self._require_same_version(existing, result)

                try:
                    await asyncio.to_thread(self._publish, staging, directory, parent)
                except OSError:
                    if not staging.exists():
                        # The rename completed and a later durability step failed.
                        raise
                    existing = await asyncio.to_thread(self._load_verified_optional, key, directory)
                    if existing is None:
                        raise
                    return self._require_same_version(existing, result)
                return result
            finally:
                if descriptor is not None:
                    await asyncio.to_thread(os.close, descriptor)
                await asyncio.to_thread(self._remove_staging, staging)

    async def put(
        self,
        key: StorageObjectKey,
        *,
        filename: str,
        content_type: str,
        chunks: AsyncIterable[bytes],
    ) -> StoredObject:
        return await self.write(key, filename=filename, content_type=content_type, chunks=chunks)

    async def write_bytes(
        self,
        key: StorageObjectKey,
        content: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> StoredObject:
        async def one_chunk():
            yield content

        return await self.write(
            key, filename=filename, content_type=content_type, chunks=one_chunk()
        )

    async def read(self, key: StorageObjectKey) -> bytes:
        return (await self.read_verified(key)).content

    async def read_verified(self, key: StorageObjectKey) -> VerifiedStorageRead:
        """Return bytes and metadata verified from one safely opened payload FD."""

        lock = await self._lock_for(key)
        async with lock:
            directory = await asyncio.to_thread(self._checked_directory, key)
            return await asyncio.to_thread(self._read_verified_sync, key, directory)

    async def iter_chunks(
        self,
        key: StorageObjectKey,
        *,
        chunk_size: int = 1024 * 1024,
        expected: StoredObject | None = None,
    ) -> AsyncIterator[bytes]:
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        lock = await self._lock_for(key)
        async with lock:
            directory = await asyncio.to_thread(self._checked_directory, key)
            existing = await asyncio.to_thread(
                self._load_verified_optional,
                key,
                directory,
                expected is None,
            )
            if existing is None:
                raise StorageObjectNotFoundError("stored object not found")
            if expected is not None and existing != expected:
                raise StorageConflictError("stored object changed after integrity validation")
            handle = await asyncio.to_thread((directory / "payload").open, "rb")
            try:
                while chunk := await asyncio.to_thread(handle.read, chunk_size):
                    yield chunk
            finally:
                await asyncio.to_thread(handle.close)

    async def stat(self, key: StorageObjectKey) -> StoredObject:
        lock = await self._lock_for(key)
        async with lock:
            directory = await asyncio.to_thread(self._checked_directory, key)
            result = await asyncio.to_thread(self._load_verified_optional, key, directory)
            if result is None:
                raise StorageObjectNotFoundError("stored object not found")
            return result

    async def exists(self, key: StorageObjectKey) -> bool:
        lock = await self._lock_for(key)
        async with lock:
            try:
                directory = await asyncio.to_thread(self._checked_directory, key)
            except StorageObjectNotFoundError:
                return False
            return await asyncio.to_thread(self._load_verified_optional, key, directory) is not None

    async def delete(self, key: StorageObjectKey) -> bool:
        lock = await self._lock_for(key)
        async with lock:
            try:
                directory = await asyncio.to_thread(self._checked_directory, key)
            except StorageObjectNotFoundError:
                return False
            payload = directory / "payload"
            metadata = directory / "metadata.json"
            if not self._safe_regular_file(payload):
                return False
            await asyncio.to_thread(payload.unlink)
            await asyncio.to_thread(self._unlink_if_present, metadata)
            await asyncio.to_thread(directory.rmdir)
            await asyncio.to_thread(self._fsync_directory, directory.parent)
            return True

    async def verify_ready(self) -> None:
        """Verify the configured volume is writable without publishing an artifact."""

        await asyncio.to_thread(self._verify_ready_sync)

    def _verify_ready_sync(self) -> None:
        self._remove_stale_staging()
        probe = self._root / f".readiness-{uuid4().hex}"
        descriptor: int | None = None
        try:
            descriptor = self._open_exclusive(probe)
            self._write_all(descriptor, b"ready")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            probe.unlink()
            self._fsync_directory(self._root)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            self._unlink_if_present(probe)

    def _remove_stale_staging(self) -> None:
        cutoff = time.time() - _STAGING_GRACE_SECONDS
        for staging in self._root.rglob(".tmp-*"):
            try:
                info = staging.lstat()
            except FileNotFoundError:
                continue
            if stat_module.S_ISLNK(info.st_mode):
                raise StorageValidationError("staging path must not be a symlink")
            if not stat_module.S_ISDIR(info.st_mode) or info.st_mtime > cutoff:
                continue
            self._remove_staging(staging)

    async def _lock_for(self, key: StorageObjectKey) -> asyncio.Lock:
        parts = self._lock_identity(key)
        async with self._locks_guard:
            return self._locks.setdefault(parts, asyncio.Lock())

    def _key_parts(self, key: StorageKey) -> tuple[str, str, str, str]:
        return (
            self._validate_component("owner_id", key.owner_id),
            self._validate_component("task_id", key.task_id),
            self._validate_component("object_id", key.object_id),
            self._validate_component("version", str(key.version)),
        )

    def _resource_key_parts(self, key: ResourceStorageKey) -> tuple[str, str, str, str]:
        scope_kind = self._validate_scope_kind(key.scope_kind)
        return (
            scope_kind,
            self._validate_component("scope_id", key.scope_id),
            self._validate_component("resource_version_id", key.resource_version_id),
            self._validate_component("asset_role", key.asset_role),
        )

    def _product_family_key_parts(
        self,
        key: ProductFamilyVersionStorageKey,
    ) -> tuple[str, str, str, str]:
        scope_kind = self._validate_scope_kind(key.scope_kind)
        return (
            scope_kind,
            self._validate_component("scope_id", key.scope_id),
            self._validate_component("family_version_id", key.family_version_id),
            self._validate_component("asset_role", key.asset_role),
        )

    def _lock_identity(self, key: StorageObjectKey) -> tuple[str, ...]:
        if isinstance(key, ProductFamilyVersionStorageKey):
            return ("product-family-version-asset", *self._product_family_key_parts(key))
        if isinstance(key, ResourceStorageKey):
            return ("resource-asset", *self._resource_key_parts(key))
        if isinstance(key, StorageKey):
            return ("artifact", *self._key_parts(key))
        raise StorageValidationError("unsupported storage key type")

    @staticmethod
    def _validate_scope_kind(value: str) -> str:
        if not isinstance(value, str) or value not in {"SYSTEM", "TENANT", "TASK"}:
            raise StorageValidationError("scope_kind must be SYSTEM, TENANT, or TASK")
        return value.lower()

    @staticmethod
    def _validate_component(label: str, value: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 255:
            raise StorageValidationError(f"{label} must be 1..255 characters")
        if (
            value in {".", ".."}
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or "/" in value
            or "\\" in value
        ):
            raise StorageValidationError(f"invalid {label}")
        return f"{label}-{hashlib.sha256(value.encode('utf-8')).hexdigest()}"

    def _validate_content_type(self, content_type: str) -> str:
        if not isinstance(content_type, str) or not content_type.strip():
            raise StorageValidationError("content_type is required")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type not in self._allowed_content_types:
            raise StorageValidationError(f"content type is not allowed: {media_type}")
        return media_type

    def _logical_directory(self, key: StorageObjectKey) -> Path:
        if isinstance(key, ProductFamilyVersionStorageKey):
            scope_kind, scope_id, family_version_id, asset_role = self._product_family_key_parts(
                key
            )
            return self._root.joinpath(
                "product-family-version-assets",
                "scopes",
                scope_kind,
                "ids",
                scope_id,
                "versions",
                family_version_id,
                "assets",
                asset_role,
            )
        if isinstance(key, ResourceStorageKey):
            scope_kind, scope_id, resource_version_id, asset_role = self._resource_key_parts(key)
            return self._root.joinpath(
                "resource-assets",
                "scopes",
                scope_kind,
                "ids",
                scope_id,
                "versions",
                resource_version_id,
                "assets",
                asset_role,
            )
        if not isinstance(key, StorageKey):
            raise StorageValidationError("unsupported storage key type")
        owner, task, object_id, version = self._key_parts(key)
        return self._root.joinpath(
            "owners", owner, "tasks", task, "objects", object_id, "versions", version
        )

    def _prepare_parent(self, key: StorageObjectKey) -> tuple[Path, Path]:
        directory = self._logical_directory(key)
        parent = directory.parent
        current = self._root
        for component in parent.relative_to(self._root).parts:
            current = current / component
            if current.exists() or current.is_symlink():
                info = current.lstat()
                if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
                    raise StorageValidationError("storage path contains a symlink or non-directory")
            else:
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    info = current.lstat()
                    if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
                        raise StorageValidationError(
                            "storage path contains a symlink or non-directory"
                        )
        self._assert_contained(parent)
        self._assert_contained(directory)
        return parent, directory

    def _checked_directory(self, key: StorageObjectKey) -> Path:
        directory = self._logical_directory(key)
        if not directory.exists():
            raise StorageObjectNotFoundError("stored object not found")
        current = self._root
        for component in directory.relative_to(self._root).parts:
            current = current / component
            info = current.lstat()
            if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
                raise StorageValidationError("storage path contains a symlink or non-directory")
        self._assert_contained(directory)
        return directory

    def _assert_contained(self, path: Path) -> None:
        try:
            path.resolve(strict=False).relative_to(self._root)
        except ValueError as exc:
            raise StorageValidationError("storage path escapes configured root") from exc

    def _safe_regular_file(self, path: Path) -> bool:
        if not path.exists() and not path.is_symlink():
            return False
        info = path.lstat()
        if stat_module.S_ISLNK(info.st_mode):
            raise StorageValidationError("storage payload must not be a symlink")
        self._assert_contained(path)
        return stat_module.S_ISREG(info.st_mode)

    @staticmethod
    def _open_exclusive(path: Path) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        return os.open(path, flags, 0o600)

    @staticmethod
    def _write_all(descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]

    def _write_metadata(self, path: Path, value: StoredObject) -> None:
        payload = json.dumps(
            {
                "filename": value.filename,
                "content_type": value.content_type,
                "size_bytes": value.size_bytes,
                "sha256": value.sha256,
                "created_at": value.created_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor = self._open_exclusive(path)
        try:
            self._write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _publish(self, staging: Path, directory: Path, parent: Path) -> None:
        """Publish one complete immutable version with a single directory rename."""

        self._assert_contained(staging)
        self._assert_contained(directory)
        os.rename(staging, directory)
        self._fsync_directory(parent)

    def _load_verified_optional(
        self,
        key: StorageObjectKey,
        directory: Path,
        verify_checksum: bool = True,
    ) -> StoredObject | None:
        if not directory.exists() and not directory.is_symlink():
            return None
        info = directory.lstat()
        if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
            raise StorageValidationError("stored version is not a regular directory")
        self._assert_contained(directory)
        metadata = directory / "metadata.json"
        if not metadata.exists():
            raise StorageConflictError("stored version is incomplete: metadata is missing")
        if not self._safe_regular_file(metadata):
            raise StorageValidationError("storage metadata is not a regular file")
        try:
            raw = json.loads(metadata.read_text(encoding="utf-8"))
            result = StoredObject(
                key=key,
                filename=str(raw["filename"]),
                content_type=str(raw["content_type"]),
                size_bytes=int(raw["size_bytes"]),
                sha256=str(raw["sha256"]),
                created_at=datetime.fromisoformat(raw["created_at"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StorageValidationError("storage metadata is invalid") from exc
        payload = directory / "payload"
        if not self._safe_regular_file(payload):
            raise StorageConflictError("stored version is incomplete: payload is missing")
        size = payload.stat().st_size
        if size != result.size_bytes:
            raise StorageConflictError("stored payload checksum does not match immutable metadata")
        if verify_checksum:
            _, checksum = self._hash_file(payload)
            if checksum != result.sha256:
                raise StorageConflictError(
                    "stored payload checksum does not match immutable metadata"
                )
        return result

    def _read_verified_sync(
        self,
        key: StorageObjectKey,
        directory: Path,
    ) -> VerifiedStorageRead:
        directory_descriptor = self._open_version_directory(directory)
        metadata_descriptor: int | None = None
        payload_descriptor: int | None = None
        try:
            metadata_descriptor, metadata_opened = self._open_regular_at(
                directory_descriptor,
                "metadata.json",
                label="metadata",
            )
            if metadata_opened.st_size > _STORAGE_METADATA_MAX_BYTES:
                raise StorageValidationError(
                    f"storage metadata exceeds {_STORAGE_METADATA_MAX_BYTES} bytes"
                )
            metadata_content = self._read_bounded_descriptor(
                metadata_descriptor,
                maximum_bytes=_STORAGE_METADATA_MAX_BYTES,
                label="metadata",
            )
            stored = self._decode_metadata(key, metadata_content)
            if stored.size_bytes > self._max_size_bytes:
                raise StorageValidationError("stored payload exceeds configured maximum size")

            payload_descriptor, payload_opened = self._open_regular_at(
                directory_descriptor,
                "payload",
                label="payload",
            )
            if payload_opened.st_size != stored.size_bytes:
                raise StorageConflictError(
                    "stored payload checksum does not match immutable metadata"
                )

            digest = hashlib.sha256()
            chunks: list[bytes] = []
            size = 0
            while chunk := self._read_chunk(payload_descriptor, 1024 * 1024):
                size += len(chunk)
                if size > self._max_size_bytes:
                    raise StorageValidationError("stored payload exceeds configured maximum size")
                digest.update(chunk)
                chunks.append(chunk)
            payload_finished = os.fstat(payload_descriptor)
            metadata_finished = os.fstat(metadata_descriptor)
            self._require_named_descriptor_unchanged(
                directory_descriptor,
                "metadata.json",
                metadata_opened,
                metadata_finished,
            )
            self._require_named_descriptor_unchanged(
                directory_descriptor,
                "payload",
                payload_opened,
                payload_finished,
            )

            current_directory_descriptor = self._open_version_directory(directory)
            try:
                current_directory = os.fstat(current_directory_descriptor)
                opened_directory = os.fstat(directory_descriptor)
            finally:
                os.close(current_directory_descriptor)
            if (current_directory.st_dev, current_directory.st_ino) != (
                opened_directory.st_dev,
                opened_directory.st_ino,
            ):
                raise StorageConflictError("stored version changed during verified read")
        finally:
            if payload_descriptor is not None:
                os.close(payload_descriptor)
            if metadata_descriptor is not None:
                os.close(metadata_descriptor)
            os.close(directory_descriptor)

        verified_sha256 = digest.hexdigest()
        content = b"".join(chunks)
        if (
            size != stored.size_bytes
            or len(content) != stored.size_bytes
            or verified_sha256 != stored.sha256
        ):
            raise StorageConflictError("stored payload checksum does not match immutable metadata")
        return VerifiedStorageRead(
            stored=stored,
            content=content,
            verified_sha256=verified_sha256,
        )

    def _open_version_directory(self, directory: Path) -> int:
        try:
            components = directory.relative_to(self._root).parts
        except ValueError as exc:
            raise StorageValidationError("storage path escapes configured root") from exc
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(self._root, flags)
            opened_root = os.fstat(descriptor)
            if (opened_root.st_dev, opened_root.st_ino) != self._root_identity:
                os.close(descriptor)
                descriptor = None
                raise StorageConflictError("storage root changed during verified read")
            for component in components:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except FileNotFoundError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise StorageConflictError("stored version changed during verified read") from exc
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise StorageValidationError(
                    "storage path contains a symlink or non-directory"
                ) from exc
            raise StorageValidationError("storage version could not be opened safely") from exc

    @staticmethod
    def _open_regular_at(
        directory_descriptor: int,
        filename: str,
        *,
        label: str,
    ) -> tuple[int, os.stat_result]:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(filename, flags, dir_fd=directory_descriptor)
        except FileNotFoundError as exc:
            raise StorageConflictError(f"stored {label} changed during verified read") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise StorageValidationError(f"storage {label} must not be a symlink") from exc
            raise StorageValidationError(f"storage {label} could not be opened safely") from exc
        opened = os.fstat(descriptor)
        if not stat_module.S_ISREG(opened.st_mode):
            os.close(descriptor)
            raise StorageValidationError(f"storage {label} is not a regular file")
        return descriptor, opened

    @staticmethod
    def _read_bounded_descriptor(
        descriptor: int,
        *,
        maximum_bytes: int,
        label: str,
    ) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, min(64 * 1024, maximum_bytes + 1)):
            size += len(chunk)
            if size > maximum_bytes:
                raise StorageValidationError(f"storage {label} exceeds {maximum_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _require_named_descriptor_unchanged(
        directory_descriptor: int,
        filename: str,
        opened: os.stat_result,
        finished: os.stat_result,
    ) -> None:
        try:
            current = os.stat(
                filename,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise StorageConflictError("stored object changed during verified read") from exc
        if (
            not stat_module.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or (finished.st_dev, finished.st_ino, finished.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_size)
            or finished.st_mtime_ns != opened.st_mtime_ns
            or finished.st_ctime_ns != opened.st_ctime_ns
        ):
            raise StorageConflictError("stored object changed during verified read")

    @staticmethod
    def _decode_metadata(key: StorageObjectKey, content: bytes) -> StoredObject:
        try:
            raw = json.loads(content.decode("utf-8"))
            return StoredObject(
                key=key,
                filename=str(raw["filename"]),
                content_type=str(raw["content_type"]),
                size_bytes=int(raw["size_bytes"]),
                sha256=str(raw["sha256"]),
                created_at=datetime.fromisoformat(raw["created_at"]),
            )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StorageValidationError("storage metadata is invalid") from exc

    @staticmethod
    def _require_same_version(existing: StoredObject, incoming: StoredObject) -> StoredObject:
        if (
            existing.filename == incoming.filename
            and existing.content_type == incoming.content_type
            and existing.size_bytes == incoming.size_bytes
            and existing.sha256 == incoming.sha256
        ):
            return existing
        raise StorageConflictError(
            "immutable storage version already exists with different content or metadata"
        )

    @staticmethod
    def _hash_file(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    @staticmethod
    def _read_chunk(descriptor: int, size: int) -> bytes:
        return os.read(descriptor, size)

    def _remove_staging(self, staging: Path) -> None:
        if not staging.exists() and not staging.is_symlink():
            return
        self._assert_contained(staging)
        info = staging.lstat()
        if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
            raise StorageValidationError("staging path is not a regular directory")
        for child in staging.iterdir():
            if not self._safe_regular_file(child):
                raise StorageValidationError("staging contains a non-regular entry")
            child.unlink()
        staging.rmdir()

    @staticmethod
    def _unlink_if_present(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


LocalStorageAdapter = LocalStorage

__all__ = [
    "DEFAULT_ALLOWED_CONTENT_TYPES",
    "LocalStorage",
    "LocalStorageAdapter",
    "sanitize_filename",
]
