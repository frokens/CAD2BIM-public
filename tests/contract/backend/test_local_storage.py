from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from CAD2BIM_backend.ports.storage import (
    ProductFamilyVersionStorageKey,
    ResourceStorageKey,
    StorageConflictError,
    StorageKey,
    StorageObjectNotFoundError,
    StorageValidationError,
)


async def _chunks(*values: bytes):
    for value in values:
        yield value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    (
        "application/vnd.opencascade.brep",
        "application/vnd.cad2bim.indexed-triangle-mesh+json;version=1",
        "application/zip",
    ),
)
async def test_geometry_catalog_content_types_use_the_shared_storage_port(
    tmp_path: Path,
    content_type: str,
) -> None:
    storage = LocalStorage(tmp_path)
    key = ResourceStorageKey(
        "TASK",
        "task",
        hashlib.sha256(content_type.encode("utf-8")).hexdigest(),
        "canonical_geometry",
    )

    stored = await storage.write_bytes(
        key,
        b"geometry",
        filename="geometry.bin",
        content_type=content_type,
    )

    assert stored.content_type == content_type.split(";", 1)[0]
    assert await storage.read(key) == b"geometry"


@pytest.mark.asyncio
async def test_write_read_checksum_metadata_and_delete(tmp_path: Path):
    storage = LocalStorage(tmp_path)
    key = StorageKey("owner-a", "task-a", "ifc-result", 1)

    stored = await storage.write(
        key,
        filename=" result?.ifc ",
        content_type="application/octet-stream",
        chunks=_chunks(b"abc", b"def"),
    )

    assert stored.filename == "result_.ifc"
    assert stored.sha256 == hashlib.sha256(b"abcdef").hexdigest()
    assert stored.size_bytes == 6
    assert not hasattr(stored, "path")
    assert await storage.read(key) == b"abcdef"
    assert b"".join([chunk async for chunk in storage.iter_chunks(key, chunk_size=2)]) == b"abcdef"
    assert await storage.stat(key) == stored
    assert await storage.exists(key)
    assert await storage.delete(key)
    assert not await storage.exists(key)
    assert not await storage.delete(key)


@pytest.mark.asyncio
async def test_resource_asset_reuses_storage_port_without_exposing_physical_path(
    tmp_path: Path,
):
    storage = LocalStorage(tmp_path)
    key = ResourceStorageKey(
        scope_kind="TASK",
        scope_id="task-a",
        resource_version_id="version-a",
        asset_role="canonical_geometry",
    )

    stored = await storage.write_bytes(
        key,
        b"geometry",
        filename="geometry.bin",
        content_type="application/octet-stream",
    )

    assert stored.key == key
    assert stored.filename == "geometry.bin"
    assert stored.sha256 == hashlib.sha256(b"geometry").hexdigest()
    assert not hasattr(stored, "path")
    assert await storage.read(key) == b"geometry"
    assert b"".join([chunk async for chunk in storage.iter_chunks(key, chunk_size=3)]) == (
        b"geometry"
    )
    assert await storage.stat(key) == stored
    assert await storage.exists(key)
    metadata = json.loads(
        (storage._logical_directory(key) / "metadata.json").read_text(encoding="utf-8")
    )
    assert set(metadata) == {
        "content_type",
        "created_at",
        "filename",
        "sha256",
        "size_bytes",
    }
    assert await storage.delete(key)
    assert not await storage.exists(key)


