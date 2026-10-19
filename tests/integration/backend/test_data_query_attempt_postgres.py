from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import delete, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import create_engine

from CAD2BIM_backend.application.data_runtime_service import (
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.domain.control_plane import ConflictError
from CAD2BIM_backend.domain.data_query_attempt import (
    DataQueryArtifactRef,
    DataQueryAttemptPreparation,
    DataQueryAttemptStatus,
    DataQueryAttemptTerminal,
    DataQueryEngine,
    DataQueryExactInputRef,
    DataQueryResultSummary,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.postgres.identity_directory import (
    PostgresIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactModel,
    DataCatalogEntryModel,
    DataPackageMemberModel,
    DataPackageModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository import (
    PostgresControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantAwareAsyncSession,
    bind_tenant_execution_context,
    set_invocation_scope,
)


pytestmark = [pytest.mark.integration, pytest.mark.network]
TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL", "").strip()
_REVISION = "20260904_0071"
_PREVIOUS_REVISION = "20260903_0070"


@pytest.mark.asyncio
async def test_attempt_sequence_lock_allows_concurrent_source_artifact_registration(
    monkeypatch,
) -> None:
    with _isolated_database("data_query_concurrency") as (config, _, scoped_url):
        command.upgrade(config, _REVISION)
        logins = _create_logins(scoped_url)
        runtime = _runtime(_runtime_url(scoped_url, logins["mcp"]))
        admin = create_async_engine(scoped_url, pool_pre_ping=True)
        try:
            suffix = uuid4().hex
            scope = await _open_scope(
                runtime, subject="concurrent-" + suffix, session_name="Concurrent queries " + suffix
            )
            exact_input = await _seed_exact_parquet_package(admin, runtime, scope, suffix=suffix)
            source = await _artifact(
                runtime,
                scope,
                suffix="source-" + suffix,
                kind="data_query_source",
                content_type="application/sql",
                digest="a" * 64,
            )
            preparation = DataQueryAttemptPreparation(
                attempt_id="attempt-" + suffix,
                run_id=scope.run_id,
                engine=DataQueryEngine.DUCKDB_SQL,
                source_artifact=DataQueryArtifactRef(
                    artifact_id=source.artifact_id,
                    version_id=source.version_id,
                    sha256=source.sha256,
                ),
                inputs=(exact_input,),
                page_size=10,
                request_identity="request-" + suffix,
                request_digest="e" * 64,
                created_at=datetime.now(UTC),
            )
            locked = asyncio.Event()
            release = asyncio.Event()
            original = runtime.repository._require_data_query_run

            async def hold_sequence_lock(session, selected_scope, task_id):
                await original(session, selected_scope, task_id)
                locked.set()
                await release.wait()

            monkeypatch.setattr(runtime.repository, "_require_data_query_run", hold_sequence_lock)
            with bind_tenant_execution_context(scope.identity):
                preparing = asyncio.create_task(
                    runtime.repository.prepare_data_query_attempt(scope, preparation)
                )
                await asyncio.wait_for(locked.wait(), 5)
                try:
                    # The registrar holds Task FOR UPDATE before its Run FK check.
                    # It must finish while the independent Attempt Run lock is held.
                    registered = await asyncio.wait_for(
                        _artifact(
                            runtime,
                            scope,
                            suffix="parallel-" + suffix,
                            kind="data_query_source",
                            content_type="application/sql",
                            digest="b" * 64,
                        ),
                        2,
                    )
                finally:
                    release.set()
                    prepared = await asyncio.wait_for(preparing, 5)
                assert registered.sha256 == "b" * 64
                assert prepared.attempt.sequence == 1
                monkeypatch.setattr(runtime.repository, "_require_data_query_run", original)
                requests = [
                    preparation.model_copy(
                        update={
                            "attempt_id": f"attempt-{suffix}-{i}",
                            "request_identity": f"request-{suffix}-{i}",
                        }
                    )
                    for i in range(3)
                ]
                results = await asyncio.gather(
                    *(
                        runtime.repository.prepare_data_query_attempt(scope, item)
                        for item in requests
                    )
                )
                assert sorted(item.attempt.sequence for item in results) == [2, 3, 4]
        finally:
            await runtime.close()
            await admin.dispose()
            _drop_logins(scoped_url, logins)


@dataclass(frozen=True, slots=True)
class _Login:
    name: str
    privilege: str
    password: str


@dataclass(slots=True)
class _Runtime:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    repository: PostgresControlPlaneRepository
    identity: PostgresIdentityDirectory

    async def close(self) -> None:
        await self.engine.dispose()


def _sync_url(value: str) -> str:
    return (
        make_url(value)
        .set(drivername="postgresql+psycopg")
        .render_as_string(hide_password=False)
    )


def _psycopg_url(value: str) -> str:
    return make_url(value).set(drivername="postgresql").render_as_string(hide_password=False)


@contextmanager
def _isolated_database(prefix: str) -> Iterator[tuple[Config, Engine, str]]:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")

    schema = f"{prefix}_{uuid4().hex}"
    admin_engine = create_engine(_sync_url(TEST_DATABASE_URL), pool_pre_ping=True)
    scoped_url = make_url(_sync_url(TEST_DATABASE_URL)).update_query_dict(
        {"options": f"-csearch_path={schema}"}
    )
    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        scoped_url.render_as_string(hide_password=False).replace("%", "%%"),
    )

    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped_engine = create_engine(scoped_url, pool_pre_ping=True)
        try:
            yield config, scoped_engine, scoped_url.render_as_string(hide_password=False)
        finally:
            scoped_engine.dispose()
    finally:
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def _create_logins(database_url: str) -> dict[str, _Login]:
    suffix = uuid4().hex[:12]
    logins = {
        kind: _Login(
            name=f"data_query_{kind}_{suffix}",
            privilege=f"cad2bim_{kind}_runtime",
            password=f"data-query-{kind}-{suffix}",
        )
        for kind in ("api", "mcp", "runner")
    }
    with psycopg.connect(_psycopg_url(database_url)) as connection:
        with connection.cursor() as cursor:
            for login in logins.values():
                cursor.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
                    ).format(sql.Identifier(login.name), sql.Literal(login.password))
                )
                cursor.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(login.privilege),
                        sql.Identifier(login.name),
                    )
                )
    return logins


