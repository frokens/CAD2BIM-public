"""Killable subprocess supervisor for the fixed, server-owned M3 kernel."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, Mapping

import psutil
import pyarrow as pa

from CAD2BIM_backend.domain.cad_measurement import (
    CAD_MEASUREMENT_COMPUTE_BUDGET,
    CAD_MEASUREMENT_PAYLOAD_NAMES,
    CAD_TOLERANCE_ANALYZER_BUDGET,
    CadAnalyzeToleranceCandidatesCommand,
    CadComputeMeasurementsCommand,
    CadMeasurementBudget,
)
from CAD2BIM_backend.infrastructure.cad_measurement.schemas import MEASUREMENT_PAYLOAD_SCHEMAS
from CAD2BIM_backend.infrastructure.worker_runtime.capacity import (
    HostWorkerCapacity, WorkerCapacityBusy, WorkerCapacityUnavailable,
    default_capacity_root,
)
from CAD2BIM_backend.infrastructure.worker_runtime.supervisor import (
    WorkerLaunchSpec, WorkerProcessSupervisor,
)


_INPUT_NAMES = frozenset({"entities", "geometry_elements", "relationships"})
_EVENT_BYTES = 32 * 1024
_STDERR_BYTES = 16 * 1024


class CadMeasurementWorkerError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: Literal["preflight", "execution", "output_validation"],
        execution_started: bool,
        retryable: bool = False,
        started_at: datetime | None = None,
    ) -> None:
        super().__init__(_bounded(message, 2_000))
        self.code = code
        self.phase = phase
        self.execution_started = execution_started
        self.retryable = retryable
        self.started_at = started_at


@dataclass(frozen=True, slots=True)
class CadMeasurementWorkerObservation:
    mode: Literal["analyze", "compute"]
    started_at: datetime
    worker_wall_seconds: float
    worker_cpu_seconds: float
    peak_rss_bytes: int
    peak_scratch_bytes: int
    input_rows: int
    input_arrow_bytes: int
    staged_input_bytes: int
    output_rows: int | None
    output_arrow_bytes: int | None
    output_parquet_bytes: int | None
    analysis_response_bytes: int | None
    outcome: Literal["succeeded"]
    code: None


@dataclass(frozen=True, slots=True)
class _WorkerExecution:
    started_at: datetime
    root: Path
    worker_wall_seconds: float
    worker_cpu_seconds: float
    peak_rss_bytes: int
    peak_scratch_bytes: int
    input_rows: int
    input_arrow_bytes: int
    staged_input_bytes: int


@dataclass(frozen=True, slots=True)
class CadMeasurementWorkerAnalysisResult:
    started_at: datetime
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CadMeasurementWorkerComputeResult:
    started_at: datetime
    tables: Mapping[str, pa.Table]
    parquet_paths: Mapping[str, Path]
    parquet_bytes: Mapping[str, int]
    _workspace: Path

    async def cleanup(self) -> None:
        await asyncio.to_thread(shutil.rmtree, self._workspace, True)


class SubprocessCadMeasurementWorker:
    """Supervise analyzer and compute through one process- and host-wide slot."""

    def __init__(
        self,
        *,
        python_executable: str | None = None,
        capacity_root: str | Path | None = None,
        child_script: str | Path | None = None,
        observation_sink: Callable[[CadMeasurementWorkerObservation], None] | None = None,
        capacity: HostWorkerCapacity | None = None,
    ) -> None:
        executable = Path(python_executable or sys.executable)
        if (
            not executable.is_absolute()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise ValueError("M3 Python executable must be absolute and executable")
        self._python = str(executable)
        script = Path(child_script or Path(__file__).with_name("supervised_child.py"))
        if not script.is_absolute() or not script.is_file():
            raise ValueError("M3 child script must be an absolute file")
        self._child_script = str(script)
        self._use_module_entry = child_script is None
        self._capacity = capacity or HostWorkerCapacity(
            max_concurrent_workers=1, capacity_root=capacity_root or default_capacity_root(pool="cad"),
            resource_pool="cad",
        )
        self._observation_sink = observation_sink
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    async def close(self) -> None:
        if self._cleanup_tasks:
            await asyncio.gather(*tuple(self._cleanup_tasks), return_exceptions=True)

    async def analyze(
        self,
        tables: Mapping[str, pa.Table],
        command: CadAnalyzeToleranceCandidatesCommand,
        *,
        deadline: float,
    ) -> CadMeasurementWorkerAnalysisResult:
        execution = await self._invoke(
            "analyze",
            tables,
            command.model_dump(mode="json"),
            CAD_TOLERANCE_ANALYZER_BUDGET,
            deadline,
        )
        root = execution.root
        try:
            path = root / "analysis.json"
            if path.stat().st_size > CAD_TOLERANCE_ANALYZER_BUDGET.response_bytes:
                raise ValueError("analysis exceeds response budget")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("analysis is not an object")
            self._observe(
                execution,
                mode="analyze",
                analysis_response_bytes=path.stat().st_size,
            )
            return CadMeasurementWorkerAnalysisResult(execution.started_at, payload)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise _error(
                "OUTPUT_INVALID",
                "M3 analyzer returned invalid output",
                "output_validation",
                True,
                started_at=execution.started_at,
            ) from exc
        finally:
            await asyncio.to_thread(shutil.rmtree, root, True)

    async def compute(
        self,
        tables: Mapping[str, pa.Table],
        command: CadComputeMeasurementsCommand,
        *,
        deadline: float,
    ) -> CadMeasurementWorkerComputeResult:
        execution = await self._invoke(
            "compute",
            tables,
            command.model_dump(mode="json"),
            CAD_MEASUREMENT_COMPUTE_BUDGET,
            deadline,
        )
        root = execution.root
        try:
            results: dict[str, pa.Table] = {}
            parquet_paths: dict[str, Path] = {}
            parquet_bytes: dict[str, int] = {}
            for name in CAD_MEASUREMENT_PAYLOAD_NAMES:
                with pa.memory_map(str(root / "outputs" / f"{name}.arrow"), "r") as source:
                    table = pa.ipc.open_file(source).read_all()
                if table.schema != MEASUREMENT_PAYLOAD_SCHEMAS[name]:
                    raise ValueError(f"{name} Arrow schema mismatch")
                results[name] = table
                parquet_path = root / "outputs" / f"{name}.parquet"
                parquet_size = parquet_path.stat().st_size
                if parquet_size > (
                    CAD_MEASUREMENT_COMPUTE_BUDGET.output_payload_parquet_bytes or 0
                ):
                    raise ValueError(f"{name} Parquet budget exceeded")
                parquet_paths[name] = parquet_path
                parquet_bytes[name] = parquet_size
            rows = sum(table.num_rows for table in results.values())
            size = sum(table.nbytes for table in results.values())
            if rows > (CAD_MEASUREMENT_COMPUTE_BUDGET.output_rows or 0) or size > (
                CAD_MEASUREMENT_COMPUTE_BUDGET.output_arrow_bytes or 0
            ):
                raise ValueError("aggregate output budget exceeded")
            if sum(parquet_bytes.values()) > (
                CAD_MEASUREMENT_COMPUTE_BUDGET.output_parquet_bytes or 0
            ):
                raise ValueError("aggregate Parquet budget exceeded")
            total_parquet_bytes = sum(parquet_bytes.values())
            self._observe(
                execution,
                mode="compute",
                output_rows=rows,
                output_arrow_bytes=size,
                output_parquet_bytes=total_parquet_bytes,
            )
            return CadMeasurementWorkerComputeResult(
                execution.started_at, results, parquet_paths, parquet_bytes, root
            )
        except (OSError, ValueError, pa.ArrowException) as exc:
            await asyncio.to_thread(shutil.rmtree, root, True)
            raise _error(
                "OUTPUT_INVALID",
                "M3 worker returned invalid exact Payloads",
                "output_validation",
                True,
                started_at=execution.started_at,
            ) from exc

    async def _invoke(
        self,
        mode: Literal["analyze", "compute"],
        tables: Mapping[str, pa.Table],
        command: Mapping[str, object],
        budget: CadMeasurementBudget,
        deadline: float,
    ) -> _WorkerExecution:
        self._validate_inputs(tables)
        root: Path | None = None
        process: asyncio.subprocess.Process | None = None
        terminal_task: asyncio.Task[dict[str, object]] | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        capacity_fd: int | None = None
        local_acquired = False
        started_at: datetime | None = None
        started_monotonic: float | None = None
        peak_rss_bytes = 0
        peak_scratch_bytes = 0
        worker_cpu_seconds = 0.0
        input_rows = sum(table.num_rows for table in tables.values())
        input_arrow_bytes = sum(table.nbytes for table in tables.values())
        staged_input_bytes = 0
        try:
            from CAD2BIM_backend.domain.runtime_execution import (
                CAD_ANALYSIS_EXECUTION, CAD_MEASUREMENT_EXECUTION,
            )
            policy = CAD_ANALYSIS_EXECUTION if mode == "analyze" else CAD_MEASUREMENT_EXECUTION
            capacity_deadline = min(
                time.monotonic() + budget.capacity_wait_seconds,
                deadline - policy.execution_seconds - policy.terminal_seconds - policy.cleanup_seconds,
            )
            capacity_fd = await self._acquire_host_capacity(capacity_deadline)
            try:
                await asyncio.wait_for(
                    self._capacity.acquire(), max(0.0, capacity_deadline - time.monotonic())
                )
            except TimeoutError:
                raise self._capacity_busy() from None
            local_acquired = True
            deadline = min(
                deadline - policy.terminal_seconds - policy.cleanup_seconds,
                time.monotonic() + policy.execution_seconds,
            )
            root = Path(tempfile.mkdtemp(prefix=f"cad2bim-m3-{mode}-"))
            os.chmod(root, 0o700)
            staging = asyncio.create_task(asyncio.to_thread(self._stage_inputs, root, tables))
            try:
                await asyncio.wait_for(
                    asyncio.shield(staging), max(0.0, deadline - time.monotonic())
                )
            except (TimeoutError, asyncio.CancelledError):
                cleanup = asyncio.create_task(self._finish_staging(staging, root, capacity_fd))
                self._cleanup_tasks.add(cleanup)
                cleanup.add_done_callback(self._cleanup_tasks.discard)
                root = None
                capacity_fd = None
                local_acquired = False
                raise
            staged_input_bytes = _tree_bytes(root / "inputs")
            request_path = root / "request.json"
            request = {
                "mode": mode,
                "root": str(root),
                "command": command,
                "budget": {
                    "worker_cpu_seconds": budget.worker_cpu_seconds,
                    "worker_rss_bytes": budget.worker_rss_bytes,
                    "worker_scratch_bytes": budget.worker_scratch_bytes,
                    "candidate_count": CAD_MEASUREMENT_COMPUTE_BUDGET.candidate_count,
                    "response_bytes": budget.response_bytes,
                    "output_rows": budget.output_rows,
                    "output_arrow_bytes": budget.output_arrow_bytes,
                    "output_parquet_bytes": budget.output_parquet_bytes,
                    "output_payload_parquet_bytes": budget.output_payload_parquet_bytes,
                },
            }
            content = json.dumps(
                request, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode()
            if len(content) > 128 * 1024:
                raise _error(
                    "INVALID_ARGUMENT", "canonical M3 request exceeds 128 KiB", "preflight", False
                )
            request_path.write_bytes(content)
            child_args = (
                (
                    "-I",
                    "-B",
                    "-m",
                    "CAD2BIM_backend.infrastructure.cad_measurement.supervised_child",
                )
                if self._use_module_entry
                else ("-I", "-B", self._child_script)
            )
            process = await WorkerProcessSupervisor.launch(
                WorkerLaunchSpec(command=(self._python, *child_args, str(request_path)),
                    cwd=root, env=_environment(root, budget.worker_threads),
                    capacity_fds=(capacity_fd,)),
                deadline=deadline, cleanup_seconds=1.0, stdin=asyncio.subprocess.PIPE,
            )
            ready = await self._read_event(process, deadline)
            if ready.get("event") != "ready":
                raise _error(
                    "WORKER_FAILED", "M3 child omitted ready handshake", "preflight", False
                )
            assert process.stdin is not None
            process.stdin.write(b'{"event":"start"}\n')
            await process.stdin.drain()
            process.stdin.close()
            started = await self._read_event(process, deadline)
            if started.get("event") != "started":
                raise _error(
                    "WORKER_FAILED", "M3 child omitted started handshake", "preflight", False
                )
            try:
                started_at = datetime.fromisoformat(str(started["started_at"]))
            except (KeyError, ValueError, TypeError) as exc:
                raise _error(
                    "WORKER_FAILED",
                    "M3 child returned invalid started timestamp",
                    "execution",
                    True,
                ) from exc
            started_monotonic = time.monotonic()
            rss, cpu = _process_sample(WorkerProcessSupervisor.execution_pid(process))
            peak_rss_bytes = max(peak_rss_bytes, rss)
            peak_scratch_bytes = max(peak_scratch_bytes, _tree_bytes(root))
            worker_cpu_seconds = max(worker_cpu_seconds, cpu)

            terminal_task = asyncio.create_task(self._read_event(process, deadline))
            stderr_task = asyncio.create_task(_read_stderr(process))
            wall_deadline = min(deadline, time.monotonic() + budget.worker_wall_seconds)
            limit: str | None = None
            while not terminal_task.done():
                remaining = wall_deadline - time.monotonic()
                if remaining <= 0:
                    limit = "TIMEOUT"
                    break
                await asyncio.wait({terminal_task}, timeout=min(0.05, remaining))
                if terminal_task.done():
                    break
                rss, cpu = _process_sample(WorkerProcessSupervisor.execution_pid(process))
                scratch = _tree_bytes(root)
                peak_rss_bytes = max(peak_rss_bytes, rss)
                peak_scratch_bytes = max(peak_scratch_bytes, scratch)
                worker_cpu_seconds = max(worker_cpu_seconds, cpu)
                if rss > budget.worker_rss_bytes or scratch > budget.worker_scratch_bytes:
                    limit = "RESOURCE_LIMIT"
                    break
            if limit:
                terminal_task.cancel()
                await _terminate(process)
                await asyncio.gather(terminal_task, stderr_task, return_exceptions=True)
                raise _error(
                    limit,
                    "M3 worker exceeded its wall, RSS, or scratch budget",
                    "execution",
                    True,
                    started_at=started_at,
                )
            try:
                terminal = await terminal_task
            except CadMeasurementWorkerError as exc:
                await process.wait()
                if process.returncode in {-signal.SIGXCPU, -signal.SIGKILL}:
                    raise _error(
                        "RESOURCE_LIMIT",
                        "M3 worker was terminated by an OS resource limit",
                        "execution",
                        True,
                        started_at=started_at,
                    ) from exc
                raise _error(exc.code, str(exc), "execution", True, started_at=started_at) from exc
            await process.wait()
            stderr = await stderr_task
            rss, cpu = _process_sample(WorkerProcessSupervisor.execution_pid(process))
            peak_rss_bytes = max(peak_rss_bytes, rss)
            peak_scratch_bytes = max(peak_scratch_bytes, _tree_bytes(root))
            worker_cpu_seconds = max(worker_cpu_seconds, cpu)
            if (
                terminal.get("event") != "terminal"
                or terminal.get("outcome") != "succeeded"
                or process.returncode != 0
            ):
                message = str(terminal.get("message") or "M3 worker failed")
                if stderr:
                    message += "; child stderr: " + stderr.decode("utf-8", "replace")
                raise _error(
                    str(terminal.get("code") or "WORKER_FAILED"),
                    message,
                    _phase(terminal.get("phase")),
                    True,
                    started_at=started_at,
                )
            assert started_monotonic is not None
            return _WorkerExecution(
                started_at=started_at,
                root=root,
                worker_wall_seconds=max(0.0, time.monotonic() - started_monotonic),
                worker_cpu_seconds=worker_cpu_seconds,
                peak_rss_bytes=peak_rss_bytes,
                peak_scratch_bytes=peak_scratch_bytes,
                input_rows=input_rows,
                input_arrow_bytes=input_arrow_bytes,
                staged_input_bytes=staged_input_bytes,
            )
        except asyncio.CancelledError as exc:
            if process is not None:
                await asyncio.shield(_terminate(process))
            for pending in (terminal_task, stderr_task):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(
                *(item for item in (terminal_task, stderr_task) if item is not None),
                return_exceptions=True,
            )
            setattr(
                exc,
                "worker_failure",
                _error(
                    "TIMEOUT",
                    "M3 worker was cancelled",
                    "execution" if started_at else "preflight",
                    started_at is not None,
                    started_at=started_at,
                ),
            )
            raise
        except CadMeasurementWorkerError:
            if process is not None:
                await _terminate(process)
            for pending in (terminal_task, stderr_task):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(
                *(item for item in (terminal_task, stderr_task) if item is not None),
                return_exceptions=True,
            )
            raise
        except TimeoutError as exc:
            if process is not None:
                await _terminate(process)
            raise _error(
                "TIMEOUT",
                "M3 total deadline expired before execution",
                "execution" if started_at else "preflight",
                started_at is not None,
                started_at=started_at,
            ) from exc
        except (OSError, pa.ArrowException) as exc:
            if process is not None:
                await _terminate(process)
            raise _error(
                "SERVICE_UNAVAILABLE",
                "M3 worker infrastructure is unavailable",
                "execution" if started_at else "preflight",
                started_at is not None,
                retryable=started_at is None,
                started_at=started_at,
            ) from exc
        finally:
            try:
                if process is not None:
                    await WorkerProcessSupervisor.stop_process(process, cleanup_seconds=2.0)
            finally:
                if capacity_fd is not None:
                    os.close(capacity_fd)
                if local_acquired:
                    self._capacity.release()
            if root is not None and (process is None or process.returncode != 0):
                await asyncio.to_thread(shutil.rmtree, root, True)

    async def _read_event(
        self, process: asyncio.subprocess.Process, deadline: float
    ) -> dict[str, object]:
        assert process.stdout is not None
        try:
            line = await asyncio.wait_for(
                process.stdout.readline(), max(0.0, deadline - time.monotonic())
            )
        except TimeoutError:
            raise _error(
                "TIMEOUT", "M3 worker protocol exceeded total deadline", "preflight", False
            ) from None
        if not line or len(line) > _EVENT_BYTES:
            raise _error(
                "WORKER_FAILED", "M3 child returned invalid protocol event", "preflight", False
            )
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _error(
                "WORKER_FAILED", "M3 child returned malformed protocol JSON", "preflight", False
            ) from exc
        if not isinstance(value, dict):
            raise _error(
                "WORKER_FAILED", "M3 child protocol event is not an object", "preflight", False
            )
        return value

    async def _finish_staging(
        self, staging: asyncio.Task[None], root: Path, capacity_fd: int,
    ) -> None:
        try:
            await staging
        except BaseException:
            pass
        finally:
            try:
                await asyncio.to_thread(shutil.rmtree, root, True)
            finally:
                os.close(capacity_fd)
                self._capacity.release()

    def _observe(
        self,
        execution: _WorkerExecution,
        *,
        mode: Literal["analyze", "compute"],
        output_rows: int | None = None,
        output_arrow_bytes: int | None = None,
        output_parquet_bytes: int | None = None,
        analysis_response_bytes: int | None = None,
    ) -> None:
        if self._observation_sink is None:
            return
        observation = CadMeasurementWorkerObservation(
            mode=mode,
            started_at=execution.started_at,
            worker_wall_seconds=execution.worker_wall_seconds,
            worker_cpu_seconds=execution.worker_cpu_seconds,
            peak_rss_bytes=execution.peak_rss_bytes,
            peak_scratch_bytes=execution.peak_scratch_bytes,
            input_rows=execution.input_rows,
            input_arrow_bytes=execution.input_arrow_bytes,
            staged_input_bytes=execution.staged_input_bytes,
            output_rows=output_rows,
            output_arrow_bytes=output_arrow_bytes,
            output_parquet_bytes=output_parquet_bytes,
            analysis_response_bytes=analysis_response_bytes,
            outcome="succeeded",
            code=None,
        )
        try:
            self._observation_sink(observation)
        except Exception:
            # Observability is non-authoritative and cannot change MCP behavior.
            pass

    async def _acquire_host_capacity(self, deadline: float) -> int:
        try:
            return await self._capacity.acquire_host(deadline)
        except (WorkerCapacityBusy, WorkerCapacityUnavailable) as exc:
            raise _error(
                "CAPACITY_BUSY" if isinstance(exc, WorkerCapacityBusy) else "SERVICE_UNAVAILABLE",
                self._capacity.failure_message(exc), "preflight", False,
                retryable=isinstance(exc, WorkerCapacityBusy) and exc.retryable,
            ) from exc

    @staticmethod
    def _validate_inputs(tables: Mapping[str, pa.Table]) -> None:
        if set(tables) != _INPUT_NAMES or any(
            not isinstance(value, pa.Table) for value in tables.values()
        ):
            raise _error(
                "INVALID_ARGUMENT",
                "M3 worker requires exact entities, geometry_elements and relationships tables",
                "preflight",
                False,
            )

    @staticmethod
    def _stage_inputs(root: Path, tables: Mapping[str, pa.Table]) -> None:
        directory = root / "inputs"
        directory.mkdir(mode=0o700)
        for name in sorted(_INPUT_NAMES):
            with pa.OSFile(str(directory / f"{name}.arrow"), "wb") as sink:
                with pa.ipc.new_file(sink, tables[name].schema) as writer:
                    writer.write_table(tables[name])

    @staticmethod
    def _capacity_busy() -> CadMeasurementWorkerError:
        return _error(
            "CAPACITY_BUSY",
            "The single host-wide M3 worker slot is busy",
            "preflight",
            False,
            retryable=True,
        )


async def _wait_total(awaitable: object, deadline: float) -> object:
    try:
        return await asyncio.wait_for(awaitable, max(0.0, deadline - time.monotonic()))
    except TimeoutError:
        raise _error(
            "TIMEOUT", "M3 total deadline expired before execution", "preflight", False
        ) from None


async def _read_stderr(process: asyncio.subprocess.Process) -> bytes:
    assert process.stderr is not None
    return (await process.stderr.read(_STDERR_BYTES + 1))[:_STDERR_BYTES]


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), 2.0)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    await process.wait()


def _environment(root: Path, threads: int) -> dict[str, str]:
    value = str(threads)
    return {
        "HOME": str(root),
        "TMPDIR": str(root),
        "PATH": str(Path(sys.executable).parent),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OMP_NUM_THREADS": value,
        "OPENBLAS_NUM_THREADS": value,
        "MKL_NUM_THREADS": value,
        "NUMEXPR_NUM_THREADS": value,
        "POLARS_MAX_THREADS": value,
        "VECLIB_MAXIMUM_THREADS": value,
        "BLIS_NUM_THREADS": value,
    }


def _process_sample(pid: int) -> tuple[int, float]:
    try:
        process = psutil.Process(pid)
        own = process.memory_info().rss
        own_times = process.cpu_times()
        cpu = float(own_times.user + own_times.system)
        try:
            rss = own
            for child in process.children(recursive=True):
                rss += child.memory_info().rss
                child_times = child.cpu_times()
                cpu += float(child_times.user + child_times.system)
            return rss, cpu
        except (OSError, psutil.Error, ProcessLookupError):
            # Restricted macOS hosts may deny the system-wide PID enumeration used
            # by ``children``.  M3 never spawns grandchildren, so own RSS remains
            # the correct enforceable boundary there.
            return own, cpu
    except (OSError, psutil.Error, ProcessLookupError):
        return 0, 0.0


def _tree_bytes(root: Path) -> int:
    total = 0
    try:
        for directory, _names, files in os.walk(root):
            for name in files:
                total += (Path(directory) / name).stat().st_size
    except OSError:
        pass
    return total


def _bounded(value: str, maximum: int) -> str:
    content = value.encode("utf-8")
    return value if len(content) <= maximum else content[:maximum].decode("utf-8", "ignore")


def _phase(value: object) -> Literal["preflight", "execution", "output_validation"]:
    if value == "preflight" or value == "output_validation":
        return value
    return "execution"


def _error(
    code: str,
    message: str,
    phase: Literal["preflight", "execution", "output_validation"],
    started: bool,
    *,
    retryable: bool = False,
    started_at: datetime | None = None,
) -> CadMeasurementWorkerError:
    return CadMeasurementWorkerError(
        code,
        message,
        phase=phase,
        execution_started=started,
        retryable=retryable,
        started_at=started_at,
    )


__all__ = [
    "CadMeasurementWorkerAnalysisResult",
    "CadMeasurementWorkerComputeResult",
    "CadMeasurementWorkerError",
    "CadMeasurementWorkerObservation",
    "SubprocessCadMeasurementWorker",
]
