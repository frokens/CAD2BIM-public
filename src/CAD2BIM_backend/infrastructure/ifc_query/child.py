"""Private child process for one bounded, read-only IfcOpenShell query."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import resource
from typing import Any, BinaryIO

import pyarrow as pa

from CAD2BIM_backend.infrastructure.ifc.product_realization_host import inspect_current_straight_body
from CAD2BIM_backend.infrastructure.ifc_query.readonly_model import (
    _ChildError, _validate_source, load_private_model, PrivateModelAudit,
)
from CAD2BIM_backend.domain.product_realization_host import ProductRealizationHostError

from CAD2BIM_backend.infrastructure.worker_runtime import (
    MAX_CONTROL_BYTES,
    MAX_STATUS_BYTES,
    PythonUserCodePreflightError,
    PythonUserCodeValidationError,
    RestrictedImportGuard,
    compile_entry,
    install_readonly_audit_hook,
    open_binary_fd,
    read_json_frame,
    runtime_code_diagnostic,
    safe_builtins,
    write_json_frame,
)


_ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "collections",
        "decimal",
        "fractions",
        "functools",
        "ifcopenshell",
        "itertools",
        "json",
        "math",
        "operator",
        "pyarrow",
        "re",
        "statistics",
    }
)
_INPUT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")




class _QueryContext:
    """Minimal query-only API over one process-private IFC model and exact inputs."""

    __slots__ = (
        "_ifc_model",
        "_ifc_sha256",
        "_input_formats",
        "_inputs",
        "_json_inputs",
        "_logs",
        "_logs_bytes",
        "_logs_limit",
    )

    def __init__(
        self,
        *,
        ifc_model: Any,
        ifc_sha256: str,
        inputs: dict[str, pa.Table],
        input_formats: dict[str, str],
        json_inputs: dict[str, Any],
        logs_limit: int,
    ) -> None:
        self._ifc_model = ifc_model
        self._ifc_sha256 = ifc_sha256
        self._inputs = inputs
        self._input_formats = input_formats
        self._json_inputs = json_inputs
        self._logs_limit = logs_limit
        self._logs_bytes = 0
        self._logs: list[str] = []

    @property
    def logs(self) -> tuple[str, ...]:
        return tuple(self._logs)

    def model(self) -> Any:
        """Return the process-private IFC query copy."""

        return self._ifc_model

    def table(self, name: str) -> pa.Table:
        try:
            if self._input_formats[name] == "json":
                raise TypeError(f"JSON input {name!r} must be read with ctx.json")
            return self._inputs[name]
        except KeyError as exc:
            raise KeyError(f"Arrow input {name!r} was not declared") from exc

    def host_sections(self, product_global_id: str) -> dict[str, Any]:
        """Read actual straight Body intervals and reusable exact section references."""
        if not isinstance(product_global_id, str) or not re.fullmatch(r"[0-9A-Za-z_$]{22}", product_global_id):
            raise ValueError("ctx.host_sections requires one IFC product GlobalId")
        try:
            body = inspect_current_straight_body(self._ifc_model, product_global_id, ifc_sha256=self._ifc_sha256)
        except ProductRealizationHostError as exc:
            raise ValueError(f"{exc.code}: {exc}") from exc
        result = body.model_dump(mode="json")
        for row, section in zip(result["sections"], body.sections):
            row["body_section_ref"] = body.section_reference(section).model_dump(mode="json")
        return result

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

    def log(self, message: str) -> None:
        if not isinstance(message, str):
            raise TypeError("ctx.log message must be text")
        size = len(message.encode("utf-8"))
        if self._logs_bytes + size > self._logs_limit:
            raise ValueError("ctx.log exceeded the bounded diagnostics budget")
        self._logs_bytes += size
        self._logs.append(message)


def _descriptor(name: str) -> int:
    try:
        descriptor = int(os.environ[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("IFC query worker descriptor is unavailable") from exc
    if descriptor < 3:
        raise RuntimeError("IFC query worker descriptor is invalid")
    return descriptor




def _decode_json_input(table: pa.Table, json_kind: str) -> Any:
    if json_kind == "document":
        if table.schema.names != ["json_value"] or table.num_rows != 1:
            raise ValueError("document JSON input must be one json_value row")
        value = json.loads(table.column("json_value")[0].as_py())
    elif json_kind == "records":
        if table.schema.names != ["record_index", "json_value"]:
            raise ValueError("records JSON input schema is invalid")
        indices = table.column("record_index").to_pylist()
        if indices != list(range(table.num_rows)):
            raise ValueError("records JSON input indices must be contiguous from zero")
        value = [json.loads(item) for item in table.column("json_value").to_pylist()]
    else:
        raise ValueError("JSON input kind is invalid")
    _validate_json_value(value)
    return value


class _JsonValueError(ValueError):
    def __init__(self, value: Any, path: str, reason: str) -> None:
        self.diagnostic = {"json_path": path, "value_type": type(value).__name__[:80], "reason": reason}
        super().__init__(reason)


def _json_child_path(path: str, segment: str) -> str:
    encoded = (path + segment).encode("utf-8")
    return encoded[:381].decode("utf-8", errors="ignore") + "..." if len(encoded) > 384 else path + segment


def _validate_json_value(value: Any, *, depth: int = 0, path: str = "$") -> None:
    if depth > 64:
        raise _JsonValueError(value, path, "maximum_depth_exceeded")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _JsonValueError(value, path, "non_finite_number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, depth=depth + 1, path=_json_child_path(path, f"[{index}]"))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _JsonValueError(key, path, "non_string_object_key")
            segment = "[" + json.dumps(key[:64], ensure_ascii=True) + ("..." if len(key) > 64 else "") + "]"
            _validate_json_value(item, depth=depth + 1, path=_json_child_path(path, segment))
        return
    raise _JsonValueError(value, path, "unsupported_type")


def _read_inputs(
    items: list[dict[str, Any]],
) -> tuple[dict[str, pa.Table], dict[str, str], dict[str, Any]]:
    if len(items) > 16:
        raise ValueError("IFC query accepts at most 16 exact inputs")
    inputs: dict[str, pa.Table] = {}
    formats: dict[str, str] = {}
    json_inputs: dict[str, Any] = {}
    for item in items:
        name = str(item["input_name"])
        if _INPUT_NAME.fullmatch(name) is None or name in inputs:
            raise ValueError("IFC query input names are invalid or duplicated")
        with open_binary_fd(int(item["fd"]), "rb") as stream:
            inputs[name] = pa.ipc.open_stream(stream).read_all()
        input_format = str(item["input_format"])
        if input_format not in {"parquet", "inline", "json"}:
            raise ValueError("IFC query input format is invalid")
        formats[name] = input_format
        if input_format == "json":
            json_inputs[name] = _decode_json_input(inputs[name], str(item.get("json_kind")))
    return inputs, formats, json_inputs






def _compile_program(code: str, *, derive: bool, allowed_imports: frozenset[str]):
    try:
        return compile_entry(
            code,
            "transform" if derive else "query",
            allowed_import_roots=allowed_imports,
            allowed_context_io_attributes=("write_table", "write_json") if derive else (),
            profile_label="IFC query",
        )
    except PythonUserCodeValidationError as exc:
        import_policy_error = exc.diagnostic.get("exception_type") == "ImportPolicyError"
        raise _ChildError(
            "IFC_QUERY_SOURCE_INVALID" if import_policy_error else "IFC_QUERY_PYTHON_INVALID",
            (_safe_text(str(exc.diagnostic["message"])) if import_policy_error
             else "IFC query source is not valid Python"),
            phase="code_validation",
            execution_started=False,
            diagnostic=_safe_diagnostic(exc.diagnostic),
        ) from None
    except PythonUserCodePreflightError as exc:
        raise _ChildError(
            "IFC_QUERY_SOURCE_INVALID",
            _safe_text(str(exc)),
            phase="code_validation",
            execution_started=False,
        ) from None


def _initialize_dependencies(import_guard: RestrictedImportGuard) -> None:
    """Load only approved installed libraries before author compilation/execution."""
    from CAD2BIM_backend.infrastructure.data_transform.capabilities import PYTHON_ARROW_APPROVED_LIBRARIES

    for name in (*PYTHON_ARROW_APPROVED_LIBRARIES, "ifcopenshell"):
        import_guard(name, {"__name__": __name__})
    # Neither descriptor is retained in the author namespace or execution phase.
    with (
        open_binary_fd(_descriptor("CAD2BIM_IFC_READY_FD"), "wb") as ready,
        open_binary_fd(_descriptor("CAD2BIM_IFC_START_FD"), "rb") as start,
    ):
        ready.write(b"READY\n")
        ready.flush()
        if start.read(6) != b"START\n":
            raise _ChildError("IFC_QUERY_WORKER_FAILED", "Host did not release author execution",
                              phase="runtime", execution_started=False)


def _execute(payload: dict[str, Any], result_stream: BinaryIO) -> dict[str, Any]:
    from CAD2BIM_backend.domain.canonical import canonical_json
    from CAD2BIM_backend.ports.ifc_query_worker import MAX_IFC_INPUT_BYTES

    code = str(payload["code"])
    _validate_source(code)
    derive = payload.get("mode") == "transform"
    entry_name = "transform" if derive else "query"
    if derive:
        from CAD2BIM_backend.infrastructure.data_transform.python_arrow_child import (
            _ALLOWED_IMPORT_ROOTS as data_imports,
        )
        allowed_imports = frozenset((*data_imports, "ifcopenshell"))
    else:
        allowed_imports = _ALLOWED_IMPORT_ROOTS
    compiled = None if derive else _compile_program(code, derive=False, allowed_imports=allowed_imports)

    with open_binary_fd(int(payload["ifc_fd"]), "rb") as ifc_stream:
        ifc_bytes = ifc_stream.read(MAX_IFC_INPUT_BYTES + 1)
    expected_sha256 = str(payload["expected_ifc_sha256"])
    source_sha256 = hashlib.sha256(ifc_bytes).hexdigest()
    model = load_private_model(ifc_bytes, expected_sha256)
    del ifc_bytes
    try:
        if derive:
            from CAD2BIM_backend.infrastructure.data_transform.python_arrow_child import _read_inputs as read_data_inputs
            inputs, formats, json_inputs = read_data_inputs(list(payload["inputs"]))
        else:
            inputs, formats, json_inputs = _read_inputs(list(payload["inputs"]))
    except (KeyError, TypeError, ValueError, pa.ArrowException) as exc:
        raise _ChildError(
            "IFC_QUERY_INPUT_INVALID",
            _safe_text(str(exc)),
            phase="input_validation",
            execution_started=False,
        ) from exc

    policy = dict(payload["policy"])
    import_guard = RestrictedImportGuard(
        allowed_import_roots=allowed_imports,
        policy=policy,
        worker_label="IFC query worker",
    )
    namespace: dict[str, Any] = {
        "__builtins__": safe_builtins(import_guard),
        "__name__": "__cad2bim_ifc_query__",
    }
    context = _QueryContext(
        ifc_model=model,
        ifc_sha256=source_sha256,
        inputs=inputs,
        input_formats=formats,
        json_inputs=json_inputs,
        logs_limit=int(payload["logs_limit"]),
    )
    output_streams: dict[str, tuple[BinaryIO, str, str | None]] = {}
    if derive:
        from CAD2BIM_backend.infrastructure.data_transform.python_arrow_child import _TransformContext

        class IfcTransformContext(_TransformContext):
            __slots__ = ("_ifc_model",)

            def model(self) -> Any:
                return self._ifc_model

        output_streams = {
            str(item["payload_name"]): (open_binary_fd(int(item["fd"]), "wb"),
                                      str(item["format"]), item.get("json_kind"))
            for item in payload["outputs"]
        }
        context = IfcTransformContext(
            inputs=inputs, input_formats=formats, json_inputs=json_inputs,
            parameters={str(item["name"]): item["value"] for item in payload["parameters"]},
            outputs=output_streams, logs_limit=int(payload["logs_limit"]),
        )
        context._ifc_model = model
    install_readonly_audit_hook(import_guard)
    if derive:
        _initialize_dependencies(import_guard)
        compiled = _compile_program(code, derive=True, allowed_imports=allowed_imports)
    execution_error: BaseException | None = None
    result: Any = None
    audit = PrivateModelAudit(model)
    try:
        exec(compiled, namespace, namespace)
        entry = namespace.get(entry_name)
        if not callable(entry):
            raise TypeError(f"{entry_name}(ctx) entry point is unavailable")
        result = entry(context)
        if derive and result is not None:
            raise TypeError("transform(ctx) must return None and submit declared outputs")
    except BaseException as exc:
        execution_error = exc
    attestation = audit.finish()
    if execution_error is not None:
        raise _ChildError(
            "IFC_QUERY_EXECUTION_FAILED",
            "IFC query execution failed",
            phase="execution",
            execution_started=True,
            diagnostic=_safe_diagnostic(runtime_code_diagnostic(code, entry_name, execution_error)),
        ) from execution_error
    if derive:
        if context.submitted != frozenset(output_streams):
            raise _ChildError("TRANSFORM_OUTPUT_INVALID", "All declared outputs must be submitted",
                              phase="output_validation", execution_started=True)
        for stream, _format, _kind in output_streams.values():
            stream.flush()
            stream.close()
        return {
            "status": "succeeded", "phase": "output_validation", "execution_started": True,
            "source_ifc_sha256": source_sha256, **attestation,
        }
    try:
        _validate_json_value(result)
        result_json = canonical_json(result)
    except (TypeError, ValueError) as exc:
        raise _ChildError(
            "IFC_QUERY_RESULT_INVALID",
            "query(ctx) must return a canonical JSON value",
            phase="result_validation",
            execution_started=True,
            diagnostic=(exc.diagnostic if isinstance(exc, _JsonValueError) else {
                "json_path": "$", "value_type": type(result).__name__[:80], "reason": "canonicalization_failed",
            }),
        ) from exc
    result_bytes = result_json.encode("utf-8")
    if len(result_bytes) > int(payload["max_result_bytes"]):
        raise _ChildError(
            "IFC_QUERY_RESULT_BUDGET_EXCEEDED",
            "IFC query result exceeds its UTF-8 byte budget",
            phase="result_validation",
            execution_started=True,
        )
    result_stream.write(result_bytes)
    result_stream.flush()
    return {
        "status": "succeeded",
        "phase": "result_validation",
        "execution_started": True,
        "source_ifc_sha256": source_sha256,
        **attestation,
        "result_bytes": len(result_bytes),
        "logs": list(context.logs),
    }


def _apply_limits(policy: dict[str, Any]) -> None:
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
    arrow_threads = max(1, int(policy["threads"]) // 2)
    pa.set_cpu_count(arrow_threads)
    pa.set_io_thread_count(arrow_threads)


def _safe_text(value: str) -> str:
    sanitized = re.sub(r"/(?:Users|home|private|var|tmp)/[^\s:'\"]+", "<host-path>", value)
    return sanitized.encode("utf-8")[:2_000].decode("utf-8", errors="ignore")


def _safe_diagnostic(value: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): _safe_text(item) if isinstance(item, str) else item for key, item in value.items()
    }


def main() -> int:
    terminal_fd = _descriptor("CAD2BIM_IFC_QUERY_TERMINAL_FD")
    try:
        with (
            open_binary_fd(_descriptor("CAD2BIM_IFC_QUERY_CONTROL_FD"), "rb") as control,
            open_binary_fd(_descriptor("CAD2BIM_IFC_QUERY_RESULT_FD"), "wb") as result,
        ):
            payload = read_json_frame(control, limit=MAX_CONTROL_BYTES)
            _apply_limits(dict(payload["policy"]))
            terminal = _execute(payload, result)
        returncode = 0
    except _ChildError as exc:
        terminal = {
            "status": "failed",
            "code": exc.code,
            "message": _safe_text(str(exc)),
            "phase": exc.phase,
            "execution_started": exc.execution_started,
        }
        if exc.diagnostic is not None:
            terminal["diagnostic"] = exc.diagnostic
        returncode = 1
    except BaseException as exc:
        terminal = {
            "status": "failed",
            "code": "IFC_QUERY_WORKER_FAILED",
            "message": "IFC query worker failed inside its isolated runtime",
            "phase": "runtime",
            "execution_started": False,
            "diagnostic": {
                "exception_type": type(exc).__name__,
                "message": _safe_text(str(exc)),
            },
        }
        returncode = 1
    with open_binary_fd(terminal_fd, "wb") as status:
        write_json_frame(status, terminal, limit=MAX_STATUS_BYTES)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
