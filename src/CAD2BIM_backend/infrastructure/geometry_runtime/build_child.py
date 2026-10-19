"""Private child for one isolated fixed build or function validation."""

from __future__ import annotations

import json
import os
from typing import Any, BinaryIO


_BUNDLE_PACKAGE = "cad2bim_authoring_bundle"
_MAX_BUNDLE_BYTES = 272 * 1024
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_ALLOWED_IMPORT_ROOTS = frozenset({"math", "cadquery", "OCP"})


def main() -> int:
    from CAD2BIM_backend.infrastructure.worker_runtime import open_binary_fd

    request_stream = open_binary_fd(_descriptor("CAD2BIM_GEOMETRY_BUILD_REQUEST_FD"), "rb")
    source_stream = open_binary_fd(_descriptor("CAD2BIM_GEOMETRY_BUILD_SOURCE_FD"), "rb")
    output_stream = open_binary_fd(_descriptor("CAD2BIM_GEOMETRY_BUILD_OUTPUT_FD"), "wb")
    terminal_stream = open_binary_fd(_descriptor("CAD2BIM_GEOMETRY_BUILD_TERMINAL_FD"), "wb")
    started = False
    try:
        request = _read_request(request_stream)
        source_bundle = source_stream.read(_MAX_BUNDLE_BYTES + 1)
        if not source_bundle or len(source_bundle) > _MAX_BUNDLE_BYTES:
            raise _ChildError("INVALID_ARGUMENT", "authoring source bundle is invalid", "preflight")
        from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_environment import ModelingEnvironmentError, require_modeling_environment
        try:
            require_modeling_environment()
        except ModelingEnvironmentError as exc:
            raise _ChildError("GEOMETRY_RUNTIME_UNAVAILABLE", str(exc), "preflight") from exc
        if request["mode"] == "interfaces":
            started = True
            _execute_interfaces(request, source_bundle, output_stream)
            _write_json(terminal_stream, {"status": "succeeded", "phase": "interface_evaluation", "execution_started": True})
            return 0
        # Load the approved native geometry stack before the Supervisor settles
        # its baseline; later user execution must not create a second thread
        # pool.  The terminal descriptor is already open so an environment
        # wiring failure remains an actionable, bounded Worker result.
        try:
            from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_environment import load_modeling_dependencies
            load_modeling_dependencies()
        except ImportError as exc:
            raise _ChildError(
                "GEOMETRY_RUNTIME_UNAVAILABLE",
                "geometry build runtime dependencies are unavailable",
                "preflight",
            ) from exc

        program = _compile_bundle(source_bundle)
        started = True
        _execute(request, program, output_stream)
        _write_json(
            terminal_stream,
            {"status": "succeeded", "phase": "execution", "execution_started": True},
        )
        return 0
    except BaseException as exc:
        _write_json(
            terminal_stream,
            {
                "status": "failed",
                "code": str(getattr(exc, "code", "GEOMETRY_BUILD_FAILED")),
                "phase": str(getattr(exc, "phase", "execution" if started else "preflight")),
                "execution_started": started,
                "message": _bounded(str(exc), 2_000),
            },
        )
        return 1


def _compile_bundle(
    source_bundle: bytes,
) -> Any:
    from CAD2BIM_backend.domain.geometry_runtime import (
        MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS,
        MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES,
        MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES,
    )
    from CAD2BIM_backend.infrastructure.worker_runtime import diagnostic_json
    from CAD2BIM_backend.infrastructure.worker_runtime.python_bundle import (
        PythonBundleValidationError,
        compile_python_bundle,
    )

    try:
        return compile_python_bundle(
            source_bundle,
            package_name=_BUNDLE_PACKAGE,
            entry_name="build",
            allowed_import_roots=_ALLOWED_IMPORT_ROOTS,
            profile_label="Geometry authoring",
            maximum_members=MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS,
            maximum_member_bytes=MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES,
            maximum_total_bytes=MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES,
            reject_private_attributes=True,
            controlled_modeling=True,
        )
    except PythonBundleValidationError as exc:
        message = diagnostic_json(exc.diagnostic) if exc.diagnostic is not None else str(exc)
        raise _ChildError("BUILD_SOURCE_INVALID", message, "preflight") from exc


