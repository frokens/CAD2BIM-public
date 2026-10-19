from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
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
from CAD2BIM_backend.domain.control_plane import ConflictError, ResourceNotFoundError
from CAD2BIM_backend.domain.geometry_query import (
    GeometryQueryArtifactRef,
    GeometryQueryAttemptPreparation,
    GeometryQueryAttemptStatus,
    GeometryQueryAttemptTerminal,
    GeometryQueryDiagnostic,
    GeometryQueryTargetKind,
)
from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometryFormat
from CAD2BIM_backend.domain.geometry_inspection import InspectionFragmentSelection, InspectionResultCoordinates
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.infrastructure.postgres.identity_directory import (
    PostgresIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactModel,
    ArtifactVersionModel,
    GeometryQueryAttemptModel,
    GeometryResourceModel,
    GeometryResourceVersionModel,
    GeometryResourceVersionAssetModel,
    RunModel,
    TaskModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository import (
    PostgresControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantAwareAsyncSession,
    bind_tenant_execution_context,
    set_tenant_execution_context,
)


pytestmark = [pytest.mark.integration, pytest.mark.network]
TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL", "").strip()


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
    return make_url(value).set(drivername="postgresql").render_as_string(hide_password=False)


def _create_logins(database_url: str) -> dict[str, _Login]:
    suffix = uuid4().hex[:12]
    logins = {
        kind: _Login(
            name=f"b302_{kind}_{suffix}",
            privilege=f"cad2bim_{kind}_runtime",
            password=f"b302-{kind}-{suffix}",
        )
        for kind in ("api", "mcp")
    }
    with psycopg.connect(_sync_url(database_url)) as connection:
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
    with psycopg.connect(_sync_url(database_url)) as connection:
        with connection.cursor() as cursor:
            for login in logins.values():
                cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login.name)))


