"""Process-private immutable Arrow snapshots for bounded query pagination."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
import tempfile
import time
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa

from CAD2BIM_backend.domain.data_query_sql import QuerySqlRowSchema, QuerySqlStatistics
from CAD2BIM_backend.domain.data_query_python import QueryPythonStatistics


QueryStatistics = QuerySqlStatistics | QueryPythonStatistics


_MAX_CONCURRENT_WRITES = 2


class _SnapshotTooLarge(RuntimeError):
    pass


class QuerySnapshotCacheError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class QuerySnapshotPolicy:
    ttl_seconds: float = 900.0
    per_snapshot_bytes: int = 128 * 1024 * 1024
    per_scope_snapshots: int = 4
    process_snapshots: int = 8
    process_cache_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.ttl_seconds <= 0:
            raise ValueError("snapshot TTL must be positive")
        for value in (
            self.per_snapshot_bytes,
            self.per_scope_snapshots,
            self.process_snapshots,
            self.process_cache_bytes,
        ):
            if value < 1:
                raise ValueError("snapshot cache limits must be positive")
        if self.per_scope_snapshots > self.process_snapshots:
            raise ValueError("per-scope snapshot limit cannot exceed process limit")
        if self.per_snapshot_bytes > self.process_cache_bytes:
            raise ValueError("one snapshot cannot exceed the process cache byte limit")


@dataclass(frozen=True, slots=True)
class QuerySnapshotWrite:
    attempt_id: str
    scope_digest: str
    exact_input_digest: str
    query_fingerprint: str
    row_schema: QuerySqlRowSchema
    page_size: int
    query_statistics: QueryStatistics
    arrow_schema: pa.Schema
    batches: tuple[pa.RecordBatch, ...]


@dataclass(frozen=True, slots=True)
class QuerySnapshotRecord:
    snapshot_id: str
    attempt_id: str
    process_generation: str
    scope_digest: str
    exact_input_digest: str
    query_fingerprint: str
    row_schema: QuerySqlRowSchema
    row_schema_digest: str
    snapshot_sha256: str
    page_size: int
    query_statistics: QueryStatistics
    total_rows: int
    created_at_unix_ms: int
    expires_at_unix_ms: int
    size_bytes: int


@dataclass(slots=True)
class _Entry:
    record: QuerySnapshotRecord
    path: Path
    batch_starts: tuple[int, ...]
    expires_at_monotonic: float
    active_readers: int = 0
    expired: bool = False


class QuerySnapshotLease:
    """Pin one snapshot against LRU deletion while a page is being read."""

    def __init__(self, cache: "QuerySnapshotCache", entry: _Entry) -> None:
        self._cache = cache
        self._entry = entry
        self._released = False

    @property
    def record(self) -> QuerySnapshotRecord:
        return self._entry.record

    async def read_rows(self, start: int, limit: int) -> list[dict[str, object]]:
        if self._released:
            raise RuntimeError("snapshot lease has been released")
        if start < 0 or limit < 0:
            raise ValueError("snapshot row range must be non-negative")
        return await asyncio.to_thread(
            _read_rows,
            self._entry.path,
            self._entry.batch_starts,
            start,
            limit,
        )

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._cache._release(self._entry.record.snapshot_id)

    async def __aenter__(self) -> "QuerySnapshotLease":
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await self.release()


class QuerySnapshotCache:
    """Bounded process-local LRU of immutable Arrow IPC files."""

    def __init__(
        self,
        *,
        policy: QuerySnapshotPolicy | None = None,
        root: str | Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._policy = policy or QuerySnapshotPolicy()
        self._clock = clock
        self._wall_clock = wall_clock
        self._generation = secrets.token_urlsafe(18)
        if root is None:
            self._root = Path(tempfile.mkdtemp(prefix="cad2bim-query-snapshots-"))
            self._owns_root = True
        else:
            self._root = Path(root)
            if self._root.is_symlink():
                raise ValueError("snapshot cache root cannot be a symlink")
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._owns_root = False
        os.chmod(self._root, 0o700)
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._attempt_snapshots: dict[str, str] = {}
        self._total_bytes = 0
        self._condition = asyncio.Condition()
        self._write_capacity = asyncio.Semaphore(_MAX_CONCURRENT_WRITES)
        self._closed = False
        self._close_complete = asyncio.Event()
        self._write_tasks: set[
            asyncio.Task[tuple[int, int, tuple[int, ...], str]]
        ] = set()
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    @property
    def process_generation(self) -> str:
        return self._generation

    async def create(
        self, value: QuerySnapshotWrite, *, row_schema_digest: str
    ) -> QuerySnapshotRecord:
        if self._closed:
            raise RuntimeError("snapshot cache is closed")
        snapshot_id = "snap_" + secrets.token_urlsafe(24)
        attempt_id = value.attempt_id
        path = self._root / f"{snapshot_id}.arrow"
        write_capacity_acquired = False
        try:
            await self._write_capacity.acquire()
            write_capacity_acquired = True
            if self._closed:
                raise RuntimeError("snapshot cache is closed")
            try:
                write = asyncio.create_task(
                    asyncio.to_thread(
                        _write_snapshot,
                        path,
                        value.arrow_schema,
                        value.batches,
                        self._policy.per_snapshot_bytes,
                    )
                )
                self._write_tasks.add(write)
                write.add_done_callback(self._write_tasks.discard)
                size_bytes, total_rows, batch_starts, snapshot_sha256 = await asyncio.shield(write)
            except asyncio.CancelledError:
                write_capacity_acquired = False
                cleanup = asyncio.create_task(
                    self._finish_cancelled_write(write, path, release_capacity=True)
                )
                self._cleanup_tasks.add(cleanup)
                cleanup.add_done_callback(self._cleanup_tasks.discard)
                raise
            except _SnapshotTooLarge as exc:
                path.unlink(missing_ok=True)
                raise self._snapshot_too_large() from exc
            except (OSError, pa.ArrowException, ValueError) as exc:
                path.unlink(missing_ok=True)
                raise QuerySnapshotCacheError(
                    "TRANSFORM_SERVICE_UNAVAILABLE",
                    "The query result snapshot could not be materialized",
                ) from exc
            if size_bytes > self._policy.per_snapshot_bytes:
                path.unlink(missing_ok=True)
                raise self._snapshot_too_large()

            now = self._clock()
            created_at_unix_ms = int(self._wall_clock() * 1000)
            record = QuerySnapshotRecord(
                snapshot_id=snapshot_id,
                attempt_id=attempt_id,
                process_generation=self._generation,
                scope_digest=value.scope_digest,
                exact_input_digest=value.exact_input_digest,
                query_fingerprint=value.query_fingerprint,
                row_schema=value.row_schema,
                row_schema_digest=row_schema_digest,
                snapshot_sha256=snapshot_sha256,
                page_size=value.page_size,
                query_statistics=value.query_statistics,
                total_rows=total_rows,
                created_at_unix_ms=created_at_unix_ms,
                expires_at_unix_ms=created_at_unix_ms + int(self._policy.ttl_seconds * 1000),
                size_bytes=size_bytes,
            )
            entry = _Entry(
                record=record,
                path=path,
                batch_starts=batch_starts,
                expires_at_monotonic=now + self._policy.ttl_seconds,
            )
            try:
                async with self._condition:
                    self._require_open_locked()
                    self._purge_expired_locked(now)
                    await self._make_room_locked(entry)
                    previous_id = self._attempt_snapshots.get(attempt_id)
                    if previous_id is not None:
                        previous = self._entries.get(previous_id)
                        if previous is not None and previous.active_readers:
                            raise QuerySnapshotCacheError(
                                "QUERY_SERVICE_UNAVAILABLE",
                                "The query attempt snapshot is currently in use",
                            )
                        self._delete_locked(previous_id)
                    self._entries[snapshot_id] = entry
                    self._attempt_snapshots[attempt_id] = snapshot_id
                    self._total_bytes += size_bytes
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return record
        finally:
            if write_capacity_acquired:
                self._write_capacity.release()

    async def acquire(self, snapshot_id: str) -> QuerySnapshotLease:
        async with self._condition:
            self._require_open_locked()
            now = self._clock()
            self._purge_expired_locked(now)
            entry = self._entries.get(snapshot_id)
            if entry is None or entry.expired or now >= entry.expires_at_monotonic:
                if entry is not None and entry.active_readers == 0:
                    self._delete_locked(snapshot_id)
                raise QuerySnapshotCacheError(
                    "QUERY_CURSOR_EXPIRED",
                    "The query cursor snapshot expired, was evicted, or was lost after restart",
                )
            entry.active_readers += 1
            self._entries.move_to_end(snapshot_id)
            return QuerySnapshotLease(self, entry)

    async def acquire_for_attempt(self, attempt_id: str) -> QuerySnapshotLease:
        """Acquire the one process-private snapshot currently owned by an Attempt."""

        async with self._condition:
            self._require_open_locked()
            now = self._clock()
            self._purge_expired_locked(now)
            snapshot_id = self._attempt_snapshots.get(attempt_id)
            entry = None if snapshot_id is None else self._entries.get(snapshot_id)
            if entry is None or entry.expired or now >= entry.expires_at_monotonic:
                if snapshot_id is not None:
                    self._attempt_snapshots.pop(attempt_id, None)
                    if entry is not None and entry.active_readers == 0:
                        self._delete_locked(snapshot_id)
                raise QuerySnapshotCacheError(
                    "QUERY_CURSOR_EXPIRED",
                    "The query Attempt result is unavailable in this process",
                )
            entry.active_readers += 1
            self._entries.move_to_end(snapshot_id)
            return QuerySnapshotLease(self, entry)

    async def available_for_attempt(self, attempt_id: str, *, scope_digest: str) -> bool:
        """Report current process availability without pinning or changing LRU order."""

        async with self._condition:
            self._require_open_locked()
            now = self._clock()
            self._purge_expired_locked(now)
            snapshot_id = self._attempt_snapshots.get(attempt_id)
            entry = None if snapshot_id is None else self._entries.get(snapshot_id)
            return bool(
                entry is not None
                and not entry.expired
                and now < entry.expires_at_monotonic
                and entry.record.scope_digest == scope_digest
            )

    async def discard_for_attempt(self, attempt_id: str) -> None:
        """Discard one process-private failed result without changing durable facts."""

        async with self._condition:
            snapshot_id = self._attempt_snapshots.pop(attempt_id, None)
            if snapshot_id is None:
                return
            entry = self._entries.get(snapshot_id)
            if entry is not None and entry.active_readers == 0:
                self._delete_locked(snapshot_id)
            elif entry is not None:
                entry.expired = True

    async def discard_for_attempt_best_effort(
        self,
        attempt_id: str,
        *,
        timeout_seconds: float,
    ) -> None:
        """Bound failed-result cleanup without competing with durable terminal facts."""

        if timeout_seconds <= 0:
            return
        try:
            async with asyncio.timeout(timeout_seconds):
                await self.discard_for_attempt(attempt_id)
        except Exception:
            return

    async def close(self) -> None:
        async with self._condition:
            if self._closed:
                wait_for_close = True
                entries: tuple[_Entry, ...] = ()
            else:
                self._closed = True
                wait_for_close = False
                while any(entry.active_readers for entry in self._entries.values()):
                    await self._condition.wait()
                entries = tuple(self._entries.values())
                self._entries.clear()
                self._attempt_snapshots.clear()
                self._total_bytes = 0
        if wait_for_close:
            await self._close_complete.wait()
            return
        try:
            while self._write_tasks:
                await asyncio.gather(*tuple(self._write_tasks), return_exceptions=True)
            while self._cleanup_tasks:
                await asyncio.gather(*tuple(self._cleanup_tasks), return_exceptions=True)
            await asyncio.to_thread(_remove_entries, entries)
            if self._owns_root:
                await asyncio.to_thread(shutil.rmtree, self._root, True)
            else:
                await asyncio.to_thread(_remove_snapshot_files, self._root)
        finally:
            self._close_complete.set()

    async def _finish_cancelled_write(
        self,
        write: asyncio.Task[tuple[int, int, tuple[int, ...], str]],
        path: Path,
        *,
        release_capacity: bool,
    ) -> None:
        try:
            await write
        except BaseException:
            pass
        finally:
            path.with_suffix(".tmp").unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            if release_capacity:
                self._write_capacity.release()

    @staticmethod
    def _snapshot_too_large() -> QuerySnapshotCacheError:
        return QuerySnapshotCacheError(
            "QUERY_RESULT_SNAPSHOT_TOO_LARGE",
            (
                "The complete query result snapshot exceeds 128 MiB; aggregate rows, "
                "select fewer columns, add WHERE filters, or publish a Package with "
                "data_derive"
            ),
        )

    async def _release(self, snapshot_id: str) -> None:
        async with self._condition:
            entry = self._entries.get(snapshot_id)
            if entry is None:
                return
            entry.active_readers -= 1
            if entry.active_readers < 0:
                raise RuntimeError("snapshot reader count became negative")
            if entry.active_readers == 0 and (
                entry.expired or self._clock() >= entry.expires_at_monotonic
            ):
                self._delete_locked(snapshot_id)
            self._condition.notify_all()

    async def _make_room_locked(self, incoming: _Entry) -> None:
        while True:
            scope_entries = [
                entry
                for entry in self._entries.values()
                if entry.record.scope_digest == incoming.record.scope_digest and not entry.expired
            ]
            over_scope = len(scope_entries) >= self._policy.per_scope_snapshots
            over_process_count = len(self._entries) >= self._policy.process_snapshots
            over_process_bytes = (
                self._total_bytes + incoming.record.size_bytes > self._policy.process_cache_bytes
            )
            if not (over_scope or over_process_count or over_process_bytes):
                return
            candidate = next(
                (
                    entry
                    for entry in self._entries.values()
                    if entry.active_readers == 0
                    and (
                        not over_scope or entry.record.scope_digest == incoming.record.scope_digest
                    )
                ),
                None,
            )
            if candidate is None:
                await self._condition.wait()
                self._require_open_locked()
                self._purge_expired_locked(self._clock())
                continue
            self._delete_locked(candidate.record.snapshot_id)

    def _purge_expired_locked(self, now: float) -> None:
        for snapshot_id, entry in tuple(self._entries.items()):
            if now < entry.expires_at_monotonic:
                continue
            entry.expired = True
            if entry.active_readers == 0:
                self._delete_locked(snapshot_id)

    def _delete_locked(self, snapshot_id: str) -> None:
        entry = self._entries.pop(snapshot_id, None)
        if entry is None:
            return
        self._total_bytes -= entry.record.size_bytes
        if self._attempt_snapshots.get(entry.record.attempt_id) == snapshot_id:
            self._attempt_snapshots.pop(entry.record.attempt_id, None)
        entry.path.unlink(missing_ok=True)

    def _require_open_locked(self) -> None:
        if self._closed:
            raise RuntimeError("snapshot cache is closed")


def _write_snapshot(
    path: Path,
    schema: pa.Schema,
    batches: Sequence[pa.RecordBatch],
    maximum_bytes: int,
) -> tuple[int, int, tuple[int, ...], str]:
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    total_rows = 0
    starts: list[int] = []
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with pa.ipc.new_file(_BoundedSnapshotFile(raw, maximum_bytes), schema) as writer:
                for batch in batches:
                    if batch.schema != schema:
                        raise ValueError("snapshot batch schema mismatch")
                    starts.append(total_rows)
                    total_rows += batch.num_rows
                    writer.write_batch(batch)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return path.stat().st_size, total_rows, tuple(starts), digest.hexdigest()
    except BaseException:
        temporary.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise


class _BoundedSnapshotFile:
    """Minimal writable file facade that rejects Arrow IPC growth before disk write."""

    def __init__(self, raw, maximum_bytes: int) -> None:
        self._raw = raw
        self._maximum_bytes = maximum_bytes

    @property
    def closed(self) -> bool:
        return self._raw.closed

    def writable(self) -> bool:
        return True

    def write(self, value) -> int:
        if self._raw.tell() + len(value) > self._maximum_bytes:
            raise _SnapshotTooLarge
        return self._raw.write(value)

    def tell(self) -> int:
        return self._raw.tell()

    def flush(self) -> None:
        self._raw.flush()


def _read_rows(
    path: Path,
    batch_starts: tuple[int, ...],
    start: int,
    limit: int,
) -> list[dict[str, object]]:
    if limit == 0:
        return []
    with path.open("rb") as raw:
        reader = pa.ipc.open_file(raw)
        if not batch_starts:
            return []
        batch_index = max(0, bisect_right(batch_starts, start) - 1)
        rows: list[dict[str, object]] = []
        position = start
        while batch_index < reader.num_record_batches and len(rows) < limit:
            batch = reader.get_batch(batch_index)
            batch_start = batch_starts[batch_index]
            offset = max(0, position - batch_start)
            take = min(limit - len(rows), batch.num_rows - offset)
            if take > 0:
                rows.extend(batch.slice(offset, take).to_pylist())
                position += take
            batch_index += 1
        return rows


def _remove_entries(entries: Sequence[_Entry]) -> None:
    for entry in entries:
        entry.path.unlink(missing_ok=True)


def _remove_snapshot_files(root: Path) -> None:
    for pattern in ("snap_*.arrow", "snap_*.tmp"):
        for path in root.glob(pattern):
            path.unlink(missing_ok=True)


__all__ = [
    "QuerySnapshotCache",
    "QuerySnapshotCacheError",
    "QuerySnapshotLease",
    "QuerySnapshotPolicy",
    "QuerySnapshotRecord",
    "QuerySnapshotWrite",
]
