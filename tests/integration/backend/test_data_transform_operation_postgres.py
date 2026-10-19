"""One real SQL Worker and PostgreSQL Operation lifecycle across repository reopen."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import multiprocessing
import os
import sys
from time import monotonic
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.data_query_service import DataQueryApplicationError, DataQueryService
from CAD2BIM_backend.application.data_runtime_service import (
    DataRuntimeService,
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.application.data_transform_service import (
    DataTransformApplicationError,
    DataTransformService,
)
from CAD2BIM_backend.application.exact_payload_resolver import ExactPayloadResolver
from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationErrorPhase,
    CriticalOperationStatus,
    DataTransformOperationState,
    ValidatedSource,
)
from CAD2BIM_backend.domain.data_transform import DataRepairTransformCommand, DataTransformCommand
from CAD2BIM_backend.domain.text_source import TextSourceView
from CAD2BIM_backend.infrastructure.source_text.source_patch_adapter import TextSourcePatchAdapter
from CAD2BIM_backend.domain.data_query import DataDescribePackageQuery
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.data_transform import SubprocessDuckDBDataTransformWorker
from CAD2BIM_backend.infrastructure.data_transform.executor_identity import (
    current_data_transform_executor_owner,
)
from CAD2BIM_backend.infrastructure.postgres.database import Database
from CAD2BIM_backend.infrastructure.postgres.identity_directory import PostgresIdentityDirectory
from CAD2BIM_backend.infrastructure.postgres.repository import PostgresControlPlaneRepository
from CAD2BIM_backend.infrastructure.postgres.runtime_roles import provision_runtime_login_roles
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantAwareAsyncSession,
    bind_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage


pytestmark = [pytest.mark.integration, pytest.mark.network]
_TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL")
_SOURCE = Path(__file__).resolve().parents[2] / "fixtures" / "cad" / "minimal_line.dxf"


def _runtime_url(url: str) -> str:
    return make_url(url).set(
        drivername="postgresql+psycopg",
        username="cad2bim_mcp_login",
        password="phase1-mcp-password",
    ).render_as_string(hide_password=False)


def _runtime(url: str, root: Path, subject: str):
    engine = create_async_engine(url, pool_pre_ping=True)
    sessions = async_sessionmaker(
        engine, class_=TenantAwareAsyncSession, expire_on_commit=False
    )
    database = Database(engine=engine, session_factory=sessions)
    repository = PostgresControlPlaneRepository(sessions)
    identity = PostgresIdentityDirectory(sessions)
    artifacts = ArtifactService(repository, LocalStorage(root / "storage"))
    direct_sessions = DirectSessionService(
        repository, identity, subject, bind_tenant_execution_context
    )
    resolver = DirectScopeResolver(
        repository, identity, subject, bind_tenant_execution_context
    )
    worker = SubprocessDuckDBDataTransformWorker(
        python_executable=sys.executable,
        capacity_root=root / "capacity",
    )
    service = DataTransformService(
        repository, artifacts, ExactPayloadResolver(repository, artifacts), worker,
        source_patch=TextSourcePatchAdapter(),
    )
    return database, repository, direct_sessions, resolver, artifacts, worker, service


def _difference_paths(left: Any, right: Any, path: str = "") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        return [
            child
            for key in left.keys() | right.keys()
            for child in _difference_paths(left.get(key), right.get(key), f"{path}.{key}")
        ]
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [path]
        return [
            child
            for index, (first, second) in enumerate(zip(left, right))
            for child in _difference_paths(first, second, f"{path}[{index}]")
        ]
    return [path] if left != right else []


def _publication_snapshot(result: Any) -> dict[str, Any]:
    snapshot = result.model_dump(mode="json")
    snapshot["package"]["members"].sort(key=lambda item: item["member_name"])
    return snapshot


class _ExitAfterCandidate:
    def __init__(self, repository: PostgresControlPlaneRepository) -> None:
        self._repository = repository

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repository, name)

    async def stage_data_transform_candidate(self, *args: Any, **kwargs: Any) -> Any:
        await self._repository.stage_data_transform_candidate(*args, **kwargs)
        os._exit(73)


def _stage_candidate_in_other_pid(
    url: str, root: Path, subject: str, direct_alias: str, command_json: dict[str, Any]
) -> None:
    async def execute() -> None:
        database, repository, _sessions, resolver, artifacts, worker, _service = _runtime(
            url, root, subject
        )
        try:
            scope = await resolver.resolve(direct_alias, command_id=f"command_{uuid4().hex}")
            service = DataTransformService(
                _ExitAfterCandidate(repository),
                artifacts,
                ExactPayloadResolver(repository, artifacts),
                worker,
            )
            with bind_tenant_execution_context(scope.identity):
                await service.transform(
                    scope,
                    DataTransformCommand.model_validate(command_json),
                    request_identity="runner:b2:K3",
                )
        finally:
            await worker.close()
            await database.dispose()

    asyncio.run(execute())


def _claim_without_candidate_in_other_pid(
    url: str, root: Path, subject: str, direct_alias: str, operation_id: str
) -> None:
    async def execute() -> None:
        database, repository, _sessions, resolver, _artifacts, worker, _service = _runtime(
            url, root, subject
        )
        try:
            scope = await resolver.resolve(direct_alias, command_id=f"command_{uuid4().hex}")
            with bind_tenant_execution_context(scope.identity):
                claim = await repository.claim_data_transform_execution(
                    scope,
                    operation_id,
                    owner=current_data_transform_executor_owner(),
                    lease_seconds=30,
                )
                assert claim is not None
                os._exit(74)
        finally:
            await worker.close()
            await database.dispose()

    asyncio.run(execute())


async def _prepare_deadline_probe(
    repository: PostgresControlPlaneRepository,
    scope: Any,
    source: DataTransformOperationState,
    *,
    request_identity: str,
    deadline_at: datetime,
) -> DataTransformOperationState:
    nonce = uuid4().hex
    prepared = source.operation.model_copy(update={
        "operation_id": f"op_{nonce}",
        "idempotency_key": f"b2-deadline:{nonce}",
        "request_fingerprint": sha256(nonce.encode()).hexdigest(),
        "status": CriticalOperationStatus.PREPARED,
        "result_package_id": None,
        "started_at": deadline_at - timedelta(minutes=1),
        "finished_at": None,
    })
    return await repository.prepare_data_transform_operation(
        scope,
        prepared,
        request_identity=request_identity,
        request_digest=source.request_digest,
        request_payload=source.request_payload,
        resolved_command=source.resolved_command,
        resolved_parent_operation_alias=source.resolved_parent_operation_alias,
        deadline_at=deadline_at,
    )


def _deadline_failure(state: DataTransformOperationState):
    return state.operation.model_copy(update={
        "status": CriticalOperationStatus.FAILED,
        "error_code": "TRANSFORM_TIMEOUT",
        "error_message": "The original execution budget expired",
        "error_phase": CriticalOperationErrorPhase.EXECUTION,
        "finished_at": datetime.now(timezone.utc),
    })


class _ControlledIfcDeriveWorker:
    """Advance real CURRENT at a deterministic boundary around a real child call."""

    def __init__(self, worker):
        self.worker = worker
        self.calls = 0
        self.before = None
        self.after = None

    async def execute_transform(self, *args, **kwargs):
        self.calls += 1
        before, self.before = self.before, None
        if before is not None:
            await before()
        result = await self.worker.execute_transform(*args, **kwargs)
        after, self.after = self.after, None
        if after is not None:
            await after()
        return result


class _LoseDerivePublicationResponse:
    def __init__(self, repository):
        self.repository = repository
        self.armed = True
        self.losses = 0

    def __getattr__(self, name):
        return getattr(self.repository, name)

    async def publish_package_result(self, *args, **kwargs):
        result = await self.repository.publish_package_result(*args, **kwargs)
        if self.armed:
            self.armed = False
            self.losses += 1
            raise RuntimeError("simulated loss after committed Data publication")
        return result


def _ifc_derivation_service(runtime, root, *, repository=None):
    """Reuse the IFC test stack, real query Worker, and original Data owners."""
    from tests.process_support.ifc_revision_runtime import build_in_process_ifc_stack

    artifacts = ArtifactService(runtime.repository, runtime.storage)
    stack = build_in_process_ifc_stack(runtime.repository, runtime.storage)
    worker = _ControlledIfcDeriveWorker(runtime.worker._delegate)
    sql_worker = SubprocessDuckDBDataTransformWorker(
        python_executable=sys.executable, capacity_root=root / "sql-capacity",
    )
    publication = _LoseDerivePublicationResponse(repository or runtime.repository)
    service = DataTransformService(
        publication, artifacts, ExactPayloadResolver(runtime.repository, artifacts), sql_worker,
        source_patch=TextSourcePatchAdapter(), ifc_revisions=stack.reader, ifc_worker=worker,
    )
    return service, artifacts, worker, sql_worker, publication, stack.reader


def _stage_ifc_repair_candidate_in_other_pid(url, root, subject, alias, repair_json):
    async def execute():
        from tests.integration.backend.test_ifc_query_postgres import _create_runtime
        runtime = _create_runtime(url, root / "storage", root / "staging-capacity", subject=subject)
        service, artifacts, _, sql_worker, _, _ = _ifc_derivation_service(
            runtime, root / "staging", repository=_ExitAfterCandidate(runtime.repository))
        try:
            scope = await runtime.resolver.resolve(alias, command_id=uuid4().hex)
            with bind_tenant_execution_context(scope.identity):
                repair = DataRepairTransformCommand.model_validate(repair_json)
                parent = await runtime.repository.get_operation(scope, repair.parent_operation_id)
                packages = (await DataRuntimeService(runtime.repository, artifacts).list_package_catalog(scope)).packages
                await service.repair_transform(scope, parent,
                    {package.catalog_entry.alias: package for package in packages}, repair,
                    request_identity="ifc:interrupted-repair", request_payload={"mode": "repair", "request": repair_json},
                    repair_parent_operation_alias=parent.catalog_entry.alias)
        finally:
            await sql_worker.close()
            await runtime.close()
    asyncio.run(execute())


async def test_ifc_derivation_exact_sources_atomic_repair_replay_and_scope(tmp_path: Path) -> None:
    """Real IFC + two Packages through the original Application/PG/Worker owners."""
    import json
    from sqlalchemy import select
    from CAD2BIM_backend.application.data_json_service import DataJsonService
    from CAD2BIM_backend.domain.data_json import JsonReadRequest
    from CAD2BIM_backend.domain.data_query import DataQueryRowsRequest, DataRowsQuery
    from CAD2BIM_backend.domain.data_runtime import PackageMemberKind
    from CAD2BIM_backend.domain.ifc_project import IfcProjectPatch, UpdateIfcProjectCommand
    from CAD2BIM_backend.infrastructure.postgres.models import CriticalOperationModel
    from tests.integration.backend.test_ifc_query_postgres import (
        _create_runtime, _ifc_scope, _open_scope,
    )

    if not _TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(_TEST_DATABASE_URL, environ=os.environ)
    url = make_url(_TEST_DATABASE_URL).set(drivername="postgresql+psycopg",
        username="cad2bim_mcp_login", password=os.environ["CAD2BIM_MCP_POSTGRES_PASSWORD"]
    ).render_as_string(hide_password=False)
    subject = f"derive-ifc-{uuid4().hex}"
    runtime = _create_runtime(url, tmp_path / "storage", tmp_path / "capacity", subject=subject)
    service, artifacts, worker, sql_worker, loss, reader = _ifc_derivation_service(runtime, tmp_path)
    restarted = other = None
    restarted_sql = None
    description = ("# IFC and CAD selection\n## Summary\nSelected project with two CAD counts.\n"
        "## Intended use\nVerify exact read sources.\n## Contents\nProject and counts.\n"
        "## Method\nRead private IFC and two uploaded Packages.\n"
        "## Scope and completeness\nSmall representative fixture.\n"
        "## Known limitations\nNo product geometry.\n")
    try:
        await runtime.database.verify_ready()
        alias, scope = await _open_scope(runtime, "IFC derivation persistence")
        await runtime.facade.initialize_ifc_model(alias, "Source A")
        with bind_tenant_execution_context(scope.identity):
            source_a = await reader.pin_current(_ifc_scope(scope))
            application = DataRuntimeService(runtime.repository, artifacts)
            packages = []
            for name in ("left", "right"):
                imported = await application.import_cad_source(scope, ValidatedSource(
                    filename=_SOURCE.name, media_type="application/dxf", content=_SOURCE.read_bytes(),
                    source_identity=f"{subject}:{name}",
                ))
                packages.append(imported.published.package)
            catalog = {package.catalog_entry.alias: package for package in packages}
            code = ("import pyarrow as pa\n"
                "def transform(ctx):\n"
                "    project = ctx.model().by_type('IfcProject')[0]\n"
                "    total = ctx.table('left_rows').num_rows + ctx.table('right_rows').num_rows\n"
                "    ctx.write_table('selected', pa.Table.from_pylist([{'global_id': project.GlobalId, 'project_name': project.Name, 'cad_rows': total}]))\n"
                "    ctx.write_json('summary', {'project_name': project.Name, 'cad_rows': total})\n")
            command = DataTransformCommand.model_validate({
                "ifc_source": "current",
                "inputs": [{"input_name": f"{name}_rows", "package_alias": package.catalog_entry.alias,
                    "payload_name": "entities"} for name, package in zip(("left", "right"), packages)],
                "execution": {"engine": "python_arrow", "code": code},
                "outputs": [
                    {"payload_name": "selected", "format": "parquet", "description": "Project and joined CAD counts", "logical_key_fields": ["global_id"]},
                    {"payload_name": "summary", "format": "json", "description": "Source summary", "json_kind": "document",
                     "schema_json": json.dumps({"type": "object", "properties": {"project_name": {"type": "string"}, "cad_rows": {"type": "integer"}}, "required": ["project_name", "cad_rows"], "additionalProperties": False})},
                ],
                "output_package": {"name": "IFC selection", "description_markdown": description},
            })
            original = await service.transform(scope, command, request_identity="ifc:original")
            assert loss.losses == 1 and worker.calls == 1
            published = original.published.package
            operation = original.published.operation
            assert operation.operation.detail.ifc_source.revision_id == source_a.revision_id
            manifest_member = next(member for member in published.members if member.member_kind is PackageMemberKind.MANIFEST)
            manifest = json.loads((await artifacts.download(scope.owner_id, manifest_member.artifact.artifact_id)).content)
            assert manifest["transformation"]["ifc_source"] == operation.operation.detail.ifc_source.model_dump(mode="json")
            queries = DataQueryService(runtime.repository, artifacts, CursorCodec("A" * 43))
            rows = await queries.query_rows(scope, DataQueryRowsRequest(query=DataRowsQuery(
                package_alias=published.catalog_entry.alias, payload_name="selected",
                select=("global_id", "project_name", "cad_rows"),
            )))
            assert len(rows.rows) == 1
            assert rows.rows[0].values["project_name"] == "Source A"
            json_result = await DataJsonService(ExactPayloadResolver(runtime.repository, artifacts), artifacts,
                CursorCodec("A" * 43)).read_json(scope, JsonReadRequest.model_validate({"query": {
                    "mode": "value", "package_alias": published.catalog_entry.alias,
                    "payload_name": "summary", "pointer": "",
                }}))
            assert json.loads(json_result.value_json) == {"project_name": "Source A", "cad_rows": 2}

            repair = DataRepairTransformCommand.model_validate({"parent_operation_id": operation.operation.operation_id,
                "edits": [{"old_text": "def transform(ctx):", "new_text": "def transform(ctx):  # reviewed"}]})
            repair_payload = {"mode": "repair", "request": repair.model_dump(mode="json", exclude_none=True)}
            async def repair_original(identity):
                return await service.repair_transform(scope, operation, catalog, repair,
                    request_identity=identity, request_payload=repair_payload,
                    repair_parent_operation_alias=operation.catalog_entry.alias)
            repaired = await repair_original("ifc:same-a")
            assert repaired.published.package.package.package_id != published.package.package_id
            assert repaired.published.operation.operation.detail.ifc_source == operation.operation.detail.ifc_source

            async def advance(name):
                await runtime.facade._require_ifc_project_service().update(_ifc_scope(scope),
                    UpdateIfcProjectCommand(patch=IfcProjectPatch(name=name)), idempotency_key=uuid4().hex)
            worker.before = lambda: advance("Source B")
            moved_create = await service.transform(scope, command, request_identity="ifc:create-pinned-a")
            assert moved_create.published.operation.operation.detail.ifc_source.revision_id == source_a.revision_id
            moved_rows = await queries.query_rows(scope, DataQueryRowsRequest(query=DataRowsQuery(
                package_alias=moved_create.published.package.catalog_entry.alias, payload_name="selected",
                select=("project_name",),
            )))
            assert moved_rows.rows[0].values["project_name"] == "Source A"
            before_count = len((await application.list_package_catalog(scope)).packages)
            calls = worker.calls
            with pytest.raises(DataTransformApplicationError) as changed:
                await repair_original("ifc:stale-a")
            assert changed.value.code == "IFC_SOURCE_CHANGED"
            assert changed.value.diagnostic["expected_revision_no"] == source_a.revision_no
            assert changed.value.diagnostic["actual_revision_no"] == source_a.revision_no + 1
            assert worker.calls == calls
            assert len((await application.list_package_catalog(scope)).packages) == before_count
            assert (await service.transform(scope, command, request_identity="ifc:original")).published.package.package.package_id == published.package.package_id
            assert worker.calls == calls

            current_b = await service.transform(scope, command, request_identity="ifc:current-b")
            parent_b = current_b.published.operation
            assert parent_b.operation.detail.ifc_source.revision_no == source_a.revision_no + 1
            repair_b = DataRepairTransformCommand.model_validate({"parent_operation_id": parent_b.operation.operation_id,
                "edits": [{"old_text": "def transform(ctx):", "new_text": "def transform(ctx):  # reviewed B"}]})
            worker.after = lambda: advance("Source C")
            before_count = len((await application.list_package_catalog(scope)).packages)
            with pytest.raises(DataTransformApplicationError) as concurrent_change:
                await service.repair_transform(scope, parent_b, catalog, repair_b,
                    request_identity="ifc:changed-during-repair", request_payload={"mode": "repair", "request": repair_b.model_dump(mode="json", exclude_none=True)},
                    repair_parent_operation_alias=parent_b.catalog_entry.alias)
            assert concurrent_change.value.code == "IFC_SOURCE_CHANGED"
            refused = await runtime.repository.get_data_transform_operation_by_identity(scope, "ifc:changed-during-repair")
            assert refused.operation.error_code == "IFC_SOURCE_CHANGED"
            assert refused.operation.result_package_id is None
            assert refused.operation.error_phase is CriticalOperationErrorPhase.COMMIT_FENCE
            assert refused.candidate is None
            assert len((await application.list_package_catalog(scope)).packages) == before_count

            invalid = command.model_copy(update={"outputs": (
                command.outputs[0].model_copy(update={"logical_key_fields": ("missing_key",)}), command.outputs[1])})
            with pytest.raises(DataTransformApplicationError) as invalid_output:
                await service.transform(scope, invalid, request_identity="ifc:invalid-output")
            assert invalid_output.value.code == "TRANSFORM_OUTPUT_INVALID"
            failed = await runtime.repository.get_data_transform_operation_by_identity(scope, "ifc:invalid-output")
            assert failed.operation.status is CriticalOperationStatus.FAILED
            assert failed.operation.result_package_id is None
            assert len((await application.list_package_catalog(scope)).packages) == before_count

            corrected = DataRepairTransformCommand.model_validate({
                "parent_operation_id": failed.operation.operation_id,
                "outputs": {"action": "edit", "set": [command.outputs[0].model_dump(mode="json")]},
            })
            corrected_json = corrected.model_dump(mode="json", exclude_none=True)
            staging = multiprocessing.get_context("spawn").Process(target=_stage_ifc_repair_candidate_in_other_pid,
                args=(url, tmp_path, subject, alias, corrected_json))
            staging.start()
            await asyncio.to_thread(staging.join, 30)
            if staging.is_alive():
                staging.terminate()
                await asyncio.to_thread(staging.join, 5)
            assert staging.exitcode == 73
            interrupted = await runtime.repository.get_data_transform_operation_by_identity(scope, "ifc:interrupted-repair")
            assert interrupted.candidate is not None
            await advance("Source D")
            calls = worker.calls
            with pytest.raises(DataTransformApplicationError) as restored_changed:
                await service.repair_transform(scope, await runtime.repository.get_operation(scope, failed.operation.operation_id),
                    catalog, corrected, request_identity="ifc:interrupted-repair",
                    request_payload={"mode": "repair", "request": corrected_json})
            assert restored_changed.value.code == "IFC_SOURCE_CHANGED"
            restored = await runtime.repository.get_data_transform_operation_by_identity(scope, "ifc:interrupted-repair")
            assert restored.operation.status is CriticalOperationStatus.FAILED
            assert restored.candidate is None and restored.operation.result_package_id is None
            assert worker.calls == calls
            assert len((await application.list_package_catalog(scope)).packages) == before_count

        other = _create_runtime(url, tmp_path / "other-storage", tmp_path / "other-capacity", subject=f"other-{uuid4().hex}")
        _, other_scope = await _open_scope(other, "Cross tenant source")
        with bind_tenant_execution_context(other_scope.identity):
            async with other.database.session_factory() as session:
                assert await session.scalar(select(CriticalOperationModel.operation_id).where(
                    CriticalOperationModel.operation_id == operation.operation.operation_id)) is None
            assert await other.repository.get_exact(_ifc_scope(other_scope), revision_id=source_a.revision_id) is None

        await sql_worker.close()
        await runtime.close()
        restarted = _create_runtime(url, tmp_path / "storage", tmp_path / "reopened-capacity", subject=subject)
        replay_service, _, replay_worker, restarted_sql, _, _ = _ifc_derivation_service(restarted, tmp_path / "reopened")
        replay_scope = await restarted.resolver.resolve(alias, command_id=uuid4().hex)
        with bind_tenant_execution_context(replay_scope.identity):
            replay = await replay_service.repair_transform(replay_scope, operation, catalog, repair,
                request_identity="ifc:same-a", request_payload=repair_payload,
                repair_parent_operation_alias=operation.catalog_entry.alias)
            assert replay.published.package.package.package_id == repaired.published.package.package.package_id
            assert replay_worker.calls == 0
    finally:
        if restarted_sql is not None:
            await restarted_sql.close()
        if restarted is not None:
            await restarted.close()
        if other is not None:
            await other.close()
        await sql_worker.close()
        await runtime.close()


async def test_source_package_producer_is_exact_and_scope_isolated(tmp_path: Path) -> None:
    if not _TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(
        _TEST_DATABASE_URL,
        environ={
            "CAD2BIM_API_POSTGRES_PASSWORD": "phase1-api-password",
            "CAD2BIM_RUNNER_POSTGRES_PASSWORD": "phase1-runner-password",
            "CAD2BIM_MCP_POSTGRES_PASSWORD": "phase1-mcp-password",
        },
    )
    nonce = uuid4().hex
    runtime = _runtime(_runtime_url(_TEST_DATABASE_URL), tmp_path, f"source-owner-{nonce}")
    database, repository, sessions, resolver, artifacts, worker, _transform = runtime
    other = _runtime(_runtime_url(_TEST_DATABASE_URL), tmp_path, f"source-other-{nonce}")
    other_database, other_repository, other_sessions, other_resolver, other_artifacts, other_worker, _ = other
    try:
        await database.verify_ready()
        opened = await sessions.open_direct_session("Uploaded source navigation")
        scope = await resolver.resolve(opened.binding.direct_session_alias, command_id="upload")
        service = DataRuntimeService(repository, artifacts)
        queries = DataQueryService(repository, artifacts, CursorCodec("A" * 43))
        with bind_tenant_execution_context(scope.identity):
            # User upload is processed by the backend; no Agent import tool or transform Worker.
            imported = await service.import_cad_source(scope, ValidatedSource(
                filename=_SOURCE.name, media_type="application/dxf", content=_SOURCE.read_bytes(),
                source_identity=f"uploaded-source:{nonce}",
            ))
            package = imported.published.package
            original = imported.published.operation
            producer = await repository.get_operation_for_result_package(scope, package.package.package_id)
            assert producer == original
            assert producer.operation.result_package_id == package.package.package_id
            catalog = await service.list_package_catalog(scope)
            assert len(catalog.packages) == 1
            listed = catalog.packages[0]
            assert listed.package == package.package
            assert listed.catalog_entry == package.catalog_entry
            # PostgreSQL reads members in member_name order; publication keeps assembly order.
            expected_members = sorted(package.members, key=lambda member: member.member_name)
            listed_members = sorted(listed.members, key=lambda member: member.member_name)
            assert listed_members == expected_members, _difference_paths(
                [member.model_dump(mode="json") for member in expected_members],
                [member.model_dump(mode="json") for member in listed_members],
            )
            assert catalog.producing_operations == {package.package.package_id: original}
            described = await queries.describe_package(scope, DataDescribePackageQuery(
                package_alias=package.catalog_entry.alias,
            ))
            assert described.producing_operation_alias == original.catalog_entry.alias

        other_opened = await other_sessions.open_direct_session("Other source scope")
        other_scope = await other_resolver.resolve(
            other_opened.binding.direct_session_alias, command_id="read-other-source",
        )
        with bind_tenant_execution_context(other_scope.identity):
            assert await other_repository.get_operation_for_result_package(
                other_scope, package.package.package_id,
            ) is None
            other_catalog = await DataRuntimeService(other_repository, other_artifacts).list_package_catalog(other_scope)
            assert other_catalog.packages == ()
            other_queries = DataQueryService(other_repository, other_artifacts, CursorCodec("A" * 43))
            with pytest.raises(DataQueryApplicationError) as inaccessible:
                await other_queries.describe_package(other_scope, DataDescribePackageQuery(
                    package_alias=package.catalog_entry.alias,
                ))
            assert inaccessible.value.code == "PACKAGE_NOT_FOUND"
    finally:
        await other_worker.close()
        await other_database.dispose()
        await worker.close()
        await database.dispose()


async def test_metadata_only_repair_persists_new_description_and_resumes_after_reopen(tmp_path: Path) -> None:
    if not _TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(_TEST_DATABASE_URL, environ={
        "CAD2BIM_API_POSTGRES_PASSWORD": "phase1-api-password",
        "CAD2BIM_RUNNER_POSTGRES_PASSWORD": "phase1-runner-password",
        "CAD2BIM_MCP_POSTGRES_PASSWORD": "phase1-mcp-password",
    })
    subject = f"metadata-repair-{uuid4().hex}"
    url = _runtime_url(_TEST_DATABASE_URL)
    first = _runtime(url, tmp_path, subject)
    database, repository, sessions, resolver, artifacts, worker, service = first
    description = ("# Selected CAD entities\n## Summary\nExact entity rows.\n"
        "## Intended use\nVerify repair.\n## Contents\nEntity keys.\n"
        "## Method\nSQL.\n## Scope and completeness\nSmall test source.\n"
        "## Known limitations\nNo IFC decisions.\n")
    try:
        await database.verify_ready()
        await worker.verify_ready()
        opened = await sessions.open_direct_session("Metadata repair persistence")
        scope = await resolver.resolve(opened.binding.direct_session_alias, command_id="metadata-repair")
        with bind_tenant_execution_context(scope.identity):
            runtime = DataRuntimeService(repository, artifacts)
            imported = await runtime.import_cad_source(scope, ValidatedSource(
                filename=_SOURCE.name, media_type="application/dxf", content=_SOURCE.read_bytes(),
                source_identity=f"metadata-repair:{subject}",
            ))
            command = DataTransformCommand.model_validate({
                "inputs": [{"input_name": "entities", "package_alias": imported.published.package.catalog_entry.alias, "payload_name": "entities"}],
                "execution": {"engine": "duckdb_sql", "code": "SELECT entity_key, layer FROM entities"},
                "outputs": [{"payload_name": "selected_entities", "format": "parquet", "description": "Exact rows", "logical_key_fields": ["entity_key"]}],
                "output_package": {"name": "selected", "description_markdown": description},
            })
            original = await service.transform(scope, command, request_identity="metadata:original")
            parent = original.published.operation
            repair = DataRepairTransformCommand.model_validate({
                "parent_operation_id": parent.operation.operation_id,
                "output_package": {"description_markdown": {"action": "edit", "edits": [{"old_text": "Exact entity rows.", "new_text": "Exact entity rows, reviewed."}]}},
            })
            request_payload = {"mode": "repair", "request": repair.model_dump(mode="json", exclude_none=True)}
            revised = await service.repair_transform(scope, parent,
                {imported.published.package.catalog_entry.alias: imported.published.package}, repair,
                request_identity="metadata:repair", request_payload=request_payload,
                repair_parent_operation_alias=parent.catalog_entry.alias,
            )
            record = revised.published.operation.operation
            assert record.description_artifact != parent.operation.description_artifact
            assert record.repair_parent_operation_id == parent.operation.operation_id
            assert record.code_artifact.sha256 == parent.operation.code_artifact.sha256
            assert revised.published.package.package.package_id != original.published.package.package.package_id
            state = await repository.get_data_transform_operation_by_identity(scope, "metadata:repair")
            assert state.resolved_command["output_package"]["description_markdown"] == description.replace("Exact entity rows.", "Exact entity rows, reviewed.")
            assert isinstance(state.resolved_command["outputs"], list)
            parent_alias = parent.catalog_entry.alias
            revised_alias = revised.published.operation.catalog_entry.alias
    finally:
        await worker.close()
        await database.dispose()
    database, repository, sessions, resolver, artifacts, worker, service = _runtime(url, tmp_path, subject)
    try:
        scope = await resolver.resolve(opened.binding.direct_session_alias, command_id="metadata-repair-reopen")
        with bind_tenant_execution_context(scope.identity):
            runtime = DataRuntimeService(repository, artifacts)
            old = await runtime.get_transform_input(scope, parent_alias, input_kind="package_description", view=TextSourceView(view_mode="full"))
            new = await runtime.get_transform_input(scope, revised_alias, input_kind="package_description", view=TextSourceView(view_mode="full"))
            assert old.source.text == description
            assert new.source.text == description.replace("Exact entity rows.", "Exact entity rows, reviewed.")
            resumed = await service.resume_by_identity(scope, "metadata:repair", request_payload, deadline=monotonic() + 30)
            assert resumed.published.operation.operation.operation_id == record.operation_id
            assert resumed.published.package.package.package_id == revised.published.package.package.package_id
    finally:
        await worker.close()
        await database.dispose()


async def test_transform_original_operation_replays_after_postgres_reopen_and_new_key_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(
        _TEST_DATABASE_URL,
        environ={
            "CAD2BIM_API_POSTGRES_PASSWORD": "phase1-api-password",
            "CAD2BIM_RUNNER_POSTGRES_PASSWORD": "phase1-runner-password",
            "CAD2BIM_MCP_POSTGRES_PASSWORD": "phase1-mcp-password",
        },
    )
    subject = f"b2-transform-{uuid4().hex}"
    url = _runtime_url(_TEST_DATABASE_URL)
    first = _runtime(url, tmp_path, subject)
    database, repository, sessions, resolver, artifacts, worker, service = first
    try:
        await database.verify_ready()
        await worker.verify_ready()
        opened = await sessions.open_direct_session("B2 exact transform")
        scope = await resolver.resolve(
            opened.binding.direct_session_alias, command_id=f"command_{uuid4().hex}"
        )
        with bind_tenant_execution_context(scope.identity):
            imported = await DataRuntimeService(repository, artifacts).import_cad_source(
                scope,
                ValidatedSource(
                    filename=_SOURCE.name,
                    media_type="application/dxf",
                    content=_SOURCE.read_bytes(),
                    source_identity=f"b2-test:{subject}",
                ),
            )
            source_alias = imported.published.package.catalog_entry.alias
            command = DataTransformCommand.model_validate({
                "inputs": [{
                    "input_name": "entities",
                    "package_alias": source_alias,
                    "payload_name": "entities",
                }],
                "execution": {
                    "engine": "duckdb_sql",
                    "code": "SELECT entity_key, layer FROM entities WHERE layer = $layer",
                },
                "parameters": [{"name": "layer", "kind": "string", "value": "0"}],
                "outputs": [{
                    "payload_name": "selected_entities",
                    "format": "parquet",
                    "description": "Selected exact CAD entities.",
                    "logical_key_fields": ["entity_key"],
                }],
                "output_package": {
                    "name": "b2_selected_entities",
                    "description_markdown": (
                        "# Selected CAD entities\n## Summary\nExact entity rows.\n"
                        "## Intended use\nVerify recovery.\n## Contents\nEntity keys.\n"
                        "## Method\nParameterized SQL.\n## Scope and completeness\n"
                        "Small test source.\n## Known limitations\nNo IFC decisions.\n"
                    ),
                },
            })
            original = await service.transform(scope, command, request_identity="runner:b2:K1")
            original_state = await repository.get_data_transform_operation_by_identity(
                scope, "runner:b2:K1"
            )
            assert original_state is not None
            assert original_state.operation.operation_id == original.published.operation.operation.operation_id
            assert original_state.execution_generation == 1
            assert original_state.candidate is not None
    finally:
        await worker.close()
        await database.engine.dispose()

    reopened = _runtime(url, tmp_path, subject)
    database, repository, _sessions, resolver, _artifacts, worker, service = reopened
    try:
        await database.verify_ready()
        scope = await resolver.resolve(
            opened.binding.direct_session_alias, command_id=f"command_{uuid4().hex}"
        )
        with bind_tenant_execution_context(scope.identity):
            replayed = await service.transform(scope, command, request_identity="runner:b2:K1")
            differences = _difference_paths(
                _publication_snapshot(original.published),
                _publication_snapshot(replayed.published),
            )
            assert not differences, differences
            changed = command.model_copy(update={
                "parameters": command.parameters[:0],
            })
            with pytest.raises(DataTransformApplicationError) as conflict:
                await service.transform(scope, changed, request_identity="runner:b2:K1")
            assert conflict.value.code == "TRANSFORM_REQUEST_IDENTITY_CONFLICT"

            second = await service.transform(scope, command, request_identity="runner:b2:K2")
            assert second.published.package.package.package_id != original.published.package.package.package_id
            assert second.published.operation.operation.operation_id != original.published.operation.operation.operation_id
            own_result = await repository.get_result_for_operation(
                scope, original.published.operation.operation.operation_id
            )
            assert own_result is not None
            assert not _difference_paths(
                _publication_snapshot(original.published),
                _publication_snapshot(own_result),
            )

            # The first publisher exits after staging exact Artifact refs on
            # PREPARED. A distinct PID must finish that candidate without a
            # second Worker execution or a second immutable Package.
            process = multiprocessing.get_context("spawn").Process(
                target=_stage_candidate_in_other_pid,
                args=(
                    url,
                    tmp_path,
                    subject,
                    opened.binding.direct_session_alias,
                    command.model_dump(mode="json"),
                ),
            )
            process.start()
            await asyncio.to_thread(process.join, 30)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
            assert process.exitcode == 73
            pending = await repository.get_data_transform_operation_by_identity(
                scope, "runner:b2:K3"
            )
            assert pending is not None
            assert pending.operation.status is CriticalOperationStatus.PREPARED
            assert pending.execution_generation == 1
            assert pending.candidate is not None

            async def forbidden_worker(*_args: Any, **_kwargs: Any) -> Any:
                raise AssertionError("staged candidate recovery must not rerun the Worker")

            monkeypatch.setattr(worker, "execute", forbidden_worker)
            recovered = await service.transform(scope, command, request_identity="runner:b2:K3")
            assert recovered.published.operation.operation.operation_id == pending.operation.operation_id
            assert recovered.published.package.package.package_id == pending.candidate.package.package_id
            settled = await repository.get_data_transform_operation_by_identity(
                scope, "runner:b2:K3"
            )
            assert settled is not None
            assert settled.operation.status is CriticalOperationStatus.SUCCEEDED
            assert settled.execution_generation == 2

            # Admission with no executor can be failed once its fixed budget
            # expires. The original Operation becomes terminal in one
            # transaction; there is no fresh action or Package.
            unclaimed = await _prepare_deadline_probe(
                repository, scope, original_state,
                request_identity="runner:b2:K4",
                deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
            expired = await repository.expire_data_transform_operation(
                scope,
                unclaimed.operation.operation_id,
                expected_owner=None,
                expected_generation=0,
                failed_operation=_deadline_failure(unclaimed),
            )
            assert expired is not None
            assert expired.operation.status is CriticalOperationStatus.FAILED
            assert expired.operation.operation_id == unclaimed.operation.operation_id

            # A claimant that is still running cannot be declared dead merely
            # because its original execution budget is now exhausted.
            live_deadline = datetime.now(timezone.utc) + timedelta(seconds=2)
            healthy = await _prepare_deadline_probe(
                repository, scope, original_state,
                request_identity="runner:b2:K5", deadline_at=live_deadline,
            )
            healthy_claim = await repository.claim_data_transform_execution(
                scope,
                healthy.operation.operation_id,
                owner=current_data_transform_executor_owner(),
                lease_seconds=30,
            )
            assert healthy_claim is not None
            await asyncio.sleep(max(0, (live_deadline - datetime.now(timezone.utc)).total_seconds()) + 0.1)
            assert await repository.expire_data_transform_operation(
                scope,
                healthy.operation.operation_id,
                expected_owner=healthy_claim.owner,
                expected_generation=healthy_claim.generation,
                failed_operation=_deadline_failure(healthy),
            ) is None

            # A different claimant exits without a candidate. Once its fixed
            # budget expires, the exact dead owner can be settled as FAILED.
            dead_deadline = datetime.now(timezone.utc) + timedelta(seconds=6)
            dead = await _prepare_deadline_probe(
                repository, scope, original_state,
                request_identity="runner:b2:K6", deadline_at=dead_deadline,
            )
            claimant = multiprocessing.get_context("spawn").Process(
                target=_claim_without_candidate_in_other_pid,
                args=(
                    url, tmp_path, subject, opened.binding.direct_session_alias,
                    dead.operation.operation_id,
                ),
            )
            claimant.start()
            await asyncio.to_thread(claimant.join, 15)
            if claimant.is_alive():
                claimant.terminate()
                await asyncio.to_thread(claimant.join, 5)
            assert claimant.exitcode == 74
            abandoned = await repository.get_data_transform_operation_by_identity(
                scope, "runner:b2:K6"
            )
            assert abandoned is not None and abandoned.execution_owner is not None
            await asyncio.sleep(max(0, (dead_deadline - datetime.now(timezone.utc)).total_seconds()) + 0.1)
            failed = await repository.expire_data_transform_operation(
                scope,
                dead.operation.operation_id,
                expected_owner=abandoned.execution_owner,
                expected_generation=abandoned.execution_generation,
                failed_operation=_deadline_failure(dead),
            )
            assert failed is not None
            assert failed.operation.status is CriticalOperationStatus.FAILED
            assert failed.operation.operation_id == dead.operation.operation_id
    finally:
        await worker.close()
        await database.engine.dispose()
