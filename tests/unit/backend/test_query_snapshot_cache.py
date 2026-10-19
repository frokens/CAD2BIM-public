from __future__ import annotations

import asyncio
import hashlib
import threading
from pathlib import Path

import pyarrow as pa
import pytest

from CAD2BIM_backend.domain.data_query_sql import (
    QuerySqlColumn,
    QuerySqlResponseEncoding,
    QuerySqlRowSchema,
    QuerySqlStatistics,
)
from CAD2BIM_backend.infrastructure.data_query.snapshot_cache import (
    QuerySnapshotCache,
    QuerySnapshotCacheError,
    QuerySnapshotPolicy,
    QuerySnapshotWrite,
)
from CAD2BIM_backend.infrastructure.data_query import snapshot_cache as snapshot_cache_module


pytestmark = pytest.mark.asyncio


def _write(scope: str, value: int = 1) -> QuerySnapshotWrite:
    batch = pa.record_batch({"value": [value, value + 1]})
    return QuerySnapshotWrite(
        attempt_id=f"attempt-{scope}-{value}",
        scope_digest=scope,
        exact_input_digest="input-digest",
        query_fingerprint=f"query-{value}",
        row_schema=QuerySqlRowSchema(
            columns=(
                QuerySqlColumn(
                    name="value",
                    logical_type="BIGINT",
                    nullable=False,
                    response_encoding=QuerySqlResponseEncoding.JSON_INTEGER,
                ),
            )
        ),
        page_size=100,
        query_statistics=QuerySqlStatistics(
            bound_input_payload_count=1,
            bound_input_row_count=2,
            bound_input_stored_bytes=10,
            execution_ms=1,
        ),
        arrow_schema=batch.schema,
        batches=(batch,),
    )


async def test_snapshot_is_immutable_readable_and_cleaned_on_close(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    cache = QuerySnapshotCache(root=root)
    record = await cache.create(_write("scope-a"), row_schema_digest="schema")

    snapshot_path = root / f"{record.snapshot_id}.arrow"
    assert record.snapshot_sha256 == hashlib.sha256(snapshot_path.read_bytes()).hexdigest()

    async with await cache.acquire(record.snapshot_id) as lease:
        assert await lease.read_rows(1, 1) == [{"value": 2}]
        assert lease.record.expires_at_unix_ms - lease.record.created_at_unix_ms == 900_000

    assert list(root.glob("*.arrow"))
    await cache.close()
    assert not list(root.glob("*.arrow"))


async def test_ttl_is_absolute_and_access_does_not_extend_it(tmp_path: Path) -> None:
    now = [100.0]
    cache = QuerySnapshotCache(root=tmp_path, clock=lambda: now[0], wall_clock=lambda: 1_000.0)
    record = await cache.create(_write("scope-a"), row_schema_digest="schema")

    now[0] = 999.0
    async with await cache.acquire(record.snapshot_id):
        pass
    now[0] = 1_000.0
    with pytest.raises(QuerySnapshotCacheError) as expired:
        await cache.acquire(record.snapshot_id)

    assert expired.value.code == "QUERY_CURSOR_EXPIRED"
    await cache.close()


async def test_per_scope_limit_uses_access_order_lru(tmp_path: Path) -> None:
    cache = QuerySnapshotCache(root=tmp_path)
    records = [
        await cache.create(_write("scope-a", index), row_schema_digest="schema")
        for index in range(4)
    ]
    async with await cache.acquire(records[0].snapshot_id):
        pass
    fifth = await cache.create(_write("scope-a", 9), row_schema_digest="schema")

    with pytest.raises(QuerySnapshotCacheError) as evicted:
        await cache.acquire(records[1].snapshot_id)
    assert evicted.value.code == "QUERY_CURSOR_EXPIRED"
    for record in (records[0], records[2], records[3], fifth):
        async with await cache.acquire(record.snapshot_id):
            pass
    await cache.close()


async def test_process_count_and_byte_limits_evict_global_lru(tmp_path: Path) -> None:
    sample_cache = QuerySnapshotCache(root=tmp_path / "sample")
    sample = await sample_cache.create(_write("sample"), row_schema_digest="schema")
    await sample_cache.close()
    policy = QuerySnapshotPolicy(
        per_snapshot_bytes=sample.size_bytes + 16,
        per_scope_snapshots=4,
        process_snapshots=8,
        process_cache_bytes=sample.size_bytes * 2 + 16,
    )
    cache = QuerySnapshotCache(root=tmp_path / "bounded", policy=policy)
    first = await cache.create(_write("scope-a", 1), row_schema_digest="schema")
    await cache.create(_write("scope-b", 2), row_schema_digest="schema")
    await cache.create(_write("scope-c", 3), row_schema_digest="schema")

    with pytest.raises(QuerySnapshotCacheError) as evicted:
        await cache.acquire(first.snapshot_id)
    assert evicted.value.code == "QUERY_CURSOR_EXPIRED"
    await cache.close()


async def test_oversized_snapshot_fails_instead_of_truncating(tmp_path: Path) -> None:
    cache = QuerySnapshotCache(
        root=tmp_path,
        policy=QuerySnapshotPolicy(
            per_snapshot_bytes=1,
            process_cache_bytes=10,
        ),
    )

    with pytest.raises(QuerySnapshotCacheError) as oversized:
        await cache.create(_write("scope-a"), row_schema_digest="schema")

    assert oversized.value.code == "QUERY_RESULT_SNAPSHOT_TOO_LARGE"
    assert not list(tmp_path.glob("*.arrow"))
    await cache.close()


async def test_cancelled_writer_transfers_cleanup_until_thread_finishes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    started = threading.Event()
    finish = threading.Event()
    original = snapshot_cache_module._write_snapshot

    def delayed_write(*args):
        started.set()
        if not finish.wait(timeout=2):
            raise RuntimeError("test writer was not released")
        return original(*args)

    monkeypatch.setattr(snapshot_cache_module, "_write_snapshot", delayed_write)
    cache = QuerySnapshotCache(root=tmp_path)
    create = asyncio.create_task(cache.create(_write("scope-a"), row_schema_digest="schema"))
    assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)

    create.cancel()
    with pytest.raises(asyncio.CancelledError):
        await create
    finish.set()
    await cache.close()

    assert not list(tmp_path.glob("*.arrow"))
    assert not list(tmp_path.glob("*.tmp"))


