from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from CAD2BIM_backend.application.data_query_python_service import (
    DataQueryPythonApplicationError,
    DataQueryPythonService,
    _ResolvedInput,
    _map_transform_error,
)
from CAD2BIM_backend.application.data_query_deadline import (
    DATA_QUERY_TERMINAL_PUBLICATION_RESERVE_SECONDS,
)
from CAD2BIM_backend.application.data_transform_service import _all_arrow_inputs
from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.data_query_python import (
    DataQueryPythonQuery,
    DataQueryPythonRequest,
    PythonArrowQueryWorkerRequest,
)
from CAD2BIM_backend.domain.data_query_attempt import DataQueryAttemptStatus
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.data_transform import (
    DataTransformWorkerError,
    DataTransformWorkerReceipt,
    TransformInputBinding,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.application.exact_payload_resolver import (
    ExactJsonPayload,
    ExactParquetPayload,
)
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.data_query.snapshot_cache import QuerySnapshotCache
from CAD2BIM_backend.infrastructure.data_transform.python_arrow_worker import (
    SubprocessPythonArrowDataTransformWorker,
)
from CAD2BIM_backend.ports.data_transform import ArrowTransformInput, ArrowTransformOutput


pytestmark = pytest.mark.asyncio


def _cursor_key() -> str:
    return base64.urlsafe_b64encode(b"p" * 32).rstrip(b"=").decode("ascii")


async def test_worker_code_validation_phase_is_preserved() -> None:
    error = _map_transform_error(
        "TRANSFORM_PYTHON_INVALID",
        "invalid query entrypoint",
        phase="code_validation",
    )

    assert error.code == "QUERY_PYTHON_INVALID"
    assert error.phase == "code_validation"


def _scope(run_id: str = "run-python-query") -> InvocationScope:
    return InvocationScope(
        identity=TenantExecutionContext(
            tenant_id="tenant-query",
            principal_id="principal-query",
            task_id="task-query",
            actor_kind=TenantActorKind.USER,
            command_id="command-query",
        ),
        owner_id="tenant-query",
        run_id=run_id,
        execution_mode=ExecutionMode.DIRECT_MCP,
        catalog_frontier=None,
    )


def _query(*, page_size: int = 1, code: str | None = None) -> DataQueryPythonQuery:
    return DataQueryPythonQuery(
        inputs=(
            TransformInputBinding(
                input_name="entities", package_alias="data_001", payload_name="entities"
            ),
        ),
        code=code
        or (
            "import pyarrow as pa\n"
            "def query(ctx):\n"
            "    return pa.table({'entity_key': ['e1', 'e2', 'e3']})\n"
        ),
        page_size=page_size,
    )


class _Worker:
    def __init__(self, output: ArrowTransformOutput) -> None:
        self.output = output
        self.calls = 0

    async def execute_query(self, request, inputs, *, deadline=None):
        self.calls += 1
        assert isinstance(request, PythonArrowQueryWorkerRequest)
        assert len(inputs) == 1
        return self.output


class _Payloads:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def materialize_members(self, members, **_kwargs):
        return {members[0].member_id: self.path}

    @staticmethod
    def remove_paths(_paths) -> None:
        return None


class _AttemptLifecycle:
    def __init__(self) -> None:
        self.attempts: dict[str, SimpleNamespace] = {}

    async def prepare(self, *_args, request_identity: str, **_kwargs):
        attempt = self.attempts.get(request_identity)
        if attempt is None:
            attempt = SimpleNamespace(
                attempt_id=f"dqa_{len(self.attempts) + 1}",
                status=DataQueryAttemptStatus.PREPARED,
                result_summary=None,
                diagnostic=None,
            )
            self.attempts[request_identity] = attempt
        return SimpleNamespace(attempt=attempt)

    async def acquire_execution(self, _scope, attempt_id: str, **_kwargs):
        return next(item for item in self.attempts.values() if item.attempt_id == attempt_id)

    def release_execution(self, _attempt_id: str) -> None:
        return None

    def attempt_alias(self, _scope, attempt) -> str:
        return f"dqa_v1.{attempt.attempt_id}"

    async def succeed(self, _scope, attempt, summary, **_kwargs):
        attempt.status = DataQueryAttemptStatus.SUCCEEDED
        attempt.result_summary = summary
        return attempt

    async def fail(self, _scope, attempt, **kwargs):
        attempt.status = DataQueryAttemptStatus.FAILED
        attempt.diagnostic = SimpleNamespace(
            code=kwargs["code"],
            message=kwargs["message"],
            phase=kwargs["phase"],
            retryable=kwargs["retryable"],
        )
        return attempt

    async def get_attempt(self, _scope, attempt_id: str):
        return next(
            (item for item in self.attempts.values() if item.attempt_id == attempt_id),
            None,
        )


def _service(tmp_path: Path) -> tuple[DataQueryPythonService, _Worker, QuerySnapshotCache]:
    input_path = tmp_path / "input.parquet"
    pq.write_table(pa.table({"entity_key": ["in1", "in2", "in3"]}), input_path)
    artifact = SimpleNamespace(
        artifact_id="artifact-entities",
        version_id="version-entities",
        sha256="a" * 64,
        model_dump=lambda **_kwargs: {
            "artifact_id": "artifact-entities",
            "version_id": "version-entities",
            "sha256": "a" * 64,
        },
    )
    member = SimpleNamespace(member_id="member-entities", row_count=3, artifact=artifact)
    payload = ExactParquetPayload(
        payload_name="entities", logical_key_fields=("entity_key",), member=member
    )
    resolved = (
        _ResolvedInput(
            request_name="entities",
            payload=payload,
            package_id="package-source",
            package=SimpleNamespace(package=SimpleNamespace(package_id="package-source")),
        ),
    )
    table = pa.table({"entity_key": ["e1", "e2", "e3"]})
    output = ArrowTransformOutput(
        receipt=DataTransformWorkerReceipt(row_count=3, arrow_nbytes=table.nbytes, execution_ms=11),
        schema=table.schema,
        batches=tuple(table.to_batches()),
        payload_name=None,
    )
    worker = _Worker(output)
    snapshots = QuerySnapshotCache(root=tmp_path / "snapshots")
    service = DataQueryPythonService(
        _Payloads(input_path),
        worker,
        CursorCodec(_cursor_key()),
        snapshots,
        _AttemptLifecycle(),
    )
    service._resolve_inputs = AsyncMock(return_value=resolved)
    return service, worker, snapshots


async def _fresh_query(service, scope, request, *, deadline=None):
    sequence = getattr(service, "_unit_request_sequence", 0) + 1
    service._unit_request_sequence = sequence
    return await service.query(
        scope,
        request,
        deadline=deadline,
        request_identity=f"unit-request-{sequence}",
    )


async def test_domain_accepts_source_before_worker_validation_and_requires_unique_bindings() -> (
    None
):
    source_first = _query(code="def transform(ctx):\n    return None\n")
    assert source_first.code.startswith("def transform")

    binding = TransformInputBinding(
        input_name="entities", package_alias="data_001", payload_name="entities"
    )
    with pytest.raises(ValidationError, match="cannot be bound twice"):
        DataQueryPythonQuery(
            inputs=(
                binding,
                binding.model_copy(update={"input_name": "entities_again"}),
            ),
            code="def query(ctx):\n    return ctx.table('entities')\n",
        )


async def test_first_page_executes_once_and_continuation_only_reads_snapshot(
    tmp_path: Path,
) -> None:
    service, worker, snapshots = _service(tmp_path)
    first = await _fresh_query(
        service, _scope(), DataQueryPythonRequest(query=_query(page_size=1))
    )
    assert first.page.next_cursor is not None
    second = await service.query(_scope(), DataQueryPythonRequest(cursor=first.page.next_cursor))
    replay = await service.query(_scope(), DataQueryPythonRequest(cursor=first.page.next_cursor))

    assert [row.values for row in first.rows] == [{"entity_key": "e1"}]
    assert [row.values for row in second.rows] == [{"entity_key": "e2"}]
    assert second == replay
    assert worker.calls == 1
    assert first.query_statistics.model_dump() == {
        "bound_input_payload_count": 1,
        "bound_input_row_count": 3,
        "bound_input_stored_bytes": (tmp_path / "input.parquet").stat().st_size,
        "bound_inline_table_count": 0,
        "bound_inline_row_count": 0,
        "execution_ms": 11,
    }
    await snapshots.close()


async def test_first_page_requires_server_invocation_identity(tmp_path: Path) -> None:
    service, worker, snapshots = _service(tmp_path)

    with pytest.raises(DataQueryPythonApplicationError) as captured:
        await service.query(_scope(), DataQueryPythonRequest(query=_query()))

    assert captured.value.code == "SYSTEM_PAUSED"
    assert worker.calls == 0
    assert service._attempts.attempts == {}
    await snapshots.close()


@pytest.mark.parametrize("discard_mode", ["error", "hang"])
async def test_failure_publication_precedes_best_effort_snapshot_discard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    discard_mode: str,
) -> None:
    service, _worker, snapshots = _service(tmp_path)
    prepared = await service._attempts.prepare(request_identity="terminal-failure")
    attempt = prepared.attempt

    async def discard(_attempt_id: str) -> None:
        if discard_mode == "error":
            raise OSError("snapshot unlink failed")
        await asyncio.Event().wait()

    monkeypatch.setattr(snapshots, "discard_for_attempt", discard)
    failure = DataQueryPythonApplicationError(
        "QUERY_EXECUTION_FAILED",
        "worker failed",
        phase="execution",
    )

    await service._terminalize_failure(
        _scope(),
        attempt,
        failure,
        time.monotonic() + 0.02,
    )

    assert attempt.status is DataQueryAttemptStatus.FAILED
    assert attempt.diagnostic.code == "QUERY_EXECUTION_FAILED"
    assert failure.attempt_alias == service._attempts.attempt_alias(_scope(), attempt)
    await snapshots.close()


