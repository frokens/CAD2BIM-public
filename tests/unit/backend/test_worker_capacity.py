from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import psutil

from CAD2BIM_backend.infrastructure.worker_runtime.capacity import (
    HostWorkerCapacity, WorkerCapacityBusy, WorkerCapacityUnavailable,
)
from CAD2BIM_backend.domain.runtime_execution import RuntimeExecutionPolicy


async def _waiters(root: Path, count: int) -> None:
    async with asyncio.timeout(5):
        while True:
            path = root / "queue-state.json"
            if path.exists() and len(json.loads(path.read_text())["waiters"]) == count:
                return
            await asyncio.sleep(0.01)


async def test_invocation_budget_survives_publication_and_sequential_settlement_task(tmp_path: Path) -> None:
    pool = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path)
    stage_policy = RuntimeExecutionPolicy(1, .03, .1, .1)
    async with pool.invocation_scope(RuntimeExecutionPolicy(1, .08, .1, .1)) as invocation:
        async with pool.execution_scope(stage_policy) as lease:
            await lease.admit()
            lease.execution_started = True
            await asyncio.sleep(.025)
        # Publication has no held slot, while its total deadline remains live.
        released = await pool.acquire_host(time.monotonic() + .1)
        os.close(released)

        async def settlement() -> None:
            async with pool.execution_scope(stage_policy) as next_lease:
                await next_lease.admit()
                assert next_lease.deadline == invocation.deadline
                await asyncio.sleep(.03)

        await asyncio.create_task(settlement())
        async with pool.execution_scope(stage_policy) as next_lease:
            await next_lease.admit()
            assert 0 < next_lease.policy.execution_seconds < stage_policy.execution_seconds
            assert next_lease.deadline == invocation.deadline
        assert invocation.active is None
        assert not pool.locked()


async def test_next_lease_uses_remaining_invocation_execution_for_both_deadlines(tmp_path, monkeypatch):
    from CAD2BIM_backend.infrastructure.worker_runtime import capacity
    pool = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path)
    policy = RuntimeExecutionPolicy(600, 240)
    now = [time.monotonic()]
    started = now[0]
    monkeypatch.setattr(capacity, "time", SimpleNamespace(monotonic=lambda: now[0]))
    queue_deadlines = []
    original_acquire = pool.acquire_host
    async def capture_admission(deadline):
        queue_deadlines.append(deadline)
        return await original_acquire(deadline)
    monkeypatch.setattr(pool, "acquire_host", capture_admission)

    async with pool.invocation_scope(policy, deadline=started + 300) as invocation:
        async with pool.execution_scope(RuntimeExecutionPolicy(600, 45)) as rendered:
            await rendered.admit()
            rendered.execution_started = True
            now[0] += 5
        assert invocation.execution_spent == 5
        async with pool.execution_scope(policy) as product:
            await product.admit()
            assert product.policy.execution_seconds == 235
            assert product.policy.terminal_seconds == policy.terminal_seconds
            assert product.policy.cleanup_seconds == policy.cleanup_seconds
            assert queue_deadlines[-1] == started + 45
            assert product.execution_deadline == started + 240
            assert product.execution_deadline <= invocation.deadline - policy.terminal_seconds - policy.cleanup_seconds
            assert product.deadline == invocation.deadline == started + 300
            now[0] = product.execution_deadline
        assert invocation.execution_spent == policy.execution_seconds
        with pytest.raises(WorkerCapacityBusy) as rejected:
            async with pool.execution_scope(policy) as exhausted:
                await exhausted.admit()
        assert rejected.value.reason == "execution_budget_exhausted"
        assert rejected.value.retryable is False
        assert len(queue_deadlines) == 2
        assert invocation.active is None
        diagnostic = pool.failure_diagnostic(rejected.value)
        assert diagnostic["execution_started"] is True
        assert diagnostic["current_stage_started"] is False
        assert not pool.locked()


async def test_invocation_does_not_reset_queue_budget_between_publications(tmp_path: Path) -> None:
    pool = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path)
    stage_policy = RuntimeExecutionPolicy(.2, .01, .1, .1)
    async with pool.invocation_scope(RuntimeExecutionPolicy(.12, .05, .1, .1)) as invocation:
        held = await pool.acquire_host(time.monotonic() + .1)
        async def release_first() -> None:
            await asyncio.sleep(.04)
            os.close(held)
        release = asyncio.create_task(release_first())
        async with pool.execution_scope(stage_policy) as lease:
            await lease.admit()
        await release
        held_again = await pool.acquire_host(time.monotonic() + .1)
        try:
            with pytest.raises(WorkerCapacityBusy) as rejected:
                async with pool.execution_scope(stage_policy) as lease:
                    await lease.admit()
            assert rejected.value.reason == "queue_timeout"
            assert .10 <= invocation.queue_spent < .18
            assert invocation.active is None
        finally:
            os.close(held_again)


