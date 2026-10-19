"""Storage contracts for immutable artifact and version-owned resource payloads."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class StorageError(Exception):
    """Base class for storage failures safe to translate at an application boundary."""


class StorageValidationError(StorageError, ValueError):
    """A logical key, filename, content type, or payload is invalid."""


class StorageConflictError(StorageError, ValueError):
    """An immutable logical version already exists with different content."""


class StorageObjectNotFoundError(StorageError, FileNotFoundError):
    """The requested logical object does not exist."""


@dataclass(frozen=True, slots=True)
class StorageKey:
    """Logical identity; it deliberately contains no filesystem path."""

    owner_id: str
    task_id: str
    object_id: str
    version: str | int


@dataclass(frozen=True, slots=True)
class ResourceStorageKey:
    """Logical identity for one exact immutable geometry-resource asset."""

    scope_kind: str
    scope_id: str
    resource_version_id: str
    asset_role: str


@dataclass(frozen=True, slots=True)
class ProductFamilyVersionStorageKey:
    """Logical identity for one exact immutable ProductFamilyVersion asset."""

    scope_kind: str
    scope_id: str
    family_version_id: str
    asset_role: str


StorageObjectKey = StorageKey | ResourceStorageKey | ProductFamilyVersionStorageKey


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Public metadata returned after publication; physical paths are private."""

    key: StorageObjectKey
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedStorageRead:
    """Metadata, bytes, and digest produced by one verified storage read."""

    stored: StoredObject
    content: bytes
    verified_sha256: str


class StoragePort(Protocol):
    async def write(
        self,
        key: StorageObjectKey,
        *,
        filename: str,
        content_type: str,
        chunks: AsyncIterable[bytes],
    ) -> StoredObject: ...

    async def read(self, key: StorageObjectKey) -> bytes: ...

    async def read_verified(self, key: StorageObjectKey) -> VerifiedStorageRead: ...

    async def delete(self, key: StorageObjectKey) -> bool: ...

    async def exists(self, key: StorageObjectKey) -> bool: ...

    async def stat(self, key: StorageObjectKey) -> StoredObject: ...

    def iter_chunks(
        self,
        key: StorageObjectKey,
        *,
        chunk_size: int = 1024 * 1024,
        expected: StoredObject | None = None,
    ) -> AsyncIterator[bytes]: ...


__all__ = [
    "StorageError",
    "StorageConflictError",
    "StorageKey",
    "ResourceStorageKey",
    "ProductFamilyVersionStorageKey",
    "StorageObjectKey",
    "StorageObjectNotFoundError",
    "StoragePort",
    "StorageValidationError",
    "StoredObject",
    "VerifiedStorageRead",
]