async def test_execution_deadline_reserves_failed_attempt_publication(
    tmp_path: Path,
) -> None:
    service, worker, snapshots = _service(tmp_path)

    async def hang(_request, _inputs, *, deadline=None):
        await asyncio.Event().wait()

    worker.execute_query = hang
    with pytest.raises(DataQueryPythonApplicationError) as captured:
        await _fresh_query(
            service,
            _scope(),
            DataQueryPythonRequest(query=_query()),
            deadline=(
                time.monotonic()
                + DATA_QUERY_TERMINAL_PUBLICATION_RESERVE_SECONDS
                + 0.02
            ),
        )

    attempt = next(iter(service._attempts.attempts.values()))
    assert captured.value.code == "QUERY_TIMEOUT"
    assert captured.value.attempt_alias == service._attempts.attempt_alias(_scope(), attempt)
    assert attempt.status is DataQueryAttemptStatus.FAILED
    assert attempt.diagnostic.code == "QUERY_TIMEOUT"
    await snapshots.close()


async def test_json_records_are_readable_and_counted_in_query_statistics(
    tmp_path: Path,
) -> None:
    value = [{"record_key": "r1"}, {"record_key": "r2"}]
    payload_bytes = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    schema_bytes = json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"record_key": {"type": "string"}},
                "required": ["record_key"],
                "additionalProperties": False,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    payload_path = tmp_path / "records.json"
    schema_path = tmp_path / "records.schema.json"
    payload_path.write_bytes(payload_bytes)
    schema_path.write_bytes(schema_bytes)

    def artifact(name: str):
        return SimpleNamespace(
            artifact_id=f"artifact-{name}",
            version_id=f"version-{name}",
            sha256="b" * 64,
            model_dump=lambda **_kwargs: {
                "artifact_id": f"artifact-{name}",
                "version_id": f"version-{name}",
                "sha256": "b" * 64,
            },
        )

    payload_member = SimpleNamespace(
        member_id="member-records",
        logical_byte_count=len(payload_bytes),
        artifact=artifact("records"),
    )
    schema_member = SimpleNamespace(
        member_id="member-records-schema",
        logical_byte_count=len(schema_bytes),
        artifact=artifact("records-schema"),
    )
    payload = ExactJsonPayload(
        payload_name="records",
        json_kind="records",
        member=payload_member,
        schema_member=schema_member,
    )
    resolved = (
        _ResolvedInput(
            request_name="records",
            payload=payload,
            package_id="package-derived",
            package=SimpleNamespace(package=SimpleNamespace(package_id="package-derived")),
        ),
    )
    query = DataQueryPythonQuery(
        inputs=(
            TransformInputBinding(
                input_name="records",
                package_alias="data_002",
                payload_name="records",
            ),
        ),
        code=(
            "import pyarrow as pa\n"
            "def query(ctx):\n"
            "    return pa.table({'record_count': [len(ctx.json('records'))]})\n"
        ),
    )
    worker_input = _all_arrow_inputs(
        resolved,
        {
            payload_member.member_id: payload_path,
            schema_member.member_id: schema_path,
        },
        query,
    )[0]
    assert worker_input.input_format == "json"
    assert worker_input.json_kind == "records"
    assert worker_input.row_count == 2

    result_table = pa.table({"record_count": [2]})
    worker = _Worker(
        ArrowTransformOutput(
            receipt=DataTransformWorkerReceipt(
                row_count=1,
                arrow_nbytes=result_table.nbytes,
                execution_ms=7,
            ),
            schema=result_table.schema,
            batches=tuple(result_table.to_batches()),
            payload_name=None,
        )
    )

    class JsonPayloads:
        async def materialize_members(self, members, **_kwargs):
            paths = {
                payload_member.member_id: payload_path,
                schema_member.member_id: schema_path,
            }
            return {member.member_id: paths[member.member_id] for member in members}

        @staticmethod
        def remove_paths(_paths) -> None:
            return None

    snapshots = QuerySnapshotCache(root=tmp_path / "json-snapshots")
    service = DataQueryPythonService(
        JsonPayloads(), worker, CursorCodec(_cursor_key()), snapshots, _AttemptLifecycle()
    )
    service._resolve_inputs = AsyncMock(return_value=resolved)
    result = await _fresh_query(service, _scope(), DataQueryPythonRequest(query=query))
    assert result.rows[0].values == {"record_count": 2}
    assert result.query_statistics.bound_input_row_count == 2
    await snapshots.close()


