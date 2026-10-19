"""Parent adapter for one process-private, read-only IfcOpenShell query."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pyarrow as pa

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.infrastructure.worker_runtime import (
    DarwinSeatbeltSandbox,
    HostWorkerCapacity,
    MAX_CONTROL_BYTES,
    MAX_STATUS_BYTES,
    WorkerCapacityBusy,
    WorkerCapacityUnavailable,
    WorkerLaunchSpec,
    WorkerProcessBudget,
    WorkerProcessSupervisor,
    WorkerProtocolError,
    WorkerSupervisorError,
    read_json_frame,
    write_json_frame,
)
from CAD2BIM_backend.ports.arrow_inputs import ArrowInput
from CAD2BIM_backend.domain.data_transform import (
    DataTransformWorkerError, DataTransformWorkerReceipt, PythonArrowWorkerPolicy,
    PythonArrowWorkerRequest,
)
from CAD2BIM_backend.ports.data_transform import ArrowTransformOutput
from CAD2BIM_backend.infrastructure.data_transform.python_arrow_worker import (
    _AggregateOutputBudget, read_arrow_output,
)
from CAD2BIM_backend.ports.ifc_query_worker import (
    IfcQueryWorkerError,
    IfcQueryWorkerRequest,
    IfcQueryWorkerResult,
)


IFC_QUERY_BUDGET = WorkerProcessBudget(
    # A representative 241,857,160-byte IFC characterization peaked at
    # 3,242,426,368 RSS bytes and 21.42 CPU seconds while parsing, querying,
    # and serializing the private IFC twice for mutation attestation.  Keep a
    # bounded child margin within the shared IFC query execution lease.
    wall_seconds=45.0,
    cpu_seconds=45.0,
    rss_bytes=5 * 1024**3,
    scratch_bytes=128 * 1024**2,
    threads=4,
    open_file_descriptors=64,
    stdout_bytes=16 * 1024,
    stderr_bytes=16 * 1024,
    cleanup_seconds=1.0,
)
_MAX_INPUT_COUNT = 16
_MAX_INPUT_ROWS = 5_000_000
_MAX_INPUT_ARROW_BYTES = 256 * 1024**2
_MAX_LOG_BYTES = 8 * 1024
_TRANSFORM_POLICY = PythonArrowWorkerPolicy()
IFC_TRANSFORM_BUDGET = replace(
    IFC_QUERY_BUDGET,
    rss_bytes=6 * 1024**3,
    wall_seconds=_TRANSFORM_POLICY.wall_seconds,
    cpu_seconds=_TRANSFORM_POLICY.cpu_seconds,
    scratch_bytes=_TRANSFORM_POLICY.scratch_bytes,
    threads=_TRANSFORM_POLICY.threads,
)
_INPUT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_ISOLATED_MODULE_BOOTSTRAP = (
    "import runpy, sys; "
    "sys.path.insert(0, sys.argv[1]); "
    "runpy.run_module(sys.argv[2], run_name='__main__')"
)
_READINESS_IFC = b"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [ReferenceView_V1.2]'),'2;1');
FILE_NAME('readiness.ifc','1970-01-01T00:00:00Z',(),(),'CAD2BIM','CAD2BIM','');
FILE_SCHEMA(('IFC4X3_ADD2'));
ENDSEC;
DATA;
#1=IFCPROJECT('0fA5xY2rjB9eNH2dFsKQv8',$,'Readiness',$,$,$,$,$,$);
ENDSEC;
END-ISO-10303-21;
"""
_READINESS_CODE = (
    "def query(ctx):\n    return {'projects': len(ctx.model().by_type('IfcProject'))}\n"
)


