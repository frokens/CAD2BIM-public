from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
import psutil
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from CAD2BIM_backend.application.data_runtime_service import (
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.application.ifc_model_service import IfcOperationReconciler
from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationErrorPhase,
    IFC_PROJECT_UPDATE_INTENT_SUMMARY,
    IfcProjectUpdateOperationDetail,
)
from CAD2BIM_backend.domain.ifc_model import (
    ExactIfcArtifactVersion,
    IFC_REVISION_CONTENT_TYPE,
    IFC_REVISION_FILENAME,
    IfcModelConflictError,
    IfcExecutionClaim,
    IfcModelFrozenError,
    IfcModelIntegrityError,
    IfcRunIdentity,
    InitializeIfcModelCommand,
    PreparedIfcInitialize,
    PreparedIfcRevision,
    VerifiedIfcPublication,
)
from CAD2BIM_backend.domain.ifc_project import (
    IfcProjectPatch,
    IfcProjectSummary,
    UpdateIfcProjectCommand,
)
from CAD2BIM_backend.domain.identity import TenantActorKind
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.local_process_identity import (
    local_executor_stopped,
    local_guard_process_group_stopped,
)
from CAD2BIM_backend.infrastructure.postgres.database import Database
from CAD2BIM_backend.infrastructure.postgres.identity_directory import (
    PostgresIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.postgres.repository import (
    PostgresControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.postgres import repository_ifc_model
from CAD2BIM_backend.infrastructure.postgres.runtime_roles import (
    provision_runtime_login_roles,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantAwareAsyncSession,
    bind_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from CAD2BIM_backend.ports.storage import StorageConflictError
from CAD2BIM_backend.ports.ifc_model import CommittedIfcRevision, IfcSelectedCandidate
from CAD2BIM_backend.ports.storage import StorageKey
from CAD2BIM_backend.ports.ifc_revision_runtime import (
    GeneratedIfcRevision, InspectExactIfcRevisionRequest,
)
from tests.process_support.ifc_revision_runtime import (
    BytesIfcCandidate, build_in_process_ifc_stack,
)


pytestmark = [pytest.mark.integration, pytest.mark.network]

TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL")
API_PASSWORD = os.getenv("CAD2BIM_API_POSTGRES_PASSWORD", "phase1-api-password")
RUNNER_PASSWORD = os.getenv("CAD2BIM_RUNNER_POSTGRES_PASSWORD", "phase1-runner-password")
MCP_PASSWORD = os.getenv("CAD2BIM_MCP_POSTGRES_PASSWORD", "phase1-mcp-password")
PRIMARY_PROJECT_NAME = "I0-A Durable Primary"
CRASH_PROJECT_NAME = "I0-A Storage Before Database"
LOSS_PROJECT_NAME = "I0-A Committed Response Loss"
CORRUPT_PROJECT_NAME = "I0-A Committed Corruption Freeze"


@dataclass(slots=True)
class _Runtime:
    database: Database
    repository: PostgresControlPlaneRepository
    identity_directory: PostgresIdentityDirectory
    storage: LocalStorage

    async def close(self) -> None:
        await self.database.dispose()


class _InitializeCommitFault:
    """Inject one failure either side of the real initialize commit."""

    def __init__(
        self,
        repository,
        *,
        persist_first: bool,
        failures_before_success: int = 1,
    ) -> None:
        self._repository = repository
        self._persist_first = persist_first
        self._failures_before_success = failures_before_success
        self.commit_calls = 0
        self.prepared: PreparedIfcInitialize | None = None

    def __getattr__(self, name: str):
        return getattr(self._repository, name)

    async def prepare_initialize(self, *args, **kwargs) -> PreparedIfcInitialize:
        self.prepared = await self._repository.prepare_initialize(*args, **kwargs)
        return self.prepared

    async def commit_initialize(self, *args, **kwargs):
        self.commit_calls += 1
        if self.commit_calls <= self._failures_before_success:
            if self._persist_first:
                await self._repository.commit_initialize(*args, **kwargs)
            raise RuntimeError("injected initialize response loss")
        return await self._repository.commit_initialize(*args, **kwargs)


class _ReadFailureStorage:
    """Preserve metadata access while simulating loss of committed IFC bytes."""

    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage
        self.read_calls = 0

    def __getattr__(self, name: str):
        return getattr(self._storage, name)

    async def read(self, key: StorageKey) -> bytes:
        del key
        self.read_calls += 1
        raise FileNotFoundError("injected committed IFC byte loss")

    async def iter_chunks(self, key: StorageKey, **kwargs):
        yield await self.read(key)


def _runtime_database_url(value: str) -> str:
    return (
        make_url(value)
        .set(
            drivername="postgresql+psycopg",
            username="cad2bim_mcp_login",
            password=MCP_PASSWORD,
        )
        .render_as_string(hide_password=False)
    )


def _create_runtime(database_url: str, storage_root: Path) -> _Runtime:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(
        engine,
        class_=TenantAwareAsyncSession,
        expire_on_commit=False,
    )
    database = Database(engine=engine, session_factory=sessions)
    return _Runtime(
        database=database,
        repository=PostgresControlPlaneRepository(sessions),
        identity_directory=PostgresIdentityDirectory(sessions),
        storage=LocalStorage(storage_root),
    )


async def _open_direct_scope(
    runtime: _Runtime,
    *,
    configured_subject: str,
    session_name: str,
):
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
        command_id=f"i0a:{uuid4().hex}",
    )
    assert invocation.identity.task_id is not None
    scope = IfcRunIdentity(
        owner_id=invocation.owner_id,
        tenant_id=invocation.identity.tenant_id,
        task_id=invocation.identity.task_id,
        run_id=invocation.run_id,
    )
    return invocation.identity, scope


def _storage_key(scope: IfcRunIdentity, artifact_id: str) -> StorageKey:
    return StorageKey(
        owner_id=scope.owner_id,
        task_id=scope.task_id,
        object_id=artifact_id,
        version=1,
    )


async def _one_chunk(content: bytes) -> AsyncIterator[bytes]:
    yield content


async def _publish_project_name_candidate(
    *,
    storage: LocalStorage,
    codec: IfcOpenShellModelCodec,
    scope: IfcRunIdentity,
    prepared: PreparedIfcRevision,
    project_name: str,
) -> VerifiedIfcPublication:
    before_key = _storage_key(scope, prepared.before.artifact.artifact_id)
    private = codec.private_copy(await storage.read(before_key))
    projects = tuple(private.model.by_type("IfcProject"))
    assert len(projects) == 1
    projects[0].Name = project_name
    content = private.to_bytes()

    key = _storage_key(scope, prepared.artifact_id)
    written = await storage.write(
        key,
        filename=IFC_REVISION_FILENAME,
        content_type=IFC_REVISION_CONTENT_TYPE,
        chunks=_one_chunk(content),
    )
    assert await storage.stat(key) == written
    reread = await storage.read(key)
    assert reread == content
    inspection = codec.inspect(reread)
    assert inspection.project_name == project_name
    assert inspection.project_global_id == prepared.before.project_global_id
    assert inspection.size_bytes == written.size_bytes
    assert inspection.sha256 == written.sha256
    return VerifiedIfcPublication(
        scope=scope,
        artifact=ExactIfcArtifactVersion(
            artifact_id=prepared.artifact_id,
            version_id=prepared.artifact_version_id,
            filename=written.filename,
            content_type=written.content_type,
            size_bytes=written.size_bytes,
            sha256=written.sha256,
        ),
        inspection=inspection,
        storage_created_at=written.created_at,
    )


async def _select_direct_project_publication(
    repository: PostgresControlPlaneRepository,
    scope: IfcRunIdentity,
    prepared: PreparedIfcRevision,
    claim: IfcExecutionClaim,
    publication: VerifiedIfcPublication,
) -> None:
    """Supply the new O fence for tests that call Repository.commit directly.

    These old CAS/cancellation tests synthesize formal storage publication;
    Writer staging and recovery are verified in separate P3 owner tests.
    """
    await repository.select_candidate(
        scope, prepared, claim,
        IfcSelectedCandidate(
            origin_generation=claim.generation, upload_id=uuid4().hex,
            size_bytes=publication.artifact.size_bytes,
            sha256=publication.artifact.sha256,
            inspection=publication.inspection,
            project_result=IfcProjectSummary(
                global_id=publication.inspection.project_global_id,
                name=publication.inspection.project_name,
            ),
        ),
    )


async def _private_project_candidate(
    storage: LocalStorage,
    codec: IfcOpenShellModelCodec,
    scope: IfcRunIdentity,
    prepared: PreparedIfcRevision,
    name: str,
) -> tuple[GeneratedIfcRevision, IfcProjectSummary]:
    base = await storage.read(_storage_key(scope, prepared.before.artifact.artifact_id))
    private = codec.private_copy(base)
    projects = tuple(private.model.by_type("IfcProject"))
    assert len(projects) == 1
    projects[0].Name = name
    content = private.to_bytes()
    inspection = codec.inspect(content)
    return (
        GeneratedIfcRevision(
            candidate=BytesIfcCandidate(content, sha256=inspection.sha256),
            inspection=inspection,
        ),
        IfcProjectSummary(global_id=inspection.project_global_id, name=inspection.project_name),
    )


async def _run_state(runtime: _Runtime, context, run_id: str) -> dict[str, int]:
    with bind_tenant_execution_context(context):
        async with runtime.database.session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT "
                            "(SELECT count(*) FROM ifc_models WHERE run_id = :run_id) "
                            "AS model_count, "
                            "(SELECT count(*) FROM ifc_model_revisions WHERE run_id = :run_id) "
                            "AS revision_count, "
                            "(SELECT count(*) FROM artifacts "
                            " WHERE run_id = :run_id AND kind = 'ifc_revision') AS artifact_count, "
                            "(SELECT count(*) FROM artifact_versions av "
                            " JOIN artifacts a ON a.artifact_id = av.artifact_id "
                            " WHERE a.run_id = :run_id AND a.kind = 'ifc_revision') "
                            "AS artifact_version_count, "
                            "(SELECT count(*) FROM critical_operations "
                            " WHERE run_id = :run_id AND operation_kind = 'ifc_model_write') "
                            "AS operation_count, "
                            "(SELECT count(*) FROM data_catalog_entries "
                            " WHERE run_id = :run_id AND resource_kind = 'operation') "
                            "AS catalog_count"
                        ),
                        {"run_id": run_id},
                    )
                )
                .mappings()
                .one()
            )
    return {key: int(value) for key, value in row.items()}