async def test_materialized_json_rows_cannot_bypass_the_input_row_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, worker, snapshots = _service(tmp_path)
    tiny = pa.table({"record_index": [0], "json_value": ['{"key":"value"}']})

    monkeypatch.setattr(
        "CAD2BIM_backend.application.data_query_python_service._all_arrow_inputs",
        lambda *_args: (
            ArrowTransformInput(
                "entities",
                tiny.to_reader(),
                input_format="json",
                json_kind="records",
                row_count=4_000_001,
            ),
        ),
    )

    with pytest.raises(DataQueryPythonApplicationError) as error:
        await _fresh_query(service, _scope(), DataQueryPythonRequest(query=_query()))

    assert error.value.code == "QUERY_INPUT_BUDGET_EXCEEDED"
    assert worker.calls == 0
    await snapshots.close()


async def test_cursor_is_tool_and_scope_bound(tmp_path: Path) -> None:
    service, _worker, snapshots = _service(tmp_path)
    first = await _fresh_query(
        service, _scope(), DataQueryPythonRequest(query=_query(page_size=1))
    )
    assert first.page.next_cursor is not None

    with pytest.raises(DataQueryPythonApplicationError) as wrong_scope:
        await service.query(
            _scope("other-run"), DataQueryPythonRequest(cursor=first.page.next_cursor)
        )
    assert wrong_scope.value.code == "CURSOR_SCOPE_MISMATCH"

    claims = service._cursor_codec.decode(first.page.next_cursor)
    claims["tool"] = "data_query_sql"
    with pytest.raises(DataQueryPythonApplicationError) as wrong_tool:
        await service.query(
            _scope(), DataQueryPythonRequest(cursor=service._cursor_codec.encode(claims))
        )
    assert wrong_tool.value.code == "CURSOR_TOOL_MISMATCH"
    await snapshots.close()


