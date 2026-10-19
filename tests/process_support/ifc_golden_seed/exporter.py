"""Atomic test-only exporter for immutable M4/IFC Golden Seed fixtures."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import unicodedata
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import rfc8785

from tests.process_support.ifc_golden_seed.artifact_inventory import (
    ArtifactReader,
    materialize_artifact_inventory,
)
from tests.process_support.ifc_golden_seed.closure import (
    SeedClosure,
    SeedExportScope,
    SeedSnapshot,
    build_export_closure,
)
from tests.process_support.ifc_golden_seed.core import (
    SeedValidationError,
    load_manifest,
    seal_manifest,
    verify_seed,
    write_manifest_exclusive,
)


class SeedExportError(RuntimeError):
    """A Seed could not be safely or atomically exported."""


@dataclass(frozen=True, slots=True)
class SeedExportMetadata:
    source_commit: str
    source_migration_head: str
    source_m4_round: str
    source_arm: str
    source_direct_session_alias: str
    created_at: datetime | str
    seed_kind: Literal["m4_run", "ifc_stage"] = "m4_run"
    parent_seed_sha256: str | None = None
    m4_run_seed_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ExportedSeed:
    root_path: Path
    manifest: dict[str, object]
    database_rows_path: Path
    closure: SeedClosure


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise SeedExportError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or "\x00" in normalized:
        raise SeedExportError(f"{field} is empty or invalid")
    return normalized


def _created_at(value: datetime | str) -> str:
    if isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return _required_text(value, "created_at")


def _canonical_json(value: object) -> bytes:
    try:
        return rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, TypeError, ValueError) as exc:
        raise SeedExportError("database closure is outside the canonical JSON domain") from exc


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


_SCOPE_PLACEHOLDERS = {
    "tenant_id": "__seed_tenant__",
    "owner_id": "__seed_owner__",
    "task_id": "__seed_task__",
    "run_id": "__seed_run__",
    "created_by_run_id": "__seed_run__",
    "storage_key": "__seed_storage_key__",
}


def normalize_database_rows(document: Mapping[str, object]) -> dict[str, object]:
    """Replace target-specific identity fields while retaining exact resource identities."""

    def normalize(value: object) -> object:
        if isinstance(value, Mapping):
            result: dict[str, object] = {}
            for key, nested in value.items():
                if key in _SCOPE_PLACEHOLDERS:
                    result[key] = _SCOPE_PLACEHOLDERS[key]
                else:
                    result[str(key)] = normalize(nested)
            return result
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        return value

    return normalize(deepcopy(dict(document)))  # type: ignore[return-value]


def canonical_database_rows_bytes(document: Mapping[str, object]) -> bytes:
    return _canonical_json(normalize_database_rows(document))


def canonical_database_rows_digest(document: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_database_rows_bytes(document)).hexdigest()


def _write_exclusive(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise SeedExportError("refusing to overwrite a Seed object") from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    for directory, _subdirectories, _files in os.walk(root, topdown=False):
        _fsync_directory(Path(directory))


def _publish_directory_exclusive(staging: Path, output: Path) -> None:
    lock_path = output.parent / f".{output.name}.publish-lock"
    lock_descriptor = -1
    try:
        lock_descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        if output.exists() or output.is_symlink():
            raise SeedExportError("refusing to overwrite an existing Seed root")
        os.rename(staging, output)
        _fsync_directory(output.parent)
    except FileExistsError as exc:
        raise SeedExportError("another publication owns the Seed destination") from exc
    finally:
        if lock_descriptor >= 0:
            os.close(lock_descriptor)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            _fsync_directory(output.parent)


def _copy_seed_objects(source_root: Path, staging_root: Path, inventory: list[object]) -> None:
    """Copy an already verified inventory into a private publication tree."""

    for raw_item in inventory:
        if not isinstance(raw_item, Mapping):
            raise SeedExportError("Seed inventory contains an invalid object record")
        relative = raw_item.get("path")
        if not isinstance(relative, str) or not relative:
            raise SeedExportError("Seed inventory contains an invalid object path")
        source = source_root / relative
        target = staging_root / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        source_descriptor = -1
        target_descriptor = -1
        try:
            source_descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            target_descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            while True:
                chunk = os.read(source_descriptor, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(target_descriptor, view)
                    view = view[written:]
            os.fsync(target_descriptor)
        except OSError as exc:
            raise SeedExportError("Seed record publication could not copy an object") from exc
        finally:
            if source_descriptor >= 0:
                os.close(source_descriptor)
            if target_descriptor >= 0:
                os.close(target_descriptor)


def publish_seed_record(
    *,
    source_root: Path,
    destination_parent: Path,
    manifest: Mapping[str, object],
) -> Path:
    """Publish one new immutable manifest record over an unchanged object inventory.

    Stage evidence and state transitions change the manifest digest while retaining
    the exact exported database and Artifact bytes.  Each record receives its own
    digest-addressed directory; no existing Seed directory or manifest is replaced.
    """

    source = Path(source_root)
    parent = Path(destination_parent)
    try:
        parent_info = parent.lstat()
    except OSError as exc:
        raise SeedExportError("Seed destination root is unavailable") from exc
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise SeedExportError("Seed destination root must be a real directory")

    source_manifest = load_manifest(source / "manifest.json")
    verify_seed(source_manifest, source)
    if manifest.get("artifact_inventory_digest") != source_manifest.get(
        "artifact_inventory_digest"
    ) or manifest.get("object_inventory") != source_manifest.get("object_inventory"):
        raise SeedExportError("Seed record transition changed the object inventory")

    root_sha256 = manifest.get("root_sha256")
    if not isinstance(root_sha256, str):
        raise SeedExportError("Seed record is not sealed")
    output = parent / root_sha256
    if output.exists() or output.is_symlink():
        raise SeedExportError("refusing to overwrite an existing digest-addressed Seed")

    wrapper = Path(tempfile.mkdtemp(prefix=".ifc-stage-record-", dir=parent))
    staging = wrapper / "record"
    published = False
    try:
        staging.mkdir(mode=0o700)
        inventory = manifest.get("object_inventory")
        if not isinstance(inventory, list):
            raise SeedExportError("Seed record inventory is invalid")
        _copy_seed_objects(source, staging, inventory)
        write_manifest_exclusive(staging / "manifest.json", manifest)
        verify_seed(manifest, staging)
        _fsync_tree(staging)
        _publish_directory_exclusive(staging, output)
        published = True
        return output.absolute()
    except (OSError, SeedValidationError) as exc:
        raise SeedExportError("Seed record publication failed integrity checks") from exc
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(wrapper, ignore_errors=True)


def _real_parent(output_root: Path) -> Path:
    parent = output_root.parent
    try:
        info = parent.lstat()
    except OSError as exc:
        raise SeedExportError("Seed output parent does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SeedExportError("Seed output parent must be a real directory")
    return parent.absolute()


def _database_document_with_paths(
    closure: SeedClosure,
    scope: SeedExportScope,
    object_paths: dict[tuple[str, str], str],
) -> dict[str, object]:
    document = closure.database_document(scope)
    versions: list[dict[str, object]] = []
    for row in document["artifact_versions"]:
        exact = (row["artifact_id"], row["version_id"])
        object_path = object_paths.get(exact)
        if object_path is None:
            raise SeedExportError("database artifact version has no materialized object")
        versions.append({**row, "object_path": object_path})
    document["artifact_versions"] = versions
    return document


def _manifest(
    *,
    metadata: SeedExportMetadata,
    scope: SeedExportScope,
    closure: SeedClosure,
    inventory: list[dict[str, object]],
    database_rows_digest: str,
) -> dict[str, object]:
    normalized_rows = normalize_database_rows(closure.database_document(scope))
    manifest: dict[str, object] = {
        "format_version": "ifc-golden-seed/v1",
        "seed_kind": metadata.seed_kind,
        "state": "candidate",
        "source_commit": _required_text(metadata.source_commit, "source_commit"),
        "source_migration_head": _required_text(
            metadata.source_migration_head, "source_migration_head"
        ),
        "source_m4_round": _required_text(metadata.source_m4_round, "source_m4_round"),
        "source_arm": _required_text(metadata.source_arm, "source_arm"),
        "source_direct_session_alias": _required_text(
            metadata.source_direct_session_alias, "source_direct_session_alias"
        ),
        "source_task_id": scope.task_id,
        "source_run_id": scope.run_id,
        "source_catalog_frontier": closure.catalog_frontier,
        "source_operation_frontier": closure.operation_frontier,
        "source_catalog_frontier_digest": _sha256_json(
            normalized_rows["catalog_entries"]
        ),
        "source_operation_frontier_digest": _sha256_json(
            normalized_rows["operations"]
        ),
        "created_at": _created_at(metadata.created_at),
        "database_rows_digest": database_rows_digest,
        "object_inventory": inventory,
    }
    if metadata.seed_kind == "ifc_stage":
        manifest["parent_seed_sha256"] = metadata.parent_seed_sha256
        manifest["m4_run_seed_sha256"] = metadata.m4_run_seed_sha256
    elif metadata.parent_seed_sha256 is not None or metadata.m4_run_seed_sha256 is not None:
        raise SeedExportError("M4 Run Seed metadata must not contain IFC parent digests")
    return seal_manifest(manifest)


async def export_seed(
    *,
    snapshot: SeedSnapshot,
    artifact_reader: ArtifactReader,
    output_root: Path,
    scope: SeedExportScope,
    metadata: SeedExportMetadata,
    catalog_frontier: int | None = None,
    operation_frontier: int | None = None,
    include_ifc: bool = True,
) -> ExportedSeed:
    """Export a closed snapshot and publish it only after complete verification."""

    output = Path(output_root)
    parent = _real_parent(output)
    if output.exists() or output.is_symlink():
        raise SeedExportError("refusing to overwrite an existing Seed root")
    closure = build_export_closure(
        snapshot,
        scope,
        catalog_frontier=catalog_frontier,
        operation_frontier=operation_frontier,
        include_ifc=include_ifc,
    )
    staging = Path(tempfile.mkdtemp(prefix=".ifc-golden-seed-", dir=parent))
    published = False
    try:
        artifact_result = await materialize_artifact_inventory(
            closure, artifact_reader, staging
        )
        database_document = normalize_database_rows(
            _database_document_with_paths(
                closure, scope, dict(artifact_result.object_paths)
            )
        )
        database_bytes = _canonical_json(database_document)
        database_relative_path = "objects/database_rows.json"
        _write_exclusive(staging / database_relative_path, database_bytes)
        database_digest = hashlib.sha256(database_bytes).hexdigest()
        inventory = [*artifact_result.inventory]
        inventory.append(
            {
                "path": database_relative_path,
                "filename": "database_rows.json",
                "media_type": "application/json",
                "size": len(database_bytes),
                "sha256": database_digest,
                "reverse_references": ["database_closure"],
            }
        )
        sealed = _manifest(
            metadata=metadata,
            scope=scope,
            closure=closure,
            inventory=inventory,
            database_rows_digest=database_digest,
        )
        write_manifest_exclusive(staging / "manifest.json", sealed)
        verify_seed(sealed, staging)
        _fsync_tree(staging)
        _publish_directory_exclusive(staging, output)
        published = True
        return ExportedSeed(
            root_path=output.absolute(),
            manifest=sealed,
            database_rows_path=output.absolute() / database_relative_path,
            closure=closure,
        )
    except (OSError, SeedValidationError) as exc:
        raise SeedExportError("Seed export failed integrity or publication checks") from exc
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)


async def export_postgres_seed(
    *,
    snapshot_reader: object,
    artifact_reader: ArtifactReader,
    output_root: Path,
    scope: SeedExportScope,
    metadata: SeedExportMetadata,
    catalog_frontier: int | None = None,
    operation_frontier: int | None = None,
    include_ifc: bool = True,
) -> ExportedSeed:
    """Load through a PostgreSQL snapshot reader, then use the common exporter."""

    load = getattr(snapshot_reader, "load", None)
    if load is None:
        raise SeedExportError("snapshot_reader must provide an async load(scope) method")
    snapshot = await load(scope)
    if not isinstance(snapshot, SeedSnapshot):
        raise SeedExportError("snapshot_reader returned an unsupported snapshot")
    return await export_seed(
        snapshot=snapshot,
        artifact_reader=artifact_reader,
        output_root=output_root,
        scope=scope,
        metadata=metadata,
        catalog_frontier=catalog_frontier,
        operation_frontier=operation_frontier,
        include_ifc=include_ifc,
    )


def _required_source_label(environment_name: str) -> str:
    value = os.environ.get(environment_name)
    if value is None:
        raise SeedExportError(f"{environment_name} must explicitly identify the source")
    return _required_text(value, environment_name)


def _required_source_commit() -> str:
    value = _required_source_label("CAD2BIM_I0_SOURCE_COMMIT")
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise SeedExportError("CAD2BIM_I0_SOURCE_COMMIT must be a lowercase full commit id")
    return value


async def export_run_fixture(
    source_database_url: str,
    source_storage_root: str | Path,
    seed_root: str | Path,
    source_run_id: str,
    catalog_frontier: int,
    operation_frontier: int,
) -> ExportedSeed:
    """Resolve and export one real Direct Run without publishing its DSN or source path."""

    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
    from tests.process_support.ifc_golden_seed.artifact_inventory import (
        StoragePortArtifactReader,
    )
    from tests.process_support.ifc_golden_seed.postgres_adapter import (
        PostgresSeedSnapshotReader,
    )

    try:
        url = make_url(source_database_url)
    except Exception as exc:
        raise SeedExportError("source database URL is invalid") from exc
    if url.drivername != "postgresql+psycopg":
        raise SeedExportError("source database URL must use postgresql+psycopg")
    storage_root = Path(source_storage_root)
    try:
        storage_info = storage_root.lstat()
    except OSError as exc:
        raise SeedExportError("source storage root is unavailable") from exc
    if stat.S_ISLNK(storage_info.st_mode) or not stat.S_ISDIR(storage_info.st_mode):
        raise SeedExportError("source storage root must be a real directory")
    destination_parent = Path(seed_root)
    try:
        destination_info = destination_parent.lstat()
    except OSError as exc:
        raise SeedExportError("Seed destination root is unavailable") from exc
    if stat.S_ISLNK(destination_info.st_mode) or not stat.S_ISDIR(destination_info.st_mode):
        raise SeedExportError("Seed destination root must be a real directory")

    engine = create_async_engine(source_database_url, pool_pre_ping=True)
    wrapper_root: Path | None = None
    try:
        snapshot_reader = PostgresSeedSnapshotReader(engine)
        resolved, snapshot = await snapshot_reader.resolve_and_load(source_run_id)
        storage = LocalStorage(
            storage_root,
            max_size_bytes=512 * 1024 * 1024,
        )
        artifact_reader = StoragePortArtifactReader(storage, resolved.scope)
        metadata = SeedExportMetadata(
            source_commit=_required_source_commit(),
            source_migration_head=resolved.migration_head,
            source_m4_round=_required_source_label("CAD2BIM_I0_SOURCE_M4_ROUND"),
            source_arm=_required_source_label("CAD2BIM_I0_SOURCE_ARM"),
            source_direct_session_alias=resolved.direct_session_alias,
            created_at=datetime.now(timezone.utc),
        )
        wrapper_root = Path(
            tempfile.mkdtemp(prefix=".ifc-golden-export-", dir=destination_parent)
        )
        exported = await export_seed(
            snapshot=snapshot,
            artifact_reader=artifact_reader,
            output_root=wrapper_root / "candidate",
            scope=resolved.scope,
            metadata=metadata,
            catalog_frontier=catalog_frontier,
            operation_frontier=operation_frontier,
            include_ifc=True,
        )
        final_root = destination_parent / str(exported.manifest["root_sha256"])
        if final_root.exists() or final_root.is_symlink():
            raise SeedExportError("refusing to overwrite an existing digest-addressed Seed")
        _fsync_tree(exported.root_path)
        _publish_directory_exclusive(exported.root_path, final_root)
        return replace(
            exported,
            root_path=final_root.absolute(),
            database_rows_path=final_root.absolute() / "objects/database_rows.json",
        )
    finally:
        await engine.dispose()
        if wrapper_root is not None:
            shutil.rmtree(wrapper_root, ignore_errors=True)


__all__ = [
    "ExportedSeed",
    "SeedExportError",
    "SeedExportMetadata",
    "canonical_database_rows_bytes",
    "canonical_database_rows_digest",
    "export_postgres_seed",
    "export_run_fixture",
    "export_seed",
    "normalize_database_rows",
    "publish_seed_record",
]
