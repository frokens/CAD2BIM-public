"""Private child entry point inside the Direct OS-enforced local sandbox.

The AST, import guard, audit hook, and resource limits in this module are only
defense in depth.  The parent launcher is the no-host-path, no-network, and
no-subprocess security boundary.  Runner/remote execution still requires OCI.
"""

from __future__ import annotations

import json
import math
import os
import resource
import time
from types import CodeType
from typing import Any, BinaryIO

import pyarrow as pa

from CAD2BIM_backend.infrastructure.data_transform.capabilities import (
    PYTHON_ARROW_APPROVED_LIBRARIES,
    PYTHON_ARROW_SAFE_STDLIB,
)
from CAD2BIM_backend.infrastructure.data_transform.worker_protocol import (
    MAX_CONTROL_BYTES,
    MAX_STATUS_BYTES,
    open_binary_fd,
    read_json_frame,
    write_json_frame,
)
from CAD2BIM_backend.infrastructure.worker_runtime.python_user_code import (
    DEFAULT_BLOCKED_IO_ATTRIBUTES,
    PythonUserCodePreflightError,
    PythonUserCodeValidationError,
    RestrictedImportGuard,
    compile_entry,
    diagnostic_json,
    install_readonly_audit_hook,
    runtime_code_diagnostic,
    safe_builtins,
)


_ALLOWED_IMPORT_ROOTS = frozenset((*PYTHON_ARROW_APPROVED_LIBRARIES, *PYTHON_ARROW_SAFE_STDLIB))
_PreflightError = PythonUserCodePreflightError
_CodeValidationError = PythonUserCodeValidationError
_diagnostic_json = diagnostic_json
_runtime_code_diagnostic = runtime_code_diagnostic


class _ExecutionError(RuntimeError):
    pass


class _QueryResultError(RuntimeError):
    pass


def _compile_source(code: str, entry_name: str) -> CodeType:
    if entry_name not in {"transform", "query"}:
        raise _PreflightError("Python worker mode is invalid")
    return compile_entry(
        code,
        entry_name,
        allowed_import_roots=_ALLOWED_IMPORT_ROOTS,
        blocked_io_attributes=DEFAULT_BLOCKED_IO_ATTRIBUTES,
        allowed_context_io_attributes=("write_table", "write_json"),
    )


class _ImportGuard(RestrictedImportGuard):
    def __init__(self, policy: dict[str, Any]) -> None:
        super().__init__(
            allowed_import_roots=_ALLOWED_IMPORT_ROOTS,
            policy=policy,
            worker_label="Python/Arrow worker",
        )


_install_audit_hook = install_readonly_audit_hook


