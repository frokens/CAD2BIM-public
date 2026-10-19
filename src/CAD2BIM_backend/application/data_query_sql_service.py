"""Bounded exploratory DuckDB SQL over exact Package Payload bindings."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pyarrow as pa
from CAD2BIM_backend.domain.runtime_execution import DATA_SQL_EXECUTION
import pyarrow.parquet as pq
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.application.exact_payload_resolver import (
    ExactParquetPayload,
    ExactPayloadResolutionError,
    ExactPayloadResolver,
)
from CAD2BIM_backend.application.data_query_attempt_service import (
    DataQueryAttemptApplicationError,
    DataQueryAttemptService,
)
from CAD2BIM_backend.application.data_query_deadline import data_query_execution_deadline
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.data_query import PageInfo
from CAD2BIM_backend.domain.data_query_attempt import (
    DataQueryAttemptRecord,
    DataQueryAttemptStatus,
    DataQueryEngine,
    DataQueryExactInputRef,
    DataQueryResultSummary,
)
from CAD2BIM_backend.domain.data_query_sql import (
    DataQuerySqlQuery,
    DataQuerySqlRequest,
    DataQuerySqlResult,
    QuerySqlColumn,
    QuerySqlResponseEncoding,
    QuerySqlRow,
    QuerySqlRowSchema,
    QuerySqlStatistics,
)
from CAD2BIM_backend.domain.data_runtime import PublishedDataPackage
from CAD2BIM_backend.domain.invocation import InvocationScope, invocation_scope_digest
from CAD2BIM_backend.domain.data_transform import (
    DataTransformWorkerError,
    DataTransformWorkerParameter,
    DataTransformWorkerRequest,
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
from CAD2BIM_backend.ports.data_transform import (
    DUCKDB_LOGICAL_TYPE_METADATA_KEY,
    ArrowTransformInput,
    ArrowTransformOutput,
    DataTransformWorkerPort,
)
from CAD2BIM_backend.domain.text_source import SourceEdit


_TOOL_NAME = "data_query_sql"
_TOTAL_TIMEOUT_SECONDS = DATA_SQL_EXECUTION.total_seconds
_INPUT_ROW_LIMIT = 4_000_000
_INPUT_PARQUET_BYTES = 256 * 1024 * 1024
_RESPONSE_BYTES = 1024 * 1024
_COMPLEX_CELL_BYTES = 256 * 1024
_ROW_BYTES = 512 * 1024
_MAX_ERROR_BYTES = 2_000


class DataQuerySqlApplicationError(RuntimeError):
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
        super().__init__(_bounded_utf8(message.strip() or "SQL query failed", _MAX_ERROR_BYTES))
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.attempt_alias = attempt_alias


@dataclass(frozen=True, slots=True)
class _ResolvedInput:
    input_name: str
    package: PublishedDataPackage
    payload: ExactParquetPayload


class DataQuerySqlService:
    """Execute a SQL query once and page its process-private immutable snapshot."""

    def __init__(
        self,
        payloads: ExactPayloadResolver,
        worker: DataTransformWorkerPort,
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
        request: DataQuerySqlRequest,
        *,
        deadline: float | None = None,
        request_identity: str | None = None,
    ) -> DataQuerySqlResult:
        if not isinstance(request, DataQuerySqlRequest):
            raise DataQuerySqlApplicationError(
                "INVALID_ARGUMENT",
                "The SQL query request is invalid",
                phase="preflight",
            )
        deadline = self._clock() + _TOTAL_TIMEOUT_SECONDS if deadline is None else deadline
        if request.query is not None:
            if request_identity is None:
                raise DataQuerySqlApplicationError(
                    "SYSTEM_PAUSED",
                    "The server could not establish a stable SQL query invocation identity",
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
        query: DataQuerySqlQuery,
        *,
        request_identity: str,
        deadline: float | None = None,
    ) -> DataQuerySqlResult:
        deadline = self._clock() + _TOTAL_TIMEOUT_SECONDS if deadline is None else deadline
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
    ) -> DataQuerySqlResult:
        deadline = self._clock() + _TOTAL_TIMEOUT_SECONDS if deadline is None else deadline
        attempts = self._attempts
        try:
            parent, source = await _before_deadline(
                attempts.repair_source(
                    scope,
                    engine=DataQueryEngine.DUCKDB_SQL,
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
        except DataQueryAttemptApplicationError as exc:
            raise _attempt_error(exc) from None
        except ValidationError as exc:
            raise DataQuerySqlApplicationError(
                "DATA_QUERY_REPAIR_INVALID",
                "The persisted SQL repair request is invalid",
                phase="preflight",
                attempt_alias=parent_attempt_alias,
            ) from exc
        resolved = await self._resolve_repair_inputs(scope, parent, deadline)
        return await self._execute_resolved(
            scope,
            source,
            parameters,
            page_size,
            resolved,
            deadline,
            request_identity=request_identity,
            repair_parent=parent,
        )

    async def _first_page(
        self,
        scope: InvocationScope,
        query: DataQuerySqlQuery,
        deadline: float,
        *,
        request_identity: str,
        repair_parent: DataQueryAttemptRecord | None = None,
    ) -> DataQuerySqlResult:
        _check_deadline(self._clock, deadline, "preflight")
        resolved = await self._resolve_inputs(scope, query, deadline)
        return await self._execute_resolved(
            scope,
            query.execution.code,
            query.parameters,
            query.page_size,
            resolved,
            deadline,
            request_identity=request_identity,
            repair_parent=repair_parent,
        )

    async def _execute_resolved(
        self,
        scope: InvocationScope,
        source_code: str,
        parameters: tuple[TransformParameter, ...],
        page_size: int,
        resolved: tuple[_ResolvedInput, ...],
        deadline: float,
        *,
        request_identity: str,
        repair_parent: DataQueryAttemptRecord | None,
    ) -> DataQuerySqlResult:
        scope_digest = invocation_scope_digest(scope)
        exact_input_digest = _exact_input_digest(resolved)
        query_fingerprint = _query_fingerprint(
            source_code, exact_input_digest, parameters, (), page_size
        )
        attempt: DataQueryAttemptRecord
        execution_claimed = False
        attempts = self._attempts
        try:
            prepared = await _before_deadline(
                attempts.prepare(
                    scope,
                    engine=DataQueryEngine.DUCKDB_SQL,
                    source_code=source_code,
                    inputs=_attempt_inputs(resolved),
                    parameters=tuple(item.model_dump(mode="json") for item in parameters),
                    inline_tables=(),
                    page_size=page_size,
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
                attempt = await attempts.acquire_execution(
                    scope,
                    attempt.attempt_id,
                    wait_timeout_seconds=max(0.0, deadline - self._clock()),
                )
            except DataQueryAttemptApplicationError as exc:
                failure = _attempt_error(exc)
                if failure.attempt_alias is None:
                    failure.attempt_alias = attempts.attempt_alias(scope, attempt)
                raise failure from None
            execution_claimed = True
            if attempt.status is not DataQueryAttemptStatus.PREPARED:
                try:
                    return await self._replay_attempt(scope, attempt, deadline)
                finally:
                    attempts.release_execution(attempt.attempt_id)
                    execution_claimed = False
        except DataQueryAttemptApplicationError as exc:
            raise _attempt_error(exc) from None
        execution_deadline = data_query_execution_deadline(deadline)
        materialized: dict[str, Path] = {}
        success_publication_started = False
        try:
            materialized = await _before_deadline(
                self._payloads.materialize_members(
                    tuple(item.payload.member for item in resolved),
                    check_budget=lambda: _check_deadline(
                        self._clock, execution_deadline, "preflight"
                    ),
                    max_total_bytes=_INPUT_PARQUET_BYTES,
                ),
                self._clock,
                execution_deadline,
                "preflight",
            )
            statistics = QuerySqlStatistics(
                bound_input_payload_count=len(resolved),
                bound_input_row_count=sum(
                    int(item.payload.member.row_count or 0) for item in resolved
                ),
                bound_input_stored_bytes=sum(
                    materialized[item.payload.member.member_id].stat().st_size for item in resolved
                ),
                execution_ms=0,
            )
            worker_request = DataTransformWorkerRequest(
                code=source_code,
                parameters=tuple(
                    DataTransformWorkerParameter(name=item.name, value=item.value)
                    for item in parameters
                ),
                result_mode="query_final_result",
                output_names=(),
            )
            try:
                outputs = await _before_deadline(
                    self._worker.execute(
                        worker_request,
                        _arrow_inputs(resolved, materialized),
                        deadline=deadline,
                    ),
                    self._clock,
                    execution_deadline,
                    "execution",
                )
            except DataTransformWorkerError as exc:
                raise DataQuerySqlApplicationError(
                    exc.code,
                    str(exc),
                    phase=exc.phase,
                    retryable=(
                        exc.retryable if exc.retryable is not None else not exc.execution_started
                        and exc.code in {"TRANSFORM_CAPACITY_BUSY", "TRANSFORM_SERVICE_UNAVAILABLE"}
                    ),
                ) from None
            if len(outputs) != 1 or outputs[0].payload_name is not None:
                raise DataQuerySqlApplicationError(
                    "TRANSFORM_WORKER_FAILED",
                    "The query Worker returned an invalid final-result envelope",
                    phase="execution",
                )
            output = outputs[0]
            row_schema = _row_schema(output)
            statistics = statistics.model_copy(update={"execution_ms": output.receipt.execution_ms})
            row_schema_digest = _row_schema_digest(row_schema)
            try:
                record = await _before_deadline(
                    self._snapshots.create(
                        QuerySnapshotWrite(
                            attempt_id=attempt.attempt_id,
                            scope_digest=scope_digest,
                            exact_input_digest=exact_input_digest,
                            query_fingerprint=query_fingerprint,
                            row_schema=row_schema,
                            page_size=page_size,
                            query_statistics=statistics,
                            arrow_schema=output.schema,
                            batches=output.batches,
                        ),
                        row_schema_digest=row_schema_digest,
                    ),
                    self._clock,
                    execution_deadline,
                    "output_validation",
                )
            except QuerySnapshotCacheError as exc:
                raise DataQuerySqlApplicationError(
                    exc.code,
                    str(exc),
                    phase="output_validation",
                    retryable=exc.code == "TRANSFORM_SERVICE_UNAVAILABLE",
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
        except DataQuerySqlApplicationError as exc:
            if (
                attempt.status is DataQueryAttemptStatus.PREPARED
                and not success_publication_started
            ):
                await self._terminalize_failure(scope, attempt, exc, deadline)
            raise
        except ExactPayloadResolutionError as exc:
            failure = DataQuerySqlApplicationError(
                exc.code,
                str(exc),
                phase="preflight",
            )
            if attempt.status is DataQueryAttemptStatus.PREPARED:
                await self._terminalize_failure(scope, attempt, failure, deadline)
            raise failure from None
        except OSError as exc:
            failure = DataQuerySqlApplicationError(
                "TRANSFORM_SERVICE_UNAVAILABLE",
                "The SQL query input or snapshot storage is unavailable",
                phase="preflight",
                retryable=True,
            )
            if attempt.status is DataQueryAttemptStatus.PREPARED:
                await self._terminalize_failure(scope, attempt, failure, deadline)
            raise failure from exc
        except Exception as exc:
            failure = DataQuerySqlApplicationError(
                "TRANSFORM_SERVICE_UNAVAILABLE",
                "The SQL query service failed before producing a valid result",
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
        failure: DataQuerySqlApplicationError,
        deadline: float,
    ) -> None:
        persisted_error: DataQuerySqlApplicationError | None = None
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

    async def _continuation(
        self,
        scope: InvocationScope,
        cursor: str,
        deadline: float,
    ) -> DataQuerySqlResult:
        _check_deadline(self._clock, deadline, "read")
        try:
            claims = self._cursor_codec.decode(cursor)
        except CursorCodecError as exc:
            raise DataQuerySqlApplicationError(
                "CURSOR_INVALID", "The query cursor is invalid"
            ) from exc
        if claims.get("tool") != _TOOL_NAME:
            if claims.get("tool") not in {None, _TOOL_NAME}:
                raise DataQuerySqlApplicationError(
                    "CURSOR_TOOL_MISMATCH",
                    "The cursor belongs to a different tool",
                )
            raise DataQuerySqlApplicationError("CURSOR_INVALID", "The query cursor is invalid")
        scope_digest = invocation_scope_digest(scope)
        if claims.get("scope") != scope_digest:
            raise DataQuerySqlApplicationError(
                "CURSOR_SCOPE_MISMATCH",
                "The cursor belongs to a different invocation scope",
            )
        if claims.get("generation") != self._snapshots.process_generation:
            raise DataQuerySqlApplicationError(
                "QUERY_CURSOR_EXPIRED",
                "The query cursor snapshot was lost after process restart",
            )
        try:
            snapshot_id = claims["snapshot"]
            attempt_id = claims.get("attempt")
            next_row = claims["next_row"]
            if (
                not isinstance(snapshot_id, str)
                or not snapshot_id.startswith("snap_")
                or not isinstance(next_row, int)
                or isinstance(next_row, bool)
                or next_row < 0
                or not isinstance(attempt_id, str)
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise DataQuerySqlApplicationError(
                "CURSOR_INVALID", "The query cursor is invalid"
            ) from exc
        try:
            attempt = await self._attempts.get_attempt(scope, attempt_id)
            if attempt is None or attempt.status is not DataQueryAttemptStatus.SUCCEEDED:
                raise DataQuerySqlApplicationError(
                    "QUERY_CURSOR_EXPIRED",
                    "The durable SQL query Attempt is unavailable",
                )
            async with await self._snapshots.acquire(snapshot_id) as lease:
                _validate_snapshot_claims(claims, lease.record)
                result = await self._page_from_lease(
                    scope_digest,
                    lease,
                    next_row,
                    deadline,
                )
                return result
        except QuerySnapshotCacheError as exc:
            raise DataQuerySqlApplicationError(exc.code, str(exc)) from None
        except (OSError, pa.ArrowException) as exc:
            raise DataQuerySqlApplicationError(
                "QUERY_CURSOR_EXPIRED",
                "The query result snapshot is no longer available",
            ) from exc

    async def _page_from_snapshot(
        self,
        scope_digest: str,
        record: QuerySnapshotRecord,
        start: int,
        deadline: float,
    ) -> DataQuerySqlResult:
        try:
            async with await self._snapshots.acquire(record.snapshot_id) as lease:
                return await self._page_from_lease(scope_digest, lease, start, deadline)
        except QuerySnapshotCacheError as exc:
            raise DataQuerySqlApplicationError(exc.code, str(exc)) from None
        except (OSError, pa.ArrowException) as exc:
            raise DataQuerySqlApplicationError(
                "QUERY_CURSOR_EXPIRED",
                "The query result snapshot is no longer available",
            ) from exc

    async def _page_from_lease(
        self,
        scope_digest: str,
        lease: QuerySnapshotLease,
        start: int,
        deadline: float,
    ) -> DataQuerySqlResult:
        record = lease.record
        if record.scope_digest != scope_digest:
            raise DataQuerySqlApplicationError(
                "CURSOR_SCOPE_MISMATCH",
                "The query cursor belongs to a different invocation scope",
            )
        if start > record.total_rows:
            raise DataQuerySqlApplicationError(
                "CURSOR_INVALID", "The query cursor row position is invalid"
            )
        raw_rows = await _before_deadline(
            lease.read_rows(start, record.page_size),
            self._clock,
            deadline,
            "read",
        )
        return await self._bounded_page(scope_digest, record, start, raw_rows, deadline)

    async def _bounded_page(
        self,
        scope_digest: str,
        record: QuerySnapshotRecord,
        start: int,
        raw_rows: Sequence[Mapping[str, object]],
        deadline: float,
    ) -> DataQuerySqlResult:
        rows: list[QuerySqlRow] = []
        for raw in raw_rows:
            row = await _before_deadline(
                asyncio.to_thread(_encoded_row, record.row_schema, raw),
                self._clock,
                deadline,
                "output_validation",
            )
            row_bytes = len(row.model_dump_json().encode("utf-8"))
            if row_bytes > _ROW_BYTES:
                raise _value_too_large()
            candidate = (*rows, row)
            result = self._result(scope_digest, record, start, candidate)
            if len(result.model_dump_json().encode("utf-8")) > _RESPONSE_BYTES:
                if not rows:
                    raise _value_too_large()
                break
            rows.append(row)
        result = self._result(scope_digest, record, start, tuple(rows))
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
    ) -> DataQuerySqlResult:
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
                raise DataQuerySqlApplicationError(
                    "CURSOR_INVALID", "The query cursor could not be encoded"
                ) from exc
        return DataQuerySqlResult(
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
    ) -> DataQuerySqlResult:
        if attempt.status is DataQueryAttemptStatus.FAILED:
            diagnostic = attempt.diagnostic
            assert diagnostic is not None
            raise DataQuerySqlApplicationError(
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
                    raise DataQuerySqlApplicationError(
                        "QUERY_RESULT_UNAVAILABLE",
                        "The SQL query result snapshot does not match its durable Attempt",
                    )
                result = await self._page_from_lease(
                    invocation_scope_digest(scope), lease, 0, deadline
                )
                return result
        except QuerySnapshotCacheError as exc:
            raise DataQuerySqlApplicationError(
                "QUERY_RESULT_UNAVAILABLE",
                "The SQL query succeeded but its process-private result is no longer available",
                attempt_alias=self._attempts.attempt_alias(scope, attempt),
            ) from exc

    async def _resolve_inputs(
        self,
        scope: InvocationScope,
        query: DataQuerySqlQuery,
        deadline: float,
    ) -> tuple[_ResolvedInput, ...]:
        result: list[_ResolvedInput] = []
        package_cache: dict[
            str,
            tuple[PublishedDataPackage, Mapping[str, ExactParquetPayload]],
        ] = {}
        seen_members: set[tuple[str, str]] = set()
        total_rows = 0
        for item in query.inputs:
            _check_deadline(self._clock, deadline, "preflight")
            try:
                cached = package_cache.get(item.package_alias)
                if cached is None:
                    package = await _before_deadline(
                        self._payloads.package_by_alias(scope, item.package_alias),
                        self._clock,
                        deadline,
                        "preflight",
                    )
                    payloads = self._payloads.parquet_payloads(package)
                    package_cache[item.package_alias] = package, payloads
                else:
                    package, payloads = cached
                payload = payloads.get(item.payload_name)
            except ExactPayloadResolutionError as exc:
                raise DataQuerySqlApplicationError(exc.code, str(exc), phase="preflight") from None
            if payload is None:
                raise DataQuerySqlApplicationError(
                    "PAYLOAD_NOT_FOUND",
                    f"Payload {item.payload_name!r} was not found in {item.package_alias}",
                    phase="preflight",
                )
            exact = (payload.member.artifact.version_id, payload.member.member_id)
            if exact in seen_members:
                raise DataQuerySqlApplicationError(
                    "INVALID_ARGUMENT",
                    "The same exact Payload cannot be bound more than once",
                    phase="preflight",
                )
            seen_members.add(exact)
            total_rows += int(payload.member.row_count or 0)
            result.append(
                _ResolvedInput(
                    input_name=item.input_name,
                    package=package,
                    payload=payload,
                )
            )
        if total_rows > _INPUT_ROW_LIMIT:
            raise DataQuerySqlApplicationError(
                "TRANSFORM_INPUT_BUDGET_EXCEEDED",
                "Exact input rows exceed the 4,000,000-row query budget",
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
                raise DataQuerySqlApplicationError(exc.code, str(exc), phase="preflight") from None
            if not isinstance(payload, ExactParquetPayload):
                raise DataQuerySqlApplicationError(
                    "PACKAGE_INTEGRITY_ERROR",
                    "A SQL repair input is no longer an exact Parquet Payload",
                    phase="preflight",
                )
            result.append(
                _ResolvedInput(
                    input_name=item.input_name,
                    package=package,
                    payload=payload,
                )
            )
        return tuple(result)


def _exact_input_digest(resolved: Sequence[_ResolvedInput]) -> str:
    value = [
        {
            "input_name": item.input_name,
            "package_id": item.package.package.package_id,
            "payload_name": item.payload.payload_name,
            "member_id": item.payload.member.member_id,
            "artifact_id": item.payload.member.artifact.artifact_id,
            "version_id": item.payload.member.artifact.version_id,
            "sha256": item.payload.member.artifact.sha256,
        }
        for item in resolved
    ]
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _attempt_inputs(resolved: Sequence[_ResolvedInput]) -> tuple[DataQueryExactInputRef, ...]:
    return tuple(
        DataQueryExactInputRef(
            ordinal=index,
            input_name=item.input_name,
            package_id=item.package.package.package_id,
            payload_member_id=item.payload.member.member_id,
            schema_member_id=None,
        )
        for index, item in enumerate(resolved, start=1)
    )


def _attempt_error(error: DataQueryAttemptApplicationError) -> DataQuerySqlApplicationError:
    phase = (
        error.phase
        if error.phase in {"read", "preflight", "code_validation", "execution", "output_validation"}
        else "read"
    )
    return DataQuerySqlApplicationError(
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


def _query_fingerprint(
    source_code: str,
    exact_input_digest: str,
    parameters: Sequence[TransformParameter],
    inline_tables: Sequence[object],
    page_size: int,
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "source_code": source_code,
                "exact_input_digest": exact_input_digest,
                "parameters": [item.model_dump(mode="json") for item in parameters],
                "inline_tables": list(inline_tables),
                "page_size": page_size,
            }
        ).encode("utf-8")
    ).hexdigest()


def _row_schema_digest(schema: QuerySqlRowSchema) -> str:
    return hashlib.sha256(
        canonical_json(schema.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()


def _arrow_inputs(
    resolved: Sequence[_ResolvedInput],
    materialized: Mapping[str, Path],
) -> tuple[ArrowTransformInput, ...]:
    result: list[ArrowTransformInput] = []
    for item in resolved:
        parquet = pq.ParquetFile(materialized[item.payload.member.member_id])
        if parquet.metadata.num_rows != item.payload.member.row_count:
            raise DataQuerySqlApplicationError(
                "PACKAGE_INTEGRITY_ERROR",
                "A Parquet member row count does not match Package metadata",
                phase="preflight",
            )
        result.append(
            ArrowTransformInput(
                input_name=item.input_name,
                reader=pa.RecordBatchReader.from_batches(
                    parquet.schema_arrow,
                    parquet.iter_batches(batch_size=65_536),
                ),
            )
        )
    return tuple(result)


def _row_schema(output: ArrowTransformOutput) -> QuerySqlRowSchema:
    if not 1 <= len(output.schema) <= 512:
        raise DataQuerySqlApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            "The SQL query result must contain 1..512 columns",
            phase="output_validation",
        )
    if len(set(output.schema.names)) != len(output.schema.names):
        raise DataQuerySqlApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            "The SQL query result column names must be unique",
            phase="output_validation",
        )
    columns: list[QuerySqlColumn] = []
    for field in output.schema:
        raw_type = (field.metadata or {}).get(DUCKDB_LOGICAL_TYPE_METADATA_KEY)
        if raw_type is None:
            raise DataQuerySqlApplicationError(
                "TRANSFORM_OUTPUT_INVALID",
                "The SQL query result is missing DuckDB logical type metadata",
                phase="output_validation",
            )
        try:
            logical_type = raw_type.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DataQuerySqlApplicationError(
                "TRANSFORM_OUTPUT_INVALID",
                "The SQL query result contains invalid logical type metadata",
                phase="output_validation",
            ) from exc
        encoding = _response_encoding(logical_type)
        columns.append(
            QuerySqlColumn(
                name=field.name,
                logical_type=logical_type,
                nullable=field.nullable,
                response_encoding=encoding,
            )
        )
    try:
        return QuerySqlRowSchema(columns=tuple(columns))
    except ValidationError as exc:
        raise DataQuerySqlApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            "The SQL query result schema is invalid",
            phase="output_validation",
        ) from exc


def _response_encoding(logical_type: str) -> QuerySqlResponseEncoding:
    normalized = logical_type.strip().upper()
    base = normalized.split("(", 1)[0].strip()
    if base == "BOOLEAN":
        return QuerySqlResponseEncoding.JSON_BOOLEAN
    if base in {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
    }:
        return QuerySqlResponseEncoding.JSON_INTEGER
    if base == "BIGNUM":
        raise DataQuerySqlApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            (
                "DuckDB BIGNUM has no approved query Response encoding; "
                "CAST the BIGNUM expression AS VARCHAR in SQL"
            ),
            phase="output_validation",
        )
    if base == "UNION":
        raise DataQuerySqlApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            (
                "DuckDB UNION loses its active member tag in the Arrow query Response; "
                "explicitly project a member or CAST the expression to a stable type in SQL"
            ),
            phase="output_validation",
        )
    if base in {"DECIMAL", "NUMERIC", "HUGEINT", "UHUGEINT", "UBIGINT"}:
        return QuerySqlResponseEncoding.DECIMAL_STRING
    if base in {"FLOAT", "REAL", "DOUBLE"}:
        # One column schema must cover finite and non-finite rows without scanning
        # up to two million values on the event loop. Finite values remain JSON
        # numbers; only NaN and infinities use the documented sentinel strings.
        return QuerySqlResponseEncoding.SPECIAL_FLOAT_STRING
    if base in {"VARCHAR", "CHAR", "BPCHAR", "TEXT", "ENUM", "UUID", "SQLNULL"}:
        return QuerySqlResponseEncoding.STRING
    if base in {
        "DATE",
        "TIME",
        "TIME WITH TIME ZONE",
        "TIMESTAMP",
        "TIMESTAMP WITH TIME ZONE",
        "TIMESTAMP_S",
        "TIMESTAMP_MS",
        "TIMESTAMP_NS",
        "TIMESTAMPTZ",
        "TIMETZ",
    }:
        return QuerySqlResponseEncoding.ISO_STRING
    if base == "INTERVAL":
        return QuerySqlResponseEncoding.INTERVAL_STRING
    if base in {"BLOB", "BIT", "BITSTRING"}:
        return QuerySqlResponseEncoding.BASE64
    if (
        base == "JSON"
        or normalized.endswith("[]")
        or base
        in {
            "LIST",
            "ARRAY",
            "STRUCT",
            "MAP",
        }
    ):
        return QuerySqlResponseEncoding.JSON_TEXT
    raise DataQuerySqlApplicationError(
        "TRANSFORM_OUTPUT_INVALID",
        f"DuckDB logical type {logical_type!r} has no approved response encoding",
        phase="output_validation",
    )


def _encoded_row(
    schema: QuerySqlRowSchema,
    raw: Mapping[str, object],
) -> QuerySqlRow:
    values: dict[str, object] = {}
    for column in schema.columns:
        if column.name not in raw:
            raise DataQuerySqlApplicationError(
                "TRANSFORM_OUTPUT_INVALID",
                "A snapshot row does not match its row schema",
                phase="output_validation",
            )
        value = _encoded_value(column.response_encoding, raw[column.name])
        if (
            column.response_encoding
            in {
                QuerySqlResponseEncoding.BASE64,
                QuerySqlResponseEncoding.JSON_TEXT,
            }
            and len(canonical_json(value).encode("utf-8")) > _COMPLEX_CELL_BYTES
        ):
            raise _value_too_large()
        values[column.name] = value
    try:
        return QuerySqlRow(values=values)
    except ValidationError as exc:
        raise DataQuerySqlApplicationError(
            "TRANSFORM_OUTPUT_INVALID",
            "A query result value does not match its approved response encoding",
            phase="output_validation",
        ) from exc


def _encoded_value(encoding: QuerySqlResponseEncoding, value: object) -> object:
    if value is None:
        return None
    if encoding is QuerySqlResponseEncoding.JSON_BOOLEAN:
        if type(value) is not bool:
            raise _encoding_error()
        return value
    if encoding is QuerySqlResponseEncoding.JSON_INTEGER:
        if type(value) is not int or not -(2**63) <= value <= 2**63 - 1:
            raise _encoding_error()
        return value
    if encoding in {
        QuerySqlResponseEncoding.JSON_NUMBER,
        QuerySqlResponseEncoding.SPECIAL_FLOAT_STRING,
    }:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise _encoding_error()
        number = float(value)
        if math.isnan(number):
            return "NaN"
        if number == math.inf:
            return "Infinity"
        if number == -math.inf:
            return "-Infinity"
        return number
    if encoding is QuerySqlResponseEncoding.DECIMAL_STRING:
        if not isinstance(value, (Decimal, int)) or isinstance(value, bool):
            raise _encoding_error()
        return format(value, "f") if isinstance(value, Decimal) else str(value)
    if encoding is QuerySqlResponseEncoding.STRING:
        return str(value)
    if encoding is QuerySqlResponseEncoding.ISO_STRING:
        if isinstance(value, (datetime, date, datetime_time)):
            return value.isoformat()
        return str(value)
    if encoding is QuerySqlResponseEncoding.INTERVAL_STRING:
        return _interval_text(value)
    if encoding is QuerySqlResponseEncoding.BASE64:
        if isinstance(value, str):
            if _base64_json_bytes(len(value)) > _COMPLEX_CELL_BYTES:
                raise _value_too_large()
            raw = value.encode("utf-8")
        elif isinstance(value, (bytes, bytearray, memoryview)):
            raw_bytes = memoryview(value).nbytes
            if _base64_json_bytes(raw_bytes) > _COMPLEX_CELL_BYTES:
                raise _value_too_large()
            raw = bytes(value)
        else:
            raise _encoding_error()
        if _base64_json_bytes(len(raw)) > _COMPLEX_CELL_BYTES:
            raise _value_too_large()
        return base64.b64encode(raw).decode("ascii")
    if encoding is QuerySqlResponseEncoding.JSON_TEXT:
        try:
            if isinstance(value, str):
                if len(value) > _COMPLEX_CELL_BYTES or len(value.encode("utf-8")) > (
                    _COMPLEX_CELL_BYTES
                ):
                    raise _value_too_large()
                parsed = json.loads(value)
            else:
                parsed = value
            return _BoundedJsonTextEncoder().encode(parsed)
        except DataQuerySqlApplicationError:
            raise
        except (
            json.JSONDecodeError,
            RecursionError,
            TypeError,
            UnicodeEncodeError,
            ValueError,
        ) as exc:
            raise _encoding_error() from exc
    raise _encoding_error()


def _base64_json_bytes(raw_bytes: int) -> int:
    """Return exact ASCII bytes for a base64 value including JSON quotes."""

    return 4 * ((raw_bytes + 2) // 3) + 2


class _BoundedJsonTextEncoder:
    """Incrementally encode approved nested values without building a second tree."""

    _MAX_DEPTH = 64
    _MAX_ITEMS = _COMPLEX_CELL_BYTES

    def __init__(self) -> None:
        self._output = io.StringIO()
        self._bytes = 0
        self._items = 0

    def encode(self, value: object) -> str:
        self._value(value, depth=0)
        return self._output.getvalue()

    def _append(self, value: str) -> None:
        self._bytes += len(value.encode("utf-8"))
        if self._bytes > _COMPLEX_CELL_BYTES:
            raise _value_too_large()
        self._output.write(value)

    def _item(self) -> None:
        self._items += 1
        if self._items > self._MAX_ITEMS:
            raise _value_too_large()

    def _scalar(self, value: object) -> None:
        self._append(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        )

    def _value(self, value: object, *, depth: int) -> None:
        if depth > self._MAX_DEPTH:
            raise _encoding_error()
        if value is None or isinstance(value, (bool, int, str)):
            self._scalar(value)
            return
        if isinstance(value, float):
            if math.isnan(value):
                self._scalar("NaN")
            elif value == math.inf:
                self._scalar("Infinity")
            elif value == -math.inf:
                self._scalar("-Infinity")
            else:
                self._scalar(value)
            return
        if isinstance(value, Decimal):
            self._scalar(format(value, "f"))
            return
        if isinstance(value, (datetime, date, datetime_time)):
            self._scalar(value.isoformat())
            return
        if isinstance(value, timedelta):
            self._scalar(_interval_text(value))
            return
        if isinstance(value, (bytes, bytearray, memoryview)):
            self._scalar(base64.b64encode(bytes(value)).decode("ascii"))
            return
        if isinstance(value, Mapping):
            self._append("{")
            for index, (key, item) in enumerate(value.items()):
                self._item()
                if index:
                    self._append(",")
                self._scalar(str(key))
                self._append(":")
                self._value(item, depth=depth + 1)
            self._append("}")
            return
        if isinstance(value, (list, tuple)):
            self._append("[")
            for index, item in enumerate(value):
                self._item()
                if index:
                    self._append(",")
                self._value(item, depth=depth + 1)
            self._append("]")
            return
        raise _encoding_error()


def _interval_text(value: object) -> str:
    if isinstance(value, timedelta):
        total_seconds = value.days * 86_400 + value.seconds
        hours, remainder = divmod(total_seconds, 3_600)
        minutes, seconds = divmod(remainder, 60)
        fraction = f".{value.microseconds:06d}".rstrip("0") if value.microseconds else ""
        return f"{value.days} days {hours % 24:02d}:{minutes:02d}:{seconds:02d}{fraction}"
    months = getattr(value, "months", None)
    days = getattr(value, "days", None)
    nanoseconds = getattr(value, "nanoseconds", None)
    if all(isinstance(item, int) for item in (months, days, nanoseconds)):
        return f"{months} months {days} days {nanoseconds} nanoseconds"
    return str(value)


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
        raise DataQuerySqlApplicationError("CURSOR_INVALID", "The query cursor is invalid")


def _encoding_error() -> DataQuerySqlApplicationError:
    return DataQuerySqlApplicationError(
        "TRANSFORM_OUTPUT_INVALID",
        "A query result value does not match its DuckDB logical type",
        phase="output_validation",
    )


def _value_too_large() -> DataQuerySqlApplicationError:
    return DataQuerySqlApplicationError(
        "QUERY_RESULT_VALUE_TOO_LARGE",
        (
            "A query value or row exceeds the response budget; select fewer columns or use "
            "unnest, struct_extract, or json_extract to expand only the required values"
        ),
        phase="output_validation",
    )


def _check_deadline(
    clock: Callable[[], float],
    deadline: float,
    phase: Literal["read", "preflight", "code_validation", "execution", "output_validation"],
) -> None:
    if clock() >= deadline:
        raise DataQuerySqlApplicationError(
            "TRANSFORM_TIMEOUT",
            "The 60-second SQL query deadline expired",
            phase=phase,
        )


async def _before_deadline(
    awaitable,
    clock: Callable[[], float],
    deadline: float,
    phase: Literal["read", "preflight", "code_validation", "execution", "output_validation"],
):
    _check_deadline(clock, deadline, phase)
    try:
        async with asyncio.timeout(max(0.0, deadline - clock())):
            return await awaitable
    except TimeoutError as exc:
        raise DataQuerySqlApplicationError(
            "TRANSFORM_TIMEOUT",
            "The 60-second SQL query deadline expired",
            phase=phase,
        ) from exc


def _bounded_utf8(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", errors="ignore")


__all__ = [
    "DataQuerySqlApplicationError",
    "DataQuerySqlService",
]
