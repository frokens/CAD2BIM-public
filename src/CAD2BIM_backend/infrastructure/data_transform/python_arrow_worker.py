"""Parent supervisor for Direct controlled-local Python/Arrow scheme A.

The subprocess, private descriptors, audit hook, and resource checks are
defense in depth for locally controlled code.  This class intentionally makes
no hostile multi-tenant isolation claim.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO

import psutil
import pyarrow as pa

from CAD2BIM_backend.domain.data_transform import (
    DataTransformWorkerError,
    DataTransformWorkerReceipt,
    PythonArrowWorkerOutputDeclaration,
    PythonArrowWorkerPolicy,
    PythonArrowWorkerRequest,
)
from CAD2BIM_backend.domain.data_query_python import PythonArrowQueryWorkerRequest
from CAD2BIM_backend.infrastructure.data_transform.python_arrow_sandbox import (
    DarwinSeatbeltPythonArrowSandbox,
)
from CAD2BIM_backend.infrastructure.data_transform.python_worker_environment import (
    DataPythonWorkerDiagnosticCategory,
    DataPythonWorkerEnvironmentError,
    data_python_worker_environment_error,
)
from CAD2BIM_backend.infrastructure.data_transform.worker_protocol import (
    MAX_CONTROL_BYTES,
    MAX_STATUS_BYTES,
    WorkerProtocolError,
    read_json_frame,
    write_json_frame,
)
from CAD2BIM_backend.infrastructure.worker_runtime.capacity import (
    HostWorkerCapacity,
    WorkerCapacityBusy,
    WorkerCapacityUnavailable,
    default_capacity_root,
)
from CAD2BIM_backend.infrastructure.worker_runtime.supervisor import (
    WorkerLaunchSpec,
    WorkerProcessBudget,
    WorkerProcessSupervisor,
    directory_size,
)
from CAD2BIM_backend.ports.data_transform import ArrowTransformInput, ArrowTransformOutput


_INPUT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_QUERY_RESULT_NAME = "__cad2bim_query_result"


@dataclass(frozen=True, slots=True)
class _QueryOutputDeclaration:
    payload_name: str = _QUERY_RESULT_NAME
    format: str = "parquet"
    json_kind: None = None


def _annotate_cancellation(
    error: asyncio.CancelledError,
    failure: DataTransformWorkerError,
) -> None:
    """Attach a bounded lifecycle fact without consuming caller cancellation.

    The application deadline cancels this task and later reconciles the worker
    lifecycle.  External cancellation must still propagate unchanged, so the
    worker records the fact on the original ``CancelledError`` instead of
    replacing it with a domain exception.
    """

    if not isinstance(getattr(error, "worker_failure", None), DataTransformWorkerError):
        setattr(error, "worker_failure", failure)


class _AggregateOutputBudget:
    def __init__(self, policy: PythonArrowWorkerPolicy) -> None:
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
                    "Aggregate Python/Arrow outputs exceed the worker budget",
                    phase="output_validation",
                    execution_started=True,
                )


def read_arrow_output(
    descriptor: int,
    aggregate: _AggregateOutputBudget,
    *,
    policy: PythonArrowWorkerPolicy,
) -> tuple[pa.Schema, tuple[pa.RecordBatch, ...], int, int]:
    rows = 0
    nbytes = 0
    batches: list[pa.RecordBatch] = []
    try:
        with os.fdopen(descriptor, "rb") as raw:
            reader = pa.ipc.open_stream(raw)
            schema = reader.schema
            if not 1 <= len(schema) <= policy.max_output_columns:
                raise DataTransformWorkerError(
                    "TRANSFORM_OUTPUT_INVALID",
                    "A Python/Arrow output must contain 1..512 columns",
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
        raise RuntimeError("Python worker returned invalid Arrow IPC") from exc


class SubprocessPythonArrowDataTransformWorker:
    """Run one declared transform in a fresh ``python -I`` process."""

    def __init__(
        self,
        *,
        policy: PythonArrowWorkerPolicy | None = None,
        python_executable: str,
        capacity_root: str | Path | None = None,
        capacity: HostWorkerCapacity | None = None,
    ) -> None:
        self._policy = policy or PythonArrowWorkerPolicy()
        # Resolving a venv launcher symlink can drop the installed environment.
        executable = Path(python_executable)
        if (
            not executable.is_absolute()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise data_python_worker_environment_error(
                DataPythonWorkerDiagnosticCategory.INTERPRETER_MISMATCH,
                "the executable must be an absolute executable file",
                python_executable=executable if executable.is_absolute() else None,
            )
        self._python = str(executable)
        self._os_sandbox = DarwinSeatbeltPythonArrowSandbox(self._python)
        self._capacity = capacity or HostWorkerCapacity(
            max_concurrent_workers=self._policy.max_concurrent_workers,
            capacity_root=capacity_root or default_capacity_root(),
        )
        self._process_supervisor = WorkerProcessSupervisor()
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    async def verify_ready(self) -> None:
        probe = pa.table({"worker_probe": [1]})
        try:
            outputs = await self.execute(
                PythonArrowWorkerRequest(
                    code=(
                        "def transform(ctx):\n"
                        "    ctx.write_table('probe_result', ctx.table('runtime_probe'))\n"
                    ),
                    outputs=(
                        PythonArrowWorkerOutputDeclaration(
                            payload_name="probe_result",
                            format="parquet",
                        ),
                    ),
                ),
                (ArrowTransformInput("runtime_probe", probe.to_reader()),),
            )
        except DataPythonWorkerEnvironmentError:
            raise
        except (DataTransformWorkerError, OSError, RuntimeError, ValueError) as exc:
            raise data_python_worker_environment_error(
                DataPythonWorkerDiagnosticCategory.CHILD_READINESS_FAILED,
                "the fixed readiness request could not complete",
                python_executable=self._python,
            ) from exc
        if (
            len(outputs) != 1
            or outputs[0].receipt.row_count != 1
            or outputs[0].schema.names != ["worker_probe"]
        ):
            raise data_python_worker_environment_error(
                DataPythonWorkerDiagnosticCategory.CHILD_READINESS_FAILED,
                "the fixed readiness request returned an invalid result",
                python_executable=self._python,
            )

    async def close(self) -> None:
        while self._cleanup_tasks:
            await asyncio.gather(*tuple(self._cleanup_tasks), return_exceptions=True)

    async def execute(
        self,
        request: PythonArrowWorkerRequest,
        inputs: tuple[ArrowTransformInput, ...],
        *, deadline: float | None = None,
    ) -> tuple[ArrowTransformOutput, ...]:
        return await self._execute(request, inputs, deadline=deadline)

    async def execute_query(
        self,
        request: PythonArrowQueryWorkerRequest,
        inputs: tuple[ArrowTransformInput, ...],
        *, deadline: float | None = None,
    ) -> ArrowTransformOutput:
        outputs = await self._execute(request, inputs, deadline=deadline)
        if len(outputs) != 1 or outputs[0].payload_name is not None:
            raise DataTransformWorkerError(
                "TRANSFORM_WORKER_FAILED",
                "Python query worker returned an invalid result envelope",
                phase="execution",
                execution_started=True,
            )
        return outputs[0]

    async def _execute(
        self,
        request: PythonArrowWorkerRequest | PythonArrowQueryWorkerRequest,
        inputs: tuple[ArrowTransformInput, ...],
        *, deadline: float | None = None,
    ) -> tuple[ArrowTransformOutput, ...]:
        maximum_deadline = time.monotonic() + self._policy.total_call_seconds
        deadline = maximum_deadline if deadline is None else min(deadline, maximum_deadline)
        request_kind = (
            "query" if isinstance(request, PythonArrowQueryWorkerRequest) else "transformation"
        )
        acquired = False
        capacity_fd: int | None = None
        root: Path | None = None
        try:
            self._validate_request(request, inputs)
            capacity_deadline = min(
                time.monotonic() + self._policy.capacity_wait_seconds,
                deadline - self._policy.execution_seconds - self._policy.terminal_seconds - self._policy.cleanup_seconds,
            )
            try:
                capacity_fd = await self._acquire_host_capacity(capacity_deadline)
            except asyncio.CancelledError as exc:
                _annotate_cancellation(exc, self._capacity_busy())
                raise
            try:
                await asyncio.wait_for(
                    self._capacity.acquire(),
                    timeout=max(0.0, capacity_deadline - time.monotonic()),
                )
            except TimeoutError:
                raise self._capacity_busy() from None
            except asyncio.CancelledError as exc:
                _annotate_cancellation(exc, self._capacity_busy())
                raise
            acquired = True
            deadline = min(
                deadline - self._policy.terminal_seconds - self._policy.cleanup_seconds,
                time.monotonic() + self._policy.execution_seconds,
            )
            prefix = (
                "cad2bim-python-query-"
                if isinstance(request, PythonArrowQueryWorkerRequest)
                else "cad2bim-python-transform-"
            )
            root = Path(tempfile.mkdtemp(prefix=prefix))
            os.chmod(root, 0o700)
            staging = asyncio.create_task(asyncio.to_thread(self._stage_inputs, root, inputs))
            try:
                staged = await asyncio.wait_for(
                    asyncio.shield(staging),
                    timeout=max(0.0, deadline - time.monotonic()),
                )
            except asyncio.CancelledError as exc:
                cleanup = asyncio.create_task(
                    self._finish_cancelled_staging(staging, root, capacity_fd)
                )
                self._cleanup_tasks.add(cleanup)
                cleanup.add_done_callback(self._cleanup_tasks.discard)
                root = None
                capacity_fd = None
                acquired = False
                _annotate_cancellation(
                    exc,
                    self._prestart_timeout(
                        "The Python/Arrow request was cancelled while staging inputs"
                    ),
                )
                raise
            except TimeoutError:
                cleanup = asyncio.create_task(
                    self._finish_cancelled_staging(staging, root, capacity_fd)
                )
                self._cleanup_tasks.add(cleanup)
                cleanup.add_done_callback(self._cleanup_tasks.discard)
                root = None
                capacity_fd = None
                acquired = False
                raise self._prestart_timeout(
                    "The Python/Arrow request deadline was reached while staging inputs"
                ) from None
            for attempt in range(2):
                try:
                    return await self._invoke_child(
                        request,
                        staged,
                        root / f"worker-{attempt + 1}",
                        deadline,
                        capacity_fd,
                    )
                except DataTransformWorkerError as exc:
                    if attempt == 0 and self._is_retryable_prestart_failure(exc):
                        continue
                    raise
            raise AssertionError(f"unreachable Python {request_kind} worker retry state")
        except TimeoutError:
            raise self._prestart_timeout(
                f"Python/Arrow {request_kind} exceeded its total deadline before execution"
            ) from None
        except DataTransformWorkerError:
            raise
        except (OSError, pa.ArrowException) as exc:
            raise self._service_unavailable() from exc
        finally:
            try:
                if root is not None:
                    cleanup = asyncio.create_task(asyncio.to_thread(shutil.rmtree, root, True))
                    self._cleanup_tasks.add(cleanup)
                    cleanup.add_done_callback(self._cleanup_tasks.discard)
                    try:
                        await asyncio.wait_for(asyncio.shield(cleanup), self._policy.cleanup_seconds)
                    except TimeoutError:
                        pass
            finally:
                if capacity_fd is not None:
                    os.close(capacity_fd)
                if acquired:
                    self._capacity.release()

    async def _finish_cancelled_staging(
        self,
        staging: asyncio.Task[tuple[tuple[ArrowTransformInput, Path], ...]],
        root: Path,
        capacity_fd: int,
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
            "The single controlled-local Python/Arrow worker slot is busy",
            phase="preflight",
            execution_started=False,
        )

    @staticmethod
    def _service_unavailable() -> DataTransformWorkerError:
        return DataTransformWorkerError(
            "TRANSFORM_SERVICE_UNAVAILABLE",
            "Python/Arrow worker infrastructure is temporarily unavailable",
            phase="preflight",
            execution_started=False,
        )

    @staticmethod
    def _prestart_timeout(message: str) -> DataTransformWorkerError:
        return DataTransformWorkerError(
            "TRANSFORM_TIMEOUT",
            message,
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

    def _validate_request(
        self,
        request: PythonArrowWorkerRequest | PythonArrowQueryWorkerRequest,
        inputs: tuple[ArrowTransformInput, ...],
    ) -> None:
        if not 1 <= len(inputs) <= 24:
            raise DataTransformWorkerError(
                "TRANSFORM_INPUT_BUDGET_EXCEEDED",
                "Python/Arrow requires 1..16 Package inputs plus at most 8 inline inputs",
                phase="preflight",
                execution_started=False,
            )
        names = [item.input_name for item in inputs]
        if len(names) != len(set(names)) or any(
            _INPUT_NAME.fullmatch(name) is None for name in names
        ):
            raise DataTransformWorkerError(
                "TRANSFORM_PYTHON_INVALID",
                "Arrow input names are invalid or duplicated",
                phase="preflight",
                execution_started=False,
            )
        if any(not isinstance(item.reader, pa.RecordBatchReader) for item in inputs):
            raise TypeError("ArrowTransformInput.reader must be a RecordBatchReader")
        if isinstance(request, PythonArrowQueryWorkerRequest):
            return
        if not 1 <= len(request.outputs) <= self._policy.max_output_count:
            raise DataTransformWorkerError(
                "TRANSFORM_PYTHON_INVALID",
                "Python/Arrow requires 1..16 declared outputs",
                phase="preflight",
                execution_started=False,
            )
        if set(request.output_names).intersection(names):
            raise DataTransformWorkerError(
                "TRANSFORM_PYTHON_INVALID",
                "Python output names must not collide with input names",
                phase="preflight",
                execution_started=False,
            )

    def _stage_inputs(
        self,
        root: Path,
        inputs: tuple[ArrowTransformInput, ...],
    ) -> tuple[tuple[ArrowTransformInput, Path], ...]:
        total_rows = 0
        total_nbytes = 0
        completed_bytes = 0
        staged: list[tuple[ArrowTransformInput, Path]] = []
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
                                "Arrow inputs exceed the Python worker budget",
                                phase="preflight",
                                execution_started=False,
                            )
                        writer.write_batch(batch)
                        if completed_bytes + raw.tell() > self._policy.scratch_bytes:
                            raise DataTransformWorkerError(
                                "TRANSFORM_RESOURCE_LIMIT",
                                "Python transform exceeded its private scratch budget",
                                phase="preflight",
                                execution_started=False,
                            )
            completed_bytes += path.stat().st_size
            staged.append((item, path))
        return tuple(staged)

    async def _invoke_child(
        self,
        request: PythonArrowWorkerRequest | PythonArrowQueryWorkerRequest,
        staged: tuple[tuple[ArrowTransformInput, Path], ...],
        scratch: Path,
        deadline: float,
        capacity_fd: int,
    ) -> tuple[ArrowTransformOutput, ...]:
        input_fds: list[int] = []
        output_reads: list[int] = []
        output_writes: list[int] = []
        control_read = control_write = status_read = status_write = -1
        status_stream: BinaryIO | None = None
        control_stream: BinaryIO | None = None
        process: asyncio.subprocess.Process | None = None
        ipc_executor: concurrent.futures.ThreadPoolExecutor | None = None
        output_tasks: tuple[
            asyncio.Future[tuple[pa.Schema, tuple[pa.RecordBatch, ...], int, int]], ...
        ] = ()
        capture_tasks: tuple[asyncio.Task[bytes], ...] = ()
        try:
            scratch.mkdir(mode=0o700)
            input_fds = [os.open(path, os.O_RDONLY) for _item, path in staged]
            query_mode = isinstance(request, PythonArrowQueryWorkerRequest)
            outputs = (_QueryOutputDeclaration(),) if query_mode else request.outputs
            output_pipes = [os.pipe() for _output in outputs]
            output_reads = [read for read, _write in output_pipes]
            output_writes = [write for _read, write in output_pipes]
            control_read, control_write = os.pipe()
            status_read, status_write = os.pipe()
            remaining_scratch = self._policy.scratch_bytes - self._directory_size(scratch.parent)
            if remaining_scratch <= 0:
                raise DataTransformWorkerError(
                    "TRANSFORM_RESOURCE_LIMIT",
                    "Python transform exceeded its private scratch budget",
                    phase="preflight",
                    execution_started=False,
                )
            policy = asdict(self._policy)
            policy["scratch_bytes"] = remaining_scratch
            payload = {
                "mode": "query" if query_mode else "transform",
                "code": request.code,
                "parameters": [
                    {
                        "name": item.name,
                        "value": list(item.value) if isinstance(item.value, tuple) else item.value,
                    }
                    for item in request.parameters
                ],
                "inputs": [
                    {
                        "input_name": item.input_name,
                        "fd": descriptor,
                        "input_format": item.input_format,
                        "json_kind": item.json_kind,
                    }
                    for (item, _path), descriptor in zip(staged, input_fds, strict=True)
                ],
                "outputs": [
                    {
                        "payload_name": output.payload_name,
                        "format": output.format,
                        "json_kind": output.json_kind,
                        "fd": descriptor,
                    }
                    for output, descriptor in zip(outputs, output_writes, strict=True)
                ],
                "policy": policy,
            }
            env = self._sanitized_environment(scratch, control_read, status_write)
            launch_command = self._os_sandbox.command(
                scratch=scratch,
                child_args=self._os_sandbox.child_arguments,
            )
            process = await self._process_supervisor.launch(
                WorkerLaunchSpec(command=tuple(launch_command), cwd=scratch, env=env,
                    pass_fds=(*input_fds, *output_writes, control_read, status_write),
                    capacity_fds=(capacity_fd,)),
                deadline=deadline, cleanup_seconds=self._policy.cleanup_seconds,
            )
            for descriptor in (*input_fds, *output_writes, control_read, status_write):
                os.close(descriptor)
            input_fds.clear()
            output_writes.clear()
            control_read = status_write = -1
            status_stream = os.fdopen(status_read, "rb", buffering=0)
            status_read = -1
            control_stream = os.fdopen(control_write, "wb", buffering=0)
            control_write = -1
            await asyncio.to_thread(
                write_json_frame,
                control_stream,
                payload,
                limit=MAX_CONTROL_BYTES,
            )
            if process.stdout is None or process.stderr is None:
                raise RuntimeError("Python worker capture pipes are unavailable")
            aggregate = _AggregateOutputBudget(self._policy)
            ipc_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=len(outputs) + 1,
                thread_name_prefix="cad2bim-python-ipc",
            )
            loop = asyncio.get_running_loop()
            output_tasks = tuple(
                loop.run_in_executor(ipc_executor, self._read_output, descriptor, aggregate)
                for descriptor in output_reads
            )
            output_reads.clear()
            capture_tasks = (
                asyncio.create_task(self._read_bounded(process.stdout, self._policy.capture_bytes)),
                asyncio.create_task(self._read_bounded(process.stderr, self._policy.capture_bytes)),
            )
            return await self._supervise(
                process,
                control_stream,
                status_stream,
                output_tasks,
                outputs,
                query_mode,
                capture_tasks,
                scratch.parent,
                deadline,
                ipc_executor,
            )
        except asyncio.CancelledError as exc:
            _annotate_cancellation(
                exc,
                self._prestart_timeout(
                    "The Python/Arrow request was cancelled before execution started"
                ),
            )
            raise
        except DataTransformWorkerError:
            raise
        except (OSError, WorkerProtocolError) as exc:
            raise self._service_unavailable() from exc
        finally:
            for descriptor in (
                *input_fds,
                *output_reads,
                *output_writes,
                control_read,
                control_write,
                status_read,
                status_write,
            ):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            if status_stream is not None:
                status_stream.close()
            if control_stream is not None:
                control_stream.close()
            for task in (*output_tasks, *capture_tasks):
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*output_tasks, *capture_tasks, return_exceptions=True)
            finally:
                try:
                    if process is not None:
                        await WorkerProcessSupervisor.stop_process(process, cleanup_seconds=self._policy.cleanup_seconds)
                finally:
                    if ipc_executor is not None:
                        ipc_executor.shutdown(wait=True, cancel_futures=True)

    def _sanitized_environment(
        self,
        scratch: Path,
        control_fd: int,
        status_fd: int,
    ) -> dict[str, str]:
        thread_limit = str(self._policy.threads)
        xdg_cache = scratch / "xdg-cache"
        xdg_config = scratch / "xdg-config"
        xdg_data = scratch / "xdg-data"
        for path in (xdg_cache, xdg_config, xdg_data):
            path.mkdir(mode=0o700)
        return {
            "ARROW_IO_THREADS": thread_limit,
            "BLIS_NUM_THREADS": thread_limit,
            "CAD2BIM_WORKER_CONTROL_FD": str(control_fd),
            "CAD2BIM_WORKER_STATUS_FD": str(status_fd),
            "HOME": str(scratch),
            "MKL_NUM_THREADS": thread_limit,
            "NUMEXPR_NUM_THREADS": thread_limit,
            "OMP_NUM_THREADS": thread_limit,
            "OPENBLAS_NUM_THREADS": thread_limit,
            "POLARS_MAX_THREADS": thread_limit,
            "PROJ_NETWORK": "OFF",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "TMPDIR": str(scratch),
            "VECLIB_MAXIMUM_THREADS": thread_limit,
            "XDG_CACHE_HOME": str(xdg_cache),
            "XDG_CONFIG_HOME": str(xdg_config),
            "XDG_DATA_HOME": str(xdg_data),
        }

    async def _supervise(
        self,
        process: asyncio.subprocess.Process,
        control_stream: BinaryIO,
        status_stream: BinaryIO,
        output_tasks: tuple[
            asyncio.Future[tuple[pa.Schema, tuple[pa.RecordBatch, ...], int, int]], ...
        ],
        outputs: tuple[PythonArrowWorkerOutputDeclaration | _QueryOutputDeclaration, ...],
        query_mode: bool,
        capture_tasks: tuple[asyncio.Task[bytes], ...],
        call_root: Path,
        deadline: float,
        ipc_executor: concurrent.futures.ThreadPoolExecutor,
    ) -> tuple[ArrowTransformOutput, ...]:
        try:
            worker = psutil.Process(self._process_supervisor.execution_pid(process))
        except psutil.Error as exc:
            raise self._service_unavailable() from exc
        execution_started_at: float | None = None
        baseline_threads = 0
        loop = asyncio.get_running_loop()
        status_task = loop.run_in_executor(
            ipc_executor,
            functools.partial(read_json_frame, status_stream, limit=MAX_STATUS_BYTES),
        )
        try:
            while True:
                done, _pending = await asyncio.wait({status_task}, timeout=0.02)
                if done:
                    try:
                        event = status_task.result()
                    except (OSError, WorkerProtocolError) as exc:
                        output_failure = await self._wait_for_output_failure(output_tasks)
                        if isinstance(output_failure, DataTransformWorkerError):
                            raise output_failure
                        if execution_started_at is None:
                            raise self._service_unavailable() from exc
                        raise DataTransformWorkerError(
                            "TRANSFORM_WORKER_FAILED",
                            "Python worker status IPC failed",
                            phase="execution",
                            execution_started=True,
                        ) from exc
                    kind = event.get("event")
                    if kind == "ready":
                        baseline_threads = await self._settle_thread_baseline(worker)
                        await asyncio.to_thread(
                            write_json_frame,
                            control_stream,
                            {"command": "start"},
                            limit=MAX_CONTROL_BYTES,
                        )
                        status_task = loop.run_in_executor(
                            ipc_executor,
                            functools.partial(
                                read_json_frame,
                                status_stream,
                                limit=MAX_STATUS_BYTES,
                            ),
                        )
                    elif kind == "started":
                        execution_started_at = time.monotonic()
                        status_task = loop.run_in_executor(
                            ipc_executor,
                            functools.partial(
                                read_json_frame,
                                status_stream,
                                limit=MAX_STATUS_BYTES,
                            ),
                        )
                    elif kind == "error":
                        await process.wait()
                        output_failure = await self._wait_for_output_failure(output_tasks)
                        if isinstance(output_failure, DataTransformWorkerError):
                            raise output_failure
                        started = execution_started_at is not None or bool(event.get("started"))
                        code = str(event.get("code") or "TRANSFORM_WORKER_FAILED")
                        child_phase = str(event.get("phase") or "")
                        phase = (
                            "execution"
                            if started
                            else "code_validation"
                            if child_phase == "code_validation"
                            and code == "TRANSFORM_PYTHON_INVALID"
                            else "preflight"
                        )
                        if started and code == "TRANSFORM_SERVICE_UNAVAILABLE":
                            code = "TRANSFORM_WORKER_FAILED"
                        raise DataTransformWorkerError(
                            code,
                            str(event.get("message") or "Python worker failed"),
                            phase=phase,
                            execution_started=started,
                        )
                    elif kind == "done":
                        await process.wait()
                        if process.returncode != 0:
                            raise DataTransformWorkerError(
                                "TRANSFORM_WORKER_FAILED",
                                "Python worker exited without a trusted result",
                                phase="execution",
                                execution_started=True,
                            )
                        try:
                            raw_outputs = await asyncio.gather(*output_tasks)
                        except DataTransformWorkerError:
                            raise
                        except Exception as exc:
                            raise DataTransformWorkerError(
                                "TRANSFORM_WORKER_FAILED",
                                "Python worker returned invalid Arrow IPC",
                                phase="execution",
                                execution_started=True,
                            ) from exc
                        capture_results = await asyncio.gather(
                            *capture_tasks,
                            return_exceptions=True,
                        )
                        if any(isinstance(item, BaseException) for item in capture_results):
                            raise DataTransformWorkerError(
                                "TRANSFORM_RESOURCE_LIMIT",
                                "Python worker diagnostics exceeded its capture budget",
                                phase="execution",
                                execution_started=True,
                            )
                        execution_ms = max(0, int(event.get("execution_ms") or 0))
                        return tuple(
                            ArrowTransformOutput(
                                receipt=DataTransformWorkerReceipt(
                                    row_count=rows,
                                    arrow_nbytes=nbytes,
                                    execution_ms=execution_ms,
                                ),
                                schema=schema,
                                batches=batches,
                                payload_name=None if query_mode else declaration.payload_name,
                            )
                            for declaration, (schema, batches, rows, nbytes) in zip(
                                outputs,
                                raw_outputs,
                                strict=True,
                            )
                        )
                    else:
                        await self._stop_process(process)
                        raise DataTransformWorkerError(
                            "TRANSFORM_WORKER_FAILED",
                            "Python worker emitted an unknown status event",
                            phase="execution" if execution_started_at else "preflight",
                            execution_started=execution_started_at is not None,
                        )

                output_failure = self._completed_output_failure(output_tasks)
                if isinstance(output_failure, DataTransformWorkerError):
                    await self._stop_process(process)
                    raise output_failure
                failed_capture = next(
                    (
                        task
                        for task in capture_tasks
                        if task.done() and not task.cancelled() and task.exception() is not None
                    ),
                    None,
                )
                if failed_capture is not None:
                    await self._stop_process(process)
                    failure = failed_capture.exception()
                    raise DataTransformWorkerError(
                        "TRANSFORM_RESOURCE_LIMIT",
                        "Python worker diagnostics exceeded its capture budget",
                        phase="execution",
                        execution_started=execution_started_at is not None,
                    ) from failure

                now = time.monotonic()
                if now > deadline or (
                    execution_started_at is not None
                    and now - execution_started_at > self._policy.wall_seconds
                ):
                    await self._stop_process(process)
                    raise DataTransformWorkerError(
                        "TRANSFORM_TIMEOUT",
                        "Python worker exceeded its wall-clock deadline",
                        phase="execution" if execution_started_at else "preflight",
                        execution_started=execution_started_at is not None,
                    )
                violation = self._resource_violation(worker, call_root, baseline_threads)
                if violation is not None:
                    await self._stop_process(process)
                    raise DataTransformWorkerError(
                        "TRANSFORM_RESOURCE_LIMIT",
                        violation,
                        phase="execution" if execution_started_at else "preflight",
                        execution_started=execution_started_at is not None,
                    )
                if process.returncode is not None:
                    # The status pipe owns terminal classification. A child can write its
                    # final frame and exit before the reader thread receives that frame; the
                    # next loop iteration consumes it. A silent exit closes the pipe and is
                    # mapped by the existing IPC error path, while the request deadline still
                    # bounds a genuinely stuck reader.
                    continue
        except asyncio.CancelledError as exc:
            _annotate_cancellation(
                exc,
                DataTransformWorkerError(
                    "TRANSFORM_TIMEOUT",
                    "The Python/Arrow request was cancelled",
                    phase="execution" if execution_started_at else "preflight",
                    execution_started=execution_started_at is not None,
                ),
            )
            raise
        finally:
            if not status_task.done():
                status_task.cancel()
            await asyncio.gather(status_task, return_exceptions=True)

    async def _wait_for_output_failure(
        self,
        output_tasks: tuple[
            asyncio.Future[tuple[pa.Schema, tuple[pa.RecordBatch, ...], int, int]], ...
        ],
    ) -> BaseException | None:
        if output_tasks:
            await asyncio.wait(output_tasks, timeout=self._policy.cleanup_seconds)
        return self._completed_output_failure(output_tasks)

    @staticmethod
    def _completed_output_failure(
        output_tasks: tuple[
            asyncio.Future[tuple[pa.Schema, tuple[pa.RecordBatch, ...], int, int]], ...
        ],
    ) -> BaseException | None:
        failed = next(
            (
                task
                for task in output_tasks
                if task.done() and not task.cancelled() and task.exception() is not None
            ),
            None,
        )
        return failed.exception() if failed is not None else None

    async def _settle_thread_baseline(self, worker: psutil.Process) -> int:
        return await self._process_supervisor.settle_thread_baseline(worker)

    def _resource_violation(
        self,
        worker: psutil.Process,
        call_root: Path,
        baseline_threads: int,
    ) -> str | None:
        violation, _observations = self._process_supervisor.resource_violation(
            worker,
            call_root,
            WorkerProcessBudget(
                wall_seconds=self._policy.wall_seconds,
                cpu_seconds=self._policy.cpu_seconds,
                rss_bytes=self._policy.rss_bytes,
                scratch_bytes=self._policy.scratch_bytes,
                threads=self._policy.threads,
                open_file_descriptors=self._policy.open_file_descriptors,
                stdout_bytes=self._policy.capture_bytes,
                stderr_bytes=self._policy.capture_bytes,
                cleanup_seconds=self._policy.cleanup_seconds,
            ),
            baseline_threads=baseline_threads,
        )
        if violation is None:
            return None
        return f"Python {violation}"

    def _read_output(
        self,
        descriptor: int,
        aggregate: _AggregateOutputBudget,
    ) -> tuple[pa.Schema, tuple[pa.RecordBatch, ...], int, int]:
        return read_arrow_output(descriptor, aggregate, policy=self._policy)

    async def _read_bounded(self, stream: asyncio.StreamReader, limit: int) -> bytes:
        return await self._process_supervisor.read_bounded(stream, limit)

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        await self._process_supervisor.stop_process(
            process,
            cleanup_seconds=self._policy.cleanup_seconds,
        )

    @staticmethod
    def _directory_size(root: Path) -> int:
        return directory_size(root)


__all__ = ["SubprocessPythonArrowDataTransformWorker", "read_arrow_output"]
