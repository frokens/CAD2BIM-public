"""The sole OCP/CadQuery adapter for canonical geometry operations."""

from __future__ import annotations

from CAD2BIM_backend.domain.runtime_execution import (
    GEOMETRY_AUTHORING_EXECUTION, GEOMETRY_QUERY_EXECUTION, RuntimeExecutionPolicy,
)

import asyncio
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.geometry_interface import GeometryModelInterfaces
from CAD2BIM_backend.domain.geometry_runtime import (
    MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS,
    MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES,
    MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES,
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometryBuildDisplayContextRequest,
    GeometryBuildDisplayContextResult,
    GeometryBuildFixedRequest,
    GeometryBuildInputs,
    GeometryFixedBuildResult,
    GeometryFunctionSampleExecution,
    GeometryFunctionValidationResult,
    GeometryFunctionInterfaceEvaluation,
    GeometryNormalizeRequest,
    GeometryDisplayMesh,
    GeometryPreview,
    GeometryQueryRequest,
    GeometryQueryResult,
    GeometryRenderRequest,
    GeometrySectionRequest,
    GeometrySectionResult,
    GeometryRuntimeUnavailableError,
    GeometryRuntimeResourceLimitError,
    GeometryRuntimeResourceDiagnostic,
    GeometryRuntimeValidationError,
    GeometrySummary,
    GeometryValidateFunctionRequest,
    IsolatedGeometryQueryExecution,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution import (
    GEOMETRY_CODEC_BUDGET,
    OcpGeometryCodecExecutor,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution_protocol import pack_fragment_inputs
from CAD2BIM_backend.infrastructure.source_text import (
    SourceBundleError,
    inspect_source_bundle,
    pack_source_bundle,
)
from CAD2BIM_backend.infrastructure.worker_runtime import (
    DarwinSeatbeltSandbox,
    HostWorkerCapacity,
    WorkerCapacityBusy,
    WorkerCapacityUnavailable,
    WorkerLaunchSpec,
    WorkerProcessBudget,
    WorkerProcessSupervisor,
    WorkerSupervisorError,
)


GEOMETRY_QUERY_BUDGET = WorkerProcessBudget(
    wall_seconds=180.0,
    cpu_seconds=180.0,
    rss_bytes=3 * 1024**3,
    scratch_bytes=128 * 1024**2,
    # OCP queries can lazily create the same bounded native pool as builds.
    threads=8,
    open_file_descriptors=64,
    stdout_bytes=16 * 1024,
    stderr_bytes=16 * 1024,
    cleanup_seconds=1.0,
)
GEOMETRY_BUILD_BUDGET = WorkerProcessBudget(
    wall_seconds=30.0,
    cpu_seconds=30.0,
    rss_bytes=2 * 1024**3,
    scratch_bytes=128 * 1024**2,
    # OCP may lazily create a small native pool during the first constructive
    # operation; BLAS/OMP remain pinned to two threads in the child env.
    threads=8,
    open_file_descriptors=64,
    stdout_bytes=16 * 1024,
    stderr_bytes=16 * 1024,
    cleanup_seconds=1.0,
)
_TERMINAL_BYTES = 16 * 1024
_ISOLATED_MODULE_BOOTSTRAP = (
    "import runpy, sys; "
    "sys.path.insert(0, sys.argv[1]); "
    "runpy.run_module(sys.argv[2], run_name='__main__')"
)
_INTERFACES_ADAPTER = TypeAdapter(GeometryModelInterfaces)


@dataclass(frozen=True, slots=True)
class _AuthoringSampleOutput:
    geometry: CanonicalGeometry
    interfaces: GeometryModelInterfaces


class OcpCadQueryGeometryRuntime:
    """Normalize, reopen, inspect, and render the fixed geometry ABI.

    Server-owned codec work and agent source execute in separate bounded children
    using the same neutral supervision and Host capacity mechanisms.
    """

    def __init__(
        self,
        *,
        python_executable: str | None = None,
        supervisor: WorkerProcessSupervisor | None = None,
        sandbox: DarwinSeatbeltSandbox | None = None,
        capacity: HostWorkerCapacity | None = None,
        capacity_root: str | Path | None = None,
        codec_executor: OcpGeometryCodecExecutor | None = None,
    ) -> None:
        executable = Path(python_executable or sys.executable)
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("geometry runtime Python executable must be an absolute file")
        self._python = str(executable)
        runtime_directory = Path(__file__).resolve().parent
        worker_runtime_directory = runtime_directory.parent / "worker_runtime"
        source_root = runtime_directory.parents[2]
        self._source_root = source_root.resolve(strict=True)
        trusted_files = tuple(
            path.resolve()
            for path in (
                source_root / "CAD2BIM_backend" / "__init__.py",
                source_root / "CAD2BIM_backend" / "domain" / "__init__.py",
                source_root / "CAD2BIM_backend" / "domain" / "canonical.py",
                source_root / "CAD2BIM_backend" / "domain" / "geometry_frame.py",
                source_root / "CAD2BIM_backend" / "domain" / "geometry_interface.py",
                source_root / "CAD2BIM_backend" / "domain" / "geometry_runtime.py",
                source_root / "CAD2BIM_backend" / "domain" / "geometry_inspection.py",
                source_root / "CAD2BIM_backend" / "domain" / "ifc_geometry_basis.py",
                source_root / "CAD2BIM_backend" / "domain" / "product_family.py",
                source_root / "CAD2BIM_backend" / "domain" / "validation_diagnostics.py",
                source_root / "CAD2BIM_backend" / "domain" / "run_events.py",
                source_root / "CAD2BIM_backend" / "infrastructure" / "__init__.py",
                runtime_directory / "__init__.py",
                runtime_directory / "adapter.py",
                runtime_directory / "build_context.py",
                runtime_directory / "build_child.py",
                runtime_directory / "modeling_capabilities.py",
                runtime_directory / "modeling_execution.py",
                runtime_directory / "modeling_environment.py",
                runtime_directory.parent / "product_realization_runtime" / "child.py",
                runtime_directory.parent / "product_realization_runtime" / "protocol.py",
                source_root / "CAD2BIM_backend" / "domain" / "product_realization.py",
                runtime_directory / "codec.py",
                runtime_directory / "query_child.py",
                runtime_directory / "render.py",
                runtime_directory / "section.py",
                runtime_directory / "codec_execution_protocol.py",
                runtime_directory / "user_geometry.py",
                worker_runtime_directory / "__init__.py",
                worker_runtime_directory / "protocol.py",
                worker_runtime_directory / "timing.py",
                worker_runtime_directory / "python_bundle.py",
                worker_runtime_directory / "python_user_code.py",
                runtime_directory.parent / "source_text" / "__init__.py",
                runtime_directory.parent / "source_text" / "source_bundle.py",
            )
            if path.is_file()
        )
        trusted_directories = tuple(
            dict.fromkeys(
                (source_root,)
                + tuple(
                    path
                    for file_path in trusted_files
                    for path in (file_path.parent, file_path.parent.parent)
                )
            )
        )
        self._sandbox = sandbox or DarwinSeatbeltSandbox(
            self._python,
            trusted_source_files=trusted_files,
            trusted_source_directories=trusted_directories,
            trusted_source_metadata_root=source_root,
        )
        self._supervisor = supervisor or WorkerProcessSupervisor()
        self._capacity = capacity or HostWorkerCapacity(
            max_concurrent_workers=1,
            capacity_root=capacity_root,
        )
        self._codec = codec_executor or OcpGeometryCodecExecutor(
            budget=GEOMETRY_CODEC_BUDGET,
            python_executable=self._python,
            capacity=self._capacity,
        )
        self._closed = False

    def execution_scope(self, policy: RuntimeExecutionPolicy, *, deadline: float | None = None):
        return self._codec.execution_scope(policy, deadline=deadline)

    def invocation_scope(self, policy: RuntimeExecutionPolicy, *, deadline: float | None = None):
        return self._codec.invocation_scope(policy, deadline=deadline)

    def release_execution(self) -> None:
        self._codec.release_execution()

    async def verify_ready(self) -> None:
        self._require_open()
        request = GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1,
            payload=(
                b'{"schema":"indexed_triangle_mesh_v1","triangles":[[0,2,1]],'
                b'"unit":"METRE","vertices":[[0,0,0],[0,1,0],[1,0,0]]}'
            ),
        )
        geometry = await self.normalize(request)
        await self.reopen(geometry)
        # Exercise the actual isolated CadQuery/OCP child as part of readiness.
        # Host-only codec checks cannot detect a miswired Python executable or
        # a missing native geometry distribution.
        await self.build_fixed(
            GeometryBuildFixedRequest(
                source_bundle=pack_source_bundle(
                    {
                        "main.py": (
                            b"import cadquery as cq\n\n"
                            b"def build(ctx):\n"
                            b"    return cq.Workplane('XY').box(0.001, 0.001, 0.001)\n"
                        )
                    }
                )
            )
        )

    async def normalize(self, request: GeometryNormalizeRequest) -> CanonicalGeometry:
        self._require_open()
        return await self._codec.normalize(request)

    async def build_display_context(
        self, request: GeometryBuildDisplayContextRequest
    ) -> GeometryBuildDisplayContextResult:
        self._require_open()
        return await self._codec.build_display_context(request)

    async def reopen(self, geometry: CanonicalGeometry) -> GeometrySummary:
        self._require_open()
        return await self._codec.reopen(geometry)

    async def render(self, request: GeometryRenderRequest) -> GeometryPreview:
        self._require_open()
        return await self._codec.render(request)

    async def section(self, request: GeometrySectionRequest) -> GeometrySectionResult:
        self._require_open()
        return await self._codec.section(request)

    async def export_stl(self, geometry: CanonicalGeometry) -> GeometryDisplayMesh:
        self._require_open()
        return await self._codec.export_stl(geometry)

    async def build_fixed(
        self,
        request: GeometryBuildFixedRequest,
    ) -> GeometryFixedBuildResult:
        async with self.execution_scope(GEOMETRY_AUTHORING_EXECUTION):
            return await self._build_fixed(request)

    async def _build_fixed(self, request: GeometryBuildFixedRequest) -> GeometryFixedBuildResult:
        self._require_open()
        source_sha256 = _validate_authoring_source_bundle(request.source_bundle)
        output_bundle = await self._run_authoring_worker(
            source_bundle=request.source_bundle,
            geometry_inputs=request.geometry_inputs,
            request_document={
                "mode": "fixed",
                "build_parameters": request.build_parameters,
            },
        )
        outputs = await self._normalize_authoring_outputs(output_bundle, expected_count=1)
        geometry = outputs[0].geometry
        interfaces = outputs[0].interfaces
        parameters_sha256 = _canonical_sha256(request.build_parameters)
        return GeometryFixedBuildResult(
            geometry=geometry,
            interfaces=interfaces,
            source_bundle_sha256=source_sha256,
            build_parameters_sha256=parameters_sha256,
        )

    async def validate_function(
        self,
        request: GeometryValidateFunctionRequest,
    ) -> GeometryFunctionValidationResult:
        async with self.execution_scope(GEOMETRY_AUTHORING_EXECUTION):
            evaluation = await self.evaluate_function_interfaces(request)
            return await self.instantiate_function(request, evaluation)

    async def evaluate_function_interfaces(self, request):
        async with self.execution_scope(GEOMETRY_AUTHORING_EXECUTION):
            self._require_open()
            _validate_authoring_source_bundle(request.source_bundle)
            output = await self._run_authoring_worker(source_bundle=request.source_bundle,
                request_document={"mode": "interfaces", "sample_parameters": list(request.sample_parameters)})
            bundle = inspect_source_bundle(output)
            expected = tuple(f"sample-{index:03d}.interfaces.json" for index in range(len(request.sample_parameters)))
            if tuple(member.path for member in bundle.members) != expected:
                raise GeometryRuntimeValidationError("numeric Geometry Worker returned unexpected interface outputs")
            interfaces = tuple(_read_sample_interfaces(bundle.read(path)) for path in expected)
            return GeometryFunctionInterfaceEvaluation(source_bundle=request.source_bundle,
                sample_parameters=request.sample_parameters, sample_interfaces=interfaces)

    async def instantiate_function(self, request, evaluation):
        if evaluation.source_bundle != request.source_bundle or evaluation.sample_parameters != request.sample_parameters:
            raise GeometryRuntimeValidationError("Geometry instantiation differs from its exact numeric evaluation")
        async with self.execution_scope(GEOMETRY_AUTHORING_EXECUTION):
            return await self._validate_function(request, evaluation=evaluation)

    async def _validate_function(self, request: GeometryValidateFunctionRequest, *, evaluation) -> GeometryFunctionValidationResult:
        self._require_open()
        output_bundle = await self._run_authoring_worker(
            source_bundle=request.source_bundle,
            geometry_inputs=request.geometry_inputs,
            request_document={
                "mode": "function",
                "sample_parameters": list(request.sample_parameters),
            },
        )
        outputs = await self._normalize_authoring_outputs(
            output_bundle,
            expected_count=len(request.sample_parameters),
            evaluated_interfaces=evaluation.sample_interfaces,
        )
        executions = tuple(
            GeometryFunctionSampleExecution(
                output_sha256=output.geometry.sha256,
                geometry_summary=output.geometry.summary,
                interfaces=output.interfaces,
            )
            for output in outputs
        )
        return GeometryFunctionValidationResult(
            sample_geometry=outputs[0].geometry,
            sample_geometries=(tuple(output.geometry for output in outputs)
                               if request.retain_sample_geometries else ()),
            sample_executions=executions,
        )

    async def query(self, request: GeometryQueryRequest) -> GeometryQueryResult:
        async with self.execution_scope(GEOMETRY_QUERY_EXECUTION):
            return await self._query(request)

    async def _query(self, request: GeometryQueryRequest) -> GeometryQueryResult:
        self._require_open()
        self._codec._verify_canonical_input(request.geometry)
        for fragment in request.fragment_inputs:
            self._codec._verify_canonical_input(fragment.geometry)
        query_descriptors: list[int] = []
        root: Path | None = None
        try:
            lease = await self._capacity.admit_current_execution()
            host_descriptor = lease.descriptor
            deadline = lease.execution_deadline
            assert host_descriptor is not None and deadline is not None
            root = Path(tempfile.mkdtemp(prefix="cad2bim-geometry-query-"))
            os.chmod(root, 0o700)
            (root / "geometry.bin").write_bytes(request.geometry.payload)
            fragment_documents, fragment_payload = pack_fragment_inputs(request.fragment_inputs)
            if fragment_payload:
                (root / "fragments.bin").write_bytes(fragment_payload)
            request_payload = canonical_json(
                {
                    "format": request.geometry.format.value,
                    "expected_geometry_sha256": request.geometry.sha256,
                    "source_code": request.source_code,
                    "max_result_bytes": request.max_result_bytes,
                    "selection": None if request.selection is None else request.selection.model_dump(mode="json"),
                    "coordinates": None if request.coordinates is None else request.coordinates.model_dump(mode="json"),
                    "inspection_context": request.inspection_context,
                    "fragment_inputs": fragment_documents,
                    "fragment_payload_bytes": len(fragment_payload),
                }
            ).encode("utf-8")
            if len(request_payload) > 512 * 1024:
                raise GeometryRuntimeValidationError("geometry query metadata/source exceeds the 512 KiB private request budget")
            (root / "request.json").write_bytes(request_payload)
            request_fd = _open_read_only(root / "request.json")
            geometry_fd = _open_read_only(root / "geometry.bin")
            result_fd = _open_write_once(root / "result.json")
            terminal_fd = _open_write_once(root / "terminal.json")
            query_descriptors.extend((request_fd, geometry_fd, result_fd, terminal_fd))
            fragment_fd = None
            if fragment_payload:
                fragment_fd = _open_read_only(root / "fragments.bin")
                query_descriptors.append(fragment_fd)
            command = self._sandbox.command(
                scratch=root,
                child_args=self._isolated_module_args(
                    "CAD2BIM_backend.infrastructure.geometry_runtime.query_child"
                ),
            )
            environment = _environment(root)
            environment.update(
                {
                    "CAD2BIM_GEOMETRY_REQUEST_FD": str(request_fd),
                    "CAD2BIM_GEOMETRY_PAYLOAD_FD": str(geometry_fd),
                    "CAD2BIM_GEOMETRY_RESULT_FD": str(result_fd),
                    "CAD2BIM_GEOMETRY_TERMINAL_FD": str(terminal_fd),
                }
            )
            if fragment_fd is not None:
                environment["CAD2BIM_GEOMETRY_FRAGMENTS_FD"] = str(fragment_fd)
            terminal = await self._supervisor.run(
                WorkerLaunchSpec(
                    command=command,
                    cwd=root,
                    env=environment,
                    pass_fds=tuple(query_descriptors),
                    capacity_fds=(host_descriptor,),
                    scratch=root,
                ),
                budget=GEOMETRY_QUERY_BUDGET,
                deadline=deadline,
            )
            for descriptor in query_descriptors:
                os.close(descriptor)
            query_descriptors.clear()
            child_terminal = _read_worker_terminal(
                root / "terminal.json",
                worker_label="geometry query worker",
            )
            if terminal.returncode != 0 or child_terminal.get("status") != "succeeded":
                self._raise_child_failure(
                    child_terminal,
                    worker_label="geometry query worker",
                )
            execution = IsolatedGeometryQueryExecution.model_validate(
                _read_json(
                    root / "result.json",
                    request.max_result_bytes,
                    worker_label="geometry query worker",
                )
            )
            if execution.geometry_sha256 != request.geometry.sha256:
                raise GeometryRuntimeValidationError(
                    "geometry query receipt does not reference the verified input payload"
                )
            result_bytes = len(canonical_json(execution.result).encode("utf-8"))
            if result_bytes > request.max_result_bytes:
                raise GeometryRuntimeValidationError("geometry query result exceeds byte budget")
            return GeometryQueryResult(
                result=execution.result,
                result_bytes=result_bytes,
                geometry_sha256=request.geometry.sha256,
            )
        except TimeoutError as exc:
            raise GeometryRuntimeUnavailableError("geometry query worker capacity is busy") from exc
        except (WorkerCapacityBusy, WorkerCapacityUnavailable) as exc:
            raise GeometryRuntimeUnavailableError(
                str(exc), diagnostic=self._capacity.failure_diagnostic(exc),
                retryable=isinstance(exc, WorkerCapacityBusy) and exc.retryable,
            ) from exc
        except WorkerSupervisorError as exc:
            if exc.execution_started and exc.code in {"WORKER_TIMEOUT", "WORKER_RESOURCE_LIMIT"}:
                raise GeometryRuntimeResourceLimitError(
                    "geometry query exceeded its supervised execution limit",
                    diagnostic=GeometryRuntimeResourceDiagnostic(
                        resource_kind=exc.resource_kind or "unknown",
                        configured_limits=asdict(exc.budget or GEOMETRY_QUERY_BUDGET),
                        sampled_peaks=asdict(exc.observations) if exc.observations else None,
                        elapsed_seconds=exc.elapsed_seconds,
                        effective_wall_seconds=exc.effective_wall_seconds,
                    ),
                ) from exc
            raise GeometryRuntimeUnavailableError(
                str(exc), diagnostic=exc.diagnostic(), retryable=exc.code != "WORKER_TIMEOUT",
            ) from exc
        finally:
            for descriptor in query_descriptors:
                os.close(descriptor)
            if root is not None:
                await asyncio.to_thread(shutil.rmtree, root, True)

    async def _run_authoring_worker(
        self,
        *,
        source_bundle: bytes,
        request_document: dict[str, Any],
        geometry_inputs: GeometryBuildInputs = (),
    ) -> bytes:
        descriptors: list[int] = []
        root: Path | None = None
        try:
            lease = await self._capacity.admit_current_execution()
            host_descriptor = lease.descriptor
            deadline = lease.execution_deadline
            assert host_descriptor is not None and deadline is not None
            root = Path(tempfile.mkdtemp(prefix="cad2bim-geometry-build-"))
            os.chmod(root, 0o700)
            input_fds = []
            if geometry_inputs:
                request_document = {**request_document, "geometry_inputs": [
                    {"input_key": item.input_key, "sha256": item.sha256, "size_bytes": len(item.payload)}
                    for item in geometry_inputs]}
                for index, item in enumerate(geometry_inputs):
                    path = root / f"geometry-input-{index:02d}.brep"
                    path.write_bytes(item.payload)
                    descriptor = _open_read_only(path)
                    descriptors.append(descriptor)
                    input_fds.append(descriptor)
            request_payload = canonical_json(request_document).encode("utf-8")
            if len(request_payload) > 128 * 1024:
                raise GeometryRuntimeValidationError("geometry build request exceeds 128 KiB")
            (root / "request.json").write_bytes(request_payload)
            (root / "source.zip").write_bytes(source_bundle)
            request_fd = _open_read_only(root / "request.json")
            source_fd = _open_read_only(root / "source.zip")
            output_fd = _open_write_once(root / "output.zip")
            terminal_fd = _open_write_once(root / "terminal.json")
            descriptors.extend((request_fd, source_fd, output_fd, terminal_fd))
            command = self._sandbox.command(
                scratch=root,
                child_args=self._isolated_module_args(
                    "CAD2BIM_backend.infrastructure.geometry_runtime.build_child"
                ),
            )
            environment = _environment(root)
            environment.update(
                {
                    "CAD2BIM_GEOMETRY_BUILD_REQUEST_FD": str(request_fd),
                    "CAD2BIM_GEOMETRY_BUILD_SOURCE_FD": str(source_fd),
                    "CAD2BIM_GEOMETRY_BUILD_OUTPUT_FD": str(output_fd),
                    "CAD2BIM_GEOMETRY_BUILD_TERMINAL_FD": str(terminal_fd),
                    "CAD2BIM_GEOMETRY_BUILD_INPUT_FDS": ",".join(map(str, input_fds)),
                }
            )
            terminal = await self._supervisor.run(
                WorkerLaunchSpec(
                    command=command,
                    cwd=root,
                    env=environment,
                    pass_fds=tuple(descriptors),
                    capacity_fds=(host_descriptor,),
                    scratch=root,
                ),
                budget=GEOMETRY_BUILD_BUDGET,
                deadline=deadline,
            )
            for descriptor in descriptors:
                os.close(descriptor)
            descriptors.clear()
            child_terminal = _read_worker_terminal(
                root / "terminal.json",
                worker_label="geometry build worker",
            )
            if terminal.returncode != 0 or child_terminal.get("status") != "succeeded":
                self._raise_child_failure(
                    child_terminal,
                    worker_label="geometry build worker",
                )
            output = (root / "output.zip").read_bytes()
            if not output or len(output) > 65 * 1024 * 1024:
                raise GeometryRuntimeValidationError(
                    "geometry build output exceeds its byte budget"
                )
            return output
        except TimeoutError as exc:
            raise GeometryRuntimeUnavailableError("geometry build worker capacity is busy") from exc
        except (WorkerCapacityBusy, WorkerCapacityUnavailable) as exc:
            raise GeometryRuntimeUnavailableError(
                str(exc), diagnostic=self._capacity.failure_diagnostic(exc),
                retryable=isinstance(exc, WorkerCapacityBusy) and exc.retryable,
            ) from exc
        except WorkerSupervisorError as exc:
            if exc.execution_started and exc.code in {"WORKER_TIMEOUT", "WORKER_RESOURCE_LIMIT"}:
                raise GeometryRuntimeResourceLimitError(
                    "geometry build exceeded its supervised execution limit",
                    diagnostic=GeometryRuntimeResourceDiagnostic(
                        resource_kind=exc.resource_kind or "unknown",
                        configured_limits=asdict(exc.budget or GEOMETRY_BUILD_BUDGET),
                        sampled_peaks=asdict(exc.observations) if exc.observations else None,
                        elapsed_seconds=exc.elapsed_seconds,
                        effective_wall_seconds=exc.effective_wall_seconds,
                    ),
                ) from exc
            raise GeometryRuntimeUnavailableError(
                str(exc), diagnostic=exc.diagnostic(), retryable=exc.code != "WORKER_TIMEOUT",
            ) from exc
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
            if root is not None:
                await asyncio.to_thread(shutil.rmtree, root, True)

    async def _normalize_authoring_outputs(
        self,
        output_bundle: bytes,
        *,
        expected_count: int,
        evaluated_interfaces=None,
    ) -> tuple[_AuthoringSampleOutput, ...]:
        if not 1 <= expected_count <= 8:
            raise GeometryRuntimeValidationError(
                "geometry build sample count must be between 1 and 8"
            )
        try:
            bundle = inspect_source_bundle(output_bundle)
        except SourceBundleError as exc:
            raise GeometryRuntimeValidationError(
                "geometry build worker returned an invalid output bundle"
            ) from exc
        expected_paths = tuple(
            path
            for index in range(expected_count)
            for path in ((f"sample-{index:03d}.brep",) if evaluated_interfaces is not None else
                         (f"sample-{index:03d}.brep", f"sample-{index:03d}.interfaces.json"))
        )
        if tuple(member.path for member in bundle.members) != expected_paths:
            raise GeometryRuntimeValidationError(
                "geometry build worker returned unexpected sample outputs"
            )
        requests = []
        sample_interfaces = []
        for index in range(expected_count):
            requests.append(
                GeometryNormalizeRequest(
                    format=CanonicalGeometryFormat.OCCT_BREP_V1,
                    source_format=None,
                    payload=bundle.read(f"sample-{index:03d}.brep"),
                    unit="METRE",
                )
            )
            interfaces = (evaluated_interfaces[index] if evaluated_interfaces is not None else
                          _read_sample_interfaces(bundle.read(f"sample-{index:03d}.interfaces.json")))
            sample_interfaces.append(interfaces)
        normalized = await self._codec.normalize_many(tuple(requests))
        if len(normalized) != expected_count:
            raise GeometryRuntimeValidationError("geometry codec returned unexpected sample count")
        return tuple(
            _AuthoringSampleOutput(geometry=geometry, interfaces=interfaces)
            for (geometry, _raw_summary), interfaces in zip(
                normalized, sample_interfaces, strict=True
            )
        )

    def _isolated_module_args(self, module_name: str) -> tuple[str, ...]:
        """Load only this installed package root even when ``-I`` ignores PYTHONPATH."""

        return (
            "-I",
            "-B",
            "-c",
            _ISOLATED_MODULE_BOOTSTRAP,
            str(self._source_root),
            module_name,
        )

    async def close(self) -> None:
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise GeometryRuntimeUnavailableError("geometry runtime is closed")

    @staticmethod
    def _raise_child_failure(
        terminal: dict[str, Any],
        *,
        worker_label: str,
    ) -> None:
        code = str(terminal.get("code", "QUERY_FAILED"))
        message = str(terminal.get("message", f"{worker_label} failed"))[:2_000]
        if code == "GEOMETRY_RUNTIME_UNAVAILABLE":
            raise GeometryRuntimeUnavailableError(message)
        raise GeometryRuntimeValidationError(f"{code}: {message}")


def _read_json(path: Path, limit: int, *, worker_label: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > limit:
            raise GeometryRuntimeValidationError(f"{worker_label} output exceeds byte budget")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GeometryRuntimeValidationError(f"{worker_label} returned invalid output") from exc
    if not isinstance(value, dict):
        raise GeometryRuntimeValidationError(f"{worker_label} output must be an object")
    return value


def _read_worker_terminal(path: Path, *, worker_label: str) -> dict[str, Any]:
    try:
        return _read_json(path, _TERMINAL_BYTES, worker_label=worker_label)
    except GeometryRuntimeValidationError as exc:
        raise GeometryRuntimeUnavailableError(
            f"{worker_label} exited without a trusted terminal"
        ) from exc


def _environment(root: Path) -> dict[str, str]:
    thread_limit = str(GEOMETRY_QUERY_BUDGET.threads)
    return {
        "BLIS_NUM_THREADS": thread_limit,
        "HOME": str(root),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "MKL_NUM_THREADS": thread_limit,
        "NUMEXPR_NUM_THREADS": thread_limit,
        "OMP_NUM_THREADS": thread_limit,
        "OPENBLAS_NUM_THREADS": thread_limit,
        "PATH": str(Path(sys.executable).parent),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(root),
        "VECLIB_MAXIMUM_THREADS": thread_limit,
    }


def _open_read_only(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _open_write_once(path: Path) -> int:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    return os.open(path, flags, 0o600)


def _validate_authoring_source_bundle(content: bytes) -> str:
    try:
        bundle = inspect_source_bundle(content)
    except SourceBundleError as exc:
        raise GeometryRuntimeValidationError("authoring source bundle is invalid") from exc
    if not 1 <= len(bundle.members) <= MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS:
        raise GeometryRuntimeValidationError(
            "authoring source bundle accepts at most 16 Python files"
        )
    if not any(member.path == "main.py" for member in bundle.members):
        raise GeometryRuntimeValidationError("authoring source bundle requires main.py")
    if sum(member.size_bytes for member in bundle.members) > MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES:
        raise GeometryRuntimeValidationError("authoring source bundle exceeds 256 KiB")
    for member in bundle.members:
        if (
            not member.path.endswith(".py")
            or member.size_bytes > MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES
            or not member.text_available
        ):
            raise GeometryRuntimeValidationError(
                "authoring bundle accepts only UTF-8 Python files up to 64 KiB each"
            )
    return bundle.sha256


def _canonical_sha256(value: Any) -> str:
    return _sha256(canonical_json(value).encode("utf-8"))


def _read_sample_interfaces(content):
    try:
        interfaces = _INTERFACES_ADAPTER.validate_python(json.loads(content))
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
        raise GeometryRuntimeValidationError("geometry Worker returned invalid sample interfaces") from exc
    if canonical_json(interfaces).encode("utf-8") != content:
        raise GeometryRuntimeValidationError("geometry Worker returned non-canonical sample interfaces")
    return interfaces


def _sha256(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


__all__ = [
    "GEOMETRY_BUILD_BUDGET",
    "GEOMETRY_QUERY_BUDGET",
    "OcpCadQueryGeometryRuntime",
]