def _runtime_url(database_url: str, login: _Login) -> str:
    return (
        make_url(database_url)
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


async def _open_context(
    runtime: _Runtime,
    *,
    subject: str,
    session_name: str,
) -> TenantExecutionContext:
    sessions = DirectSessionService(
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
    opened = await sessions.open_direct_session(session_name)
    invocation = await resolver.resolve(
        opened.binding.direct_session_alias,
        command_id=f"b302-resolve-{uuid4().hex}",
    )
    assert invocation.identity.task_id is not None
    return invocation.identity


async def _task_owner_and_run(
    admin: AsyncEngine,
    context: TenantExecutionContext,
) -> tuple[str, str]:
    async with admin.connect() as connection:
        owner_id = await connection.scalar(
            select(TaskModel.owner_id).where(TaskModel.task_id == context.task_id)
        )
        run_id = await connection.scalar(
            select(RunModel.run_id).where(RunModel.task_id == context.task_id)
        )
    assert owner_id is not None and run_id is not None
    return owner_id, run_id


async def _artifact(
    runtime: _Runtime,
    context: TenantExecutionContext,
    *,
    owner_id: str,
    run_id: str,
    suffix: str,
    digest: str,
    filename: str | None = None,
    content_type: str = "application/json",
    kind: str = "geometry_query",
    version_metadata: dict[str, object] | None = None,
) -> GeometryQueryArtifactRef:
    with bind_tenant_execution_context(context):
        record = await runtime.repository.register_artifact(
            owner_id,
            context.task_id or "",
            artifact_id=f"b302-artifact-{suffix}",
            version_no=1,
            filename=filename or f"{suffix}.json",
            content_type=content_type,
            size_bytes=2,
            sha256=digest,
            idempotency_key=f"b302-artifact-key-{suffix}",
            kind=kind,
            run_id=run_id,
            version_metadata=version_metadata,
        )
    return GeometryQueryArtifactRef(
        artifact_id=record.artifact_id,
        version_id=record.version_id,
        sha256=record.sha256,
    )


@pytest.mark.asyncio
async def test_geometry_query_attempts_are_exact_task_scoped_and_one_way_terminal() -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")

    logins: dict[str, _Login] = {}
    runtimes: dict[str, _Runtime] = {}
    admin = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    suffix = uuid4().hex
    attempt_ids = {
        f"b302-attempt-{suffix}",
        f"b302-repair-{suffix}",
        f"b307-preview-attempt-{suffix}",
    }
    artifact_ids: set[str] = set()
    resource_id = f"b302-resource-{suffix}"
    resource_version_id = f"b302-resource-version-{suffix}"
    try:
        logins = _create_logins(TEST_DATABASE_URL)
        runtimes = {
            kind: _runtime(_runtime_url(TEST_DATABASE_URL, login)) for kind, login in logins.items()
        }
        subject = f"b302-subject-{suffix}"
        task_one = await _open_context(
            runtimes["mcp"], subject=subject, session_name=f"B3.02 one {suffix}"
        )
        task_two = await _open_context(
            runtimes["mcp"], subject=subject, session_name=f"B3.02 two {suffix}"
        )
        other_tenant = await _open_context(
            runtimes["mcp"],
            subject=f"b302-other-{suffix}",
            session_name=f"B3.02 other {suffix}",
        )
        assert task_one.tenant_id == task_two.tenant_id
        assert task_one.task_id != task_two.task_id
        assert task_one.tenant_id != other_tenant.tenant_id

        owner_one, run_one = await _task_owner_and_run(admin, task_one)
        owner_two, run_two = await _task_owner_and_run(admin, task_two)
        source = await _artifact(
            runtimes["mcp"],
            task_one,
            owner_id=owner_one,
            run_id=run_one,
            suffix=f"source-{suffix}",
            digest="a" * 64,
        )
        result = await _artifact(
            runtimes["mcp"],
            task_one,
            owner_id=owner_one,
            run_id=run_one,
            suffix=f"result-{suffix}",
            digest="b" * 64,
        )
        task_two_source = await _artifact(
            runtimes["mcp"],
            task_two,
            owner_id=owner_two,
            run_id=run_two,
            suffix=f"task-two-{suffix}",
            digest="c" * 64,
        )
        artifact_ids.update((source.artifact_id, result.artifact_id, task_two_source.artifact_id))
        inspection = await _artifact(
            runtimes["mcp"],
            task_one,
            owner_id=owner_one,
            run_id=run_one,
            suffix=f"inspection-{suffix}",
            digest="4" * 64,
            filename="geometry-inspection.zip",
            content_type="application/vnd.cad2bim.geometry-inspection+zip",
            kind="geometry_inspection",
            version_metadata={
                "source_kind": "family_realization",
                "request_digest": "5" * 64,
            },
        )
        artifact_ids.add(inspection.artifact_id)

        async with admin.begin() as connection:
            await connection.execute(
                GeometryResourceModel.__table__.insert().values(
                    resource_id=resource_id,
                    resource_kind="mapped_model",
                    owner_scope="SYSTEM",
                    owner_tenant_id=None,
                    owner_task_id=None,
                    display_name="B3.02 neutral geometry fixture",
                )
            )
            await connection.execute(
                GeometryResourceVersionModel.__table__.insert().values(
                    resource_version_id=resource_version_id,
                    resource_id=resource_id,
                    version_no=1,
                    origin_kind="neutral_fixture",
                    content_digest="d" * 64,
                    manifest_digest="e" * 64,
                    manifest_json={},
                    lineage_json={},
                )
            )
            for role, asset_format, media_type, digest in (
                ("canonical_geometry", "occt_brep_v1", "application/vnd.opencascade.brep", "f" * 64),
                ("manifest", "geometry_resource_manifest_v1", "application/json", "e" * 64),
            ):
                await connection.execute(GeometryResourceVersionAssetModel.__table__.insert().values(
                    resource_version_id=resource_version_id, asset_role=role, asset_format=asset_format,
                    filename=f"{role}.fixture", content_type=media_type, size_bytes=2, sha256=digest,
                ))

        created_at = datetime.now(UTC)
        preparation = GeometryQueryAttemptPreparation(
            attempt_id=f"b302-attempt-{suffix}",
            origin_run_id=run_one,
            resource_version_id=resource_version_id,
            source_artifact=source,
            request_identity=f"b302-request-{suffix}",
            request_digest="f" * 64,
            created_at=created_at,
        )
        with bind_tenant_execution_context(task_one):
            prepared = await runtimes["mcp"].repository.create_geometry_query_attempt(
                task_one, preparation
            )
        assert prepared.status is GeometryQueryAttemptStatus.PREPARED
        assert prepared.origin_run_id == run_one

        replay_request = preparation.model_copy(
            update={
                "attempt_id": f"ignored-retry-{suffix}",
                "created_at": created_at + timedelta(seconds=1),
            }
        )
        with bind_tenant_execution_context(task_one):
            replay = await runtimes["mcp"].repository.create_geometry_query_attempt(
                task_one, replay_request
            )
        assert replay == prepared

        for hidden_context in (task_two, other_tenant):
            with bind_tenant_execution_context(hidden_context):
                assert (
                    await runtimes["mcp"].repository.get_geometry_query_attempt(
                        hidden_context,
                        attempt_id=prepared.attempt_id,
                    )
                    is None
                )

        forged = preparation.model_copy(
            update={
                "attempt_id": f"forged-{suffix}",
                "origin_run_id": run_two,
                "source_artifact": task_two_source,
                "request_identity": f"forged-request-{suffix}",
                "request_digest": "0" * 64,
            }
        )
        with bind_tenant_execution_context(task_one):
            with pytest.raises(ResourceNotFoundError, match="origin Run"):
                await runtimes["mcp"].repository.create_geometry_query_attempt(task_one, forged)

        terminal = GeometryQueryAttemptTerminal(
            status=GeometryQueryAttemptStatus.SUCCEEDED,
            result_artifact=result,
            diagnostics=(
                GeometryQueryDiagnostic(code="QUERY_COMPLETE", message="query completed"),
            ),
            finished_at=created_at + timedelta(seconds=2),
        )
        with bind_tenant_execution_context(task_one):
            completed = await runtimes["mcp"].repository.complete_geometry_query_attempt(
                task_one,
                attempt_id=prepared.attempt_id,
                terminal=terminal,
            )
        assert completed.status is GeometryQueryAttemptStatus.SUCCEEDED
        assert completed.result_artifact == result

        with bind_tenant_execution_context(task_one):
            terminal_replay = await runtimes["mcp"].repository.complete_geometry_query_attempt(
                task_one,
                attempt_id=prepared.attempt_id,
                terminal=terminal.model_copy(
                    update={"finished_at": terminal.finished_at + timedelta(seconds=1)}
                ),
            )
            with pytest.raises(ConflictError, match="different terminal state"):
                await runtimes["mcp"].repository.complete_geometry_query_attempt(
                    task_one,
                    attempt_id=prepared.attempt_id,
                    terminal=GeometryQueryAttemptTerminal(
                        status=GeometryQueryAttemptStatus.FAILED,
                        diagnostics=(
                            GeometryQueryDiagnostic(
                                code="WORKER_FAILED", message="different terminal"
                            ),
                        ),
                        finished_at=terminal.finished_at + timedelta(seconds=2),
                    ),
                )
        assert terminal_replay == completed

        repair = GeometryQueryAttemptPreparation(
            attempt_id=f"b302-repair-{suffix}",
            origin_run_id=run_one,
            resource_version_id=resource_version_id,
            source_artifact=source,
            repair_parent_attempt_id=prepared.attempt_id,
            request_identity=f"b302-repair-request-{suffix}",
            request_digest="1" * 64,
            created_at=created_at + timedelta(seconds=3),
        )
        with bind_tenant_execution_context(task_one):
            repaired = await runtimes["mcp"].repository.create_geometry_query_attempt(
                task_one, repair
            )
        assert repaired.repair_parent_attempt_id == prepared.attempt_id
        assert repaired.resource_version_id == prepared.resource_version_id

        inspection_preparation = GeometryQueryAttemptPreparation(
            attempt_id=f"b307-preview-attempt-{suffix}",
            origin_run_id=run_one,
            target_kind=GeometryQueryTargetKind.GEOMETRY_INSPECTION,
            resource_version_id=None,
            target_artifact=inspection,
            selection=InspectionFragmentSelection(fragment_key="piece_a"),
            coordinate_space=InspectionResultCoordinates(),
            source_artifact=source,
            request_identity=f"b307-preview-request-{suffix}",
            request_digest="7" * 64,
            created_at=created_at + timedelta(seconds=4),
        )
        with bind_tenant_execution_context(task_one):
            inspection_attempt = await runtimes["mcp"].repository.create_geometry_query_attempt(
                task_one, inspection_preparation
            )
        assert inspection_attempt.target_kind is GeometryQueryTargetKind.GEOMETRY_INSPECTION
        assert inspection_attempt.target_artifact == inspection
        assert inspection_attempt.selection == inspection_preparation.selection
        assert inspection_attempt.coordinate_space == inspection_preparation.coordinate_space
        with bind_tenant_execution_context(task_one):
            assert await runtimes["mcp"].repository.create_geometry_query_attempt(
                task_one, inspection_preparation,
            ) == inspection_attempt
            with pytest.raises(ConflictError, match="different inputs"):
                await runtimes["mcp"].repository.create_geometry_query_attempt(task_one,
                    inspection_preparation.model_copy(update={"selection": InspectionFragmentSelection(fragment_key="piece_b")}))
            for kind in (GeometryQueryTargetKind.PRODUCT_REALIZATION_PREVIEW, GeometryQueryTargetKind.GEOMETRY_PREVIEW):
                with pytest.raises(ValueError, match="retired preview targets"):
                    await runtimes["mcp"].repository.create_geometry_query_attempt(
                        task_one, inspection_preparation.model_copy(update={
                            "target_kind": kind, "target_geometry_format": CanonicalGeometryFormat.OCCT_BREP_V1,
                            "selection": None, "coordinate_space": None,
                        }),
                    )
        with bind_tenant_execution_context(task_two):
            assert await runtimes["mcp"].repository.get_geometry_query_attempt(
                task_two, attempt_id=inspection_attempt.attempt_id,
            ) is None

        cross_target_repair = inspection_preparation.model_copy(
            update={
                "attempt_id": f"b307-cross-target-{suffix}",
                "repair_parent_attempt_id": prepared.attempt_id,
                "request_identity": f"b307-cross-target-request-{suffix}",
                "request_digest": "8" * 64,
            }
        )
        with bind_tenant_execution_context(task_one):
            with pytest.raises(ConflictError, match="preserve the exact target"):
                await runtimes["mcp"].repository.create_geometry_query_attempt(
                    task_one, cross_target_repair
                )

        # Terminal filtering happens in SQL before LIMIT/keyset: the newer
        # PREPARED repair must not hide the older terminal attempt.
        with bind_tenant_execution_context(task_one):
            terminal_page = await runtimes["mcp"].repository.list_terminal_geometry_query_attempts(
                task_one,
                statuses=(
                    GeometryQueryAttemptStatus.SUCCEEDED,
                    GeometryQueryAttemptStatus.FAILED,
                ),
                before_created_at=None,
                before_attempt_id=None,
                limit=1,
            )
        assert terminal_page == (completed,)

        with bind_tenant_execution_context(task_one):
            after_terminal = await runtimes["mcp"].repository.list_terminal_geometry_query_attempts(
                task_one,
                statuses=(GeometryQueryAttemptStatus.SUCCEEDED,),
                before_created_at=completed.created_at,
                before_attempt_id=completed.attempt_id,
                limit=1,
            )
        assert after_terminal == ()

        with bind_tenant_execution_context(task_two):
            hidden_terminal_page = await runtimes[
                "mcp"
            ].repository.list_terminal_geometry_query_attempts(
                task_two,
                statuses=(GeometryQueryAttemptStatus.SUCCEEDED,),
                before_created_at=None,
                before_attempt_id=None,
                limit=1,
            )
        assert hidden_terminal_page == ()

        failed_inspection_terminal = GeometryQueryAttemptTerminal(
            status=GeometryQueryAttemptStatus.FAILED,
            diagnostics=(GeometryQueryDiagnostic(
                code="INSPECTION_INTEGRITY_FAILED", message="The exact saved bundle is corrupt",
                phase="target", retryable=False,
            ),),
            finished_at=created_at + timedelta(seconds=5),
        )
        with bind_tenant_execution_context(task_one):
            failed_inspection = await runtimes["mcp"].repository.complete_geometry_query_attempt(
                task_one, attempt_id=inspection_attempt.attempt_id, terminal=failed_inspection_terminal,
            )
            restarted_repository = PostgresControlPlaneRepository(runtimes["mcp"].sessions)
            saved_failure = await restarted_repository.get_geometry_query_attempt(
                task_one, attempt_id=inspection_attempt.attempt_id,
            )
        assert saved_failure == failed_inspection
        assert saved_failure.diagnostics[0].phase == "target"
        assert saved_failure.diagnostics[0].retryable is False

        api_context = task_one.model_copy(update={"command_id": f"b302-api-{suffix}"})
        with bind_tenant_execution_context(api_context):
            with pytest.raises(SQLAlchemyError):
                await runtimes["api"].repository.create_geometry_query_attempt(
                    api_context,
                    repair.model_copy(
                        update={
                            "attempt_id": f"api-forbidden-{suffix}",
                            "request_identity": f"api-forbidden-{suffix}",
                        }
                    ),
                )

        with bind_tenant_execution_context(task_one):
            with pytest.raises(DBAPIError):
                async with runtimes["mcp"].sessions() as session, session.begin():
                    await set_tenant_execution_context(session, task_one)
                    await session.execute(
                        text(
                            "UPDATE geometry_query_attempts "
                            "SET request_digest = :changed WHERE attempt_id = :attempt_id"
                        ),
                        {"changed": "2" * 64, "attempt_id": prepared.attempt_id},
                    )
    finally:
        for runtime in runtimes.values():
            await runtime.close()
        try:
            async with admin.begin() as connection:
                await connection.execute(
                    delete(GeometryQueryAttemptModel).where(
                        GeometryQueryAttemptModel.attempt_id.in_(attempt_ids)
                    )
                )
                if artifact_ids:
                    await connection.execute(
                        update(ArtifactModel)
                        .where(ArtifactModel.artifact_id.in_(artifact_ids))
                        .values(current_version_id=None)
                    )
                    await connection.execute(
                        delete(ArtifactVersionModel).where(
                            ArtifactVersionModel.artifact_id.in_(artifact_ids)
                        )
                    )
                    await connection.execute(
                        delete(ArtifactModel).where(ArtifactModel.artifact_id.in_(artifact_ids))
                    )
                await connection.execute(
                    delete(GeometryResourceVersionAssetModel).where(
                        GeometryResourceVersionAssetModel.resource_version_id == resource_version_id
                    )
                )
                await connection.execute(
                    delete(GeometryResourceVersionModel).where(
                        GeometryResourceVersionModel.resource_version_id == resource_version_id
                    )
                )
                await connection.execute(
                    delete(GeometryResourceModel).where(
                        GeometryResourceModel.resource_id == resource_id
                    )
                )
        finally:
            await admin.dispose()
            if logins:
                _drop_logins(TEST_DATABASE_URL, logins)