class IfcOpenShellQueryWorker:
    """Execute ``query(ctx)`` in a fresh Seatbelt-confined Python process."""

    def __init__(
        self,
        *,
        python_executable: str | None = None,
        supervisor: WorkerProcessSupervisor | None = None,
        sandbox: DarwinSeatbeltSandbox | None = None,
        capacity: HostWorkerCapacity | None = None,
        capacity_root: str | Path | None = None,
    ) -> None:
        executable = Path(python_executable or sys.executable)
        if (
            not executable.is_absolute()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise ValueError("IFC query Python executable must be an absolute executable file")
        self._python = str(executable)
        self._transform_policy = _TRANSFORM_POLICY
        runtime_directory = Path(__file__).resolve().parent
        worker_runtime_directory = runtime_directory.parent / "worker_runtime"
        ifc_directory = runtime_directory.parent / "ifc"
        source_root = runtime_directory.parents[2].resolve(strict=True)
        self._source_root = source_root
        trusted_files = tuple(
            path.resolve()
            for path in (
                source_root / "CAD2BIM_backend" / "__init__.py",
                source_root / "CAD2BIM_backend" / "domain" / "__init__.py",
                source_root / "CAD2BIM_backend" / "domain" / "canonical.py",
                source_root / "CAD2BIM_backend" / "domain" / "run_events.py",
                *(source_root / "CAD2BIM_backend" / "domain" / name for name in (
                    "ifc_model.py", "ifc_geometry_basis.py", "ifc_spatial_structure.py",
                    "geometry_frame.py", "geometry_interface.py", "geometry_resource.py",
                    "geometry_runtime.py", "geometry_inspection.py", "product_family.py", "product_realization.py",
                    "product_realization_host.py", "resource_scope.py", "validation_diagnostics.py",
                    "version_usage.py",
                )),
                source_root / "CAD2BIM_backend" / "infrastructure" / "__init__.py",
                *(ifc_directory / name for name in (
                    "__init__.py", "codec.py", "geometry_basis.py", "placement.py", "product_realization_host.py",
                )),
                runtime_directory / "__init__.py",
                runtime_directory / "child.py",
                runtime_directory / "readonly_model.py",
                *(runtime_directory.parent / "data_transform" / name for name in (
                    "__init__.py", "capabilities.py", "python_arrow_child.py", "worker_protocol.py",
                )),
                *(source_root / "CAD2BIM_backend" / "ports" / name for name in (
                    "__init__.py", "arrow_inputs.py", "ifc_query_worker.py",
                )),
                source_root / "CAD2BIM_backend" / "domain" / "runtime_execution.py",
                worker_runtime_directory / "__init__.py",
                worker_runtime_directory / "protocol.py",
                worker_runtime_directory / "python_user_code.py",
            )
            if path.is_file()
        )
        trusted_directories = tuple(
            dict.fromkeys(
                (source_root,)
                + tuple(
                    directory
                    for file_path in trusted_files
                    for directory in (file_path.parent, file_path.parent.parent)
                )
            )
        )
        self._sandbox = sandbox or DarwinSeatbeltSandbox(
            self._python,
            trusted_source_files=trusted_files,
            trusted_source_directories=trusted_directories,
            trusted_source_metadata_root=source_root,
            allow_scratch_writes=False,
        )
        self._supervisor = supervisor or WorkerProcessSupervisor()
        self._capacity = capacity or HostWorkerCapacity(
            max_concurrent_workers=1,
            capacity_root=capacity_root,
        )
        self._closed = False

    def execution_scope(self, policy, *, deadline: float | None = None):
        return self._capacity.execution_scope(policy, deadline=deadline)

    async def verify_ready(self) -> None:
        """Exercise the real Seatbelt/FD/IfcOpenShell child without durable effects."""

        if self._closed:
            raise _unavailable("IFC query worker is closed")
        try:
            result = await self.execute_query(
                IfcQueryWorkerRequest(
                    code=_READINESS_CODE,
                    ifc_bytes=_READINESS_IFC,
                    expected_ifc_sha256=hashlib.sha256(_READINESS_IFC).hexdigest(),
                    max_result_bytes=64,
                )
            )
        except IfcQueryWorkerError as exc:
            raise IfcQueryWorkerError(
                "IFC_QUERY_SERVICE_UNAVAILABLE",
                "IFC query worker readiness probe failed",
                phase="runtime",
                execution_started=exc.execution_started,
                retryable=not exc.execution_started,
            ) from exc
        if result.result != {"projects": 1} or result.logs:
            raise IfcQueryWorkerError(
                "IFC_QUERY_SERVICE_UNAVAILABLE",
                "IFC query worker readiness probe returned an invalid result",
                phase="runtime",
                execution_started=True,
            )

    async def execute_query(
        self,
        request: IfcQueryWorkerRequest,
        inputs: tuple[ArrowInput, ...] = (),
        *, deadline: float | None = None,
    ) -> IfcQueryWorkerResult:
        from CAD2BIM_backend.domain.runtime_execution import IFC_QUERY_EXECUTION

        async with self._capacity.execution_scope(IFC_QUERY_EXECUTION, deadline=deadline):
            return await self._execute_query(request, inputs)

    async def _execute_query(
        self,
        request: IfcQueryWorkerRequest | PythonArrowWorkerRequest,
        inputs: tuple[ArrowInput, ...],
        *, ifc_bytes: bytes | None = None, expected_ifc_sha256: str | None = None,
    ) -> IfcQueryWorkerResult | tuple[ArrowTransformOutput, ...]:
        derive = isinstance(request, PythonArrowWorkerRequest)
        budget = IFC_TRANSFORM_BUDGET if derive else IFC_QUERY_BUDGET
        source_bytes = ifc_bytes if derive else request.ifc_bytes
        source_sha256 = expected_ifc_sha256 if derive else request.expected_ifc_sha256
        started = time.monotonic()
        if self._closed:
            raise IfcQueryWorkerError(
                "IFC_QUERY_SERVICE_UNAVAILABLE",
                "IFC query worker is closed",
                phase="runtime",
                execution_started=False,
                retryable=True,
            )
        self._validate_inputs(inputs, derive=derive)
        capacity_fd: int | None = None
        descriptors: list[int] = []
        host_descriptors: list[int] = []
        author_started = not derive
        root: Path | None = None
        try:
            lease = await self._capacity.admit_current_execution()
            capacity_fd = lease.descriptor
            deadline = lease.execution_deadline
            assert capacity_fd is not None and deadline is not None
            root = Path(tempfile.mkdtemp(prefix="cad2bim-ifc-query-"))
            os.chmod(root, 0o700)
            ifc_path = root / "source.ifc"
            ifc_path.write_bytes(source_bytes)
            staged_inputs = await asyncio.to_thread(self._stage_inputs, root, inputs, derive=derive)

            ifc_fd = _open_read_only(ifc_path)
            descriptors.append(ifc_fd)
            input_fds = []
            for _item, path in staged_inputs:
                descriptor = _open_read_only(path)
                descriptors.append(descriptor)
                input_fds.append(descriptor)
            # Only inherited read-only descriptors cross into the child.  Once
            # opened, remove the staging names so even a future library-level
            # path bypass cannot reopen the exact IFC or Arrow inputs.
            ifc_path.unlink()
            for _item, path in staged_inputs:
                path.unlink()
            control_path = root / "control.bin"
            if derive:
                ready_read, ready_write = os.pipe()
                start_read, start_write = os.pipe()
                host_descriptors.extend((ready_read, start_write))
                descriptors.extend((ready_write, start_read))
            output_fds = []
            if derive:
                for i, _ in enumerate(request.outputs):
                    descriptor = _open_write_once(root / f"output-{i}.arrow")
                    descriptors.append(descriptor)
                    output_fds.append(descriptor)
            control_write = _open_write_once(control_path)
            with os.fdopen(control_write, "wb", buffering=0) as stream:
                write_json_frame(
                    stream,
                    {
                        "code": request.code,
                        "expected_ifc_sha256": source_sha256,
                        "mode": "transform" if derive else "query",
                        "parameters": ([asdict(item) for item in request.parameters] if derive else []),
                        "outputs": ([dict(asdict(item), fd=fd) for item, fd in zip(request.outputs, output_fds, strict=True)] if derive else []),
                        "ifc_fd": ifc_fd,
                        "inputs": [
                            {
                                "input_name": item.input_name,
                                "fd": descriptor,
                                "input_format": item.input_format,
                                "json_kind": item.json_kind,
                            }
                            for (item, _path), descriptor in zip(
                                staged_inputs, input_fds, strict=True
                            )
                        ],
                        "max_result_bytes": 0 if derive else request.max_result_bytes,
                        "logs_limit": _MAX_LOG_BYTES,
                        "policy": {
                            "cpu_seconds": budget.cpu_seconds,
                            "open_file_descriptors": budget.open_file_descriptors,
                            "scratch_bytes": budget.scratch_bytes,
                            "threads": budget.threads,
                            "rss_bytes": budget.rss_bytes,
                        },
                    },
                    limit=MAX_CONTROL_BYTES,
                )
            control_fd = _open_read_only(control_path)
            result_fd = _open_write_once(root / "result.json")
            terminal_fd = _open_write_once(root / "terminal.bin")
            descriptors.extend((control_fd, result_fd, terminal_fd))
            environment = _environment(root, budget=budget)
            environment.update(
                {
                    "CAD2BIM_IFC_QUERY_CONTROL_FD": str(control_fd),
                    "CAD2BIM_IFC_QUERY_RESULT_FD": str(result_fd),
                    "CAD2BIM_IFC_QUERY_TERMINAL_FD": str(terminal_fd),
                }
            )
            if derive:
                environment.update({"CAD2BIM_IFC_READY_FD": str(ready_write),
                                    "CAD2BIM_IFC_START_FD": str(start_read)})

            async def initialize(control):
                nonlocal author_started
                # These capabilities now belong only to the trusted child.
                for descriptor in (ready_write, start_read):
                    os.close(descriptor)
                    descriptors.remove(descriptor)
                loop = asyncio.get_running_loop()
                ready = loop.create_future()
                buffer = bytearray()

                def consume_ready():
                    if ready.done():
                        return
                    try:
                        chunk = os.read(ready_read, 32)
                        if not chunk:
                            child_status = _read_terminal(root / "terminal.bin")
                            raise _child_error(child_status)
                        buffer.extend(chunk)
                        if len(buffer) >= 6:
                            if bytes(buffer) != b"READY\n":
                                raise WorkerProtocolError("IFC dependency readiness is invalid")
                            ready.set_result(None)
                    except Exception as exc:
                        ready.set_exception(exc)

                loop.add_reader(ready_read, consume_ready)
                try:
                    async with asyncio.timeout_at(deadline):
                        await ready
                        control.accept_thread_baseline()
                        os.write(start_write, b"START\n")
                        author_started = True
                finally:
                    loop.remove_reader(ready_read)
            terminal = await self._supervisor.run(
                WorkerLaunchSpec(
                    command=self._sandbox.command(
                        scratch=root,
                        child_args=(
                            "-I",
                            "-B",
                            "-c",
                            _ISOLATED_MODULE_BOOTSTRAP,
                            str(self._source_root),
                            "CAD2BIM_backend.infrastructure.ifc_query.child",
                        ),
                    ),
                    cwd=root,
                    env=environment,
                    pass_fds=tuple(descriptors),
                    capacity_fds=(capacity_fd,),
                    scratch=root,
                ),
                budget=budget,
                deadline=deadline,
                **({"initialize": initialize} if derive else {}),
            )
            _close_all(descriptors)
            descriptors.clear()
            child_terminal = _read_terminal(root / "terminal.bin")
            if terminal.returncode != 0 or child_terminal.get("status") != "succeeded":
                raise _child_error(child_terminal)
            if derive:
                self._validate_attestation(child_terminal, source_sha256)
                aggregate = _AggregateOutputBudget(self._transform_policy)
                outputs = []
                for i, declaration in enumerate(request.outputs):
                    try:
                        schema, batches, rows, nbytes = await asyncio.to_thread(
                            read_arrow_output, _open_read_only(root / f"output-{i}.arrow"),
                            aggregate, policy=self._transform_policy,
                        )
                    except DataTransformWorkerError:
                        raise
                    except RuntimeError as exc:
                        raise DataTransformWorkerError(
                            "TRANSFORM_OUTPUT_INVALID", "IFC worker returned invalid Arrow IPC",
                            phase="output_validation", execution_started=True,
                        ) from exc
                    outputs.append(ArrowTransformOutput(
                        receipt=DataTransformWorkerReceipt(rows, nbytes, int((time.monotonic()-started)*1000)),
                        schema=schema, batches=batches, payload_name=declaration.payload_name,
                    ))
                return tuple(outputs)
            result_json = _read_result(root / "result.json", request.max_result_bytes)
            try:
                result = json.loads(result_json)
            except json.JSONDecodeError as exc:
                raise IfcQueryWorkerError(
                    "IFC_QUERY_WORKER_FAILED",
                    "IFC query worker returned invalid result JSON",
                    phase="runtime",
                    execution_started=True,
                ) from exc
            # Re-canonicalize at the trust boundary; the child cannot choose a
            # merely equivalent non-canonical representation.
            if canonical_json(result) != result_json:
                raise IfcQueryWorkerError(
                    "IFC_QUERY_WORKER_FAILED",
                    "IFC query worker returned non-canonical result JSON",
                    phase="runtime",
                    execution_started=True,
                )
            if str(child_terminal.get("source_ifc_sha256")) != request.expected_ifc_sha256:
                raise IfcQueryWorkerError(
                    "IFC_QUERY_WORKER_FAILED",
                    "IFC query worker did not attest the exact IFC input",
                    phase="runtime",
                    execution_started=True,
                )
            private_before = child_terminal.get("private_ifc_sha256_before")
            private_after = child_terminal.get("private_ifc_sha256_after")
            operation_count = child_terminal.get("transaction_operation_count")
            if (
                not isinstance(private_before, str)
                or not isinstance(private_after, str)
                or private_before != private_after
                or operation_count != 0
            ):
                raise IfcQueryWorkerError(
                    "IFC_QUERY_MUTATION_DETECTED",
                    "IFC query changed its process-private model copy",
                    phase="execution",
                    execution_started=True,
                )
            result_bytes = len(result_json.encode("utf-8"))
            if child_terminal.get("result_bytes") != result_bytes:
                raise IfcQueryWorkerError(
                    "IFC_QUERY_WORKER_FAILED",
                    "IFC query worker returned inconsistent result metadata",
                    phase="runtime",
                    execution_started=True,
                )
            logs = child_terminal.get("logs")
            if (
                not isinstance(logs, list)
                or any(not isinstance(item, str) for item in logs)
                or sum(len(item.encode("utf-8")) for item in logs) > _MAX_LOG_BYTES
            ):
                raise IfcQueryWorkerError(
                    "IFC_QUERY_WORKER_FAILED",
                    "IFC query worker returned invalid bounded diagnostics",
                    phase="runtime",
                    execution_started=True,
                )
            return IfcQueryWorkerResult(
                result=result,
                result_json=result_json,
                result_bytes=result_bytes,
                ifc_sha256=request.expected_ifc_sha256,
                logs=tuple(logs),
            )
        except IfcQueryWorkerError:
            raise
        except TimeoutError as exc:
            raise _unavailable("IFC query worker capacity is busy") from exc
        except (WorkerCapacityBusy, WorkerCapacityUnavailable) as exc:
            raise IfcQueryWorkerError(
                "IFC_QUERY_SERVICE_UNAVAILABLE", _safe_message(str(exc)), phase="runtime",
                execution_started=False, diagnostic=self._capacity.failure_diagnostic(exc),
                retryable=isinstance(exc, WorkerCapacityBusy) and exc.retryable,
            ) from exc
        except WorkerSupervisorError as exc:
            execution_started = exc.execution_started and author_started
            if exc.code == "WORKER_TIMEOUT":
                code, message = "IFC_QUERY_TIMEOUT", "IFC query exceeded its time budget"
            elif exc.code == "WORKER_RESOURCE_LIMIT":
                code, message = "IFC_QUERY_RESOURCE_LIMIT", _safe_message(exc.message)
            else:
                raise _unavailable(exc.message, execution_started=execution_started) from exc
            raise IfcQueryWorkerError(
                code,
                message,
                phase="execution" if execution_started else "runtime",
                execution_started=execution_started,
                diagnostic=exc.diagnostic(),
            ) from exc
        except (OSError, WorkerProtocolError, ValueError, TypeError) as exc:
            raise _unavailable("IFC query worker infrastructure is unavailable") from exc
        finally:
            _close_all(descriptors)
            _close_all(host_descriptors)
            if root is not None:
                await asyncio.to_thread(shutil.rmtree, root, True)

    async def execute_transform(
        self, request: PythonArrowWorkerRequest, inputs: tuple[ArrowInput, ...] = (),
        *, ifc_bytes: bytes, expected_ifc_sha256: str, deadline: float | None = None,
    ) -> tuple[ArrowTransformOutput, ...]:
        from CAD2BIM_backend.domain.runtime_execution import DATA_PYTHON_EXECUTION
        from CAD2BIM_backend.ports.ifc_query_worker import MAX_IFC_INPUT_BYTES
        try:
            if (not isinstance(ifc_bytes, bytes) or not ifc_bytes or
                len(ifc_bytes) > MAX_IFC_INPUT_BYTES or
                hashlib.sha256(ifc_bytes).hexdigest() != expected_ifc_sha256):
                raise IfcQueryWorkerError("IFC_QUERY_EXACT_INPUT_INVALID", "Exact IFC input is invalid",
                                          phase="input_validation", execution_started=False)
            if set(request.output_names).intersection(item.input_name for item in inputs):
                raise IfcQueryWorkerError("IFC_QUERY_INPUT_INVALID", "Output names collide with inputs",
                                          phase="input_validation", execution_started=False)
            async with self._capacity.execution_scope(DATA_PYTHON_EXECUTION, deadline=deadline):
                return await self._execute_query(request, inputs, ifc_bytes=ifc_bytes,
                                                 expected_ifc_sha256=expected_ifc_sha256)
        except IfcQueryWorkerError as exc:
            failure = DataTransformWorkerError(
                exc.code, exc.message, phase=exc.phase, execution_started=exc.execution_started,
                retryable=exc.retryable, diagnostic=exc.diagnostic,
            )
            raise failure from exc

    @staticmethod
    def _validate_attestation(terminal: dict[str, Any], source_sha256: str) -> None:
        before = terminal.get("private_ifc_sha256_before")
        after = terminal.get("private_ifc_sha256_after")
        if (terminal.get("source_ifc_sha256") != source_sha256 or
            not isinstance(before, str) or before != after or
            terminal.get("transaction_operation_count") != 0):
            raise IfcQueryWorkerError("IFC_QUERY_MUTATION_DETECTED", "Private IFC audit is invalid",
                                      phase="mutation_audit", execution_started=True)

    async def close(self) -> None:
        self._closed = True

    @staticmethod
    def _validate_inputs(inputs: tuple[ArrowInput, ...], *, derive: bool = False) -> None:
        if len(inputs) > (24 if derive else _MAX_INPUT_COUNT):
            raise IfcQueryWorkerError(
                "IFC_QUERY_INPUT_BUDGET_EXCEEDED",
                "IFC query accepts at most 16 exact inputs",
                phase="input_validation",
                execution_started=False,
            )
        names = [item.input_name for item in inputs]
        if len(names) != len(set(names)) or any(
            _INPUT_NAME.fullmatch(name) is None for name in names
        ):
            raise IfcQueryWorkerError(
                "IFC_QUERY_INPUT_INVALID",
                "IFC query input names are invalid or duplicated",
                phase="input_validation",
                execution_started=False,
            )
        if any(item.input_format not in {"parquet", "inline", "json"} for item in inputs):
            raise IfcQueryWorkerError(
                "IFC_QUERY_INPUT_INVALID",
                "IFC query input formats are invalid",
                phase="input_validation",
                execution_started=False,
            )
        if any(not isinstance(item.reader, pa.RecordBatchReader) for item in inputs):
            raise TypeError("ArrowInput.reader must be a RecordBatchReader")

    @staticmethod
    def _stage_inputs(
        root: Path,
        inputs: tuple[ArrowInput, ...],
        *, derive: bool = False,
    ) -> tuple[tuple[ArrowInput, Path], ...]:
        total_rows = 0
        total_arrow_bytes = 0
        staged: list[tuple[ArrowInput, Path]] = []
        for ordinal, item in enumerate(inputs):
            path = root / f"input-{ordinal:02d}.arrow"
            descriptor = _open_write_once(path)
            with os.fdopen(descriptor, "wb", buffering=0) as raw:
                with pa.ipc.new_stream(raw, item.reader.schema) as writer:
                    for batch in item.reader:
                        total_rows += batch.num_rows
                        total_arrow_bytes += batch.nbytes
                        if (
                            total_rows > (_TRANSFORM_POLICY.input_row_limit if derive else _MAX_INPUT_ROWS)
                            or total_arrow_bytes > (_TRANSFORM_POLICY.input_arrow_bytes if derive else _MAX_INPUT_ARROW_BYTES)
                        ):
                            raise IfcQueryWorkerError(
                                "IFC_QUERY_INPUT_BUDGET_EXCEEDED",
                                "IFC query exact inputs exceed their Arrow budget",
                                phase="input_validation",
                                execution_started=False,
                            )
                        writer.write_batch(batch)
            staged.append((item, path))
        return tuple(staged)


def _read_terminal(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb", buffering=0) as stream:
            return read_json_frame(stream, limit=MAX_STATUS_BYTES)
    except (OSError, WorkerProtocolError) as exc:
        raise _unavailable("IFC query worker exited without a trusted terminal", True) from exc


def _read_result(path: Path, limit: int) -> str:
    try:
        payload = path.read_bytes()
        if not payload or len(payload) > limit:
            raise ValueError("invalid result size")
        return payload.decode("utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise IfcQueryWorkerError(
            "IFC_QUERY_WORKER_FAILED",
            "IFC query worker returned an invalid bounded result",
            phase="runtime",
            execution_started=True,
        ) from exc


def _child_error(terminal: dict[str, Any]) -> IfcQueryWorkerError:
    diagnostic = terminal.get("diagnostic")
    return IfcQueryWorkerError(
        str(terminal.get("code", "IFC_QUERY_WORKER_FAILED")),
        _safe_message(str(terminal.get("message", "IFC query worker failed"))),
        phase=str(terminal.get("phase", "execution")),
        execution_started=bool(terminal.get("execution_started", False)),
        diagnostic=diagnostic if isinstance(diagnostic, dict) else None,
        retryable=bool(terminal.get("retryable", False)),
    )


def _unavailable(message: str, execution_started: bool = False) -> IfcQueryWorkerError:
    return IfcQueryWorkerError(
        "IFC_QUERY_SERVICE_UNAVAILABLE",
        _safe_message(message),
        phase="runtime",
        execution_started=execution_started,
        retryable=not execution_started,
    )


def _safe_message(message: str) -> str:
    value = re.sub(r"/(?:Users|home|private|var|tmp)/[^\s:'\"]+", "<host-path>", message)
    return value.encode("utf-8")[:2_000].decode("utf-8", errors="ignore")


def _environment(root: Path, *, budget: WorkerProcessBudget = IFC_QUERY_BUDGET) -> dict[str, str]:
    thread_limit = str(budget.threads)
    return {
        "ARROW_IO_THREADS": thread_limit,
        "BLIS_NUM_THREADS": thread_limit,
        "HOME": str(root),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "MKL_NUM_THREADS": thread_limit,
        "NUMEXPR_NUM_THREADS": thread_limit,
        "OMP_NUM_THREADS": thread_limit,
        "OPENBLAS_NUM_THREADS": thread_limit,
        "POLARS_MAX_THREADS": thread_limit,
        "PROJ_NETWORK": "OFF",
        "PATH": str(Path(sys.executable).parent),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(root),
        "VECLIB_MAXIMUM_THREADS": thread_limit,
    }


def _open_read_only(path: Path) -> int:
    return os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )


def _open_write_once(path: Path) -> int:
    return os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )


def _close_all(descriptors: list[int]) -> None:
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError:
            pass