async def test_invocation_rejects_simultaneous_worker_leases(tmp_path: Path) -> None:
    pool = HostWorkerCapacity(max_concurrent_workers=2, capacity_root=tmp_path)
    policy = RuntimeExecutionPolicy(1, 1, 1, 1)
    admitted, release = asyncio.Event(), asyncio.Event()
    async def first() -> None:
        async with pool.execution_scope(policy) as lease:
            await lease.admit()
            admitted.set()
            await release.wait()
    async with pool.invocation_scope(policy):
        task = asyncio.create_task(first())
        await admitted.wait()
        try:
            with pytest.raises(WorkerCapacityUnavailable, match="concurrently"):
                async with pool.execution_scope(policy) as lease:
                    await lease.admit()
        finally:
            release.set()
            await task


async def test_execution_scope_reuses_one_lease_deadline_and_rejects_parallel_inheritance(tmp_path: Path) -> None:
    pool = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path)
    policy = RuntimeExecutionPolicy(1, 1, 1, 1)
    async with pool.execution_scope(policy) as lease:
        assert lease.descriptor is None
        await pool.admit_current_execution()
        descriptor, deadline = lease.descriptor, lease.execution_deadline
        async with pool.execution_scope(RuntimeExecutionPolicy(10, 10, 10, 10)) as nested:
            assert nested is lease
            assert (await pool.admit_current_execution()).descriptor == descriptor
            assert nested.execution_deadline == deadline
        with lease.stage():
            with pytest.raises(WorkerCapacityUnavailable, match="exclusive lease"):
                with lease.stage():
                    pass
            with pytest.raises(WorkerCapacityUnavailable, match="must stop"):
                lease.release()

        async def inherited() -> None:
            async with pool.execution_scope(policy):
                pass

        with pytest.raises(WorkerCapacityUnavailable, match="another request"):
            await asyncio.create_task(inherited())
        lease.release()
        assert not pool.locked()
        with pytest.raises(WorkerCapacityUnavailable, match="already ended"):
            await pool.admit_current_execution()
    recovered = await pool.acquire_host(time.monotonic() + 1)
    os.close(recovered)


async def test_execution_scope_reserves_execution_terminal_cleanup_and_rejects_cross_pool(tmp_path: Path) -> None:
    pool = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path / "first")
    other = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path / "second")
    policy = RuntimeExecutionPolicy(0.02, 0.1, 0.1, 0.1)
    held = await pool.acquire_host(time.monotonic() + 1)
    try:
        async with pool.execution_scope(policy) as lease:
            with pytest.raises(WorkerCapacityBusy):
                await pool.admit_current_execution()
            assert lease.deadline - time.monotonic() > 0.25
            assert lease.execution_started is False
            with pytest.raises(WorkerCapacityUnavailable, match="different resource pools"):
                async with other.execution_scope(policy):
                    pass
    finally:
        os.close(held)


async def test_host_capacity_uses_distinct_real_slots_across_instances(tmp_path: Path) -> None:
    pools = [HostWorkerCapacity(max_concurrent_workers=2, capacity_root=tmp_path) for _ in range(3)]
    first = await pools[0].acquire_host(time.monotonic() + 1)
    second = await pools[1].acquire_host(time.monotonic() + 1)
    try:
        assert os.fstat(first).st_ino != os.fstat(second).st_ino
        with pytest.raises(WorkerCapacityBusy) as caught:
            await pools[2].acquire_host(time.monotonic() + 0.05)
        assert caught.value.reason == "queue_timeout"
        assert caught.value.waited_seconds >= 0.05
    finally:
        os.close(first)
        os.close(second)
    recovered = await pools[2].acquire_host(time.monotonic() + 1)
    os.close(recovered)


async def test_explicit_idle_slot_configuration_preserves_pool_locks_and_normal_mismatch_rejection(tmp_path):
    old = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path)
    held = await old.acquire_host(time.monotonic() + 1)
    inode = (tmp_path / "slot-00.lock").stat().st_ino
    original = (tmp_path / "queue-state.json").read_bytes()
    selected = HostWorkerCapacity(max_concurrent_workers=4, capacity_root=tmp_path)
    with pytest.raises(WorkerCapacityUnavailable):
        selected.configure_idle_host()
    assert (tmp_path / "queue-state.json").read_bytes() == original
    os.close(held)
    ticket = old._try_lock("wait-002.lock")
    try:
        with pytest.raises(WorkerCapacityUnavailable):
            selected.configure_idle_host()
        assert (tmp_path / "queue-state.json").read_bytes() == original
    finally:
        os.close(ticket)
    assert selected.configure_idle_host() is True
    assert (tmp_path / "slot-00.lock").stat().st_ino == inode
    assert json.loads((tmp_path / "queue-state.json").read_text())["slots"] == 4
    assert selected.configure_idle_host() is False
    with pytest.raises(WorkerCapacityUnavailable):
        await old.acquire_host(time.monotonic() + 1)
    descriptors = []
    try:
        for _ in range(4):
            descriptors.append(await selected.acquire_host(time.monotonic() + 1))
        assert len({os.fstat(fd).st_ino for fd in descriptors}) == 4
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