def _execute(
    request: dict[str, Any],
    program: Any,
    output_stream: BinaryIO,
    geometries: dict[str, Any] | None = None,
) -> None:
    from CAD2BIM_backend.domain.canonical import canonical_json
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
    from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import (
        serialize_user_shape,
    )
    from CAD2BIM_backend.infrastructure.source_text.source_bundle import pack_source_bundle
    from CAD2BIM_backend.infrastructure.worker_runtime import (
        RestrictedImportGuard,
        diagnostic_json,
        install_readonly_audit_hook,
        runtime_code_diagnostic,
    )

    import_guard = RestrictedImportGuard(
        allowed_import_roots=_ALLOWED_IMPORT_ROOTS,
        policy={
            "threads": 2,
            "rss_bytes": 2 * 1024**3,
            "scratch_bytes": 128 * 1024**2,
        },
        worker_label="geometry authoring worker",
    )
    install_readonly_audit_hook(import_guard)
    parameter_sets = (
        [request["build_parameters"]]
        if request["mode"] == "fixed"
        else request["sample_parameters"]
    )
    outputs: dict[str, bytes] = {}
    for index, parameters in enumerate(parameter_sets):
        source_path, entry_name, phase = "main.py", "build", "geometry_build"
        try:
            if geometries is None:
                geometries = _read_geometry_inputs(request)
            shape, model_interfaces = execute_geometry_build(program, dict(parameters), geometries, allow_interfaces=request["mode"] == "fixed")
            raw = serialize_user_shape(shape)
            if request["mode"] == "fixed":
                interfaces = canonical_json(model_interfaces).encode("utf-8")
        except BaseException as exc:
            diagnostic = runtime_code_diagnostic(
                program.sources[source_path],
                entry_name,
                exc,
                virtual_sources=program.sources,
                virtual_filename_prefix="<cad2bim-bundle:",
            )
            raise _ChildError(
                "INTERFACE_EVALUATION_FAILED" if phase == "interface_evaluation" else "BUILD_EXECUTION_FAILED",
                diagnostic_json(diagnostic),
                phase,
            ) from exc
        outputs[f"sample-{index:03d}.brep"] = raw
        if request["mode"] == "fixed":
            outputs[f"sample-{index:03d}.interfaces.json"] = interfaces
    packed = pack_source_bundle(outputs)
    if len(packed) > _MAX_OUTPUT_BYTES:
        raise _ChildError("BUILD_OUTPUT_BUDGET_EXCEEDED", "geometry build output exceeds 64 MiB")
    output_stream.write(packed)
    output_stream.flush()


def _execute_interfaces(request, source_bundle, output_stream):
    from CAD2BIM_backend.domain.canonical import canonical_json
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import compile_geometry_interfaces, execute_geometry_interfaces
    from CAD2BIM_backend.infrastructure.source_text.source_bundle import pack_source_bundle
    from CAD2BIM_backend.infrastructure.worker_runtime import RestrictedImportGuard, install_readonly_audit_hook, diagnostic_json, runtime_code_diagnostic
    from CAD2BIM_backend.infrastructure.worker_runtime.python_bundle import PythonBundleValidationError
    try:
        program = compile_geometry_interfaces(source_bundle, "cad2bim_geometry_interfaces")
    except PythonBundleValidationError as exc:
        raise _ChildError("BUILD_SOURCE_INVALID", diagnostic_json(exc.diagnostic) if exc.diagnostic else str(exc), "code_validation") from exc
    guard = RestrictedImportGuard(allowed_import_roots={"math"}, worker_label="geometry interface worker")
    install_readonly_audit_hook(guard)
    outputs = {}
    for index, parameters in enumerate(request["sample_parameters"]):
        try:
            interfaces = execute_geometry_interfaces(program, parameters)
        except BaseException as exc:
            diagnostic = runtime_code_diagnostic(program.sources["interfaces.py"], "evaluate", exc,
                virtual_sources=program.sources, virtual_filename_prefix="<cad2bim-bundle:")
            raise _ChildError("INTERFACE_EVALUATION_FAILED", diagnostic_json(diagnostic), "interface_evaluation") from exc
        outputs[f"sample-{index:03d}.interfaces.json"] = canonical_json(interfaces).encode("utf-8")
    output_stream.write(pack_source_bundle(outputs))
    output_stream.flush()


