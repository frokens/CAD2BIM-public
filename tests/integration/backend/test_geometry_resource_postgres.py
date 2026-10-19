from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from pydantic import ValidationError
from psycopg import sql
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from CAD2BIM_backend.application.data_runtime_service import (
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.application.geometry_resource_service import (
    GeometryResourceIntegrityError,
    GeometryResourceService,
)
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.control_plane import (
    ConflictError,
    IdempotencyConflictError,
)
from CAD2BIM_backend.domain.geometry_catalog import GeometryResourceVersionListQuery
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
    GeometryResourceListQuery,
    GeometryResourceRecord,
    GeometryResourceScope,
    GeometryResourceScopeKind,
    GeometryResourceVersionAssetRecord,
    GeometryResourceVersionBundle,
    GeometryResourceVersionRecord,
    GeometryResourceInput,
    geometry_resource_content_digest,
)
from CAD2BIM_backend.domain.identity import (
    TenantActorKind,
    TenantExecutionContext,
)
from CAD2BIM_backend.infrastructure.postgres.database import Database
from CAD2BIM_backend.infrastructure.geometry_seed.cli import (
    run_production_system_geometry_seed,
)
from CAD2BIM_backend.infrastructure.postgres.identity_directory import (
    PostgresIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.postgres.models import (
    GeometryResourceModel,
    GeometryResourceVersionAssetModel,
    GeometryResourceVersionModel,
    GeometryResourceVersionInputModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository import (
    PostgresControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantAwareAsyncSession,
    TenantContextMismatch,
    bind_tenant_execution_context,
    set_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from CAD2BIM_backend.ports.storage import (
    ResourceStorageKey,
    StorageConflictError,
)


pytestmark = [pytest.mark.integration, pytest.mark.network]

TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL", "").strip()


@dataclass(slots=True)
class _Runtime:
    engine: AsyncEngine
    database: Database
    sessions: async_sessionmaker[AsyncSession]
    repository: PostgresControlPlaneRepository
    identity_directory: PostgresIdentityDirectory

    async def close(self) -> None:
        await self.database.dispose()


@dataclass(frozen=True, slots=True)
class _RuntimeLogin:
    login_role: str
    privilege_role: str
    password: str


def _psycopg_database_url(value: str) -> str:
    return make_url(value).set(drivername="postgresql").render_as_string(hide_password=False)


def _create_test_runtime_logins(database_url: str) -> dict[str, _RuntimeLogin]:
    suffix = uuid4().hex[:12]
    roles = {
        kind: _RuntimeLogin(
            login_role=f"b301_{kind}_{suffix}",
            privilege_role=f"cad2bim_{kind}_runtime",
            password=f"b301-{kind}-{suffix}",
        )
        for kind in ("api", "runner", "mcp")
    }
    roles["curator"] = _RuntimeLogin(
        login_role=f"b301_curator_{suffix}",
        privilege_role="cad2bim_system_curator",
        password=f"b301-curator-{suffix}",
    )
    with psycopg.connect(_psycopg_database_url(database_url)) as connection:
        with connection.cursor() as cursor:
            for role in roles.values():
                cursor.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
                    ).format(
                        sql.Identifier(role.login_role),
                        sql.Literal(role.password),
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(role.privilege_role),
                        sql.Identifier(role.login_role),
                    )
                )
    return roles


def _drop_test_runtime_logins(
    database_url: str,
    roles: dict[str, _RuntimeLogin],
) -> None:
    if not roles:
        return
    with psycopg.connect(_psycopg_database_url(database_url)) as connection:
        with connection.cursor() as cursor:
            for role in roles.values():
                cursor.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role.login_role))
                )


def _runtime_database_url(database_url: str, role: _RuntimeLogin) -> str:
    return (
        make_url(database_url)
        .set(
            drivername="postgresql+psycopg",
            username=role.login_role,
            password=role.password,
        )
        .render_as_string(hide_password=False)
    )


def _create_runtime(database_url: str) -> _Runtime:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(
        engine,
        class_=TenantAwareAsyncSession,
        expire_on_commit=False,
    )
    database = Database(engine=engine, session_factory=sessions)
    return _Runtime(
        engine=engine,
        database=database,
        sessions=sessions,
        repository=PostgresControlPlaneRepository(sessions),
        identity_directory=PostgresIdentityDirectory(sessions),
    )


async def _open_direct_context(
    runtime: _Runtime,
    *,
    subject: str,
    session_name: str,
) -> TenantExecutionContext:
    sessions = DirectSessionService(
        runtime.repository,
        runtime.identity_directory,
        subject,
        bind_tenant_execution_context,
    )
    resolver = DirectScopeResolver(
        runtime.repository,
        runtime.identity_directory,
        subject,
        bind_tenant_execution_context,
    )
    opened = await sessions.open_direct_session(session_name)
    invocation = await resolver.resolve(
        opened.binding.direct_session_alias,
        command_id=f"b301-resolve-{uuid4().hex}",
    )
    assert invocation.identity.task_id is not None
    return invocation.identity


def _resource_storage_key(
    scope: GeometryResourceScope,
    *,
    resource_version_id: str,
    asset_role: GeometryResourceAssetRole,
) -> ResourceStorageKey:
    return ResourceStorageKey(
        scope_kind=scope.scope_kind.name,
        scope_id=scope.scope_id,
        resource_version_id=resource_version_id,
        asset_role=asset_role.value,
    )


def _asset_specifications(
    salt: str,
    manifest: dict[str, str],
) -> dict[GeometryResourceAssetRole, tuple[str, str, str, bytes]]:
    return {
        GeometryResourceAssetRole.CANONICAL_GEOMETRY: (
            "occt_brep_v1",
            "model.brep",
            "application/octet-stream",
            f"BREP:{salt}".encode(),
        ),
        GeometryResourceAssetRole.MANIFEST: (
            "json",
            "manifest.json",
            "application/json",
            canonical_json(manifest).encode("utf-8"),
        ),
    }


