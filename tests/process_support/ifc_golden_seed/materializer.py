"""Restore one verified Golden Seed into a fresh Direct-session scope."""

from __future__ import annotations

import asyncio
import hashlib
import json
import stat
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, ContextManager
from uuid import uuid4

from sqlalchemy import DateTime, func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.data_runtime_service import (
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.domain.data_runtime import (
    CatalogResourceKind,
    DirectSessionBinding,
)
from CAD2BIM_backend.domain.identity import (
    TenantExecutionContext,
)
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactModel,
    ArtifactVersionModel,
    CriticalOperationInputModel,
    CriticalOperationModel,
    DataCatalogEntryModel,
    DataPackageMemberModel,
    DataPackageModel,
    DataQueryAttemptModel,
    DataQueryInputModel,
    DirectSessionBindingModel,
    IfcModelModel,
    IfcModelRevisionModel,
    IdentityMigrationQuarantineModel,
    PrincipalModel,
    RunModel,
    TaskModel,
    TaskTenantBindingModel,
    TenantModel,
    TenantPrincipalModel,
    UserModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository_base import artifact_storage_key
from CAD2BIM_backend.infrastructure.postgres.identity_directory import (
    PostgresIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.postgres.repository import (
    PostgresControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantAwareAsyncSession,
    bind_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from CAD2BIM_backend.ports.data_runtime import DataRuntimeRepository
from CAD2BIM_backend.ports.identity import IdentityDirectory
from CAD2BIM_backend.ports.storage import StorageKey
from tests.process_support.ifc_golden_seed.core import (
    CHUNK_BYTES,
    load_manifest,
    verify_seed,
)
from tests.process_support.ifc_golden_seed.closure import (
    SeedClosureError,
    SeedExportScope,
    SeedSnapshot,
    build_export_closure,
)
from tests.process_support.ifc_golden_seed.exporter import (
    canonical_database_rows_digest,
)
from tests.process_support.ifc_revision_runtime import build_in_process_ifc_stack


_DATABASE_ROWS_PATH = "objects/database_rows.json"
_DATABASE_ROWS_FIELDS = {
    "scope",
    "catalog_frontier",
    "operation_frontier",
    "catalog_entries",
    "packages",
    "package_members",
    "operations",
    "operation_inputs",
    "artifacts",
    "artifact_versions",
    "ifc_models",
    "ifc_revisions",
}
_ROW_COLLECTIONS = _DATABASE_ROWS_FIELDS - {
    "scope",
    "catalog_frontier",
    "operation_frontier",
}
_SCOPE_FIELDS = {"tenant_id", "owner_id", "task_id", "run_id"}
_NORMALIZED_SCOPE = {
    "tenant_id": "__seed_tenant__",
    "owner_id": "__seed_owner__",
    "task_id": "__seed_task__",
    "run_id": "__seed_run__",
}
_EMPTY_TARGET_MODELS = (
    PrincipalModel,
    TenantModel,
    TenantPrincipalModel,
    IdentityMigrationQuarantineModel,
    UserModel,
    TaskTenantBindingModel,
    DirectSessionBindingModel,
    TaskModel,
    RunModel,
    ArtifactModel,
    ArtifactVersionModel,
    DataPackageModel,
    DataPackageMemberModel,
    CriticalOperationModel,
    CriticalOperationInputModel,
    IfcModelModel,
    IfcModelRevisionModel,
    DataCatalogEntryModel,
    DataQueryAttemptModel,
    DataQueryInputModel,
)

ScopeBinder = Callable[[TenantExecutionContext], ContextManager[None]]


class MaterializationError(ValueError):
    """Raised when a Seed cannot be safely restored into an isolated target."""


@dataclass(frozen=True, slots=True)
class MaterializedSeed:
    """Fresh Direct binding and formally resolved scopes for one restored copy."""

    binding: DirectSessionBinding
    configured_subject: str
    invocation_scope: InvocationScope
    ifc_scope: IfcRunIdentity
    storage: LocalStorage
    source_root_sha256: str
    artifact_count: int
    package_count: int
    operation_count: int
    catalog_entry_count: int
    ifc_revision_count: int


@dataclass(frozen=True, slots=True)
class _Rows:
    scope: Mapping[str, str]
    catalog_frontier: int
    operation_frontier: int
    collections: Mapping[str, tuple[dict[str, Any], ...]]


async def materialize_seed(
    seed_root: Path,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    admin_session_factory: async_sessionmaker[AsyncSession] | None = None,
    repository: DataRuntimeRepository,
    identity_directory: IdentityDirectory,
    storage_root: Path,
    configured_subject: str,
    session_name: str,
    scope_binder: ScopeBinder = bind_tenant_execution_context,
) -> MaterializedSeed:
    """Verify and restore a Seed without copying its source Direct binding.

    The target database must contain no identity or business rows, and
    ``storage_root`` must be empty.
    Artifact bytes are published before one transaction inserts all restored rows.
    The fresh Direct alias is returned only after formal application/repository reads.
    """

    root = Path(seed_root)
    manifest = load_manifest(root / "manifest.json")
    verify_seed(manifest, root)
    rows = _load_database_rows(root, manifest)
    _require_manifest_scope(manifest, rows)
    await _require_empty_database(admin_session_factory or session_factory)
    storage = _prepare_empty_storage(storage_root)

    sessions = DirectSessionService(
        repository,
        identity_directory,
        configured_subject,
        scope_binder,
    )
    opened = await sessions.open_direct_session(session_name)
    if opened.resumed:
        raise MaterializationError("target Direct session must be newly created")

    resolver = DirectScopeResolver(
        repository,
        identity_directory,
        configured_subject,
        scope_binder,
    )
    invocation = await resolver.resolve(
        opened.binding.direct_session_alias,
        command_id=f"seed-target-resolve-{uuid4().hex}",
    )
    if invocation.identity.task_id is None:
        raise MaterializationError("fresh Direct scope did not resolve a Task")
    ifc_scope = IfcRunIdentity(
        owner_id=invocation.owner_id,
        tenant_id=invocation.identity.tenant_id,
        task_id=invocation.identity.task_id,
        run_id=invocation.run_id,
    )
    target_scope = {
        "owner_id": ifc_scope.owner_id,
        "tenant_id": ifc_scope.tenant_id,
        "task_id": ifc_scope.task_id,
        "run_id": ifc_scope.run_id,
    }
    rebound = _rebind_rows(rows.collections, rows.scope, target_scope)

    await _publish_artifact_bytes(
        root=root,
        manifest=manifest,
        source_rows=rows.collections,
        rebound_rows=rebound,
        storage=storage,
        target_scope=target_scope,
    )
    with scope_binder(invocation.identity):
        await _insert_restored_rows(session_factory, rebound)
        await _formal_readback(
            repository=repository,
            storage=storage,
            invocation=invocation,
            ifc_scope=ifc_scope,
            rebound=rebound,
        )
        restored_digest, restored_document = await _restored_database_rows_digest(
            session_factory=session_factory,
            target_scope=target_scope,
            catalog_frontier=rows.catalog_frontier,
            operation_frontier=rows.operation_frontier,
            rebound=rebound,
        )
        if restored_digest != manifest["database_rows_digest"]:
            expected_document: dict[str, object] = {
                "scope": target_scope,
                "catalog_frontier": rows.catalog_frontier,
                "operation_frontier": rows.operation_frontier,
                **rebound,
            }
            mismatched_sections = [
                section
                for section in sorted(expected_document)
                if canonical_database_rows_digest(
                    {"section": expected_document[section]}
                )
                != canonical_database_rows_digest(
                    {"section": restored_document[section]}
                )
            ]
            first_difference = _first_difference_path(
                expected_document,
                restored_document,
            )
            raise MaterializationError(
                "normalized database rows digest changed after formal readback: "
                + ", ".join(mismatched_sections)
                + f"; first difference at {first_difference}"
            )

    return MaterializedSeed(
        binding=opened.binding,
        configured_subject=configured_subject,
        invocation_scope=invocation,
        ifc_scope=ifc_scope,
        storage=storage,
        source_root_sha256=manifest["root_sha256"],
        artifact_count=len(rebound["artifacts"]),
        package_count=len(rebound["packages"]),
        operation_count=len(rebound["operations"]),
        catalog_entry_count=len(rebound["catalog_entries"]),
        ifc_revision_count=len(rebound["ifc_revisions"]),
    )


async def restore_run_fixture(
    seed_root: Path,
    target_database_url: str,
    target_storage_root: Path,
    target_session_name: str,
) -> MaterializedSeed:
    """Compose an isolated PostgreSQL/LocalStorage restore for maintenance callers."""

    engine = create_async_engine(target_database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(
        engine,
        class_=TenantAwareAsyncSession,
        expire_on_commit=False,
    )
    repository = PostgresControlPlaneRepository(sessions)
    configured_subject = f"ifc-seed-restore-{uuid4().hex}"
    try:
        return await materialize_seed(
            seed_root,
            session_factory=sessions,
            repository=repository,
            identity_directory=PostgresIdentityDirectory(sessions),
            storage_root=target_storage_root,
            configured_subject=configured_subject,
            session_name=target_session_name,
        )
    finally:
        await engine.dispose()


def _load_database_rows(root: Path, manifest: Mapping[str, Any]) -> _Rows:
    inventory = manifest["object_inventory"]
    matches = [item for item in inventory if item["path"] == _DATABASE_ROWS_PATH]
    if len(matches) != 1:
        raise MaterializationError("Seed must contain exactly one canonical database rows object")
    item = matches[0]
    if item["media_type"] != "application/json":
        raise MaterializationError("database rows object must use application/json")
    expected_digest = manifest.get("database_rows_digest")
    if expected_digest != item["sha256"]:
        raise MaterializationError("database rows digest does not identify its inventory object")
    path = root.joinpath(*PurePosixPath(_DATABASE_ROWS_PATH).parts)
    try:
        payload = path.read_bytes()
        document = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaterializationError("database rows object is unreadable") from exc
    if hashlib.sha256(payload).hexdigest() != expected_digest:
        raise MaterializationError("database rows bytes changed after Seed verification")
    if not isinstance(document, dict) or set(document) != _DATABASE_ROWS_FIELDS:
        raise MaterializationError("database rows object does not match the closed restore schema")
    scope = document["scope"]
    if (
        not isinstance(scope, dict)
        or set(scope) != _SCOPE_FIELDS
        or scope != _NORMALIZED_SCOPE
    ):
        raise MaterializationError("database rows source scope is not normalized")
    collections: dict[str, tuple[dict[str, Any], ...]] = {}
    for name in _ROW_COLLECTIONS:
        value = document[name]
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise MaterializationError(f"database rows collection {name} is invalid")
        collections[name] = tuple(dict(row) for row in value)
    if canonical_database_rows_digest(document) != expected_digest:
        raise MaterializationError("database rows object is not the canonical normalized record")
    _validate_normalized_source_rows(collections, scope)
    catalog_frontier = _nonnegative_int(document["catalog_frontier"], "catalog_frontier")
    operation_frontier = _nonnegative_int(
        document["operation_frontier"], "operation_frontier"
    )
    _validate_export_closure(
        collections,
        catalog_frontier=catalog_frontier,
        operation_frontier=operation_frontier,
    )
    return _Rows(
        scope=scope,
        catalog_frontier=catalog_frontier,
        operation_frontier=operation_frontier,
        collections=collections,
    )


def _validate_normalized_source_rows(
    collections: Mapping[str, Sequence[Mapping[str, Any]]],
    source_scope: Mapping[str, str],
) -> None:
    for name, rows in collections.items():
        for row in rows:
            for field in ("owner_id", "tenant_id", "task_id", "run_id"):
                if field in row and row[field] != source_scope[field]:
                    raise MaterializationError(f"{name}.{field} is not scope-normalized")
            if "created_by_run_id" in row and row["created_by_run_id"] != source_scope["run_id"]:
                raise MaterializationError(
                    f"{name}.created_by_run_id is not scope-normalized"
                )
    for row in collections["artifact_versions"]:
        if row.get("storage_key") != "__seed_storage_key__":
            raise MaterializationError("artifact version storage_key is not normalized")


def _validate_export_closure(
    collections: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    catalog_frontier: int,
    operation_frontier: int,
) -> None:
    snapshot = SeedSnapshot.from_rows(
        catalog_entries=collections["catalog_entries"],
        packages=collections["packages"],
        package_members=collections["package_members"],
        operations=collections["operations"],
        operation_inputs=collections["operation_inputs"],
        artifacts=collections["artifacts"],
        artifact_versions=collections["artifact_versions"],
        ifc_models=collections["ifc_models"],
        ifc_revisions=collections["ifc_revisions"],
    )
    scope = SeedExportScope.normalized(
        tenant_id=_NORMALIZED_SCOPE["tenant_id"],
        principal_id="__seed_principal__",
        owner_id=_NORMALIZED_SCOPE["owner_id"],
        task_id=_NORMALIZED_SCOPE["task_id"],
        run_id=_NORMALIZED_SCOPE["run_id"],
    )
    try:
        closure = build_export_closure(
            snapshot,
            scope,
            catalog_frontier=catalog_frontier,
            operation_frontier=operation_frontier,
            include_ifc=True,
        )
    except SeedClosureError as exc:
        raise MaterializationError("database rows do not form a closed frontier") from exc
    for collection in _ROW_COLLECTIONS:
        if len(getattr(closure, collection)) != len(collections[collection]):
            raise MaterializationError("database rows contain resources outside the frontier")


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MaterializationError(f"{field} must be a non-negative integer")
    return value


def _require_manifest_scope(manifest: Mapping[str, Any], rows: _Rows) -> None:
    if not manifest.get("source_task_id") or not manifest.get("source_run_id"):
        raise MaterializationError("manifest source Task/Run provenance is incomplete")
    if manifest.get("source_catalog_frontier") != rows.catalog_frontier:
        raise MaterializationError("manifest Catalog frontier does not match database rows")
    if manifest.get("source_operation_frontier") != rows.operation_frontier:
        raise MaterializationError("manifest Operation frontier does not match database rows")


def _prepare_empty_storage(storage_root: Path) -> LocalStorage:
    root = Path(storage_root)
    try:
        mode = root.lstat().st_mode
    except FileNotFoundError:
        mode = None
    except OSError as exc:
        raise MaterializationError("target storage root is unreadable") from exc
    if mode is not None:
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise MaterializationError("target storage root must be a real directory")
        try:
            first_entry = next(root.iterdir(), None)
        except OSError as exc:
            raise MaterializationError("target storage root is unreadable") from exc
        if first_entry is not None:
            raise MaterializationError("target storage root must be empty")
    else:
        try:
            root.mkdir(parents=True, mode=0o700)
        except OSError as exc:
            raise MaterializationError("target storage root could not be created") from exc
    return LocalStorage(root, max_size_bytes=512 * 1024 * 1024)


async def _require_empty_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Prove global emptiness with RLS disabled, or fail closed."""

    try:
        async with session_factory() as session, session.begin():
            await session.execute(text("SET LOCAL row_security = off"))
            for model in _EMPTY_TARGET_MODELS:
                count = await session.scalar(select(func.count()).select_from(model))
                if count:
                    raise MaterializationError("target database is not empty")
    except MaterializationError:
        raise
    except SQLAlchemyError as exc:
        raise MaterializationError(
            "target database emptiness could not be proven with RLS disabled"
        ) from exc


def _rebind_rows(
    collections: Mapping[str, Sequence[Mapping[str, Any]]],
    source_scope: Mapping[str, str],
    target_scope: Mapping[str, str],
) -> dict[str, tuple[dict[str, Any], ...]]:
    rebound: dict[str, tuple[dict[str, Any], ...]] = {}
    for name, source_rows in collections.items():
        target_rows: list[dict[str, Any]] = []
        for source in source_rows:
            row = dict(source)
            for field in ("owner_id", "tenant_id", "task_id", "run_id"):
                if field in row:
                    if row[field] != source_scope[field]:
                        raise MaterializationError(f"{name}.{field} escapes the source scope")
                    row[field] = target_scope[field]
            if "created_by_run_id" in row:
                if row["created_by_run_id"] != source_scope["run_id"]:
                    raise MaterializationError(f"{name}.created_by_run_id escapes the source Run")
                row["created_by_run_id"] = target_scope["run_id"]
            target_rows.append(row)
        rebound[name] = tuple(target_rows)

    for row in rebound["artifact_versions"]:
        row["storage_key"] = artifact_storage_key(
            target_scope["owner_id"],
            target_scope["task_id"],
            row["artifact_id"],
            row["version_no"],
        )
    return rebound


async def _publish_artifact_bytes(
    *,
    root: Path,
    manifest: Mapping[str, Any],
    source_rows: Mapping[str, Sequence[Mapping[str, Any]]],
    rebound_rows: Mapping[str, Sequence[Mapping[str, Any]]],
    storage: LocalStorage,
    target_scope: Mapping[str, str],
) -> None:
    inventory_by_path = {item["path"]: item for item in manifest["object_inventory"]}
    identified_inventory: set[tuple[str, str]] = set()
    for item in manifest["object_inventory"]:
        artifact_id = item.get("source_artifact_id")
        version_id = item.get("source_artifact_version_id")
        if (artifact_id is None) != (version_id is None):
            raise MaterializationError("artifact inventory contains a partial exact identity")
        if artifact_id is not None:
            exact = (artifact_id, version_id)
            if exact in identified_inventory:
                raise MaterializationError("artifact inventory repeats an exact version")
            identified_inventory.add(exact)
    expected_inventory = {
        (row["artifact_id"], row["version_id"])
        for row in source_rows["artifact_versions"]
    }
    if identified_inventory != expected_inventory:
        raise MaterializationError("artifact inventory does not equal the database byte closure")
    rebound_by_source_version = {
        source["version_id"]: rebound
        for source, rebound in zip(
            source_rows["artifact_versions"], rebound_rows["artifact_versions"], strict=True
        )
    }
    source_by_version = {
        row["version_id"]: row for row in source_rows["artifact_versions"]
    }
    if len(source_by_version) != len(source_rows["artifact_versions"]):
        raise MaterializationError("database rows repeat an ArtifactVersion identity")
    inventory_versions: set[str] = set()
    for source in source_rows["artifact_versions"]:
        object_path = source.get("object_path")
        if not isinstance(object_path, str) or object_path not in inventory_by_path:
            raise MaterializationError("artifact version does not identify an inventory object")
        item = inventory_by_path[object_path]
        if (
            item.get("source_artifact_id") != source["artifact_id"]
            or item.get("source_artifact_version_id") != source["version_id"]
            or item["filename"] != source["filename"]
            or item["media_type"] != source["content_type"]
            or item["size"] != source["size_bytes"]
            or item["sha256"] != source["sha256"]
        ):
            raise MaterializationError("artifact inventory does not match its exact version row")
        if source["version_id"] in inventory_versions:
            raise MaterializationError("artifact version is mapped to multiple objects")
        inventory_versions.add(source["version_id"])

    for source_version_id, rebound in rebound_by_source_version.items():
        source = source_by_version[source_version_id]
        object_path = source["object_path"]
        path = root.joinpath(*PurePosixPath(object_path).parts)
        key = StorageKey(
            owner_id=target_scope["owner_id"],
            task_id=target_scope["task_id"],
            object_id=rebound["artifact_id"],
            version=rebound["version_no"],
        )
        stored = await storage.write(
            key,
            filename=rebound["filename"],
            content_type=rebound["content_type"],
            chunks=_file_chunks(path),
        )
        if (
            stored.filename != rebound["filename"]
            or stored.content_type != rebound["content_type"]
            or stored.size_bytes != rebound["size_bytes"]
            or stored.sha256 != rebound["sha256"]
        ):
            raise MaterializationError("target storage publication changed artifact bytes")


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as stream:
        while True:
            chunk = await asyncio.to_thread(stream.read, CHUNK_BYTES)
            if not chunk:
                return
            yield chunk


def _model_row(
    model: type[Any],
    row: Mapping[str, Any],
    *,
    extras: frozenset[str] = frozenset(),
) -> Any:
    columns = {column.name: column for column in model.__table__.columns}
    unknown = set(row) - set(columns) - extras
    if unknown:
        raise MaterializationError(
            f"{model.__tablename__} row contains unsupported fields: {sorted(unknown)}"
        )
    values: dict[str, Any] = {}
    for name, value in row.items():
        if name in extras:
            continue
        column = columns[name]
        if isinstance(column.type, DateTime) and isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise MaterializationError(
                    f"{model.__tablename__}.{name} is not an RFC3339 datetime"
                ) from exc
        values[name] = value
    return model(**values)


async def _insert_restored_rows(
    session_factory: async_sessionmaker[AsyncSession],
    rows: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
        artifact_models = [
            _model_row(ArtifactModel, {**row, "current_version_id": None})
            for row in rows["artifacts"]
        ]
        desired_current = {
            row["artifact_id"]: row.get("current_version_id") for row in rows["artifacts"]
        }
        desired_updated_at = {
            model.artifact_id: model.updated_at for model in artifact_models
        }
        session.add_all(artifact_models)
        await session.flush()
        session.add_all(
            _model_row(ArtifactVersionModel, row, extras=frozenset({"object_path"}))
            for row in rows["artifact_versions"]
        )
        session.add_all(_model_row(DataPackageModel, row) for row in rows["packages"])
        await session.flush()
        session.add_all(
            _model_row(DataPackageMemberModel, row) for row in rows["package_members"]
        )
        session.add_all(_model_row(IfcModelModel, row) for row in rows["ifc_models"])
        await session.flush()

        pending_operations = [dict(row) for row in rows["operations"]]
        pending_revisions = [dict(row) for row in rows["ifc_revisions"]]
        inserted_operations: set[str] = set()
        inserted_revisions: set[str] = set()
        while pending_operations or pending_revisions:
            progressed = False
            for row in tuple(pending_operations):
                parent = row.get("repair_parent_operation_id")
                base = row.get("base_ifc_revision_id")
                if (parent is None or parent in inserted_operations) and (
                    base is None or base in inserted_revisions
                ):
                    session.add(_model_row(CriticalOperationModel, row))
                    await session.flush()
                    inserted_operations.add(row["operation_id"])
                    pending_operations.remove(row)
                    progressed = True
            for row in tuple(pending_revisions):
                if row["operation_id"] in inserted_operations:
                    session.add(_model_row(IfcModelRevisionModel, row))
                    await session.flush()
                    inserted_revisions.add(row["revision_id"])
                    pending_revisions.remove(row)
                    progressed = True
            if not progressed:
                raise MaterializationError("Operation/IFC revision closure cannot be ordered")

        session.add_all(
            _model_row(CriticalOperationInputModel, row) for row in rows["operation_inputs"]
        )
        session.add_all(
            _model_row(DataCatalogEntryModel, row) for row in rows["catalog_entries"]
        )
        for model in artifact_models:
            await session.execute(
                update(ArtifactModel)
                .where(ArtifactModel.artifact_id == model.artifact_id)
                .values(
                    current_version_id=desired_current[model.artifact_id],
                    updated_at=desired_updated_at[model.artifact_id],
                )
            )
        await session.flush()


async def _formal_readback(
    *,
    repository: DataRuntimeRepository,
    storage: LocalStorage,
    invocation: InvocationScope,
    ifc_scope: IfcRunIdentity,
    rebound: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    catalog = await repository.list_catalog(invocation)
    expected_catalog = {
        (row["resource_kind"], row["alias"], row.get("package_id"), row.get("operation_id"))
        for row in rebound["catalog_entries"]
    }
    actual_catalog = {
        (
            entry.resource_kind.value,
            entry.alias,
            entry.package_id,
            entry.operation_id,
        )
        for entry in catalog
    }
    if actual_catalog != expected_catalog:
        raise MaterializationError("formal Catalog readback differs from restored rows")
    for entry in catalog:
        if entry.resource_kind is CatalogResourceKind.PACKAGE:
            if await repository.get_package(invocation, entry.package_id) is None:
                raise MaterializationError("formal Package readback failed")
        elif await repository.get_operation(invocation, entry.operation_id) is None:
            raise MaterializationError("formal Operation readback failed")

    artifact_service = ArtifactService(repository, storage)
    for row in rebound["artifact_versions"]:
        download = await artifact_service.download_version_by_number(
            ifc_scope.owner_id,
            row["artifact_id"],
            row["version_no"],
        )
        if hashlib.sha256(download.content).hexdigest() != row["sha256"]:
            raise MaterializationError("formal Artifact readback changed exact bytes")

    if rebound["ifc_models"]:
        ifc_stack = build_in_process_ifc_stack(repository, storage)
        try:
            await ifc_stack.model_service.info(ifc_scope)
        finally:
            await ifc_stack.runtime.close()
        exact = await repository.get_current(ifc_scope)
        expected_model = rebound["ifc_models"][0]
        if (
            exact is None
            or exact.model_id != expected_model["ifc_model_id"]
            or exact.revision_id != expected_model["current_revision_id"]
        ):
            raise MaterializationError("formal IFC Application readback differs from restored rows")


_READBACK_MODELS: Mapping[str, tuple[type[Any], str]] = {
    "catalog_entries": (DataCatalogEntryModel, "catalog_entry_id"),
    "packages": (DataPackageModel, "package_id"),
    "package_members": (DataPackageMemberModel, "member_id"),
    "operations": (CriticalOperationModel, "operation_id"),
    "operation_inputs": (CriticalOperationInputModel, "input_binding_id"),
    "artifacts": (ArtifactModel, "artifact_id"),
    "artifact_versions": (ArtifactVersionModel, "version_id"),
    "ifc_models": (IfcModelModel, "ifc_model_id"),
    "ifc_revisions": (IfcModelRevisionModel, "revision_id"),
}


def _json_readback_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise MaterializationError("restored database returned a naive datetime")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _json_readback_value(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_readback_value(item) for item in value]
    return value


def _first_difference_path(expected: Any, actual: Any, path: str = "database_rows") -> str:
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        if set(expected) != set(actual):
            return f"{path}.fields"
        for key in sorted(expected):
            nested = _first_difference_path(expected[key], actual[key], f"{path}.{key}")
            if nested:
                return nested
        return ""
    if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        if len(expected) != len(actual):
            return f"{path}.length"
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual, strict=True)):
            nested = _first_difference_path(
                expected_item,
                actual_item,
                f"{path}[{index}]",
            )
            if nested:
                return nested
        return ""
    return "" if expected == actual else path


async def _restored_database_rows_digest(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    target_scope: Mapping[str, str],
    catalog_frontier: int,
    operation_frontier: int,
    rebound: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[str, dict[str, object]]:
    document: dict[str, object] = {
        "scope": dict(target_scope),
        "catalog_frontier": catalog_frontier,
        "operation_frontier": operation_frontier,
    }
    async with session_factory() as session:
        for collection, (model, primary_key) in _READBACK_MODELS.items():
            actual_rows: list[dict[str, Any]] = []
            for expected in rebound[collection]:
                stored = await session.get(model, expected[primary_key])
                if stored is None:
                    raise MaterializationError(
                        f"restored {model.__tablename__} row disappeared before digest readback"
                    )
                actual: dict[str, Any] = {}
                for field in expected:
                    if field == "object_path":
                        actual[field] = expected[field]
                    else:
                        actual[field] = _json_readback_value(getattr(stored, field))
                actual_rows.append(actual)
            document[collection] = actual_rows
    return canonical_database_rows_digest(document), document


__all__ = [
    "MaterializationError",
    "MaterializedSeed",
    "materialize_seed",
    "restore_run_fixture",
]