@pytest.mark.asyncio
async def test_artifact_geometry_and_product_family_assets_use_separate_namespaces(
    tmp_path: Path,
):
    storage = LocalStorage(tmp_path)
    artifact_key = StorageKey("owner", "task", "object", "v1")
    resource_key = ResourceStorageKey("TASK", "task", "object", "v1")
    family_key = ProductFamilyVersionStorageKey("TASK", "task", "object", "v1")

    artifact_parts = storage._key_parts(artifact_key)
    resource_parts = storage._resource_key_parts(resource_key)
    family_parts = storage._product_family_key_parts(family_key)

    assert storage._logical_directory(artifact_key) == tmp_path.joinpath(
        "owners",
        artifact_parts[0],
        "tasks",
        artifact_parts[1],
        "objects",
        artifact_parts[2],
        "versions",
        artifact_parts[3],
    )
    assert storage._logical_directory(resource_key) == tmp_path.joinpath(
        "resource-assets",
        "scopes",
        "task",
        "ids",
        resource_parts[1],
        "versions",
        resource_parts[2],
        "assets",
        resource_parts[3],
    )
    assert storage._logical_directory(family_key) == tmp_path.joinpath(
        "product-family-version-assets",
        "scopes",
        "task",
        "ids",
        family_parts[1],
        "versions",
        family_parts[2],
        "assets",
        family_parts[3],
    )

    await storage.write_bytes(
        artifact_key,
        b"artifact",
        filename="artifact.bin",
        content_type="application/octet-stream",
    )
    await storage.write_bytes(
        resource_key,
        b"resource",
        filename="resource.bin",
        content_type="application/octet-stream",
    )
    await storage.write_bytes(
        family_key,
        b"family",
        filename="family.zip",
        content_type="application/zip",
    )
    assert await storage.read(artifact_key) == b"artifact"
    assert await storage.read(resource_key) == b"resource"
    assert await storage.read(family_key) == b"family"


@pytest.mark.asyncio
async def test_product_family_version_asset_reuses_storage_port_and_is_restart_safe(
    tmp_path: Path,
) -> None:
    key = ProductFamilyVersionStorageKey(
        scope_kind="TASK",
        scope_id="task-a",
        family_version_id="family-version-a",
        asset_role="realization_source_bundle",
    )
    storage = LocalStorage(tmp_path)
    stored = await storage.write_bytes(
        key,
        b"family-source",
        filename="source.zip",
        content_type="application/zip",
    )

    restarted = LocalStorage(tmp_path)
    assert await restarted.stat(key) == stored
    assert await restarted.read(key) == b"family-source"
    verified = await restarted.read_verified(key)
    assert verified.stored == stored
    assert verified.content == b"family-source"
    assert verified.verified_sha256 == stored.sha256
    assert not hasattr(stored, "path")


@pytest.mark.asyncio
async def test_resource_scope_version_and_asset_role_are_distinct_namespaces(tmp_path: Path):
    storage = LocalStorage(tmp_path)
    keys = (
        ResourceStorageKey("SYSTEM", "shared", "version", "geometry"),
        ResourceStorageKey("TENANT", "shared", "version", "geometry"),
        ResourceStorageKey("TASK", "shared", "version", "geometry"),
        ResourceStorageKey("TASK", "other", "version", "geometry"),
        ResourceStorageKey("TASK", "shared", "other-version", "geometry"),
        ResourceStorageKey("TASK", "shared", "version", "manifest"),
    )

    for index, key in enumerate(keys):
        await storage.write_bytes(
            key,
            f"payload-{index}".encode(),
            filename="asset.bin",
            content_type="application/octet-stream",
        )

    for index, key in enumerate(keys):
        assert await storage.read(key) == f"payload-{index}".encode()
    assert len({storage._logical_directory(key) for key in keys}) == len(keys)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        ResourceStorageKey("system", "scope", "version", "asset"),
        ResourceStorageKey("UNKNOWN", "scope", "version", "asset"),
        ResourceStorageKey("TASK", "../scope", "version", "asset"),
        ResourceStorageKey("TASK", "scope", "version/other", "asset"),
        ResourceStorageKey("TASK", "scope", "version", "../asset"),
        ResourceStorageKey("TASK", "", "version", "asset"),
        ResourceStorageKey([], "scope", "version", "asset"),  # type: ignore[arg-type]
    ],
)
async def test_resource_key_rejects_invalid_scope_and_path_components(
    tmp_path: Path, key: ResourceStorageKey
):
    storage = LocalStorage(tmp_path)

    with pytest.raises(StorageValidationError):
        await storage.write_bytes(
            key,
            b"x",
            filename="asset.bin",
            content_type="application/octet-stream",
        )