async def _mapped_bundle(
    *,
    storage: LocalStorage,
    resource_id: str,
    resource_version_id: str,
    version_no: int,
    scope: GeometryResourceScope,
    created_at: datetime,
    salt: str,
    persist_assets: bool = True,
    geometry_inputs: tuple[GeometryResourceInput, ...] = (),
    include_canonical_sha: bool = False,
) -> GeometryResourceVersionBundle:
    manifest = {"schema": "geometry_resource_manifest_v1", "salt": salt}
    if include_canonical_sha:
        manifest["canonical_geometry_sha256"] = hashlib.sha256(f"BREP:{salt}".encode()).hexdigest()
    if geometry_inputs:
        manifest["geometry_inputs"] = [item.model_dump(mode="json") for item in geometry_inputs]
    assets: list[GeometryResourceVersionAssetRecord] = []
    for role, (asset_format, filename, content_type, content) in _asset_specifications(
        salt,
        manifest,
    ).items():
        key = _resource_storage_key(
            scope,
            resource_version_id=resource_version_id,
            asset_role=role,
        )
        if persist_assets:
            stored = await storage.write_bytes(
                key,
                content,
                filename=filename,
                content_type=content_type,
            )
            size_bytes = stored.size_bytes
            digest = stored.sha256
            asset_created_at = stored.created_at
        else:
            size_bytes = len(content)
            digest = hashlib.sha256(content).hexdigest()
            asset_created_at = created_at
        assets.append(
            GeometryResourceVersionAssetRecord(
                resource_version_id=resource_version_id,
                asset_role=role,
                asset_format=asset_format,
                filename=filename,
                content_type=content_type,
                size_bytes=size_bytes,
                sha256=digest,
                created_at=asset_created_at,
            )
        )

    manifest_digest = sha256_digest(manifest)
    resource = GeometryResourceRecord(
        resource_id=resource_id,
        resource_kind=GeometryResourceKind.MAPPED_MODEL,
        owner_scope=scope,
        display_name=f"Mapped model {resource_id}",
        created_at=created_at,
    )
    version = GeometryResourceVersionRecord(
        resource_version_id=resource_version_id,
        resource_id=resource_id,
        version_no=version_no,
        origin_kind="agent_generated",
        content_digest=geometry_resource_content_digest(
            resource_kind=resource.resource_kind,
            manifest_digest=manifest_digest,
            assets=assets,
        ),
        manifest_digest=manifest_digest,
        manifest=manifest,
        lineage={"source": salt},
        created_at=created_at,
    )
    return GeometryResourceVersionBundle(
        resource=resource,
        version=version,
        assets=tuple(assets),
    )


def _program_bundle(
    *,
    template: GeometryResourceVersionBundle,
    resource_id: str,
    resource_version_id: str,
    scope: GeometryResourceScope,
    created_at: datetime,
    source_format: str = "python_source_bundle_v1",
    source_asset_format: str = "source_bundle_v1",
    source_content_type: str = "application/zip",
) -> GeometryResourceVersionBundle:
    manifest = {**template.version.manifest, "source_format": source_format}
    assets = tuple(
        asset.model_copy(update={
            "resource_version_id": resource_version_id,
            "created_at": created_at,
            **({"asset_format": source_asset_format, "content_type": source_content_type}
               if asset.asset_role is GeometryResourceAssetRole.SOURCE_BUNDLE else {}),
        })
        for asset in template.assets
    )
    resource = GeometryResourceRecord(
        resource_id=resource_id,
        resource_kind=GeometryResourceKind.MODELING_FUNCTION,
        owner_scope=scope,
        display_name=f"Program {resource_id}",
        created_at=created_at,
    )
    manifest_digest = sha256_digest(manifest)
    version = GeometryResourceVersionRecord(
        resource_version_id=resource_version_id,
        resource_id=resource_id,
        version_no=1,
        origin_kind="agent_generated",
        content_digest=geometry_resource_content_digest(
            resource_kind=resource.resource_kind,
            manifest_digest=manifest_digest,
            assets=assets,
        ),
        manifest_digest=manifest_digest,
        manifest=manifest,
        created_at=created_at,
    )
    return GeometryResourceVersionBundle(resource=resource, version=version, assets=assets)


async def _assert_storage_matches_bundle(
    storage: LocalStorage,
    bundle: GeometryResourceVersionBundle,
) -> None:
    for asset in bundle.assets:
        key = _resource_storage_key(
            bundle.resource.owner_scope,
            resource_version_id=bundle.version.resource_version_id,
            asset_role=asset.asset_role,
        )
        stored = await storage.stat(key)
        assert stored.filename == asset.filename
        assert stored.content_type == asset.content_type
        assert stored.size_bytes == asset.size_bytes
        assert stored.sha256 == asset.sha256
        assert hashlib.sha256(await storage.read(key)).hexdigest() == asset.sha256


async def _publish_after_storage_validation(
    runtime: _Runtime,
    storage: LocalStorage,
    context: TenantExecutionContext,
    bundle: GeometryResourceVersionBundle,
) -> GeometryResourceVersionBundle:
    service = GeometryResourceService(runtime.repository, storage)
    with bind_tenant_execution_context(context):
        return await service.publish_candidate(
            context,
            bundle,
        )


async def _insert_admin_bundle(
    admin_engine: AsyncEngine,
    bundle: GeometryResourceVersionBundle,
) -> None:
    scope = bundle.resource.owner_scope
    async with admin_engine.begin() as connection:
        await connection.execute(
            insert(GeometryResourceModel).values(
                resource_id=bundle.resource.resource_id,
                resource_kind=bundle.resource.resource_kind.value,
                owner_scope=scope.scope_kind.name,
                owner_tenant_id=scope.tenant_id,
                owner_task_id=scope.task_id,
                display_name=bundle.resource.display_name,
                created_at=bundle.resource.created_at,
            )
        )
        await connection.execute(
            insert(GeometryResourceVersionModel).values(
                resource_version_id=bundle.version.resource_version_id,
                resource_id=bundle.version.resource_id,
                version_no=bundle.version.version_no,
                origin_kind=bundle.version.origin_kind,
                content_digest=bundle.version.content_digest,
                manifest_digest=bundle.version.manifest_digest,
                manifest_json=bundle.version.manifest,
                lineage_json=bundle.version.lineage,
                created_at=bundle.version.created_at,
            )
        )
        await connection.execute(
            insert(GeometryResourceVersionAssetModel),
            [
                {
                    "resource_version_id": asset.resource_version_id,
                    "asset_role": asset.asset_role.value,
                    "asset_format": asset.asset_format,
                    "filename": asset.filename,
                    "content_type": asset.content_type,
                    "size_bytes": asset.size_bytes,
                    "sha256": asset.sha256,
                    "created_at": asset.created_at,
                }
                for asset in bundle.assets
            ],
        )