def _drop_logins(database_url: str, logins: dict[str, _Login]) -> None:
    with psycopg.connect(_psycopg_url(database_url)) as connection:
        with connection.cursor() as cursor:
            for login in logins.values():
                cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login.name)))


def _runtime_url(scoped_url: str, login: _Login) -> str:
    return (
        make_url(scoped_url)
        .set(
            drivername="postgresql+psycopg",
            username=login.name,
            password=login.password,
        )
        .render_as_string(hide_password=False)
    )


def _runtime(database_url: str) -> _Runtime:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(
        engine,
        class_=TenantAwareAsyncSession,
        expire_on_commit=False,
    )
    return _Runtime(
        engine=engine,
        sessions=sessions,
        repository=PostgresControlPlaneRepository(sessions),
        identity=PostgresIdentityDirectory(sessions),
    )


async def _open_scope(
    runtime: _Runtime,
    *,
    subject: str,
    session_name: str,
) -> InvocationScope:
    session_service = DirectSessionService(
        runtime.repository,
        runtime.identity,
        subject,
        bind_tenant_execution_context,
    )
    resolver = DirectScopeResolver(
        runtime.repository,
        runtime.identity,
        subject,
        bind_tenant_execution_context,
    )
    opened = await session_service.open_direct_session(session_name)
    return await resolver.resolve(
        opened.binding.direct_session_alias,
        command_id=f"data-query-resolve-{uuid4().hex}",
    )


async def _artifact(
    runtime: _Runtime,
    scope: InvocationScope,
    *,
    suffix: str,
    kind: str,
    content_type: str,
    digest: str,
):
    with bind_tenant_execution_context(scope.identity):
        return await runtime.repository.register_artifact(
            scope.owner_id,
            scope.identity.task_id or "",
            artifact_id=f"data-query-artifact-{suffix}",
            version_no=1,
            filename=f"{suffix}.bin",
            content_type=content_type,
            size_bytes=16,
            sha256=digest,
            idempotency_key=f"data-query-artifact-key-{suffix}",
            kind=kind,
            run_id=scope.run_id,
        )


