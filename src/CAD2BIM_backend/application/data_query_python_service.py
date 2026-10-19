"""Bounded exploratory Python/Arrow analysis over exact Package Payloads."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pyarrow as pa
from CAD2BIM_backend.domain.runtime_execution import DATA_PYTHON_EXECUTION
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.application.data_query_sql_service import DataQuerySqlApplicationError, _encoded_row
from CAD2BIM_backend.application.data_query_attempt_service import (
    DataQueryAttemptApplicationError,
    DataQueryAttemptService,
)
from CAD2BIM_backend.application.data_query_deadline import data_query_execution_deadline
from CAD2BIM_backend.application.data_transform_service import (
    DataTransformApplicationError,
    _all_arrow_inputs,
    _resolved_members,
)
from CAD2BIM_backend.application.exact_payload_resolver import (
    ExactJsonPayload,
    ExactParquetPayload,
    ExactPayloadResolutionError,
    ExactPayloadResolver,
    ExactTransformPayload,
)
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.data_query import PageInfo
from CAD2BIM_backend.domain.data_query_attempt import (
    DataQueryAttemptRecord,
    DataQueryAttemptStatus,
    DataQueryEngine,
    DataQueryExactInputRef,
    DataQueryResultSummary,
)
from CAD2BIM_backend.domain.data_query_python import (
    DataQueryPythonQuery,
    DataQueryPythonRequest,
    DataQueryPythonResult,
    PythonArrowQueryWorkerRequest,
    QueryPythonStatistics,
)
from CAD2BIM_backend.domain.data_query_sql import (
    QuerySqlColumn,
    QuerySqlResponseEncoding,
    QuerySqlRow,
    QuerySqlRowSchema,
)
from CAD2BIM_backend.domain.data_runtime import PublishedDataPackage
from CAD2BIM_backend.domain.invocation import InvocationScope, invocation_scope_digest
from CAD2BIM_backend.domain.data_transform import (
    DataTransformWorkerError,
    DataTransformWorkerParameter,
    InlineTable,
    TransformParameter,
)
from CAD2BIM_backend.infrastructure.data_query import CursorCodec, CursorCodecError
from CAD2BIM_backend.infrastructure.data_query.snapshot_cache import (
    QuerySnapshotCache,
    QuerySnapshotCacheError,
    QuerySnapshotLease,
    QuerySnapshotRecord,
    QuerySnapshotWrite,
)
from CAD2BIM_backend.ports.data_transform import ArrowTransformOutput, PythonArrowQueryWorkerPort
from CAD2BIM_backend.domain.text_source import SourceEdit


_TOOL_NAME = "data_query_python"
_FIRST_PAGE_TIMEOUT_SECONDS = DATA_PYTHON_EXECUTION.total_seconds
_CONTINUATION_TIMEOUT_SECONDS = 15.0
_INPUT_ROW_LIMIT = 4_000_000
_INPUT_STORED_BYTES = 256 * 1024 * 1024
_RESPONSE_BYTES = 1024 * 1024
_ROW_BYTES = 512 * 1024
_MAX_ERROR_BYTES = 2_000


class DataQueryPythonApplicationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: Literal[
            "read", "preflight", "code_validation", "execution", "output_validation"
        ] = "read",
        retryable: bool = False,
        attempt_alias: str | None = None,
    ) -> None:
        super().__init__(_bounded_utf8(message.strip() or "Python query failed", _MAX_ERROR_BYTES))
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.attempt_alias = attempt_alias


@dataclass(frozen=True, slots=True)
class _ResolvedInput:
    request_name: str
    payload: ExactTransformPayload
    package_id: str
    package: PublishedDataPackage


@dataclass(frozen=True, slots=True)
class _PythonExecutionFacts:
    source_code: str
    parameters: tuple[TransformParameter, ...]
    inline_tables: tuple[InlineTable, ...]
    page_size: int


class DataQueryPythonService:
    """Execute Python once, then page the immutable process-private Arrow snapshot."""

    def __init__(
        self,
        payloads: ExactPayloadResolver,
        worker: PythonArrowQueryWorkerPort,
        cursor_codec: CursorCodec,
        snapshots: QuerySnapshotCache,
        attempts: DataQueryAttemptService,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._payloads = payloads
        self._worker = worker
        self._cursor_codec = cursor_codec
        self._snapshots = snapshots
        self._attempts = attempts
        self._clock = clock

    async def query(
        self,
        scope: InvocationScope,
        request: DataQueryPythonRequest,
        *,
        deadline: float | None = None,
        request_identity: str | None = None,
    ) -> DataQueryPythonResult:
        if not isinstance(request, DataQueryPythonRequest):
            raise DataQueryPythonApplicationError(
                "INVALID_ARGUMENT", "The Python query request is invalid", phase="preflight"
            )
        if deadline is None:
            timeout = (
                _FIRST_PAGE_TIMEOUT_SECONDS
                if request.query is not None
                else _CONTINUATION_TIMEOUT_SECONDS
            )
            deadline = self._clock() + timeout
        if request.query is not None:
            if request_identity is None:
                raise DataQueryPythonApplicationError(
                    "SYSTEM_PAUSED",
                    "The server could not establish a stable Python query invocation identity",
                    phase="preflight",
                )
            return await self._first_page(
                scope, request.query, deadline, request_identity=request_identity
            )
        assert request.cursor is not None
        return await self._continuation(scope, request.cursor, deadline)

    async def fresh(
        self,
        scope: InvocationScope,
        query: DataQueryPythonQuery,
        *,
        request_identity: str,
        deadline: float | None = None,
    ) -> DataQueryPythonResult:
        deadline = self._clock() + _FIRST_PAGE_TIMEOUT_SECONDS if deadline is None else deadline
        return await self._first_page(scope, query, deadline, request_identity=request_identity)

    async def repair(
        self,
        scope: InvocationScope,
        *,
        parent_attempt_alias: str,

        edits: Sequence[SourceEdit],
        page_size: int,
        request_identity: str,
        deadline: float | None = None,
    ) -> DataQueryPythonResult:
        deadline = self._clock() + _FIRST_PAGE_TIMEOUT_SECONDS if deadline is None else deadline
        attempts = self._attempts
        try:
            parent, source = await _before_deadline(
                attempts.repair_source(
                    scope,
                    engine=DataQueryEngine.PYTHON_ARROW,
                    parent_attempt_alias=parent_attempt_alias,

                    edits=edits,
                ),
                self._clock,
                deadline,
                "preflight",
            )
            parameters = TypeAdapter(tuple[TransformParameter, ...]).validate_python(
                parent.parameters
            )
            inline_tables = TypeAdapter(tuple[InlineTable, ...]).validate_python(
                parent.inline_tables
            )
        except DataQueryAttemptApplicationError as exc:
            raise _attempt_error(exc) from None
        except ValidationError as exc:
            raise DataQueryPythonApplicationError(
                "DATA_QUERY_REPAIR_INVALID",
                "The persisted Python repair request is invalid",
                phase="preflight",
                attempt_alias=parent_attempt_alias,
            ) from exc
        resolved = await self._resolve_repair_inputs(scope, parent, deadline)
        return await self._execute_resolved(
            scope,
            _PythonExecutionFacts(
                source_code=source,
                parameters=parameters,
                inline_tables=inline_tables,
                page_size=page_size,
            ),
            resolved,
            deadline,
            request_identity=request_identity,
            repair_parent=parent,
        )

    async def _first_page(
        self,
        scope: InvocationScope,
        query: DataQueryPythonQuery,
        deadline: float,
        *,
        request_identity: str,
        repair_parent: DataQueryAttemptRecord | None = None,
    ) -> DataQueryPythonResult:
        _check_deadline(self._clock, deadline, "preflight")
        resolved = await self._resolve_inputs(scope, query, deadline)
        return await self._execute_resolved(
            scope,
            _PythonExecutionFacts(
                source_code=query.code,
                parameters=query.parameters,
                inline_tables=query.inline_tables,
                page_size=query.page_size,
            ),
            resolved,
            deadline,
            request_identity=request_identity,
            repair_parent=repair_parent,
        )

    async def _execute_resolved(
        self,
        scope: InvocationScope,
        execution: _PythonExecutionFacts,
        resolved: tuple[_ResolvedInput, ...],
        deadline: float,
        *,
        request_identity: str,
        repair_parent: DataQueryAttemptRecord | None,
    ) -> DataQueryPythonResult:
        exact_input_digest = _exact_input_digest(resolved)
        query_fingerprint = _query_fingerprint(execution, exact_input_digest)
        attempt: DataQueryAttemptRecord
        execution_claimed = False
        try:
            prepared = await _before_deadline(
                self._attempts.prepare(
                    scope,
                    engine=DataQueryEngine.PYTHON_ARROW,
                    source_code=execution.source_code,
                    inputs=_attempt_inputs(resolved),
                    parameters=tuple(
                        item.model_dump(mode="json") for item in execution.parameters
                    ),
                    inline_tables=tuple(
                        item.model_dump(mode="json") for item in execution.inline_tables
                    ),
                    page_size=execution.page_size,
                    request_identity=request_identity,
                    repair_parent_attempt_id=(
                        None if repair_parent is None else repair_parent.attempt_id
                    ),
                ),
                self._clock,
                deadline,
                "preflight",
            )
            attempt = prepared.attempt
            try:
                attempt = await self._attempts.acquire_execution(
                    scope,
                    attempt.attempt_id,
                    wait_timeout_seconds=max(0.0, deadline - self._clock()),
                )
            except DataQueryAttemptApplicationError as exc:
                failure = _attempt_error(exc)
                if failure.attempt_alias is None:
                    failure.attempt_alias = self._attempts.attempt_alias(scope, attempt)
                raise failure from None
            execution_claimed = True
            if attempt.status is not DataQueryAttemptStatus.PREPARED:
                try:
                    return await self._replay_attempt(scope, attempt, deadline)
                finally:
                    self._attempts.release_execution(attempt.attempt_id)
                    execution_claimed = False
        except DataQueryAttemptApplicationError as exc:
            raise _attempt_error(exc) from None
        execution_deadline = data_query_execution_deadline(deadline)
        materialized: dict[str, Path] = {}
        success_publication_started = False
        try:
            materialized = await _before_deadline(
                self._payloads.materialize_members(
                    _resolved_members(resolved),
                    check_budget=lambda: _check_deadline(
                        self._clock, execution_deadline, "preflight"
                    ),
                    max_total_bytes=_INPUT_STORED_BYTES,
                ),
                self._clock,
                execution_deadline,
                "preflight",
            )
            try:
                worker_inputs = await _before_deadline(
                    asyncio.to_thread(_all_arrow_inputs, resolved, materialized, execution),
                    self._clock,
                    execution_deadline,
                    "preflight",
                )
                bound_input_rows = sum(
                    int(item.row_count or 0) for item in worker_inputs[: len(resolved)]
                )
                bound_inline_rows = sum(len(item.rows) for item in execution.inline_tables)
                if bound_input_rows + bound_inline_rows > _INPUT_ROW_LIMIT:
                    raise DataQueryPythonApplicationError(
                        "QUERY_INPUT_BUDGET_EXCEEDED",
                        "Exact and inline input rows exceed the 4,000,000-row Python query budget",
                        phase="preflight",
                    )
                statistics = QueryPythonStatistics(
                    bound_input_payload_count=len(resolved),
                    bound_input_row_count=bound_input_rows,
                    bound_input_stored_bytes=sum(
                        materialized[item.payload.member.member_id].stat().st_size
                        for item in resolved
                    ),
                    bound_inline_table_count=len(execution.inline_tables),
                    bound_inline_row_count=bound_inline_rows,
                    execution_ms=0,
                )
                output = await _before_deadline(
                    self._worker.execute_query(
                        PythonArrowQueryWorkerRequest(
                            code=execution.source_code,
                            parameters=tuple(
                                DataTransformWorkerParameter(name=item.name, value=item.value)
                                for item in execution.parameters
                            ),
                        ),
                        worker_inputs,
                        deadline=deadline,
                    ),
                    self._clock,
                    execution_deadline,
                    "execution",
                )
            except DataTransformApplicationError as exc:
                raise _map_transform_error(exc.code, str(exc), phase=exc.phase) from None
            except DataTransformWorkerError as exc:
                raise _map_transform_error(
                    exc.code,
                    str(exc),
                    phase=exc.phase,
                    execution_started=exc.execution_started,
                    retryable=exc.retryable,
                ) from None
            row_schema = _python_row_schema(output)
            statistics = statistics.model_copy(update={"execution_ms": output.receipt.execution_ms})
            scope_digest = invocation_scope_digest(scope)
            try:
                record = await _before_deadline(
                    self._snapshots.create(
                        QuerySnapshotWrite(
                            attempt_id=attempt.attempt_id,
                            scope_digest=scope_digest,
                            exact_input_digest=exact_input_digest,
                            query_fingerprint=query_fingerprint,
                            row_schema=row_schema,
                            page_size=execution.page_size,
                            query_statistics=statistics,
                            arrow_schema=output.schema,
                            batches=output.batches,
                        ),
                        row_schema_digest=_row_schema_digest(row_schema),
                    ),
                    self._clock,
                    execution_deadline,
                    "output_validation",
                )
            except QuerySnapshotCacheError as exc:
                code = (
                    exc.code
                    if exc.code == "QUERY_RESULT_SNAPSHOT_TOO_LARGE"
                    else "QUERY_SERVICE_UNAVAILABLE"
                )
                raise DataQueryPythonApplicationError(
                    code,
                    str(exc),
                    phase="output_validation",
                    retryable=code == "QUERY_SERVICE_UNAVAILABLE",
                ) from None
            result = await self._page_from_snapshot(
                scope_digest, record, 0, execution_deadline
            )
            try:
                success_publication_started = True
                attempt = await self._attempts.succeed(
                    scope,
                    attempt,
                    DataQueryResultSummary(
                        exact_input_digest=record.exact_input_digest,
                        query_fingerprint=record.query_fingerprint,
                        row_schema_digest=record.row_schema_digest,
                        snapshot_sha256=record.snapshot_sha256,
                        row_count=record.total_rows,
                        snapshot_size_bytes=record.size_bytes,
                    ),
                    publication_timeout_seconds=max(0.0, deadline - self._clock()),
                )
            except DataQueryAttemptApplicationError as exc:
                raise _attempt_error(exc) from None
            return result
        except DataQueryPythonApplicationError as exc:
            if (
                attempt.status is DataQueryAttemptStatus.PREPARED
                and not success_publication_started
            ):
                await self._terminalize_failure(scope, attempt, exc, deadline)
            raise
        except ExactPayloadResolutionError as exc:
            if exc.code == "TRANSFORM_INPUT_BUDGET_EXCEEDED":
                failure = DataQueryPythonApplicationError(
                    "QUERY_INPUT_BUDGET_EXCEEDED", str(exc), phase="preflight"
                )
            else:
                failure = DataQueryPythonApplicationError(exc.code, str(exc), phase="preflight")
            if attempt.status is DataQueryAttemptStatus.PREPARED:
                await self._terminalize_failure(scope, attempt, failure, deadline)
            raise failure from None
        except OSError as exc:
            failure = DataQueryPythonApplicationError(
                "QUERY_SERVICE_UNAVAILABLE",
                "The Python query input or snapshot storage is unavailable",
                phase="preflight",
                retryable=True,
            )
            if attempt.status is DataQueryAttemptStatus.PREPARED:
                await self._terminalize_failure(scope, attempt, failure, deadline)
            raise failure from exc
        except Exception as exc:
            failure = DataQueryPythonApplicationError(
                "QUERY_SERVICE_UNAVAILABLE",
                "The Python query service failed before producing a valid result",
                phase="execution",
            )
            if attempt.status is DataQueryAttemptStatus.PREPARED:
                await self._terminalize_failure(scope, attempt, failure, deadline)
            raise failure from exc
        finally:
            self._payloads.remove_paths(materialized.values())
            if execution_claimed:
                self._attempts.release_execution(attempt.attempt_id)

    async def _terminalize_failure(
        self,
        scope: InvocationScope,
        attempt: DataQueryAttemptRecord,
        failure: DataQueryPythonApplicationError,
        deadline: float,
    ) -> None:
        persisted_error: DataQueryPythonApplicationError | None = None
        try:
            await self._attempts.fail(
                scope,
                attempt,
                code=failure.code,
                message=str(failure),
                phase=failure.phase,
                retryable=failure.retryable,
                publication_timeout_seconds=max(0.0, deadline - self._clock()),
            )
        except DataQueryAttemptApplicationError as persisted:
            persisted_error = _attempt_error(persisted)
        else:
            failure.attempt_alias = self._attempts.attempt_alias(scope, attempt)
        await self._snapshots.discard_for_attempt_best_effort(
            attempt.attempt_id,
            timeout_seconds=max(0.0, deadline - self._clock()),
        )
        if persisted_error is not None:
            raise persisted_error

    async def _resolve_inputs(
        self,
        scope: InvocationScope,
        query: DataQueryPythonQuery,
        deadline: float,
    ) -> tuple[_ResolvedInput, ...]:
        result: list[_ResolvedInput] = []
        seen_members: set[tuple[str, str]] = set()
        cache: dict[str, tuple[PublishedDataPackage, Mapping[str, ExactTransformPayload]]] = {}
        total_rows = sum(len(item.rows) for item in query.inline_tables)
        for binding in query.inputs:
            _check_deadline(self._clock, deadline, "preflight")
            cached = cache.get(binding.package_alias)
            if cached is None:
                package = await _before_deadline(
                    self._payloads.package_by_alias(scope, binding.package_alias),
                    self._clock,
                    deadline,
                    "preflight",
                )
                payloads = self._payloads.transform_payloads(package)
                cache[binding.package_alias] = package, payloads
            else:
                package, payloads = cached
            payload = payloads.get(binding.payload_name)
            if payload is None:
                raise DataQueryPythonApplicationError(
                    "PAYLOAD_NOT_FOUND",
                    f"Payload {binding.payload_name!r} was not found in {binding.package_alias}",
                    phase="preflight",
                )
            exact = (payload.member.artifact.version_id, payload.member.member_id)
            if exact in seen_members:
                raise DataQueryPythonApplicationError(
                    "INVALID_ARGUMENT",
                    "The same exact Payload cannot be bound more than once",
                    phase="preflight",
                )
            seen_members.add(exact)
            if isinstance(payload, ExactParquetPayload):
                total_rows += int(payload.member.row_count or 0)
            result.append(
                _ResolvedInput(
                    request_name=binding.input_name,
                    payload=payload,
                    package_id=package.package.package_id,
                    package=package,
                )
            )
        if total_rows > _INPUT_ROW_LIMIT:
            raise DataQueryPythonApplicationError(
                "QUERY_INPUT_BUDGET_EXCEEDED",
                "Exact and inline input rows exceed the 4,000,000-row Python query budget",
                phase="preflight",
            )
        return tuple(result)

    async def _resolve_repair_inputs(
        self,
        scope: InvocationScope,
        parent: DataQueryAttemptRecord,
        deadline: float,
    ) -> tuple[_ResolvedInput, ...]:
        result: list[_ResolvedInput] = []
        for item in parent.inputs:
            _check_deadline(self._clock, deadline, "preflight")
            try:
                package, payload = await _before_deadline(
                    self._payloads.payload_by_exact_member(
                        scope,
                        package_id=item.package_id,
                        payload_member_id=item.payload_member_id,
                        schema_member_id=item.schema_member_id,
                    ),
                    self._clock,
                    deadline,
                    "preflight",
                )
            except ExactPayloadResolutionError as exc:
                raise DataQueryPythonApplicationError(
                    exc.code, str(exc), phase="preflight"
                ) from None
            result.append(
                _ResolvedInput(
                    request_name=item.input_name,
                    payload=payload,
                    package_id=package.package.package_id,
                    package=package,
                )
            )
        return tuple(result)

    async def _continuation(
        self, scope: InvocationScope, cursor: str, deadline: float
    ) -> DataQueryPythonResult:
        _check_deadline(self._clock, deadline, "read")
        try:
            claims = self._cursor_codec.decode(cursor)
        except CursorCodecError as exc:
            raise DataQueryPythonApplicationError(
                "CURSOR_INVALID", "The query cursor is invalid"
            ) from exc
        if claims.get("tool") != _TOOL_NAME:
            if claims.get("tool") not in {None, _TOOL_NAME}:
                raise DataQueryPythonApplicationError(
                    "CURSOR_TOOL_MISMATCH", "The cursor belongs to a different tool"
                )
            raise DataQueryPythonApplicationError("CURSOR_INVALID", "The query cursor is invalid")
        scope_digest = invocation_scope_digest(scope)
        if claims.get("scope") != scope_digest:
            raise DataQueryPythonApplicationError(
                "CURSOR_SCOPE_MISMATCH", "The cursor belongs to a different invocation scope"
            )
        if claims.get("generation") != self._snapshots.process_generation:
            raise DataQueryPythonApplicationError(
                "QUERY_CURSOR_EXPIRED", "The query cursor snapshot was lost after process restart"
            )
        snapshot_id = claims.get("snapshot")
        attempt_id = claims.get("attempt")
        next_row = claims.get("next_row")
        if (
            not isinstance(snapshot_id, str)
            or not snapshot_id.startswith("snap_")
            or not isinstance(next_row, int)
            or isinstance(next_row, bool)
            or next_row < 0
            or not isinstance(attempt_id, str)
        ):
            raise DataQueryPythonApplicationError("CURSOR_INVALID", "The query cursor is invalid")
        try:
            attempt = await self._attempts.get_attempt(scope, attempt_id)
            if attempt is None or attempt.status is not DataQueryAttemptStatus.SUCCEEDED:
                raise DataQueryPythonApplicationError(
                    "QUERY_CURSOR_EXPIRED",
                    "The durable Python query Attempt is unavailable",
                )
            async with await self._snapshots.acquire(snapshot_id) as lease:
                _validate_snapshot_claims(claims, lease.record)
                return await self._page_from_lease(scope_digest, lease, next_row, deadline)
        except QuerySnapshotCacheError as exc:
            raise DataQueryPythonApplicationError(exc.code, str(exc)) from None
        except (OSError, pa.ArrowException) as exc:
            raise DataQueryPythonApplicationError(
                "QUERY_CURSOR_EXPIRED", "The query result snapshot is no longer available"
            ) from exc

    async def _page_from_snapshot(
        self, scope_digest: str, record: QuerySnapshotRecord, start: int, deadline: float
    ) -> DataQueryPythonResult:
        try:
            async with await self._snapshots.acquire(record.snapshot_id) as lease:
                return await self._page_from_lease(scope_digest, lease, start, deadline)
        except QuerySnapshotCacheError as exc:
            raise DataQueryPythonApplicationError(exc.code, str(exc)) from None

    async def _page_from_lease(
        self, scope_digest: str, lease: QuerySnapshotLease, start: int, deadline: float
    ) -> DataQueryPythonResult:
        record = lease.record
        if record.scope_digest != scope_digest:
            raise DataQueryPythonApplicationError(
                "CURSOR_SCOPE_MISMATCH", "The cursor belongs to a different invocation scope"
            )
        if start > record.total_rows:
            raise DataQueryPythonApplicationError(
                "CURSOR_INVALID", "The query cursor row position is invalid"
            )
        raw_rows = await _before_deadline(
            lease.read_rows(start, record.page_size), self._clock, deadline, "read"
        )
        rows: list[QuerySqlRow] = []
        for raw in raw_rows:
            try:
                row = await _before_deadline(
                    asyncio.to_thread(_encoded_row, record.row_schema, raw),
                    self._clock,
                    deadline,
                    "output_validation",
                )
            except DataQuerySqlApplicationError as exc:
                code = (
                    "QUERY_RESULT_VALUE_TOO_LARGE"
                    if exc.code == "QUERY_RESULT_VALUE_TOO_LARGE"
                    else "QUERY_PYTHON_INVALID"
                )
                raise DataQueryPythonApplicationError(
                    code, str(exc), phase="output_validation"
                ) from None
            if len(row.model_dump_json().encode("utf-8")) > _ROW_BYTES:
                raise _value_too_large()
            candidate = (*rows, row)
            result = self._result(scope_digest, record, start, candidate)
            if len(result.model_dump_json().encode("utf-8")) > _RESPONSE_BYTES:
                if not rows:
                    raise _value_too_large()
                break
            rows.append(row)
        result = self._result(scope_digest, record, start, rows)
        if len(result.model_dump_json().encode("utf-8")) > _RESPONSE_BYTES:
            raise _value_too_large()
        if not rows and start < record.total_rows:
            raise _value_too_large()
        return result

    def _result(
        self,
        scope_digest: str,
        record: QuerySnapshotRecord,
        start: int,
        rows: Sequence[QuerySqlRow],
    ) -> DataQueryPythonResult:
        if not isinstance(record.query_statistics, QueryPythonStatistics):
            raise DataQueryPythonApplicationError("CURSOR_INVALID", "The query cursor is invalid")
        next_row = start + len(rows)
        remaining = record.total_rows - next_row
        next_cursor = None
        if remaining > 0:
            try:
                next_cursor = self._cursor_codec.encode(
                    {
                        "tool": _TOOL_NAME,
                        "generation": record.process_generation,
                        "snapshot": record.snapshot_id,
                        "attempt": record.attempt_id,
                        "scope": scope_digest,
                        "exact_input": record.exact_input_digest,
                        "query_fingerprint": record.query_fingerprint,
                        "row_schema": record.row_schema_digest,
                        "snapshot_sha256": record.snapshot_sha256,
                        "page_size": record.page_size,
                        "next_row": next_row,
                        "total": record.total_rows,
                        "expires_at": record.expires_at_unix_ms,
                    }
                )
            except CursorCodecError as exc:
                raise DataQueryPythonApplicationError(
                    "CURSOR_INVALID", "The query cursor could not be encoded"
                ) from exc
        return DataQueryPythonResult(
            row_schema=record.row_schema,
            rows=tuple(rows),
            page=PageInfo(
                total_matched_count=record.total_rows,
                returned_count=len(rows),
                remaining_count=remaining,
                complete=remaining == 0,
                next_cursor=next_cursor,
            ),
            query_statistics=record.query_statistics,
        )

    async def _replay_attempt(
        self,
        scope: InvocationScope,
        attempt: DataQueryAttemptRecord,
        deadline: float,
    ) -> DataQueryPythonResult:
        if attempt.status is DataQueryAttemptStatus.FAILED:
            diagnostic = attempt.diagnostic
            assert diagnostic is not None
            raise DataQueryPythonApplicationError(
                diagnostic.code,
                diagnostic.message,
                phase=diagnostic.phase,
                retryable=diagnostic.retryable,
                attempt_alias=self._attempts.attempt_alias(scope, attempt),
            )
        try:
            async with await self._snapshots.acquire_for_attempt(attempt.attempt_id) as lease:
                summary = attempt.result_summary
                if summary is None or not _summary_matches(summary, lease.record):
                    raise DataQueryPythonApplicationError(
                        "QUERY_RESULT_UNAVAILABLE",
                        "The Python query result snapshot does not match its durable Attempt",
                    )
                return await self._page_from_lease(
                    invocation_scope_digest(scope), lease, 0, deadline
                )
        except QuerySnapshotCacheError as exc:
            raise DataQueryPythonApplicationError(
                "QUERY_RESULT_UNAVAILABLE",
                "The Python query succeeded but its process-private result is no longer available",
                attempt_alias=self._attempts.attempt_alias(scope, attempt),
            ) from exc


def _python_row_schema(output: ArrowTransformOutput) -> QuerySqlRowSchema:
    if not 1 <= len(output.schema) <= 512 or len(set(output.schema.names)) != len(
        output.schema.names
    ):
        raise DataQueryPythonApplicationError(
            "QUERY_PYTHON_INVALID",
            "The Python query result must contain 1..512 uniquely named columns",
            phase="output_validation",
        )
    try:
        return QuerySqlRowSchema(
            columns=tuple(
                QuerySqlColumn(
                    name=field.name,
                    logical_type=str(field.type),
                    nullable=field.nullable,
                    response_encoding=_arrow_response_encoding(field.type),
                )
                for field in output.schema
            )
        )
    except (ValidationError, ValueError) as exc:
        raise DataQueryPythonApplicationError(
            "QUERY_PYTHON_INVALID",
            "The Python query result schema is invalid",
            phase="output_validation",
        ) from exc


def _arrow_response_encoding(value: pa.DataType) -> QuerySqlResponseEncoding:
    if pa.types.is_dictionary(value):
        return _arrow_response_encoding(value.value_type)
    if pa.types.is_boolean(value):
        return QuerySqlResponseEncoding.JSON_BOOLEAN
    if pa.types.is_integer(value):
        return (
            QuerySqlResponseEncoding.DECIMAL_STRING
            if pa.types.is_uint64(value)
            else QuerySqlResponseEncoding.JSON_INTEGER
        )
    if pa.types.is_floating(value):
        return QuerySqlResponseEncoding.SPECIAL_FLOAT_STRING
    if pa.types.is_decimal(value):
        return QuerySqlResponseEncoding.DECIMAL_STRING
    if pa.types.is_string(value) or pa.types.is_large_string(value) or pa.types.is_null(value):
        return QuerySqlResponseEncoding.STRING
    if pa.types.is_date(value) or pa.types.is_time(value) or pa.types.is_timestamp(value):
        return QuerySqlResponseEncoding.ISO_STRING
    if pa.types.is_duration(value) or pa.types.is_interval(value):
        return QuerySqlResponseEncoding.INTERVAL_STRING
    if (
        pa.types.is_binary(value)
        or pa.types.is_large_binary(value)
        or pa.types.is_fixed_size_binary(value)
    ):
        return QuerySqlResponseEncoding.BASE64
    if (
        pa.types.is_list(value)
        or pa.types.is_large_list(value)
        or pa.types.is_fixed_size_list(value)
        or pa.types.is_struct(value)
        or pa.types.is_map(value)
    ):
        return QuerySqlResponseEncoding.JSON_TEXT
    raise ValueError(f"Arrow type {value} has no approved query response encoding")


def _exact_input_digest(resolved: Sequence[_ResolvedInput]) -> str:
    value = []
    for item in resolved:
        exact = {
            "input_name": item.request_name,
            "package_id": item.package_id,
            "payload_name": item.payload.payload_name,
            "member_id": item.payload.member.member_id,
            "artifact": item.payload.member.artifact.model_dump(mode="json"),
        }
        if isinstance(item.payload, ExactJsonPayload):
            exact["schema_member"] = {
                "member_id": item.payload.schema_member.member_id,
                "artifact": item.payload.schema_member.artifact.model_dump(mode="json"),
            }
        value.append(exact)
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _attempt_inputs(resolved: Sequence[_ResolvedInput]) -> tuple[DataQueryExactInputRef, ...]:
    result: list[DataQueryExactInputRef] = []
    for index, item in enumerate(resolved, start=1):
        result.append(
            DataQueryExactInputRef(
                ordinal=index,
                input_name=item.request_name,
                package_id=item.package_id,
                payload_member_id=item.payload.member.member_id,
                schema_member_id=(
                    item.payload.schema_member.member_id
                    if isinstance(item.payload, ExactJsonPayload)
                    else None
                ),
            )
        )
    return tuple(result)


def _attempt_error(error: DataQueryAttemptApplicationError) -> DataQueryPythonApplicationError:
    phase = (
        error.phase
        if error.phase in {"read", "preflight", "code_validation", "execution", "output_validation"}
        else "read"
    )
    return DataQueryPythonApplicationError(
        error.code,
        str(error),
        phase=phase,
        retryable=error.retryable,
        attempt_alias=error.attempt_alias,
    )


def _summary_matches(summary: DataQueryResultSummary, record: QuerySnapshotRecord) -> bool:
    return (
        summary.exact_input_digest == record.exact_input_digest
        and summary.query_fingerprint == record.query_fingerprint
        and summary.row_schema_digest == record.row_schema_digest
        and summary.snapshot_sha256 == record.snapshot_sha256
        and summary.row_count == record.total_rows
        and summary.snapshot_size_bytes == record.size_bytes
    )


def _query_fingerprint(execution: _PythonExecutionFacts, exact_input_digest: str) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "source_code": execution.source_code,
                "exact_input_digest": exact_input_digest,
                "parameters": [item.model_dump(mode="json") for item in execution.parameters],
                "inline_tables": [item.model_dump(mode="json") for item in execution.inline_tables],
                "page_size": execution.page_size,
            }
        ).encode("utf-8")
    ).hexdigest()


def _row_schema_digest(schema: QuerySqlRowSchema) -> str:
    return hashlib.sha256(
        canonical_json(schema.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()


def _validate_snapshot_claims(claims: Mapping[str, object], record: QuerySnapshotRecord) -> None:
    expected = {
        "generation": record.process_generation,
        "snapshot": record.snapshot_id,
        "attempt": record.attempt_id,
        "scope": record.scope_digest,
        "exact_input": record.exact_input_digest,
        "query_fingerprint": record.query_fingerprint,
        "row_schema": record.row_schema_digest,
        "snapshot_sha256": record.snapshot_sha256,
        "page_size": record.page_size,
        "total": record.total_rows,
        "expires_at": record.expires_at_unix_ms,
    }
    if any(claims.get(name) != value for name, value in expected.items()):
        raise DataQueryPythonApplicationError("CURSOR_INVALID", "The query cursor is invalid")


def _map_transform_error(
    code: str,
    message: str,
    *,
    phase: str,
    execution_started: bool = False,
    retryable: bool | None = None,
) -> DataQueryPythonApplicationError:
    mapping = {
        "TRANSFORM_PYTHON_INVALID": "QUERY_PYTHON_INVALID",
        "TRANSFORM_INPUT_BUDGET_EXCEEDED": "QUERY_INPUT_BUDGET_EXCEEDED",
        "TRANSFORM_INLINE_TABLE_INVALID": "QUERY_INPUT_BUDGET_EXCEEDED",
        "TRANSFORM_EXECUTION_FAILED": "QUERY_EXECUTION_FAILED",
        "TRANSFORM_WORKER_FAILED": "QUERY_EXECUTION_FAILED",
        "TRANSFORM_RESOURCE_LIMIT": "QUERY_RESOURCE_LIMIT",
        "TRANSFORM_OUTPUT_BUDGET_EXCEEDED": "QUERY_RESOURCE_LIMIT",
        "TRANSFORM_TIMEOUT": "QUERY_TIMEOUT",
        "TRANSFORM_CAPACITY_BUSY": "QUERY_CAPACITY_BUSY",
        "TRANSFORM_SERVICE_UNAVAILABLE": "QUERY_SERVICE_UNAVAILABLE",
        "TRANSFORM_OUTPUT_INVALID": "QUERY_PYTHON_INVALID",
    }
    mapped = mapping.get(code, code)
    retryable = retryable if retryable is not None else (
        mapped in {"QUERY_CAPACITY_BUSY", "QUERY_SERVICE_UNAVAILABLE"} and not execution_started
    )
    normalized_phase = (
        phase
        if phase in {"preflight", "code_validation", "execution", "output_validation"}
        else "preflight"
    )
    return DataQueryPythonApplicationError(
        mapped, message, phase=normalized_phase, retryable=retryable
    )


def _value_too_large() -> DataQueryPythonApplicationError:
    return DataQueryPythonApplicationError(
        "QUERY_RESULT_VALUE_TOO_LARGE",
        "A Python query value or row exceeds the bounded response budget",
        phase="output_validation",
    )


def _check_deadline(clock: Callable[[], float], deadline: float, phase: str) -> None:
    if clock() >= deadline:
        raise DataQueryPythonApplicationError(
            "QUERY_TIMEOUT", "The Python query deadline expired", phase=phase
        )


async def _before_deadline(awaitable, clock: Callable[[], float], deadline: float, phase: str):
    _check_deadline(clock, deadline, phase)
    try:
        async with asyncio.timeout(max(0.0, deadline - clock())):
            return await awaitable
    except TimeoutError as exc:
        raise DataQueryPythonApplicationError(
            "QUERY_TIMEOUT", "The Python query deadline expired", phase=phase
        ) from exc


def _bounded_utf8(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    return value if len(encoded) <= maximum else encoded[:maximum].decode("utf-8", errors="ignore")


__all__ = ["DataQueryPythonApplicationError", "DataQueryPythonService"]