@pytest.mark.parametrize("cancel_index", [0, 1])
async def test_host_fifo_cancelled_head_or_middle_does_not_block_or_overtake(
    tmp_path: Path, cancel_index: int,
) -> None:
    pool = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path, max_waiting_requests=3)
    held = await pool.acquire_host(time.monotonic() + 1)
    tasks = []
    order = []
    released = asyncio.Event()

    async def wait(index: int) -> None:
        fd = await pool.acquire_host(time.monotonic() + 5)
        try:
            order.append(index)
            await released.wait()
        finally:
            os.close(fd)

    try:
        for index in range(3):
            tasks.append(asyncio.create_task(wait(index)))
            await _waiters(tmp_path, index + 1)
        tasks[cancel_index].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[cancel_index]
        os.close(held)
        held = None
        released.set()
        await asyncio.gather(*(task for i, task in enumerate(tasks) if i != cancel_index))
        assert order == [i for i in range(3) if i != cancel_index]
    finally:
        if held is not None:
            os.close(held)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_full_queue_rejects_before_execution_and_expired_request_cannot_take_free_slot(tmp_path: Path) -> None:
    pool = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path, max_waiting_requests=1)
    held = await pool.acquire_host(time.monotonic() + 1)
    queued = asyncio.create_task(pool.acquire_host(time.monotonic() + 5))
    try:
        await _waiters(tmp_path, 1)
        with pytest.raises(WorkerCapacityBusy) as caught:
            await pool.acquire_host(time.monotonic() + 1)
        assert caught.value.reason == "queue_full"
    finally:
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        os.close(held)
    with pytest.raises(WorkerCapacityBusy) as caught:
        await pool.acquire_host(time.monotonic() - 1)
    assert caught.value.reason == "queue_timeout"
    recovered = await pool.acquire_host(time.monotonic() + 1)
    os.close(recovered)


@pytest.mark.parametrize("failure", ["corrupt", "symlink", "incompatible"])
async def test_capacity_facility_faults_fail_closed_without_replacing_slot_inode(tmp_path: Path, failure: str) -> None:
    pool = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path)
    held = await pool.acquire_host(time.monotonic() + 1)
    inode = os.fstat(held).st_ino
    try:
        if failure == "corrupt":
            (tmp_path / "queue-state.json").write_text("{broken")
        elif failure == "symlink":
            (tmp_path / "queue-state.json").unlink()
            (tmp_path / "queue-state.json").symlink_to(tmp_path / "slot-00.lock")
        else:
            pool = HostWorkerCapacity(max_concurrent_workers=2, capacity_root=tmp_path)
        with pytest.raises(WorkerCapacityUnavailable) as caught:
            await pool.acquire_host(time.monotonic() + 1)
        assert str(tmp_path) not in str(caught.value)
        assert (tmp_path / "slot-00.lock").stat().st_ino == inode
    finally:
        os.close(held)


_CHILD = """
import asyncio, os, sys, time
from CAD2BIM_backend.infrastructure.worker_runtime.capacity import HostWorkerCapacity
async def main():
    pool = HostWorkerCapacity(max_concurrent_workers=int(sys.argv[2]) if len(sys.argv) > 2 else 1,
        capacity_root=sys.argv[1])
    fd = await pool.acquire_host(time.monotonic()+20)
    try:
        print('admitted', flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        os.close(fd)
asyncio.run(main())
"""