async def _visible_controlled_count(
    runtime: _Runtime,
    context: TenantExecutionContext,
    resource_ids: set[str],
) -> int:
    with bind_tenant_execution_context(context):
        async with runtime.sessions() as session, session.begin():
            await set_tenant_execution_context(session, context)
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(GeometryResourceModel)
                    .where(GeometryResourceModel.resource_id.in_(resource_ids))
                )
                or 0
            )


async def _geometry_row_counts(
    admin_engine: AsyncEngine,
    *,
    resource_id: str,
    resource_version_id: str,
) -> tuple[int, int, int]:
    async with admin_engine.connect() as connection:
        resource_count = await connection.scalar(
            select(func.count())
            .select_from(GeometryResourceModel)
            .where(GeometryResourceModel.resource_id == resource_id)
        )
        version_count = await connection.scalar(
            select(func.count())
            .select_from(GeometryResourceVersionModel)
            .where(GeometryResourceVersionModel.resource_version_id == resource_version_id)
        )
        asset_count = await connection.scalar(
            select(func.count())
            .select_from(GeometryResourceVersionAssetModel)
            .where(GeometryResourceVersionAssetModel.resource_version_id == resource_version_id)
        )
    return int(resource_count or 0), int(version_count or 0), int(asset_count or 0)


async def _listed_controlled_ids(
    runtime: _Runtime,
    context: TenantExecutionContext,
    controlled_ids: set[str],
) -> tuple[str, ...]:
    found: list[str] = []
    query = GeometryResourceListQuery(page_size=1)
    for _ in range(len(controlled_ids) + 3):
        with bind_tenant_execution_context(context):
            page = await runtime.repository.list_geometry_resources(
                context,
                query=query,
            )
        found.extend(
            resource.resource_id
            for resource in page.resources
            if resource.resource_id in controlled_ids
        )
        if page.complete or set(found) == controlled_ids:
            break
        assert page.next_before_created_at is not None
        assert page.next_before_resource_id is not None
        query = GeometryResourceListQuery(
            before_created_at=page.next_before_created_at,
            before_resource_id=page.next_before_resource_id,
            page_size=1,
        )
    return tuple(found)


async def _assert_forbidden_root_insert(
    runtime: _Runtime,
    context: TenantExecutionContext,
    *,
    owner_scope: str,
    resource_id: str,
    target_tenant_id: str | None = None,
    target_task_id: str | None = None,
) -> None:
    tenant_id = (None if owner_scope == "SYSTEM" else
                 context.tenant_id if target_tenant_id is None else target_tenant_id)
    assert owner_scope != "TASK" or target_task_id is not None
    with pytest.raises(DBAPIError) as failure:
        with bind_tenant_execution_context(context):
            async with runtime.sessions() as session, session.begin():
                await set_tenant_execution_context(session, context)
                await session.execute(
                    text(
                        "INSERT INTO geometry_resources ("
                        "resource_id, resource_kind, owner_scope, owner_tenant_id, "
                        "owner_task_id, display_name"
                        ") VALUES ("
                        ":resource_id, 'mapped_model', :owner_scope, :tenant_id, :task_id, "
                        "'forbidden runtime resource'"
                        ")"
                    ),
                    {
                        "resource_id": resource_id,
                        "owner_scope": owner_scope,
                        "tenant_id": tenant_id,
                        "task_id": target_task_id,
                    },
                )
    assert failure.value.orig.sqlstate == "42501"


async def _cleanup_geometry_rows(
    admin_engine: AsyncEngine,
    resource_ids: set[str],
) -> None:
    if not resource_ids:
        return
    async with admin_engine.begin() as connection:
        version_ids = tuple(
            (
                await connection.scalars(
                    select(GeometryResourceVersionModel.resource_version_id).where(
                        GeometryResourceVersionModel.resource_id.in_(resource_ids)
                    )
                )
            ).all()
        )
        if version_ids:
            await connection.execute(
                delete(GeometryResourceVersionInputModel).where(
                    GeometryResourceVersionInputModel.resource_version_id.in_(version_ids)
                )
            )
            await connection.execute(
                delete(GeometryResourceVersionAssetModel).where(
                    GeometryResourceVersionAssetModel.resource_version_id.in_(version_ids)
                )
            )
            await connection.execute(
                delete(GeometryResourceVersionModel).where(
                    GeometryResourceVersionModel.resource_version_id.in_(version_ids)
                )
            )
        await connection.execute(
            delete(GeometryResourceModel).where(GeometryResourceModel.resource_id.in_(resource_ids))
        )