async def _fresh_process_readback(
    *,
    runtime_url: str,
    storage_root: Path,
    context,
    scope: IfcRunIdentity,
) -> dict[str, object]:
    script = """
import asyncio
import json
import os
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity
from CAD2BIM_backend.infrastructure.postgres.repository import PostgresControlPlaneRepository
from CAD2BIM_backend.infrastructure.postgres.tenant_context import TenantAwareAsyncSession, bind_tenant_execution_context
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from tests.process_support.ifc_revision_runtime import build_in_process_ifc_stack

async def main():
    engine = create_async_engine(os.environ["I0A_DATABASE_URL"], pool_pre_ping=True)
    sessions = async_sessionmaker(
        engine,
        class_=TenantAwareAsyncSession,
        expire_on_commit=False,
    )
    stack = build_in_process_ifc_stack(
        PostgresControlPlaneRepository(sessions),
        LocalStorage(os.environ["I0A_STORAGE_ROOT"]),
    )
    service = stack.model_service
    context = TenantExecutionContext.model_validate_json(os.environ["I0A_CONTEXT"])
    scope = IfcRunIdentity.model_validate_json(os.environ["I0A_SCOPE"])
    try:
        with bind_tenant_execution_context(context):
            exact = await service.get_current_exact(scope)
            info = await service.info(scope)
        print(json.dumps({
            "revision_no": exact.revision_no if exact is not None else None,
            "write_epoch": exact.write_epoch if exact is not None else None,
            "project_name": info.project.name if info.project is not None else None,
        }, sort_keys=True))
    finally:
        await engine.dispose()

asyncio.run(main())
"""
    environment = dict(os.environ)
    environment.update(
        {
            "I0A_DATABASE_URL": runtime_url,
            "I0A_STORAGE_ROOT": str(storage_root),
            "I0A_CONTEXT": context.model_dump_json(),
            "I0A_SCOPE": scope.model_dump_json(),
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    return json.loads(stdout.decode("utf-8"))


@pytest.mark.asyncio
async def test_i0a_durable_ifc_kernel_survives_failures_concurrency_and_restart(
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
    runtime_url = _runtime_database_url(TEST_DATABASE_URL)
    storage_root = tmp_path / "storage"
    runtime = _create_runtime(runtime_url, storage_root)
    restarted: _Runtime | None = None
    try:
        await runtime.database.verify_ready()
        subject = f"i0a-primary-{uuid4().hex}"
        context, scope = await _open_direct_scope(
            runtime,
            configured_subject=subject,
            session_name="I0-A durable primary",
        )
        crash_context, crash_scope = await _open_direct_scope(
            runtime,
            configured_subject=subject,
            session_name="I0-A storage before database",
        )
        loss_context, loss_scope = await _open_direct_scope(
            runtime,
            configured_subject=subject,
            session_name="I0-A committed response loss",
        )
        corrupt_context, corrupt_scope = await _open_direct_scope(
            runtime,
            configured_subject=subject,
            session_name="I0-A committed corruption freeze",
        )

        codec = IfcOpenShellModelCodec()
        stack = build_in_process_ifc_stack(
            runtime.repository,
            runtime.storage,
            codec=codec,
        )
        service = stack.model_service
        with bind_tenant_execution_context(context):
            initialized = await service.initialize(
                scope,
                InitializeIfcModelCommand(project_name=PRIMARY_PROJECT_NAME),
            )
            current_v1 = await service.get_current_exact(scope)
        assert initialized.operation_alias == "op_001"
        assert initialized.model.project.name == PRIMARY_PROJECT_NAME
        assert initialized.model.geometry_basis_configured is False
        assert current_v1 is not None
        assert current_v1.revision_no == current_v1.write_epoch == 1
        assert current_v1.artifact.version_no == 1

        with bind_tenant_execution_context(context):
            async with runtime.database.session_factory() as session:
                durable_row = (
                    (
                        await session.execute(
                            text(
                                "SELECT im.ifc_model_id, im.current_revision_id, im.write_epoch, "
                                "ir.revision_id, ir.revision_no, ir.artifact_id, "
                                "ir.artifact_version_id, a.current_version_id, "
                                "av.version_no, av.size_bytes, av.sha256, "
                                "co.status AS operation_status, co.finished_at, "
                                "dce.alias AS operation_alias "
                                "FROM ifc_models im "
                                "JOIN ifc_model_revisions ir "
                                "  ON ir.run_id = im.run_id "
                                " AND ir.revision_id = im.current_revision_id "
                                "JOIN artifacts a ON a.artifact_id = ir.artifact_id "
                                "JOIN artifact_versions av "
                                "  ON av.artifact_id = ir.artifact_id "
                                " AND av.version_id = ir.artifact_version_id "
                                "JOIN critical_operations co "
                                "  ON co.operation_id = ir.operation_id "
                                "JOIN data_catalog_entries dce "
                                "  ON dce.operation_id = co.operation_id "
                                "WHERE im.run_id = :run_id"
                            ),
                            {"run_id": scope.run_id},
                        )
                    )
                    .mappings()
                    .one()
                )
        assert durable_row["ifc_model_id"] == current_v1.model_id
        assert durable_row["current_revision_id"] == durable_row["revision_id"]
        assert durable_row["revision_no"] == durable_row["write_epoch"] == 1
        assert durable_row["artifact_id"] == current_v1.artifact.artifact_id
        assert durable_row["artifact_version_id"] == current_v1.artifact.version_id
        assert durable_row["current_version_id"] == current_v1.artifact.version_id
        assert durable_row["version_no"] == 1
        assert durable_row["size_bytes"] == current_v1.artifact.size_bytes
        assert durable_row["sha256"] == current_v1.artifact.sha256
        assert durable_row["operation_status"] == "succeeded"
        assert durable_row["finished_at"] is not None
        assert durable_row["operation_alias"] == initialized.operation_alias
        assert await _run_state(runtime, context, scope.run_id) == {
            "model_count": 1,
            "revision_count": 1,
            "artifact_count": 1,
            "artifact_version_count": 1,
            "operation_count": 1,
            "catalog_count": 1,
        }

        with bind_tenant_execution_context(context):
            replayed = await service.initialize(
                scope,
                InitializeIfcModelCommand(project_name=PRIMARY_PROJECT_NAME),
            )
            replayed_current = await service.get_current_exact(scope)
        assert replayed == initialized
        assert replayed_current == current_v1
        assert await _run_state(runtime, context, scope.run_id) == {
            "model_count": 1,
            "revision_count": 1,
            "artifact_count": 1,
            "artifact_version_count": 1,
            "operation_count": 1,
            "catalog_count": 1,
        }

        crash_fault = _InitializeCommitFault(
            runtime.repository,
            persist_first=False,
            failures_before_success=3,
        )
        crashing_stack = build_in_process_ifc_stack(
            crash_fault,
            runtime.storage,
            codec=codec,
        )
        crashing_service = crashing_stack.model_service
        with bind_tenant_execution_context(crash_context):
            with pytest.raises(IfcModelIntegrityError, match="technical retries"):
                await crashing_service.initialize(
                    crash_scope,
                    InitializeIfcModelCommand(project_name=CRASH_PROJECT_NAME),
                )
        assert crash_fault.commit_calls == 3
        assert crash_fault.prepared is not None
        assert await runtime.storage.exists(
            _storage_key(crash_scope, crash_fault.prepared.artifact_id)
        )
        assert await _run_state(runtime, crash_context, crash_scope.run_id) == {
            "model_count": 0,
            "revision_count": 0,
            "artifact_count": 0,
            "artifact_version_count": 0,
            "operation_count": 1,
            "catalog_count": 0,
        }
        rebuilt_repository = PostgresControlPlaneRepository(runtime.database.session_factory)
        rebuilt_codec = IfcOpenShellModelCodec()
        rebuilt_stack = build_in_process_ifc_stack(
            rebuilt_repository,
            LocalStorage(storage_root),
            codec=rebuilt_codec,
        )
        rebuilt_service = rebuilt_stack.model_service
        with bind_tenant_execution_context(crash_context):
            recovered = await IfcOperationReconciler(
                rebuilt_service,
                rebuilt_repository,
                rebuilt_codec,
            ).reconcile_initialize(
                crash_scope,
                InitializeIfcModelCommand(project_name=CRASH_PROJECT_NAME),
            )
        assert recovered.operation_alias == "op_001"
        assert await _run_state(runtime, crash_context, crash_scope.run_id) == {
            "model_count": 1,
            "revision_count": 1,
            "artifact_count": 1,
            "artifact_version_count": 1,
            "operation_count": 1,
            "catalog_count": 1,
        }

        response_loss = _InitializeCommitFault(runtime.repository, persist_first=True)
        response_loss_stack = build_in_process_ifc_stack(
            response_loss,
            runtime.storage,
            codec=codec,
        )
        response_loss_service = response_loss_stack.model_service
        with bind_tenant_execution_context(loss_context):
            reconciled = await response_loss_service.initialize(
                loss_scope,
                InitializeIfcModelCommand(project_name=LOSS_PROJECT_NAME),
            )
        assert response_loss.commit_calls == 2
        assert reconciled.operation_alias == "op_001"
        assert await _run_state(runtime, loss_context, loss_scope.run_id) == {
            "model_count": 1,
            "revision_count": 1,
            "artifact_count": 1,
            "artifact_version_count": 1,
            "operation_count": 1,
            "catalog_count": 1,
        }

        with bind_tenant_execution_context(corrupt_context):
            await service.initialize(
                corrupt_scope,
                InitializeIfcModelCommand(project_name=CORRUPT_PROJECT_NAME),
            )
            read_failure_storage = _ReadFailureStorage(runtime.storage)
            corrupting_stack = build_in_process_ifc_stack(
                runtime.repository,
                read_failure_storage,
                codec=codec,
            )
            corrupting_service = corrupting_stack.model_service
            with pytest.raises(
                IfcModelIntegrityError,
                match="technical retries",
            ):
                await corrupting_service.info(corrupt_scope)
            assert read_failure_storage.read_calls == 3
            with pytest.raises(IfcModelFrozenError):
                await service.info(corrupt_scope)
            async with runtime.database.session_factory() as session:
                frozen_row = (
                    (
                        await session.execute(
                            text(
                                "SELECT im.status AS model_status, im.freeze_reason_code, "
                                "co.status AS operation_status "
                                "FROM ifc_models im "
                                "JOIN ifc_model_revisions ir "
                                "  ON ir.revision_id = im.current_revision_id "
                                "JOIN critical_operations co "
                                "  ON co.operation_id = ir.operation_id "
                                "WHERE im.run_id = :run_id"
                            ),
                            {"run_id": corrupt_scope.run_id},
                        )
                    )
                    .mappings()
                    .one()
                )
        assert dict(frozen_row) == {
            "model_status": "frozen",
            "freeze_reason_code": "IFC_INVOCATION_RETRIES_EXHAUSTED",
            "operation_status": "succeeded",
        }

        command_a = UpdateIfcProjectCommand(patch=IfcProjectPatch(name="candidate-a"))
        command_b = UpdateIfcProjectCommand(patch=IfcProjectPatch(name="candidate-b"))
        request_a = hashlib.sha256(command_a.model_dump_json().encode("utf-8")).hexdigest()
        request_b = hashlib.sha256(command_b.model_dump_json().encode("utf-8")).hexdigest()
        with bind_tenant_execution_context(context):
            prepared_a = await runtime.repository.prepare_revision(
                scope,
                tool_name="ifc_project_update",
                idempotency_key=f"i0a-generic-a:{uuid4().hex}",
                request_digest=request_a,
                intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                operation_detail=IfcProjectUpdateOperationDetail(
                    request=command_a
                ).persisted_payload(),
            )
            prepared_b = await runtime.repository.prepare_revision(
                scope,
                tool_name="ifc_project_update",
                idempotency_key=f"i0a-generic-b:{uuid4().hex}",
                request_digest=request_b,
                intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                operation_detail=IfcProjectUpdateOperationDetail(
                    request=command_b
                ).persisted_payload(),
            )
            claim_a = await runtime.repository.claim_execution(
                scope, operation_id=prepared_a.operation_id,
                request_digest=prepared_a.request_digest,
            )
            claim_b = await runtime.repository.claim_execution(
                scope, operation_id=prepared_b.operation_id,
                request_digest=prepared_b.request_digest,
            )
        assert prepared_a.before == prepared_b.before == current_v1
        publication_a = await _publish_project_name_candidate(
            storage=runtime.storage,
            codec=codec,
            scope=scope,
            prepared=prepared_a,
            project_name="candidate-a",
        )
        publication_b = await _publish_project_name_candidate(
            storage=runtime.storage,
            codec=codec,
            scope=scope,
            prepared=prepared_b,
            project_name="candidate-b",
        )
        with bind_tenant_execution_context(context):
            await _select_direct_project_publication(
                runtime.repository, scope, prepared_a, claim_a, publication_a,
            )
            await _select_direct_project_publication(
                runtime.repository, scope, prepared_b, claim_b, publication_b,
            )
            commit_results = await asyncio.gather(
                runtime.repository.commit_revision(
                    scope,
                    prepared=prepared_a,
                    publication=publication_a,
                    claim=claim_a,
                    project_result=IfcProjectSummary(
                        global_id=publication_a.inspection.project_global_id,
                        name=publication_a.inspection.project_name,
                    ),
                ),
                runtime.repository.commit_revision(
                    scope,
                    prepared=prepared_b,
                    publication=publication_b,
                    claim=claim_b,
                    project_result=IfcProjectSummary(
                        global_id=publication_b.inspection.project_global_id,
                        name=publication_b.inspection.project_name,
                    ),
                ),
                return_exceptions=True,
            )
        winners = [
            (index, result)
            for index, result in enumerate(commit_results)
            if isinstance(result, CommittedIfcRevision)
        ]
        losers = [result for result in commit_results if isinstance(result, IfcModelConflictError)]
        assert len(winners) == len(losers) == 1
        winner_index, committed_winner = winners[0]
        winner = committed_winner.exact
        assert winner.revision_no == winner.write_epoch == 2
        assert winner.revision_id in {
            prepared_a.after_revision_id,
            prepared_b.after_revision_id,
        }

        with bind_tenant_execution_context(context):
            current_v2 = await runtime.repository.get_current(scope)
            async with runtime.database.session_factory() as session:
                operation_rows = (
                    (
                        await session.execute(
                            text(
                                "SELECT operation_id, status, error_code, error_phase "
                                "FROM critical_operations "
                                "WHERE operation_id IN (:operation_a, :operation_b) "
                                "ORDER BY operation_id"
                            ),
                            {
                                "operation_a": prepared_a.operation_id,
                                "operation_b": prepared_b.operation_id,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
        assert current_v2 == winner
        assert sorted(row["status"] for row in operation_rows) == ["failed", "succeeded"]
        failed_row = next(row for row in operation_rows if row["status"] == "failed")
        assert failed_row["error_code"] == "IFC_CURRENT_CONFLICT"
        assert failed_row["error_phase"] == "commit_fence"
        assert await _run_state(runtime, context, scope.run_id) == {
            "model_count": 1,
            "revision_count": 2,
            "artifact_count": 2,
            "artifact_version_count": 2,
            "operation_count": 3,
            "catalog_count": 3,
        }

        restarted = _create_runtime(runtime_url, storage_root)
        await restarted.database.verify_ready()
        restarted_stack = build_in_process_ifc_stack(
            restarted.repository,
            restarted.storage,
        )
        restarted_service = restarted_stack.model_service
        expected_name = ("candidate-a", "candidate-b")[winner_index]
        with bind_tenant_execution_context(context):
            restarted_exact = await restarted_service.get_current_exact(scope)
            restarted_info = await restarted_service.info(scope)
            revision1_info = await restarted_service.get_exact_info(
                scope,
                revision_id=current_v1.revision_id,
            )
        assert restarted_exact == winner
        assert restarted_info.project is not None
        assert restarted_info.project.name == expected_name
        assert revision1_info.project is not None
        assert revision1_info.project.name == PRIMARY_PROJECT_NAME

        fresh_process = await _fresh_process_readback(
            runtime_url=runtime_url,
            storage_root=storage_root,
            context=context,
            scope=scope,
        )
        assert fresh_process == {
            "project_name": expected_name,
            "revision_no": 2,
            "write_epoch": 2,
        }

        other_context, other_scope = await _open_direct_scope(
            restarted,
            configured_subject=f"i0a-isolated-{uuid4().hex}",
            session_name="I0-A isolated tenant",
        )
        with bind_tenant_execution_context(other_context):
            async with restarted.database.session_factory() as session:
                current_user = await session.scalar(text("SELECT current_user"))
                hidden = (
                    (
                        await session.execute(
                            text(
                                "SELECT "
                                "(SELECT count(*) FROM ifc_models WHERE run_id = :run_id) "
                                "AS model_count, "
                                "(SELECT count(*) FROM ifc_model_revisions "
                                " WHERE run_id = :run_id) AS revision_count"
                            ),
                            {"run_id": scope.run_id},
                        )
                    )
                    .mappings()
                    .one()
                )
            isolated_current = await restarted.repository.get_current(other_scope)
        assert current_user == "cad2bim_mcp_login"
        assert dict(hidden) == {"model_count": 0, "revision_count": 0}
        assert isolated_current is None
    finally:
        if restarted is not None:
            await restarted.close()
        await runtime.close()


@pytest.mark.asyncio
async def test_non_initial_revision_reconcile_is_exact_and_never_rebases(
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
        _runtime_database_url(TEST_DATABASE_URL),
        tmp_path / "revision-reconcile-storage",
    )
    try:
        await runtime.database.verify_ready()
        subject = f"i0a-revision-reconcile-{uuid4().hex}"
        succeeded_context, succeeded_scope = await _open_direct_scope(
            runtime,
            configured_subject=subject,
            session_name="I0-B2 succeeded revision reconcile",
        )
        retry_context, retry_scope = await _open_direct_scope(
            runtime,
            configured_subject=subject,
            session_name="I0-B2 uncommitted revision reconcile",
        )
        conflict_context, conflict_scope = await _open_direct_scope(
            runtime,
            configured_subject=subject,
            session_name="I0-B2 stale frontier reconcile",
        )
        codec = IfcOpenShellModelCodec()
        stack = build_in_process_ifc_stack(
            runtime.repository,
            runtime.storage,
            codec=codec,
        )
        service = stack.model_service
        writer = stack.writer

        async def initialize(context, scope, project_name: str) -> None:
            with bind_tenant_execution_context(context):
                await service.initialize(
                    scope,
                    InitializeIfcModelCommand(project_name=project_name),
                )

        async def prepare_project_write(context, scope, name: str, key: str):
            command = UpdateIfcProjectCommand(patch=IfcProjectPatch(name=name))
            digest = hashlib.sha256(command.model_dump_json().encode("utf-8")).hexdigest()
            detail = IfcProjectUpdateOperationDetail(request=command).persisted_payload()
            with bind_tenant_execution_context(context):
                prepared = await writer.prepare(
                    scope,
                    tool_name="ifc_project_update",
                    idempotency_key=key,
                    request_digest=digest,
                    intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                    operation_detail=detail,
                )
            return prepared, digest, detail

        await initialize(succeeded_context, succeeded_scope, "Succeeded reconcile")
        succeeded_key = f"i0b2-succeeded:{uuid4().hex}"
        succeeded_prepared, succeeded_digest, succeeded_detail = await prepare_project_write(
            succeeded_context,
            succeeded_scope,
            "Succeeded exact",
            succeeded_key,
        )
        succeeded_publication = await _publish_project_name_candidate(
            storage=runtime.storage,
            codec=codec,
            scope=succeeded_scope,
            prepared=succeeded_prepared,
            project_name="Succeeded exact",
        )
        with bind_tenant_execution_context(succeeded_context):
            succeeded_claim = await runtime.repository.claim_execution(
                succeeded_scope, operation_id=succeeded_prepared.operation_id,
                request_digest=succeeded_prepared.request_digest,
            )
            await _select_direct_project_publication(
                runtime.repository, succeeded_scope, succeeded_prepared,
                succeeded_claim, succeeded_publication,
            )
            succeeded_commit = await runtime.repository.commit_revision(
                succeeded_scope,
                prepared=succeeded_prepared,
                publication=succeeded_publication,
                claim=succeeded_claim,
                project_result=IfcProjectSummary(
                    global_id=succeeded_publication.inspection.project_global_id,
                    name=succeeded_publication.inspection.project_name,
                ),
            )
            await runtime.repository.freeze_indeterminate(
                succeeded_scope,
                model_id=succeeded_commit.exact.model_id,
                operation_id=None,
                reason_code="IFC_INVOCATION_RETRIES_EXHAUSTED",
            )
            replayed = await writer.prepare(
                succeeded_scope,
                tool_name="ifc_project_update",
                idempotency_key=succeeded_key,
                request_digest=succeeded_digest,
                intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                operation_detail=succeeded_detail,
            )
            succeeded_current = await runtime.repository.get_current(succeeded_scope)
        assert replayed.operation_id == succeeded_prepared.operation_id
        assert succeeded_current is not None
        assert succeeded_current.revision_id == succeeded_prepared.after_revision_id
        assert succeeded_current.model_state.value == "active"

        await initialize(retry_context, retry_scope, "Uncommitted reconcile")
        retry_key = f"i0b2-retry:{uuid4().hex}"
        retry_prepared, retry_digest, retry_detail = await prepare_project_write(
            retry_context,
            retry_scope,
            "Safe retry",
            retry_key,
        )
        with bind_tenant_execution_context(retry_context):
            await runtime.repository.freeze_indeterminate(
                retry_scope,
                model_id=retry_prepared.before.model_id,
                operation_id=retry_prepared.operation_id,
                reason_code="IFC_INVOCATION_RETRIES_EXHAUSTED",
            )
            reopened = await writer.prepare(
                retry_scope,
                tool_name="ifc_project_update",
                idempotency_key=retry_key,
                request_digest=retry_digest,
                intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                operation_detail=retry_detail,
            )
            retry_current = await runtime.repository.get_current(retry_scope)
        assert reopened.operation_id == retry_prepared.operation_id
        assert retry_current is not None
        assert retry_current.revision_id == retry_prepared.before.revision_id
        assert retry_current.write_epoch == retry_prepared.before.write_epoch
        assert retry_current.model_state.value == "active"

        await initialize(conflict_context, conflict_scope, "Conflict reconcile")
        stale_key = f"i0b2-stale:{uuid4().hex}"
        stale, stale_digest, stale_detail = await prepare_project_write(
            conflict_context,
            conflict_scope,
            "Stale request",
            stale_key,
        )
        winner, _, _ = await prepare_project_write(
            conflict_context,
            conflict_scope,
            "Concurrent winner",
            f"i0b2-winner:{uuid4().hex}",
        )
        winner_publication = await _publish_project_name_candidate(
            storage=runtime.storage,
            codec=codec,
            scope=conflict_scope,
            prepared=winner,
            project_name="Concurrent winner",
        )
        with bind_tenant_execution_context(conflict_context):
            winner_claim = await runtime.repository.claim_execution(
                conflict_scope, operation_id=winner.operation_id,
                request_digest=winner.request_digest,
            )
            await _select_direct_project_publication(
                runtime.repository, conflict_scope, winner, winner_claim, winner_publication,
            )
            await runtime.repository.commit_revision(
                conflict_scope,
                prepared=winner,
                publication=winner_publication,
                claim=winner_claim,
                project_result=IfcProjectSummary(
                    global_id=winner_publication.inspection.project_global_id,
                    name=winner_publication.inspection.project_name,
                ),
            )
            await runtime.repository.freeze_indeterminate(
                conflict_scope,
                model_id=stale.before.model_id,
                operation_id=stale.operation_id,
                reason_code="IFC_INVOCATION_RETRIES_EXHAUSTED",
            )
            with pytest.raises(IfcModelFrozenError, match="CURRENT left"):
                await writer.prepare(
                    conflict_scope,
                    tool_name="ifc_project_update",
                    idempotency_key=stale_key,
                    request_digest=stale_digest,
                    intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                    operation_detail=stale_detail,
                )
            conflict_current = await runtime.repository.get_current(conflict_scope)
        assert conflict_current is not None
        assert conflict_current.revision_id == winner.after_revision_id
        assert conflict_current.model_state.value == "frozen"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_ifc_claim_rejects_live_child_and_stale_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(TEST_DATABASE_URL, environ={
        "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
        "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
        "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
    })
    runtime = _create_runtime(_runtime_database_url(TEST_DATABASE_URL), tmp_path / "storage")
    guardian: subprocess.Popen[bytes] | None = None
    try:
        context, scope = await _open_direct_scope(
            runtime, configured_subject=f"p2-claim-{uuid4().hex}",
            session_name="P2 execution claim",
        )
        command = InitializeIfcModelCommand(project_name="P2 claim proof")
        digest = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
        project_id = IfcOpenShellModelCodec().stable_project_global_id(scope.model_dump_json())
        with bind_tenant_execution_context(context):
            prepared = await runtime.repository.prepare_initialize(
                scope, request_digest=digest, project_global_id=project_id,
                project_name=command.project_name,
            )
            first = await runtime.repository.claim_execution(
                scope, operation_id=prepared.operation_id, request_digest=digest,
            )
            # SQL CHECK must reject partial active claims. PostgreSQL accepts
            # UNKNOWN from a nullable expression unless the condition is explicit.
            with pytest.raises(IntegrityError):
                async with runtime.database.session_factory() as session, session.begin():
                    await session.execute(text(
                        "UPDATE critical_operations SET ifc_execution_token_sha256 = NULL "
                        "WHERE operation_id = :operation_id"
                    ), {"operation_id": prepared.operation_id})
            with pytest.raises(IntegrityError):
                async with runtime.database.session_factory() as session, session.begin():
                    await session.execute(text(
                        "UPDATE critical_operations SET ifc_guard_pid = 12345 "
                        "WHERE operation_id = :operation_id"
                    ), {"operation_id": prepared.operation_id})
            with pytest.raises(IfcModelConflictError, match="active or unknown"):
                await runtime.repository.claim_execution(
                    scope, operation_id=prepared.operation_id, request_digest=digest,
                )
            guardian = subprocess.Popen(
                [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
                start_new_session=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            birth_us = round(psutil.Process(guardian.pid).create_time() * 1_000_000)
            await runtime.repository.associate_execution_guard(
                scope, first, guardian_pid=guardian.pid, guardian_birth_us=birth_us,
            )
            # Model a dead Backend PID while the registered group remains alive.
            monkeypatch.setattr(repository_ifc_model, "local_executor_stopped", lambda *_a, **_k: True)
            with pytest.raises(IfcModelConflictError, match="child is active"):
                await runtime.repository.claim_execution(
                    scope, operation_id=prepared.operation_id, request_digest=digest,
                )
            os.killpg(guardian.pid, signal.SIGKILL)
            guardian.wait(timeout=5)
            second = await runtime.repository.claim_execution(
                scope, operation_id=prepared.operation_id, request_digest=digest,
            )
            assert second.generation == first.generation + 1
            assert second.deadline_at == first.deadline_at
            with pytest.raises(IfcModelConflictError, match="stale"):
                await runtime.repository.record_failed_ifc_operation(
                    scope, operation_id=prepared.operation_id,
                    error_code="IFC_TEST_REJECTED", error_message="known rejection",
                    error_phase=CriticalOperationErrorPhase.IFC_VALIDATION, claim=first,
                )
            alias = await runtime.repository.record_failed_ifc_operation(
                scope, operation_id=prepared.operation_id,
                error_code="IFC_TEST_REJECTED", error_message="known rejection",
                error_phase=CriticalOperationErrorPhase.IFC_VALIDATION, claim=second,
            )
            assert alias.startswith("op_")
    finally:
        if guardian is not None and guardian.poll() is None:
            os.killpg(guardian.pid, signal.SIGKILL)
            guardian.wait(timeout=5)
        await runtime.close()


@pytest.mark.asyncio
async def test_ifc_cancel_and_commit_use_run_then_operation_lock_order(tmp_path: Path) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(TEST_DATABASE_URL, environ={
        "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
        "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
        "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
    })
    runtime = _create_runtime(_runtime_database_url(TEST_DATABASE_URL), tmp_path / "storage")
    try:
        stack = build_in_process_ifc_stack(runtime.repository, runtime.storage)
        for commit_first in (False, True):
            context, scope = await _open_direct_scope(
                runtime, configured_subject=f"p2-order-{uuid4().hex}",
                session_name=f"P2 lock order {commit_first}",
            )
            command = UpdateIfcProjectCommand(
                patch=IfcProjectPatch(name=f"P2 update {commit_first}"),
            )
            digest = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
            detail = IfcProjectUpdateOperationDetail(request=command).persisted_payload()
            key = f"p2-lock:{uuid4().hex}"
            with bind_tenant_execution_context(context):
                await stack.model_service.initialize(
                    scope, InitializeIfcModelCommand(project_name="P2 lock base"),
                )
                prepared = await runtime.repository.prepare_revision(
                    scope, tool_name="ifc_project_update", idempotency_key=key,
                    request_digest=digest,
                    intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                    operation_detail=detail,
                )
                claim = await runtime.repository.claim_execution(
                    scope, operation_id=prepared.operation_id, request_digest=digest,
                )
            publication = await _publish_project_name_candidate(
                storage=runtime.storage, codec=stack.codec, scope=scope,
                prepared=prepared, project_name=f"P2 update {commit_first}",
            )
            project = IfcProjectSummary(
                global_id=publication.inspection.project_global_id,
                name=publication.inspection.project_name,
            )
            with bind_tenant_execution_context(context):
                await _select_direct_project_publication(
                    runtime.repository, scope, prepared, claim, publication,
                )
                if commit_first:
                    committed = await runtime.repository.commit_revision(
                        scope, prepared=prepared, publication=publication,
                        project_result=project, claim=claim,
                    )
                    await runtime.repository.cancel_run(scope.owner_id, scope.run_id)
                    replay = await runtime.repository.commit_revision(
                        scope, prepared=prepared, publication=publication,
                        project_result=project, claim=claim,
                    )
                    assert replay == committed
                    exact = await runtime.repository.get_exact(
                        scope, revision_id=prepared.after_revision_id,
                    )
                    assert exact == committed.exact
                else:
                    await runtime.repository.cancel_run(scope.owner_id, scope.run_id)
                    with pytest.raises(IfcModelConflictError, match="no longer accepts"):
                        await runtime.repository.commit_revision(
                            scope, prepared=prepared, publication=publication,
                            project_result=project, claim=claim,
                        )
                    assert await runtime.repository.get_exact(
                        scope, revision_id=prepared.after_revision_id,
                    ) is None
                    # The child already returned a deterministic failure, so
                    # cancellation cannot prevent its original O from settling.
                    await runtime.repository.record_failed_ifc_operation(
                        scope, operation_id=prepared.operation_id,
                        error_code="IFC_TEST_CANCELLED", error_message="no revision committed",
                        error_phase=CriticalOperationErrorPhase.IFC_VALIDATION, claim=claim,
                    )
                    state = await runtime.repository.get_revision_operation_state(
                        scope, tool_name="ifc_project_update", idempotency_key=key,
                        request_digest=digest,
                        intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                        operation_detail=detail,
                    )
                    assert state is not None and state.status.value == "failed"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_ifc_claim_columns_are_writable_under_runner_rls(tmp_path: Path) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(TEST_DATABASE_URL, environ={
        "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
        "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
        "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
    })
    runtime = _create_runtime(_runtime_database_url(TEST_DATABASE_URL), tmp_path / "storage")
    runner_url = make_url(TEST_DATABASE_URL).set(
        drivername="postgresql+psycopg", username="cad2bim_runner_login",
        password=RUNNER_PASSWORD,
    ).render_as_string(hide_password=False)
    runner_engine = create_async_engine(runner_url, pool_pre_ping=True)
    api_url = make_url(TEST_DATABASE_URL).set(
        drivername="postgresql+psycopg", username="cad2bim_api_login",
        password=API_PASSWORD,
    ).render_as_string(hide_password=False)
    api_engine = create_async_engine(api_url, pool_pre_ping=True)
    runner_sessions = async_sessionmaker(
        runner_engine, class_=TenantAwareAsyncSession, expire_on_commit=False,
    )
    runner_repository = PostgresControlPlaneRepository(runner_sessions)
    api_repository = PostgresControlPlaneRepository(async_sessionmaker(
        api_engine, class_=TenantAwareAsyncSession, expire_on_commit=False,
    ))
    try:
        context, scope = await _open_direct_scope(
            runtime, configured_subject=f"p2-runner-rls-{uuid4().hex}",
            session_name="P2 Runner RLS",
        )
        command = InitializeIfcModelCommand(project_name="Runner column proof")
        digest = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
        project_id = IfcOpenShellModelCodec().stable_project_global_id(scope.model_dump_json())
        with bind_tenant_execution_context(context):
            prepared = await runtime.repository.prepare_initialize(
                scope, request_digest=digest, project_global_id=project_id,
                project_name=command.project_name,
            )
        runner_context = context.model_copy(update={"actor_kind": TenantActorKind.RUNNER})
        with bind_tenant_execution_context(runner_context):
            claim = await runner_repository.claim_execution(
                scope, operation_id=prepared.operation_id, request_digest=digest,
            )
            encoded = IfcOpenShellModelCodec().create_revision1(
                project_global_id=project_id, project_name=command.project_name,
                header_timestamp=prepared.prepared_at,
            )
            selected = await runner_repository.select_candidate(
                scope, prepared, claim,
                IfcSelectedCandidate(
                    origin_generation=claim.generation, upload_id=uuid4().hex,
                    size_bytes=encoded.inspection.size_bytes,
                    sha256=encoded.inspection.sha256,
                    inspection=encoded.inspection,
                ),
            )
            await runner_repository.retire_execution(scope, claim)
        with bind_tenant_execution_context(context):
            api_claim = await api_repository.claim_execution(
                scope, operation_id=prepared.operation_id, request_digest=digest,
            )
            operation_state = await api_repository.get_initialize_operation_state(
                scope, request_digest=digest, project_global_id=project_id,
                project_name=command.project_name,
            )
            assert operation_state is not None and operation_state.status.value == "prepared"
        assert api_claim.generation == claim.generation + 1
        with bind_tenant_execution_context(runner_context):
            with pytest.raises(IfcModelConflictError, match="stale"):
                await runner_repository.select_candidate(
                    scope, prepared, claim, selected,
                )
        with bind_tenant_execution_context(context):
            assert await api_repository.get_selected_candidate(
                scope, prepared, api_claim,
            ) == selected
            assert await api_repository.get_exact(
                scope, revision_id=prepared.revision_id,
            ) is None
            await api_repository.retire_execution(scope, api_claim)
        foreign_context = runner_context.model_copy(update={"tenant_id": "tenant_other"})
        with bind_tenant_execution_context(foreign_context):
            async with runner_sessions() as session:
                foreign = await session.scalar(text(
                    "SELECT operation_id FROM critical_operations WHERE operation_id = :operation_id"
                ), {"operation_id": prepared.operation_id})
        assert foreign is None
    finally:
        await api_engine.dispose()
        await runner_engine.dispose()
        await runtime.close()


@pytest.mark.asyncio
async def test_ifc_claim_real_host_death_and_fixed_deadline(tmp_path: Path) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(TEST_DATABASE_URL, environ={
        "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
        "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
        "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
    })
    runtime = _create_runtime(_runtime_database_url(TEST_DATABASE_URL), tmp_path / "storage")
    admin_engine = create_async_engine(TEST_DATABASE_URL)
    hosts: list[subprocess.Popen[bytes]] = []
    guards: list[tuple[int, int]] = []
    try:
        context, scope = await _open_direct_scope(
            runtime, configured_subject=f"p2-host-death-{uuid4().hex}",
            session_name="P2 real claim host death",
        )
        stack = build_in_process_ifc_stack(runtime.repository, runtime.storage)
        initial_command = InitializeIfcModelCommand(project_name="P2 death base")
        with bind_tenant_execution_context(context):
            initial_result = await stack.model_service.initialize(scope, initial_command)
            initial_state = await runtime.repository.get_initialize_operation_state(
                scope,
                request_digest=hashlib.sha256(
                    initial_command.model_dump_json().encode()
                ).hexdigest(),
                project_global_id=stack.codec.stable_project_global_id(scope.model_dump_json()),
                project_name=initial_command.project_name,
            )
            assert initial_state is not None
            command = UpdateIfcProjectCommand(
                patch=IfcProjectPatch(name="P2 crash candidate"),
            )
            digest = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
            prepared = await runtime.repository.prepare_revision(
                scope, tool_name="ifc_project_update", idempotency_key=f"p2-host:{uuid4().hex}",
                request_digest=digest, intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                operation_detail=IfcProjectUpdateOperationDetail(request=command).persisted_payload(),
            )
        publication = await _publish_project_name_candidate(
            storage=runtime.storage, codec=stack.codec, scope=scope,
            prepared=prepared, project_name="P2 crash candidate",
        )
        project = IfcProjectSummary(
            global_id=publication.inspection.project_global_id,
            name=publication.inspection.project_name,
        )

        async def wait_file(path: Path, host: subprocess.Popen[bytes]) -> None:
            for _ in range(160):
                if path.exists():
                    return
                if host.poll() is not None:
                    raise AssertionError(f"claim host exited before {path.name}: {host.stderr.read()[-1200:]!r}")
                await asyncio.sleep(0.05)
            raise AssertionError(f"claim host did not reach {path.name}")

        async def wait_group_stopped(pid: int, birth_us: int) -> None:
            for _ in range(160):
                if local_guard_process_group_stopped(pid, birth_us):
                    return
                await asyncio.sleep(0.05)
            raise AssertionError("registered child process group survived host death")

        original_deadline = None
        for mode in ("before", "after"):
            phase = tmp_path / mode
            phase.mkdir()
            environment = os.environ.copy()
            environment.update({
                "P2_IFC_HOST_PHASE": str(phase),
                "P2_IFC_HOST_MODE": mode,
                "P2_IFC_HOST_SCOPE": scope.model_dump_json(),
                "P2_IFC_HOST_CONTEXT": context.model_dump_json(),
                "P2_IFC_HOST_DATABASE_URL": _runtime_database_url(TEST_DATABASE_URL),
                "P2_IFC_HOST_OPERATION_ID": prepared.operation_id,
                "P2_IFC_HOST_DIGEST": digest,
            })
            host = subprocess.Popen(
                [sys.executable, "-m", "tests.process_support.ifc_claim_crash_host"],
                cwd=Path(__file__).resolve().parents[3], env=environment,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            hosts.append(host)
            await wait_file(phase / "guard.json", host)
            old_claim = IfcExecutionClaim.model_validate_json(
                (phase / "claim.json").read_text()
            )
            guard = json.loads((phase / "guard.json").read_text())
            guard_identity = (guard["pid"], guard["birth_us"])
            guards.append(guard_identity)
            assert not local_guard_process_group_stopped(*guard_identity)
            if mode == "before":
                assert not (phase / "registered").exists()
                assert not (phase / "child-started").exists()
            else:
                await wait_file(phase / "registered", host)
                await wait_file(phase / "child-started", host)
            os.kill(host.pid, signal.SIGKILL)
            await asyncio.to_thread(host.wait, 5)
            assert local_executor_stopped(old_claim.owner, kind="ifc")
            await wait_group_stopped(*guard_identity)
            if mode == "before":
                assert not (phase / "child-started").exists()
            with bind_tenant_execution_context(context):
                new_claim = await runtime.repository.claim_execution(
                    scope, operation_id=prepared.operation_id, request_digest=digest,
                )
                assert new_claim.generation == old_claim.generation + 1
                assert new_claim.deadline_at == old_claim.deadline_at
                if original_deadline is None:
                    original_deadline = new_claim.deadline_at
                else:
                    assert new_claim.deadline_at == original_deadline
                with pytest.raises(IfcModelConflictError, match="stale"):
                    await runtime.repository.commit_revision(
                        scope, prepared=prepared, publication=publication,
                        project_result=project, claim=old_claim,
                    )
                if mode == "before":
                    await runtime.repository.retire_execution(scope, new_claim)

        async with admin_engine.begin() as connection:
            await connection.execute(text(
                "UPDATE critical_operations SET ifc_deadline_at = clock_timestamp() - interval '1 second' "
                "WHERE operation_id IN (:prepared_id, :successful_id)"
            ), {
                "prepared_id": prepared.operation_id,
                "successful_id": initial_state.prepared.operation_id,
            })
        assert original_deadline is not None
        with bind_tenant_execution_context(context):
            with pytest.raises(IfcModelConflictError, match="deadline has expired"):
                await runtime.repository.commit_revision(
                    scope, prepared=prepared, publication=publication,
                    project_result=project, claim=new_claim,
                )
            await runtime.repository.retire_execution(scope, new_claim)
            with pytest.raises(IfcModelConflictError, match="deadline has expired"):
                await runtime.repository.claim_execution(
                    scope, operation_id=prepared.operation_id, request_digest=digest,
                )
            reopened = build_in_process_ifc_stack(runtime.repository, runtime.storage)
            assert await reopened.model_service.initialize(scope, initial_command) == initial_result
    finally:
        for host in hosts:
            if host.poll() is None:
                os.kill(host.pid, signal.SIGKILL)
                await asyncio.to_thread(host.wait, 5)
            if host.stderr is not None:
                host.stderr.close()
        for pid, birth_us in guards:
            if not local_guard_process_group_stopped(pid, birth_us):
                os.killpg(pid, signal.SIGKILL)
        await admin_engine.dispose()
        await runtime.close()


@pytest.mark.asyncio
async def test_ifc_cancel_commit_races_use_real_run_lock(tmp_path: Path) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(TEST_DATABASE_URL, environ={
        "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
        "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
        "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
    })
    runtime = _create_runtime(_runtime_database_url(TEST_DATABASE_URL), tmp_path / "storage")
    admin_engine = create_async_engine(TEST_DATABASE_URL)
    try:
        stack = build_in_process_ifc_stack(runtime.repository, runtime.storage)
        for commit_first in (False, True):
            context, scope = await _open_direct_scope(
                runtime, configured_subject=f"p2-race-{uuid4().hex}",
                session_name=f"P2 lock race {commit_first}",
            )
            command = UpdateIfcProjectCommand(
                patch=IfcProjectPatch(name=f"P2 race {commit_first}"),
            )
            digest = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
            with bind_tenant_execution_context(context):
                await stack.model_service.initialize(
                    scope, InitializeIfcModelCommand(project_name="P2 race base"),
                )
                prepared = await runtime.repository.prepare_revision(
                    scope, tool_name="ifc_project_update", idempotency_key=f"p2-race:{uuid4().hex}",
                    request_digest=digest, intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                    operation_detail=IfcProjectUpdateOperationDetail(request=command).persisted_payload(),
                )
                claim = await runtime.repository.claim_execution(
                    scope, operation_id=prepared.operation_id, request_digest=digest,
                )
            publication = await _publish_project_name_candidate(
                storage=runtime.storage, codec=stack.codec, scope=scope,
                prepared=prepared, project_name=f"P2 race {commit_first}",
            )
            project = IfcProjectSummary(
                global_id=publication.inspection.project_global_id,
                name=publication.inspection.project_name,
            )
            with bind_tenant_execution_context(context):
                await _select_direct_project_publication(
                    runtime.repository, scope, prepared, claim, publication,
                )
            race_repository = PostgresControlPlaneRepository(runtime.database.session_factory)
            original_ifc_run = race_repository._require_ifc_run
            original_cancel_run = race_repository._require_run
            winner_locked = asyncio.Event()
            loser_entered = asyncio.Event()
            release_winner = asyncio.Event()
            winner_pid: int | None = None
            loser_pid: int | None = None

            async def hold_ifc_run(session, selected_scope, *, lock):
                nonlocal winner_pid
                result = await original_ifc_run(session, selected_scope, lock=lock)
                if commit_first and lock:
                    winner_pid = await session.scalar(text("SELECT pg_backend_pid()"))
                    winner_locked.set()
                    await release_winner.wait()
                return result

            async def hold_cancel_run(session, owner_id, run_id, *, for_update=False):
                nonlocal winner_pid
                result = await original_cancel_run(
                    session, owner_id, run_id, for_update=for_update,
                )
                if not commit_first and for_update:
                    winner_pid = await session.scalar(text("SELECT pg_backend_pid()"))
                    winner_locked.set()
                    await release_winner.wait()
                return result

            async def watch_ifc_run(session, selected_scope, *, lock):
                nonlocal loser_pid
                loser_pid = await session.scalar(text("SELECT pg_backend_pid()"))
                loser_entered.set()
                return await original_ifc_run(session, selected_scope, lock=lock)

            async def watch_cancel_run(session, owner_id, run_id, *, for_update=False):
                nonlocal loser_pid
                loser_pid = await session.scalar(text("SELECT pg_backend_pid()"))
                loser_entered.set()
                return await original_cancel_run(
                    session, owner_id, run_id, for_update=for_update,
                )

            race_repository._require_ifc_run = hold_ifc_run if commit_first else watch_ifc_run
            race_repository._require_run = watch_cancel_run if commit_first else hold_cancel_run

            async def commit():
                return await race_repository.commit_revision(
                    scope, prepared=prepared, publication=publication,
                    project_result=project, claim=claim,
                )

            async def cancel():
                return await race_repository.cancel_run(scope.owner_id, scope.run_id)

            with bind_tenant_execution_context(context):
                winner = asyncio.create_task(commit() if commit_first else cancel())
                await asyncio.wait_for(winner_locked.wait(), timeout=5)
                loser = asyncio.create_task(cancel() if commit_first else commit())
                try:
                    await asyncio.wait_for(loser_entered.wait(), timeout=5)
                    assert winner_pid is not None and loser_pid is not None
                    assert winner_pid != loser_pid
                    for _ in range(100):
                        async with admin_engine.connect() as connection:
                            blocking = await connection.scalar(text(
                                "SELECT pg_blocking_pids(:pid)"
                            ), {"pid": loser_pid})
                        if winner_pid in blocking:
                            break
                        await asyncio.sleep(0.02)
                    else:
                        raise AssertionError("losing IFC transaction never waited for the Run lock")
                    assert not loser.done()
                finally:
                    release_winner.set()
                if commit_first:
                    committed = await winner
                    await loser
                    assert await runtime.repository.get_exact(
                        scope, revision_id=prepared.after_revision_id,
                    ) == committed.exact
                else:
                    await winner
                    with pytest.raises(IfcModelConflictError, match="no longer accepts"):
                        await loser
                    assert await runtime.repository.get_exact(
                        scope, revision_id=prepared.after_revision_id,
                    ) is None
    finally:
        await admin_engine.dispose()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("formal_key_before_restart", [False, True])
async def test_ifc_selected_revision_resumes_in_new_pid_without_mutation(
    tmp_path: Path, formal_key_before_restart: bool,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(TEST_DATABASE_URL, environ={
        "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
        "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
        "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
    })
    storage_root = tmp_path / "storage"
    runtime = _create_runtime(_runtime_database_url(TEST_DATABASE_URL), storage_root)
    try:
        context, scope = await _open_direct_scope(
            runtime, configured_subject=f"p3-selected-{uuid4().hex}",
            session_name="P3 selected candidate restart",
        )
        stack = build_in_process_ifc_stack(runtime.repository, runtime.storage)
        command = UpdateIfcProjectCommand(
            patch=IfcProjectPatch(name="Selected before restart"),
        )
        digest = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
        with bind_tenant_execution_context(context):
            await stack.model_service.initialize(
                scope, InitializeIfcModelCommand(project_name="P3 selected base"),
            )
            prepared = await runtime.repository.prepare_revision(
                scope, tool_name="ifc_project_update", idempotency_key=f"p3-selected:{uuid4().hex}",
                request_digest=digest, intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                operation_detail=IfcProjectUpdateOperationDetail(request=command).persisted_payload(),
            )
            claim = await runtime.repository.claim_execution(
                scope, operation_id=prepared.operation_id, request_digest=digest,
            )
        generated, project = await _private_project_candidate(
            runtime.storage, stack.codec, scope, prepared, "Selected before restart",
        )
        final_key = _storage_key(scope, prepared.artifact_id)
        with bind_tenant_execution_context(context):
            selected, staged = await stack.writer._stage_and_select(
                scope, prepared, claim, generated, project_result=project,
            )
            assert staged is not None
            assert selected.project_result == project
            with pytest.raises(IfcModelConflictError, match="cannot change"):
                await runtime.repository.select_candidate(
                    scope, prepared, claim,
                    selected.model_copy(update={"upload_id": uuid4().hex}),
                )
            if formal_key_before_restart:
                await stack.writer._publish_selected(
                    scope, prepared, selected, staged=staged,
                )
                assert await runtime.storage.exists(final_key)
            else:
                assert not await runtime.storage.exists(final_key)
            state = await runtime.repository.get_revision_operation_state(
                scope, tool_name="ifc_project_update",
                idempotency_key=prepared.idempotency_key,
                request_digest=digest,
                intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                operation_detail=IfcProjectUpdateOperationDetail(request=command).persisted_payload(),
            )
            assert state is not None and state.status.value == "prepared"
            await runtime.repository.retire_execution(scope, claim)
        await generated.candidate.aclose()
        environment = os.environ.copy()
        environment.update({
            "P3_IFC_SCOPE": scope.model_dump_json(),
            "P3_IFC_CONTEXT": context.model_dump_json(),
            "P3_IFC_PREPARED": prepared.model_dump_json(),
            "P3_IFC_DATABASE_URL": _runtime_database_url(TEST_DATABASE_URL),
            "P3_IFC_STORAGE_ROOT": str(storage_root),
        })
        completed = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "tests.process_support.ifc_selected_candidate_resume"],
            cwd=Path(__file__).resolve().parents[3], env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
        )
        assert completed.returncode == 0, completed.stderr.decode()[-1600:]
        recovered = json.loads(completed.stdout)
        assert recovered["revision_id"] == prepared.after_revision_id
        assert recovered["sha256"] == selected.sha256
        assert recovered["project_name"] == project.name
        assert recovered["generation"] == claim.generation + 1
        assert recovered["mutations"] == 0
        assert recovered["inspections"] >= 1
        with bind_tenant_execution_context(context):
            exact = await runtime.repository.get_exact(
                scope, revision_id=prepared.after_revision_id,
            )
            assert exact is not None and exact.artifact.sha256 == selected.sha256
            assert await runtime.storage.exists(final_key)
            with pytest.raises(IfcModelConflictError, match="stale"):
                await runtime.repository.commit_revision(
                    scope, prepared=prepared, claim=claim,
                    publication=await stack.writer._publish_selected(
                        scope, prepared, selected, staged=None,
                    ),
                    project_result=project,
                )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_ifc_unselected_private_bytes_cannot_occupy_formal_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(TEST_DATABASE_URL, environ={
        "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
        "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
        "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
    })
    runtime = _create_runtime(_runtime_database_url(TEST_DATABASE_URL), tmp_path / "storage")
    try:
        context, scope = await _open_direct_scope(
            runtime, configured_subject=f"p3-unselected-{uuid4().hex}",
            session_name="P3 unselected private bytes",
        )
        stack = build_in_process_ifc_stack(runtime.repository, runtime.storage)
        command = UpdateIfcProjectCommand(patch=IfcProjectPatch(name="Selected B"))
        digest = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
        with bind_tenant_execution_context(context):
            await stack.model_service.initialize(
                scope, InitializeIfcModelCommand(project_name="P3 unselected base"),
            )
            prepared = await runtime.repository.prepare_revision(
                scope, tool_name="ifc_project_update", idempotency_key=f"p3-unselected:{uuid4().hex}",
                request_digest=digest, intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                operation_detail=IfcProjectUpdateOperationDetail(request=command).persisted_payload(),
            )
            first = await runtime.repository.claim_execution(
                scope, operation_id=prepared.operation_id, request_digest=digest,
            )
        candidate_a, project_a = await _private_project_candidate(
            runtime.storage, stack.codec, scope, prepared, "Orphan A",
        )
        original_select = runtime.repository.select_candidate

        async def fail_select(*args, **kwargs):
            raise RuntimeError("injected host death before selection")

        monkeypatch.setattr(runtime.repository, "select_candidate", fail_select)
        with bind_tenant_execution_context(context):
            with pytest.raises(RuntimeError, match="before selection"):
                await stack.writer._stage_and_select(
                    scope, prepared, first, candidate_a, project_result=project_a,
                )
            assert not await runtime.storage.exists(_storage_key(scope, prepared.artifact_id))
            await runtime.repository.retire_execution(scope, first)
        await candidate_a.candidate.aclose()
        monkeypatch.setattr(runtime.repository, "select_candidate", original_select)
        with bind_tenant_execution_context(context):
            second = await runtime.repository.claim_execution(
                scope, operation_id=prepared.operation_id, request_digest=digest,
            )
        candidate_b, project_b = await _private_project_candidate(
            runtime.storage, stack.codec, scope, prepared, "Selected B",
        )
        with bind_tenant_execution_context(context):
            committed = await stack.writer.commit(
                scope, prepared, candidate_b, claim=second, project_result=project_b,
            )
            await runtime.repository.retire_execution(scope, second)
        await candidate_b.candidate.aclose()
        assert committed.inspection.project_name == "Selected B"
        assert committed.inspection.sha256 != candidate_a.inspection.sha256
        assert (await runtime.storage.read(_storage_key(scope, prepared.artifact_id)))
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_ifc_selected_initialize_resumes_without_rebuilding(tmp_path: Path) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(TEST_DATABASE_URL, environ={
        "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
        "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
        "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
    })
    runtime = _create_runtime(_runtime_database_url(TEST_DATABASE_URL), tmp_path / "storage")
    try:
        context, scope = await _open_direct_scope(
            runtime, configured_subject=f"p3-init-resume-{uuid4().hex}",
            session_name="P3 selected initialization",
        )
        stack = build_in_process_ifc_stack(runtime.repository, runtime.storage)
        command = InitializeIfcModelCommand(project_name="Selected initial project")
        digest = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
        global_id = stack.codec.stable_project_global_id(scope.model_dump_json())
        with bind_tenant_execution_context(context):
            prepared = await runtime.repository.prepare_initialize(
                scope, request_digest=digest, project_global_id=global_id,
                project_name=command.project_name,
            )
            claim = await runtime.repository.claim_execution(
                scope, operation_id=prepared.operation_id, request_digest=digest,
            )
        encoded = stack.codec.create_revision1(
            project_global_id=global_id, project_name=command.project_name,
            header_timestamp=prepared.prepared_at,
        )
        generated = GeneratedIfcRevision(
            candidate=BytesIfcCandidate(encoded.content, sha256=encoded.inspection.sha256),
            inspection=encoded.inspection,
        )
        with bind_tenant_execution_context(context):
            selected, _ = await stack.writer._stage_and_select(
                scope, prepared, claim, generated,
            )
            assert selected.inspection == encoded.inspection
            assert await runtime.repository.get_exact(
                scope, revision_id=prepared.revision_id,
            ) is None
            await runtime.repository.retire_execution(scope, claim)
        await generated.candidate.aclose()
        restarted = build_in_process_ifc_stack(runtime.repository, runtime.storage)
        original_execute = restarted.runtime.execute
        mutations = 0

        async def inspect_only(request, *, source=None):
            nonlocal mutations
            if not isinstance(request, InspectExactIfcRevisionRequest):
                mutations += 1
                raise AssertionError("selected initialization rebuilt revision 1")
            return await original_execute(request, source=source)

        restarted.runtime.execute = inspect_only
        with bind_tenant_execution_context(context):
            result = await restarted.model_service.initialize(scope, command)
            exact = await runtime.repository.get_exact(scope, revision_id=prepared.revision_id)
        assert result.model == encoded.inspection.to_safe_info()
        assert exact is not None and exact.revision_no == 1
        assert exact.artifact.sha256 == encoded.inspection.sha256
        assert mutations == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["private", "formal"])
async def test_ifc_selected_bytes_must_match_real_storage_before_commit(
    tmp_path: Path, damage: str,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(TEST_DATABASE_URL, environ={
        "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
        "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
        "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
    })
    runtime = _create_runtime(_runtime_database_url(TEST_DATABASE_URL), tmp_path / "storage")
    try:
        context, scope = await _open_direct_scope(
            runtime, configured_subject=f"p3-damage-{uuid4().hex}",
            session_name="P3 selected bytes integrity",
        )
        stack = build_in_process_ifc_stack(runtime.repository, runtime.storage)
        command = UpdateIfcProjectCommand(patch=IfcProjectPatch(name="Selected intact"))
        digest = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
        with bind_tenant_execution_context(context):
            await stack.model_service.initialize(
                scope, InitializeIfcModelCommand(project_name="P3 damage base"),
            )
            prepared = await runtime.repository.prepare_revision(
                scope, tool_name="ifc_project_update", idempotency_key=f"p3-damage:{uuid4().hex}",
                request_digest=digest, intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                operation_detail=IfcProjectUpdateOperationDetail(request=command).persisted_payload(),
            )
            claim = await runtime.repository.claim_execution(
                scope, operation_id=prepared.operation_id, request_digest=digest,
            )
        generated, project = await _private_project_candidate(
            runtime.storage, stack.codec, scope, prepared, "Selected intact",
        )
        with bind_tenant_execution_context(context):
            selected, _ = await stack.writer._stage_and_select(
                scope, prepared, claim, generated, project_result=project,
            )
            await runtime.repository.retire_execution(scope, claim)
        await generated.candidate.aclose()
        private_key = StorageKey(
            owner_id=scope.owner_id, task_id=scope.task_id,
            object_id=f"ifc-candidate-{prepared.operation_id}",
            version=f"{selected.origin_generation}-{selected.upload_id}",
        )
        formal_key = _storage_key(scope, prepared.artifact_id)
        if damage == "private":
            payload = runtime.storage._logical_directory(private_key) / "payload"
            content = payload.read_bytes()
            payload.write_bytes(bytes([content[0] ^ 1]) + content[1:])
        else:
            await runtime.storage.write(
                formal_key, filename=IFC_REVISION_FILENAME,
                content_type=IFC_REVISION_CONTENT_TYPE,
                chunks=_one_chunk(b"different IFC bytes"),
            )
        with bind_tenant_execution_context(context):
            next_claim = await runtime.repository.claim_execution(
                scope, operation_id=prepared.operation_id, request_digest=digest,
            )
            with pytest.raises(
                StorageConflictError if damage == "private" else IfcModelIntegrityError
            ):
                await stack.writer.resume_selected_revision(scope, prepared, next_claim)
            assert await runtime.repository.get_exact(
                scope, revision_id=prepared.after_revision_id,
            ) is None
            await runtime.repository.retire_execution(scope, next_claim)
    finally:
        await runtime.close()


async def _product_transaction_fixture(tmp_path):
    """Actual typed input bytes and runtime-role DB; no current resource library."""
    from datetime import datetime, timezone
    import pyarrow as pa
    import pyarrow.parquet as pq
    from CAD2BIM_backend.domain.control_plane import ArtifactRecord
    from CAD2BIM_backend.domain.data_runtime import (DataPackageRecord, DataPackageKind,
        DataPackageMemberRecord, PackageMemberKind, ArtifactVersionRef, ArrowColumnRecord,
        CatalogResourceKind, IfcProductsApplyOperationDetail, IFC_PRODUCTS_APPLY_INTENT_SUMMARY)
    from CAD2BIM_backend.domain.product_apply import ApplyIfcProductsCommand, ProductApplyInputBinding
    from CAD2BIM_backend.infrastructure.postgres.repository_artifact import register_artifact_in_transaction
    from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import append_catalog_entry, next_catalog_revision
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(TEST_DATABASE_URL, environ={
        "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD, "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
        "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD})
    runtime = _create_runtime(_runtime_database_url(TEST_DATABASE_URL), tmp_path / "storage")
    context, scope = await _open_direct_scope(runtime, configured_subject=f"product-report-{uuid4().hex}",
                                             session_name="Product report joint transaction")
    stack = build_in_process_ifc_stack(runtime.repository, runtime.storage)
    now = datetime.now(timezone.utc)
    package_id, member_id = f"data_package_{uuid4().hex}", f"package_member_{uuid4().hex}"
    table = pa.Table.from_pylist([{"product_key": "transaction-product", "origin_x_m": 0.,
        "origin_y_m": 0., "origin_z_m": 0., "parameters_json": "{}", "external_constraints_json": "[]"}],
        schema=pa.schema([pa.field("product_key", pa.string(), nullable=False),
            *[pa.field(name, pa.float64(), nullable=False) for name in ("origin_x_m", "origin_y_m", "origin_z_m")],
            pa.field("parameters_json", pa.string(), nullable=False), pa.field("external_constraints_json", pa.string(), nullable=False)]))
    sink = pa.BufferOutputStream(); pq.write_table(table, sink)
    contents = [("payloads/input/part-00000.parquet", PackageMemberKind.PAYLOAD,
                 "application/vnd.apache.parquet", sink.getvalue().to_pybytes()),
                ("description.md", PackageMemberKind.DESCRIPTION, "text/markdown", b"# One product transaction input\n"),
                ("manifest.json", PackageMemberKind.MANIFEST, "application/json",
                 json.dumps({"publication_state": "published", "package_id": package_id,
                             "package_kind": "derived", "payloads": ["input"]}).encode())]
    artifacts, members = [], []
    for name, kind, media, content in contents:
        artifact_id, version_id = f"artifact_{uuid4().hex}", f"artifact_version_{uuid4().hex}"
        stored = await runtime.storage.write(StorageKey(scope.owner_id, scope.task_id, artifact_id, 1),
            filename=name.rsplit("/", 1)[-1], content_type=media, chunks=_one_chunk(content))
        artifacts.append(ArtifactRecord(artifact_id=artifact_id, version_id=version_id,
            owner_id=scope.owner_id, task_id=scope.task_id, run_id=scope.run_id, kind="data_input_fixture",
            filename=stored.filename, content_type=media, size_bytes=stored.size_bytes, sha256=stored.sha256,
            idempotency_key=f"input-asset:{artifact_id}", created_at=now))
        members.append(DataPackageMemberRecord(member_id=member_id if kind is PackageMemberKind.PAYLOAD else f"package_member_{uuid4().hex}",
            owner_id=scope.owner_id, tenant_id=scope.tenant_id, task_id=scope.task_id, package_id=package_id,
            member_name=name, member_kind=kind, artifact=ArtifactVersionRef(artifact_id=artifact_id, version_id=version_id, sha256=stored.sha256),
            media_type=media, created_at=now, logical_payload_name="input" if kind is PackageMemberKind.PAYLOAD else None,
            row_count=1 if kind is PackageMemberKind.PAYLOAD else None,
            logical_key_fields=("product_key",) if kind is PackageMemberKind.PAYLOAD else (),
            arrow_schema=tuple(ArrowColumnRecord(name=f.name, type=str(f.type), nullable=f.nullable) for f in table.schema)
                if kind is PackageMemberKind.PAYLOAD else ()))
    package = DataPackageRecord(package_id=package_id, owner_id=scope.owner_id, tenant_id=scope.tenant_id,
        task_id=scope.task_id, created_by_run_id=scope.run_id, name="One product input", package_kind=DataPackageKind.DERIVED,
        description_title="One product input", summary="Typed persistence test input", section_titles=("Input",),
        idempotency_key=f"input-package:{package_id}", request_fingerprint=hashlib.sha256(b"input").hexdigest(), created_at=now)
    with bind_tenant_execution_context(context):
        async with runtime.database.session_factory() as session, session.begin():
            for artifact in artifacts:
                await register_artifact_in_transaction(session, artifact, tenant_id=scope.tenant_id,
                    request_value=artifact.model_dump(mode="json"), lineage={})
            session.add(runtime.repository._package_model(package)); await session.flush()
            session.add_all(runtime.repository._member_model(member) for member in members); await session.flush()
            await append_catalog_entry(session, owner_id=scope.owner_id, tenant_id=scope.tenant_id,
                task_id=scope.task_id, run_id=scope.run_id, kind=CatalogResourceKind.PACKAGE,
                catalog_revision=await next_catalog_revision(session, scope.run_id), package_id=package_id)
        await stack.model_service.initialize(scope, InitializeIfcModelCommand(project_name="Product report transaction base"))
        detail = IfcProductsApplyOperationDetail(request=ApplyIfcProductsCommand(
            family_version_id="family_version_transaction_fixture", package_id=package_id, payload_name="input"),
            input_binding=ProductApplyInputBinding(package_id=package_id, member_id=member_id,
                payload_name="input", logical_key_fields=("product_key",)))
        digest = hashlib.sha256(detail.model_dump_json().encode()).hexdigest()
        prepared = await runtime.repository.prepare_revision(scope, tool_name="ifc_products_apply",
            idempotency_key=f"product-transaction:{uuid4().hex}", request_digest=digest,
            intent_summary=IFC_PRODUCTS_APPLY_INTENT_SUMMARY, operation_detail=detail.persisted_payload())
        claim = await runtime.repository.claim_execution(scope, operation_id=prepared.operation_id, request_digest=digest)
    return runtime, context, scope, stack, detail, prepared, claim


async def _product_transaction_candidate(runtime, scope, stack, detail, prepared):
    from CAD2BIM_backend.application.product_apply_report import stage_product_apply_report, empty_report_rows, report_for_commit
    from CAD2BIM_backend.domain.product_apply import ProductApplySummary
    from CAD2BIM_backend.domain.ifc_product_compilation import IfcProductComponentBinding
    import ifcopenshell.guid
    content = await runtime.storage.read(_storage_key(scope, prepared.before.artifact.artifact_id))
    private = stack.codec.private_copy(content)
    guid = ifcopenshell.guid.new()
    private.model.create_entity("IfcPipeSegment", GlobalId=guid, Name="transaction-product")
    content = private.to_bytes(); inspection = stack.codec.inspect(content)
    generated = GeneratedIfcRevision(candidate=BytesIfcCandidate(content, sha256=inspection.sha256), inspection=inspection)
    bindings = (IfcProductComponentBinding(product_key="transaction-product", input_row_index=0,
        component_key="pipe", parent_component_key=None, global_id=guid),)
    rows = empty_report_rows()
    rows["products"] = [{"input_row_index": 0, "product_key": "transaction-product",
        "solve_status": "SOLVED", "build_status": "BUILT", "commit_status": "NOT_COMMITTED"}]
    rows["components"] = [{"product_key": "transaction-product", "component_key": "pipe",
        "ifc_class": "IfcPipeSegment", "project_world_transform_json": "{}",
        "build_status": "BUILT", "commit_status": "NOT_COMMITTED"}]
    reports = []
    for committed in (True, False):
        summary = ProductApplySummary(outcome="committed" if committed else "technical_failure",
            input_count=1, solve_executed_count=1, solved_count=1, interface_constraints_evaluated_product_count=1,
            build_executed_count=1, built_count=1, committed_count=1 if committed else 0, not_executed_count=0)
        reports.append(await stage_product_apply_report(runtime.storage, scope, prepared, detail=detail,
            summary=summary, rows=report_for_commit(rows, bindings, committed=committed)))
    return generated, guid, *reports


async def _product_transaction_visible(runtime, context, scope):
    with bind_tenant_execution_context(context):
        async with runtime.database.session_factory() as session:
            return dict((await session.execute(text("SELECT "
                "(SELECT current_revision_id FROM ifc_models WHERE run_id=:run) AS current, "
                "(SELECT count(*) FROM ifc_model_revisions WHERE run_id=:run) AS revisions, "
                "(SELECT count(*) FROM artifacts WHERE run_id=:run AND kind='product_apply_report') AS reports, "
                "(SELECT count(*) FROM data_packages WHERE created_by_run_id=:run) AS packages, "
                "(SELECT count(*) FROM data_catalog_entries WHERE run_id=:run AND resource_kind='package') AS catalog"),
                {"run": scope.run_id})).mappings().one())


async def _discover_product_report(runtime, context, scope, package_id):
    from CAD2BIM_backend.domain.invocation import InvocationScope
    from CAD2BIM_backend.domain.control_plane import ExecutionMode
    authority = InvocationScope(identity=context, owner_id=scope.owner_id, run_id=scope.run_id,
        execution_mode=ExecutionMode.DIRECT_MCP, catalog_frontier=None)
    report = await runtime.repository.get_package(authority, package_id)
    assert report is not None and report.package.package_id == package_id
    assert {m.logical_payload_name for m in report.members if m.logical_payload_name} == {
        "products", "components", "interfaces", "checks"}
    return report


@pytest.mark.asyncio
async def test_product_apply_recovery_discovery_pages_unpublished_states_with_rls(tmp_path):
    """Actual original rows, Catalog absence and keyset ties; reads never settle writes."""
    from datetime import datetime, timezone
    from CAD2BIM_backend.application.data_runtime_service import DataRuntimeService
    from CAD2BIM_backend.domain.control_plane import ExecutionMode
    from CAD2BIM_backend.domain.data_runtime import IFC_PRODUCTS_APPLY_INTENT_SUMMARY
    from CAD2BIM_backend.domain.invocation import InvocationScope

    runtime, context, scope, stack, detail, prepared, claim = await _product_transaction_fixture(tmp_path)
    admin_engine = create_async_engine(TEST_DATABASE_URL)
    generated = None
    try:
        authority = InvocationScope(identity=context, owner_id=scope.owner_id, run_id=scope.run_id,
            execution_mode=ExecutionMode.DIRECT_MCP, catalog_frontier=None)
        service = DataRuntimeService(runtime.repository, None, ifc_repository=runtime.repository)
        with bind_tenant_execution_context(context):
            generated, _, report, failed_report = await _product_transaction_candidate(
                runtime, scope, stack, detail, prepared)
            committed = await stack.writer.commit(scope, prepared, generated, claim=claim,
                product_report=report, product_failure_report=failed_report)
            await runtime.repository.retire_execution(scope, claim)
            originals = {"succeeded": prepared}
            for status in ("failed", "prepared", "indeterminate"):
                item = await stack.writer.prepare(scope, tool_name="ifc_products_apply",
                    idempotency_key=f"recovery-discovery-{status}:{uuid4().hex}",
                    request_digest=prepared.request_digest, intent_summary=IFC_PRODUCTS_APPLY_INTENT_SUMMARY,
                    operation_detail=detail.persisted_payload())
                originals[status] = item
                if status == "prepared":
                    continue
                item_claim = await runtime.repository.claim_execution(scope,
                    operation_id=item.operation_id, request_digest=item.request_digest)
                if status == "failed":
                    await stack.writer.fail_prepared(scope, item, claim=item_claim,
                        error_code="PRODUCT_PRECHECK_REJECTED", error_message="Known rejected original input")
                else:
                    await stack.writer.freeze_indeterminate(scope, item, claim=item_claim,
                        reason_code="PRODUCT_APPLY_COMMIT_UNCERTAIN")
                await runtime.repository.retire_execution(scope, item_claim)
            # Equal creation times exercise the operation-id tie breaker rather
            # than relying on timing gaps between database transactions.
            boundary = datetime.now(timezone.utc)
            async with admin_engine.begin() as connection:
                await connection.execute(text("UPDATE critical_operations SET created_at=:created "
                    "WHERE run_id=:run AND tool_name='ifc_products_apply'"),
                    {"created": boundary, "run": scope.run_id})
            before = await _product_transaction_visible(runtime, context, scope)
            first = await service.list_ifc_products_recovery_operations(authority, tool_name="ifc_products_apply", limit=2)
            second = await service.list_ifc_products_recovery_operations(authority, tool_name="ifc_products_apply",
                before_created_at=first[-1].created_at,
                before_operation_id=first[-1].operation.operation_id, limit=2)
            last = await service.list_ifc_products_recovery_operations(authority, tool_name="ifc_products_apply",
                before_created_at=second[-1].created_at,
                before_operation_id=second[-1].operation.operation_id, limit=2)
            found = first + second
            assert len(first) == len(second) == 2 and last == ()
            assert [item.operation.operation_id for item in found] == sorted(
                (item.operation_id for item in originals.values()), reverse=True)
            assert {item.operation.status.value for item in found} == set(originals)
            for item in found:
                readback = await service.get_operation_by_locator(authority,
                    origin_run_id=scope.run_id, operation_id=item.operation.operation_id)
                assert readback.operation.operation.status == item.operation.status
                assert readback.input_packages["input"].package.package_id == detail.input_binding.package_id
                if item.operation.status.value in {"prepared", "indeterminate"}:
                    assert item.catalog_entry is None and readback.operation.catalog_entry is None
                    assert readback.result_package is None
                else:
                    assert readback.operation.catalog_entry == item.catalog_entry
                    assert item.catalog_entry.alias.startswith("op_")
            assert next(item for item in found if item.operation.status.value == "succeeded").catalog_entry.alias == committed.operation_alias
            assert await _product_transaction_visible(runtime, context, scope) == before
            assert (await runtime.repository.get_current(scope)).revision_id == committed.exact.revision_id
            # Frozen uncertainty is allowed through the read-only permission
            # check; the original Writer still owns reconciliation and claims.
            await stack.writer.require_recovery_allowed(scope)
        other_context, other_scope = await _open_direct_scope(runtime,
            configured_subject=f"recovery-other-{uuid4().hex}", session_name="Other recovery tenant")
        other_authority = InvocationScope(identity=other_context, owner_id=other_scope.owner_id,
            run_id=other_scope.run_id, execution_mode=ExecutionMode.DIRECT_MCP, catalog_frontier=None)
        with bind_tenant_execution_context(other_context):
            assert await service.list_ifc_products_recovery_operations(other_authority, tool_name="ifc_products_apply") == ()
            assert await runtime.repository.get_ifc_products_recovery_operation(other_authority,
                origin_run_id=scope.run_id, operation_id=prepared.operation_id) is None
            async with runtime.database.session_factory() as session:
                role = (await session.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
                assert role == (False, False)
                assert await session.scalar(text("SELECT count(*) FROM critical_operations WHERE run_id=:run"),
                    {"run": scope.run_id}) == 0
    finally:
        if generated is not None:
            await generated.candidate.aclose()
        await admin_engine.dispose()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_selected,lose_response",[(False,False),(True,False),(False,True)],
                         ids=["new-commit","selected-resume","commit-response-loss"])
async def test_product_committed_actual_readback_failure_freezes_exact_and_replay_stays_failed(
    tmp_path, monkeypatch, resume_selected, lose_response,
):
    from CAD2BIM_backend.domain.ifc_model import IfcCommittedVerificationError
    from CAD2BIM_backend.domain.data_runtime import IFC_PRODUCTS_APPLY_INTENT_SUMMARY
    runtime,context,scope,stack,detail,prepared,claim=await _product_transaction_fixture(tmp_path)
    generated=None
    try:
        generated,guid,report,failure=await _product_transaction_candidate(runtime,scope,stack,detail,prepared)
        original=runtime.repository.commit_revision
        payload=runtime.storage._logical_directory(_storage_key(scope,prepared.artifact_id))/"payload"
        saved_bytes=None
        async def corrupt_after_actual_commit(*args,**kwargs):
            nonlocal saved_bytes
            committed=await original(*args,**kwargs)
            if saved_bytes is None:
                saved_bytes=payload.read_bytes()
                # Real stored byte loss, after the PostgreSQL transaction actually succeeded.
                payload.write_bytes(b"corrupt exact IFC after committed transaction")
                if lose_response: raise RuntimeError("lost successful commit response with real storage corruption")
            return committed
        monkeypatch.setattr(runtime.repository,"commit_revision",corrupt_after_actual_commit)
        with bind_tenant_execution_context(context):
            if resume_selected:
                await stack.writer._stage_and_select(scope,prepared,claim,generated,
                    product_report=report,product_failure_report=failure)
                await runtime.repository.retire_execution(scope,claim)
                claim=await runtime.repository.claim_execution(scope,operation_id=prepared.operation_id,
                                                              request_digest=prepared.request_digest)
                operation=stack.writer.resume_selected_revision(scope,prepared,claim)
            else:
                operation=stack.writer.commit(scope,prepared,generated,claim=claim,
                    product_report=report,product_failure_report=failure)
            with pytest.raises(IfcCommittedVerificationError) as first:
                await operation
            assert first.value.revision_id==prepared.after_revision_id
            assert first.value.operation_alias is not None and first.value.__cause__ is not None
            current=await runtime.repository.get_current(scope)
            assert current.revision_id==prepared.after_revision_id and current.model_state.value=="frozen"
            before=await _product_transaction_visible(runtime,context,scope)
            state=await runtime.repository.get_revision_operation_state(scope,tool_name="ifc_products_apply",
                idempotency_key=prepared.idempotency_key,request_digest=prepared.request_digest,
                intent_summary=IFC_PRODUCTS_APPLY_INTENT_SUMMARY,operation_detail=detail.persisted_payload())
            assert state.status.value=="succeeded" and state.error_code is None
            assert state.report_package_id==report.package.package_id and state.product_summary==report.summary
            await _discover_product_report(runtime,context,scope,state.report_package_id)
            replay_args=dict(tool_name="ifc_products_apply",idempotency_key=prepared.idempotency_key,
                request_digest=prepared.request_digest,intent_summary=IFC_PRODUCTS_APPLY_INTENT_SUMMARY,
                operation_detail=detail.persisted_payload())
            with pytest.raises(IfcCommittedVerificationError) as replay:
                await stack.writer.recover_revision(scope,**replay_args)
            assert replay.value.operation_alias==first.value.operation_alias
            assert replay.value.revision_id==prepared.after_revision_id
            assert await _product_transaction_visible(runtime,context,scope)==before
            # Restore only the owned damaged test fixture; recovery still uses the same exact revision/report.
            payload.write_bytes(saved_bytes)
            recovered=await stack.writer.recover_revision(scope,**replay_args)
            assert recovered.status.value=="succeeded" and recovered.report_package_id==report.package.package_id
            assert recovered.operation_alias==first.value.operation_alias
            assert await _product_transaction_visible(runtime,context,scope)==before
            await runtime.repository.retire_execution(scope,claim)
    finally:
        if generated is not None: await generated.candidate.aclose()
        await runtime.close()


@pytest.mark.asyncio
async def test_product_report_registry_failure_rolls_back_ifc_and_is_invisible_to_other_connection(tmp_path, monkeypatch):
    runtime, context, scope, stack, detail, prepared, claim = await _product_transaction_fixture(tmp_path)
    observer = _create_runtime(_runtime_database_url(TEST_DATABASE_URL), tmp_path / "observer-storage")
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.repository._register_product_report
    async def registry_fault(session, owner_scope, operation, report):
        await original(session, owner_scope, operation, report)
        entered.set(); await release.wait()
        raise RuntimeError("injected report registry failure after real inserts")
    monkeypatch.setattr(runtime.repository, "_register_product_report", registry_fault)
    try:
        baseline = await _product_transaction_visible(observer, context, scope)
        generated, guid, report, failure = await _product_transaction_candidate(runtime, scope, stack, detail, prepared)
        with bind_tenant_execution_context(context):
            selected, staged = await stack.writer._stage_and_select(scope, prepared, claim, generated,
                product_report=report, product_failure_report=failure)
            publication = await stack.writer._publish_selected(scope, prepared, selected, staged=staged)
            task = asyncio.create_task(runtime.repository.commit_revision(scope, prepared=prepared, publication=publication, claim=claim))
            await asyncio.wait_for(entered.wait(), timeout=10)
            assert await _product_transaction_visible(observer, context, scope) == baseline
            release.set()
            with pytest.raises(RuntimeError, match="injected report registry failure"):
                await task
            assert await _product_transaction_visible(observer, context, scope) == baseline
            assert await runtime.repository.get_exact(scope, revision_id=prepared.after_revision_id) is None
            await runtime.repository.retire_execution(scope, claim)
        await generated.candidate.aclose()
    finally:
        release.set()
        await observer.close(); await runtime.close()


@pytest.mark.asyncio
async def test_product_selected_restart_response_loss_and_failed_replay_do_not_duplicate_reports(tmp_path):
    from CAD2BIM_backend.domain.data_runtime import IFC_PRODUCTS_APPLY_INTENT_SUMMARY
    runtime, context, scope, stack, detail, prepared, claim = await _product_transaction_fixture(tmp_path)
    try:
        baseline = await _product_transaction_visible(runtime, context, scope)
        generated, guid, report, failure = await _product_transaction_candidate(runtime, scope, stack, detail, prepared)
        with bind_tenant_execution_context(context):
            selected, _ = await stack.writer._stage_and_select(scope, prepared, claim, generated,
                product_report=report, product_failure_report=failure)
            await runtime.repository.retire_execution(scope, claim)
        await generated.candidate.aclose()
        await runtime.close()
        runtime = _create_runtime(_runtime_database_url(TEST_DATABASE_URL), tmp_path / "storage")
        stack = build_in_process_ifc_stack(runtime.repository, runtime.storage)
        original = runtime.repository.commit_revision
        calls = []
        async def lose_commit_response(*args, **kwargs):
            result = await original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("injected committed product response loss")
            return result
        runtime.repository.commit_revision = lose_commit_response
        with bind_tenant_execution_context(context):
            next_claim = await runtime.repository.claim_execution(scope, operation_id=prepared.operation_id, request_digest=prepared.request_digest)
            resumed, recovered = await stack.writer.resume_selected_revision(scope, prepared, next_claim)
            assert recovered == selected and recovered.product_report.package.package_id == report.package.package_id
            saved = stack.codec.private_copy(await runtime.storage.read(_storage_key(scope, prepared.artifact_id))).model
            assert saved.by_guid(guid).Name == "transaction-product"
            visible = await _product_transaction_visible(runtime, context, scope)
            assert visible["reports"] == len(report.artifacts)
            assert visible["packages"] == baseline["packages"] + 1
            state = await stack.writer.recover_revision(scope, tool_name="ifc_products_apply",
                idempotency_key=prepared.idempotency_key, request_digest=prepared.request_digest,
                intent_summary=IFC_PRODUCTS_APPLY_INTENT_SUMMARY, operation_detail=detail.persisted_payload())
            assert state.report_package_id == report.package.package_id and state.product_summary == report.summary
            discovered = await _discover_product_report(runtime, context, scope, state.report_package_id)
            assert discovered.package == report.package
            assert await _product_transaction_visible(runtime, context, scope) == visible
            await runtime.repository.retire_execution(scope, next_claim)
            rejected = await runtime.repository.prepare_revision(scope, tool_name="ifc_products_apply",
                idempotency_key=f"rejected:{uuid4().hex}", request_digest=prepared.request_digest,
                intent_summary=IFC_PRODUCTS_APPLY_INTENT_SUMMARY, operation_detail=detail.persisted_payload())
            failed_claim = await runtime.repository.claim_execution(scope, operation_id=rejected.operation_id, request_digest=rejected.request_digest)
            rejected_candidate, _, _, rejected_report = await _product_transaction_candidate(runtime, scope, stack, detail, rejected)
            await rejected_candidate.candidate.aclose()
            await stack.writer.fail_prepared(scope, rejected, claim=failed_claim,
                error_code="PRODUCT_TECHNICAL_REJECTED", error_message="known original rejection", product_report=rejected_report)
            after_failure = await _product_transaction_visible(runtime, context, scope)
            replay = await stack.writer.recover_revision(scope, tool_name="ifc_products_apply",
                idempotency_key=rejected.idempotency_key, request_digest=rejected.request_digest,
                intent_summary=IFC_PRODUCTS_APPLY_INTENT_SUMMARY, operation_detail=detail.persisted_payload())
            assert replay.status.value == "failed" and replay.report_package_id == rejected_report.package.package_id
            await _discover_product_report(runtime, context, scope, replay.report_package_id)
            assert await _product_transaction_visible(runtime, context, scope) == after_failure
            await runtime.repository.retire_execution(scope, failed_claim)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_product_cas_loser_publishes_only_failed_report_and_other_tenant_sees_nothing(tmp_path):
    import io
    import pyarrow.parquet as pq
    runtime, context, scope, stack, detail, prepared, claim = await _product_transaction_fixture(tmp_path)
    try:
        baseline = await _product_transaction_visible(runtime, context, scope)
        generated, guid, report, failure = await _product_transaction_candidate(runtime, scope, stack, detail, prepared)
        with bind_tenant_execution_context(context):
            selected, staged = await stack.writer._stage_and_select(scope, prepared, claim, generated,
                product_report=report, product_failure_report=failure)
            publication = await stack.writer._publish_selected(scope, prepared, selected, staged=staged)
            await runtime.repository.retire_execution(scope, claim)
            from CAD2BIM_backend.application.ifc_project_service import IfcProjectService
            winner = await IfcProjectService(stack.writer, stack.runtime, stack.coordinator).update(
                scope, UpdateIfcProjectCommand(patch=IfcProjectPatch(name="CAS winner")),
                idempotency_key=f"cas-winner:{uuid4().hex}")
            next_claim = await runtime.repository.claim_execution(scope, operation_id=prepared.operation_id, request_digest=prepared.request_digest)
            with pytest.raises(IfcModelConflictError):
                await runtime.repository.commit_revision(scope, prepared=prepared, publication=publication, claim=next_claim)
            state = await runtime.repository.get_revision_operation_state(scope, tool_name="ifc_products_apply",
                idempotency_key=prepared.idempotency_key, request_digest=prepared.request_digest,
                intent_summary="Add all products from one exact Family and Parquet input atomically.",
                operation_detail=detail.persisted_payload())
            assert state.status.value == "failed" and state.error_code == "IFC_CURRENT_CONFLICT"
            assert state.report_package_id == failure.package.package_id
            await _discover_product_report(runtime, context, scope, state.report_package_id)
            assert (await runtime.repository.get_current(scope)).revision_id != prepared.after_revision_id
            assert await runtime.repository.get_exact(scope, revision_id=prepared.after_revision_id) is None
            visible = await _product_transaction_visible(runtime, context, scope)
            assert visible["reports"] == len(failure.artifacts)
            assert visible["packages"] == baseline["packages"] + 1
            # The real new DB constraint rejects a committed summary on a
            # failed, no-IFC Operation even when bypassing Python DTO parsing.
            with pytest.raises(IntegrityError):
                async with runtime.database.session_factory() as session, session.begin():
                    await session.execute(text("UPDATE critical_operations SET operation_detail_json="
                        "jsonb_set(operation_detail_json,'{result,outcome}',to_jsonb(CAST('committed' AS text))) "
                        "WHERE operation_id=:id"), {"id": prepared.operation_id})
            assert await _product_transaction_visible(runtime, context, scope) == visible
            for name, guid_field in (("products", "root_global_id"), ("components", "global_id")):
                member = next(m for m in failure.members if m.logical_payload_name == name)
                table = pq.read_table(io.BytesIO(await runtime.storage.read(StorageKey(scope.owner_id, scope.task_id, member.artifact.artifact_id, 1))))
                assert set(table.column("commit_status").to_pylist()) == {"NOT_COMMITTED"}
                assert table.column(guid_field).to_pylist() == [None]
            await runtime.repository.retire_execution(scope, next_claim)
        other_context, other_scope = await _open_direct_scope(runtime, configured_subject=f"other-report-{uuid4().hex}", session_name="Other report tenant")
        with bind_tenant_execution_context(other_context):
            async with runtime.database.session_factory() as session:
                role = (await session.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
                assert role == (False, False)
                assert await session.scalar(text("SELECT count(*) FROM data_packages WHERE package_id=:id"), {"id": failure.package.package_id}) == 0
                assert await session.scalar(text("SELECT count(*) FROM artifact_versions WHERE version_id=:id"), {"id": failure.artifacts[0].version_id}) == 0
        await generated.candidate.aclose()
    finally:
        await runtime.close()
