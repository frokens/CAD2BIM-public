"""PostgreSQL and formal-read helpers for IFC Golden Seed integration tests."""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import errors, sql
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from CAD2BIM_backend.application.data_runtime_service import (
    DataRuntimeService,
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.domain.data_runtime import DirectSessionBinding, ValidatedSource
from CAD2BIM_backend.domain.ifc_model import IfcModelInfo, IfcRunIdentity, InitializeIfcModelCommand
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.postgres.database import Database
from CAD2BIM_backend.infrastructure.postgres.identity_directory import (
    PostgresIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.postgres.repository import (
    PostgresControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.postgres.runtime_roles import (
    provision_runtime_login_roles,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantAwareAsyncSession,
    bind_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from tests.process_support.ifc_golden_seed.artifact_inventory import (
    StoragePortArtifactReader,
)
from tests.process_support.ifc_golden_seed.closure import SeedExportScope
from tests.process_support.ifc_golden_seed.exporter import (
    ExportedSeed,
    SeedExportMetadata,
    export_postgres_seed,
)
from tests.process_support.ifc_golden_seed.postgres_adapter import (
    PostgresSeedSnapshotReader,
)
from tests.process_support.ifc_revision_runtime import (
    TestIfcWriteStack,
    build_in_process_ifc_stack,
)


API_PASSWORD = "seed-api-password"
RUNNER_PASSWORD = "seed-runner-password"
MCP_PASSWORD = "seed-mcp-password"
_PASSWORD_ENVIRONMENT = {
    "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
    "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
    "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
}
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_MINIMAL_DXF = Path(__file__).resolve().parents[2] / "fixtures" / "cad" / "minimal_line.dxf"
TEST_PROJECT_NAME = "Golden Seed Foundation"


@dataclass(slots=True)
class SeedTestRuntime:
    """One independently disposable process composition over a test database."""

    database: Database
    repository: PostgresControlPlaneRepository
    identity_directory: PostgresIdentityDirectory
    storage: LocalStorage
    ifc: TestIfcWriteStack

    async def close(self) -> None:
        await self.ifc.runtime.close()
        await self.database.dispose()


@dataclass(frozen=True, slots=True)
class OpenedSeedScope:
    binding: DirectSessionBinding
    invocation_scope: InvocationScope


@dataclass(frozen=True, slots=True)
class ExportedTestSeed:
    exported: ExportedSeed
    source_binding: DirectSessionBinding
    source_invocation_scope: InvocationScope
    source_ifc_scope: IfcRunIdentity
    source_ifc_info: IfcModelInfo
    visible_package_alias: str
    visible_operation_alias: str
    late_package_alias: str
    late_operation_alias: str
    catalog_frontier: int


@dataclass(frozen=True, slots=True)
class IsolatedDatabasePair:
    admin_url_a: str
    admin_url_b: str
    runtime_url_a: str
    runtime_url_b: str
    created_by_test: bool


class IsolatedDatabaseUnavailable(RuntimeError):
    """The configured PostgreSQL account cannot create the required two databases."""


def runtime_database_url(value: str) -> str:
    """Return a non-superuser MCP runtime URL without logging credential material."""

    return (
        make_url(value)
        .set(
            drivername="postgresql+psycopg",
            username="cad2bim_mcp_login",
            password=MCP_PASSWORD,
        )
        .render_as_string(hide_password=False)
    )


def create_seed_runtime(
    database_url: str,
    storage_root: Path,
    *,
    storage_max_size_bytes: int = 100 * 1024 * 1024,
) -> SeedTestRuntime:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(
        engine,
        class_=TenantAwareAsyncSession,
        expire_on_commit=False,
    )
    database = Database(engine=engine, session_factory=sessions)
    repository = PostgresControlPlaneRepository(sessions)
    identity_directory = PostgresIdentityDirectory(sessions)
    storage = LocalStorage(storage_root, max_size_bytes=storage_max_size_bytes)
    return SeedTestRuntime(
        database=database,
        repository=repository,
        identity_directory=identity_directory,
        storage=storage,
        ifc=build_in_process_ifc_stack(repository, storage),
    )


async def open_seed_scope(
    runtime: SeedTestRuntime,
    *,
    configured_subject: str,
    session_name: str,
) -> OpenedSeedScope:
    sessions = DirectSessionService(
        runtime.repository,
        runtime.identity_directory,
        configured_subject,
        bind_tenant_execution_context,
    )
    resolver = DirectScopeResolver(
        runtime.repository,
        runtime.identity_directory,
        configured_subject,
        bind_tenant_execution_context,
    )
    opened = await sessions.open_direct_session(session_name)
    invocation = await resolver.resolve(
        opened.binding.direct_session_alias,
        command_id=f"seed-test:{uuid4().hex}",
    )
    return OpenedSeedScope(
        binding=opened.binding,
        invocation_scope=invocation,
    )


def ifc_scope(invocation: InvocationScope) -> IfcRunIdentity:
    task_id = invocation.identity.task_id
    if task_id is None:
        raise AssertionError("formal Direct scope did not resolve a Task")
    return IfcRunIdentity(
        owner_id=invocation.owner_id,
        tenant_id=invocation.identity.tenant_id,
        task_id=task_id,
        run_id=invocation.run_id,
    )


async def export_test_seed(
    runtime: SeedTestRuntime,
    *,
    output_root: Path,
    configured_subject: str,
    session_name: str,
) -> ExportedTestSeed:
    """Create a real Package/Operation/IFC source and export a pre-late frontier."""

    opened = await open_seed_scope(
        runtime,
        configured_subject=configured_subject,
        session_name=session_name,
    )
    invocation = opened.invocation_scope
    model_scope = ifc_scope(invocation)
    application = DataRuntimeService(
        runtime.repository,
        ArtifactService(runtime.repository, runtime.storage),
    )
    model_service = runtime.ifc.model_service
    source_bytes = _MINIMAL_DXF.read_bytes()
    with bind_tenant_execution_context(invocation.identity):
        visible = await application.import_cad_source(
            invocation,
            ValidatedSource(
                filename=_MINIMAL_DXF.name,
                media_type="application/dxf",
                content=source_bytes,
                source_identity="synthetic-seed:visible-minimal-line",
                adapter_metadata={"fixture_kind": "synthetic_seed_visible"},
            ),
        )
        initialized = await model_service.initialize(
            model_scope,
            InitializeIfcModelCommand(project_name=TEST_PROJECT_NAME),
        )
        source_ifc_info = await model_service.info(model_scope)
        fixed_entries = await runtime.repository.list_catalog(invocation)
        catalog_frontier = max(entry.catalog_revision for entry in fixed_entries)
        late = await application.import_cad_source(
            invocation,
            ValidatedSource(
                filename=_MINIMAL_DXF.name,
                media_type="application/dxf",
                content=source_bytes,
                source_identity="synthetic-seed:late-minimal-line",
                adapter_metadata={"fixture_kind": "synthetic_seed_late"},
            ),
        )

    export_scope = SeedExportScope.normalized(
        tenant_id=invocation.identity.tenant_id,
        principal_id=invocation.identity.principal_id,
        owner_id=invocation.owner_id,
        task_id=model_scope.task_id,
        run_id=invocation.run_id,
    )
    exported = await export_postgres_seed(
        snapshot_reader=PostgresSeedSnapshotReader(runtime.database.engine),
        artifact_reader=StoragePortArtifactReader(runtime.storage, export_scope),
        output_root=output_root,
        scope=export_scope,
        metadata=SeedExportMetadata(
            source_commit=hashlib.sha256(b"synthetic-seed-source").hexdigest(),
            source_migration_head="head",
            source_m4_round="synthetic",
            source_arm="A",
            source_direct_session_alias=opened.binding.direct_session_alias,
            created_at=datetime.now(UTC),
        ),
        catalog_frontier=catalog_frontier,
        include_ifc=True,
    )
    return ExportedTestSeed(
        exported=exported,
        source_binding=opened.binding,
        source_invocation_scope=invocation,
        source_ifc_scope=model_scope,
        source_ifc_info=source_ifc_info,
        visible_package_alias=visible.published.package.catalog_entry.alias,
        visible_operation_alias=initialized.operation_alias,
        late_package_alias=late.published.package.catalog_entry.alias,
        late_operation_alias=late.published.operation.catalog_entry.alias,
        catalog_frontier=catalog_frontier,
    )


def provision_seed_runtime_roles(database_url: str) -> None:
    provision_runtime_login_roles(database_url, environ=_PASSWORD_ENVIRONMENT)


def _migration_config(database_url: str) -> Config:
    config = Config(_REPOSITORY_ROOT / "alembic.ini")
    rendered = (
        make_url(database_url)
        .set(drivername="postgresql+psycopg")
        .render_as_string(hide_password=False)
        .replace("%", "%%")
    )
    config.set_main_option("sqlalchemy.url", rendered)
    return config


def _psycopg_url(value: str | URL) -> str:
    return make_url(value).set(drivername="postgresql").render_as_string(hide_password=False)


def _create_database(admin_url: str, database_name: str) -> None:
    try:
        with psycopg.connect(_psycopg_url(admin_url), autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(
                        sql.Identifier(database_name)
                    )
                )
    except errors.InsufficientPrivilege as exc:
        raise IsolatedDatabaseUnavailable(
            "PostgreSQL CREATEDB privilege is required for physical two-database isolation"
        ) from exc


def _drop_database(admin_url: str, database_name: str) -> None:
    with psycopg.connect(_psycopg_url(admin_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name))
            )


def _prepare_database(database_url: str) -> None:
    command.upgrade(_migration_config(database_url), "head")
    provision_seed_runtime_roles(database_url)


@contextmanager
def isolated_database_pair(base_database_url: str) -> Iterator[IsolatedDatabasePair]:
    """Yield two distinct migrated logical databases, deleting only test-created names."""

    explicit_a = os.getenv("CAD2BIM_TEST_SEED_DATABASE_URL_A", "").strip()
    explicit_b = os.getenv("CAD2BIM_TEST_SEED_DATABASE_URL_B", "").strip()
    if bool(explicit_a) != bool(explicit_b):
        raise IsolatedDatabaseUnavailable(
            "both CAD2BIM_TEST_SEED_DATABASE_URL_A and _B are required together"
        )
    if explicit_a and explicit_b:
        url_a = make_url(explicit_a)
        url_b = make_url(explicit_b)
        if (
            url_a.database == url_b.database
            and url_a.host == url_b.host
            and url_a.port == url_b.port
        ):
            raise IsolatedDatabaseUnavailable("the two configured Seed database URLs are identical")
        yield IsolatedDatabasePair(
            admin_url_a=explicit_a,
            admin_url_b=explicit_b,
            runtime_url_a=runtime_database_url(explicit_a),
            runtime_url_b=runtime_database_url(explicit_b),
            created_by_test=False,
        )
        return

    base = make_url(base_database_url).set(drivername="postgresql+psycopg")
    database_a = f"cad2bim_seed_a_{uuid4().hex}"
    database_b = f"cad2bim_seed_b_{uuid4().hex}"
    created: list[str] = []
    try:
        _create_database(base.render_as_string(hide_password=False), database_a)
        created.append(database_a)
        _create_database(base.render_as_string(hide_password=False), database_b)
        created.append(database_b)
        admin_a = base.set(database=database_a).render_as_string(hide_password=False)
        admin_b = base.set(database=database_b).render_as_string(hide_password=False)
        _prepare_database(admin_a)
        _prepare_database(admin_b)
        yield IsolatedDatabasePair(
            admin_url_a=admin_a,
            admin_url_b=admin_b,
            runtime_url_a=runtime_database_url(admin_a),
            runtime_url_b=runtime_database_url(admin_b),
            created_by_test=True,
        )
    finally:
        for database_name in reversed(created):
            _drop_database(base.render_as_string(hide_password=False), database_name)


__all__ = [
    "IsolatedDatabasePair",
    "IsolatedDatabaseUnavailable",
    "OpenedSeedScope",
    "ExportedTestSeed",
    "SeedTestRuntime",
    "TEST_PROJECT_NAME",
    "create_seed_runtime",
    "export_test_seed",
    "ifc_scope",
    "isolated_database_pair",
    "open_seed_scope",
    "provision_seed_runtime_roles",
    "runtime_database_url",
]