@pytest.mark.asyncio
async def test_resource_asset_replay_conflict_restart_and_corruption(tmp_path: Path):
    key = ResourceStorageKey("TENANT", "tenant-a", "version-a", "source_bundle")
    storage = LocalStorage(tmp_path)
    original = await storage.write_bytes(
        key,
        b"trusted",
        filename="source.tar",
        content_type="application/x-tar",
    )

    assert (
        await storage.write_bytes(
            key,
            b"trusted",
            filename="source.tar",
            content_type="application/x-tar",
        )
        == original
    )
    with pytest.raises(StorageConflictError, match="immutable"):
        await storage.write_bytes(
            key,
            b"changed",
            filename="source.tar",
            content_type="application/x-tar",
        )

    restarted = LocalStorage(tmp_path)
    assert await restarted.read(key) == b"trusted"
    (restarted._logical_directory(key) / "payload").write_bytes(b"tampered")
    with pytest.raises(StorageConflictError, match="checksum"):
        await restarted.read(key)


@pytest.mark.asyncio
async def test_resource_asset_symlink_cannot_escape_root(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    storage = LocalStorage(root)
    key = ResourceStorageKey("SYSTEM", "catalog", "version", "geometry")
    (root / "resource-assets").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StorageValidationError, match="symlink"):
        await storage.write_bytes(
            key,
            b"escape",
            filename="asset.bin",
            content_type="application/octet-stream",
        )
    assert not list(outside.rglob("*"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        StorageKey("../owner", "task", "object", 1),
        StorageKey("owner", "task/other", "object", 1),
        StorageKey("owner", "task", "..", 1),
        StorageKey("owner", "task", "object", "../2"),
    ],
)
async def test_logical_key_path_traversal_is_rejected(tmp_path: Path, key: StorageKey):
    storage = LocalStorage(tmp_path)
    with pytest.raises(StorageValidationError):
        await storage.write_bytes(
            key, b"x", filename="safe.ifc", content_type="application/octet-stream"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["folder\\file.ifc", "folder/file.ifc", "bad\x00.ifc"])
async def test_filename_separators_and_nul_are_rejected(tmp_path: Path, filename: str):
    storage = LocalStorage(tmp_path)
    with pytest.raises(StorageValidationError):
        await storage.write_bytes(
            StorageKey("owner", "task", "object", 1),
            b"x",
            filename=filename,
            content_type="application/octet-stream",
        )


@pytest.mark.asyncio
async def test_type_and_size_validation_preserve_old_atomic_value_and_clean_temp(
    tmp_path: Path,
):
    storage = LocalStorage(tmp_path, max_size_bytes=3, allowed_content_types={"text/plain"})
    key = StorageKey("owner", "task", "object", 1)
    await storage.write_bytes(key, b"old", filename="value.txt", content_type="text/plain")

    with pytest.raises(StorageValidationError, match="not allowed"):
        await storage.write_bytes(
            key, b"new", filename="value.txt", content_type="application/x-executable"
        )
    with pytest.raises(StorageValidationError, match="exceeds"):
        await storage.write(
            key,
            filename="value.txt",
            content_type="text/plain",
            chunks=_chunks(b"to", b"o-long"),
        )

    assert await storage.read(key) == b"old"
    assert not list(tmp_path.rglob(".tmp-*"))


@pytest.mark.asyncio
async def test_owner_isolation_uses_distinct_logical_namespaces(tmp_path: Path):
    storage = LocalStorage(tmp_path)
    owner_a = StorageKey("owner-a", "same-task", "same-object", "v1")
    owner_b = StorageKey("owner-b", "same-task", "same-object", "v1")
    await storage.write_bytes(
        owner_a, b"private", filename="data.bin", content_type="application/octet-stream"
    )

    assert not await storage.exists(owner_b)
    with pytest.raises(StorageObjectNotFoundError):
        await storage.read(owner_b)