@pytest.mark.parametrize(
    ("worker_code", "query_code", "retryable"),
    [
        ("TRANSFORM_PYTHON_INVALID", "QUERY_PYTHON_INVALID", False),
        ("TRANSFORM_INPUT_BUDGET_EXCEEDED", "QUERY_INPUT_BUDGET_EXCEEDED", False),
        ("TRANSFORM_EXECUTION_FAILED", "QUERY_EXECUTION_FAILED", False),
        ("TRANSFORM_RESOURCE_LIMIT", "QUERY_RESOURCE_LIMIT", False),
        ("TRANSFORM_TIMEOUT", "QUERY_TIMEOUT", False),
        ("TRANSFORM_CAPACITY_BUSY", "QUERY_CAPACITY_BUSY", True),
        ("TRANSFORM_SERVICE_UNAVAILABLE", "QUERY_SERVICE_UNAVAILABLE", True),
    ],
)
async def test_worker_errors_are_projected_to_query_semantics(
    worker_code: str, query_code: str, retryable: bool
) -> None:
    error = _map_transform_error(worker_code, "bounded", phase="preflight")
    assert error.code == query_code
    assert error.retryable is retryable


async def test_python_worker_query_context_is_read_only_and_returns_one_arrow_result(
    tmp_path: Path,
) -> None:
    worker = SubprocessPythonArrowDataTransformWorker(
        python_executable=os.environ.get(
            "CAD2BIM_TEST_DATA_PYTHON_WORKER_EXECUTABLE", sys.executable
        ),
        capacity_root=tmp_path / "capacity",
    )
    try:
        result = await worker.execute_query(
            PythonArrowQueryWorkerRequest(
                code=(
                    "import pyarrow as pa\n"
                    "def query(ctx):\n"
                    "    return pa.table({'count': [ctx.table('source').num_rows]})\n"
                )
            ),
            (ArrowTransformInput("source", pa.table({"value": [1, 2]}).to_reader()),),
        )
        assert result.payload_name is None
        assert result.receipt.row_count == 1
        assert result.batches[0].column(0).to_pylist() == [2]

        json_records = pa.table(
            {
                "record_index": [0, 1],
                "json_value": ['{"record_key":"r1"}', '{"record_key":"r2"}'],
            }
        )
        json_result = await worker.execute_query(
            PythonArrowQueryWorkerRequest(
                code=(
                    "import pyarrow as pa\n"
                    "def query(ctx):\n"
                    "    return pa.table({'record_count': [len(ctx.json('records'))]})\n"
                )
            ),
            (
                ArrowTransformInput(
                    "records",
                    json_records.to_reader(),
                    input_format="json",
                    json_kind="records",
                    row_count=2,
                ),
            ),
        )
        assert json_result.batches[0].column(0).to_pylist() == [2]

        with pytest.raises(DataTransformWorkerError) as unavailable_write:
            await worker.execute_query(
                PythonArrowQueryWorkerRequest(
                    code=(
                        "def query(ctx):\n"
                        "    ctx.write_table('result', ctx.table('source'))\n"
                        "    return ctx.table('source')\n"
                    )
                ),
                (ArrowTransformInput("source", pa.table({"value": [1]}).to_reader()),),
            )
        assert unavailable_write.value.code == "TRANSFORM_EXECUTION_FAILED"

        with pytest.raises(DataTransformWorkerError) as invalid_result:
            await worker.execute_query(
                PythonArrowQueryWorkerRequest(code="def query(ctx):\n    return {'count': 1}\n"),
                (ArrowTransformInput("source", pa.table({"value": [1]}).to_reader()),),
            )
        assert invalid_result.value.code == "TRANSFORM_PYTHON_INVALID"
    finally:
        await worker.close()
