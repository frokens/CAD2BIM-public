"""Neutral subprocess supervision for controlled local worker adapters."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
import threading
from dataclasses import asdict, dataclass, replace
import io
from contextlib import nullcontext, asynccontextmanager
import math
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Awaitable, Callable, Mapping

import psutil

from CAD2BIM_backend.infrastructure.execution_observability import (
    observe_execution_phase,
)
from CAD2BIM_backend.infrastructure.worker_runtime.capacity import current_execution_lease
from CAD2BIM_backend.infrastructure.worker_runtime.protocol import read_json_frame, WorkerProtocolError
from CAD2BIM_backend.infrastructure.worker_runtime.timing import MAX_TIMING_BYTES


@dataclass(frozen=True, slots=True)
class WorkerLaunchSpec:
    command: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    pass_fds: tuple[int, ...] = ()
    scratch: Path | None = None
    capacity_fds: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkerProcessBudget:
    wall_seconds: float
    cpu_seconds: float
    rss_bytes: int
    scratch_bytes: int
    threads: int
    open_file_descriptors: int
    stdout_bytes: int
    stderr_bytes: int
    cleanup_seconds: float


@dataclass(frozen=True, slots=True)
class WorkerProcessObservations:
    peak_rss_bytes: int = 0
    peak_cpu_seconds: float = 0.0
    peak_threads: int = 0
    peak_open_file_descriptors: int = 0
    peak_scratch_bytes: int = 0


@dataclass(frozen=True, slots=True)
class WorkerTerminal:
    returncode: int
    status_payload: Mapping[str, object] | None
    stdout: bytes
    stderr: bytes
    execution_started: bool
    observations: WorkerProcessObservations


class WorkerResourceViolation(StrEnum):
    """Stable resource identity while preserving existing string diagnostics."""

    SUBPROCESS = "worker created a forbidden subprocess"
    RSS = "worker exceeded its RSS budget"
    CPU = "worker exceeded its CPU budget"
    THREADS = "worker exceeded its native thread budget"
    OPEN_FILE_DESCRIPTORS = "worker exceeded its open-file-descriptor budget"
    SCRATCH = "worker exceeded its private scratch budget"


class WorkerSupervisorError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, execution_started: bool,
        resource_kind: str | None = None,
        budget_scope: str | None = None, limit: float | None = None,
        observed: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.execution_started = execution_started
        self.resource_kind = resource_kind
        self.budget_scope = budget_scope
        self.limit = limit
        self.observed = observed
        self.budget: WorkerProcessBudget | None = None
        self.observations: WorkerProcessObservations | None = None
        self.elapsed_seconds: float | None = None
        self.effective_wall_seconds: float | None = None
        # Interaction tasks can report failure, but only their owning supervisor
        # may read the execution lease and attach its lifecycle observations.
        self.prior_execution_started = False
        self.resource_pool = None

    def diagnostic(self) -> dict[str, object]:
        return {
            "resource_kind": self.resource_kind or "unknown",
            "budget_scope": self.budget_scope,
            "limit": self.limit,
            "observed": self.observed,
            "execution_started": self.execution_started or self.prior_execution_started,
            "current_stage_started": self.execution_started,
            "resource_pool": self.resource_pool,
            "phase": "execution" if self.execution_started else "startup",
            "configured_limits": asdict(self.budget) if self.budget is not None else None,
            "sampled_peaks": asdict(self.observations) if self.observations is not None else None,
            "elapsed_seconds": self.elapsed_seconds,
            "effective_wall_seconds": self.effective_wall_seconds,
        }


class _InteractionFailure(Exception):
    def __init__(self, error: Exception) -> None:
        self.error = error


@lru_cache(maxsize=1)
def _darwin_child_query():
    """Load the host PID query once; unavailable platforms retain psutil."""
    import ctypes

    try:
        function = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_listchildpids
    except (OSError, AttributeError):
        return None
    function.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
    function.restype = ctypes.c_int
    return function


def _worker_children(worker: psutil.Process) -> list[psutil.Process]:
    """Avoid Darwin's full-system PPID scan only for a verified empty tree."""
    if sys.platform != "darwin":
        return worker.children(recursive=True)
    query = _darwin_child_query()
    if query is None:
        return worker.children(recursive=True)
    # libproc also returns zero for a gone/invalid PID. Preserve psutil's
    # original PID/create-time association before accepting an empty result.
    if not worker.is_running():
        raise psutil.NoSuchProcess(worker.pid)
    import ctypes

    children = (ctypes.c_int * 64)()
    try:
        ctypes.set_errno(0)
        count = query(worker.pid, children, ctypes.sizeof(children))
        error = ctypes.get_errno()
    except (OSError, ValueError, TypeError):
        return worker.children(recursive=True)
    if count == 0 and error == 0 and not any(children):
        if not worker.is_running():
            raise psutil.NoSuchProcess(worker.pid)
        return []
    # Positive, truncated or erroneous queries keep the existing recursive
    # inspection, subtree resource accounting and forbidden-child rejection.
    return worker.children(recursive=True)