def _read_request(stream: BinaryIO) -> dict[str, Any]:
    content = stream.read(128 * 1024 + 1)
    if not content or len(content) > 128 * 1024:
        raise _ChildError("INVALID_ARGUMENT", "geometry build request is invalid", "preflight")
    value = json.loads(content)
    if not isinstance(value, dict) or value.get("mode") not in {"fixed", "function", "interfaces"}:
        raise _ChildError("INVALID_ARGUMENT", "geometry build request is invalid", "preflight")
    expected = (
        {"mode", "build_parameters"} if value["mode"] == "fixed" else {"mode", "sample_parameters"}
    )
    if value["mode"] == "interfaces" and "geometry_inputs" in value:
        raise _ChildError("INVALID_ARGUMENT", "numeric interfaces cannot consume geometry inputs", "preflight")
    if "geometry_inputs" in value:
        expected.add("geometry_inputs")
    if set(value) != expected:
        raise _ChildError("INVALID_ARGUMENT", "geometry build request is invalid", "preflight")
    parameter_sets = (
        [value.get("build_parameters")]
        if value["mode"] == "fixed"
        else value.get("sample_parameters")
    )
    if (
        not isinstance(parameter_sets, list)
        or not parameter_sets
        or len(parameter_sets) > 8
        or any(not isinstance(item, dict) for item in parameter_sets)
    ):
        raise _ChildError("INVALID_ARGUMENT", "geometry build parameters are invalid", "preflight")
    return value


def _read_geometry_inputs(request: dict[str, Any]) -> dict[str, Any]:
    """Read the existing private FD channel before installing the user-code guard."""
    from pydantic import TypeAdapter
    from CAD2BIM_backend.domain.geometry_runtime import (
        GeometryBuildInput, GeometryBuildInputs, MAX_GEOMETRY_BUILD_INPUT_BYTES,
        MAX_GEOMETRY_BUILD_INPUTS,
    )
    from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import reopen_user_shape, validate_user_shape
    from CAD2BIM_backend.infrastructure.worker_runtime import open_binary_fd

    try:
        metadata = request.get("geometry_inputs", [])
        raw_fds = os.environ.pop("CAD2BIM_GEOMETRY_BUILD_INPUT_FDS", "")
        descriptors = [int(value) for value in raw_fds.split(",")] if raw_fds else []
        if (not isinstance(metadata, list) or len(metadata) != len(descriptors)
                or len(metadata) > MAX_GEOMETRY_BUILD_INPUTS
                or len(set(descriptors)) != len(descriptors) or any(fd < 3 for fd in descriptors)):
            raise ValueError("geometry input descriptor count is invalid")
        total = 0
        values = []
        for item, descriptor in zip(metadata, descriptors, strict=True):
            if not isinstance(item, dict) or set(item) != {"input_key", "sha256", "size_bytes"}:
                raise ValueError("geometry input metadata is invalid")
            size = item["size_bytes"]
            if type(size) is not int or not 1 <= size <= MAX_GEOMETRY_BUILD_INPUT_BYTES:
                raise ValueError("geometry input byte budget is invalid")
            total += size
            if total > MAX_GEOMETRY_BUILD_INPUT_BYTES:
                raise ValueError("geometry inputs exceed the aggregate 32 MiB limit")
            with open_binary_fd(descriptor, "rb") as stream:
                payload = stream.read(size + 1)
            if len(payload) != size:
                raise ValueError("geometry input size changed in transit")
            values.append(GeometryBuildInput(input_key=item["input_key"], payload=payload, sha256=item["sha256"]))
        inputs = TypeAdapter(GeometryBuildInputs).validate_python(values)
        geometries = {}
        for item in inputs:
            shape = reopen_user_shape(item.payload)
            validate_user_shape(shape)
            geometries[item.input_key] = shape
        return geometries
    except (ValueError, TypeError, OSError) as exc:
        raise _ChildError("BUILD_GEOMETRY_INPUT_INVALID", str(exc), "preflight") from exc


class _ChildError(RuntimeError):
    def __init__(self, code: str, message: str, phase: str = "execution") -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase


def _write_json(stream: BinaryIO, value: dict[str, Any]) -> None:
    try:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        stream.flush()
    except OSError:
        pass


def _descriptor(name: str) -> int:
    try:
        value = int(os.environ.pop(name))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("geometry build worker descriptor contract is invalid") from exc
    if value < 3:
        raise RuntimeError("geometry build worker descriptor contract is invalid")
    return value


def _bounded(value: str, limit: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return encoded.decode("utf-8")
    return encoded[:limit].decode("utf-8", errors="ignore")


if __name__ == "__main__":
    raise SystemExit(main())
