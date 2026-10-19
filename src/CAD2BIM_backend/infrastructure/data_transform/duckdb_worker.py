"""Parent-side supervisor for one short-lived M2 DuckDB process."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

import psutil
import pyarrow as pa

from CAD2BIM_backend.domain.data_transform import (
    DataTransformWorkerError,
    DataTransformWorkerPolicy,
    DataTransformWorkerReceipt,
    DataTransformWorkerRequest,
)
from CAD2BIM_backend.ports.data_transform import ArrowTransformInput, ArrowTransformOutput
from CAD2BIM_backend.infrastructure.worker_runtime.capacity import (
    HostWorkerCapacity, WorkerCapacityBusy, WorkerCapacityUnavailable,
    default_capacity_root,
)
from CAD2BIM_backend.infrastructure.worker_runtime.supervisor import (
    WorkerLaunchSpec, WorkerProcessSupervisor,
)


_INPUT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_StagedInput = tuple[str, Path, str, str | None]


class _StagingOwnershipTransferred(DataTransformWorkerError):
    def __init__(self) -> None:
        super().__init__(
            "TRANSFORM_TIMEOUT",
            "The request was cancelled before DuckDB execution started",
            phase="preflight",
            execution_started=False,
        )


class _AggregateOutputBudget:
    def __init__(self, policy: DataTransformWorkerPolicy) -> None:
        self._policy = policy
        self._lock = threading.Lock()
        self.rows = 0
        self.arrow_nbytes = 0

    def add(self, batch: pa.RecordBatch) -> None:
        with self._lock:
            self.rows += batch.num_rows
            self.arrow_nbytes += batch.nbytes
            if (
                self.rows > self._policy.output_row_limit
                or self.arrow_nbytes > self._policy.output_arrow_bytes
            ):
                raise DataTransformWorkerError(
                    "TRANSFORM_OUTPUT_BUDGET_EXCEEDED",
                    "Aggregate Arrow outputs exceed the M2b worker budget",
                    phase="output_validation",
                    execution_started=True,
                )


class SubprocessDuckDBDataTransformWorker:
    """Execute one bounded script in a fresh process and private DuckDB connection."""

    def __init__(
        self,
        *,
        policy: DataTransformWorkerPolicy | None = None,
        python_executable: str | None = None,
        capacity_root: str | Path | None = None,
        capacity: HostWorkerCapacity | None = None,
    ) -> None:
        self._policy = policy or DataTransformWorkerPolicy()
        # Preserve a virtual-environment launcher symlink. Resolving it to the base
        # interpreter drops the venv's installed worker dependencies.
        self._python = str(Path(python_executable or sys.executable).absolute())
        self._capacity = capacity or HostWorkerCapacity(
            max_concurrent_workers=self._policy.max_concurrent_workers,
            capacity_root=capacity_root or default_capacity_root(pool="sql"),
            resource_pool="sql",
        )
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    async def verify_ready(self) -> None:
        """Prove the installed child can lock DuckDB and round-trip Arrow IPC."""

        probe = pa.table({"worker_probe": [1]})
        result = await self.execute(
            DataTransformWorkerRequest("SELECT worker_probe FROM runtime_probe"),
            (ArrowTransformInput("runtime_probe", probe.to_reader()),),
        )
        if (
            len(result) != 1
            or result[0].receipt.row_count != 1
            or result[0].schema.names != ["worker_probe"]
        ):
            raise RuntimeError("DuckDB data transform worker readiness probe is invalid")

    async def close(self) -> None:
        """Wait for any cancelled staging thread and its owned scratch cleanup."""

        while self._cleanup_tasks:
            await asyncio.gather(*tuple(self._cleanup_tasks), return_exceptions=True)

    async def execute(
        self,
        request: DataTransformWorkerRequest,
        inputs: tuple[ArrowTransformInput, ...],
        *, deadline: float | None = None,
    ) -> tuple[ArrowTransformOutput, ...]:
        maximum_deadline = time.monotonic() + (
            self._policy.capacity_wait_seconds + self._policy.execution_seconds
            + self._policy.terminal_seconds + self._policy.cleanup_seconds
        )
        deadline = maximum_deadline if deadline is None else min(deadline, maximum_deadline)
        capacity_deadline = min(
            time.monotonic() + self._policy.capacity_wait_seconds,
            deadline - self._policy.execution_seconds - self._policy.terminal_seconds - self._policy.cleanup_seconds,
        )
        local_capacity_acquired = False
        host_capacity_fd: int | None = None
        ownership_transferred = False
        try:
            host_capacity_fd = await self._acquire_host_capacity(capacity_deadline)
            try:
                await asyncio.wait_for(
                    self._capacity.acquire(),
                    timeout=max(0.0, capacity_deadline - time.monotonic()),
                )
            except TimeoutError:
                raise self._capacity_busy() from None
            local_capacity_acquired = True
            try:
                return await self._execute_with_capacity(
                    request, inputs, host_capacity_fd,
                    min(time.monotonic() + self._policy.execution_seconds,
                        deadline - self._policy.terminal_seconds - self._policy.cleanup_seconds),
                )
            except _StagingOwnershipTransferred as exc:
                ownership_transferred = True
                if isinstance(exc.__cause__, TimeoutError):
                    raise DataTransformWorkerError(
                        "TRANSFORM_TIMEOUT", "Data transform staging exceeded its execution budget",
                        phase="preflight", execution_started=False,
                    ) from None
                raise
        finally:
            if not ownership_transferred:
                if host_capacity_fd is not None:
                    os.close(host_capacity_fd)
                if local_capacity_acquired:
                    self._capacity.release()

    async def _acquire_host_capacity(self, deadline: float) -> int:
        try:
            return await self._capacity.acquire_host(deadline)
        except (WorkerCapacityBusy, WorkerCapacityUnavailable) as exc:
            raise DataTransformWorkerError(
                "TRANSFORM_CAPACITY_BUSY" if isinstance(exc, WorkerCapacityBusy) else "TRANSFORM_SERVICE_UNAVAILABLE",
                self._capacity.failure_message(exc), phase="preflight", execution_started=False,
                retryable=isinstance(exc, WorkerCapacityBusy) and exc.retryable,
            ) from exc

    @staticmethod
    def _capacity_busy() -> DataTransformWorkerError:
        return DataTransformWorkerError(
            "TRANSFORM_CAPACITY_BUSY",
            "Data transform worker capacity is busy",
            phase="preflight",
            execution_started=False,
        )

    async def _execute_with_capacity(
        self,
        request: DataTransformWorkerRequest,
        inputs: tuple[ArrowTransformInput, ...],
        host_capacity_fd: int,
        deadline: float,
    ) -> tuple[ArrowTransformOutput, ...]:
        self._validate_inputs(inputs)
        self._validate_outputs(request, inputs)
        root: Path | None = None
        try:
            try:
                root = Path(tempfile.mkdtemp(prefix="cad2bim-transform-"))
                os.chmod(root, 0o700)
                staged = await self._await_staging(root, inputs, host_capacity_fd, deadline)
            except _StagingOwnershipTransferred:
                root = None
                raise
            except DataTransformWorkerError:
                raise
            except (OSError, pa.ArrowException) as exc:
                raise self._service_unavailable() from exc
            for attempt in range(2):
                try:
                    return await self._invoke_child(
                        request,
                        staged,
                        root / f"worker-{attempt + 1}",
                        host_capacity_fd,
                        deadline,
                    )
                except DataTransformWorkerError as exc:
                    if attempt == 0 and self._is_retryable_prestart_failure(exc):
                        continue
                    raise
            raise AssertionError("unreachable DuckDB worker retry state")
        finally:
            if root is not None:
                await asyncio.to_thread(shutil.rmtree, root, True)

    async def _await_staging(
        self,
        root: Path,
        inputs: tuple[ArrowTransformInput, ...],
        host_capacity_fd: int,
        deadline: float,
    ) -> tuple[_StagedInput, ...]:
        task = asyncio.create_task(asyncio.to_thread(self._stage_inputs, root, inputs))
        try:
            return await asyncio.wait_for(asyncio.shield(task), max(0.0, deadline - time.monotonic()))
        except (asyncio.CancelledError, TimeoutError) as exc:
            cleanup = asyncio.create_task(
                self._finish_cancelled_staging(task, root, host_capacity_fd)
            )
            self._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._cleanup_tasks.discard)
            raise _StagingOwnershipTransferred from exc

    async def _finish_cancelled_staging(
        self,
        staging: asyncio.Task[tuple[_StagedInput, ...]],
        root: Path,
        host_capacity_fd: int,
    ) -> None:
        try:
            await staging
        except BaseException:
            pass
        finally:
            try:
                await asyncio.to_thread(shutil.rmtree, root, True)
            finally:
                try:
                    os.close(host_capacity_fd)
                finally:
                    self._capacity.release()

    @staticmethod
    def _service_unavailable() -> DataTransformWorkerError:
        return DataTransformWorkerError(
            "TRANSFORM_SERVICE_UNAVAILABLE",
            "DuckDB worker infrastructure is temporarily unavailable",
            phase="preflight",
            execution_started=False,
        )

    @staticmethod
    def _is_retryable_prestart_failure(exc: DataTransformWorkerError) -> bool:
        return (
            exc.code == "TRANSFORM_SERVICE_UNAVAILABLE"
            and exc.phase == "preflight"
            and not exc.execution_started
            and exc.retryable is not False
        )

    @staticmethod
    def _validate_inputs(inputs: tuple[ArrowTransformInput, ...]) -> None:
        if not 1 <= len(inputs) <= 24:
            raise DataTransformWorkerError(
                "TRANSFORM_SQL_NOT_ALLOWED",
                "DuckDB transforms require 1..24 Arrow input relations",
                phase="preflight",
                execution_started=False,
            )
        names = [item.input_name for item in inputs]
        if len(names) != len({name.casefold() for name in names}) or any(
            _INPUT_NAME.fullmatch(name) is None for name in names
        ):
            raise DataTransformWorkerError(
                "TRANSFORM_SQL_NOT_ALLOWED",
                "Arrow input names are invalid or duplicated",
                phase="preflight",
                execution_started=False,
            )
        if any(not isinstance(item.reader, pa.RecordBatchReader) for item in inputs):
            raise TypeError("ArrowTransformInput.reader must be a RecordBatchReader")

    def _validate_outputs(
        self,
        request: DataTransformWorkerRequest,
        inputs: tuple[ArrowTransformInput, ...],
    ) -> None:
        count = len(request.output_names) if request.result_mode == "named_relations" else 1
        if not 1 <= count <= self._policy.max_output_count:
            raise DataTransformWorkerError(
                "TRANSFORM_SQL_NOT_ALLOWED",
                "M2b requires 1..16 Worker outputs",
                phase="preflight",
                execution_started=False,
            )
        input_names = {item.input_name.casefold() for item in inputs}
        if {name.casefold() for name in request.output_names}.intersection(input_names):
            raise DataTransformWorkerError(
                "TRANSFORM_SQL_NOT_ALLOWED",
                "Worker output names must not collide with read-only input names",
                phase="preflight",
                execution_started=False,
            )

    def _stage_inputs(
        self,
        root: Path,
        inputs: tuple[ArrowTransformInput, ...],
    ) -> tuple[_StagedInput, ...]:
        total_rows = 0
        total_nbytes = 0
        completed_file_bytes = 0
        staged: list[_StagedInput] = []
        for ordinal, item in enumerate(inputs):
            path = root / f"input-{ordinal:02d}.arrow"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", buffering=0) as raw:
                with pa.ipc.new_stream(raw, item.reader.schema) as writer:
                    for batch in item.reader:
                        total_rows += batch.num_rows
                        total_nbytes += batch.nbytes
                        if (
                            total_rows > self._policy.input_row_limit
                            or total_nbytes > self._policy.input_arrow_bytes
                        ):
                            raise DataTransformWorkerError(
                                "TRANSFORM_INPUT_BUDGET_EXCEEDED",
                                "Arrow inputs exceed the M2a worker budget",
                                phase="preflight",
                                execution_started=False,
                            )
                        writer.write_batch(batch)
                        if completed_file_bytes + raw.tell() > self._policy.scratch_bytes:
                            raise DataTransformWorkerError(
                                "TRANSFORM_RESOURCE_LIMIT",
                                "Data transform call exceeded its private scratch budget",
                                phase="preflight",
                                execution_started=False,
                            )
            completed_file_bytes += path.stat().st_size
            if completed_file_bytes > self._policy.scratch_bytes:
                raise DataTransformWorkerError(
                    "TRANSFORM_RESOURCE_LIMIT",
                    "Data transform call exceeded its private scratch budget",
                    phase="preflight",
                    execution_started=False,
                )
            input_format = getattr(item, "input_format", "parquet")
            json_kind = getattr(item, "json_kind", None)
            staged.append(
                (
                    item.input_name,
                    path,
                    str(getattr(input_format, "value", input_format)),
                    None if json_kind is None else str(getattr(json_kind, "value", json_kind)),
                )
            )
        return tuple(staged)

    def _child_policy_for_root(self, root: Path) -> dict[str, object]:
        remaining = self._policy.scratch_bytes - self._directory_size(root)
        if remaining <= 0:
            raise DataTransformWorkerError(
                "TRANSFORM_RESOURCE_LIMIT",
                "Data transform call exceeded its private scratch budget",
                phase="preflight",
                execution_started=False,
            )
        child_policy = asdict(self._policy)
        child_policy["scratch_bytes"] = remaining
        return child_policy

    async def _invoke_child(
        self,
        request: DataTransformWorkerRequest,
        staged: tuple[_StagedInput, ...],
        scratch: Path,
        capacity_fd: int,
        deadline: float,
    ) -> tuple[ArrowTransformOutput, ...]:
        input_fds: list[int] = []
        output_reads: list[int] = []
        output_writes: list[int] = []
        interrupt_read = interrupt_write = -1
        process: asyncio.subprocess.Process | None = None
        output_tasks: tuple[
            asyncio.Task[tuple[pa.Schema, tuple[pa.RecordBatch, ...], int, int]], ...
        ] = ()
        stderr_task: asyncio.Task[bytes] | None = None
        try:
            call_root = scratch.parent
            child_policy = self._child_policy_for_root(call_root)
            scratch.mkdir(mode=0o700)
            input_fds = [
                os.open(path, os.O_RDONLY) for _name, path, _input_format, _json_kind in staged
            ]
            output_names: tuple[str | None, ...] = (
                tuple(request.output_names) if request.result_mode == "named_relations" else (None,)
            )
            output_pipes = [os.pipe() for _name in output_names]
            output_reads = [read for read, _write in output_pipes]
            output_writes = [write for _read, write in output_pipes]
            interrupt_read, interrupt_write = os.pipe()
            payload = {
                "code": request.code,
                "result_mode": request.result_mode,
                "parameters": [
                    {
                        "name": item.name,
                        "value": list(item.value) if isinstance(item.value, tuple) else item.value,
                    }
                    for item in request.parameters
                ],
                "inputs": [
                    {
                        "input_name": name,
                        "fd": descriptor,
                        "input_format": input_format,
                        "json_kind": json_kind,
                    }
                    for (name, _path, input_format, json_kind), descriptor in zip(
                        staged, input_fds, strict=True
                    )
                ],
                "outputs": [
                    {"payload_name": name, "fd": descriptor}
                    for name, descriptor in zip(output_names, output_writes, strict=True)
                ],
                "interrupt_fd": interrupt_read,
                "policy": child_policy,
            }
            process = await WorkerProcessSupervisor.launch(
                WorkerLaunchSpec(command=(self._python, "-I", "-m",
                    "CAD2BIM_backend.infrastructure.data_transform.duckdb_child"),
                cwd=scratch, env={
                    "HOME": str(scratch),
                    "TMPDIR": str(scratch),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1",
                },
                pass_fds=(*input_fds, *output_writes, interrupt_read),
                capacity_fds=(capacity_fd,)),
                stdin=asyncio.subprocess.PIPE,
                deadline=deadline,
                cleanup_seconds=self._policy.cleanup_seconds,
            )
            for descriptor in output_writes:
                os.close(descriptor)
            output_writes.clear()
            os.close(interrupt_read)
            interrupt_read = -1
            for descriptor in input_fds:
                os.close(descriptor)
            input_fds.clear()

            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise RuntimeError("DuckDB worker pipes are unavailable")
            process.stdin.write(
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            )
            await process.stdin.drain()
            process.stdin.close()

            aggregate = _AggregateOutputBudget(self._policy)
            output_tasks = tuple(
                asyncio.create_task(asyncio.to_thread(self._read_output, descriptor, aggregate))
                for descriptor in output_reads
            )
            output_reads.clear()
            stderr_task = asyncio.create_task(
                self._read_bounded(process.stderr, self._policy.capture_bytes)
            )
            return await self._supervise(
                process,
                process.stdout,
                output_tasks,
                output_names,
                stderr_task,
                call_root,
                interrupt_write,
                deadline,
            )
        except DataTransformWorkerError:
            raise
        except OSError as exc:
            raise self._service_unavailable() from exc
        finally:
            for descriptor in (
                *input_fds,
                *output_reads,
                *output_writes,
                interrupt_read,
                interrupt_write,
            ):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            for task in (*output_tasks, stderr_task):
                if task is not None and not task.done():
                    task.cancel()
            try:
                await asyncio.gather(
                    *(task for task in (*output_tasks, stderr_task) if task is not None),
                    return_exceptions=True,
                )
            finally:
                if process is not None:
                    await WorkerProcessSupervisor.stop_process(process, cleanup_seconds=self._policy.cleanup_seconds)

    async def _supervise(
        self,
        process: asyncio.subprocess.Process,
        stdout: asyncio.StreamReader,
        output_tasks: tuple[
            asyncio.Task[tuple[pa.Schema, tuple[pa.RecordBatch, ...], int, int]], ...
        ],
        output_names: tuple[str | None, ...],
        stderr_task: asyncio.Task[bytes],
        call_root: Path,
        interrupt_fd: int,
        deadline: float,
    ) -> tuple[ArrowTransformOutput, ...]:
        try:
            worker_process = psutil.Process(WorkerProcessSupervisor.execution_pid(process))
        except psutil.Error as exc:
            raise self._service_unavailable() from exc
        spawned_at = time.monotonic()
        execution_started_at: float | None = None
        execution_ms = 0
        status_bytes = 0
        while True:
            try:
                line = await asyncio.wait_for(stdout.readline(), timeout=0.02)
            except TimeoutError:
                line = b""
            if line:
                status_bytes += len(line)
                if status_bytes > self._policy.capture_bytes:
                    await self._stop_process(process, interrupt_fd)
                    if execution_started_at is None:
                        raise self._service_unavailable()
                    raise DataTransformWorkerError(
                        "TRANSFORM_WORKER_FAILED",
                        "DuckDB worker status exceeded its byte limit",
                        phase="execution",
                        execution_started=True,
                    )
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    event = {}
                kind = event.get("event")
                if kind == "started":
                    execution_started_at = time.monotonic()
                elif kind == "error":
                    await process.wait()
                    started = execution_started_at is not None or bool(event.get("started"))
                    code = str(event.get("code") or "TRANSFORM_WORKER_FAILED")
                    phase = str(event.get("phase") or "execution")
                    if started and code == "TRANSFORM_SERVICE_UNAVAILABLE":
                        code = "TRANSFORM_WORKER_FAILED"
                        phase = "execution"
                    raise DataTransformWorkerError(
                        code,
                        str(event.get("message") or "DuckDB worker failed"),
                        phase=phase,
                        execution_started=started,
                    )
                elif kind == "done":
                    execution_ms = max(0, int(event.get("execution_ms") or 0))
                    await process.wait()
                    raw_outputs = await asyncio.gather(*output_tasks)
                    await stderr_task
                    if process.returncode != 0:
                        raise DataTransformWorkerError(
                            "TRANSFORM_WORKER_FAILED",
                            "DuckDB worker exited without a trusted result",
                            phase="execution",
                            execution_started=True,
                        )
                    return tuple(
                        ArrowTransformOutput(
                            receipt=DataTransformWorkerReceipt(
                                row_count=rows,
                                arrow_nbytes=nbytes,
                                execution_ms=execution_ms,
                            ),
                            schema=schema,
                            batches=batches,
                            payload_name=payload_name,
                        )
                        for payload_name, (schema, batches, rows, nbytes) in zip(
                            output_names, raw_outputs, strict=True
                        )
                    )

            failed_output_task = next(
                (
                    task
                    for task in output_tasks
                    if task.done() and not task.cancelled() and task.exception() is not None
                ),
                None,
            )
            if failed_output_task is not None:
                await self._stop_process(process, interrupt_fd)
                failure = failed_output_task.exception()
                if isinstance(failure, DataTransformWorkerError):
                    raise failure
                if execution_started_at is None:
                    raise self._service_unavailable() from failure
                raise DataTransformWorkerError(
                    "TRANSFORM_WORKER_FAILED",
                    "DuckDB worker output IPC failed",
                    phase="execution",
                    execution_started=True,
                ) from failure
            if stderr_task.done() and stderr_task.exception() is not None:
                await self._stop_process(process, interrupt_fd)
                failure = stderr_task.exception()
                if execution_started_at is None:
                    raise self._service_unavailable() from failure
                raise DataTransformWorkerError(
                    "TRANSFORM_WORKER_FAILED",
                    "DuckDB worker diagnostics IPC failed",
                    phase="execution",
                    execution_started=True,
                ) from failure

            now = time.monotonic()
            execution_timed_out = (
                execution_started_at is not None
                and now - execution_started_at > self._policy.wall_seconds
            )
            preflight_timed_out = execution_started_at is None and now - spawned_at > max(
                10.0, self._policy.wall_seconds
            )
            if execution_timed_out or preflight_timed_out or now >= deadline:
                await self._stop_process(process, interrupt_fd)
                raise DataTransformWorkerError(
                    "TRANSFORM_TIMEOUT",
                    "DuckDB worker exceeded its wall-clock deadline",
                    phase="execution" if execution_started_at else "preflight",
                    execution_started=execution_started_at is not None,
                )
            try:
                rss = worker_process.memory_info().rss
            except psutil.Error:
                rss = 0
            if (
                rss > self._policy.rss_bytes
                or self._directory_size(call_root) > self._policy.scratch_bytes
            ):
                await self._stop_process(process, interrupt_fd)
                raise DataTransformWorkerError(
                    "TRANSFORM_RESOURCE_LIMIT",
                    "DuckDB worker exceeded its RSS or scratch budget",
                    phase="execution" if execution_started_at else "preflight",
                    execution_started=execution_started_at is not None,
                )
            if process.returncode is not None:
                await process.wait()
                if execution_started_at is None:
                    raise self._service_unavailable()
                code = (
                    "TRANSFORM_RESOURCE_LIMIT"
                    if process.returncode in {-signal.SIGXCPU, -signal.SIGKILL}
                    else "TRANSFORM_WORKER_FAILED"
                )
                raise DataTransformWorkerError(
                    code,
                    "DuckDB worker exited before producing a trusted result",
                    phase="execution",
                    execution_started=True,
                )

    def _read_output(
        self,
        descriptor: int,
        aggregate: _AggregateOutputBudget,
    ) -> tuple[pa.Schema, tuple[pa.RecordBatch, ...], int, int]:
        rows = 0
        nbytes = 0
        batches: list[pa.RecordBatch] = []
        try:
            # Arrow IPC messages exceed a pipe's atomic read size. Buffering
            # satisfies Arrow's exact-size reads when the OS returns short reads.
            with os.fdopen(descriptor, "rb") as raw:
                reader = pa.ipc.open_stream(raw)
                schema = reader.schema
                if not 1 <= len(schema) <= self._policy.max_output_columns:
                    raise DataTransformWorkerError(
                        "TRANSFORM_OUTPUT_INVALID",
                        "A DuckDB output must contain 1..512 columns",
                        phase="output_validation",
                        execution_started=True,
                    )
                for batch in reader:
                    rows += batch.num_rows
                    nbytes += batch.nbytes
                    aggregate.add(batch)
                    batches.append(batch)
                return schema, tuple(batches), rows, nbytes
        except DataTransformWorkerError:
            raise
        except (OSError, pa.ArrowException) as exc:
            raise RuntimeError("DuckDB worker returned invalid Arrow IPC") from exc

    @staticmethod
    async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while chunk := await stream.read(min(4096, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise RuntimeError("DuckDB worker diagnostics exceeded their byte limit")
        return b"".join(chunks)

    async def _stop_process(
        self,
        process: asyncio.subprocess.Process,
        interrupt_fd: int | None,
    ) -> None:
        if interrupt_fd is not None:
            try:
                os.write(interrupt_fd, b"!")
            except OSError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=self._policy.cleanup_seconds)
            return
        except TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

    @staticmethod
    def _directory_size(root: Path) -> int:
        total = 0
        try:
            for path in root.rglob("*"):
                try:
                    if path.is_file() and not path.is_symlink():
                        total += path.stat().st_size
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            return 0
        return total


__all__ = ["SubprocessDuckDBDataTransformWorker"]
