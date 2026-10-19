"""In-process and host-shared capacity for controlled local workers."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import os
import stat
import tempfile
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path

from CAD2BIM_backend.domain.runtime_execution import RuntimeExecutionPolicy, DEFAULT_MAX_WAITING_REQUESTS
from CAD2BIM_backend.infrastructure.execution_observability import observe_execution_phase


_CURRENT_EXECUTION: ContextVar[WorkerExecutionLease | None] = ContextVar(
    "cad2bim_worker_execution_lease", default=None,
)
_CURRENT_INVOCATION: ContextVar[WorkerInvocationBudget | None] = ContextVar(
    "cad2bim_worker_invocation_budget", default=None,
)


class WorkerInvocationBudget:
    """One call's finite time allowance across publication-separated worker leases."""

    def __init__(self, capacity: HostWorkerCapacity, policy: RuntimeExecutionPolicy, deadline: float) -> None:
        self.capacity = capacity
        self.policy = policy
        self.deadline = deadline
        self.queue_spent = 0.0
        self.execution_spent = 0.0
        self.active: WorkerExecutionLease | None = None
        self.closed = False
        self.execution_started = False

    def begin(self, lease: WorkerExecutionLease) -> float:
        if self.closed or self.active is not None or lease.capacity.identity != self.capacity.identity:
            raise WorkerCapacityUnavailable("invocation budget cannot be reused concurrently or across pools")
        remaining_execution = self.policy.execution_seconds - self.execution_spent
        if remaining_execution <= 0:
            raise WorkerCapacityBusy(
                "worker invocation has insufficient remaining execution budget",
                reason="execution_budget_exhausted",
            )
        lease.policy = replace(lease.policy,
            execution_seconds=min(lease.policy.execution_seconds, remaining_execution))
        self.active = lease
        return min(
            time.monotonic() + max(0.0, self.policy.queue_seconds - self.queue_spent),
            self.deadline - remaining_execution - self.policy.terminal_seconds - self.policy.cleanup_seconds,
        )


class WorkerExecutionLease:
    """One task's ephemeral Host ownership across sequential worker stages."""

    def __init__(
        self, capacity: HostWorkerCapacity, policy: RuntimeExecutionPolicy, deadline: float,
        invocation: WorkerInvocationBudget | None = None,
    ) -> None:
        self.capacity = capacity
        self.policy = policy
        self.deadline = deadline
        self.owner = asyncio.current_task()
        self.descriptor: int | None = None
        self.execution_deadline: float | None = None
        self.execution_started = False
        self._active_stage = False
        self._released = False
        self._local_acquired = False
        self.admitted_at: float | None = None
        self.invocation = invocation

    def require_owner(self) -> None:
        if asyncio.current_task() is not self.owner:
            raise WorkerCapacityUnavailable("worker execution scope belongs to another request")

    async def admit(self) -> WorkerExecutionLease:
        self.require_owner()
        if self._released:
            raise WorkerCapacityUnavailable("worker execution chain has already ended")
        if self.descriptor is None:
            invocation_queue_deadline = self.invocation.begin(self) if self.invocation is not None else None
            queue_deadline = min(
                time.monotonic() + self.policy.queue_seconds,
                self.deadline - self.policy.execution_seconds - self.policy.terminal_seconds - self.policy.cleanup_seconds,
            )
            if invocation_queue_deadline is not None:
                queue_deadline = min(queue_deadline, invocation_queue_deadline)
            queue_started = time.monotonic()
            try:
                self.descriptor = await self.capacity.acquire_host(queue_deadline)
            finally:
                if self.invocation is not None:
                    self.invocation.queue_spent += time.monotonic() - queue_started
            try:
                async with asyncio.timeout_at(queue_deadline):
                    await self.capacity.acquire()
                self._local_acquired = True
            except BaseException:
                os.close(self.descriptor)
                self.descriptor = None
                raise
            self.admitted_at = time.monotonic()
            self.execution_deadline = min(
                time.monotonic() + self.policy.execution_seconds,
                self.deadline - self.policy.terminal_seconds - self.policy.cleanup_seconds,
            )
        return self

    @contextmanager
    def stage(self):
        self.require_owner()
        if self.descriptor is None or self._released or self._active_stage:
            raise WorkerCapacityUnavailable("worker execution stage has no exclusive lease")
        self._active_stage = True
        try:
            with observe_execution_phase("worker_capacity", "stage", details={
                "resource_pool": self.capacity.resource_pool,
                "prior_execution_started": self.execution_started,
                "execution_deadline": self.execution_deadline,
            }):
                yield
        finally:
            self._active_stage = False

    def release(self) -> None:
        self.require_owner()
        if self._active_stage:
            raise WorkerCapacityUnavailable("worker computation must stop before capacity release")
        if self._released:
            return
        with observe_execution_phase("worker_capacity", "release", details={
            "resource_pool": self.capacity.resource_pool,
            "execution_started": self.execution_started,
            "held_seconds": time.monotonic() - self.admitted_at if self.admitted_at is not None else 0.0,
        }):
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None
            if self._local_acquired:
                self.capacity.release()
                self._local_acquired = False
            self._released = True
            if self.invocation is not None and self.invocation.active is self:
                if self.admitted_at is not None:
                    self.invocation.execution_spent += time.monotonic() - self.admitted_at
                self.invocation.execution_started |= self.execution_started
                self.invocation.active = None