@pytest.mark.asyncio
async def test_full_database_owner_length_uses_safe_fixed_width_directory(tmp_path: Path):
    owner_id = "租" * 255
    storage = LocalStorage(tmp_path)
    key = StorageKey(owner_id, "task", "object", 1)

    await storage.write_bytes(
        key,
        b"private",
        filename="data.bin",
        content_type="application/octet-stream",
    )

    assert await storage.read(key) == b"private"
    assert owner_id not in str(tuple(tmp_path.rglob("*")))


@pytest.mark.asyncio
async def test_concurrent_different_content_allows_one_immutable_publication(tmp_path: Path):
    storage = LocalStorage(tmp_path)
    key = StorageKey("owner", "task", "object", 1)

    results = await asyncio.gather(
        storage.write_bytes(
            key, b"first", filename="data.bin", content_type="application/octet-stream"
        ),
        storage.write_bytes(
            key, b"second", filename="data.bin", content_type="application/octet-stream"
        ),
        return_exceptions=True,
    )
    successes = [item for item in results if not isinstance(item, BaseException)]
    conflicts = [item for item in results if isinstance(item, StorageConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 1

    payload = await storage.read(key)
    current = await storage.stat(key)
    assert payload in {b"first", b"second"}
    assert current.sha256 == hashlib.sha256(payload).hexdigest()
    assert successes == [current]

    repeated = await storage.write_bytes(
        key, payload, filename="data.bin", content_type="application/octet-stream"
    )
    assert repeated == current
    assert not list(tmp_path.rglob(".tmp-*"))


@pytest.mark.asyncio
async def test_existing_version_rejects_content_filename_or_type_changes(tmp_path: Path):
    storage = LocalStorage(tmp_path)
    key = StorageKey("owner", "task", "object", 1)
    original = await storage.write_bytes(
        key, b"original", filename="data.bin", content_type="application/octet-stream"
    )

    changes = (
        (b"changed", "data.bin", "application/octet-stream"),
        (b"original", "renamed.bin", "application/octet-stream"),
        (b"original", "data.bin", "application/pdf"),
    )
    for content, filename, content_type in changes:
        with pytest.raises(StorageConflictError, match="immutable"):
            await storage.write_bytes(
                key,
                content,
                filename=filename,
                content_type=content_type,
            )

    assert await storage.read(key) == b"original"
    assert await storage.stat(key) == original
    assert not list(tmp_path.rglob(".tmp-*"))


@pytest.mark.asyncio
async def test_payload_corruption_is_detected_before_download(tmp_path: Path):
    storage = LocalStorage(tmp_path)
    key = StorageKey("owner", "task", "object", 1)
    await storage.write_bytes(
        key,
        b"trusted",
        filename="data.bin",
        content_type="application/octet-stream",
    )
    (storage._logical_directory(key) / "payload").write_bytes(b"tampered")

    with pytest.raises(StorageConflictError, match="checksum"):
        await storage.stat(key)
    with pytest.raises(StorageConflictError, match="checksum"):
        await storage.read(key)


@pytest.mark.asyncio
async def test_verified_read_materializes_and_hashes_one_payload_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path)
    key = StorageKey("owner", "task", "object", 1)
    content = b"verified-content" * 100
    stored = await storage.write_bytes(
        key,
        content,
        filename="data.bin",
        content_type="application/octet-stream",
    )
    original_read_chunk = storage._read_chunk
    observed_bytes = 0

    def read_chunk(descriptor: int, size: int) -> bytes:
        nonlocal observed_bytes
        chunk = original_read_chunk(descriptor, size)
        observed_bytes += len(chunk)
        return chunk

    def reject_second_checksum_scan(_path: Path) -> tuple[int, str]:
        raise AssertionError("read_verified must not start a separate checksum scan")

    monkeypatch.setattr(storage, "_read_chunk", read_chunk)
    monkeypatch.setattr(storage, "_hash_file", reject_second_checksum_scan)

    verified = await storage.read_verified(key)

    assert verified.stored == stored
    assert verified.content == content
    assert verified.verified_sha256 == hashlib.sha256(content).hexdigest()
    assert observed_bytes == len(content)


@pytest.mark.asyncio
async def test_verified_read_rejects_truncation_and_metadata_digest_drift(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path)
    key = StorageKey("owner", "task", "object", 1)
    await storage.write_bytes(
        key,
        b"trusted",
        filename="data.bin",
        content_type="application/octet-stream",
    )
    directory = storage._logical_directory(key)
    (directory / "payload").write_bytes(b"short")

    with pytest.raises(StorageConflictError, match="checksum"):
        await storage.read_verified(key)

    (directory / "payload").write_bytes(b"trusted")
    metadata_path = directory / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["sha256"] = "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(StorageConflictError, match="checksum"):
        await storage.read_verified(key)


@pytest.mark.asyncio
async def test_verified_read_rejects_payload_symlink_without_reading_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"trusted")
    storage = LocalStorage(root)
    key = StorageKey("owner", "task", "object", 1)
    await storage.write_bytes(
        key,
        b"trusted",
        filename="data.bin",
        content_type="application/octet-stream",
    )
    payload = storage._logical_directory(key) / "payload"
    payload.unlink()
    payload.symlink_to(outside)

    with pytest.raises(StorageValidationError, match="symlink"):
        await storage.read_verified(key)