async def _seed_exact_parquet_package(
    admin: AsyncEngine,
    runtime: _Runtime,
    scope: InvocationScope,
    *,
    suffix: str,
) -> DataQueryExactInputRef:
    payload = await _artifact(
        runtime,
        scope,
        suffix=f"payload-{suffix}",
        kind="data_payload",
        content_type="application/vnd.apache.parquet",
        digest="b" * 64,
    )
    package_id = f"data-query-package-{suffix}"
    member_id = f"data-query-member-{suffix}"
    task_id = scope.identity.task_id or ""
    async with admin.begin() as connection:
        await connection.execute(
            DataPackageModel.__table__.insert().values(
                package_id=package_id,
                owner_id=scope.owner_id,
                tenant_id=scope.identity.tenant_id,
                task_id=task_id,
                created_by_run_id=scope.run_id,
                name=f"Data Query fixture {suffix}",
                package_kind="derived",
                source_filename=None,
                description_title="Data Query exact input",
                summary="Exact immutable Parquet input for DataQueryAttempt integration tests.",
                section_titles_json=[],
                idempotency_key=f"data-query-package-key-{suffix}",
                request_fingerprint="c" * 64,
            )
        )
        await connection.execute(
            DataPackageMemberModel.__table__.insert().values(
                member_id=member_id,
                owner_id=scope.owner_id,
                tenant_id=scope.identity.tenant_id,
                task_id=task_id,
                package_id=package_id,
                member_name="payloads/rows.parquet",
                member_kind="payload",
                artifact_id=payload.artifact_id,
                artifact_version_id=payload.version_id,
                artifact_sha256=payload.sha256,
                media_type="application/vnd.apache.parquet",
                row_count=1,
                logical_payload_name="rows",
                payload_description="One exact test row.",
                logical_key_fields_json=["row_id"],
                arrow_schema_json=[{"name": "row_id", "type": "int64"}],
                logical_byte_count=16,
                json_kind=None,
            )
        )
        await connection.execute(
            DataCatalogEntryModel.__table__.insert().values(
                catalog_entry_id=f"data-query-catalog-{suffix}",
                owner_id=scope.owner_id,
                tenant_id=scope.identity.tenant_id,
                task_id=task_id,
                run_id=scope.run_id,
                resource_kind="package",
                alias="data_001",
                ordinal=1,
                catalog_revision=1,
                package_id=package_id,
                operation_id=None,
            )
        )
    return DataQueryExactInputRef(
        ordinal=1,
        input_name="rows",
        package_id=package_id,
        payload_member_id=member_id,
    )


def _seed_identity_and_run(engine: Engine, *, suffix: str) -> tuple[str, str, str, str]:
    tenant_id = f"tenant-{suffix}"
    owner_id = f"owner-{suffix}"
    task_id = f"task-{suffix}"
    run_id = f"run-{suffix}"
    values = {
        "principal_id": f"principal-{suffix}",
        "tenant_id": tenant_id,
        "owner_id": owner_id,
        "task_id": task_id,
        "run_id": run_id,
        "run_key": f"run-key-{suffix}",
        "thread_id": f"thread-{suffix}",
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO principals (principal_id, issuer, subject, status) "
                "VALUES (:principal_id, 'test-issuer', :owner_id, 'active')"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO tenants (tenant_id, kind, status) "
                "VALUES (:tenant_id, 'personal', 'active')"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO tenant_principals (tenant_id, principal_id, access) "
                "VALUES (:tenant_id, :principal_id, 'owner')"
            ),
            values,
        )
        connection.execute(
            text("INSERT INTO users (owner_id, tenant_id) VALUES (:owner_id, :tenant_id)"),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO tasks (task_id, owner_id, tenant_id, title, metadata_json, "
                "status, active_module) VALUES (:task_id, :owner_id, :tenant_id, "
                "'Data Query downgrade fixture', '{}'::jsonb, 'running', 'data_runtime')"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO runs (run_id, owner_id, tenant_id, task_id, module, stage_key, "
                "input_payload, idempotency_key, request_fingerprint, thread_id, status, "
                "execution_protocol_status, execution_mode) VALUES (:run_id, :owner_id, "
                ":tenant_id, :task_id, 'data_runtime', 'direct', '{}'::jsonb, :run_key, "
                "repeat('a', 64), :thread_id, 'running', 'not_applicable', 'direct_mcp')"
            ),
            values,
        )
    return owner_id, tenant_id, task_id, run_id


def test_0071_from_0070_and_empty_downgrade_roundtrip() -> None:
    with _isolated_database("data_query_roundtrip") as (config, engine, _scoped_url):
        command.upgrade(config, _PREVIOUS_REVISION)
        command.upgrade(config, _REVISION)
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == _REVISION
            )
            assert {
                "data_query_attempts",
                "data_query_inputs",
            } == set(
                connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema() "
                        "AND table_name IN ('data_query_attempts', 'data_query_inputs')"
                    )
                ).scalars()
            )

        command.downgrade(config, _PREVIOUS_REVISION)
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == _PREVIOUS_REVISION
            )

        command.upgrade(config, _REVISION)