def current_execution_lease() -> WorkerExecutionLease | None:
    lease = _CURRENT_EXECUTION.get()
    if lease is not None:
        lease.require_owner()
    return lease


class WorkerCapacityBusy(RuntimeError):
    def __init__(self, message: str, *, reason: str = "queue_timeout", waited_seconds: float = 0.0) -> None:
        super().__init__(message)
        self.reason = reason
        self.waited_seconds = waited_seconds
        self.retryable = reason in {"queue_timeout", "queue_full"}


class WorkerCapacityUnavailable(RuntimeError):
    pass


def default_capacity_root(*, pool: str = "python") -> Path:
    # Preserve the established host lock path so old Python/Arrow workers and
    # new adapters contend for the same single controlled-local slot.
    roots = {"python": "cad2bim-python-capacity", "sql": "cad2bim-transform-capacity",
             "cad": "cad2bim-m3-capacity"}
    return Path(tempfile.gettempdir()) / f"{roots[pool]}-{os.getuid()}"


class HostWorkerCapacity:
    """Bound workers in this process and across backend processes on one host."""

    def __init__(
        self,
        *,
        max_concurrent_workers: int,
        capacity_root: str | Path | None = None,
        lock_name: str = "slot-00.lock",
        max_waiting_requests: int = DEFAULT_MAX_WAITING_REQUESTS,
        resource_pool: str = "python",
    ) -> None:
        if not 1 <= max_concurrent_workers <= 128:
            raise ValueError("worker capacity must be between 1 and 128")
        if not lock_name or Path(lock_name).name != lock_name:
            raise ValueError("worker capacity lock name must be a filename")
        if not 1 <= max_waiting_requests <= 128:
            raise ValueError("worker waiting capacity must be between 1 and 128")
        if max_concurrent_workers > 1 and lock_name != "slot-00.lock":
            raise ValueError("multiple worker slots require the standard slot filenames")
        self._semaphore = asyncio.Semaphore(max_concurrent_workers)
        self._slot_count = max_concurrent_workers
        self._max_waiters = max_waiting_requests
        self._capacity_root = Path(capacity_root or default_capacity_root())
        self._lock_name = lock_name
        if resource_pool not in {"python", "sql", "cad"}:
            raise ValueError("worker resource pool must be python, sql, or cad")
        self.resource_pool = resource_pool
        self._prepare_root()

    def failure_diagnostic(self, error: WorkerCapacityBusy | WorkerCapacityUnavailable) -> dict[str, object]:
        lease = current_execution_lease()
        invocation = _CURRENT_INVOCATION.get()
        return {
            "resource_pool": self.resource_pool,
            "reason": error.reason if isinstance(error, WorkerCapacityBusy) else "capacity_unavailable",
            "waited_seconds": round(error.waited_seconds, 3) if isinstance(error, WorkerCapacityBusy) else None,
            "phase": "admission",
            "execution_started": bool(
                (lease is not None and lease.execution_started)
                or (invocation is not None and invocation.execution_started)
            ),
            "current_stage_started": False,
        }

    def failure_message(self, error: WorkerCapacityBusy | WorkerCapacityUnavailable) -> str:
        """Use the existing bounded diagnostic message when a port has no detail field."""
        summary = "worker capacity is busy" if isinstance(error, WorkerCapacityBusy) else "worker capacity is unavailable"
        return summary + "; diagnostic=" + json.dumps(
            self.failure_diagnostic(error), separators=(",", ":"), allow_nan=False,
        )

    @asynccontextmanager
    async def invocation_scope(self, policy: RuntimeExecutionPolicy, *, deadline: float | None = None):
        prior = _CURRENT_INVOCATION.get()
        if prior is not None:
            if prior.capacity.identity != self.identity or prior.closed:
                raise WorkerCapacityUnavailable("invocation budget belongs to a different resource pool")
            yield prior
            return
        budget = WorkerInvocationBudget(
            self, policy, time.monotonic() + policy.total_seconds if deadline is None
            else min(deadline, time.monotonic() + policy.total_seconds),
        )
        token = _CURRENT_INVOCATION.set(budget)
        try:
            async with asyncio.timeout_at(budget.deadline - policy.cleanup_seconds):
                yield budget
        finally:
            budget.closed = True
            _CURRENT_INVOCATION.reset(token)

    @property
    def identity(self) -> tuple:
        info = self._capacity_root.stat()
        return (info.st_dev, info.st_ino, self._lock_name, self._slot_count, self._max_waiters)

    @asynccontextmanager
    async def execution_scope(self, policy: RuntimeExecutionPolicy, *, deadline: float | None = None):
        prior = current_execution_lease()
        if prior is not None:
            if prior.capacity.identity != self.identity:
                raise WorkerCapacityUnavailable("nested execution cannot hold different resource pools")
            yield prior
            return
        invocation = _CURRENT_INVOCATION.get()
        resolved_deadline = time.monotonic() + policy.total_seconds if deadline is None else deadline
        if invocation is not None:
            resolved_deadline = invocation.deadline if deadline is None else min(deadline, invocation.deadline)
        lease = WorkerExecutionLease(self, policy, resolved_deadline, invocation)
        token = _CURRENT_EXECUTION.set(lease)
        try:
            yield lease
        finally:
            try:
                lease.release()
            finally:
                _CURRENT_EXECUTION.reset(token)

    async def admit_current_execution(self) -> WorkerExecutionLease:
        lease = current_execution_lease()
        if lease is None or lease.capacity.identity != self.identity:
            raise WorkerCapacityUnavailable("worker execution scope is not configured")
        return await lease.admit()

    async def acquire(self) -> bool:
        return await self._semaphore.acquire()

    def release(self) -> None:
        self._semaphore.release()

    def locked(self) -> bool:
        return self._semaphore.locked()

    def configure_idle_host(self) -> bool:
        """Explicitly select this slot count while the existing host pool is idle.

        Ordinary admission never changes a conflicting configuration. Controlled
        local preparation may use this boundary before starting all participants
        with the same selected count. No slot or ticket inode is replaced.
        """
        mutex = self._try_lock("queue.lock")
        if mutex is None:
            raise WorkerCapacityUnavailable("worker host capacity configuration is busy")
        probes = []
        try:
            state = self._read_queue(allow_slot_mismatch=True)
            if state["slots"] == self._slot_count:
                return False
            if state["waiters"]:
                raise WorkerCapacityUnavailable("worker host capacity has pending requests")
            names = [f"wait-{index:03d}.lock" for index in range(self._max_waiters)]
            names.extend(f"slot-{index:02d}.lock" for index in range(max(state["slots"], self._slot_count)))
            for name in names:
                descriptor = self._try_lock(name)
                if descriptor is None:
                    raise WorkerCapacityUnavailable("worker host capacity is not idle")
                probes.append(descriptor)
            state["slots"] = self._slot_count
            self._write_queue(state)
            return True
        finally:
            for descriptor in probes:
                os.close(descriptor)
            os.close(mutex)

    async def acquire_host(self, deadline: float) -> int:
        details: dict[str, object] = {
            "resource_pool": self.resource_pool, "slot_count": self._slot_count,
            "max_waiting_requests": self._max_waiters, "admitted": False,
        }
        started = time.monotonic()
        with observe_execution_phase("worker_capacity", "queue", details=details):
            try:
                descriptor = await self._acquire_host(deadline)
                details["admitted"] = True
                return descriptor
            except WorkerCapacityBusy as exc:
                details["reason"] = exc.reason
                raise
            except WorkerCapacityUnavailable:
                details["reason"] = "capacity_unavailable"
                raise
            finally:
                details["waited_seconds"] = time.monotonic() - started

    async def _acquire_host(self, deadline: float) -> int:
        """Join a bounded Host FIFO and return one owned, crash-safe slot FD.

        Queue and ticket lock inodes are never removed. Closing the request's
        ticket descriptor makes cancellation/crash immediately observable; the
        next short critical section drops that inactive queue entry.
        """

        started = time.monotonic()
        ticket: tuple[int, int] | None = None
        try:
            while True:
                if time.monotonic() >= deadline:
                    raise WorkerCapacityBusy(
                        "worker host capacity is busy: queue deadline reached",
                        waited_seconds=time.monotonic() - started,
                    )
                mutex = self._try_lock("queue.lock")
                if mutex is not None:
                    slot = None
                    try:
                        state = self._read_queue()
                        live = []
                        for entry in state["waiters"]:
                            probe = self._try_lock(f"wait-{entry[0]:03d}.lock")
                            if probe is None:
                                live.append(entry)
                            else:
                                os.close(probe)
                        changed = live != state["waiters"]
                        state["waiters"] = live
                        if ticket is None:
                            if len(live) >= self._max_waiters:
                                raise WorkerCapacityBusy(
                                    "worker host capacity is busy: queue is full",
                                    reason="queue_full", waited_seconds=time.monotonic() - started,
                                )
                            occupied = {entry[0] for entry in live}
                            ordinal = next(i for i in range(self._max_waiters) if i not in occupied)
                            descriptor = self._try_lock(f"wait-{ordinal:03d}.lock")
                            if descriptor is None:
                                raise WorkerCapacityUnavailable("worker capacity ticket state is inconsistent")
                            ticket = (ordinal, descriptor)
                            state["waiters"].append([ordinal, state["next_sequence"]])
                            state["next_sequence"] += 1
                            changed = True
                        if state["waiters"][0][0] == ticket[0]:
                            for ordinal in range(self._slot_count):
                                name = self._lock_name if self._slot_count == 1 else f"slot-{ordinal:02d}.lock"
                                slot = self._try_lock(name)
                                if slot is not None:
                                    state["waiters"].pop(0)
                                    changed = True
                                    break
                        if changed:
                            self._write_queue(state)
                    except BaseException:
                        if slot is not None:
                            os.close(slot)
                        raise
                    finally:
                        os.close(mutex)
                    if slot is not None:
                        return slot
                await asyncio.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        except OSError as exc:
            raise WorkerCapacityUnavailable("worker capacity facility is unavailable") from exc
        finally:
            if ticket is not None:
                os.close(ticket[1])

    def _open_file(self, name: str, *, truncate: bool = False) -> int:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self._capacity_root / name, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise WorkerCapacityUnavailable("worker capacity file is unsafe")
            os.fchmod(descriptor, 0o600)
            if truncate:
                os.ftruncate(descriptor, 0)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _try_lock(self, name: str) -> int | None:
        descriptor = self._open_file(name)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except OSError as exc:
            os.close(descriptor)
            if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                return None
            raise

    def _read_queue(self, *, allow_slot_mismatch: bool = False) -> dict:
        path = self._capacity_root / "queue-state.json"
        if path.is_symlink():
            raise WorkerCapacityUnavailable("worker capacity queue metadata is unsafe")
        if not path.exists():
            for ordinal in range(self._max_waiters):
                probe = self._try_lock(f"wait-{ordinal:03d}.lock")
                if probe is None:
                    raise WorkerCapacityUnavailable("worker capacity queue metadata is missing")
                os.close(probe)
            return {"slots": self._slot_count, "max_waiters": self._max_waiters,
                    "lock_name": self._lock_name, "next_sequence": 0, "waiters": []}
        try:
            with os.fdopen(self._open_file("queue-state.json"), "rb") as stream:
                state = json.loads(stream.read(65_537))
            if (
                not isinstance(state, dict)
                or set(state) != {"slots", "max_waiters", "lock_name", "next_sequence", "waiters"}
                or type(state["slots"]) is not int or not 1 <= state["slots"] <= 128
                or (not allow_slot_mismatch and state["slots"] != self._slot_count)
                or state["max_waiters"] != self._max_waiters
                or state["lock_name"] != self._lock_name
                or type(state["next_sequence"]) is not int
                or not 0 <= state["next_sequence"] < 2**63 - 1
                or not isinstance(state["waiters"], list)
                or len(state["waiters"]) > self._max_waiters
            ):
                raise ValueError("invalid capacity metadata")
            ordinals = set()
            previous = -1
            for entry in state["waiters"]:
                if (
                    not isinstance(entry, list) or len(entry) != 2
                    or any(type(value) is not int for value in entry)
                    or not 0 <= entry[0] < self._max_waiters
                    or entry[0] in ordinals
                    or not previous < entry[1] < state["next_sequence"]
                ):
                    raise ValueError("invalid capacity ticket metadata")
                ordinals.add(entry[0])
                previous = entry[1]
            return state
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkerCapacityUnavailable("worker capacity queue metadata is invalid or incompatible") from exc

    def _write_queue(self, state: dict) -> None:
        with os.fdopen(self._open_file("queue-state.pending", truncate=True), "w") as stream:
            json.dump(state, stream, separators=(",", ":"))
            stream.flush()
        os.replace(self._capacity_root / "queue-state.pending", self._capacity_root / "queue-state.json")

    def _prepare_root(self) -> None:
        if self._capacity_root.is_symlink():
            raise ValueError("worker capacity root cannot be a symlink")
        self._capacity_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self._capacity_root.is_dir():
            raise ValueError("worker capacity root must be a directory")
        os.chmod(self._capacity_root, 0o700)


__all__ = [
    "HostWorkerCapacity",
    "WorkerCapacityBusy",
    "WorkerCapacityUnavailable",
    "default_capacity_root",
]