async def test_four_host_slots_hold_distinct_processes_while_fifth_waits(tmp_path: Path) -> None:
    """The round's configured four slots are shared across actual service processes."""
    import fcntl

    children = []
    fifth_read = None
    try:
        for _ in range(4):
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-B", "-c", _CHILD, str(tmp_path), "4",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")})
            children.append(child)
            assert await asyncio.wait_for(child.stdout.readline(), 5) == b"admitted\n"
        assert len({child.pid for child in children}) == 4
        for path in (tmp_path / f"slot-{index:02d}.lock" for index in range(4)):
            with path.open("rb") as stream:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fifth = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-c", _CHILD, str(tmp_path), "4",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")})
        children.append(fifth)
        fifth_read = asyncio.create_task(fifth.stdout.readline())
        await _waiters(tmp_path, 1)
        assert not fifth_read.done()
        children[0].stdin.write(b"release\n")
        await children[0].stdin.drain()
        assert await asyncio.wait_for(fifth_read, 5) == b"admitted\n"
        assert await children[0].wait() == 0
        for child in children[1:]:
            child.stdin.write(b"release\n")
            await child.stdin.drain()
        assert await asyncio.gather(*(child.wait() for child in children[1:])) == [0] * 4
        pool = HostWorkerCapacity(max_concurrent_workers=4, capacity_root=tmp_path)
        recovered = []
        try:
            for _ in range(4):
                recovered.append(await pool.acquire_host(time.monotonic() + 1))
            assert len({os.fstat(fd).st_ino for fd in recovered}) == 4
        finally:
            for fd in recovered:
                os.close(fd)
    finally:
        if fifth_read is not None and not fifth_read.done():
            fifth_read.cancel()
            await asyncio.gather(fifth_read, return_exceptions=True)
        for child in children:
            if child.returncode is None:
                child.kill()
        await asyncio.gather(*(child.wait() for child in children))


_PARENT = """
import asyncio, json, os, sys, time
from pathlib import Path
from CAD2BIM_backend.infrastructure.worker_runtime.capacity import HostWorkerCapacity
from CAD2BIM_backend.infrastructure.worker_runtime.supervisor import WorkerLaunchSpec, WorkerProcessSupervisor
async def main():
    pool = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=sys.argv[1])
    fd = await pool.acquire_host(time.monotonic()+5)
    try:
        code = 'import signal,time; '+('signal.signal(signal.SIGTERM, signal.SIG_IGN); ' if sys.argv[2]=='ignore' else '')+'print("ready",flush=True); time.sleep(30)'
        process = await WorkerProcessSupervisor.launch(
            WorkerLaunchSpec(command=(sys.executable, '-c', code), cwd=Path(sys.argv[1]),
                             env=os.environ, capacity_fds=(fd,)),
            deadline=time.monotonic()+5, cleanup_seconds=0.1)
        assert await process.stdout.readline() == b'ready\\n'
        print(json.dumps({'worker':WorkerProcessSupervisor.execution_pid(process),'guardian':process.pid}),flush=True)
        await process.wait()
    finally:
        os.close(fd)
asyncio.run(main())
"""


@pytest.mark.parametrize("term_behavior", ["normal", "ignore"])
async def test_parent_death_stops_real_worker_before_another_request_owns_slot(
    tmp_path: Path, term_behavior: str,
) -> None:
    parent = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _PARENT, str(tmp_path), term_behavior,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")},
    )
    pids = {}
    try:
        raw = await asyncio.wait_for(parent.stdout.readline(), 5)
        assert raw, (await parent.stderr.read()).decode()
        pids = json.loads(raw)
        pool = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path)
        with pytest.raises(WorkerCapacityBusy):
            await pool.acquire_host(time.monotonic() + 0.05)
        parent.kill()
        await parent.wait()
        recovered = await pool.acquire_host(time.monotonic() + 5)
        try:
            for pid in pids.values():
                try:
                    assert psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
                except psutil.NoSuchProcess:
                    pass
        finally:
            os.close(recovered)
    finally:
        if parent.returncode is None:
            parent.kill()
        await parent.wait()
        for pid in pids.values():
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass


async def test_host_fifo_and_dead_waiter_liveness_use_actual_process_locks(tmp_path: Path) -> None:
    pool = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path)
    held = await pool.acquire_host(time.monotonic() + 1)
    children = []
    try:
        for index in range(3):
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-c", _CHILD, str(tmp_path),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")},
            )
            children.append(child)
            await _waiters(tmp_path, index + 1)
        children[1].kill()
        await children[1].wait()
        os.close(held)
        held = None
        assert await asyncio.wait_for(children[0].stdout.readline(), 5) == b"admitted\n"
        # The third process is still waiting while the first owns the real slot.
        await _waiters(tmp_path, 1)
        with pytest.raises(WorkerCapacityBusy):
            await pool.acquire_host(time.monotonic() + 0.05)
        children[0].stdin.write(b"release\n")
        await children[0].stdin.drain()
        assert await asyncio.wait_for(children[2].stdout.readline(), 5) == b"admitted\n"
        children[2].stdin.write(b"release\n")
        await children[2].stdin.drain()
        assert await children[0].wait() == 0
        assert await children[2].wait() == 0
        recovered = await pool.acquire_host(time.monotonic() + 1)
        os.close(recovered)
    finally:
        if held is not None:
            os.close(held)
        for child in children:
            if child.returncode is None:
                child.kill()
        await asyncio.gather(*(child.wait() for child in children))