async def test_close_blocks_new_work_and_waits_for_inflight_writer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    started = threading.Event()
    finish = threading.Event()
    original = snapshot_cache_module._write_snapshot

    def delayed_write(*args):
        started.set()
        if not finish.wait(timeout=2):
            raise RuntimeError("test writer was not released")
        return original(*args)

    monkeypatch.setattr(snapshot_cache_module, "_write_snapshot", delayed_write)
    cache = QuerySnapshotCache(root=tmp_path)
    create = asyncio.create_task(cache.create(_write("scope-a"), row_schema_digest="schema"))
    assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)
    close = asyncio.create_task(cache.close())
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="closed"):
        await cache.acquire("snap_never-created")
    finish.set()
    await close
    with pytest.raises(RuntimeError, match="closed"):
        await create

    assert not list(tmp_path.glob("*.arrow"))
    assert not list(tmp_path.glob("*.tmp"))


async def test_pending_snapshot_writes_are_bounded_to_worker_concurrency(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock = threading.Lock()
    two_started = threading.Event()
    finish = threading.Event()
    calls = 0
    active = 0
    maximum_active = 0
    original = snapshot_cache_module._write_snapshot

    def delayed_write(*args):
        nonlocal active, calls, maximum_active
        with lock:
            calls += 1
            active += 1
            maximum_active = max(maximum_active, active)
            if calls == 2:
                two_started.set()
        try:
            if not finish.wait(timeout=2):
                raise RuntimeError("test writers were not released")
            return original(*args)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(snapshot_cache_module, "_write_snapshot", delayed_write)
    cache = QuerySnapshotCache(root=tmp_path)
    creates = [
        asyncio.create_task(
            cache.create(_write(f"scope-{index}", index), row_schema_digest="schema")
        )
        for index in range(3)
    ]
    assert await asyncio.wait_for(asyncio.to_thread(two_started.wait, 1), timeout=2)
    await asyncio.sleep(0.02)

    assert calls == 2
    assert maximum_active == 2
    finish.set()
    await asyncio.gather(*creates)
    await cache.close()


async def test_written_but_unregistered_snapshots_keep_capacity_until_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = QuerySnapshotPolicy(per_scope_snapshots=1, process_snapshots=1)
    cache = QuerySnapshotCache(root=tmp_path, policy=policy)
    existing = await cache.create(_write("scope-pinned", 1), row_schema_digest="schema")
    lease = await cache.acquire(existing.snapshot_id)
    lock = threading.Lock()
    writes = 0
    original = snapshot_cache_module._write_snapshot

    def counted_write(*args):
        nonlocal writes
        with lock:
            writes += 1
        return original(*args)

    monkeypatch.setattr(snapshot_cache_module, "_write_snapshot", counted_write)
    creates = [
        asyncio.create_task(
            cache.create(_write(f"scope-{index}", index + 10), row_schema_digest="schema")
        )
        for index in range(5)
    ]
    for _ in range(100):
        if writes == 2 and len(list(tmp_path.glob("*.arrow"))) == 3:
            break
        await asyncio.sleep(0.01)

    assert writes == 2
    assert len(list(tmp_path.glob("*.arrow"))) == 3
    for create in creates:
        create.cancel()
    await asyncio.gather(*creates, return_exceptions=True)
    assert len(list(tmp_path.glob("*.arrow"))) == 1

    more_creates = [
        asyncio.create_task(
            cache.create(_write(f"scope-close-{index}", index + 20), row_schema_digest="schema")
        )
        for index in range(5)
    ]
    for _ in range(100):
        if writes == 4 and len(list(tmp_path.glob("*.arrow"))) == 3:
            break
        await asyncio.sleep(0.01)
    assert writes == 4
    assert len(list(tmp_path.glob("*.arrow"))) == 3

    close = asyncio.create_task(cache.close())
    await asyncio.sleep(0)
    await lease.release()
    await close
    await asyncio.gather(*more_creates, return_exceptions=True)
    assert not list(tmp_path.glob("*.arrow"))
    assert not list(tmp_path.glob("*.tmp"))