class _TransformContext:
    __slots__ = (
        "_inputs",
        "_input_formats",
        "_json_inputs",
        "_logs_bytes",
        "_logs_limit",
        "_outputs",
        "_parameters",
        "_submitted",
    )

    def __init__(
        self,
        *,
        inputs: dict[str, pa.Table],
        input_formats: dict[str, str],
        json_inputs: dict[str, Any],
        parameters: dict[str, Any],
        outputs: dict[str, tuple[BinaryIO, str, str | None]],
        logs_limit: int,
    ) -> None:
        self._inputs = inputs
        self._input_formats = input_formats
        self._json_inputs = json_inputs
        self._parameters = parameters
        self._outputs = outputs
        self._submitted: set[str] = set()
        self._logs_bytes = 0
        self._logs_limit = logs_limit

    @property
    def submitted(self) -> frozenset[str]:
        return frozenset(self._submitted)

    def table(self, name: str) -> pa.Table:
        try:
            if self._input_formats[name] == "json":
                raise TypeError(f"JSON input {name!r} must be read with ctx.json")
            return self._inputs[name]
        except KeyError as exc:
            raise KeyError(f"Arrow input {name!r} was not declared") from exc

    def batches(self, name: str) -> pa.RecordBatchReader:
        table = self.table(name)
        return pa.RecordBatchReader.from_batches(table.schema, table.to_batches())

    def param(self, name: str) -> Any:
        try:
            return self._parameters[name]
        except KeyError as exc:
            raise KeyError(f"parameter {name!r} was not declared") from exc

    def write_table(
        self,
        name: str,
        value: pa.Table | pa.RecordBatch | pa.RecordBatchReader,
    ) -> None:
        if name not in self._outputs:
            raise ValueError(f"Arrow output {name!r} was not declared")
        if name in self._submitted:
            raise ValueError(f"Arrow output {name!r} was submitted more than once")
        stream, output_format, _json_kind = self._outputs[name]
        if output_format != "parquet":
            raise TypeError(f"JSON output {name!r} must be submitted with ctx.write_json")
        if not isinstance(value, (pa.Table, pa.RecordBatch, pa.RecordBatchReader)):
            raise TypeError(
                "ctx.write_table accepts only pyarrow.Table, RecordBatch, or RecordBatchReader"
            )
        if isinstance(value, pa.Table):
            schema = value.schema
            batches = value.to_batches()
        elif isinstance(value, pa.RecordBatch):
            schema = value.schema
            batches = (value,)
        else:
            schema = value.schema
            batches = value
        with pa.ipc.new_stream(stream, schema) as writer:
            for batch in batches:
                writer.write_batch(batch)
        self._submitted.add(name)

    def log(self, message: str) -> None:
        if not isinstance(message, str):
            raise TypeError("ctx.log message must be text")
        size = len(message.encode("utf-8"))
        if self._logs_bytes + size > self._logs_limit:
            raise ValueError("ctx.log exceeded the bounded diagnostics budget")
        self._logs_bytes += size

    def json(self, name: str) -> Any:
        try:
            return self._json_inputs[name]
        except KeyError as exc:
            if name in self._inputs:
                raise TypeError(f"Arrow input {name!r} must be read with ctx.table") from exc
            raise KeyError(f"JSON input {name!r} was not declared") from exc

    def write_json(self, name: str, value: Any) -> None:
        if name not in self._outputs:
            raise ValueError(f"JSON output {name!r} was not declared")
        if name in self._submitted:
            raise ValueError(f"JSON output {name!r} was submitted more than once")
        stream, output_format, json_kind = self._outputs[name]
        if output_format != "json":
            raise TypeError(f"Arrow output {name!r} must be submitted with ctx.write_table")
        if json_kind == "records" and not isinstance(value, list):
            raise TypeError("A records JSON output must be a top-level list")
        _validate_json_value(value)
        if json_kind == "document":
            table = pa.table({"json_value": [_canonical_json(value)]})
        else:
            assert isinstance(value, list)
            table = pa.table(
                {
                    "record_index": pa.array(range(len(value)), type=pa.int64()),
                    "json_value": [_canonical_json(item) for item in value],
                }
            )
        with pa.ipc.new_stream(stream, table.schema) as writer:
            writer.write_table(table)
        self._submitted.add(name)


class _QueryContext:
    """Read-only query surface; it intentionally has no output submission methods."""

    __slots__ = (
        "_inputs",
        "_input_formats",
        "_json_inputs",
        "_logs_bytes",
        "_logs_limit",
        "_parameters",
    )

    def __init__(
        self,
        *,
        inputs: dict[str, pa.Table],
        input_formats: dict[str, str],
        json_inputs: dict[str, Any],
        parameters: dict[str, Any],
        logs_limit: int,
    ) -> None:
        self._inputs = inputs
        self._input_formats = input_formats
        self._json_inputs = json_inputs
        self._parameters = parameters
        self._logs_bytes = 0
        self._logs_limit = logs_limit

    def table(self, name: str) -> pa.Table:
        try:
            if self._input_formats[name] == "json":
                raise TypeError(f"JSON input {name!r} must be read with ctx.json")
            return self._inputs[name]
        except KeyError as exc:
            raise KeyError(f"Arrow input {name!r} was not declared") from exc

    def batches(self, name: str) -> pa.RecordBatchReader:
        table = self.table(name)
        return pa.RecordBatchReader.from_batches(table.schema, table.to_batches())

    def json(self, name: str) -> Any:
        try:
            return self._json_inputs[name]
        except KeyError as exc:
            if name in self._inputs:
                raise TypeError(f"Arrow input {name!r} must be read with ctx.table") from exc
            raise KeyError(f"JSON input {name!r} was not declared") from exc

    def param(self, name: str) -> Any:
        try:
            return self._parameters[name]
        except KeyError as exc:
            raise KeyError(f"parameter {name!r} was not declared") from exc

    def log(self, message: str) -> None:
        if not isinstance(message, str):
            raise TypeError("ctx.log message must be text")
        size = len(message.encode("utf-8"))
        if self._logs_bytes + size > self._logs_limit:
            raise ValueError("ctx.log exceeded the bounded diagnostics budget")
        self._logs_bytes += size


