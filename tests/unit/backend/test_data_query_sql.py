from __future__ import annotations

import base64
import asyncio
import threading
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from CAD2BIM_backend.application import data_query_sql_service as query_sql_module
from CAD2BIM_backend.application.data_query_deadline import (
    DATA_QUERY_TERMINAL_PUBLICATION_RESERVE_SECONDS,
)
from CAD2BIM_backend.application.data_query_sql_service import (
    DataQuerySqlApplicationError,
    DataQuerySqlService,
    _ResolvedInput,
    _row_schema,
)
from CAD2BIM_backend.application.data_query_attempt_service import (
    DataQueryAttemptApplicationError,
)
from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.data_query_sql import (
    DataQuerySqlQuery,
    DataQuerySqlRequest,
    QuerySqlResponseEncoding,
)
from CAD2BIM_backend.domain.data_query_attempt import DataQueryAttemptStatus
from CAD2BIM_backend.domain.invocation import InvocationScope, invocation_scope_digest
from CAD2BIM_backend.domain.data_transform import (
    DataTransformWorkerReceipt,
    DuckDbSqlExecution,
    TransformInputBinding,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.data_query.snapshot_cache import QuerySnapshotCache
from CAD2BIM_backend.infrastructure.data_query.snapshot_cache import (
    QuerySnapshotPolicy,
)
from CAD2BIM_backend.infrastructure.data_query import snapshot_cache as snapshot_cache_module
from CAD2BIM_backend.ports.data_transform import (
    DUCKDB_LOGICAL_TYPE_METADATA_KEY,
    ArrowTransformOutput,
)


pytestmark = pytest.mark.asyncio


def _cursor_key() -> str:
    return base64.urlsafe_b64encode(b"q" * 32).rstrip(b"=").decode("ascii")


def _scope(run_id: str = "run-query") -> InvocationScope:
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


def _query(page_size: int = 1) -> DataQuerySqlQuery:
    return DataQuerySqlQuery(
        inputs=(
            TransformInputBinding(
                input_name="entities",
                package_alias="data_001",
                payload_name="entities",
            ),
        ),
        execution=DuckDbSqlExecution(
            engine="duckdb_sql",
            code="SELECT entity_key FROM entities ORDER BY entity_key",
        ),
        page_size=page_size,
    )


def _schema(fields: list[tuple[str, pa.DataType, str, bool]]) -> pa.Schema:
    return pa.schema(
        [
            pa.field(
                name,
                arrow_type,
                nullable=nullable,
                metadata={DUCKDB_LOGICAL_TYPE_METADATA_KEY: logical_type.encode("utf-8")},
            )
            for name, arrow_type, logical_type, nullable in fields
        ]
    )


def _output(schema: pa.Schema, arrays: list[pa.Array]) -> ArrowTransformOutput:
    batch = pa.RecordBatch.from_arrays(arrays, schema=schema)
    return ArrowTransformOutput(
        receipt=DataTransformWorkerReceipt(
            row_count=batch.num_rows,
            arrow_nbytes=batch.nbytes,
            execution_ms=7,
        ),
        schema=schema,
        batches=(batch,),
        payload_name=None,
    )


class _Worker:
    def __init__(self, output: ArrowTransformOutput) -> None:
        self.output = output
        self.calls = 0

    async def execute(self, request, inputs, *, deadline=None):
        self.calls += 1
        assert request.result_mode == "query_final_result"
        assert request.output_names == ()
        assert len(inputs) == 1
        return (self.output,)


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


class _PublicationUncertainAttempts:
    def __init__(self) -> None:
        self.attempt = SimpleNamespace(
            attempt_id="dqa_publication_uncertain",
            status=DataQueryAttemptStatus.PREPARED,
        )
        self.fail_calls = 0

    async def prepare(self, *_args, **_kwargs):
        return SimpleNamespace(attempt=self.attempt)

    async def acquire_execution(self, *_args, **_kwargs):
        return self.attempt

    def release_execution(self, _attempt_id: str) -> None:
        return None

    def attempt_alias(self, *_args) -> str:
        return "cur.publication-uncertain"

    async def succeed(self, *_args, **_kwargs):
        raise DataQueryAttemptApplicationError(
            "DATA_QUERY_ATTEMPT_PUBLICATION_UNCERTAIN",
            "terminal publication is uncertain",
            phase="output_validation",
            retryable=True,
            attempt_alias=self.attempt_alias(),
        )

    async def fail(self, *_args, **_kwargs):
        self.fail_calls += 1
        raise AssertionError("publication uncertainty must not be rewritten as failure")


def _service(
    tmp_path: Path,
    output: ArrowTransformOutput,
) -> tuple[DataQuerySqlService, _Worker, QuerySnapshotCache]:
    input_path = tmp_path / "input.parquet"
    pq.write_table(pa.table({"entity_key": ["e1", "e2", "e3"]}), input_path)
    member = SimpleNamespace(
        member_id="member-entities",
        row_count=3,
        artifact=SimpleNamespace(
            artifact_id="artifact-entities",
            version_id="version-entities",
            sha256="a" * 64,
        ),
    )
    resolved = (
        _ResolvedInput(
            input_name="entities",
            package=SimpleNamespace(package=SimpleNamespace(package_id="package-source")),
            payload=SimpleNamespace(payload_name="entities", member=member),
        ),
    )
    worker = _Worker(output)
    snapshots = QuerySnapshotCache(root=tmp_path / "snapshots")
    service = DataQuerySqlService(
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


async def test_first_page_executes_once_and_continuations_only_read_snapshot(
    tmp_path: Path,
) -> None:
    schema = _schema([("entity_key", pa.string(), "VARCHAR", False)])
    service, worker, snapshots = _service(
        tmp_path,
        _output(schema, [pa.array(["e1", "e2", "e3"])]),
    )

    first = await _fresh_query(service, _scope(), DataQuerySqlRequest(query=_query(page_size=1)))
    cursor = first.page.next_cursor
    assert cursor is not None
    second = await service.query(_scope(), DataQuerySqlRequest(cursor=cursor))
    replay = await service.query(_scope(), DataQuerySqlRequest(cursor=cursor))

    assert [row.values for row in first.rows] == [{"entity_key": "e1"}]
    assert [row.values for row in second.rows] == [{"entity_key": "e2"}]
    assert second == replay
    assert worker.calls == 1
    assert first.query_statistics.model_dump() == {
        "bound_input_payload_count": 1,
        "bound_input_row_count": 3,
        "bound_input_stored_bytes": (tmp_path / "input.parquet").stat().st_size,
        "execution_ms": 7,
    }
    await snapshots.close()


async def test_first_page_requires_server_invocation_identity(tmp_path: Path) -> None:
    schema = _schema([("entity_key", pa.string(), "VARCHAR", False)])
    service, worker, snapshots = _service(
        tmp_path,
        _output(schema, [pa.array(["e1"])]),
    )

    with pytest.raises(DataQuerySqlApplicationError) as captured:
        await service.query(_scope(), DataQuerySqlRequest(query=_query()))

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
    schema = _schema([("entity_key", pa.string(), "VARCHAR", False)])
    service, _worker, snapshots = _service(
        tmp_path,
        _output(schema, [pa.array(["e1"])]),
    )
    prepared = await service._attempts.prepare(request_identity="terminal-failure")
    attempt = prepared.attempt

    async def discard(_attempt_id: str) -> None:
        if discard_mode == "error":
            raise OSError("snapshot unlink failed")
        await asyncio.Event().wait()

    monkeypatch.setattr(snapshots, "discard_for_attempt", discard)
    failure = DataQuerySqlApplicationError(
        "TRANSFORM_WORKER_FAILED",
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
    assert attempt.diagnostic.code == "TRANSFORM_WORKER_FAILED"
    assert failure.attempt_alias == service._attempts.attempt_alias(_scope(), attempt)
    await snapshots.close()


async def test_execution_deadline_reserves_failed_attempt_publication(
    tmp_path: Path,
) -> None:
    schema = _schema([("entity_key", pa.string(), "VARCHAR", False)])
    service, worker, snapshots = _service(
        tmp_path,
        _output(schema, [pa.array(["unused"])]),
    )

    async def hang(_request, _inputs, *, deadline=None):
        await asyncio.Event().wait()

    worker.execute = hang
    with pytest.raises(DataQuerySqlApplicationError) as captured:
        await _fresh_query(
            service,
            _scope(),
            DataQuerySqlRequest(query=_query()),
            deadline=(
                time.monotonic()
                + DATA_QUERY_TERMINAL_PUBLICATION_RESERVE_SECONDS
                + 0.02
            ),
        )

    attempt = next(iter(service._attempts.attempts.values()))
    assert captured.value.code == "TRANSFORM_TIMEOUT"
    assert captured.value.attempt_alias == service._attempts.attempt_alias(_scope(), attempt)
    assert attempt.status is DataQueryAttemptStatus.FAILED
    assert attempt.diagnostic.code == "TRANSFORM_TIMEOUT"
    await snapshots.close()


async def test_success_publication_uncertainty_keeps_snapshot_for_reconcile(
    tmp_path: Path,
) -> None:
    schema = _schema([("entity_key", pa.string(), "VARCHAR", False)])
    service, _worker, snapshots = _service(
        tmp_path,
        _output(schema, [pa.array(["e1", "e2"])]),
    )
    attempts = _PublicationUncertainAttempts()
    service._attempts = attempts

    with pytest.raises(DataQuerySqlApplicationError) as captured:
        await service.fresh(
            _scope(),
            _query(page_size=1),
            request_identity="request-publication-uncertain",
        )

    assert captured.value.code == "DATA_QUERY_ATTEMPT_PUBLICATION_UNCERTAIN"
    assert attempts.fail_calls == 0
    assert await snapshots.available_for_attempt(
        attempts.attempt.attempt_id,
        scope_digest=invocation_scope_digest(_scope()),
    )
    await snapshots.close()


async def test_cursor_is_scope_bound_and_restart_expires_snapshot(tmp_path: Path) -> None:
    schema = _schema([("entity_key", pa.string(), "VARCHAR", False)])
    service, _worker, snapshots = _service(
        tmp_path,
        _output(schema, [pa.array(["e1", "e2"])]),
    )
    first = await _fresh_query(service, _scope(), DataQuerySqlRequest(query=_query(page_size=1)))
    assert first.page.next_cursor is not None

    with pytest.raises(DataQuerySqlApplicationError) as wrong_scope:
        await service.query(
            _scope("run-other"),
            DataQuerySqlRequest(cursor=first.page.next_cursor),
        )
    assert wrong_scope.value.code == "CURSOR_SCOPE_MISMATCH"

    restarted_cache = QuerySnapshotCache(root=tmp_path / "restarted")
    restarted = DataQuerySqlService(
        service._payloads,
        service._worker,
        CursorCodec(_cursor_key()),
        restarted_cache,
        service._attempts,
    )
    with pytest.raises(DataQuerySqlApplicationError) as expired:
        await restarted.query(
            _scope(),
            DataQuerySqlRequest(cursor=first.page.next_cursor),
        )
    assert expired.value.code == "QUERY_CURSOR_EXPIRED"
    assert expired.value.retryable is False
    await snapshots.close()
    await restarted_cache.close()


async def test_complex_values_preserve_logical_type_and_approved_encoding(
    tmp_path: Path,
) -> None:
    schema = _schema(
        [
            ("flag", pa.bool_(), "BOOLEAN", False),
            ("count", pa.int64(), "BIGINT", False),
            ("cost", pa.decimal128(38, 6), "DECIMAL(38,6)", False),
            ("blob_value", pa.binary(), "BLOB", False),
            ("document", pa.string(), "JSON", False),
            ("neighbors", pa.list_(pa.int32()), "INTEGER[]", False),
            ("ratio", pa.float64(), "DOUBLE", False),
        ]
    )
    output = _output(
        schema,
        [
            pa.array([True]),
            pa.array([3], type=pa.int64()),
            pa.array([Decimal("12345678901234567890.123456")], type=pa.decimal128(38, 6)),
            pa.array([b"ab"]),
            pa.array(['{"a":1}']),
            pa.array([[1, 2]], type=pa.list_(pa.int32())),
            pa.array([float("nan")]),
        ],
    )
    service, _worker, snapshots = _service(tmp_path, output)

    result = await _fresh_query(service, _scope(), DataQuerySqlRequest(query=_query(page_size=10)))

    assert [column.logical_type for column in result.row_schema.columns] == [
        "BOOLEAN",
        "BIGINT",
        "DECIMAL(38,6)",
        "BLOB",
        "JSON",
        "INTEGER[]",
        "DOUBLE",
    ]
    assert [column.response_encoding for column in result.row_schema.columns] == [
        QuerySqlResponseEncoding.JSON_BOOLEAN,
        QuerySqlResponseEncoding.JSON_INTEGER,
        QuerySqlResponseEncoding.DECIMAL_STRING,
        QuerySqlResponseEncoding.BASE64,
        QuerySqlResponseEncoding.JSON_TEXT,
        QuerySqlResponseEncoding.JSON_TEXT,
        QuerySqlResponseEncoding.SPECIAL_FLOAT_STRING,
    ]
    assert result.rows[0].values == {
        "flag": True,
        "count": 3,
        "cost": "12345678901234567890.123456",
        "blob_value": "YWI=",
        "document": '{"a":1}',
        "neighbors": "[1,2]",
        "ratio": "NaN",
    }
    await snapshots.close()


async def test_struct_and_all_special_floats_use_provider_safe_text(
    tmp_path: Path,
) -> None:
    struct_type = pa.struct(
        [
            pa.field("name", pa.string()),
            pa.field("weight", pa.float64()),
        ]
    )
    schema = _schema(
        [
            ("detail", struct_type, "STRUCT(name VARCHAR, weight DOUBLE)", False),
            ("not_a_number", pa.float64(), "DOUBLE", False),
            ("positive_infinity", pa.float64(), "DOUBLE", False),
            ("negative_infinity", pa.float64(), "DOUBLE", False),
            ("finite", pa.float64(), "DOUBLE", False),
        ]
    )
    service, _worker, snapshots = _service(
        tmp_path,
        _output(
            schema,
            [
                pa.array([{"name": "node", "weight": 1.5}], type=struct_type),
                pa.array([float("nan")]),
                pa.array([float("inf")]),
                pa.array([float("-inf")]),
                pa.array([1.25]),
            ],
        ),
    )

    result = await _fresh_query(service, _scope(), DataQuerySqlRequest(query=_query(page_size=1)))

    assert [column.response_encoding for column in result.row_schema.columns] == [
        QuerySqlResponseEncoding.JSON_TEXT,
        QuerySqlResponseEncoding.SPECIAL_FLOAT_STRING,
        QuerySqlResponseEncoding.SPECIAL_FLOAT_STRING,
        QuerySqlResponseEncoding.SPECIAL_FLOAT_STRING,
        QuerySqlResponseEncoding.SPECIAL_FLOAT_STRING,
    ]
    assert result.rows[0].values == {
        "detail": '{"name":"node","weight":1.5}',
        "not_a_number": "NaN",
        "positive_infinity": "Infinity",
        "negative_infinity": "-Infinity",
        "finite": 1.25,
    }
    await snapshots.close()


async def test_bignum_opaque_arrow_value_requires_explicit_varchar_cast(
    tmp_path: Path,
) -> None:
    schema = _schema([("huge_value", pa.binary(), "BIGNUM", False)])
    service, _worker, snapshots = _service(
        tmp_path,
        _output(schema, [pa.array([b"opaque-bignum"])]),
    )

    with pytest.raises(DataQuerySqlApplicationError) as invalid:
        await _fresh_query(service, _scope(), DataQuerySqlRequest(query=_query(page_size=1)))

    assert invalid.value.code == "TRANSFORM_OUTPUT_INVALID"
    assert invalid.value.phase == "output_validation"
    assert "CAST" in str(invalid.value)
    assert "VARCHAR" in str(invalid.value)
    await snapshots.close()


async def test_union_logical_type_requires_explicit_stable_projection(
    tmp_path: Path,
) -> None:
    union_type = pa.dense_union(
        [
            pa.field("as_integer", pa.int32()),
            pa.field("as_text", pa.string()),
        ]
    )
    union_value = pa.UnionArray.from_dense(
        pa.array([0], type=pa.int8()),
        pa.array([0], type=pa.int32()),
        [pa.array([7], type=pa.int32()), pa.array([], type=pa.string())],
        field_names=["as_integer", "as_text"],
    )
    schema = _schema(
        [("ambiguous", union_type, "UNION(as_integer INTEGER, as_text VARCHAR)", False)]
    )
    service, _worker, snapshots = _service(
        tmp_path,
        _output(schema, [union_value]),
    )

    with pytest.raises(DataQuerySqlApplicationError) as invalid:
        await _fresh_query(service, _scope(), DataQuerySqlRequest(query=_query(page_size=1)))

    assert invalid.value.code == "TRANSFORM_OUTPUT_INVALID"
    assert invalid.value.phase == "output_validation"
    assert "UNION" in str(invalid.value)
    assert "CAST" in str(invalid.value)
    await snapshots.close()


async def test_float_schema_selection_does_not_scan_result_batches() -> None:
    class _ExplodingBatches:
        def __iter__(self):
            raise AssertionError("schema selection must not scan result values")

    schema = _schema([("ratio", pa.float64(), "DOUBLE", False)])
    output = SimpleNamespace(schema=schema, batches=_ExplodingBatches())

    row_schema = _row_schema(output)

    assert row_schema.columns[0].response_encoding is (
        QuerySqlResponseEncoding.SPECIAL_FLOAT_STRING
    )


async def test_response_byte_budget_ends_page_early_without_losing_rows(tmp_path: Path) -> None:
    schema = _schema([("value", pa.string(), "VARCHAR", False)])
    large = "x" * 450_000
    service, worker, snapshots = _service(
        tmp_path,
        _output(schema, [pa.array([large + "1", large + "2", large + "3"])]),
    )

    first = await _fresh_query(service, _scope(), DataQuerySqlRequest(query=_query(page_size=3)))
    second = await service.query(_scope(), DataQuerySqlRequest(cursor=first.page.next_cursor))

    assert first.page.returned_count == 2
    assert first.page.remaining_count == 1
    assert second.page.returned_count == 1
    assert second.page.complete is True
    assert len(first.model_dump_json().encode("utf-8")) <= 1024 * 1024
    assert worker.calls == 1
    await snapshots.close()


async def test_complex_cell_over_256_kib_fails_before_json_parse(
    tmp_path: Path,
    monkeypatch,
) -> None:
    schema = _schema([("document", pa.string(), "JSON", False)])
    value = '{"value":"' + "x" * (256 * 1024) + '"}'
    service, _worker, snapshots = _service(
        tmp_path,
        _output(schema, [pa.array([value])]),
    )
    parsed = False

    def forbidden_parse(_value):
        nonlocal parsed
        parsed = True
        raise AssertionError("oversized JSON must be rejected before parsing")

    monkeypatch.setattr(query_sql_module.json, "loads", forbidden_parse)

    with pytest.raises(DataQuerySqlApplicationError) as oversized:
        await _fresh_query(service, _scope(), DataQuerySqlRequest(query=_query(page_size=1)))

    assert oversized.value.code == "QUERY_RESULT_VALUE_TOO_LARGE"
    assert oversized.value.phase == "output_validation"
    assert parsed is False
    await snapshots.close()


async def test_huge_list_encoding_is_bounded_and_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    list_type = pa.list_(pa.int32())
    schema = _schema([("items", list_type, "INTEGER[]", False)])
    service, _worker, snapshots = _service(
        tmp_path,
        _output(schema, [pa.array([list(range(100_000))], type=list_type)]),
    )
    main_thread = threading.get_ident()
    encoding_threads: list[int] = []
    original = query_sql_module._encoded_row

    def observed_encode(*args):
        encoding_threads.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(query_sql_module, "_encoded_row", observed_encode)
    with pytest.raises(DataQuerySqlApplicationError) as oversized:
        await _fresh_query(service, _scope(), DataQuerySqlRequest(query=_query(page_size=1)))

    assert oversized.value.code == "QUERY_RESULT_VALUE_TOO_LARGE"
    assert encoding_threads
    assert all(thread_id != main_thread for thread_id in encoding_threads)
    await snapshots.close()


async def test_huge_blob_fails_before_copy_or_base64_encoding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    schema = _schema([("payload", pa.binary(), "BLOB", False)])
    service, _worker, snapshots = _service(
        tmp_path,
        _output(schema, [pa.array([b"x" * 300_000])]),
    )
    encoded = False

    def forbidden_encode(_value):
        nonlocal encoded
        encoded = True
        raise AssertionError("oversized BLOB must be rejected before base64 encoding")

    monkeypatch.setattr(
        query_sql_module,
        "base64",
        SimpleNamespace(b64encode=forbidden_encode),
    )
    with pytest.raises(DataQuerySqlApplicationError) as oversized:
        await _fresh_query(service, _scope(), DataQuerySqlRequest(query=_query(page_size=1)))

    assert oversized.value.code == "QUERY_RESULT_VALUE_TOO_LARGE"
    assert oversized.value.phase == "output_validation"
    assert encoded is False
    await snapshots.close()


async def test_simple_scalar_row_over_512_kib_fails_separately_from_complex_cell(
    tmp_path: Path,
) -> None:
    schema = _schema(
        [
            ("left", pa.string(), "VARCHAR", False),
            ("right", pa.string(), "VARCHAR", False),
        ]
    )
    service, _worker, snapshots = _service(
        tmp_path,
        _output(
            schema,
            [
                pa.array(["l" * 270_000]),
                pa.array(["r" * 270_000]),
            ],
        ),
    )

    with pytest.raises(DataQuerySqlApplicationError) as oversized:
        await _fresh_query(service, _scope(), DataQuerySqlRequest(query=_query(page_size=1)))

    assert oversized.value.code == "QUERY_RESULT_VALUE_TOO_LARGE"
    assert oversized.value.phase == "output_validation"
    await snapshots.close()


async def test_deep_json_returns_stable_output_validation_error(tmp_path: Path) -> None:
    schema = _schema([("document", pa.string(), "JSON", False)])
    deeply_nested = "[" * 1_100 + "0" + "]" * 1_100
    service, _worker, snapshots = _service(
        tmp_path,
        _output(schema, [pa.array([deeply_nested])]),
    )

    with pytest.raises(DataQuerySqlApplicationError) as invalid:
        await _fresh_query(service, _scope(), DataQuerySqlRequest(query=_query(page_size=1)))

    assert invalid.value.code == "TRANSFORM_OUTPUT_INVALID"
    assert invalid.value.phase == "output_validation"
    assert invalid.value.retryable is False
    await snapshots.close()


async def test_signed_cursor_claims_are_bound_to_snapshot_metadata_and_range(
    tmp_path: Path,
) -> None:
    schema = _schema([("entity_key", pa.string(), "VARCHAR", False)])
    service, _worker, snapshots = _service(
        tmp_path,
        _output(schema, [pa.array(["e1", "e2"])]),
    )
    first = await _fresh_query(service, _scope(), DataQuerySqlRequest(query=_query(page_size=1)))
    claims = service._cursor_codec.decode(first.page.next_cursor)
    mutations = {
        "exact_input": "wrong-input",
        "query_fingerprint": "wrong-query",
        "row_schema": "wrong-schema",
        "page_size": 2,
        "next_row": claims["total"] + 1,
        "total": claims["total"] + 1,
    }

    for name, value in mutations.items():
        malformed = dict(claims)
        malformed[name] = value
        cursor = service._cursor_codec.encode(malformed)
        with pytest.raises(DataQuerySqlApplicationError) as rejected:
            await service.query(_scope(), DataQuerySqlRequest(cursor=cursor))
        assert rejected.value.code == "CURSOR_INVALID", name

    await snapshots.close()


async def test_snapshot_storage_failure_is_retryable_output_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    schema = _schema([("entity_key", pa.string(), "VARCHAR", False)])
    service, _worker, snapshots = _service(
        tmp_path,
        _output(schema, [pa.array(["e1"])]),
    )

    def fail_write(*_args):
        raise OSError("private path omitted")

    monkeypatch.setattr(snapshot_cache_module, "_write_snapshot", fail_write)
    with pytest.raises(DataQuerySqlApplicationError) as unavailable:
        await _fresh_query(service, _scope(), DataQuerySqlRequest(query=_query()))

    assert unavailable.value.code == "TRANSFORM_SERVICE_UNAVAILABLE"
    assert unavailable.value.phase == "output_validation"
    assert unavailable.value.retryable is True
    assert "private path omitted" not in str(unavailable.value)
    await snapshots.close()


async def test_snapshot_capacity_wait_obeys_total_deadline_and_cleans_candidate(
    tmp_path: Path,
) -> None:
    schema = _schema([("entity_key", pa.string(), "VARCHAR", False)])
    service, worker, snapshots = _service(
        tmp_path,
        _output(schema, [pa.array(["e1", "e2"])]),
    )
    snapshots._policy = QuerySnapshotPolicy(
        per_scope_snapshots=1,
        process_snapshots=1,
    )
    first = await _fresh_query(service, _scope(), DataQuerySqlRequest(query=_query(page_size=1)))
    claims = service._cursor_codec.decode(first.page.next_cursor)
    lease = await snapshots.acquire(claims["snapshot"])
    try:
        with pytest.raises(DataQuerySqlApplicationError) as timeout:
            await _fresh_query(
                service,
                _scope(),
                DataQuerySqlRequest(query=_query(page_size=1)),
                deadline=(
                    time.monotonic()
                    + DATA_QUERY_TERMINAL_PUBLICATION_RESERVE_SECONDS
                    + 0.03
                ),
            )
    finally:
        await lease.release()

    assert timeout.value.code == "TRANSFORM_TIMEOUT"
    assert timeout.value.phase == "output_validation"
    assert worker.calls == 2
    assert len(list((tmp_path / "snapshots").glob("*.arrow"))) == 1
    await snapshots.close()
