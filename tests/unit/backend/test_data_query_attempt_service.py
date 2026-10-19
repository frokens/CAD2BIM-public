from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
import pytest

from CAD2BIM_backend.application.data_query_attempt_service import (
    DataQueryAttemptApplicationError,
    DataQueryAttemptService,
)
from CAD2BIM_backend.application.errors import (
    ConflictError as ArtifactConflictError,
    ValidationError as ArtifactValidationError,
)
from CAD2BIM_backend.domain.control_plane import ArtifactRecord, ConflictError, ExecutionMode
from CAD2BIM_backend.application.artifact_service import ArtifactDownload
from CAD2BIM_backend.domain.data_query_attempt import (
    DataQueryAttemptPreparation,
    DataQueryAttemptRecord,
    DataQueryAttemptStatus,
    DataQueryEngine,
    DataQueryExactInputRef,
    DataQueryPrepared,
    DataQueryResultSummary,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.text_source import SourceEdit
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.memory.data_query_attempt_repository import (
    DataQueryAttemptRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_data_query_attempt import (
    PostgresDataQueryAttemptRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.source_text.source_patch_adapter import TextSourcePatchAdapter


NOW = datetime(2026, 9, 4, 10, tzinfo=UTC)


def _scope(
    *,
    run_id: str = "run-query",
    principal_id: str = "principal-query",
    execution_mode: ExecutionMode = ExecutionMode.DIRECT_MCP,
    catalog_frontier: int | None = None,
) -> InvocationScope:
    return InvocationScope(
        identity=TenantExecutionContext(
            tenant_id="tenant-query",
            principal_id=principal_id,
            task_id="task-query",
            actor_kind=TenantActorKind.USER,
            command_id="command-query",
        ),
        owner_id="tenant-query",
        run_id=run_id,
        execution_mode=execution_mode,
        catalog_frontier=catalog_frontier,
    )


def _input() -> DataQueryExactInputRef:
    return DataQueryExactInputRef(
        ordinal=1,
        input_name="records",
        package_id="package-records",
        payload_member_id="payload-records",
    )


def _summary() -> DataQueryResultSummary:
    return DataQueryResultSummary(
        exact_input_digest="b" * 64,
        query_fingerprint="c" * 64,
        row_schema_digest="d" * 64,
        snapshot_sha256="e" * 64,
        row_count=1,
        snapshot_size_bytes=32,
    )


class _Artifacts:
    def __init__(self) -> None:
        self.uploads: list[dict[str, object]] = []
        self.records: dict[str, tuple[ArtifactRecord, bytes]] = {}
        self.failure: Exception | None = None

    async def upload_bytes(self, **values) -> ArtifactRecord:
        if self.failure is not None:
            raise self.failure
        self.uploads.append(values)
        content = values["content"]
        digest = hashlib.sha256(content).hexdigest()
        record = ArtifactRecord(
            artifact_id="artifact-query-source",
            version_id="artifact-query-source-v1",
            owner_id=values["owner_id"],
            task_id=values["task_id"],
            run_id=values["run_id"],
            kind=values["kind"],
            filename=values["filename"],
            content_type=values["content_type"],
            size_bytes=len(content),
            sha256=digest,
            idempotency_key=values["idempotency_key"],
            version_metadata=values["version_metadata"],
            created_at=NOW,
        )
        self.records[record.artifact_id] = record, content
        return record

    async def download_version(self, owner_id, artifact_id, version_id, *, run_id=None):
        record, content = self.records[artifact_id]
        assert (record.owner_id, record.version_id, record.run_id) == (owner_id, version_id, run_id)
        return ArtifactDownload(artifact=record, content=content)


class _Repository:
    def __init__(self) -> None:
        self.records: dict[str, DataQueryAttemptRecord] = {}
        self.replays: dict[str, str] = {}
        self.lose_terminal_response = False
        self.hang_terminal = False

    async def prepare_data_query_attempt(self, scope, preparation) -> DataQueryPrepared:
        replay_id = self.replays.get(preparation.request_identity)
        if replay_id is not None:
            return DataQueryPrepared(attempt=self.records[replay_id], created=False)
        record = DataQueryAttemptRecord(
            **preparation.model_dump(mode="python"),
            sequence=len(self.records) + 1,
            owner_id=scope.owner_id,
            tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id,
            status=DataQueryAttemptStatus.PREPARED,
        )
        self.records[record.attempt_id] = record
        self.replays[record.request_identity] = record.attempt_id
        return DataQueryPrepared(attempt=record, created=True)

    async def get_data_query_attempt(self, scope, *, attempt_id):
        record = self.records.get(attempt_id)
        if record is None:
            return None
        if (
            record.tenant_id != scope.identity.tenant_id
            or record.task_id != scope.identity.task_id
            or record.run_id != scope.run_id
        ):
            return None
        return record

    async def commit_data_query_attempt_terminal(self, scope, *, attempt_id, terminal):
        if self.hang_terminal:
            await asyncio.Event().wait()
        current = self.records[attempt_id]
        result = DataQueryAttemptRecord.model_validate(
            current.model_copy(
                update={
                    "status": terminal.status,
                    "diagnostic": terminal.diagnostic,
                    "result_summary": terminal.result_summary,
                    "finished_at": terminal.finished_at,
                }
            )
        )
        self.records[attempt_id] = result
        if self.lose_terminal_response:
            self.lose_terminal_response = False
            raise RuntimeError("simulated response loss")
        return result


class _Snapshots:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.allowed_scope_digest: str | None = None

    async def available_for_attempt(self, attempt_id: str, *, scope_digest: str) -> bool:
        self.calls.append((attempt_id, scope_digest))
        if self.allowed_scope_digest is None:
            self.allowed_scope_digest = scope_digest
        return scope_digest == self.allowed_scope_digest


def _service(*, snapshots=None):
    repository = _Repository()
    artifacts = _Artifacts()
    service = DataQueryAttemptService(
        repository,
        artifacts,
        CursorCodec("A" * 43),
        TextSourcePatchAdapter(),
        snapshots,
    )
    return service, repository, artifacts


async def _prepare(service: DataQueryAttemptService, scope: InvocationScope):
    return await service.prepare(
        scope,
        engine=DataQueryEngine.DUCKDB_SQL,
        source_code="SELECT * FROM records",
        inputs=(_input(),),
        parameters=(),
        inline_tables=(),
        page_size=100,
        request_identity="request-query",
    )


@pytest.mark.parametrize("engine,source", [
    (DataQueryEngine.DUCKDB_SQL, "SELECT bad FROM records"),
    (DataQueryEngine.PYTHON_ARROW, "def query(ctx):\n    return bad\n"),
])
async def test_repair_uses_immutable_parent_and_literal_precondition_without_caller_hash(
    engine, source,
) -> None:
    service, repository, artifacts = _service()
    scope = _scope()
    prepared = await service.prepare(
        scope, engine=engine, source_code=source, inputs=(_input(),),
        parameters=(), inline_tables=(), page_size=100, request_identity="repair-parent",
    )
    await service.succeed(scope, prepared.attempt, _summary())
    parent = repository.records[prepared.attempt.attempt_id]
    parent_alias = service.attempt_alias(scope, parent)

    repaired_parent, patched = await service.repair_source(
        scope, engine=engine, parent_attempt_alias=parent_alias,
        edits=(SourceEdit(old_text="bad", new_text="good"),),
    )
    assert repaired_parent == parent
    assert patched == source.replace("bad", "good")
    assert artifacts.records[parent.source_artifact.artifact_id][1] == source.encode()
    with pytest.raises(DataQueryAttemptApplicationError) as invalid:
        await service.repair_source(
            scope, engine=engine, parent_attempt_alias=parent_alias,
            edits=(SourceEdit(old_text="absent literal", new_text="good"),),
        )
    assert invalid.value.code == "DATA_QUERY_REPAIR_MATCH_NOT_FOUND"
    assert len(repository.records) == 1


async def test_prepare_saves_exact_source_before_one_run_scoped_attempt() -> None:
    service, repository, artifacts = _service()
    scope = _scope()
    prepared = await _prepare(service, scope)

    assert prepared.created is True
    assert prepared.attempt.status is DataQueryAttemptStatus.PREPARED
    assert prepared.attempt.inputs == (_input(),)
    assert tuple(repository.records) == (prepared.attempt.attempt_id,)
    assert len(artifacts.uploads) == 1
    upload = artifacts.uploads[0]
    assert upload["kind"] == "data_query_source"
    assert upload["content_type"] == "application/sql"
    assert upload["run_id"] == scope.run_id
    assert upload["version_metadata"] == {"engine": "duckdb_sql"}

    replay = await _prepare(service, scope)
    assert replay.created is False
    assert replay.attempt.attempt_id == prepared.attempt.attempt_id
    assert len(repository.records) == 1


@pytest.mark.parametrize(
    "require_same",
    [
        DataQueryAttemptRepositoryMixin._require_same_data_query_preparation,
        PostgresDataQueryAttemptRepositoryMixin._require_same_data_query_preparation,
    ],
)
async def test_request_replay_ignores_server_time_but_rejects_changed_facts(
    require_same,
) -> None:
    service, _, _ = _service()
    record = (await _prepare(service, _scope())).attempt
    stable_fields = set(DataQueryAttemptPreparation.model_fields)
    later = DataQueryAttemptPreparation.model_validate(
        record.model_dump(include=stable_fields, mode="python")
        | {"created_at": record.created_at + timedelta(seconds=1)}
    )

    require_same(record, later)
    changed = later.model_copy(update={"request_digest": "f" * 64})
    with pytest.raises(ConflictError, match="different facts"):
        require_same(record, changed)


async def test_unknown_source_persistence_error_is_masked_before_attempt_creation() -> None:
    service, repository, artifacts = _service()
    artifacts.failure = OSError("private storage path")
    with pytest.raises(DataQueryAttemptApplicationError) as captured:
        await _prepare(service, _scope())
    assert captured.value.code == "DATA_QUERY_ATTEMPT_UNAVAILABLE"
    assert captured.value.retryable is True
    assert captured.value.attempt_alias is None
    assert str(captured.value) == "Data query source persistence is unavailable"
    assert repository.records == {}


async def test_source_storage_validation_failure_uses_safe_unavailable_error() -> None:
    service, repository, artifacts = _service()
    artifacts.failure = ArtifactValidationError("storage")

    with pytest.raises(DataQueryAttemptApplicationError) as captured:
        await _prepare(service, _scope())

    assert captured.value.code == "DATA_QUERY_ATTEMPT_UNAVAILABLE"
    assert captured.value.retryable is True
    assert captured.value.attempt_alias is None
    assert repository.records == {}


async def test_source_idempotency_conflict_is_nonretryable_and_has_no_attempt_alias() -> None:
    service, repository, artifacts = _service()
    artifacts.failure = ArtifactConflictError("different deterministic bytes")

    with pytest.raises(DataQueryAttemptApplicationError) as captured:
        await _prepare(service, _scope())

    assert captured.value.code == "DATA_QUERY_REQUEST_CONFLICT"
    assert captured.value.retryable is False
    assert captured.value.attempt_alias is None
    assert repository.records == {}


async def test_response_loss_reconciles_terminal_state_and_failure_returns_alias() -> None:
    service, repository, _ = _service()
    scope = _scope()
    attempt = (await _prepare(service, scope)).attempt
    repository.lose_terminal_response = True
    succeeded = await service.succeed(scope, attempt, _summary())
    assert succeeded.status is DataQueryAttemptStatus.SUCCEEDED

    second = await service.prepare(
        scope,
        engine=DataQueryEngine.DUCKDB_SQL,
        source_code="SELECT missing FROM records",
        inputs=(_input(),),
        parameters=(),
        inline_tables=(),
        page_size=100,
        request_identity="request-failure",
    )
    with pytest.raises(DataQueryAttemptApplicationError) as captured:
        await service.fail(
            scope,
            second.attempt,
            code="QUERY_BIND_FAILED",
            message="missing column",
            phase="code_validation",
            retryable=False,
        )
    assert captured.value.phase == "code_validation"
    assert captured.value.attempt_alias is not None
    resolved = await service.resolve_attempt_alias(scope, captured.value.attempt_alias)
    assert resolved.status is DataQueryAttemptStatus.FAILED


async def test_attempt_alias_is_run_scoped_and_snapshot_availability_uses_full_invocation_scope() -> (
    None
):
    snapshots = _Snapshots()
    service, _, _ = _service(snapshots=snapshots)
    scope = _scope()
    attempt = await service.succeed(scope, (await _prepare(service, scope)).attempt, _summary())
    alias = service.attempt_alias(scope, attempt)
    assert await service.resolve_attempt_alias(scope, alias) == attempt

    with pytest.raises(DataQueryAttemptApplicationError) as captured:
        await service.resolve_attempt_alias(_scope(run_id="run-other"), alias)
    assert captured.value.code == "DATA_QUERY_ATTEMPT_SCOPE_MISMATCH"

    assert await service.result_available(scope, attempt) is True
    assert await service.result_available(_scope(principal_id="principal-other"), attempt) is False
    assert len(snapshots.calls) == 2
    assert snapshots.calls[0][1] != snapshots.calls[1][1]


async def test_process_private_execution_claim_serializes_and_releases_without_leak() -> None:
    service, _, _ = _service()
    scope = _scope()
    attempt = (await _prepare(service, scope)).attempt
    assert (
        await service.acquire_execution(
            scope, attempt.attempt_id, wait_timeout_seconds=1.0
        )
        == attempt
    )

    waiter = asyncio.create_task(
        service.acquire_execution(scope, attempt.attempt_id, wait_timeout_seconds=1.0)
    )
    await asyncio.sleep(0)
    assert waiter.done() is False
    service.release_execution(attempt.attempt_id)
    assert await waiter == attempt
    service.release_execution(attempt.attempt_id)
    assert service._execution_claims == {}


async def test_execution_claim_wait_timeout_does_not_terminalize_owner_or_leak() -> None:
    service, repository, _ = _service()
    scope = _scope()
    attempt = (await _prepare(service, scope)).attempt
    await service.acquire_execution(scope, attempt.attempt_id, wait_timeout_seconds=1.0)

    with pytest.raises(DataQueryAttemptApplicationError) as captured:
        await service.acquire_execution(scope, attempt.attempt_id, wait_timeout_seconds=0.001)

    assert captured.value.code == "DATA_QUERY_ATTEMPT_UNAVAILABLE"
    assert captured.value.retryable is True
    assert repository.records[attempt.attempt_id].status is DataQueryAttemptStatus.PREPARED
    assert service._execution_claims[attempt.attempt_id].users == 1
    service.release_execution(attempt.attempt_id)
    assert service._execution_claims == {}


async def test_hanging_terminal_publication_is_bounded_and_left_reconcilable() -> None:
    service, repository, _ = _service()
    scope = _scope()
    attempt = (await _prepare(service, scope)).attempt
    repository.hang_terminal = True

    with pytest.raises(DataQueryAttemptApplicationError) as captured:
        await service.succeed(
            scope,
            attempt,
            _summary(),
            publication_timeout_seconds=0.01,
        )

    assert captured.value.code == "DATA_QUERY_ATTEMPT_PUBLICATION_UNCERTAIN"
    assert captured.value.retryable is True
    assert captured.value.attempt_alias is not None
    assert repository.records[attempt.attempt_id].status is DataQueryAttemptStatus.PREPARED