def _write_query_result(
    stream: BinaryIO,
    value: pa.Table | pa.RecordBatch | pa.RecordBatchReader,
) -> None:
    if not isinstance(value, (pa.Table, pa.RecordBatch, pa.RecordBatchReader)):
        raise _QueryResultError(
            "query(ctx) must return pyarrow.Table, RecordBatch, or RecordBatchReader"
        )
    if isinstance(value, pa.Table):
        schema = value.schema
        batches = value.to_batches()
    elif isinstance(value, pa.RecordBatch):
        schema = value.schema
        batches = (value,)
    else:
        schema = value.schema
        batches = value
    with pa.ipc.new_stream(stream, schema) as writer:
        for batch in batches:
            writer.write_batch(batch)


def _apply_resource_limits(policy: dict[str, Any]) -> None:
    resource.setrlimit(
        resource.RLIMIT_CPU,
        (int(policy["cpu_seconds"]), int(policy["cpu_seconds"])),
    )
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(
        resource.RLIMIT_NOFILE,
        (
            int(policy["open_file_descriptors"]),
            int(policy["open_file_descriptors"]),
        ),
    )
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (int(policy["scratch_bytes"]), int(policy["scratch_bytes"])),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > 64:
        raise ValueError("JSON output exceeds the maximum nesting depth of 64")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON output numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        for item in value.values():
            _validate_json_value(item, depth=depth + 1)
        return
    raise TypeError("JSON output contains a non-JSON Python value")


def _decode_json_input(table: pa.Table, json_kind: str) -> Any:
    if json_kind == "document":
        if table.schema.names != ["json_value"] or table.num_rows != 1:
            raise _PreflightError("Document JSON input must be one json_value row")
        value = json.loads(table.column("json_value")[0].as_py())
    elif json_kind == "records":
        if table.schema.names != ["record_index", "json_value"]:
            raise _PreflightError("Records JSON input must contain record_index and json_value")
        indices = table.column("record_index").to_pylist()
        if indices != list(range(table.num_rows)):
            raise _PreflightError("Records JSON input indices must be contiguous from zero")
        value = [json.loads(item) for item in table.column("json_value").to_pylist()]
    else:
        raise _PreflightError("JSON input requires document or records kind")
    _validate_json_value(value)
    return value


def _read_inputs(
    items: list[dict[str, Any]],
) -> tuple[dict[str, pa.Table], dict[str, str], dict[str, Any]]:
    inputs: dict[str, pa.Table] = {}
    input_formats: dict[str, str] = {}
    json_inputs: dict[str, Any] = {}
    for item in items:
        name = str(item["input_name"])
        with open_binary_fd(int(item["fd"]), "rb") as stream:
            inputs[name] = pa.ipc.open_stream(stream).read_all()
        input_format = str(item.get("input_format", "parquet"))
        input_formats[name] = input_format
        if input_format == "json":
            json_inputs[name] = _decode_json_input(inputs[name], str(item.get("json_kind")))
    return inputs, input_formats, json_inputs


def _emit(status: BinaryIO, event: str, **fields: Any) -> None:
    write_json_frame(status, {"event": event, **fields}, limit=MAX_STATUS_BYTES)