def test_0071_orphan_source_artifact_blocks_downgrade_without_rewriting_history() -> None:
    with _isolated_database("data_query_orphan") as (config, engine, _scoped_url):
        command.upgrade(config, _REVISION)
        owner_id, tenant_id, task_id, run_id = _seed_identity_and_run(
            engine, suffix=uuid4().hex
        )
        artifact_id = f"data-query-orphan-{uuid4().hex}"
        with engine.begin() as connection:
            connection.execute(
                ArtifactModel.__table__.insert().values(
                    artifact_id=artifact_id,
                    owner_id=owner_id,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    run_id=run_id,
                    kind="data_query_source",
                    filename="orphan.sql",
                    content_type="application/sql",
                    status="available",
                    current_version_id=None,
                    idempotency_key=f"data-query-orphan-key-{artifact_id}",
                    request_fingerprint="d" * 64,
                )
            )

        with pytest.raises(DBAPIError, match="Data Query history is not empty"):
            command.downgrade(config, _PREVIOUS_REVISION)

        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == _REVISION
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM artifacts WHERE artifact_id = :artifact_id"),
                    {"artifact_id": artifact_id},
                ).scalar_one()
                == 1
            )


@pytest.mark.asyncio
async def test_data_query_attempt_repository_is_exact_run_scoped_and_append_only() -> None:
    with _isolated_database("data_query_repository") as (config, sync_engine, scoped_url):
        command.upgrade(config, _REVISION)
        logins: dict[str, _Login] = {}
        runtimes: dict[str, _Runtime] = {}
        admin = create_async_engine(scoped_url, pool_pre_ping=True)
        try:
            logins = _create_logins(scoped_url)
            runtimes = {
                kind: _runtime(_runtime_url(scoped_url, login))
                for kind, login in logins.items()
            }
            suffix = uuid4().hex
            task_one = await _open_scope(
                runtimes["mcp"],
                subject=f"data-query-owner-{suffix}",
                session_name=f"Data Query one {suffix}",
            )
            task_two = await _open_scope(
                runtimes["mcp"],
                subject=f"data-query-owner-{suffix}",
                session_name=f"Data Query two {suffix}",
            )
            other_tenant = await _open_scope(
                runtimes["mcp"],
                subject=f"data-query-other-{suffix}",
                session_name=f"Data Query other {suffix}",
            )
            assert task_one.identity.tenant_id == task_two.identity.tenant_id
            assert task_one.identity.task_id != task_two.identity.task_id
            assert task_one.identity.tenant_id != other_tenant.identity.tenant_id

            exact_input = await _seed_exact_parquet_package(
                admin,
                runtimes["mcp"],
                task_one,
                suffix=suffix,
            )
            source = await _artifact(
                runtimes["mcp"],
                task_one,
                suffix=f"source-{suffix}",
                kind="data_query_source",
                content_type="application/sql",
                digest="a" * 64,
            )
            source_ref = DataQueryArtifactRef(
                artifact_id=source.artifact_id,
                version_id=source.version_id,
                sha256=source.sha256,
            )
            created_at = datetime.now(UTC)
            preparation = DataQueryAttemptPreparation(
                attempt_id=f"data-query-attempt-{suffix}",
                run_id=task_one.run_id,
                engine=DataQueryEngine.DUCKDB_SQL,
                source_artifact=source_ref,
                inputs=(exact_input,),
                page_size=50,
                request_identity=f"data-query-request-{suffix}",
                request_digest="e" * 64,
                created_at=created_at,
            )
            with bind_tenant_execution_context(task_one.identity):
                prepared = await runtimes["mcp"].repository.prepare_data_query_attempt(
                    task_one,
                    preparation,
                )
            assert prepared.created is True
            assert prepared.attempt.status is DataQueryAttemptStatus.PREPARED
            assert prepared.attempt.inputs == (exact_input,)
            assert prepared.attempt.source_artifact == source_ref

            replay_request = preparation.model_copy(
                update={
                    "created_at": created_at + timedelta(seconds=1),
                }
            )
            with bind_tenant_execution_context(task_one.identity):
                replay = await runtimes["mcp"].repository.prepare_data_query_attempt(
                    task_one,
                    replay_request,
                )
            assert replay.created is False
            assert replay.attempt == prepared.attempt

            hidden_run = task_one.model_copy(update={"run_id": f"hidden-run-{suffix}"})
            for hidden_scope in (hidden_run, task_two, other_tenant):
                with bind_tenant_execution_context(hidden_scope.identity):
                    assert (
                        await runtimes["mcp"].repository.get_data_query_attempt(
                            hidden_scope,
                            attempt_id=preparation.attempt_id,
                        )
                        is None
                    )

            api_scope = task_one.model_copy(
                update={
                    "identity": task_one.identity.model_copy(
                        update={"command_id": f"data-query-api-{suffix}"}
                    )
                }
            )
            with bind_tenant_execution_context(api_scope.identity):
                assert (
                    await runtimes["api"].repository.get_data_query_attempt(
                        api_scope,
                        attempt_id=preparation.attempt_id,
                    )
                    == prepared.attempt
                )
                with pytest.raises(SQLAlchemyError):
                    await runtimes["api"].repository.prepare_data_query_attempt(
                        api_scope,
                        preparation.model_copy(
                            update={
                                "attempt_id": f"api-forbidden-{suffix}",
                                "request_identity": f"api-forbidden-{suffix}",
                            }
                        ),
                    )

            with bind_tenant_execution_context(task_one.identity):
                assert (
                    await runtimes["runner"].repository.get_data_query_attempt(
                        task_one,
                        attempt_id=preparation.attempt_id,
                    )
                    == prepared.attempt
                )

            terminal = DataQueryAttemptTerminal(
                status=DataQueryAttemptStatus.SUCCEEDED,
                result_summary=DataQueryResultSummary(
                    exact_input_digest="1" * 64,
                    query_fingerprint="2" * 64,
                    row_schema_digest="3" * 64,
                    row_count=1,
                    snapshot_size_bytes=16,
                    snapshot_sha256="7" * 64,
                ),
                finished_at=created_at + timedelta(seconds=2),
            )
            with bind_tenant_execution_context(task_one.identity):
                completed = await runtimes["mcp"].repository.commit_data_query_attempt_terminal(
                    task_one,
                    attempt_id=preparation.attempt_id,
                    terminal=terminal,
                )
                terminal_replay = (
                    await runtimes["mcp"].repository.commit_data_query_attempt_terminal(
                        task_one,
                        attempt_id=preparation.attempt_id,
                        terminal=terminal,
                    )
                )
                with pytest.raises(ConflictError, match="terminal state is immutable"):
                    await runtimes["mcp"].repository.commit_data_query_attempt_terminal(
                        task_one,
                        attempt_id=preparation.attempt_id,
                        terminal=terminal.model_copy(
                            update={"finished_at": terminal.finished_at + timedelta(seconds=1)}
                        ),
                    )
            assert completed.status is DataQueryAttemptStatus.SUCCEEDED
            assert terminal_replay == completed

            repair_source = await _artifact(
                runtimes["mcp"],
                task_one,
                suffix=f"repair-source-{suffix}",
                kind="data_query_source",
                content_type="application/sql",
                digest="f" * 64,
            )
            repair = preparation.model_copy(
                update={
                    "attempt_id": f"data-query-repair-{suffix}",
                    "source_artifact": DataQueryArtifactRef(
                        artifact_id=repair_source.artifact_id,
                        version_id=repair_source.version_id,
                        sha256=repair_source.sha256,
                    ),
                    "repair_parent_attempt_id": preparation.attempt_id,
                    "request_identity": f"data-query-repair-request-{suffix}",
                    "request_digest": "4" * 64,
                    "created_at": created_at + timedelta(seconds=3),
                }
            )
            with bind_tenant_execution_context(task_one.identity):
                repaired = await runtimes["mcp"].repository.prepare_data_query_attempt(
                    task_one,
                    repair,
                )
            assert repaired.attempt.repair_parent_attempt_id == preparation.attempt_id
            assert repaired.attempt.inputs == completed.inputs
            assert repaired.attempt.engine is completed.engine

            mismatched_repair = repair.model_copy(
                update={
                    "attempt_id": f"data-query-mismatched-repair-{suffix}",
                    "inputs": (
                        exact_input.model_copy(update={"input_name": "different_rows"}),
                    ),
                    "request_identity": f"data-query-mismatched-request-{suffix}",
                    "request_digest": "8" * 64,
                    "created_at": created_at + timedelta(seconds=4),
                }
            )
            with bind_tenant_execution_context(task_one.identity):
                with pytest.raises(ConflictError, match="inherit exact inputs"):
                    await runtimes["mcp"].repository.prepare_data_query_attempt(
                        task_one,
                        mismatched_repair,
                    )

            with bind_tenant_execution_context(task_one.identity):
                with pytest.raises(DBAPIError):
                    async with runtimes["mcp"].sessions() as session, session.begin():
                        await set_invocation_scope(session, task_one)
                        await session.execute(
                            text(
                                "UPDATE data_query_attempts SET request_digest = :digest "
                                "WHERE attempt_id = :attempt_id"
                            ),
                            {"digest": "5" * 64, "attempt_id": preparation.attempt_id},
                        )
                with pytest.raises(DBAPIError):
                    async with runtimes["mcp"].sessions() as session, session.begin():
                        await set_invocation_scope(session, task_one)
                        await session.execute(
                            text("DELETE FROM data_query_inputs WHERE attempt_id = :attempt_id"),
                            {"attempt_id": repair.attempt_id},
                        )

            ordinary_id = f"ordinary-artifact-{suffix}"
            async with admin.begin() as connection:
                await connection.execute(
                    ArtifactModel.__table__.insert().values(
                        artifact_id=ordinary_id,
                        owner_id=task_one.owner_id,
                        tenant_id=task_one.identity.tenant_id,
                        task_id=task_one.identity.task_id,
                        run_id=task_one.run_id,
                        kind="ordinary_fixture",
                        filename="ordinary.bin",
                        content_type="application/octet-stream",
                        status="available",
                        current_version_id=None,
                        idempotency_key=f"ordinary-artifact-key-{suffix}",
                        request_fingerprint="6" * 64,
                    )
                )
                await connection.execute(
                    delete(ArtifactModel).where(ArtifactModel.artifact_id == ordinary_id)
                )
                assert (
                    await connection.scalar(
                        select(ArtifactModel.artifact_id).where(
                            ArtifactModel.artifact_id == ordinary_id
                        )
                    )
                    is None
                )
        finally:
            for runtime in runtimes.values():
                await runtime.close()
            await admin.dispose()
            if logins:
                _drop_logins(scoped_url, logins)

        with pytest.raises(DBAPIError, match="Data Query history is not empty"):
            command.downgrade(config, _PREVIOUS_REVISION)
        with sync_engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == _REVISION
            )