class WorkerInvocationControl:
    """Supervisor-owned per-invocation limits inside one process lifecycle."""

    def __init__(self, worker: psutil.Process, budget: WorkerProcessBudget, *, initializing: bool = False):
        self._worker, self._budget = worker, budget
        self._active = None
        self._cpu_seconds = 0.0
        self._initializing = initializing
        self._thread_baseline: int | None = None

    @property
    def thread_baseline(self) -> int | None:
        return self._thread_baseline

    def accept_thread_baseline(self) -> int:
        """Accept trusted dependency readiness once, before releasing user code.

        Only the Host initialization callback receives this control. The value
        comes from process accounting, never from a child-provided thread count.
        """
        if not self._initializing or self._thread_baseline is not None:
            raise RuntimeError("worker thread baseline can only be accepted once during initialization")
        self._thread_baseline = max(1, self._worker.num_threads())
        self._initializing = False
        return self._thread_baseline

    @property
    def remaining_cpu_seconds(self) -> float:
        """Fresh process CPU remaining in the supervisor's aggregate budget."""
        try:
            cpu = self._worker.cpu_times()
        except psutil.Error as exc:
            raise WorkerSupervisorError("WORKER_SERVICE_UNAVAILABLE",
                "worker CPU accounting is unavailable", execution_started=True) from exc
        return max(0.0, self._budget.cpu_seconds - cpu.user - cpu.system)

    @asynccontextmanager
    async def invocation(self, *, wall_seconds: float, cpu_seconds: float):
        if self._active is not None or any(not math.isfinite(v) or v <= 0 for v in (wall_seconds, cpu_seconds)):
            raise ValueError("worker invocation limits require one finite active interval")
        cpu = self._worker.cpu_times()
        self._active = (time.monotonic(), cpu.user + cpu.system,
                        min(wall_seconds, self._budget.wall_seconds), min(cpu_seconds, self._budget.cpu_seconds))
        try:
            yield
            self.check()
        finally:
            self._active = None

    def check(self):
        if self._active is not None:
            elapsed = time.monotonic() - self._active[0]
            if elapsed > self._active[2]:
                raise WorkerSupervisorError("WORKER_TIMEOUT", "worker invocation exceeded its wall-clock budget",
                    execution_started=True, resource_kind="wall", budget_scope="invocation",
                    limit=self._active[2], observed=elapsed)
        try:
            cpu = self._worker.cpu_times()
        except psutil.NoSuchProcess:
            return
        self._cpu_seconds = cpu.user + cpu.system
        if cpu.user + cpu.system > self._budget.cpu_seconds:
            raise WorkerSupervisorError("WORKER_RESOURCE_LIMIT", "worker exceeded its CPU budget",
                execution_started=True, resource_kind="cpu", budget_scope="process",
                limit=self._budget.cpu_seconds, observed=self._cpu_seconds)
        if self._active is not None and cpu.user + cpu.system - self._active[1] > self._active[3]:
            raise WorkerSupervisorError("WORKER_RESOURCE_LIMIT", "worker invocation exceeded its CPU budget",
                execution_started=True, resource_kind="cpu", budget_scope="invocation",
                limit=self._active[3], observed=self._cpu_seconds - self._active[1])