def _bounded_error(value: str, limit: int = 16 * 1024) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _run(control_fd: int, status_fd: int) -> int:
    with (
        open_binary_fd(control_fd, "rb") as control,
        open_binary_fd(status_fd, "wb") as status,
    ):
        started = False
        try:
            payload = read_json_frame(control, limit=MAX_CONTROL_BYTES)
            policy = dict(payload["policy"])
            _apply_resource_limits(policy)
            arrow_pool_threads = max(1, int(policy["threads"]) // 2)
            pa.set_cpu_count(arrow_pool_threads)
            pa.set_io_thread_count(arrow_pool_threads)
            mode = str(payload.get("mode", "transform"))
            entry_name = "query" if mode == "query" else "transform"
            if mode not in {"transform", "query"}:
                raise _PreflightError("Python worker mode is invalid")
            code = str(payload["code"])
            compiled = _compile_source(code, entry_name)
            inputs, input_formats, json_inputs = _read_inputs(list(payload["inputs"]))
            output_streams = {
                str(item["payload_name"]): (
                    open_binary_fd(int(item["fd"]), "wb"),
                    str(item["format"]),
                    str(item["json_kind"]) if item.get("json_kind") is not None else None,
                )
                for item in list(payload["outputs"])
            }
            parameters = {str(item["name"]): item["value"] for item in list(payload["parameters"])}
            import_guard = _ImportGuard(policy)
            _install_audit_hook(import_guard)
            controlled_builtins = safe_builtins(import_guard)
            namespace: dict[str, Any] = {
                "__builtins__": controlled_builtins,
                "__name__": f"__cad2bim_{entry_name}__",
            }
            exec(compiled, namespace, namespace)
            entry = namespace.get(entry_name)
            if not callable(entry):
                raise _PreflightError(f"Source did not produce a callable {entry_name}(ctx)")
            if mode == "query":
                context = _QueryContext(
                    inputs=inputs,
                    input_formats=input_formats,
                    json_inputs=json_inputs,
                    parameters=parameters,
                    logs_limit=int(policy["capture_bytes"]),
                )
            else:
                context = _TransformContext(
                    inputs=inputs,
                    input_formats=input_formats,
                    json_inputs=json_inputs,
                    parameters=parameters,
                    outputs=output_streams,
                    logs_limit=int(policy["capture_bytes"]),
                )
            _emit(status, "ready")
            command = read_json_frame(control, limit=MAX_CONTROL_BYTES)
            if command != {"command": "start"}:
                raise _PreflightError("Host did not confirm the Python execution start")
            _emit(status, "started")
            started = True
            started_at = time.monotonic()
            result = entry(context)
            if mode == "query":
                if set(output_streams) != {"__cad2bim_query_result"}:
                    raise _PreflightError("Python query result stream is invalid")
                _write_query_result(output_streams["__cad2bim_query_result"][0], result)
            else:
                if result is not None:
                    raise _ExecutionError("transform(ctx) must return None")
                missing = set(output_streams).difference(context.submitted)
                if missing:
                    raise _ExecutionError(
                        "Declared Arrow outputs were not submitted: "
                        + ", ".join(sorted(missing))
                    )
            _emit(
                status,
                "done",
                execution_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            )
            return 0
        except _CodeValidationError as exc:
            _emit(
                status,
                "error",
                code="TRANSFORM_PYTHON_INVALID",
                message=str(exc),
                diagnostic=exc.diagnostic,
                phase="code_validation",
                started=False,
            )
            return 2
        except _PreflightError as exc:
            _emit(
                status,
                "error",
                code="TRANSFORM_PYTHON_INVALID",
                message=_bounded_error(str(exc)),
                phase="preflight",
                started=False,
            )
            return 2
        except _QueryResultError as exc:
            _emit(
                status,
                "error",
                code="TRANSFORM_PYTHON_INVALID",
                message=_bounded_error(str(exc)),
                phase="output_validation",
                started=started,
            )
            return 2
        except BaseException as exc:
            if not started:
                _emit(
                    status,
                    "error",
                    code="TRANSFORM_SERVICE_UNAVAILABLE",
                    message="Python worker failed before user code execution",
                    phase="preflight",
                    started=False,
                )
                return 3
            source_code = locals().get("code")
            if not isinstance(source_code, str):
                raw_payload = locals().get("payload")
                source_code = (
                    str(raw_payload.get("code", ""))
                    if isinstance(raw_payload, dict)
                    else ""
                )
            diagnostic = _runtime_code_diagnostic(
                source_code,
                locals().get("entry_name", "transform"),
                exc,
            )
            _emit(
                status,
                "error",
                code="TRANSFORM_EXECUTION_FAILED",
                message=_diagnostic_json(diagnostic),
                diagnostic=diagnostic,
                phase="execution",
                started=True,
            )
            return 3
        finally:
            for stream, _format, _json_kind in locals().get("output_streams", {}).values():
                try:
                    stream.close()
                except OSError:
                    pass


def main() -> int:
    try:
        control_fd = int(os.environ.pop("CAD2BIM_WORKER_CONTROL_FD"))
        status_fd = int(os.environ.pop("CAD2BIM_WORKER_STATUS_FD"))
    except (KeyError, ValueError):
        return 64
    return _run(control_fd, status_fd)


if __name__ == "__main__":
    raise SystemExit(main())