@pytest.mark.asyncio
async def test_system_geometry_seed_curator_passes_readiness_and_empty_formal_cli(
    tmp_path: Path,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")

    runtime_logins: dict[str, _RuntimeLogin] = {}
    runtime: _Runtime | None = None
    admin_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        runtime_logins = _create_test_runtime_logins(TEST_DATABASE_URL)
        curator = runtime_logins["curator"]
        curator_url = _runtime_database_url(TEST_DATABASE_URL, curator)
        runtime = _create_runtime(curator_url)

        await runtime.database.verify_ready()
        async with runtime.engine.connect() as connection:
            role = (
                await connection.execute(
                    text(
                        "SELECT current_user, rolsuper, rolbypassrls "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
            ).one()
        assert role == (curator.login_role, False, False)

        async with admin_engine.connect() as connection:
            before_counts = []
            for model in (
                GeometryResourceModel,
                GeometryResourceVersionModel,
                GeometryResourceVersionAssetModel,
            ):
                before_counts.append(
                    (await connection.execute(select(func.count()).select_from(model))).scalar_one()
                )
            before = tuple(before_counts)
            revision_relation = (
                await connection.execute(text("SELECT current_schema() || '.alembic_version'"))
            ).scalar_one()
            privileges = {
                privilege: (
                    await connection.execute(
                        text("SELECT has_table_privilege(:role, :relation, :privilege)"),
                        {
                            "role": curator.login_role,
                            "relation": revision_relation,
                            "privilege": privilege,
                        },
                    )
                ).scalar_one()
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
            }
        assert privileges == {
            "SELECT": True,
            "INSERT": False,
            "UPDATE": False,
            "DELETE": False,
        }

        result = await run_production_system_geometry_seed(
            settings=Settings(
                environment="test",
                database_url=curator_url,
                storage_root=tmp_path / "formal-empty-seed-storage",
            )
        )
        assert result.published == ()
        assert result.replayed == ()

        async with admin_engine.connect() as connection:
            after_counts = []
            for model in (
                GeometryResourceModel,
                GeometryResourceVersionModel,
                GeometryResourceVersionAssetModel,
            ):
                after_counts.append(
                    (await connection.execute(select(func.count()).select_from(model))).scalar_one()
                )
            after = tuple(after_counts)
        assert after == before
        assert not tuple((tmp_path / "formal-empty-seed-storage").rglob("*"))
    finally:
        if runtime is not None:
            await runtime.close()
        await admin_engine.dispose()
        if runtime_logins:
            _drop_test_runtime_logins(TEST_DATABASE_URL, runtime_logins)


@pytest.mark.asyncio
async def test_executable_program_versions_filter_before_cross_root_keyset_and_keep_rls(
    tmp_path: Path,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")

    from tests.unit.backend.test_authoring_draft_service import geometry_create, setup_services

    # Reuse current asset assembly in the existing memory authoring owner.
    # The PostgreSQL rows below remain explicit fixtures with controlled identities and scopes.
    fixture_scope, _, authoring, _, publication = setup_services()
    declaration = geometry_create()
    declaration["source"]["source_files"][0]["source"] = (
        "import cadquery as cq\ndef build(ctx):\n"
        "    diameter = ctx.param('diameter')\n"
        "    return cq.Workplane('XY').box(diameter, diameter, diameter)\n"
    )
    saved = await authoring.save_draft_from_aliases(
        fixture_scope, declaration=declaration, request_identity="catalog-template-write")
    committed = await publication.publish(fixture_scope, root_kind="geometry",
        draft_ref=authoring.draft_reference(fixture_scope, saved.draft),
        expected_edit_token=1, request_identity="catalog-template-publish")
    template, = committed.geometry_versions
    assert {asset.asset_role for asset in template.assets} == {
        GeometryResourceAssetRole.SOURCE_BUNDLE, GeometryResourceAssetRole.PARAMETER_SCHEMA}
    publication._geometry_runtime.build_fixed.assert_not_awaited()

    runtime_logins: dict[str, _RuntimeLogin] = {}
    runtime: _Runtime | None = None
    admin_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    prefix = f"program-list-{uuid4().hex}"
    controlled_ids: set[str] = set()
    try:
        runtime_logins = _create_test_runtime_logins(TEST_DATABASE_URL)
        runtime = _create_runtime(
            _runtime_database_url(TEST_DATABASE_URL, runtime_logins["mcp"])
        )
        await runtime.database.verify_ready()
        subject = f"{prefix}-subject"
        task_one = await _open_direct_context(
            runtime,
            subject=subject,
            session_name=f"{prefix} task one",
        )
        task_two = await _open_direct_context(
            runtime,
            subject=subject,
            session_name=f"{prefix} task two",
        )
        other = await _open_direct_context(
            runtime,
            subject=f"{prefix}-other-subject",
            session_name=f"{prefix} other tenant",
        )
        scopes = {
            "system": GeometryResourceScope(scope_kind=GeometryResourceScopeKind.SYSTEM),
            "tenant": GeometryResourceScope(
                scope_kind=GeometryResourceScopeKind.TENANT,
                tenant_id=task_one.tenant_id,
            ),
            "task_one": GeometryResourceScope(
                scope_kind=GeometryResourceScopeKind.TASK,
                tenant_id=task_one.tenant_id,
                task_id=task_one.task_id,
            ),
            "task_two": GeometryResourceScope(
                scope_kind=GeometryResourceScopeKind.TASK,
                tenant_id=task_two.tenant_id,
                task_id=task_two.task_id,
            ),
            "other": GeometryResourceScope(
                scope_kind=GeometryResourceScopeKind.TASK,
                tenant_id=other.tenant_id,
                task_id=other.task_id,
            ),
        }
        base_time = datetime(2099, 1, 1, tzinfo=UTC)

        def program(
            name: str,
            scope_name: str,
            seconds: int,
            **overrides: str,
        ) -> GeometryResourceVersionBundle:
            resource_id = f"{prefix}-{name}"
            controlled_ids.add(resource_id)
            return _program_bundle(
                template=template,
                resource_id=resource_id,
                resource_version_id=f"{resource_id}-v1",
                scope=scopes[scope_name],
                created_at=base_time + timedelta(seconds=seconds),
                **overrides,
            )

        system = program("system", "system", 6)
        tenant = program("tenant", "tenant", 5)
        task_z = program("task-z", "task_one", 4)
        task_a = program("task-a", "task_one", 4)
        hidden_task = program("hidden-task", "task_two", 9)
        hidden_other = program("hidden-other", "other", 10)
        wrong_manifest = program(
            "wrong-manifest",
            "task_one",
            8,
            source_format="ifc",
        )
        wrong_bundle = program(
            "wrong-bundle",
            "task_one",
            7,
            source_asset_format="raw_source",
        )
        for bundle in (
            system,
            tenant,
            task_z,
            task_a,
            hidden_task,
            hidden_other,
            wrong_manifest,
            wrong_bundle,
        ):
            await _insert_admin_bundle(admin_engine, bundle)

        mapped = await _mapped_bundle(
            storage=LocalStorage(tmp_path / "storage"),
            resource_id=f"{prefix}-mapped",
            resource_version_id=f"{prefix}-mapped-v1",
            version_no=1,
            scope=scopes["task_one"],
            created_at=base_time + timedelta(seconds=11),
            salt="mapped",
            persist_assets=False,
        )
        controlled_ids.add(mapped.resource.resource_id)
        await _insert_admin_bundle(admin_engine, mapped)

        with bind_tenant_execution_context(task_one):
            first = await runtime.repository.list_executable_program_versions(
                task_one,
                before_created_at=None,
                before_resource_version_id=None,
                limit=3,
            )
            second = await runtime.repository.list_executable_program_versions(
                task_one,
                before_created_at=first[-1].version.created_at,
                before_resource_version_id=first[-1].version.resource_version_id,
                limit=3,
            )
        assert tuple(item.version.resource_version_id for item in first) == (
            system.version.resource_version_id,
            tenant.version.resource_version_id,
            task_z.version.resource_version_id,
        )
        assert tuple(item.version.resource_version_id for item in second) == (
            task_a.version.resource_version_id,
        )

        with bind_tenant_execution_context(task_two):
            task_two_visible = await runtime.repository.list_executable_program_versions(
                task_two,
                before_created_at=None,
                before_resource_version_id=None,
                limit=10,
            )
        assert tuple(item.version.resource_version_id for item in task_two_visible) == (
            hidden_task.version.resource_version_id,
            system.version.resource_version_id,
            tenant.version.resource_version_id,
        )

        with bind_tenant_execution_context(other):
            other_visible = await runtime.repository.list_executable_program_versions(
                other,
                before_created_at=None,
                before_resource_version_id=None,
                limit=10,
            )
        assert tuple(item.version.resource_version_id for item in other_visible) == (
            hidden_other.version.resource_version_id,
            system.version.resource_version_id,
        )
    finally:
        if runtime is not None:
            await runtime.close()
        try:
            await _cleanup_geometry_rows(admin_engine, controlled_ids)
        finally:
            await admin_engine.dispose()
            if runtime_logins:
                _drop_test_runtime_logins(TEST_DATABASE_URL, runtime_logins)


@pytest.mark.asyncio
async def test_geometry_resources_are_exact_durable_and_rls_scoped(
    tmp_path: Path,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")

    runtime_logins: dict[str, _RuntimeLogin] = {}
    runtimes: dict[str, _Runtime] = {}
    restarted: _Runtime | None = None
    admin_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    storage_root = tmp_path / "storage"
    storage = LocalStorage(storage_root)
    prefix = f"b301-{uuid4().hex}"
    controlled_ids = {
        f"{prefix}-task-one",
        f"{prefix}-task-two",
        f"{prefix}-tenant",
        f"{prefix}-system",
        f"{prefix}-other-tenant",
        f"{prefix}-incomplete",
        f"{prefix}-storage-mismatch",
    }
    try:
        runtime_logins = _create_test_runtime_logins(TEST_DATABASE_URL)
        runtimes = {
            kind: _create_runtime(_runtime_database_url(TEST_DATABASE_URL, role))
            for kind, role in runtime_logins.items()
        }
        await runtimes["mcp"].database.verify_ready()

        for runtime in runtimes.values():
            async with runtime.engine.connect() as connection:
                role = (
                    await connection.execute(
                        text(
                            "SELECT current_user, rolsuper, rolbypassrls "
                            "FROM pg_roles WHERE rolname = current_user"
                        )
                    )
                ).one()
                assert role[1:] == (False, False)

        subject = f"b301-subject-{uuid4().hex}"
        task_one_context = await _open_direct_context(
            runtimes["mcp"],
            subject=subject,
            session_name=f"B3.01 task one {prefix}",
        )
        task_two_context = await _open_direct_context(
            runtimes["mcp"],
            subject=subject,
            session_name=f"B3.01 task two {prefix}",
        )
        other_context = await _open_direct_context(
            runtimes["mcp"],
            subject=f"b301-other-{uuid4().hex}",
            session_name=f"B3.01 other tenant {prefix}",
        )
        assert task_one_context.tenant_id == task_two_context.tenant_id
        assert task_one_context.task_id != task_two_context.task_id
        assert other_context.tenant_id != task_one_context.tenant_id

        with pytest.raises(TenantContextMismatch, match="requires a tenant execution context"):
            await runtimes["mcp"].repository.list_geometry_resources(
                task_one_context,
                query=GeometryResourceListQuery(page_size=1),
            )
        with bind_tenant_execution_context(task_one_context):
            with pytest.raises(TenantContextMismatch, match="does not match"):
                await runtimes["mcp"].repository.list_geometry_resources(
                    task_two_context,
                    query=GeometryResourceListQuery(page_size=1),
                )

        task_one_scope = GeometryResourceScope(
            scope_kind=GeometryResourceScopeKind.TASK,
            tenant_id=task_one_context.tenant_id,
            task_id=task_one_context.task_id,
        )
        task_two_scope = GeometryResourceScope(
            scope_kind=GeometryResourceScopeKind.TASK,
            tenant_id=task_two_context.tenant_id,
            task_id=task_two_context.task_id,
        )
        tenant_scope = GeometryResourceScope(
            scope_kind=GeometryResourceScopeKind.TENANT,
            tenant_id=task_one_context.tenant_id,
        )
        system_scope = GeometryResourceScope(
            scope_kind=GeometryResourceScopeKind.SYSTEM,
        )
        other_scope = GeometryResourceScope(
            scope_kind=GeometryResourceScopeKind.TASK,
            tenant_id=other_context.tenant_id,
            task_id=other_context.task_id,
        )
        base_time = datetime.now(UTC) + timedelta(hours=1)

        incomplete = await _mapped_bundle(
            storage=storage,
            resource_id=f"{prefix}-incomplete",
            resource_version_id=f"{prefix}-incomplete-v1",
            version_no=1,
            scope=task_one_scope,
            created_at=base_time + timedelta(seconds=7),
            salt="incomplete",
            persist_assets=False,
        )
        with pytest.raises(ValidationError, match="missing required assets"):
            GeometryResourceVersionBundle(
                resource=incomplete.resource,
                version=incomplete.version,
                assets=tuple(
                    asset
                    for asset in incomplete.assets
                    if asset.asset_role is not GeometryResourceAssetRole.CANONICAL_GEOMETRY
                ),
            )
        incomplete_service = GeometryResourceService(
            runtimes["mcp"].repository,
            storage,
        )
        with bind_tenant_execution_context(task_one_context):
            with pytest.raises(GeometryResourceIntegrityError, match="asset is missing"):
                await incomplete_service.publish_candidate(
                    task_one_context,
                    incomplete,
                )
        assert await _geometry_row_counts(
            admin_engine,
            resource_id=incomplete.resource.resource_id,
            resource_version_id=incomplete.version.resource_version_id,
        ) == (0, 0, 0)
        with bind_tenant_execution_context(task_one_context):
            assert (
                await runtimes["mcp"].repository.get_geometry_resource(
                    task_one_context,
                    resource_id=f"{prefix}-incomplete",
                )
                is None
            )

        mismatched = await _mapped_bundle(
            storage=storage,
            resource_id=f"{prefix}-storage-mismatch",
            resource_version_id=f"{prefix}-storage-mismatch-v1",
            version_no=1,
            scope=task_one_scope,
            created_at=base_time + timedelta(seconds=6),
            salt="storage-mismatch",
        )
        mismatched_assets = tuple(
            asset.model_copy(update={"sha256": "0" * 64})
            if asset.asset_role is GeometryResourceAssetRole.CANONICAL_GEOMETRY
            else asset
            for asset in mismatched.assets
        )
        mismatched_version = mismatched.version.model_copy(
            update={
                "content_digest": geometry_resource_content_digest(
                    resource_kind=mismatched.resource.resource_kind,
                    manifest_digest=mismatched.version.manifest_digest,
                    assets=mismatched_assets,
                )
            }
        )
        mismatched_binding = GeometryResourceVersionBundle(
            resource=mismatched.resource,
            version=mismatched_version,
            assets=mismatched_assets,
        )
        mismatch_service = GeometryResourceService(
            runtimes["mcp"].repository,
            storage,
        )
        with bind_tenant_execution_context(task_one_context):
            with pytest.raises(
                GeometryResourceIntegrityError,
                match="metadata or digest does not match",
            ):
                await mismatch_service.publish_candidate(
                    task_one_context,
                    mismatched_binding,
                )
        assert await _geometry_row_counts(
            admin_engine,
            resource_id=mismatched.resource.resource_id,
            resource_version_id=mismatched.version.resource_version_id,
        ) == (0, 0, 0)
        with bind_tenant_execution_context(task_one_context):
            assert (
                await runtimes["mcp"].repository.get_geometry_resource(
                    task_one_context,
                    resource_id=f"{prefix}-storage-mismatch",
                )
                is None
            )

        task_one_v1 = await _mapped_bundle(
            storage=storage,
            resource_id=f"{prefix}-task-one",
            resource_version_id=f"{prefix}-task-one-v1",
            version_no=1,
            scope=task_one_scope,
            created_at=base_time + timedelta(seconds=5),
            salt="task-one-v1",
        )
        published_v1 = await _publish_after_storage_validation(
            runtimes["mcp"], storage, task_one_context, task_one_v1
        )
        replayed_v1 = await _publish_after_storage_validation(
            runtimes["mcp"], storage, task_one_context, task_one_v1
        )
        assert replayed_v1 == published_v1

        timestamp_only_v1 = GeometryResourceVersionBundle(
            resource=task_one_v1.resource,
            version=task_one_v1.version.model_copy(update={"created_at": base_time + timedelta(seconds=6)}),
            assets=task_one_v1.assets,
        )
        with bind_tenant_execution_context(task_one_context):
            assert await runtimes["mcp"].repository.publish_geometry_resource_version(
                task_one_context, timestamp_only_v1) == published_v1

        conflicting_v1 = await _mapped_bundle(
            storage=storage,
            resource_id=task_one_v1.resource.resource_id,
            resource_version_id=task_one_v1.version.resource_version_id,
            version_no=1,
            scope=task_one_scope,
            created_at=task_one_v1.version.created_at,
            salt="task-one-v1-conflict",
            persist_assets=False,
        )
        assert conflicting_v1.version.manifest != task_one_v1.version.manifest
        assert conflicting_v1.version.content_digest != task_one_v1.version.content_digest
        assert next(asset.sha256 for asset in conflicting_v1.assets
                    if asset.asset_role is GeometryResourceAssetRole.CANONICAL_GEOMETRY) != next(
                        asset.sha256 for asset in task_one_v1.assets
                        if asset.asset_role is GeometryResourceAssetRole.CANONICAL_GEOMETRY)
        with bind_tenant_execution_context(task_one_context):
            with pytest.raises(IdempotencyConflictError):
                await runtimes["mcp"].repository.publish_geometry_resource_version(
                    task_one_context,
                    conflicting_v1,
                )
        canonical_key = _resource_storage_key(
            task_one_scope,
            resource_version_id=task_one_v1.version.resource_version_id,
            asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY,
        )
        with pytest.raises(StorageConflictError):
            await storage.write_bytes(
                canonical_key,
                b"different immutable geometry",
                filename="model.brep",
                content_type="application/octet-stream",
            )

        task_one_v2 = await _mapped_bundle(
            storage=storage,
            resource_id=task_one_v1.resource.resource_id,
            resource_version_id=f"{prefix}-task-one-v2",
            version_no=2,
            scope=task_one_scope,
            created_at=base_time + timedelta(seconds=5, microseconds=1),
            salt="task-one-v2",
        )
        runner_context = task_one_context.model_copy(
            update={
                "actor_kind": TenantActorKind.RUNNER,
                "command_id": f"b301-runner-{uuid4().hex}",
            }
        )
        published_v2 = await _publish_after_storage_validation(
            runtimes["runner"], storage, runner_context, task_one_v2
        )
        assert published_v2.version.version_no == 2
        with bind_tenant_execution_context(task_one_context):
            assert (
                await runtimes["mcp"].repository.get_exact_geometry_resource_version(
                    task_one_context,
                    resource_id=task_one_v1.resource.resource_id,
                    resource_version_id=task_one_v1.version.resource_version_id,
                )
                == published_v1
            )
            assert (
                await runtimes["mcp"].repository.get_geometry_resource_version_by_id(
                    task_one_context,
                    resource_version_id=task_one_v1.version.resource_version_id,
                )
                == published_v1
            )
            assert (
                await runtimes["mcp"].repository.get_latest_geometry_resource_version(
                    task_one_context,
                    resource_id=task_one_v1.resource.resource_id,
                )
                == published_v2
            )
            first_version_page = (
                await runtimes["mcp"].repository.list_geometry_resource_versions(
                    task_one_context,
                    query=GeometryResourceVersionListQuery(
                        resource_id=task_one_v1.resource.resource_id,
                        page_size=1,
                    ),
                )
            )
            assert first_version_page.versions == (published_v2,)
            assert first_version_page.next_before_version_no == 2
            second_version_page = (
                await runtimes["mcp"].repository.list_geometry_resource_versions(
                    task_one_context,
                    query=GeometryResourceVersionListQuery(
                        resource_id=task_one_v1.resource.resource_id,
                        before_version_no=first_version_page.next_before_version_no,
                        page_size=1,
                    ),
                )
            )
            assert second_version_page.versions == (published_v1,)
            assert second_version_page.complete is True

        skipped_v4 = await _mapped_bundle(
            storage=storage,
            resource_id=task_one_v1.resource.resource_id,
            resource_version_id=f"{prefix}-task-one-v4",
            version_no=4,
            scope=task_one_scope,
            created_at=base_time + timedelta(seconds=5, microseconds=2),
            salt="task-one-v4",
            persist_assets=False,
        )
        with bind_tenant_execution_context(runner_context):
            with pytest.raises(ConflictError, match="next immutable version"):
                await runtimes["runner"].repository.publish_geometry_resource_version(
                    runner_context,
                    skipped_v4,
                )

        task_two_v1 = await _mapped_bundle(
            storage=storage,
            resource_id=f"{prefix}-task-two",
            resource_version_id=f"{prefix}-task-two-v1",
            version_no=1,
            scope=task_two_scope,
            created_at=base_time + timedelta(seconds=4),
            salt="task-two-v1",
        )
        other_v1 = await _mapped_bundle(
            storage=storage,
            resource_id=f"{prefix}-other-tenant",
            resource_version_id=f"{prefix}-other-tenant-v1",
            version_no=1,
            scope=other_scope,
            created_at=base_time + timedelta(seconds=1),
            salt="other-tenant-v1",
        )
        await _publish_after_storage_validation(
            runtimes["mcp"], storage, task_two_context, task_two_v1
        )
        await _publish_after_storage_validation(runtimes["mcp"], storage, other_context, other_v1)

        tenant_v1 = await _mapped_bundle(
            storage=storage,
            resource_id=f"{prefix}-tenant",
            resource_version_id=f"{prefix}-tenant-v1",
            version_no=1,
            scope=tenant_scope,
            created_at=base_time + timedelta(seconds=3),
            salt="tenant-v1",
        )
        system_v1 = await _mapped_bundle(
            storage=storage,
            resource_id=f"{prefix}-system",
            resource_version_id=f"{prefix}-system-v1",
            version_no=1,
            scope=system_scope,
            created_at=base_time + timedelta(seconds=2),
            salt="system-v1",
        )
        await _assert_storage_matches_bundle(storage, tenant_v1)
        await _assert_storage_matches_bundle(storage, system_v1)
        await _insert_admin_bundle(admin_engine, tenant_v1)
        curator_context = TenantExecutionContext(
            tenant_id="system",
            principal_id="system-curator",
            task_id=None,
            actor_kind=TenantActorKind.SYSTEM_CURATOR,
            command_id=f"b301-curator-{uuid4().hex}",
        )
        published_system = await _publish_after_storage_validation(
            runtimes["curator"],
            storage,
            curator_context,
            system_v1,
        )
        assert published_system == system_v1

        visible_ids = {
            task_one_v1.resource.resource_id,
            task_two_v1.resource.resource_id,
            tenant_v1.resource.resource_id,
            system_v1.resource.resource_id,
            other_v1.resource.resource_id,
        }
        task_one_expected = {
            task_one_v1.resource.resource_id,
            tenant_v1.resource.resource_id,
            system_v1.resource.resource_id,
        }
        task_two_expected = {
            task_two_v1.resource.resource_id,
            tenant_v1.resource.resource_id,
            system_v1.resource.resource_id,
        }
        other_expected = {
            other_v1.resource.resource_id,
            system_v1.resource.resource_id,
        }
        api_context = task_one_context.model_copy(update={"command_id": f"b301-api-{uuid4().hex}"})
        for runtime, context in (
            (runtimes["mcp"], task_one_context),
            (runtimes["runner"], runner_context),
            (runtimes["api"], api_context),
        ):
            listed = await _listed_controlled_ids(runtime, context, visible_ids)
            assert set(listed) == task_one_expected
            assert await _visible_controlled_count(runtime, context, visible_ids) == 3
            with bind_tenant_execution_context(context):
                assert (
                    await runtime.repository.get_geometry_resource(
                        context,
                        resource_id=task_two_v1.resource.resource_id,
                    )
                    is None
                )
                assert (
                    await runtime.repository.get_exact_geometry_resource_version(
                        context,
                        resource_id=other_v1.resource.resource_id,
                        resource_version_id=other_v1.version.resource_version_id,
                    )
                    is None
                )
                assert (
                    await runtime.repository.get_geometry_resource_version_by_id(
                        context,
                        resource_version_id=other_v1.version.resource_version_id,
                    )
                    is None
                )

        task_two_listed = await _listed_controlled_ids(
            runtimes["mcp"], task_two_context, visible_ids
        )
        assert set(task_two_listed) == task_two_expected
        assert await _visible_controlled_count(runtimes["mcp"], task_two_context, visible_ids) == 3
        other_listed = await _listed_controlled_ids(runtimes["mcp"], other_context, visible_ids)
        assert set(other_listed) == other_expected
        assert await _visible_controlled_count(runtimes["mcp"], other_context, visible_ids) == 2

        tenant_context = task_one_context.model_copy(
            update={
                "task_id": None,
                "command_id": f"b301-tenant-view-{uuid4().hex}",
            }
        )
        tenant_listed = await _listed_controlled_ids(runtimes["mcp"], tenant_context, visible_ids)
        assert set(tenant_listed) == {
            tenant_v1.resource.resource_id,
            system_v1.resource.resource_id,
        }
        curator_listed = await _listed_controlled_ids(
            runtimes["curator"], curator_context, visible_ids
        )
        assert set(curator_listed) == {system_v1.resource.resource_id}
        with bind_tenant_execution_context(curator_context):
            assert (
                await runtimes["curator"].repository.get_geometry_resource(
                    curator_context,
                    resource_id=tenant_v1.resource.resource_id,
                )
                is None
            )
        hidden_service = GeometryResourceService(runtimes["mcp"].repository, storage)
        with bind_tenant_execution_context(task_one_context):
            assert (
                await hidden_service.read_exact_asset(
                    task_one_context,
                    resource_id=other_v1.resource.resource_id,
                    resource_version_id=other_v1.version.resource_version_id,
                    asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY,
                )
                is None
            )

        # A root inserted without a version is RLS-visible metadata but must never
        # enter the ready Catalog projection.
        empty_root_id = f"{prefix}-incomplete"
        async with admin_engine.begin() as connection:
            await connection.execute(
                insert(GeometryResourceModel).values(
                    resource_id=empty_root_id,
                    resource_kind=GeometryResourceKind.MAPPED_MODEL.value,
                    owner_scope=GeometryResourceScopeKind.TASK.name,
                    owner_tenant_id=task_one_context.tenant_id,
                    owner_task_id=task_one_context.task_id,
                    display_name="not ready",
                    created_at=base_time + timedelta(seconds=8),
                )
            )
        with bind_tenant_execution_context(task_one_context):
            assert (
                await runtimes["mcp"].repository.get_geometry_resource(
                    task_one_context,
                    resource_id=empty_root_id,
                )
                is not None
            )
            ready_page = await runtimes["mcp"].repository.list_geometry_resources(
                task_one_context,
                query=GeometryResourceListQuery(page_size=100),
            )
        assert empty_root_id not in {item.resource_id for item in ready_page.resources}

        tenant_publications = []
        for index, (kind, context) in enumerate((
            ("mcp", task_one_context), ("runner", runner_context),
        ), start=1):
            root_id = f"{prefix}-tenant-{kind}"
            controlled_ids.add(root_id)
            candidate = await _mapped_bundle(
                storage=storage,
                resource_id=root_id,
                resource_version_id=f"{root_id}-v1",
                version_no=1,
                scope=tenant_scope,
                created_at=base_time + timedelta(seconds=3, microseconds=index),
                salt=f"tenant-{kind}-v1",
            )
            # Independent roots prove each runtime INSERTs, with real saved assets.
            committed = await _publish_after_storage_validation(
                runtimes[kind], storage, context, candidate)
            assert committed == candidate
            assert await _geometry_row_counts(admin_engine,
                resource_id=root_id,
                resource_version_id=candidate.version.resource_version_id) == (1, 1, len(candidate.assets))
            tenant_publications.append(committed)
        system_v2 = await _mapped_bundle(
            storage=storage,
            resource_id=system_v1.resource.resource_id,
            resource_version_id=f"{prefix}-system-v2",
            version_no=2,
            scope=system_scope,
            created_at=base_time + timedelta(seconds=2, microseconds=1),
            salt="system-v2",
            persist_assets=False,
        )
        for runtime, context in (
            (runtimes["mcp"], task_one_context),
            (runtimes["runner"], runner_context),
        ):
            with bind_tenant_execution_context(context):
                with pytest.raises(ConflictError):
                    await runtime.repository.publish_geometry_resource_version(
                        context,
                        system_v2,
                    )

        for runtime, context, kind in (
            (runtimes["mcp"], task_one_context, "mcp"),
            (runtimes["runner"], runner_context, "runner"),
            (runtimes["api"], api_context, "api"),
        ):
            for owner_scope, tenant_id, task_id, suffix in (
                ("SYSTEM", None, None, "system"),
                ("TENANT", other_context.tenant_id, None, "foreign-tenant"),
                ("TASK", task_one_context.tenant_id, task_two_context.task_id, "foreign-task"),
            ):
                forged_id = f"{prefix}-forged-{kind}-{suffix}"
                controlled_ids.add(forged_id)
                await _assert_forbidden_root_insert(runtime, context,
                    owner_scope=owner_scope, resource_id=forged_id,
                    target_tenant_id=tenant_id, target_task_id=task_id)
        api_tenant_id = f"{prefix}-forged-api-own-tenant"
        controlled_ids.add(api_tenant_id)
        await _assert_forbidden_root_insert(runtimes["api"], api_context,
            owner_scope="TENANT", resource_id=api_tenant_id)

        await runtimes["mcp"].close()
        del runtimes["mcp"]
        restarted = _create_runtime(_runtime_database_url(TEST_DATABASE_URL, runtime_logins["mcp"]))
        restarted_storage = LocalStorage(storage_root)
        restarted_service = GeometryResourceService(
            restarted.repository,
            restarted_storage,
        )
        await restarted.database.verify_ready()
        with bind_tenant_execution_context(task_one_context):
            restarted_v1 = await restarted.repository.get_exact_geometry_resource_version(
                task_one_context,
                resource_id=task_one_v1.resource.resource_id,
                resource_version_id=task_one_v1.version.resource_version_id,
            )
            restarted_v2 = await restarted.repository.get_exact_geometry_resource_version(
                task_one_context,
                resource_id=task_one_v2.resource.resource_id,
                resource_version_id=task_one_v2.version.resource_version_id,
            )
        assert restarted_v1 == published_v1
        assert restarted_v2 == published_v2
        restarted_tenant_versions = []
        for expected in tenant_publications:
            with bind_tenant_execution_context(task_one_context):
                exact = await restarted.repository.get_exact_geometry_resource_version(
                    task_one_context, resource_id=expected.resource.resource_id,
                    resource_version_id=expected.version.resource_version_id)
            assert exact == expected
            restarted_tenant_versions.append(exact)
        for bundle in (restarted_v1, restarted_v2, *restarted_tenant_versions):
            for expected_asset in bundle.assets:
                with bind_tenant_execution_context(task_one_context):
                    exact = await restarted_service.read_exact_asset(
                        task_one_context,
                        resource_id=bundle.resource.resource_id,
                        resource_version_id=bundle.version.resource_version_id,
                        asset_role=expected_asset.asset_role,
                    )
                assert exact is not None
                actual_asset, content = exact
                assert actual_asset == expected_asset
                assert hashlib.sha256(content).hexdigest() == expected_asset.sha256
        new_tenant_ids = {bundle.resource.resource_id for bundle in tenant_publications}
        restarted_visible_ids = visible_ids | new_tenant_ids
        for runtime, context, expected in (
            (restarted, task_one_context, task_one_expected | new_tenant_ids),
            (runtimes["runner"], runner_context, task_one_expected | new_tenant_ids),
            (runtimes["api"], api_context, task_one_expected | new_tenant_ids),
            (restarted, task_two_context, task_two_expected | new_tenant_ids),
            (restarted, other_context, other_expected),
            (restarted, tenant_context, {tenant_v1.resource.resource_id, system_v1.resource.resource_id} | new_tenant_ids),
            (runtimes["curator"], curator_context, {system_v1.resource.resource_id}),
        ):
            listed = await _listed_controlled_ids(runtime, context, restarted_visible_ids)
            assert set(listed) == expected
            assert await _visible_controlled_count(runtime, context, restarted_visible_ids) == len(expected)
    finally:
        for runtime in runtimes.values():
            await runtime.close()
        if restarted is not None:
            await restarted.close()
        try:
            await _cleanup_geometry_rows(admin_engine, controlled_ids)
        finally:
            await admin_engine.dispose()
            if runtime_logins:
                _drop_test_runtime_logins(TEST_DATABASE_URL, runtime_logins)