class WorkerProcessSupervisor:
    """Launch, account, bound, and terminate one isolated subprocess."""

    @staticmethod
    async def launch(
        spec: WorkerLaunchSpec,
        *,
        deadline: float,
        cleanup_seconds: float,
        stdin: int = asyncio.subprocess.DEVNULL,
        on_guard_registered: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> asyncio.subprocess.Process:
        """Create the existing isolated command with OS parent-liveness ownership."""

        if time.monotonic() >= deadline:
            raise WorkerSupervisorError(
                "WORKER_TIMEOUT", "worker execution deadline expired before launch",
                execution_started=False, resource_kind="wall",
            )

        parent_read, parent_write = os.pipe()
        pid_read, pid_write = os.pipe()
        start_read, start_write = os.pipe()
        process = None
        start_released = False
        spawned = asyncio.create_task(asyncio.create_subprocess_exec(
            sys.executable, "-I", "-B", str(Path(__file__).with_name("parent_guard.py")),
            "--parent-fd", str(parent_read), "--pid-fd", str(pid_write),
            "--start-fd", str(start_read),
            "--pass-fds", ",".join(map(str, spec.pass_fds)),
            "--capacity-fds", ",".join(map(str, spec.capacity_fds)),
            "--cleanup-seconds", str(cleanup_seconds), "--", *spec.command,
            stdin=stdin, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=spec.cwd, env=dict(spec.env), close_fds=True,
            pass_fds=(parent_read, pid_write, start_read, *spec.pass_fds, *spec.capacity_fds),
            start_new_session=True,
        ))
        try:
            process = await asyncio.shield(spawned)
            os.close(pid_write)
            pid_write = -1
            os.close(start_read)
            start_read = -1
            loop = asyncio.get_running_loop()
            line_buffer = bytearray()

            async def read_line() -> bytes:
                ready: asyncio.Future[bytes] = loop.create_future()

                def consume() -> None:
                    if ready.done():
                        return
                    try:
                        chunk = os.read(pid_read, 32)
                        if not chunk:
                            raise ValueError("worker guardian closed its identity pipe")
                        line_buffer.extend(chunk)
                        if b"\n" in line_buffer:
                            end = line_buffer.index(b"\n")
                            value = bytes(line_buffer[:end])
                            del line_buffer[:end + 1]
                            ready.set_result(value)
                    except Exception as exc:
                        ready.set_exception(exc)

                if b"\n" in line_buffer:
                    end = line_buffer.index(b"\n")
                    value = bytes(line_buffer[:end])
                    del line_buffer[:end + 1]
                    return value
                loop.add_reader(pid_read, consume)
                try:
                    async with asyncio.timeout_at(deadline):
                        return await ready
                finally:
                    loop.remove_reader(pid_read)

            if await read_line() != b"READY":
                raise ValueError("worker guardian did not hold its start gate")
            if on_guard_registered is not None:
                guard_birth_us = round(psutil.Process(process.pid).create_time() * 1_000_000)
                async with asyncio.timeout_at(deadline):
                    await on_guard_registered(process.pid, guard_birth_us)
            # Registration must finish within the original invocation deadline.
            if time.monotonic() >= deadline:
                raise WorkerSupervisorError(
                    "WORKER_TIMEOUT", "worker deadline expired before its start gate opened",
                    execution_started=False, resource_kind="wall",
                )
            os.write(start_write, b"1")
            start_released = True
            os.close(start_write)
            start_write = -1
            worker_identity = await read_line()
            if not worker_identity.isdigit():
                raise ValueError("worker launcher did not identify its child")
            worker_pid = int(worker_identity)
            setattr(process, "_cad2bim_worker_pid", worker_pid)

            async def release_liveness(descriptor: int) -> None:
                try:
                    await process.wait()
                finally:
                    # A guardian crash must not leave its computation or any
                    # forbidden descendants alive in the owned process group.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    os.close(descriptor)

            cleanup = asyncio.create_task(release_liveness(parent_write))
            setattr(process, "_cad2bim_liveness_cleanup", cleanup)
            parent_write = -1
            return process
        except BaseException as exc:
            if process is None:
                # Spawning is already owned: repeated cancellation cannot make
                # us close the parent liveness pipe before collecting its child.
                while not spawned.done():
                    try:
                        await asyncio.shield(spawned)
                    except asyncio.CancelledError:
                        continue
                process = spawned.result()
            if process is not None:
                await WorkerProcessSupervisor.stop_process(process, cleanup_seconds=cleanup_seconds)
            if isinstance(exc, TimeoutError):
                raise WorkerSupervisorError(
                    "WORKER_TIMEOUT", "worker launch exceeded its original deadline",
                    execution_started=start_released, resource_kind="wall",
                ) from exc
            raise
        finally:
            for descriptor in (
                parent_read, parent_write, pid_read, pid_write, start_read, start_write,
            ):
                if descriptor >= 0:
                    os.close(descriptor)

    @staticmethod
    def execution_pid(process: asyncio.subprocess.Process) -> int:
        return getattr(process, "_cad2bim_worker_pid", process.pid)

    async def run(
        self,
        spec: WorkerLaunchSpec,
        *,
        budget: WorkerProcessBudget,
        deadline: float,
        interaction: Callable[[WorkerInvocationControl], Awaitable[None]] | None = None,
        initialize: Callable[[WorkerInvocationControl], Awaitable[None]] | None = None,
        on_guard_registered: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> WorkerTerminal:
        lease = current_execution_lease()
        stage = lease.stage() if lease is not None else nullcontext()
        if lease is not None:
            assert lease.execution_deadline is not None
            deadline = min(deadline, lease.execution_deadline)
        details: dict[str, object] = {}
        with stage, observe_execution_phase("worker_runtime", "process", details=details):
            timing_path = None
            timing_fd = None
            try:
                if spec.env.get("CAD2BIM_WORKER_TIMING") == "1":
                    if spec.scratch is None:
                        raise ValueError("worker timing requires owned scratch")
                    timing_path = spec.scratch / "worker-timing.frames"
                    timing_fd = os.open(timing_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    spec = replace(spec, pass_fds=(*spec.pass_fds, timing_fd),
                                   env={**spec.env, "CAD2BIM_WORKER_TIMING_FD": str(timing_fd)})
                details["launch_started_ns"] = time.monotonic_ns()
                sample_costs = {}
                details["supervision_costs"] = sample_costs
                terminal = await self._run(spec, budget=budget, deadline=deadline, interaction=interaction,
                    initialize=initialize, _sample_costs=sample_costs, on_guard_registered=on_guard_registered)
            except WorkerSupervisorError as exc:
                details["failure_diagnostic"] = exc.diagnostic()
                raise
            except asyncio.CancelledError as exc:
                failure = getattr(exc, "worker_failure", None)
                if isinstance(failure, WorkerSupervisorError):
                    details["failure_diagnostic"] = failure.diagnostic()
                raise
            finally:
                details["process_finished_ns"] = time.monotonic_ns()
                if timing_fd is not None:
                    assert timing_path is not None
                    os.close(timing_fd)
                    # Telemetry is untrusted and must never decide business success.
                    try:
                        with timing_path.open("rb") as stream:
                            payload = stream.read(MAX_TIMING_BYTES + 1)
                        if len(payload) > MAX_TIMING_BYTES:
                            raise WorkerProtocolError("timing byte budget exceeded")
                        stream = io.BytesIO(payload)
                        events = []
                        while stream.tell() < len(payload):
                            event = read_json_frame(stream, limit=1020)
                            if (set(event) != {"phase", "start_ns", "end_ns", "pid", "details"}
                                    or type(event["start_ns"]) is not int
                                    or type(event["end_ns"]) is not int
                                    or event["end_ns"] < event["start_ns"]):
                                raise WorkerProtocolError("invalid timing event")
                            events.append(event)
                        details["child_timing"] = events
                    except (OSError, ValueError, WorkerProtocolError) as exc:
                        details["child_timing_unavailable"] = type(exc).__name__
            details["returncode"] = terminal.returncode
            details["sampled_peaks"] = asdict(terminal.observations)
            return terminal

    async def _run(
        self,
        spec: WorkerLaunchSpec,
        *,
        budget: WorkerProcessBudget,
        deadline: float,
        interaction: Callable[[WorkerInvocationControl], Awaitable[None]] | None = None,
        initialize: Callable[[WorkerInvocationControl], Awaitable[None]] | None = None,
        on_guard_registered: Callable[[int, int], Awaitable[None]] | None = None,
        _sample_costs: dict[str, int] | None = None,
    ) -> WorkerTerminal:
        if not spec.command:
            raise ValueError("worker launch command cannot be empty")
        scratch = (spec.scratch or spec.cwd).resolve(strict=True)
        process: asyncio.subprocess.Process | None = None
        capture_tasks: tuple[asyncio.Task[bytes], asyncio.Task[bytes]] = ()
        interaction_task: asyncio.Task[None] | None = None
        started_at: float | None = None
        observations = WorkerProcessObservations()
        sample_task = None
        sampling_stopped = threading.Event()
        try:
            process = await self.launch(
                spec, deadline=deadline, cleanup_seconds=budget.cleanup_seconds,
                on_guard_registered=on_guard_registered,
            )
            started_at = time.monotonic()
            lease = current_execution_lease()
            if lease is not None:
                lease.execution_started = True
            if process.stdout is None or process.stderr is None:
                raise WorkerSupervisorError(
                    "WORKER_SERVICE_UNAVAILABLE",
                    "worker capture pipes are unavailable",
                    execution_started=False,
                )
            capture_tasks = (
                asyncio.create_task(self.read_bounded(process.stdout, budget.stdout_bytes)),
                asyncio.create_task(self.read_bounded(process.stderr, budget.stderr_bytes)),
            )
            try:
                worker = psutil.Process(self.execution_pid(process))
            except psutil.Error as exc:
                raise WorkerSupervisorError(
                    "WORKER_SERVICE_UNAVAILABLE",
                    "worker process accounting is unavailable",
                    execution_started=False,
                ) from exc
            baseline_threads = 0 if initialize is not None else await self.settle_thread_baseline(worker)
            invocation_control = WorkerInvocationControl(worker, budget, initializing=initialize is not None)
            # oneshot caches belong to the Process instance. Keep them private
            # to the sampler so invocation CPU reads remain fresh on the loop.
            sampling_worker = psutil.Process(worker.pid)
            next_scratch_scan = 0.0

            def sample(*, final: bool = False):
                nonlocal next_scratch_scan
                sample_started = time.monotonic_ns()
                scan_scratch = final or time.monotonic() >= next_scratch_scan
                result = self.resource_violation(
                    sampling_worker, scratch, budget,
                    baseline_threads=(invocation_control.thread_baseline or 0) if initialize is not None else baseline_threads,
                    previous=observations,
                    _sample_costs=_sample_costs, _scan_scratch=scan_scratch,
                    _stop_event=sampling_stopped,
                )
                if scan_scratch:
                    next_scratch_scan = time.monotonic() + 0.5
                if _sample_costs is not None:
                    elapsed = time.monotonic_ns() - sample_started
                    _sample_costs["sample_count"] = _sample_costs.get("sample_count", 0) + 1
                    _sample_costs["sample_wall_ns"] = _sample_costs.get("sample_wall_ns", 0) + elapsed
                    _sample_costs["sample_max_ns"] = max(_sample_costs.get("sample_max_ns", 0), elapsed)
                return result

            def accept_sample(result):
                nonlocal observations
                violation, sampled = result
                observations = replace(sampled, peak_cpu_seconds=max(
                    sampled.peak_cpu_seconds, observations.peak_cpu_seconds))
                if violation is not None:
                    raise WorkerSupervisorError(
                        "WORKER_RESOURCE_LIMIT", violation, execution_started=True,
                        resource_kind=violation.name.lower(),
                    )

            async def finish_sample():
                try:
                    async with asyncio.timeout_at(min(deadline, started_at + budget.wall_seconds)):
                        accept_sample(await asyncio.shield(sample_task))
                except TimeoutError as exc:
                    raise WorkerSupervisorError(
                        "WORKER_TIMEOUT", "worker exceeded its wall-clock deadline",
                        execution_started=True, resource_kind="wall",
                    ) from exc
            if interaction is not None or initialize is not None:
                async def interact():
                    try:
                        if initialize is not None:
                            initialization_details: dict[str, object] = {}
                            with observe_execution_phase("worker_runtime", "initialize", details=initialization_details):
                                await initialize(invocation_control)
                                if invocation_control.thread_baseline is None:
                                    raise RuntimeError("worker initialization did not accept a thread baseline")
                                initialization_details["thread_baseline"] = invocation_control.thread_baseline
                        if interaction is not None:
                            await interaction(invocation_control)
                    except WorkerSupervisorError:
                        raise
                    except Exception as exc:
                        # Domain/closed-protocol ValueError is not an OS or
                        # launcher failure. Preserve its original attribution.
                        raise _InteractionFailure(exc) from exc
                interaction_task = asyncio.create_task(interact())
            while process.returncode is None:
                try:
                    invocation_control.check()
                finally:
                    observations = replace(observations, peak_cpu_seconds=max(
                        observations.peak_cpu_seconds, invocation_control._cpu_seconds))
                if interaction_task is not None and interaction_task.done():
                    # A protocol/instance failure must retire this same owned
                    # process immediately, even if its native call is blocked.
                    interaction_task.result()
                failed_capture = next(
                    (
                        task
                        for task in capture_tasks
                        if task.done() and not task.cancelled() and task.exception() is not None
                    ),
                    None,
                )
                if failed_capture is not None:
                    raise WorkerSupervisorError(
                        "WORKER_RESOURCE_LIMIT",
                        "worker diagnostics exceeded their capture budget",
                        execution_started=True,
                        resource_kind="capture",
                    ) from failed_capture.exception()
                now = time.monotonic()
                if now > deadline or now - started_at > budget.wall_seconds:
                    raise WorkerSupervisorError(
                        "WORKER_TIMEOUT",
                        "worker exceeded its wall-clock deadline",
                        execution_started=True,
                        resource_kind="wall",
                    )
                if sample_task is not None and sample_task.done():
                    accept_sample(sample_task.result())
                    sample_task = None
                if sample_task is None:
                    sample_task = asyncio.create_task(asyncio.to_thread(sample))
                expected_wake = time.monotonic_ns() + 20_000_000
                await asyncio.sleep(0.02)
                if _sample_costs is not None:
                    delay = max(0, time.monotonic_ns() - expected_wake)
                    _sample_costs["loop_wake_delay_max_ns"] = max(_sample_costs.get("loop_wake_delay_max_ns", 0), delay)
            await process.wait()
            if sample_task is not None:
                await finish_sample()
                sample_task = None
            # The worker may finish between samples, including before the first
            # process read. Always account for its final on-disk output.
            sample_task = asyncio.create_task(asyncio.to_thread(sample, final=True))
            await finish_sample()
            sample_task = None
            if interaction_task is not None:
                try:
                    async with asyncio.timeout_at(deadline):
                        await interaction_task
                except TimeoutError as exc:
                    raise WorkerSupervisorError("WORKER_TIMEOUT",
                        "worker interaction exceeded its wall-clock deadline after process exit",
                        execution_started=True, resource_kind="wall") from exc
            captures = await asyncio.gather(*capture_tasks, return_exceptions=True)
            if any(isinstance(item, BaseException) for item in captures):
                raise WorkerSupervisorError(
                    "WORKER_RESOURCE_LIMIT",
                    "worker diagnostics exceeded their capture budget",
                    execution_started=True,
                    resource_kind="capture",
                )
            return WorkerTerminal(
                returncode=process.returncode,
                status_payload=None,
                stdout=captures[0],
                stderr=captures[1],
                execution_started=True,
                observations=observations,
            )
        except asyncio.CancelledError as exc:
            failed_at = time.monotonic()
            failure = WorkerSupervisorError(
                "WORKER_TIMEOUT",
                "worker request was cancelled",
                execution_started=started_at is not None,
                resource_kind="cancelled",
            )
            self._attach_failure_observations(
                failure, budget, observations, started_at, failed_at, deadline,
            )
            if not isinstance(getattr(exc, "worker_failure", None), WorkerSupervisorError):
                setattr(exc, "worker_failure", failure)
            if process is not None:
                try:
                    await self.stop_process(process, cleanup_seconds=budget.cleanup_seconds)
                except asyncio.CancelledError:
                    # stop_process has drained the owned child; keep the original
                    # cancellation's lifecycle facts after repeated cancellation.
                    pass
            raise
        except WorkerSupervisorError as exc:
            self._attach_failure_observations(
                exc, budget, observations, started_at, time.monotonic(), deadline,
            )
            if process is not None:
                await self.stop_process(process, cleanup_seconds=budget.cleanup_seconds)
            raise
        except _InteractionFailure as exc:
            if process is not None:
                await self.stop_process(process, cleanup_seconds=budget.cleanup_seconds)
            raise exc.error from exc
        except (OSError, ValueError) as exc:
            if process is not None:
                await self.stop_process(process, cleanup_seconds=budget.cleanup_seconds)
            failure = WorkerSupervisorError(
                "WORKER_SERVICE_UNAVAILABLE",
                "worker infrastructure is temporarily unavailable",
                execution_started=started_at is not None,
            )
            self._attach_failure_observations(
                failure, budget, observations, started_at, time.monotonic(), deadline,
            )
            raise failure from exc
        except BaseException:
            if process is not None:
                await self.stop_process(process, cleanup_seconds=budget.cleanup_seconds)
            raise
        finally:
            sampling_stopped.set()
            if sample_task is not None:
                # Never detach a thread touching owned scratch or observations.
                # Stop requests are checked between process and directory reads.
                while not sample_task.done():
                    try:
                        await asyncio.shield(sample_task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                await asyncio.gather(sample_task, return_exceptions=True)
            if interaction_task is not None:
                if not interaction_task.done():
                    interaction_task.cancel()
                await asyncio.gather(interaction_task, return_exceptions=True)
            for task in capture_tasks:
                if not task.done():
                    task.cancel()
            if capture_tasks:
                await asyncio.gather(*capture_tasks, return_exceptions=True)
            if process is not None:
                cleanup = getattr(process, "_cad2bim_liveness_cleanup", None)
                if cleanup is not None:
                    await asyncio.shield(cleanup)

    @staticmethod
    def _attach_failure_observations(
        failure: WorkerSupervisorError,
        budget: WorkerProcessBudget,
        observations: WorkerProcessObservations,
        started_at: float | None,
        failed_at: float,
        deadline: float,
    ) -> None:
        lease = current_execution_lease()
        failure.prior_execution_started = bool(lease is not None and (
            lease.execution_started or (lease.invocation is not None and lease.invocation.execution_started)
        ))
        failure.resource_pool = lease.capacity.resource_pool if lease is not None else None
        failure.budget = budget
        # A process accounting failure may leave the zero initializer untouched.
        # Do not present those zeroes as actual measurements.
        failure.observations = observations if observations != WorkerProcessObservations() else None
        if started_at is not None:
            failure.elapsed_seconds = max(0.0, failed_at - started_at)
            failure.effective_wall_seconds = max(0.0, min(budget.wall_seconds, deadline-started_at))

    @staticmethod
    async def read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
        if limit < 0:
            raise ValueError("worker capture limit cannot be negative")
        chunks: list[bytes] = []
        size = 0
        while chunk := await stream.read(min(4096, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise RuntimeError("worker diagnostics exceeded their byte limit")
        return b"".join(chunks)

    @staticmethod
    async def stop_process(
        process: asyncio.subprocess.Process,
        *,
        cleanup_seconds: float,
    ) -> None:
        async def cleanup() -> None:
            await WorkerProcessSupervisor._stop_process(process, cleanup_seconds=cleanup_seconds)
            liveness = getattr(process, "_cad2bim_liveness_cleanup", None)
            if liveness is not None:
                await liveness

        owned_cleanup = asyncio.create_task(cleanup())
        cancelled = False
        while not owned_cleanup.done():
            try:
                await asyncio.shield(owned_cleanup)
            except asyncio.CancelledError:
                cancelled = True
        await owned_cleanup
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    async def _stop_process(
        process: asyncio.subprocess.Process, *, cleanup_seconds: float,
    ) -> None:
        if process.returncode is not None:
            await process.wait()
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            await process.wait()
            return
        except PermissionError:
            try:
                process.terminate()
            except ProcessLookupError:
                await process.wait()
                return
        try:
            await asyncio.wait_for(process.wait(), timeout=cleanup_seconds)
            return
        except TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            await process.wait()
            return
        except PermissionError:
            try:
                process.kill()
            except ProcessLookupError:
                await process.wait()
                return
        await process.wait()

    @staticmethod
    async def settle_thread_baseline(worker: psutil.Process) -> int:
        """Measure launcher/library threads before applying user growth budget."""

        last = 1
        stable_samples = 0
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                current = max(1, worker.num_threads())
            except (psutil.Error, OSError):
                return last
            if current == last:
                stable_samples += 1
                if stable_samples >= 3:
                    return current
            else:
                last = current
                stable_samples = 0
            await asyncio.sleep(0.02)
        return last

    @staticmethod
    def resource_violation(
        worker: psutil.Process,
        scratch: Path,
        budget: WorkerProcessBudget,
        *,
        baseline_threads: int,
        previous: WorkerProcessObservations | None = None,
        _sample_costs: dict[str, int] | None = None,
        _scan_scratch: bool = True,
        _stop_event: threading.Event | None = None,
    ) -> tuple[WorkerResourceViolation | None, WorkerProcessObservations]:
        prior = previous or WorkerProcessObservations()
        thread_snapshot_available = True
        try:
            try:
                children = _worker_children(worker)
            except (psutil.Error, OSError):
                children = []
            processes = [worker, *children]
            rss = cpu = threads = descriptors = 0
            for process in processes:
                if _stop_event is not None and _stop_event.is_set():
                    return None, prior
                with process.oneshot():
                    rss += process.memory_info().rss
                    snapshot = process.cpu_times()
                    cpu += snapshot.user + snapshot.system
                    threads += process.num_threads()
                    descriptors += process.num_fds()
        except (psutil.Error, OSError, AttributeError):
            thread_snapshot_available = False
            children = []
            rss, cpu = prior.peak_rss_bytes, prior.peak_cpu_seconds
            threads, descriptors = prior.peak_threads, prior.peak_open_file_descriptors
        scan_started = time.monotonic_ns()
        scratch_bytes = directory_size(scratch, _stop_event=_stop_event) if _scan_scratch else prior.peak_scratch_bytes
        if _sample_costs is not None and _scan_scratch:
            elapsed = time.monotonic_ns() - scan_started
            _sample_costs["scratch_scan_wall_ns"] = _sample_costs.get("scratch_scan_wall_ns", 0) + elapsed
            _sample_costs["scratch_scan_max_ns"] = max(_sample_costs.get("scratch_scan_max_ns", 0), elapsed)
        observed = WorkerProcessObservations(
            peak_rss_bytes=max(prior.peak_rss_bytes, rss),
            peak_cpu_seconds=max(prior.peak_cpu_seconds, cpu),
            peak_threads=max(prior.peak_threads, threads),
            peak_open_file_descriptors=max(prior.peak_open_file_descriptors, descriptors),
            peak_scratch_bytes=max(prior.peak_scratch_bytes, scratch_bytes),
        )
        if children:
            return WorkerResourceViolation.SUBPROCESS, observed
        if rss > budget.rss_bytes:
            return WorkerResourceViolation.RSS, observed
        if cpu > budget.cpu_seconds:
            return WorkerResourceViolation.CPU, observed
        # Initialization peaks remain useful observations, but after process
        # exit they are not a live snapshot of growth above the ready baseline.
        if thread_snapshot_available and baseline_threads and threads > baseline_threads + budget.threads:
            return WorkerResourceViolation.THREADS, observed
        if descriptors > budget.open_file_descriptors:
            return WorkerResourceViolation.OPEN_FILE_DESCRIPTORS, observed
        if scratch_bytes > budget.scratch_bytes:
            return WorkerResourceViolation.SCRATCH, observed
        return None, observed


def directory_size(root: Path, *, _stop_event: threading.Event | None = None) -> int:
    total = 0
    directories = [root]
    while directories:
        if _stop_event is not None and _stop_event.is_set():
            return total
        try:
            with os.scandir(directories.pop()) as entries:
                for entry in entries:
                    if _stop_event is not None and _stop_event.is_set():
                        return total
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            directories.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            continue
    return total


__all__ = [
    "WorkerLaunchSpec",
    "WorkerProcessBudget",
    "WorkerProcessObservations",
    "WorkerProcessSupervisor",
    "WorkerResourceViolation",
    "WorkerSupervisorError",
    "WorkerTerminal",
    "directory_size",
]
