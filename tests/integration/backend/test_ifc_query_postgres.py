from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.data_runtime_service import (
    DataRuntimeService,
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.application.code_discovery_service import CodeDiscoveryService
from CAD2BIM_backend.application.exact_payload_resolver import ExactPayloadResolver
from CAD2BIM_backend.application.ifc_project_service import IfcProjectService
from CAD2BIM_backend.application.ifc_query_service import IfcQueryService
from CAD2BIM_backend.application.ifc_revision_reader import IfcRevisionReader
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity
from CAD2BIM_backend.domain.ifc_project import IfcProjectPatch, UpdateIfcProjectCommand
from CAD2BIM_backend.domain.control_plane import ResourceNotFoundError
from CAD2BIM_backend.domain.data_runtime import ValidatedSource
from CAD2BIM_backend.domain.ifc_query import (
    IfcQueryArtifactRef,
    IfcQueryAttemptPreparation,
    IfcQueryAttemptStatus,
    IfcQueryAttemptTerminal,
    IfcQueryBudgetSummary,
    IfcQueryExactInputRef,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.ifc_query import IfcOpenShellQueryWorker
from CAD2BIM_backend.infrastructure.postgres.database import Database
from CAD2BIM_backend.infrastructure.postgres.identity_directory import (
    PostgresIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactModel,
    CriticalOperationModel,
    DataPackageModel,
    IfcModelRevisionModel,
    IfcQueryAttemptModel,
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
    set_invocation_scope,
    set_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.source_text import TextSourcePatchAdapter
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.code.contracts import (
    CodeListFirstPageInput,
    SingleTextCodeReadInput,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.query.contracts import (
    DirectIfcQueryCursorInput,
    DirectIfcQueryFreshInput,
    DirectIfcQueryRepairInput,
    IfcQueryExactInput,
    IfcQuerySourceEdit,
)
from tests.process_support.ifc_revision_runtime import build_in_process_ifc_stack, InProcessIfcRevisionRuntime


pytestmark = [pytest.mark.integration, pytest.mark.network]
TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL", "").strip()
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "cad" / "minimal_line.dxf"
API_PASSWORD = "i0b304-api-password"
RUNNER_PASSWORD = "i0b304-runner-password"
MCP_PASSWORD = "i0b304-mcp-password"
CURSOR_KEY = base64.urlsafe_b64encode(b"i0b304-postgres-cursor-key-32b!!").rstrip(b"=").decode()


class _CommitResponseLossRepository:
    """Lose one response only after PostgreSQL has committed the terminal attempt."""

    def __init__(self, repository: PostgresControlPlaneRepository) -> None:
        self._repository = repository
        self.armed = False
        self.losses = 0

    def __getattr__(self, name: str):
        return getattr(self._repository, name)

    async def commit_ifc_query_attempt_terminal(self, context, *, attempt_id, terminal):
        committed = await self._repository.commit_ifc_query_attempt_terminal(
            context,
            attempt_id=attempt_id,
            terminal=terminal,
        )
        if self.armed:
            self.armed = False
            self.losses += 1
            raise RuntimeError("simulated committed response loss")
        return committed


class _ControlledWorker:
    """Count real worker dispatches and pause one after the exact inputs are fixed."""

    def __init__(self, delegate: IfcOpenShellQueryWorker) -> None:
        self._delegate = delegate
        self.calls = 0
        self.pause_next = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute_query(self, request, inputs=(), *, deadline=None):
        self.calls += 1
        if self.pause_next:
            self.pause_next = False
            self.started.set()
            await self.release.wait()
        return await self._delegate.execute_query(request, inputs, deadline=deadline)

    def execution_scope(self, *args, **kwargs):
        return self._delegate.execution_scope(*args, **kwargs)

    async def verify_ready(self) -> None:
        await self._delegate.verify_ready()

    async def close(self) -> None:
        await self._delegate.close()


@dataclass(slots=True)
class _Runtime:
    database: Database
    repository: PostgresControlPlaneRepository
    storage: LocalStorage
    sessions: DirectSessionService
    resolver: DirectScopeResolver
    query_service: IfcQueryService
    worker: _ControlledWorker
    response_loss: _CommitResponseLossRepository
    facade: DirectApplicationFacade

    async def close(self) -> None:
        try:
            await self.worker.close()
        finally:
            await self.database.dispose()


@dataclass(frozen=True, slots=True)
class _BusinessSnapshot:
    current_revision_id: str
    write_epoch: int
    revision_ids: tuple[str, ...]
    artifact_id: str
    artifact_version_id: str
    artifact_size_bytes: int
    artifact_sha256: str
    artifact_bytes: bytes
    critical_operation_ids: tuple[str, ...]
    data_package_ids: tuple[str, ...]


def _runtime_url(value: str, *, username: str, password: str) -> str:
    return (
        make_url(value)
        .set(
            drivername="postgresql+psycopg",
            username=username,
            password=password,
        )
        .render_as_string(hide_password=False)
    )


def _ifc_scope(scope: InvocationScope) -> IfcRunIdentity:
    return IfcRunIdentity(
        owner_id=scope.owner_id,
        tenant_id=scope.identity.tenant_id,
        task_id=scope.identity.task_id or "",
        run_id=scope.run_id,
    )


def _create_runtime(
    database_url: str,
    storage_root: Path,
    capacity_root: Path,
    *,
    subject: str,
) -> _Runtime:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(
        engine,
        class_=TenantAwareAsyncSession,
        expire_on_commit=False,
    )
    database = Database(engine=engine, session_factory=session_factory)
    repository = PostgresControlPlaneRepository(session_factory)
    identity = PostgresIdentityDirectory(session_factory)
    storage = LocalStorage(storage_root, max_size_bytes=100 * 1024 * 1024)
    artifacts = ArtifactService(repository, storage)
    codec = IfcOpenShellModelCodec()
    revisions = IfcRevisionReader(repository, storage, InProcessIfcRevisionRuntime(codec))
    ifc_stack = build_in_process_ifc_stack(repository, storage, codec=codec)
    worker = _ControlledWorker(
        IfcOpenShellQueryWorker(
            python_executable=os.getenv(
                "CAD2BIM_TEST_IFC_QUERY_PYTHON_EXECUTABLE",
                sys.executable,
            ),
            capacity_root=capacity_root,
        )
    )
    response_loss = _CommitResponseLossRepository(repository)
    query_service = IfcQueryService(
        response_loss,
        artifacts,
        ExactPayloadResolver(repository, artifacts),
        revisions,
        worker,
        CursorCodec(CURSOR_KEY),
        TextSourcePatchAdapter(),
    )
    sessions = DirectSessionService(
        repository,
        identity,
        subject,
        bind_tenant_execution_context,
    )
    resolver = DirectScopeResolver(
        repository,
        identity,
        subject,
        bind_tenant_execution_context,
    )
    application = DataRuntimeService(repository, artifacts)
    code_discovery = CodeDiscoveryService(
        application,
        None,
        None,
        None,
        None,
        query_service,
        None,
        CursorCodec(CURSOR_KEY),
    )
    facade = DirectApplicationFacade(
        application,
        sessions,
        resolver,
        bind_tenant_execution_context,
        ifc_model_service=ifc_stack.model_service,
        ifc_project_service=IfcProjectService(
            ifc_stack.writer,
            ifc_stack.runtime,
            ifc_stack.coordinator,
        ),
        ifc_query_service=query_service,
        code_discovery_service=code_discovery,
    )
    return _Runtime(
        database=database,
        repository=repository,
        storage=storage,
        sessions=sessions,
        resolver=resolver,
        query_service=query_service,
        worker=worker,
        response_loss=response_loss,
        facade=facade,
    )


async def _open_scope(
    runtime: _Runtime,
    session_name: str,
) -> tuple[str, InvocationScope]:
    opened = await runtime.facade.open_direct_session(session_name)
    alias = opened.direct_session_alias
    scope = await runtime.resolver.resolve(alias, command_id=f"i0b304:{uuid4().hex}")
    return alias, scope


async def _open_context(
    repository: PostgresControlPlaneRepository,
    identity: PostgresIdentityDirectory,
    *,
    subject: str,
    session_name: str,
):
    sessions = DirectSessionService(
        repository,
        identity,
        subject,
        bind_tenant_execution_context,
    )
    resolver = DirectScopeResolver(
        repository,
        identity,
        subject,
        bind_tenant_execution_context,
    )
    opened = await sessions.open_direct_session(session_name)
    return await resolver.resolve(
        opened.binding.direct_session_alias,
        command_id=f"i0b304-isolation:{uuid4().hex}",
    )


async def _business_snapshot(runtime: _Runtime, scope: InvocationScope) -> _BusinessSnapshot:
    ifc_scope = _ifc_scope(scope)
    with bind_tenant_execution_context(scope.identity):
        exact = await runtime.repository.get_current(ifc_scope)
        assert exact is not None
        verified = await IfcRevisionReader(
            runtime.repository,
            runtime.storage,
            InProcessIfcRevisionRuntime(IfcOpenShellModelCodec()),
        ).read_exact(exact)
        async with runtime.database.session_factory() as session:
            await set_tenant_execution_context(session, scope.identity)
            revision_ids = tuple(
                (
                    await session.scalars(
                        select(IfcModelRevisionModel.revision_id)
                        .where(IfcModelRevisionModel.run_id == scope.run_id)
                        .order_by(IfcModelRevisionModel.revision_no)
                    )
                ).all()
            )
            operation_ids = tuple(
                (
                    await session.scalars(
                        select(CriticalOperationModel.operation_id)
                        .where(CriticalOperationModel.run_id == scope.run_id)
                        .order_by(CriticalOperationModel.operation_id)
                    )
                ).all()
            )
            package_ids = tuple(
                (
                    await session.scalars(
                        select(DataPackageModel.package_id)
                        .where(DataPackageModel.created_by_run_id == scope.run_id)
                        .order_by(DataPackageModel.package_id)
                    )
                ).all()
            )
    assert hashlib.sha256(verified.content).hexdigest() == exact.artifact.sha256
    return _BusinessSnapshot(
        current_revision_id=exact.revision_id,
        write_epoch=exact.write_epoch,
        revision_ids=revision_ids,
        artifact_id=exact.artifact.artifact_id,
        artifact_version_id=exact.artifact.version_id,
        artifact_size_bytes=exact.artifact.size_bytes,
        artifact_sha256=exact.artifact.sha256,
        artifact_bytes=verified.content,
        critical_operation_ids=operation_ids,
        data_package_ids=package_ids,
    )


async def _query_artifact_count(
    session_factory: async_sessionmaker[AsyncSession],
    scope: InvocationScope,
) -> int:
    with bind_tenant_execution_context(scope.identity):
        async with session_factory() as session:
            await set_invocation_scope(session, scope)
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(ArtifactModel)
                    .where(ArtifactModel.kind.in_(("ifc_query_source", "ifc_query_result")))
                )
                or 0
            )


async def _assert_sql_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    scope: InvocationScope,
    statement: str,
    parameters: dict[str, object],
    expected_message: str,
) -> None:
    with bind_tenant_execution_context(scope.identity):
        with pytest.raises(DBAPIError) as exc_info:
            async with session_factory() as session, session.begin():
                await set_invocation_scope(session, scope)
                await session.execute(text(statement), parameters)
    assert expected_message in str(exc_info.value)


async def _attempt_by_alias(
    runtime: _Runtime,
    scope: InvocationScope,
    attempt_alias: str,
):
    with bind_tenant_execution_context(scope.identity):
        attempt_id = await runtime.query_service.resolve_attempt_alias(
            scope,
            attempt_alias,
        )
        attempt = await runtime.repository.get_ifc_query_attempt(
            scope,
            attempt_id=attempt_id,
        )
    assert attempt is not None
    return attempt_id, attempt


@pytest.mark.asyncio
async def test_ifc_terminal_attempt_list_is_sql_filtered_keyset_and_run_bound(
    tmp_path: Path,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")

    provision_runtime_login_roles(
        TEST_DATABASE_URL,
        environ={
            "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
            "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
            "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
        },
    )
    runtime = _create_runtime(
        _runtime_url(
            TEST_DATABASE_URL,
            username="cad2bim_mcp_login",
            password=MCP_PASSWORD,
        ),
        tmp_path / "list-storage",
        tmp_path / "list-capacity",
        subject=f"i0b304-list-{uuid4().hex}",
    )
    try:
        await runtime.database.verify_ready()
        await runtime.worker.verify_ready()
        direct_alias, scope = await _open_scope(
            runtime,
            f"I0-B3.04 terminal list {uuid4().hex}",
        )
        await runtime.facade.initialize_ifc_model(direct_alias, "Terminal list project")
        succeeded = await runtime.facade.query_ifc_python(
            DirectIfcQueryFreshInput(
                direct_session_alias=direct_alias,
                mode="fresh",
                code="def query(ctx):\n    return []\n",
            ),
            request_identity=f"list-success-{uuid4().hex}",
        )
        _succeeded_id, succeeded_attempt = await _attempt_by_alias(
            runtime,
            scope,
            succeeded.attempt_alias,
        )

        with pytest.raises(DirectApplicationError) as failed:
            await runtime.facade.query_ifc_python(
                DirectIfcQueryFreshInput(
                    direct_session_alias=direct_alias,
                    mode="fresh",
                    code="def query(ctx)\n    return []\n",
                ),
                request_identity=f"list-failure-{uuid4().hex}",
            )
        failed_alias = (failed.value.diagnostic or {}).get("attempt_alias")
        assert isinstance(failed_alias, str)
        _failed_id, failed_attempt = await _attempt_by_alias(
            runtime,
            scope,
            failed_alias,
        )

        prepared_created_at = max(
            succeeded_attempt.created_at,
            failed_attempt.created_at,
        ) + timedelta(seconds=1)
        with bind_tenant_execution_context(scope.identity):
            prepared = await runtime.repository.prepare_ifc_query_attempt(
                scope,
                IfcQueryAttemptPreparation(
                    attempt_id=f"ifc_query_attempt_{uuid4().hex}",
                    run_id=scope.run_id,
                    ifc_model_id=succeeded_attempt.ifc_model_id,
                    ifc_revision_id=succeeded_attempt.ifc_revision_id,
                    source_artifact=succeeded_attempt.source_artifact,
                    request_identity=f"list-prepared-{uuid4().hex}",
                    request_digest=hashlib.sha256(b"list-prepared").hexdigest(),
                    budget=IfcQueryBudgetSummary(max_result_bytes=256 * 1024),
                    created_at=prepared_created_at,
                ),
            )
            statuses = (
                IfcQueryAttemptStatus.SUCCEEDED,
                IfcQueryAttemptStatus.FAILED,
            )
            first = await runtime.repository.list_terminal_ifc_query_attempts(
                scope,
                statuses=statuses,
                before_created_at=None,
                before_attempt_id=None,
                limit=1,
            )
            second = await runtime.repository.list_terminal_ifc_query_attempts(
                scope,
                statuses=statuses,
                before_created_at=first[-1].created_at,
                before_attempt_id=first[-1].attempt_id,
                limit=1,
            )
            failed_only = await runtime.repository.list_terminal_ifc_query_attempts(
                scope,
                statuses=(IfcQueryAttemptStatus.FAILED,),
                before_created_at=None,
                before_attempt_id=None,
                limit=2,
            )
            hidden_other_run = await runtime.repository.list_terminal_ifc_query_attempts(
                scope.model_copy(update={"run_id": f"run_{uuid4().hex}"}),
                statuses=statuses,
                before_created_at=None,
                before_attempt_id=None,
                limit=2,
            )

        assert prepared.attempt.status is IfcQueryAttemptStatus.PREPARED
        assert len(first) == len(second) == 1
        assert {first[0].attempt_id, second[0].attempt_id} == {
            succeeded_attempt.attempt_id,
            failed_attempt.attempt_id,
        }
        assert (first[0].created_at, first[0].attempt_id) > (
            second[0].created_at,
            second[0].attempt_id,
        )
        assert failed_only == (failed_attempt,)
        assert hidden_other_run == ()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_ifc_query_postgres_vertical_exact_read_recovery_and_rls(
    tmp_path: Path,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")

    provision_runtime_login_roles(
        TEST_DATABASE_URL,
        environ={
            "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
            "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
            "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
        },
    )
    mcp_url = _runtime_url(
        TEST_DATABASE_URL,
        username="cad2bim_mcp_login",
        password=MCP_PASSWORD,
    )
    subject = f"i0b304-{uuid4().hex}"
    session_name = f"I0-B3.04 vertical {uuid4().hex}"
    storage_root = tmp_path / "storage"
    runtime = _create_runtime(
        mcp_url,
        storage_root,
        tmp_path / "capacity-first",
        subject=subject,
    )
    restarted: _Runtime | None = None
    api_engine = None
    admin_engine = None
    try:
        await runtime.database.verify_ready()
        await runtime.worker.verify_ready()
        direct_alias, scope = await _open_scope(runtime, session_name)
        initialized = await runtime.facade.initialize_ifc_model(
            direct_alias,
            "Pinned query project",
        )
        assert initialized.model.project.name == "Pinned query project"

        source = ValidatedSource(filename=FIXTURE.name, media_type="application/dxf",
            content=FIXTURE.read_bytes(), source_identity=f"uploaded-source:{FIXTURE.name}", adapter_metadata={})
        with bind_tenant_execution_context(scope.identity):
            imported = await DataRuntimeService(runtime.repository,
                ArtifactService(runtime.repository, runtime.storage)).import_cad_source(scope, source)
        query_code = (
            "def query(ctx):\n"
            "    project = ctx.model().by_type('IfcProject')[0]\n"
            "    rows = ctx.table('entities').num_rows\n"
            "    return [{'ordinal': index, 'project': project.Name, 'rows': rows} "
            "for index in range(3)]\n"
        )
        query_request = DirectIfcQueryFreshInput(
            direct_session_alias=direct_alias,
            mode="fresh",
            inputs=(
                IfcQueryExactInput(
                    input_name="entities",
                    package_alias=imported.published.package.catalog_entry.alias,
                    payload_name="entities",
                ),
            ),
            code=query_code,
            page_size=1,
        )

        runtime.worker.pause_next = True
        runtime.response_loss.armed = True
        pinned_request_identity = f"pinned-{uuid4().hex}"
        pending = asyncio.create_task(
            runtime.facade.query_ifc_python(
                query_request,
                request_identity=pinned_request_identity,
            )
        )
        try:
            started = asyncio.create_task(runtime.worker.started.wait())
            done, _pending = await asyncio.wait(
                {pending, started},
                timeout=20,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if pending in done:
                await pending
            assert started in done, "IFC query did not reach the worker within 20 seconds"
            updated = await runtime.facade.update_ifc_project(
                direct_alias,
                UpdateIfcProjectCommand(patch=IfcProjectPatch(name="Advanced query project")),
                idempotency_key=f"i0b304-advance:{uuid4().hex}",
            )
            assert updated.project.name == "Advanced query project"
            before_queries = await _business_snapshot(runtime, scope)
            async with runtime.database.session_factory() as session:
                await set_invocation_scope(session, scope)
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(IfcQueryAttemptModel)
                        .where(IfcQueryAttemptModel.status == "PREPARED")
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(ArtifactModel)
                        .where(ArtifactModel.kind == "ifc_query_source")
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(ArtifactModel)
                        .where(ArtifactModel.kind == "ifc_query_result")
                    )
                    == 0
                )
        except BaseException:
            runtime.worker.release.set()
            await asyncio.gather(pending, return_exceptions=True)
            raise
        finally:
            if "started" in locals() and not started.done():
                started.cancel()
        runtime.worker.release.set()
        pinned = await pending

        pinned_page = json.loads(pinned.result_json)
        assert pinned_page == [
            {
                "ordinal": 0,
                "project": "Pinned query project",
                "rows": imported.manifest["payloads"]["entities"]["row_count"],
            }
        ]
        assert pinned.next_cursor is not None
        assert runtime.response_loss.losses == 1
        assert runtime.worker.calls == 1

        parent_id, parent = await _attempt_by_alias(runtime, scope, pinned.attempt_alias)
        assert parent.status is IfcQueryAttemptStatus.SUCCEEDED
        assert parent.result_artifact is not None

        same_task_other_run_id = f"run_{uuid4().hex}"
        with bind_tenant_execution_context(scope.identity):
            async with runtime.database.session_factory() as session, session.begin():
                await set_tenant_execution_context(session, scope.identity)
                await session.execute(
                    text(
                        """
                        INSERT INTO runs (
                            run_id, owner_id, tenant_id, task_id, module, stage_key,
                            input_payload, idempotency_key, request_fingerprint,
                            thread_id, status, execution_protocol_status, execution_mode
                        ) VALUES (
                            :run_id, :owner_id, :tenant_id, :task_id,
                            'direct_mcp', 'direct_mcp', '{}'::jsonb,
                            :idempotency_key, :request_fingerprint,
                            :thread_id, 'completed', 'not_applicable', 'direct_mcp'
                        )
                        """
                    ),
                    {
                        "run_id": same_task_other_run_id,
                        "owner_id": scope.owner_id,
                        "tenant_id": scope.identity.tenant_id,
                        "task_id": scope.identity.task_id,
                        "idempotency_key": f"i0b304-other-run:{uuid4().hex}",
                        "request_fingerprint": uuid4().hex * 2,
                        "thread_id": f"i0b304-other-thread:{uuid4().hex}",
                    },
                )
        same_task_other_run = scope.model_copy(update={"run_id": same_task_other_run_id})

        with bind_tenant_execution_context(scope.identity):
            assert (
                await runtime.repository.get_ifc_query_attempt(
                    same_task_other_run,
                    attempt_id=parent_id,
                )
                is None
            )
            with pytest.raises(ResourceNotFoundError):
                await runtime.repository.commit_ifc_query_attempt_terminal(
                    same_task_other_run,
                    attempt_id=parent_id,
                    terminal=IfcQueryAttemptTerminal(
                        status=IfcQueryAttemptStatus.SUCCEEDED,
                        result_artifact=parent.result_artifact,
                        finished_at=parent.finished_at or datetime.now(UTC),
                    ),
                )

        artifact_service = ArtifactService(runtime.repository, runtime.storage)
        with bind_tenant_execution_context(scope.identity):
            other_run_source = await artifact_service.upload_bytes(
                owner_id=scope.owner_id,
                task_id=scope.identity.task_id or "",
                filename="other_run_ifc_query.py",
                content_type="text/x-python",
                content=b"def query(ctx):\n    return []\n",
                idempotency_key=f"ifc-query-source-other-run:{uuid4().hex}",
                kind="ifc_query_source",
                run_id=same_task_other_run_id,
                version_metadata={"source_sha256": hashlib.sha256(b"other").hexdigest()},
            )
            with pytest.raises(ResourceNotFoundError):
                await runtime.repository.prepare_ifc_query_attempt(
                    scope,
                    IfcQueryAttemptPreparation(
                        attempt_id=f"ifc_query_attempt_{uuid4().hex}",
                        run_id=scope.run_id,
                        ifc_model_id=parent.ifc_model_id,
                        ifc_revision_id=parent.ifc_revision_id,
                        source_artifact=IfcQueryArtifactRef(
                            artifact_id=other_run_source.artifact_id,
                            version_id=other_run_source.version_id,
                            sha256=other_run_source.sha256,
                        ),
                        request_identity=f"cross-run-source-{uuid4().hex}",
                        request_digest=hashlib.sha256(b"cross-run-source").hexdigest(),
                        budget=IfcQueryBudgetSummary(max_result_bytes=256 * 1024),
                        created_at=datetime.now(UTC),
                    ),
                )

            other_run_import = await DataRuntimeService(
                runtime.repository,
                artifact_service,
            ).import_cad_source(
                same_task_other_run,
                ValidatedSource(
                    filename=source.filename,
                    media_type=source.media_type,
                    content=source.content,
                    source_identity=source.source_identity,
                    adapter_metadata=source.adapter_metadata,
                ),
            )
            other_payload = next(
                member
                for member in other_run_import.published.package.members
                if member.member_name.startswith("payloads/entities/")
            )
            with pytest.raises(ResourceNotFoundError):
                await runtime.repository.prepare_ifc_query_attempt(
                    scope,
                    IfcQueryAttemptPreparation(
                        attempt_id=f"ifc_query_attempt_{uuid4().hex}",
                        run_id=scope.run_id,
                        ifc_model_id=parent.ifc_model_id,
                        ifc_revision_id=parent.ifc_revision_id,
                        source_artifact=parent.source_artifact,
                        inputs=(
                            IfcQueryExactInputRef(
                                ordinal=1,
                                input_name="other_run_entities",
                                package_id=other_run_import.published.package.package.package_id,
                                payload_member_id=other_payload.member_id,
                            ),
                        ),
                        request_identity=f"cross-run-package-{uuid4().hex}",
                        request_digest=hashlib.sha256(b"cross-run-package").hexdigest(),
                        budget=IfcQueryBudgetSummary(max_result_bytes=256 * 1024),
                        created_at=datetime.now(UTC),
                    ),
                )

        repaired = await runtime.facade.query_ifc_python(
            DirectIfcQueryRepairInput(
                direct_session_alias=direct_alias,
                mode="repair",
                parent_attempt_alias=pinned.attempt_alias,

                edits=(IfcQuerySourceEdit(old_text="range(3)", new_text="range(2)"),),
                page_size=10,
            ),
            request_identity=f"repair-{uuid4().hex}",
        )
        repaired_page = json.loads(repaired.result_json)
        assert [item["project"] for item in repaired_page] == [
            "Pinned query project",
            "Pinned query project",
        ]
        _child_id, child = await _attempt_by_alias(runtime, scope, repaired.attempt_alias)
        assert child.repair_parent_attempt_id == parent.attempt_id
        assert child.ifc_model_id == parent.ifc_model_id
        assert child.ifc_revision_id == parent.ifc_revision_id
        assert child.inputs == parent.inputs
        assert len(child.inputs) == 1

        latest = await runtime.facade.query_ifc_python(
            DirectIfcQueryFreshInput(
                direct_session_alias=direct_alias,
                mode="fresh",
                code=(
                    "def query(ctx):\n"
                    "    return {'project': ctx.model().by_type('IfcProject')[0].Name}\n"
                ),
                page_size=10,
            ),
            request_identity=f"latest-{uuid4().hex}",
        )
        assert json.loads(latest.result_json) == [{"project": "Advanced query project"}]

        local_code = (
            "def query(ctx):\n"
            "    counts = {}\n"
            "    names = ['discard']\n"
            "    for project in ctx.model().by_type('IfcProject'):\n"
            "        counts['IfcProject'] = counts.get('IfcProject', 0) + 1\n"
            "        names.append(project.Name)\n"
            "    del names[0]\n"
            "    return {'counts': counts, 'names': names}\n"
        )
        local_result = await runtime.facade.query_ifc_python(
            DirectIfcQueryFreshInput(
                direct_session_alias=direct_alias,
                mode="fresh",
                code=local_code,
            ),
            request_identity=f"local-containers-{uuid4().hex}",
        )
        assert json.loads(local_result.result_json) == [
            {
                "counts": {"IfcProject": 1},
                "names": ["Advanced query project"],
            }
        ]

        failures = (
            (
                "IFC_QUERY_PYTHON_INVALID",
                "def query(ctx)\n    return {}\n",
            ),
            (
                "IFC_QUERY_EXECUTION_FAILED",
                "def query(ctx):\n    return missing_name\n",
            ),
            (
                "IFC_QUERY_MUTATION_FORBIDDEN",
                "def query(ctx):\n    return ctx.model().create_entity('IfcWall')\n",
            ),
            (
                "IFC_QUERY_MUTATION_DETECTED",
                "def query(ctx):\n"
                "    project = ctx.model().by_type('IfcProject')[0]\n"
                "    original = project[2]\n"
                "    project[2] = 'temporary'\n"
                "    project[2] = original\n"
                "    return {}\n",
            ),
            (
                "IFC_QUERY_EXECUTION_FAILED",
                "def query(ctx):\n    del ctx.model().by_type('IfcProject')[0][2]\n    return {}\n",
            ),
        )
        for expected_code, failed_code in failures:
            with pytest.raises(DirectApplicationError) as failed:
                await runtime.facade.query_ifc_python(
                    DirectIfcQueryFreshInput(
                        direct_session_alias=direct_alias,
                        mode="fresh",
                        code=failed_code,
                    ),
                    request_identity=f"failed-{expected_code}-{uuid4().hex}",
                )
            assert failed.value.code == expected_code
            assert failed.value.diagnostic is not None
            attempt_alias = failed.value.diagnostic.get("attempt_alias")
            assert isinstance(attempt_alias, str)
            code_page = await runtime.facade.list_code(
                CodeListFirstPageInput(
                    direct_session_alias=direct_alias,
                    mode="first",
                    query={
                        "code_kind": "ifc_query_attempt",
                        "statuses": ["failed"],
                        "page_size": 50,
                    },
                )
            )
            code_card = next(item for item in code_page.codes if item.owner_alias == attempt_alias)
            source_read = await runtime.facade.read_code(
                SingleTextCodeReadInput(
                    direct_session_alias=direct_alias,
                    source_form="single_text",
                    code_locator=code_card.code_locator,
                    view={"view_mode": "full"},
                )
            )
            assert source_read.text == failed_code
            _failed_id, failed_attempt = await _attempt_by_alias(
                runtime,
                scope,
                attempt_alias,
            )
            assert failed_attempt.status is IfcQueryAttemptStatus.FAILED
            assert failed_attempt.diagnostics[0].code == expected_code
            diagnostic_text = failed_attempt.diagnostics[0].model_dump_json()
            assert str(tmp_path) not in diagnostic_text
            assert "/Users/" not in diagnostic_text
            assert "Traceback" not in diagnostic_text
            if expected_code == "IFC_QUERY_MUTATION_DETECTED":
                repaired_local = await runtime.facade.query_ifc_python(
                    DirectIfcQueryRepairInput(
                        direct_session_alias=direct_alias,
                        mode="repair",
                        parent_attempt_alias=attempt_alias,

                        edits=(IfcQuerySourceEdit(old_text=failed_code, new_text=local_code),),
                    ),
                    request_identity=f"repair-index-mutation-{uuid4().hex}",
                )
                assert (
                    repaired_local.source_sha256 == hashlib.sha256(local_code.encode()).hexdigest()
                )
                assert json.loads(repaired_local.result_json) == json.loads(
                    local_result.result_json
                )
                _, repaired_attempt = await _attempt_by_alias(
                    runtime, scope, repaired_local.attempt_alias
                )
                assert repaired_attempt.repair_parent_attempt_id == failed_attempt.attempt_id
                assert repaired_attempt.ifc_revision_id == failed_attempt.ifc_revision_id

        after_queries = await _business_snapshot(runtime, scope)
        assert after_queries == before_queries

        same_tenant_other = await _open_scope(
            runtime,
            f"I0-B3.04 same tenant other Task {uuid4().hex}",
        )
        other_identity = PostgresIdentityDirectory(runtime.database.session_factory)
        other_tenant = await _open_context(
            runtime.repository,
            other_identity,
            subject=f"i0b304-other-{uuid4().hex}",
            session_name="I0-B3.04 other tenant",
        )
        assert same_tenant_other[1].identity.tenant_id == scope.identity.tenant_id
        assert same_tenant_other[1].identity.task_id != scope.identity.task_id
        assert other_tenant.identity.tenant_id != scope.identity.tenant_id
        api_engine = create_async_engine(
            _runtime_url(
                TEST_DATABASE_URL,
                username="cad2bim_api_login",
                password=API_PASSWORD,
            ),
            pool_pre_ping=True,
        )
        api_sessions = async_sessionmaker(
            api_engine,
            class_=TenantAwareAsyncSession,
            expire_on_commit=False,
        )
        api_repository = PostgresControlPlaneRepository(api_sessions)
        with bind_tenant_execution_context(scope.identity):
            visible_parent = await api_repository.get_ifc_query_attempt(
                scope,
                attempt_id=parent_id,
            )
        assert visible_parent == parent
        with bind_tenant_execution_context(scope.identity):
            same_task_other_run_hidden = await api_repository.get_ifc_query_attempt(
                same_task_other_run,
                attempt_id=parent_id,
            )
        assert same_task_other_run_hidden is None
        with bind_tenant_execution_context(same_tenant_other[1].identity):
            same_tenant_hidden = await api_repository.get_ifc_query_attempt(
                same_tenant_other[1],
                attempt_id=parent_id,
            )
        assert same_tenant_hidden is None
        with bind_tenant_execution_context(other_tenant.identity):
            other_tenant_hidden = await api_repository.get_ifc_query_attempt(
                other_tenant,
                attempt_id=parent_id,
            )
        assert other_tenant_hidden is None
        # Nine original artifacts plus local success (2), two failed sources (2),
        # and the successful exact repair (2).
        assert await _query_artifact_count(api_sessions, scope) == 15
        assert await _query_artifact_count(api_sessions, same_task_other_run) == 1
        assert await _query_artifact_count(api_sessions, same_tenant_other[1]) == 0
        assert await _query_artifact_count(api_sessions, other_tenant) == 0

        assert parent.result_artifact is not None
        await _assert_sql_rejected(
            api_sessions,
            scope,
            """
            INSERT INTO artifacts (
                artifact_id, owner_id, tenant_id, task_id, run_id, kind,
                filename, content_type, status, current_version_id,
                idempotency_key, request_fingerprint
            ) VALUES (
                :artifact_id, :owner_id, :tenant_id, :task_id, :run_id,
                'ifc_query_source', 'api.py', 'text/x-python', 'available', NULL,
                :idempotency_key, :request_fingerprint
            )
            """,
            {
                "artifact_id": f"artifact_{uuid4().hex}",
                "owner_id": scope.owner_id,
                "tenant_id": scope.identity.tenant_id,
                "task_id": scope.identity.task_id,
                "run_id": scope.run_id,
                "idempotency_key": f"api-query-artifact:{uuid4().hex}",
                "request_fingerprint": uuid4().hex * 2,
            },
            "API runtime cannot publish IFC query Artifacts",
        )
        await _assert_sql_rejected(
            api_sessions,
            scope,
            "UPDATE artifacts SET filename = 'changed.py' WHERE artifact_id = :artifact_id",
            {"artifact_id": parent.source_artifact.artifact_id},
            "API runtime cannot update IFC query Artifacts",
        )
        await _assert_sql_rejected(
            api_sessions,
            scope,
            """
            INSERT INTO artifact_versions (
                version_id, owner_id, tenant_id, task_id, artifact_id,
                version_no, storage_key, filename, content_type,
                size_bytes, sha256, metadata_json, lineage_json
            ) VALUES (
                :version_id, :owner_id, :tenant_id, :task_id, :artifact_id,
                2, 'forbidden/api', 'api.py', 'text/x-python', 0,
                :sha256, '{}'::jsonb, '{}'::jsonb
            )
            """,
            {
                "version_id": f"artifact_version_{uuid4().hex}",
                "owner_id": scope.owner_id,
                "tenant_id": scope.identity.tenant_id,
                "task_id": scope.identity.task_id,
                "artifact_id": parent.source_artifact.artifact_id,
                "sha256": hashlib.sha256(b"").hexdigest(),
            },
            "API runtime cannot publish IFC query ArtifactVersions",
        )
        await _assert_sql_rejected(
            runtime.database.session_factory,
            scope,
            "UPDATE artifacts SET filename = filename WHERE artifact_id = :artifact_id",
            {"artifact_id": parent.result_artifact.artifact_id},
            "IFC query Artifact publication is append-only",
        )
        await _assert_sql_rejected(
            runtime.database.session_factory,
            scope,
            "UPDATE artifact_versions SET filename = filename WHERE version_id = :version_id",
            {"version_id": parent.result_artifact.version_id},
            "permission denied for table artifact_versions",
        )
        await _assert_sql_rejected(
            runtime.database.session_factory,
            scope,
            "DELETE FROM artifacts WHERE artifact_id = :artifact_id",
            {"artifact_id": parent.result_artifact.artifact_id},
            "permission denied for table artifacts",
        )

        admin_engine = create_async_engine(TEST_DATABASE_URL)
        async with admin_engine.connect() as connection:
            policies = set(
                (
                    await connection.scalars(
                        text(
                            "SELECT policyname FROM pg_policies "
                            "WHERE schemaname = current_schema() "
                            "AND tablename IN ('ifc_query_attempts', 'ifc_query_inputs')"
                        )
                    )
                ).all()
            )
        assert {
            "ifc_query_attempts_runtime_select",
            "ifc_query_attempts_runtime_insert",
            "ifc_query_attempts_runtime_terminal",
            "ifc_query_inputs_runtime_select",
            "ifc_query_inputs_runtime_insert",
        }.issubset(policies)

        first_cursor = pinned.next_cursor
        first_alias = pinned.attempt_alias
        first_source = query_code
        await runtime.close()
        restarted = _create_runtime(
            mcp_url,
            storage_root,
            tmp_path / "capacity-restarted",
            subject=subject,
        )
        await restarted.database.verify_ready()
        reopened_alias, reopened_scope = await _open_scope(restarted, session_name)
        assert reopened_alias == direct_alias
        restarted_codes = await restarted.facade.list_code(
            CodeListFirstPageInput(
                direct_session_alias=reopened_alias,
                mode="first",
                query={
                    "code_kind": "ifc_query_attempt",
                    "statuses": ["succeeded"],
                    "page_size": 50,
                },
            )
        )
        restarted_card = next(
            item for item in restarted_codes.codes if item.owner_alias == first_alias
        )
        restarted_source = await restarted.facade.read_code(
            SingleTextCodeReadInput(
                direct_session_alias=reopened_alias,
                source_form="single_text",
                code_locator=restarted_card.code_locator,
                view={"view_mode": "full"},
            )
        )
        assert restarted_source.text == first_source
        replayed = await restarted.facade.query_ifc_python(
            query_request,
            request_identity=pinned_request_identity,
        )
        assert replayed.attempt_alias == first_alias
        assert replayed.result_json == pinned.result_json
        assert restarted.worker.calls == 0
        continuation = await restarted.facade.query_ifc_python(
            DirectIfcQueryCursorInput(
                direct_session_alias=reopened_alias,
                mode="cursor",
                cursor=first_cursor,
            ),
            request_identity=f"ignored-cursor-{uuid4().hex}",
        )
        assert json.loads(continuation.result_json) == [
            {
                "ordinal": 1,
                "project": "Pinned query project",
                "rows": imported.manifest["payloads"]["entities"]["row_count"],
            }
        ]
        restarted_parent_id, restarted_parent = await _attempt_by_alias(
            restarted,
            reopened_scope,
            first_alias,
        )
        assert restarted_parent_id == parent_id
        assert restarted_parent.source_artifact == parent.source_artifact
        assert restarted_parent.result_artifact == parent.result_artifact
        assert restarted.worker.calls == 0
        assert await _business_snapshot(restarted, reopened_scope) == before_queries
    finally:
        if restarted is not None:
            await restarted.close()
        else:
            await runtime.close()
        if api_engine is not None:
            await api_engine.dispose()
        if admin_engine is not None:
            await admin_engine.dispose()
