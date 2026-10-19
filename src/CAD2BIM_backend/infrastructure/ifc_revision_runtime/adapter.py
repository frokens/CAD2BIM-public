"""Parent adapter for supervised, fixed-command IFC revision computation."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING
from CAD2BIM_backend.infrastructure.worker_runtime.timing import ISOLATED_TIMED_BOOTSTRAP

from CAD2BIM_backend.domain.ifc_model import InitializeIfcModelCommand, IfcRevisionInspection, IfcModelIntegrityError
from CAD2BIM_backend.domain.ifc_product_compilation import IfcProductBodyPreflight, IfcProductRevisionEffect, IfcProductCompilationBlock
from CAD2BIM_backend.domain.ifc_products_read import IfcProductsReadError
from CAD2BIM_backend.domain.product_edit import IfcProductsEditPreflightResult, IfcProductsEditRevisionEffect
from CAD2BIM_backend.domain.ifc_project import IfcProjectSummary
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    IfcSpatialCreatedBinding,
    IfcSpatialNodeSummary,
)
from CAD2BIM_backend.infrastructure.ifc_revision_runtime.protocol import (
    MAX_IFC_REVISION_CONTROL_BYTES,
    MAX_IFC_REVISION_INPUT_BYTES,
    MAX_IFC_REVISION_RESULT_BYTES,
    MAX_IFC_PRODUCT_BLOCK_BYTES, MAX_IFC_PRODUCT_STREAM_BYTES,
)
from CAD2BIM_backend.infrastructure.worker_runtime import (
    DarwinSeatbeltSandbox,
    HostWorkerCapacity,
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
from CAD2BIM_backend.ports.ifc_revision_runtime import (
    ApplyIfcSpatialStructureRevisionRequest,
    CompileIfcProductsRevisionRequest,
    EditIfcProductsRevisionRequest,
    ConfigureIfcGeometryBasisRevisionRequest,
    GeneratedIfcRevision,
    IfcGeometryBasisRuntimeBusinessError,
    IfcProjectRevisionEffect,
    IfcProjectRuntimeBusinessError,
    IfcRevisionChunkSource,
    IfcRevisionEffect,
    IfcRevisionRuntimeError,
    IfcRevisionRuntimeRequest,
    IfcRevisionRuntimeResult,
    IfcSpatialRevisionEffect,
    IfcSpatialRuntimeBusinessError,
    InitializeIfcRevisionRequest,
    InspectedIfcRevision,
    InspectExactIfcRevisionRequest,
    UpdateIfcProjectRevisionRequest,
)


if TYPE_CHECKING:
    from CAD2BIM_backend.domain.product_realization_host import ProductRealizationHostError


_GUARD_REGISTRATION: ContextVar[Callable[[int, int], Awaitable[None]] | None] = ContextVar(
    "ifc_revision_guard_registration", default=None,
)


IFC_REVISION_RUNTIME_BUDGET = WorkerProcessBudget(
    wall_seconds=150.0,
    cpu_seconds=150.0,
    rss_bytes=8 * 1024**3,
    scratch_bytes=2 * MAX_IFC_REVISION_INPUT_BYTES + 32 * 1024**2,
    threads=4,
    open_file_descriptors=64,
    stdout_bytes=16 * 1024,
    stderr_bytes=16 * 1024,
    cleanup_seconds=1.0,
)
_CHUNK_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ISOLATED_MODULE_BOOTSTRAP = ISOLATED_TIMED_BOOTSTRAP
_REQUEST_TYPES = (
    CompileIfcProductsRevisionRequest,
    EditIfcProductsRevisionRequest,
    InitializeIfcRevisionRequest,
    ConfigureIfcGeometryBasisRevisionRequest,
    UpdateIfcProjectRevisionRequest,
    ApplyIfcSpatialStructureRevisionRequest,
    InspectExactIfcRevisionRequest,
)


class _PrivateCandidateHandle:
    def __init__(
        self,
        *,
        root: Path,
        path: Path,
        size_bytes: int,
        sha256: str,
        on_close: Callable[["_PrivateCandidateHandle"], None],
    ) -> None:
        self._root = root
        self._path = path
        self._size_bytes = size_bytes
        self._sha256 = sha256
        self._on_close = on_close
        self._closed = False
        self._stream_lock = asyncio.Lock()

    @property
    def size_bytes(self) -> int:
        return self._size_bytes

    @property
    def sha256(self) -> str:
        return self._sha256

    @property
    def closed(self) -> bool:
        return self._closed

    async def iter_chunks(self, *, chunk_size: int = _CHUNK_BYTES):  # type: ignore[no-untyped-def]
        if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size < 1:
            raise ValueError("candidate chunk_size must be a positive integer")
        async with self._stream_lock:
            if self._closed:
                raise IfcRevisionRuntimeError(
                    "IFC_REVISION_CANDIDATE_CLOSED",
                    "IFC revision candidate is closed",
                    phase="runtime",
                    execution_started=True,
                )
            try:
                stream = await asyncio.to_thread(self._path.open, "rb", buffering=0)
            except OSError as exc:
                raise _worker_failed("IFC revision candidate is unavailable") from exc
            digest = hashlib.sha256()
            size = 0
            try:
                while chunk := await asyncio.to_thread(stream.read, chunk_size):
                    digest.update(chunk)
                    size += len(chunk)
                    yield chunk
            finally:
                await asyncio.to_thread(stream.close)
            if size != self._size_bytes or digest.hexdigest() != self._sha256:
                raise _worker_failed("IFC revision candidate changed after validation")

    async def aclose(self) -> None:
        async with self._stream_lock:
            if self._closed:
                return
            self._closed = True
            await asyncio.to_thread(shutil.rmtree, self._root, True)
            self._on_close(self)


class IfcOpenShellRevisionRuntime:
    """Run one closed IFC operation in a fresh supervised child process."""

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
            raise ValueError("IFC revision Python executable must be an absolute executable file")
        self._python = str(executable)
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
                source_root / "CAD2BIM_backend" / "domain" / "run_events.py",
                source_root / "CAD2BIM_backend" / "domain" / "ifc_model.py",
                source_root / "CAD2BIM_backend" / "domain" / "runtime_execution.py",
                source_root / "CAD2BIM_backend" / "domain" / "ifc_geometry_basis.py",
                source_root / "CAD2BIM_backend" / "domain" / "ifc_project.py",
                source_root / "CAD2BIM_backend" / "domain" / "ifc_spatial_structure.py",
                source_root / "CAD2BIM_backend" / "domain" / "canonical.py",
                source_root / "CAD2BIM_backend" / "domain" / "geometry_frame.py",
                source_root / "CAD2BIM_backend" / "domain" / "geometry_interface.py",
                source_root / "CAD2BIM_backend" / "domain" / "geometry_resource.py",
                source_root / "CAD2BIM_backend" / "domain" / "geometry_runtime.py",
                source_root / "CAD2BIM_backend" / "domain" / "geometry_inspection.py",
                source_root / "CAD2BIM_backend" / "domain" / "product_family.py",
                source_root / "CAD2BIM_backend" / "domain" / "product_realization.py",
                source_root / "CAD2BIM_backend" / "domain" / "ifc_product_compilation.py",
                source_root / "CAD2BIM_backend" / "domain" / "product_edit.py",
                source_root / "CAD2BIM_backend" / "domain" / "ifc_products_read.py",
                source_root / "CAD2BIM_backend" / "domain" / "ifc_geometry_construction.py",
                source_root / "CAD2BIM_backend" / "domain" / "product_realization_host.py",
                source_root / "CAD2BIM_backend" / "domain" / "resource_scope.py",
                source_root / "CAD2BIM_backend" / "domain" / "validation_diagnostics.py",
                source_root / "CAD2BIM_backend" / "domain" / "version_usage.py",
                source_root / "CAD2BIM_backend" / "ports" / "__init__.py",
                source_root / "CAD2BIM_backend" / "ports" / "ifc_revision_runtime.py",
                source_root / "CAD2BIM_backend" / "infrastructure" / "__init__.py",
                ifc_directory / "__init__.py",
                ifc_directory / "codec.py",
                ifc_directory / "geometry_basis.py",
                ifc_directory / "project.py",
                ifc_directory / "spatial_structure.py",
                ifc_directory / "placement.py",
                ifc_directory / "product_realization_host.py",
                ifc_directory / "product_compilation.py",
                ifc_directory / "product_edit.py",
                ifc_directory / "product_read.py",
                ifc_directory / "advanced_brep_encoding.py",
                ifc_directory / "parametric_encoding.py",
                runtime_directory / "__init__.py",
                runtime_directory / "child.py",
                runtime_directory / "protocol.py",
                worker_runtime_directory / "__init__.py",
                worker_runtime_directory / "protocol.py",
                worker_runtime_directory / "timing.py",
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
        )
        self._supervisor = supervisor or WorkerProcessSupervisor()
        self._capacity = capacity or HostWorkerCapacity(
            max_concurrent_workers=1,
            capacity_root=capacity_root,
        )
        self._handles: set[_PrivateCandidateHandle] = set()
        self._closed = False

    async def verify_ready(self) -> None:
        if self._closed:
            raise _unavailable("IFC revision runtime is closed")
        try:
            result = await self.execute(
                InitializeIfcRevisionRequest(
                    command=InitializeIfcModelCommand(project_name="IFC revision readiness"),
                    project_global_id="0fA5xY2rjB9eNH2dFsKQv8",
                    header_timestamp=datetime(1970, 1, 1, tzinfo=timezone.utc),
                )
            )
        except IfcRevisionRuntimeError as exc:
            raise IfcRevisionRuntimeError(
                "IFC_REVISION_SERVICE_UNAVAILABLE",
                "IFC revision runtime readiness probe failed",
                phase="runtime",
                execution_started=exc.execution_started,
                retryable=not exc.execution_started,
            ) from exc
        if not isinstance(result, GeneratedIfcRevision):
            raise _worker_failed("IFC revision readiness returned an invalid result")
        try:
            if (
                result.inspection.project_global_id != "0fA5xY2rjB9eNH2dFsKQv8"
                or result.inspection.project_name != "IFC revision readiness"
            ):
                raise _worker_failed("IFC revision readiness returned an invalid model")
        finally:
            await result.candidate.aclose()

    async def execute(
        self,
        request: IfcRevisionRuntimeRequest,
        *,
        source: IfcRevisionChunkSource | None = None,
    ) -> IfcRevisionRuntimeResult:
        from CAD2BIM_backend.domain.runtime_execution import IFC_REVISION_EXECUTION

        async with self._capacity.execution_scope(IFC_REVISION_EXECUTION):
            return await self._execute(request, source=source)

    @asynccontextmanager
    async def invocation_scope(
        self, policy, *, deadline: float | None = None,
        on_guard_registered: Callable[[int, int], Awaitable[None]] | None = None,
    ):
        token = _GUARD_REGISTRATION.set(on_guard_registered)
        try:
            async with self._capacity.invocation_scope(policy, deadline=deadline) as invocation:
                yield invocation
        finally:
            _GUARD_REGISTRATION.reset(token)

    async def _execute(
        self,
        request: IfcRevisionRuntimeRequest,
        *,
        source: IfcRevisionChunkSource | None,
    ) -> IfcRevisionRuntimeResult:
        if self._closed:
            raise _unavailable("IFC revision runtime is closed")
        if not isinstance(request, _REQUEST_TYPES):
            raise TypeError("request must be a closed IFC revision runtime request")
        if isinstance(request, InitializeIfcRevisionRequest):
            if source is not None:
                raise ValueError("initialize must not receive an exact source")
        elif source is None:
            raise ValueError(f"{request.kind} requires an exact source")

        capacity_fd: int | None = None
        descriptors: list[int] = []
        root: Path | None = None
        transferred_root = False
        try:
            lease = await self._capacity.admit_current_execution()
            capacity_fd = lease.descriptor
            deadline = lease.execution_deadline
            assert capacity_fd is not None and deadline is not None
            root = Path(tempfile.mkdtemp(prefix="cad2bim-ifc-revision-"))
            os.chmod(root, 0o700)

            source_payload: dict[str, Any] | None = None
            source_fd: int | None = None
            if source is not None:
                source_fd, source_payload = await self._stage_source(root, source)
                descriptors.append(source_fd)
            candidate_path: Path | None = None
            candidate_write_fd: int | None = None
            candidate_read_fd: int | None = None
            if not isinstance(request, InspectExactIfcRevisionRequest):
                candidate_path = root / "candidate.ifc"
                candidate_write_fd = _open_write_once(candidate_path)
                candidate_read_fd = _open_read_only(candidate_path)
                descriptors.extend((candidate_write_fd, candidate_read_fd))

            control_path = root / "control.bin"
            control = self._control_payload(request, source_payload)
            if isinstance(request, InspectExactIfcRevisionRequest) and request.projection == "products":
                control["deadline_monotonic"] = deadline
            block_write_fd: int | None = None
            if isinstance(request, (CompileIfcProductsRevisionRequest, EditIfcProductsRevisionRequest)):
                block_read_fd, block_write_fd = os.pipe()
                descriptors.extend((block_read_fd, block_write_fd))
                control["product_blocks_fd"] = block_read_fd
            with os.fdopen(_open_write_once(control_path), "wb", buffering=0) as stream:
                write_json_frame(
                    stream,
                    control,
                    limit=MAX_IFC_REVISION_CONTROL_BYTES,
                )
            control_fd = _open_read_only(control_path)
            control_path.unlink()
            result_path = root / "result.bin"
            terminal_path = root / "terminal.bin"
            result_fd = _open_write_once(result_path)
            terminal_fd = _open_write_once(terminal_path)
            descriptors.extend((control_fd, result_fd, terminal_fd))

            environment = _environment(root)
            environment.update(
                {
                    "CAD2BIM_IFC_REVISION_CONTROL_FD": str(control_fd),
                    "CAD2BIM_IFC_REVISION_RESULT_FD": str(result_fd),
                    "CAD2BIM_IFC_REVISION_TERMINAL_FD": str(terminal_fd),
                }
            )
            if candidate_write_fd is not None and candidate_read_fd is not None:
                environment.update(
                    {
                        "CAD2BIM_IFC_REVISION_CANDIDATE_WRITE_FD": str(candidate_write_fd),
                        "CAD2BIM_IFC_REVISION_CANDIDATE_READ_FD": str(candidate_read_fd),
                    }
                )

            launch = WorkerLaunchSpec(
                    command=self._sandbox.command(
                        scratch=root,
                        metadata_read_fds=(source_fd,) if source_fd is not None and isinstance(
                            request, (InspectExactIfcRevisionRequest, CompileIfcProductsRevisionRequest, EditIfcProductsRevisionRequest),
                        ) else (),
                        child_args=(
                            "-I",
                            "-B",
                            "-c",
                            _ISOLATED_MODULE_BOOTSTRAP,
                            str(self._source_root),
                            "CAD2BIM_backend.infrastructure.ifc_revision_runtime.child",
                        ),
                    ),
                    cwd=root,
                    env=environment,
                    pass_fds=tuple(fd for fd in descriptors if fd != block_write_fd),
                    capacity_fds=(capacity_fd,),
                    scratch=root,
                )
            run = self._supervisor.run(
                launch,
                budget=IFC_REVISION_RUNTIME_BUDGET,
                deadline=deadline,
                on_guard_registered=_GUARD_REGISTRATION.get(),
            )
            if isinstance(request, (CompileIfcProductsRevisionRequest, EditIfcProductsRevisionRequest)):
                assert block_write_fd is not None
                # The producer owns this fd from here, including cancellation.
                descriptors.remove(block_write_fd)
                terminal = await self._run_product_stream(run, request, block_write_fd)
            else:
                terminal = await run
            _close_all(descriptors)
            descriptors.clear()
            child_terminal = _read_frame(terminal_path, MAX_STATUS_BYTES)
            if terminal.returncode != 0 and child_terminal.get("status") == "succeeded":
                raise _worker_failed("worker exited unsuccessfully after its terminal")
            if child_terminal.get("status") != "succeeded":
                raise _child_error(child_terminal, request)
            if (
                set(child_terminal) != {"status", "kind", "source_sha256"}
                or child_terminal.get("kind") != request.kind
            ):
                raise _worker_failed("worker returned an invalid terminal attestation")
            expected_source_sha = None if source_payload is None else source_payload["sha256"]
            if child_terminal.get("source_sha256") != expected_source_sha:
                raise _worker_failed("worker did not attest the exact IFC source")

            result_payload = _read_frame(result_path, MAX_IFC_REVISION_RESULT_BYTES)
            inspection = IfcRevisionInspection.model_validate(result_payload.get("inspection"))
            if isinstance(request, InspectExactIfcRevisionRequest):
                from CAD2BIM_backend.domain.ifc_geometry_basis import IfcRepresentationContextCatalog
                from CAD2BIM_backend.domain.ifc_spatial_structure import IfcSpatialStructurePage
                from pydantic import TypeAdapter
                from CAD2BIM_backend.domain.product_realization_host import ResolvedCurrentIfcHostFact

                from CAD2BIM_backend.domain.ifc_products_read import IfcProductsReadRuntimeResult

                extra_key = {"model": None, "contexts": "contexts", "spatial": "spatial_page", "host_facts": "host_facts", "body": "body", "products": "products", "edit_targets": "edit_targets"}[request.projection]
                expected_keys = {"inspection"} if extra_key is None else {"inspection", extra_key}
                if set(result_payload) != expected_keys:
                    raise _worker_failed("inspect worker returned an invalid result shape")
                return InspectedIfcRevision(
                    inspection=inspection,
                    contexts=(IfcRepresentationContextCatalog.model_validate(result_payload["contexts"]) if request.projection == "contexts" else None),
                    spatial_page=(IfcSpatialStructurePage.model_validate(result_payload["spatial_page"]) if request.projection == "spatial" else None),
                    host_facts=(TypeAdapter(tuple[ResolvedCurrentIfcHostFact, ...]).validate_python(result_payload["host_facts"]) if request.projection == "host_facts" else None),
                    body=(IfcProductBodyPreflight.model_validate(result_payload["body"]) if request.projection == "body" else None),
                    products=(IfcProductsReadRuntimeResult.model_validate(result_payload["products"]) if request.projection == "products" else None),
                    edit_targets=(IfcProductsEditPreflightResult.model_validate(result_payload["edit_targets"]) if request.projection == "edit_targets" else None),
                )

            if set(result_payload) != {"candidate", "inspection", "effect"}:
                raise _worker_failed("mutation worker returned an invalid result shape")

            if candidate_path is None:
                raise _worker_failed("worker omitted its candidate capability")
            candidate_metadata = result_payload.get("candidate")
            if not isinstance(candidate_metadata, dict):
                raise _worker_failed("worker omitted candidate metadata")
            size_bytes = candidate_metadata.get("size_bytes")
            sha256 = candidate_metadata.get("sha256")
            if (
                not isinstance(size_bytes, int)
                or isinstance(size_bytes, bool)
                or not 1 <= size_bytes <= MAX_IFC_REVISION_INPUT_BYTES
                or not isinstance(sha256, str)
                or _SHA256.fullmatch(sha256) is None
            ):
                raise _worker_failed("worker returned invalid candidate metadata")
            actual_size, actual_sha256 = await asyncio.to_thread(_file_digest, candidate_path)
            if (actual_size, actual_sha256) != (size_bytes, sha256):
                raise _worker_failed("candidate bytes do not match worker metadata")
            if inspection.size_bytes != size_bytes or inspection.sha256 != sha256:
                raise _worker_failed("candidate inspection does not match exact bytes")
            effect = _parse_effect(request, result_payload.get("effect"))
            if (
                isinstance(effect, IfcProjectRevisionEffect)
                and (
                    effect.project.global_id != inspection.project_global_id
                    or effect.project.name != inspection.project_name
                )
            ):
                raise _worker_failed("Project effect does not match candidate inspection")
            if (
                isinstance(effect, IfcSpatialRevisionEffect)
                and effect.project_global_id != inspection.project_global_id
            ):
                raise _worker_failed("spatial effect does not match candidate inspection")
            os.chmod(candidate_path, 0o400)
            handle = _PrivateCandidateHandle(
                root=root,
                path=candidate_path,
                size_bytes=size_bytes,
                sha256=sha256,
                on_close=self._handles.discard,
            )
            self._handles.add(handle)
            transferred_root = True
            return GeneratedIfcRevision(candidate=handle, inspection=inspection, effect=effect)
        except (IfcRevisionRuntimeError, IfcProductsReadError):
            raise
        except TimeoutError as exc:
            raise _unavailable("IFC revision worker capacity is busy") from exc
        except (WorkerCapacityBusy, WorkerCapacityUnavailable) as exc:
            diagnostic = self._capacity.failure_diagnostic(exc)
            raise IfcRevisionRuntimeError(
                "IFC_REVISION_SERVICE_UNAVAILABLE", _safe_message(str(exc)), phase="runtime",
                execution_started=bool(diagnostic["execution_started"]), diagnostic=diagnostic,
                retryable=isinstance(exc, WorkerCapacityBusy) and exc.retryable,
            ) from exc
        except WorkerSupervisorError as exc:
            if exc.code == "WORKER_TIMEOUT":
                code, message = "IFC_REVISION_TIMEOUT", "IFC revision exceeded its time budget"
            elif exc.code == "WORKER_RESOURCE_LIMIT":
                code, message = "IFC_REVISION_RESOURCE_LIMIT", _safe_message(exc.message)
            else:
                raise IfcRevisionRuntimeError(
                    "IFC_REVISION_SERVICE_UNAVAILABLE", _safe_message(exc.message),
                    phase="execution" if exc.execution_started else "runtime",
                    execution_started=exc.execution_started, diagnostic=exc.diagnostic(),
                    retryable=not exc.execution_started,
                ) from exc
            raise IfcRevisionRuntimeError(
                code,
                message,
                phase="execution" if exc.execution_started else "runtime",
                execution_started=exc.execution_started,
                diagnostic=exc.diagnostic(),
                retryable=True,
            ) from exc
        except (OSError, WorkerProtocolError, ValueError, TypeError) as exc:
            raise _unavailable("IFC revision worker infrastructure is unavailable") from exc
        finally:
            _close_all(descriptors)
            if root is not None and not transferred_root:
                await asyncio.to_thread(shutil.rmtree, root, True)

    async def close(self) -> None:
        self._closed = True
        handles = tuple(self._handles)
        if handles:
            await asyncio.gather(*(handle.aclose() for handle in handles))

    async def _run_product_stream(self, run: Awaitable[Any], request: CompileIfcProductsRevisionRequest | EditIfcProductsRevisionRequest,
                                  descriptor: int) -> Any:
        from CAD2BIM_backend.infrastructure.worker_runtime.protocol import AsyncWorkerChannel, encode_json_frame, frame_json_payload
        async def produce() -> None:
            channel = AsyncWorkerChannel(descriptor, descriptor)
            count = total = 0
            try:
                async for block in request.blocks:
                    if not isinstance(block, IfcProductCompilationBlock):
                        raise TypeError("product stream must yield typed compilation blocks")
                    canonical_bytes = sum(len(source.geometry.payload) for item in block.items
                        for source in item.fragment_sources if source.geometry is not None)
                    if canonical_bytes > MAX_IFC_PRODUCT_BLOCK_BYTES:
                        raise IfcRevisionRuntimeError("IFC_PRODUCT_INPUT_BUDGET_EXCEEDED",
                            "product block canonical bytes exceed the block limit", phase="input",
                            execution_started=True, diagnostic={"stage": "ifc_block_transport", "block_index": count})
                    try:
                        # Pydantic emits the validated block directly. Avoid a
                        # second full Python object tree and JSON traversal.
                        payload = (b'{"status":"block","index":' + str(count).encode("ascii")
                                   + b',"block":' + block.model_dump_json().encode("utf-8") + b'}')
                        frame = frame_json_payload(payload, limit=MAX_IFC_PRODUCT_BLOCK_BYTES)
                        del payload
                    except WorkerProtocolError as exc:
                        raise IfcRevisionRuntimeError("IFC_PRODUCT_INPUT_BUDGET_EXCEEDED",
                            "product block encoded bytes exceed the block limit", phase="input",
                            execution_started=True, diagnostic={"stage": "ifc_block_transport", "block_index": count}) from exc
                    total += len(frame)
                    if total > MAX_IFC_PRODUCT_STREAM_BYTES:
                        raise IfcRevisionRuntimeError("IFC_PRODUCT_INPUT_BUDGET_EXCEEDED",
                            "product stream exceeds its cumulative byte limit", phase="input",
                            execution_started=True, diagnostic={"stage": "ifc_block_transport", "block_index": count})
                    await channel.write(frame)
                    del frame, block
                    count += 1
                end = encode_json_frame({"status": "end", "count": count}, limit=MAX_IFC_PRODUCT_BLOCK_BYTES)
                if total + len(end) > MAX_IFC_PRODUCT_STREAM_BYTES:
                    raise IfcRevisionRuntimeError("IFC_PRODUCT_INPUT_BUDGET_EXCEEDED",
                        "product stream terminator exceeds its cumulative byte limit", phase="input",
                        execution_started=True, diagnostic={"stage": "ifc_block_transport", "block_index": count})
                await channel.write(end)
            finally:
                os.close(descriptor)
                close = getattr(request.blocks, "aclose", None)
                if close is not None:
                    await close()
        producer = asyncio.create_task(produce())
        try:
            # The supervisor must stay in the task owning the execution lease.
            # A producer failure closes the pipe, so the child observes EOF and
            # stops; an early child failure cancels a backpressured producer.
            terminal = await run
            if producer.done() and not producer.cancelled():
                error = producer.exception()
                if error is not None and not isinstance(error, BrokenPipeError):
                    raise error
            if terminal.returncode == 0:
                await producer
            return terminal
        finally:
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    async def _stage_source(
        self,
        root: Path,
        source: IfcRevisionChunkSource,
    ) -> tuple[int, dict[str, Any]]:
        size_bytes = source.size_bytes
        sha256 = source.sha256
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or not 1 <= size_bytes <= MAX_IFC_REVISION_INPUT_BYTES
            or not isinstance(sha256, str)
            or _SHA256.fullmatch(sha256) is None
        ):
            raise IfcRevisionRuntimeError(
                "IFC_REVISION_SOURCE_INVALID",
                "IFC revision source metadata is invalid",
                phase="input_validation",
                execution_started=False,
            )
        path = root / "source.ifc"
        descriptor = _open_write_once(path)
        digest = hashlib.sha256()
        total = 0
        try:
            async for chunk in source.iter_chunks(chunk_size=_CHUNK_BYTES):
                if not isinstance(chunk, bytes) or not chunk:
                    raise IfcRevisionRuntimeError(
                        "IFC_REVISION_SOURCE_INVALID",
                        "IFC revision source yielded an invalid chunk",
                        phase="input_validation",
                        execution_started=False,
                    )
                total += len(chunk)
                if total > size_bytes or total > MAX_IFC_REVISION_INPUT_BYTES:
                    raise IfcRevisionRuntimeError(
                        "IFC_REVISION_SOURCE_INVALID",
                        "IFC revision source exceeds its declared size",
                        phase="input_validation",
                        execution_started=False,
                    )
                digest.update(chunk)
                await asyncio.to_thread(_write_all, descriptor, chunk)
            await asyncio.to_thread(os.fsync, descriptor)
        finally:
            os.close(descriptor)
        if total != size_bytes or digest.hexdigest() != sha256:
            raise IfcRevisionRuntimeError(
                "IFC_REVISION_SOURCE_INVALID",
                "IFC revision source does not match its declared metadata",
                phase="input_validation",
                execution_started=False,
            )
        source_fd = _open_read_only(path)
        path.unlink()
        return source_fd, {"fd": source_fd, "size_bytes": size_bytes, "sha256": sha256}

    @staticmethod
    def _control_payload(
        request: IfcRevisionRuntimeRequest,
        source: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": request.kind,
            "source": source,
        }
        if not isinstance(request, InspectExactIfcRevisionRequest):
            payload["command"] = request.command.model_dump(mode="json", exclude_none=True)
        if isinstance(request, InitializeIfcRevisionRequest):
            payload["project_global_id"] = request.project_global_id
            payload["header_timestamp"] = request.header_timestamp.isoformat()
        elif isinstance(request, InspectExactIfcRevisionRequest):
            payload["expected_project_global_id"] = request.expected_project_global_id
            payload["expected_project_name"] = request.expected_project_name
            payload["projection"] = request.projection
            if request.spatial_query is not None:
                payload["spatial_query"] = request.spatial_query.model_dump(mode="json")
            if request.host_request is not None:
                payload["host_request"] = request.host_request.model_dump(mode="json")
            if request.products_request is not None:
                payload["products_request"] = request.products_request.model_dump(mode="json")
            if request.edit_targets_request is not None:
                payload["edit_targets_request"] = request.edit_targets_request.model_dump(mode="json")
        return payload


def _parse_effect(
    request: IfcRevisionRuntimeRequest,
    value: Any,
) -> IfcRevisionEffect | None:
    if isinstance(request, EditIfcProductsRevisionRequest):
        if not isinstance(value, dict) or value.get("kind") != "products_edit":
            raise _worker_failed("product editor returned an invalid typed effect")
        return IfcProductsEditRevisionEffect.model_validate({key: item for key, item in value.items() if key != "kind"})
    if isinstance(request, CompileIfcProductsRevisionRequest):
        if not isinstance(value, dict) or value.get("kind") != "products":
            raise _worker_failed("product compiler returned an invalid typed effect")
        return IfcProductRevisionEffect.model_validate({key: item for key, item in value.items() if key != "kind"})
    if isinstance(request, UpdateIfcProjectRevisionRequest):
        if not isinstance(value, dict) or value.get("kind") != "project":
            raise _worker_failed("project worker returned an invalid typed effect")
        return IfcProjectRevisionEffect(
            project=IfcProjectSummary.model_validate(value.get("project"))
        )
    if isinstance(request, ApplyIfcSpatialStructureRevisionRequest):
        if not isinstance(value, dict) or value.get("kind") != "spatial":
            raise _worker_failed("spatial worker returned an invalid typed effect")
        created = value.get("created")
        affected = value.get("affected_global_ids")
        spatial_summary = value.get("spatial_summary")
        if (
            not isinstance(created, list)
            or not isinstance(affected, list)
            or not isinstance(spatial_summary, list)
        ):
            raise _worker_failed("spatial worker returned an invalid typed effect")
        return IfcSpatialRevisionEffect(
            project_global_id=value.get("project_global_id"),
            created=tuple(IfcSpatialCreatedBinding.model_validate(item) for item in created),
            affected_global_ids=tuple(affected),
            spatial_summary=tuple(
                IfcSpatialNodeSummary.model_validate(item) for item in spatial_summary
            ),
        )
    if value is not None:
        raise _worker_failed("worker returned an unexpected typed effect")
    return None


def _read_frame(path: Path, limit: int) -> dict[str, Any]:
    try:
        with path.open("rb", buffering=0) as stream:
            return read_json_frame(stream, limit=limit)
    except (OSError, WorkerProtocolError) as exc:
        raise _worker_failed("IFC revision worker returned an invalid bounded frame") from exc


def _child_error(
    terminal: dict[str, Any],
    request: IfcRevisionRuntimeRequest,
) -> IfcRevisionRuntimeError | IfcModelIntegrityError | ProductRealizationHostError:
    if isinstance(request, InspectExactIfcRevisionRequest) and terminal.get("phase") == "integrity":
        return IfcModelIntegrityError(_safe_message(str(terminal.get("message", "exact IFC failed integrity validation"))))
    diagnostic = terminal.get("diagnostic")
    error_type: type[IfcRevisionRuntimeError] = IfcRevisionRuntimeError
    if terminal.get("phase") == "business":
        if isinstance(request, InspectExactIfcRevisionRequest) and request.projection == "products":
            from CAD2BIM_backend.domain.ifc_products_read import IfcProductsReadError

            return IfcProductsReadError(str(terminal["code"]), _safe_message(str(terminal["message"])))
        if isinstance(request, InspectExactIfcRevisionRequest) and request.projection == "host_facts":
            from CAD2BIM_backend.domain.product_realization_host import ProductRealizationHostError, ProductRealizationHostErrorCode
            return ProductRealizationHostError(
                ProductRealizationHostErrorCode(terminal["code"]),
                _safe_message(str(terminal["message"])),
            )
        if isinstance(request, ConfigureIfcGeometryBasisRevisionRequest):
            error_type = IfcGeometryBasisRuntimeBusinessError
        elif isinstance(request, UpdateIfcProjectRevisionRequest):
            error_type = IfcProjectRuntimeBusinessError
        elif isinstance(request, ApplyIfcSpatialStructureRevisionRequest):
            error_type = IfcSpatialRuntimeBusinessError
    return error_type(
        str(terminal.get("code", "IFC_REVISION_WORKER_FAILED")),
        _safe_message(str(terminal.get("message", "IFC revision worker failed"))),
        phase=str(terminal.get("phase", "execution")),
        execution_started=bool(terminal.get("execution_started", True)),
        diagnostic=diagnostic if isinstance(diagnostic, dict) else None,
        retryable=bool(terminal.get("retryable", False)),
    )


def _worker_failed(message: str) -> IfcRevisionRuntimeError:
    return IfcRevisionRuntimeError(
        "IFC_REVISION_WORKER_FAILED",
        message,
        phase="runtime",
        execution_started=True,
        retryable=True,
    )


def _unavailable(message: str, execution_started: bool = False) -> IfcRevisionRuntimeError:
    return IfcRevisionRuntimeError(
        "IFC_REVISION_SERVICE_UNAVAILABLE",
        _safe_message(message),
        phase="runtime",
        execution_started=execution_started,
        retryable=not execution_started,
    )


def _safe_message(message: str) -> str:
    value = re.sub(r"/(?:Users|home|private|var|tmp)/[^\s:'\"]+", "<host-path>", message)
    return value.encode("utf-8")[:2_000].decode("utf-8", errors="ignore")


def _environment(root: Path) -> dict[str, str]:
    thread_limit = str(IFC_REVISION_RUNTIME_BUDGET.threads)
    return {
        "BLIS_NUM_THREADS": thread_limit,
        "CAD2BIM_WORKER_TIMING": "1",
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


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write while staging exact IFC source")
        remaining = remaining[written:]


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _close_all(descriptors: list[int]) -> None:
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError:
            pass


__all__ = ["IFC_REVISION_RUNTIME_BUDGET", "IfcOpenShellRevisionRuntime"]