@pytest.mark.asyncio
async def test_verified_read_requires_a_regular_payload_file(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = StorageKey("owner", "task", "object", 1)
    await storage.write_bytes(
        key,
        b"trusted",
        filename="data.bin",
        content_type="application/octet-stream",
    )
    payload = storage._logical_directory(key) / "payload"
    payload.unlink()
    payload.mkdir()

    with pytest.raises(StorageValidationError, match="payload is not a regular file"):
        await storage.read_verified(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ("metadata.json", "payload"))
async def test_verified_read_rejects_fifo_without_waiting_for_a_writer(
    tmp_path: Path,
    filename: str,
) -> None:
    storage = LocalStorage(tmp_path)
    key = StorageKey("owner", "task", "object", 1)
    await storage.write_bytes(
        key,
        b"trusted",
        filename="data.bin",
        content_type="application/octet-stream",
    )
    target = storage._logical_directory(key) / filename
    target.unlink()
    os.mkfifo(target)

    label = "metadata" if filename == "metadata.json" else "payload"
    with pytest.raises(StorageValidationError, match=f"{label} is not a regular file"):
        await asyncio.wait_for(storage.read_verified(key), timeout=1.0)


@pytest.mark.asyncio
async def test_verified_read_rejects_metadata_symlink_without_reading_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    storage = LocalStorage(root)
    key = StorageKey("owner", "task", "object", 1)
    await storage.write_bytes(
        key,
        b"trusted",
        filename="data.bin",
        content_type="application/octet-stream",
    )
    metadata = storage._logical_directory(key) / "metadata.json"
    outside = tmp_path / "outside-metadata.json"
    outside.write_bytes(metadata.read_bytes())
    metadata.unlink()
    metadata.symlink_to(outside)

    with pytest.raises(StorageValidationError, match="metadata must not be a symlink"):
        await storage.read_verified(key)


@pytest.mark.asyncio
async def test_verified_read_rejects_ancestor_replaced_after_directory_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    storage = LocalStorage(root)
    key = StorageKey("owner", "task", "object", 1)
    await storage.write_bytes(
        key,
        b"private",
        filename="data.bin",
        content_type="application/octet-stream",
    )
    original_checked_directory = storage._checked_directory
    owner_directory = root / "owners" / storage._key_parts(key)[0]
    moved_directory = root / "detached-owner"

    def replace_ancestor_after_check(current_key):
        directory = original_checked_directory(current_key)
        owner_directory.rename(moved_directory)
        owner_directory.symlink_to(moved_directory, target_is_directory=True)
        return directory

    monkeypatch.setattr(storage, "_checked_directory", replace_ancestor_after_check)

    with pytest.raises(StorageValidationError, match="symlink or non-directory"):
        await storage.read_verified(key)


@pytest.mark.asyncio
async def test_verified_read_rejects_payload_replaced_while_fd_is_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path, max_size_bytes=3 * 1024 * 1024)
    key = StorageKey("owner", "task", "object", 1)
    content = b"a" * (1024 * 1024 + 17)
    await storage.write_bytes(
        key,
        content,
        filename="data.bin",
        content_type="application/octet-stream",
    )
    payload = storage._logical_directory(key) / "payload"
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"b" * len(content))
    original_read_chunk = storage._read_chunk
    replaced = False

    def replace_after_first_chunk(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read_chunk(descriptor, size)
        if chunk and not replaced:
            os.replace(replacement, payload)
            replaced = True
        return chunk

    monkeypatch.setattr(storage, "_read_chunk", replace_after_first_chunk)

    with pytest.raises(StorageConflictError, match="changed during verified read"):
        await storage.read_verified(key)

    assert replaced is True


@pytest.mark.asyncio
async def test_metadata_and_publish_failures_leave_old_value_and_no_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    storage = LocalStorage(tmp_path)
    old_key = StorageKey("owner", "task", "object", 1)
    await storage.write_bytes(
        old_key, b"old", filename="data.bin", content_type="application/octet-stream"
    )

    with monkeypatch.context() as patch:

        def fail_metadata(*_args, **_kwargs):
            raise OSError("metadata failure")

        patch.setattr(storage, "_write_metadata", fail_metadata)
        with pytest.raises(OSError, match="metadata failure"):
            await storage.write_bytes(
                old_key,
                b"old",
                filename="data.bin",
                content_type="application/octet-stream",
            )

    assert await storage.read(old_key) == b"old"
    assert not list(tmp_path.rglob(".tmp-*"))

    new_key = StorageKey("owner", "task", "object", 2)
    with monkeypatch.context() as patch:

        def fail_publish(*_args, **_kwargs):
            raise OSError("publish failure")

        patch.setattr(storage, "_publish", fail_publish)
        with pytest.raises(OSError, match="publish failure"):
            await storage.write_bytes(
                new_key,
                b"new",
                filename="data.bin",
                content_type="application/octet-stream",
            )

    assert await storage.read(old_key) == b"old"
    assert not await storage.exists(new_key)
    assert not list(tmp_path.rglob(".tmp-*"))


@pytest.mark.asyncio
async def test_symlink_cannot_escape_root(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "owners").mkdir(parents=True)
    outside.mkdir()
    storage = LocalStorage(root)
    key = StorageKey("owner", "task", "object", 1)
    encoded_owner = storage._key_parts(key)[0]
    (root / "owners" / encoded_owner).symlink_to(outside, target_is_directory=True)

    with pytest.raises(StorageValidationError, match="symlink"):
        await storage.write_bytes(
            key,
            b"escape",
            filename="data.bin",
            content_type="application/octet-stream",
        )
    assert not list(outside.rglob("*"))


@pytest.mark.asyncio
async def test_readiness_removes_only_stale_crash_staging(tmp_path: Path):
    storage = LocalStorage(tmp_path)
    stale = tmp_path / "owners" / ".tmp-stale"
    fresh = tmp_path / "owners" / ".tmp-fresh"
    stale.mkdir(parents=True)
    fresh.mkdir(parents=True)
    (stale / "payload").write_bytes(b"partial")
    (fresh / "payload").write_bytes(b"active")
    old = time.time() - 2 * 60 * 60
    os.utime(stale / "payload", (old, old))
    os.utime(stale, (old, old))

    await storage.verify_ready()

    assert not stale.exists()
    assert fresh.exists()
    assert not list(tmp_path.glob(".readiness-*"))
