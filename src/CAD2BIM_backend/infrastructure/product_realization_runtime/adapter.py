"""Controlled-local adapter for one exact Product Realization Program."""

from __future__ import annotations

from CAD2BIM_backend.domain.runtime_execution import GEOMETRY_CODEC_EXECUTION, PRODUCT_REALIZATION_EXECUTION, RuntimeExecutionPolicy

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, replace
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    GeometryNormalizeRequest,
    GeometryRuntimeError,
    GeometryRuntimeResourceLimitError,
    GeometryRuntimeUnavailableError,
    GeometryRuntimeValidationError,
    GeometrySourceFormat,
)
from CAD2BIM_backend.domain.product_realization import (
    PRODUCT_REALIZATION_CPU_SECONDS,
    PRODUCT_REALIZATION_RSS_BYTES,
    PRODUCT_REALIZATION_SCRATCH_BYTES,
    PRODUCT_REALIZATION_THREADS,
    PRODUCT_REALIZATION_WALL_SECONDS,
    ProductRealizationGeometryExecution,
    ProductGeometryInstanceReceipt,
    complete_product_realization_instantiation,
    geometry_source_request_parameters,
    geometry_source_to_fragment,
    ProductRealizationRequest,
    ProductRealizationResult,
    ProductRealizationOutputNeed,
    ProductRealizationStatus,
    MAX_PRODUCT_REALIZATION_BATCH_ITEMS,
    SolvedProductRealizationProposal,
    ResolvedMappedGeometryBinding,
    validate_product_realization_result_integrity,
    validate_product_realization_instantiation_result,
    product_realization_binding_digest,
    _prepare_geometry_binding_parameters,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution import (
    GEOMETRY_CODEC_BUDGET,
    OcpGeometryCodecExecutor,
)
from CAD2BIM_backend.infrastructure.execution_observability import observe_execution_phase
from CAD2BIM_backend.infrastructure.product_realization_runtime.protocol import (
    MAX_PRODUCT_REALIZATION_TERMINAL_BYTES,
    ProductRealizationPreviewGeometryMetadata,
    materialize_batch,
    _geometry_source_request_key,
    product_window_sizes,
    batch_invocation_frame,
    normalize_batch_output_needs,
    validate_batch_result_frame,
    MAX_PRODUCT_REALIZATION_CONTROL_BYTES,
    MAX_PRODUCT_BATCH_RESULT_FRAME_BYTES,
    MAX_PRODUCT_BATCH_OUTPUT_BYTES,
    MAX_PRODUCT_BATCH_INPUT_BYTES,
)
from CAD2BIM_backend.infrastructure.worker_runtime.protocol import AsyncWorkerChannel, WorkerProtocolError, encode_json_frame
from CAD2BIM_backend.infrastructure.worker_runtime.timing import ISOLATED_TIMED_BOOTSTRAP
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
from CAD2BIM_backend.ports.product_realization_runtime import (
    ProductGeometrySourceCache,
    ProductRealizationRuntimeUnavailableError,
    ProductRealizationRuntimeResourceLimitError,
    ProductRealizationRuntimeValidationError,
)


PRODUCT_REALIZATION_BUDGET = WorkerProcessBudget(
    wall_seconds=PRODUCT_REALIZATION_WALL_SECONDS,
    cpu_seconds=PRODUCT_REALIZATION_CPU_SECONDS,
    rss_bytes=PRODUCT_REALIZATION_RSS_BYTES,
    scratch_bytes=PRODUCT_REALIZATION_SCRATCH_BYTES,
    threads=PRODUCT_REALIZATION_THREADS,
    open_file_descriptors=64,
    stdout_bytes=16 * 1024,
    stderr_bytes=16 * 1024,
    cleanup_seconds=1.0,
)
_ISOLATED_MODULE_BOOTSTRAP = ISOLATED_TIMED_BOOTSTRAP
_GUARD_REGISTRATION = ContextVar("product_realization_guard_registration", default=None)


@dataclass(frozen=True, slots=True)
class _PendingGeometrySource:
    """A same-window source awaiting its independent Codec result."""

    metadata: ProductRealizationPreviewGeometryMetadata
    origin: tuple[int, str]
    parameters: dict

    @property
    def sha256(self):
        return self.metadata.sha256

    @property
    def summary(self):
        return self.metadata.summary


def _source_metadata(geometry):
    if isinstance(geometry, _PendingGeometrySource):
        return geometry.metadata
    return ProductRealizationPreviewGeometryMetadata(format=geometry.format,
        content_type=geometry.content_type, byte_length=len(geometry.payload),
        sha256=geometry.sha256, summary=geometry.summary)




class OcpCadQueryProductRealizationRuntime:
    """Execute the family-owned program in one supervised OCP child."""

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
            raise ValueError("product realization Python executable must be an absolute file")
        self._python = str(executable)
        runtime_directory = Path(__file__).resolve().parent
        infrastructure_directory = runtime_directory.parent
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
                source_root / "CAD2BIM_backend" / "domain" / "geometry_resource.py",
                source_root / "CAD2BIM_backend" / "domain" / "geometry_runtime.py",
                source_root / "CAD2BIM_backend" / "domain" / "geometry_inspection.py",
                source_root / "CAD2BIM_backend" / "domain" / "ifc_geometry_basis.py",
                source_root / "CAD2BIM_backend" / "domain" / "ifc_geometry_construction.py",
                source_root / "CAD2BIM_backend" / "domain" / "validation_diagnostics.py",
                source_root / "CAD2BIM_backend" / "domain" / "version_usage.py",
                source_root / "CAD2BIM_backend" / "domain" / "product_family.py",
                source_root / "CAD2BIM_backend" / "domain" / "product_realization.py",
                source_root / "CAD2BIM_backend" / "domain" / "resource_scope.py",
                source_root / "CAD2BIM_backend" / "domain" / "run_events.py",
                source_root / "CAD2BIM_backend" / "domain" / "runtime_execution.py",
                infrastructure_directory / "__init__.py",
                infrastructure_directory / "geometry_runtime" / "__init__.py",
                infrastructure_directory / "geometry_runtime" / "build_context.py",
                infrastructure_directory / "geometry_runtime" / "build_child.py",
                infrastructure_directory / "geometry_runtime" / "modeling_capabilities.py",
                infrastructure_directory / "geometry_runtime" / "modeling_execution.py",
                infrastructure_directory / "geometry_runtime" / "modeling_environment.py",
                infrastructure_directory / "geometry_runtime" / "codec.py",
                infrastructure_directory / "geometry_runtime" / "user_geometry.py",
                runtime_directory / "__init__.py",
                runtime_directory / "adapter.py",
                runtime_directory / "child.py",
                runtime_directory / "protocol.py",
                infrastructure_directory / "source_text" / "__init__.py",
                infrastructure_directory / "source_text" / "source_bundle.py",
                infrastructure_directory / "worker_runtime" / "__init__.py",
                infrastructure_directory / "worker_runtime" / "protocol.py",
                infrastructure_directory / "worker_runtime" / "timing.py",
                infrastructure_directory / "worker_runtime" / "python_bundle.py",
                infrastructure_directory / "worker_runtime" / "python_user_code.py",
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
        self._codec = codec_executor or OcpGeometryCodecExecutor(
            budget=GEOMETRY_CODEC_BUDGET,
            python_executable=self._python,
            capacity=self._capacity,
        )
        self._closed = False

    def execution_scope(self, policy: RuntimeExecutionPolicy = PRODUCT_REALIZATION_EXECUTION, *, deadline: float | None = None):
        return self._codec.execution_scope(policy, deadline=deadline)

    @asynccontextmanager
    async def invocation_scope(self, policy: RuntimeExecutionPolicy, *, deadline: float | None = None, on_guard_registered=None):
        token = _GUARD_REGISTRATION.set(on_guard_registered)
        try:
            async with self._codec.invocation_scope(policy, deadline=deadline, on_guard_registered=on_guard_registered) as invocation:
                yield invocation
        finally:
            _GUARD_REGISTRATION.reset(token)

    def release_execution(self) -> None:
        self._codec.release_execution()

    async def verify_ready(self) -> None:
        self._require_open()
        await self.realize(_readiness_request())

    async def realize(self, request: ProductRealizationRequest) -> ProductRealizationResult:
        return (await self.realize_geometry(request)).realization

    async def precheck_batch(self, requests: Sequence[ProductRealizationRequest], *, window_size: int | None = None):
        """Run the bounded numeric-only lifecycle; no BREP crosses its boundary."""
        async for execution in self.realize_batch(requests, output_need=ProductRealizationOutputNeed.INTERFACES, _window_size=window_size):
            yield execution.realization

    async def instantiate_batch(self, requests, prechecks, *, output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS, source_cache: ProductGeometrySourceCache | None = None, window_size: int | None = None):
        """Instantiate unchanged successful results retained from precheck_batch.

        The child revalidates the transmitted precheck; the host verifies that
        only the execution stage and checked geometry receipts were added.
        """
        try:
            product_window_sizes(len(requests), window_size)
        except ValueError as exc:
            raise ProductRealizationRuntimeValidationError("REALIZATION_BINDING_INVALID", str(exc), phase="preflight") from exc
        if len(requests) != len(prechecks):
            raise ProductRealizationRuntimeValidationError("REALIZATION_BINDING_INVALID", "Prechecks do not match the input page", phase="preflight")
        for index, (request, result) in enumerate(zip(requests, prechecks, strict=True)):
            if result.execution_stage != "interface_constraints_evaluated" or result.frame_check_status != "PASS":
                raise ProductRealizationRuntimeValidationError("REALIZATION_BINDING_INVALID", "Instantiation requires every precheck to pass", phase="preflight", item_index=index)
            if (result.binding_digest != product_realization_binding_digest(request)
                    or result.source_bundle_sha256 != request.source_bundle_sha256
                    or result.normalized_public_parameters != request.invocation.public_parameters
                    or result.invocation != request.invocation or result.policy != request.policy
                    or result.family_contract_digest != request.family_contract_digest):
                raise ProductRealizationRuntimeValidationError("REALIZATION_BINDING_INVALID", "Precheck changed its exact request association", phase="preflight", item_index=index)
        if source_cache is not None and output_need is not ProductRealizationOutputNeed.LOCAL_FRAGMENTS:
            raise ProductRealizationRuntimeValidationError("REALIZATION_BINDING_INVALID", "Source reuse requires local fragments", phase="preflight")
        keys, reused, source_transforms = [], [], []
        for request, precheck in zip(requests, prechecks, strict=True):
            bindings = {binding.binding_key: binding for binding in request.geometry_bindings}
            row_keys, row_sources, row_transforms = {}, {}, {}
            for evaluation in precheck.interface_evaluations:
                binding = bindings[evaluation.binding_key]
                key = _geometry_source_request_key(binding, evaluation)
                source = source_cache.get(key) if source_cache is not None else None
                transform = None
                if source is not None:
                    transform = geometry_source_to_fragment(binding, source[2], evaluation.normalized_parameters,
                                                            uniform_scale=evaluation.uniform_scale)
                    if transform is None:
                        key = _geometry_source_request_key(binding, evaluation, exact=True)
                        source = source_cache.get(key)
                        if source is not None:
                            transform = geometry_source_to_fragment(binding, source[2], evaluation.normalized_parameters,
                                                                    uniform_scale=evaluation.uniform_scale)
                            if transform is None:
                                raise ProductRealizationRuntimeValidationError("REALIZATION_OUTPUT_INVALID", "Exact cached source parameters differ from their request", phase="output_validation")
                row_keys[evaluation.fragment_key] = (key, binding.ifc_representation_route)
                if source is not None:
                    geometry, construction, _ = source
                    if (binding.ifc_representation_route.value == "parametric") != (construction is not None):
                        raise ProductRealizationRuntimeValidationError("REALIZATION_OUTPUT_INVALID", "Cached source construction differs from its exact route", phase="output_validation")
                    row_sources[evaluation.fragment_key] = (geometry, construction)
                    row_transforms[evaluation.fragment_key] = transform
            keys.append(row_keys)
            reused.append(row_sources)
            source_transforms.append(row_transforms)
        if requests and all(len(sources) == len(precheck.interface_evaluations)
                            for sources, precheck in zip(reused, prechecks, strict=True)):
            with observe_execution_phase("product_realization", "checked_source_reuse", details={"item_count": len(requests), "fragment_count": sum(map(len, reused))}):
                for request, precheck, sources, transforms in zip(requests, prechecks, reused, source_transforms, strict=True):
                    receipts = tuple(ProductGeometryInstanceReceipt.from_evaluation(evaluation,
                        geometry_sha256=sources[evaluation.fragment_key][0].sha256,
                        geometry_summary=sources[evaluation.fragment_key][0].summary)
                        for evaluation in precheck.interface_evaluations)
                    result = complete_product_realization_instantiation(precheck, receipts)
                    validate_product_realization_instantiation_result(request, precheck, result, output_need=output_need)
                    yield ProductRealizationGeometryExecution(realization=result, output_need=output_need,
                        instance_geometries={key: value[0] for key, value in sources.items()},
                        instance_constructions={key: value[1].model_copy(deep=True) for key, value in sources.items() if value[1] is not None},
                        instance_source_transforms=transforms)
            return
        index = 0
        async for execution in self.realize_batch(requests, output_need=output_need, _prechecks=prechecks, _reused=reused, _source_keys=keys, _source_transforms=source_transforms, _window_size=window_size, _source_cache=source_cache):
            if source_cache is not None:
                for fragment_key, (key, route) in keys[index].items():
                    if fragment_key not in reused[index]:
                        source_cache.put(key, execution.instance_geometries[fragment_key],
                            (execution.instance_constructions or {}).get(fragment_key), route,
                            next(item.normalized_parameters for item in prechecks[index].interface_evaluations if item.fragment_key == fragment_key))
            if source_transforms[index]:
                execution = execution.model_copy(update={"instance_source_transforms": source_transforms[index]})
            index += 1
            yield execution

    async def realize_geometry(
        self,
        request: ProductRealizationRequest,
        *,
        include_instances: bool = False,
    ) -> ProductRealizationGeometryExecution:
        async with self.execution_scope():
            checked = [value async for value in self.precheck_batch((request,))]
            if len(checked) != 1:
                raise ProductRealizationRuntimeUnavailableError("Product realization returned no numeric result")
            result = checked[0]
            if result.proposal.status is not ProductRealizationStatus.SOLVED or result.frame_check_status != "PASS":
                return ProductRealizationGeometryExecution(realization=result)
            async for execution in self.instantiate_batch((request,), checked, output_need=ProductRealizationOutputNeed.PREVIEW_WORLD):
                return execution if include_instances else execution.model_copy(update={"instance_geometries": None})
            raise ProductRealizationRuntimeUnavailableError("Product realization returned no geometry result")


    async def realize_batch(self, requests: Sequence[ProductRealizationRequest], *, output_need: ProductRealizationOutputNeed | Sequence[ProductRealizationOutputNeed], _prechecks=None, _reused=None, _source_keys=None, _source_transforms=None, _window_size=None, _source_cache=None):
        """One bounded same-closure lifecycle, followed by sequential codec use.

        Complete received records survive a later Product failure. Results are
        yielded only after independent codec validation; callers own their ledger.
        Close the iterator on early exit to release the owned scope and scratch.
        """
        self._require_open()
        try:
            window_sizes = product_window_sizes(len(requests), _window_size)
            output_needs = normalize_batch_output_needs(output_need, len(requests))
        except (TypeError, ValueError) as exc:
            raise ProductRealizationRuntimeValidationError("REALIZATION_BINDING_INVALID", "Product batch output needs are invalid", phase="preflight") from exc
        if _prechecks is None and any(need not in (ProductRealizationOutputNeed.SOLVE_ONLY, ProductRealizationOutputNeed.INTERFACES) for need in output_needs):
            raise ProductRealizationRuntimeValidationError("REALIZATION_BINDING_INVALID",
                "Geometry output requires precheck_batch then instantiate_batch",
                phase="preflight")
        if all(need in (ProductRealizationOutputNeed.SOLVE_ONLY, ProductRealizationOutputNeed.INTERFACES) for need in output_needs):
            requests = tuple(request.model_copy(update={"geometry_bindings": tuple(
                binding.model_copy(update={"geometry": None}) if isinstance(binding, ResolvedMappedGeometryBinding)
                else binding.model_copy(update={"geometry_inputs": ()})
                for binding in request.geometry_bindings)}) for request in requests)
        descriptors: set[int] = set()
        root = None
        active_index = None
        batch_phase = "startup"
        ready_started, ready_limit = None, None
        async with self.execution_scope():
            try:
                lease = await self._capacity.admit_current_execution()
                deadline, host_fd = lease.execution_deadline, lease.descriptor
                assert deadline is not None and host_fd is not None
                # Reserve existing execution time for independent validation of
                # received records, including when Product reaches its hard cap.
                product_deadline = deadline - GEOMETRY_CODEC_EXECUTION.execution_seconds
                stopped_resource = None
                root = Path(tempfile.mkdtemp(prefix="cad2bim-product-realization-batch-")).resolve(strict=True)
                os.chmod(root, 0o700)
                with observe_execution_phase("product_realization", "materialize_batch", details={"item_count": len(requests)}):
                    try:
                        control, source = materialize_batch(requests, root, output_needs=output_needs, window_size=_window_size)
                        parameter_rules = (_prepare_geometry_binding_parameters(requests[0].geometry_bindings)
                                           if _prechecks is None else None)
                    except (ValueError, WorkerProtocolError) as exc:
                        raise ProductRealizationRuntimeValidationError("REALIZATION_BINDING_INVALID", str(exc), phase="preflight") from exc
                (root / "request.json").write_bytes(control)
                (root / "source.zip").write_bytes(source)
                request_fd, source_fd = _open_read_only(root / "request.json"), _open_read_only(root / "source.zip")
                terminal_fd = _open_write_once(root / "terminal.json")
                child_read, parent_write = os.pipe()
                parent_read, child_write = os.pipe()
                descriptors.update((request_fd, source_fd, terminal_fd, child_read, parent_write, parent_read, child_write))
                channel = AsyncWorkerChannel(parent_read, parent_write)
                records, reused_by_result, pending_sources = [], {}, {}
                staging_root = _source_cache.spool_directory if _source_cache is not None else root
                staged_bytes = _source_cache.spool_size_bytes if _source_cache is not None else 0

                async def receive_batch(supervision):
                    nonlocal active_index, stopped_resource, batch_phase, ready_started, ready_limit, staged_bytes
                    # These copies would otherwise keep pipe EOF artificially
                    # alive if a Child crashes. Inherited copies remain owned.
                    for descriptor in (child_read, child_write):
                        os.close(descriptor)
                        descriptors.remove(descriptor)
                    try:
                        ready_started = time.monotonic()
                        ready_limit = min(product_deadline - ready_started, PRODUCT_REALIZATION_WALL_SECONDS)
                        async with asyncio.timeout_at(ready_started + ready_limit):
                            ready = await channel.read_json(limit=4096)
                        if ready.get("status") == "failed":
                            _raise_child_failure(ready)
                        if ready != {"status": "ready"}:
                            raise WorkerProtocolError("product batch readiness or closure mismatch")
                        batch_phase = "execution"
                        window_ends = set()
                        cumulative = 0
                        for count in window_sizes[:-1]:
                            cumulative += count
                            window_ends.add(cumulative)
                        window_input_start, window_output_start = channel.bytes_written, 0
                        for index, request in enumerate(requests):
                            if index in window_ends:
                                with observe_execution_phase("product_realization", "window_boundary", details={"completed_count": index}):
                                    boundary_frame = encode_json_frame({"window_end": index}, limit=1024)
                                    if channel.bytes_written - window_input_start + len(boundary_frame) > MAX_PRODUCT_BATCH_INPUT_BYTES:
                                        raise ProductRealizationRuntimeValidationError("REALIZATION_BINDING_INVALID", "product window wire input byte budget exceeded", phase="input_validation", item_index=index)
                                    await channel.write(boundary_frame)
                                    async with asyncio.timeout_at(product_deadline):
                                        if await channel.read_json(limit=1024) != {"status": "window_finished", "item_count": index}:
                                            raise WorkerProtocolError("product window boundary acknowledgement mismatch")
                                if channel.bytes_read - window_output_start > MAX_PRODUCT_BATCH_OUTPUT_BYTES:
                                    raise WorkerProtocolError("product window wire output byte budget exceeded")
                                window_input_start, window_output_start = channel.bytes_written, channel.bytes_read
                            if product_deadline - time.monotonic() <= PRODUCT_REALIZATION_WALL_SECONDS + PRODUCT_REALIZATION_BUDGET.cleanup_seconds:
                                stopped_resource = "wall"
                                break
                            if supervision.remaining_cpu_seconds <= PRODUCT_REALIZATION_CPU_SECONDS + PRODUCT_REALIZATION_BUDGET.cleanup_seconds:
                                stopped_resource = "cpu"
                                break
                            active_index = index
                            item_need = output_needs[index]
                            async with supervision.invocation(wall_seconds=PRODUCT_REALIZATION_WALL_SECONDS, cpu_seconds=PRODUCT_REALIZATION_CPU_SECONDS), asyncio.timeout_at(min(product_deadline, time.monotonic() + PRODUCT_REALIZATION_WALL_SECONDS)):
                                reused_sources = _reused[index] if _reused else {}
                                if item_need is ProductRealizationOutputNeed.LOCAL_FRAGMENTS and _prechecks is not None:
                                    bindings = {binding.binding_key: binding for binding in request.geometry_bindings}
                                    for evaluation in _prechecks[index].interface_evaluations:
                                        binding = bindings[evaluation.binding_key]
                                        key, route = _source_keys[index][evaluation.fragment_key]
                                        if evaluation.fragment_key not in reused_sources and key in pending_sources:
                                            source = pending_sources[key]
                                            transform = geometry_source_to_fragment(binding, source.parameters, evaluation.normalized_parameters,
                                                                                    uniform_scale=evaluation.uniform_scale)
                                            if transform is None:
                                                key = _geometry_source_request_key(binding, evaluation, exact=True)
                                                _source_keys[index][evaluation.fragment_key] = (key, route)
                                                source = pending_sources.get(key)
                                                transform = (geometry_source_to_fragment(binding, source.parameters, evaluation.normalized_parameters,
                                                                                         uniform_scale=evaluation.uniform_scale) if source is not None else None)
                                            if source is not None and transform is not None:
                                                reused_sources[evaluation.fragment_key] = (source, None)
                                                _source_transforms[index][evaluation.fragment_key] = transform
                                frame = batch_invocation_frame(index, request, output_need=item_need,
                                    precheck=_prechecks[index] if _prechecks is not None else None,
                                    reused_fragments={key: _source_metadata(geometry)
                                        for key, (geometry, _) in reused_sources.items()})
                                encoded = encode_json_frame(frame, limit=MAX_PRODUCT_REALIZATION_CONTROL_BYTES)
                                if channel.bytes_written - window_input_start + len(encoded) > MAX_PRODUCT_BATCH_INPUT_BYTES:
                                    raise ProductRealizationRuntimeValidationError("REALIZATION_BINDING_INVALID", "product batch wire input byte budget exceeded", phase="input_validation", item_index=index)
                                with observe_execution_phase("product_realization", "batch_send", details={"item_index": index, "byte_length": len(encoded)}):
                                    await channel.write(encoded)
                                with observe_execution_phase("product_realization", "batch_wait_result", details={"item_index": index}):
                                    response = await channel.read_json(limit=MAX_PRODUCT_BATCH_RESULT_FRAME_BYTES)
                                if channel.bytes_read - window_output_start > MAX_PRODUCT_BATCH_OUTPUT_BYTES:
                                    raise WorkerProtocolError("product batch wire output byte budget exceeded")
                                if response.get("status") == "failed":
                                    _raise_child_failure(response)
                                validate_batch_result_frame(response, item_index=index,
                                    output_need=item_need)
                                result = ProductRealizationResult.model_validate(response["result"])
                                if _prechecks is not None:
                                    validate_product_realization_instantiation_result(
                                        request, _prechecks[index], result, output_need=item_need,
                                    )
                                else:
                                    validate_product_realization_result_integrity(
                                        request, result, output_need=item_need if result.execution_stage == "instantiated" else None,
                                        _parameter_rules=parameter_rules,
                                    )
                                receipts = {r.fragment_key: r for r in result.instance_receipts}
                                expected_reused = [key for key in receipts if key in reused_sources]
                                if response.get("reused_fragments", []) != expected_reused:
                                    raise WorkerProtocolError("product batch reused sources differ from the host lookup")
                                for handle, (geometry, _) in reused_sources.items():
                                    receipt = receipts.get(handle)
                                    if receipt is None or receipt.geometry_sha256 != geometry.sha256 or receipt.geometry_summary != geometry.summary:
                                        raise WorkerProtocolError("product batch reused receipt differs from its checked source")
                                if reused_sources:
                                    reused_by_result[id(result)] = reused_sources
                                has_world = item_need is ProductRealizationOutputNeed.PREVIEW_WORLD and result.execution_stage == "instantiated" and isinstance(result.proposal, SolvedProductRealizationProposal)
                                expected_handles = ([None] if has_world else []) + (
                                    [key for key in receipts if key not in reused_sources] if item_need not in (ProductRealizationOutputNeed.SOLVE_ONLY, ProductRealizationOutputNeed.INTERFACES) else [])
                                geometries = response["geometries"]
                                if not isinstance(geometries, list) or len(geometries) != len(expected_handles):
                                    raise WorkerProtocolError("product batch geometry count mismatch")
                                output_bytes = channel.bytes_read - window_output_start
                                outputs = []
                                for ordinal, (entry, handle) in enumerate(zip(geometries, expected_handles, strict=True)):
                                    if not isinstance(entry, dict) or set(entry) != {"fragment_key", "geometry"} or entry["fragment_key"] != handle:
                                        raise WorkerProtocolError("product batch fragment key/order mismatch")
                                    metadata = ProductRealizationPreviewGeometryMetadata.model_validate(entry["geometry"])
                                    if handle is not None and metadata.sha256 != receipts[handle].geometry_sha256:
                                        raise WorkerProtocolError("product batch fragment does not match its receipt")
                                    if handle is not None and metadata.summary != receipts[handle].geometry_summary:
                                        raise WorkerProtocolError("product batch fragment summary does not match its receipt")
                                    output_bytes += metadata.byte_length
                                    if output_bytes > MAX_PRODUCT_BATCH_OUTPUT_BYTES:
                                        raise WorkerProtocolError("product batch aggregate output byte budget exceeded")
                                    path = staging_root / (f"geometry-{metadata.sha256}.brep" if _source_cache is not None else f"output-{index:04d}-{ordinal:03d}.brep")
                                    new_path = (_source_cache.reserve_spool_file(path, metadata.byte_length)
                                                if _source_cache is not None else not path.exists())
                                    if new_path and _source_cache is None:
                                        staged_bytes += metadata.byte_length
                                        if staged_bytes > MAX_PRODUCT_BATCH_OUTPUT_BYTES:
                                            raise WorkerProtocolError("product unique source spool byte budget exceeded")
                                    digest, remaining = hashlib.sha256(), metadata.byte_length
                                    with observe_execution_phase("product_realization", "batch_receive_payload", details={"item_index": index, "byte_length": metadata.byte_length}), (path.open("xb") if new_path else nullcontext()) as stream:
                                        while remaining:
                                            chunk = await channel.read_exact(min(65_536, remaining))
                                            digest.update(chunk)
                                            if stream is not None:
                                                stream.write(chunk)
                                            remaining -= len(chunk)
                                            if channel.bytes_read - window_output_start > MAX_PRODUCT_BATCH_OUTPUT_BYTES:
                                                raise WorkerProtocolError("product batch wire output byte budget exceeded")
                                    if digest.hexdigest() != metadata.sha256:
                                        raise WorkerProtocolError("product batch payload SHA mismatch")
                                    outputs.append((handle, metadata, path))
                                binding_by_key = {binding.binding_key: binding for binding in request.geometry_bindings}
                                expected_constructions = [receipt.fragment_key for receipt in result.instance_receipts
                                    if item_need is ProductRealizationOutputNeed.LOCAL_FRAGMENTS
                                    and receipt.fragment_key not in reused_sources
                                    and binding_by_key[receipt.binding_key].ifc_representation_route.value == "parametric"]
                                descriptions = response["constructions"]
                                if [entry["fragment_key"] for entry in descriptions] != expected_constructions:
                                    raise WorkerProtocolError("product batch construction fragment closure mismatch")
                                constructions = []
                                for ordinal, entry in enumerate(descriptions):
                                    length = entry["byte_length"]
                                    output_bytes += length
                                    if output_bytes > MAX_PRODUCT_BATCH_OUTPUT_BYTES:
                                        raise WorkerProtocolError("product batch aggregate output byte budget exceeded")
                                    path = staging_root / (f"construction-{entry['sha256']}.json" if _source_cache is not None else f"construction-{index:04d}-{ordinal:03d}.json")
                                    new_path = (_source_cache.reserve_spool_file(path, length)
                                                if _source_cache is not None else not path.exists())
                                    if new_path and _source_cache is None:
                                        staged_bytes += length
                                        if staged_bytes > MAX_PRODUCT_BATCH_OUTPUT_BYTES:
                                            raise WorkerProtocolError("product unique source spool byte budget exceeded")
                                    digest, remaining = hashlib.sha256(), length
                                    with (path.open("xb") if new_path else nullcontext()) as stream:
                                        while remaining:
                                            chunk = await channel.read_exact(min(65_536, remaining))
                                            digest.update(chunk)
                                            if stream is not None:
                                                stream.write(chunk)
                                            remaining -= len(chunk)
                                            if channel.bytes_read - window_output_start > MAX_PRODUCT_BATCH_OUTPUT_BYTES:
                                                raise WorkerProtocolError("product batch wire output byte budget exceeded")
                                    if digest.hexdigest() != entry["sha256"]:
                                        raise WorkerProtocolError("product batch construction payload SHA mismatch")
                                    constructions.append((entry["fragment_key"], path, length))
                                if output_bytes > MAX_PRODUCT_BATCH_OUTPUT_BYTES:
                                    raise WorkerProtocolError("product batch aggregate output byte budget exceeded")
                            records.append((result, outputs, constructions, item_need))
                            if item_need is ProductRealizationOutputNeed.LOCAL_FRAGMENTS:
                                metadata_by_handle = {handle: metadata for handle, metadata, _ in outputs}
                                for evaluation in result.interface_evaluations:
                                    metadata = metadata_by_handle.get(evaluation.fragment_key)
                                    if metadata is not None:
                                        key, _ = _source_keys[index][evaluation.fragment_key]
                                        pending_sources.setdefault(key, _PendingGeometrySource(metadata, (id(result), evaluation.fragment_key), evaluation.normalized_parameters))
                            active_index = None
                        finish_frame = encode_json_frame({"finish": len(records)}, limit=1024)
                        batch_phase = "finalization"
                        if channel.bytes_written - window_input_start + len(finish_frame) > MAX_PRODUCT_BATCH_INPUT_BYTES:
                            raise ProductRealizationRuntimeValidationError("REALIZATION_BINDING_INVALID", "product batch wire input byte budget exceeded", phase="input_validation")
                        await channel.write(finish_frame)
                        os.close(parent_write)
                        descriptors.remove(parent_write)
                        async with asyncio.timeout_at(product_deadline):
                            finished = await channel.read_json(limit=1024)
                            if channel.bytes_read - window_output_start > MAX_PRODUCT_BATCH_OUTPUT_BYTES:
                                raise WorkerProtocolError("product batch wire output byte budget exceeded")
                            if (finished != {"status": "finished", "item_count": len(records)}
                                    or type(finished.get("item_count")) is not int):
                                raise WorkerProtocolError("product batch finish count mismatch")
                            await channel.require_eof()
                    except TimeoutError as exc:
                        raise WorkerSupervisorError("WORKER_TIMEOUT", "product batch invocation exceeded its wall-clock budget", execution_started=True, resource_kind="wall") from exc

                environment = {**_environment(root), "CAD2BIM_WORKER_TIMING": "1", "CAD2BIM_PRODUCT_BATCH": "1",
                    "CAD2BIM_PRODUCT_NUMERIC_ONLY": "1" if all(need in (ProductRealizationOutputNeed.SOLVE_ONLY, ProductRealizationOutputNeed.INTERFACES) for need in output_needs) else "0",
                    "CAD2BIM_PRODUCT_INSTANTIATE_ONLY": "1" if _prechecks is not None else "0",
                    "CAD2BIM_PRODUCT_BINDING_ROOT": str(root), "CAD2BIM_PRODUCT_REQUEST_FD": str(request_fd),
                    "CAD2BIM_PRODUCT_SOURCE_FD": str(source_fd), "CAD2BIM_PRODUCT_TERMINAL_FD": str(terminal_fd),
                    "CAD2BIM_PRODUCT_BATCH_INPUT_FD": str(child_read), "CAD2BIM_PRODUCT_BATCH_OUTPUT_FD": str(child_write)}
                product_failure = None
                try:
                    terminal = await self._supervisor.run(WorkerLaunchSpec(
                        command=self._sandbox.command(scratch=root, child_args=self._isolated_module_args("CAD2BIM_backend.infrastructure.product_realization_runtime.child")),
                        cwd=root, env=environment, pass_fds=(request_fd, source_fd, terminal_fd, child_read, child_write),
                        capacity_fds=(host_fd,), scratch=root),
                        budget=replace(PRODUCT_REALIZATION_BUDGET, wall_seconds=PRODUCT_REALIZATION_EXECUTION.execution_seconds,
                                       cpu_seconds=PRODUCT_REALIZATION_EXECUTION.execution_seconds),
                        deadline=product_deadline, interaction=receive_batch,
                        on_guard_registered=_GUARD_REGISTRATION.get())
                    child_terminal = _read_json(root / "terminal.json", MAX_PRODUCT_REALIZATION_TERMINAL_BYTES, unavailable=True)
                    if terminal.returncode != 0 or child_terminal.get("status") != "succeeded":
                        _raise_child_failure(child_terminal)
                    if (type(child_terminal.get("item_count")) is not int
                            or child_terminal["item_count"] != len(records)
                            or (stopped_resource is None and len(records) != len(requests))):
                        raise WorkerProtocolError("product batch terminal count mismatch")
                except (WorkerSupervisorError, ProductRealizationRuntimeValidationError,
                        ProductRealizationRuntimeUnavailableError, WorkerProtocolError,
                        ValidationError, ValueError, OSError) as exc:
                    product_failure = exc
                # Product has exited: codec may use the same single Host slot.
                try:
                    async for value in self._normalize_batch_records(records, reused_by_result=reused_by_result, retain_staged=_source_cache is not None):
                        yield value
                except (ProductRealizationRuntimeValidationError, ProductRealizationRuntimeUnavailableError) as exc:
                    if product_failure is not None:
                        raise exc from product_failure
                    raise
                if product_failure is not None:
                    raise product_failure
                if stopped_resource is not None:
                    raise ProductRealizationRuntimeResourceLimitError(
                        "product batch stopped before dispatching an item beyond its remaining budget",
                        diagnostic={"phase": "batch_dispatch", "reason": "batch_budget_exhausted",
                            "completed_count": len(records), "remaining_count": len(requests) - len(records),
                            "resource_kind": stopped_resource}, retryable=False)
            except WorkerSupervisorError as exc:
                if exc.execution_started and exc.code in {"WORKER_TIMEOUT", "WORKER_RESOURCE_LIMIT"}:
                    diagnostic = {**exc.diagnostic(), "phase": batch_phase}
                    if batch_phase == "startup" and ready_started is not None:
                        diagnostic.update(limit=ready_limit, observed=time.monotonic() - ready_started)
                    raise ProductRealizationRuntimeResourceLimitError(str(exc), diagnostic=diagnostic,
                        retryable=False, item_index=active_index) from exc
                raise ProductRealizationRuntimeUnavailableError(str(exc), diagnostic=exc.diagnostic(),
                    item_index=active_index) from exc
            except (WorkerCapacityBusy, WorkerCapacityUnavailable) as exc:
                raise ProductRealizationRuntimeUnavailableError(str(exc),
                    diagnostic=self._capacity.failure_diagnostic(exc), item_index=active_index) from exc
            except ProductRealizationRuntimeValidationError:
                raise
            except (WorkerProtocolError, ValidationError, ValueError) as exc:
                raise ProductRealizationRuntimeValidationError("REALIZATION_OUTPUT_INVALID", str(exc), phase="output_validation", item_index=active_index) from exc
            except OSError as exc:
                raise ProductRealizationRuntimeUnavailableError("product batch transport or storage failed",
                    item_index=active_index) from exc
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)
                if root is not None:
                    shutil.rmtree(root, ignore_errors=True)

    async def _normalize_batch_records(self, records, *, reused_by_result=None, retain_staged=False):
        from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution_protocol import (
            MAX_CODEC_BATCH_TOTAL_ITEMS, MAX_CODEC_BATCH_INPUT_BYTES,
        )
        checked_reused_sources, checked_geometry_sources = {}, {}
        async def normalize_group(group, start_index):
            # Keep only a bounded observation sample; the error reports the
            # whole unconfirmed group rather than guessing a failing item.
            fragment_map = []
            ordinal = 0
            for item_offset, (_, outputs, _, _) in enumerate(group):
                for handle, _, _ in outputs:
                    if len(fragment_map) < 32:
                        fragment_map.append({"codec_ordinal": ordinal,
                            "item_index": start_index + item_offset, "fragment_key": handle})
                    ordinal += 1
            with observe_execution_phase("product_realization", "codec_group", details={
                "group_start_index": start_index,
                "group_item_count": len(group),
                "fragment_count": ordinal,
                "fragment_map": fragment_map,
                "fragment_map_truncated": ordinal > len(fragment_map),
            }):
                return await self._normalize_record_group(group, start_index=start_index,
                    **({"reused_by_result": reused_by_result, "checked_reused_sources": checked_reused_sources, "checked_geometry_sources": checked_geometry_sources, "retain_staged": retain_staged}
                       if reused_by_result is not None else {}))

        group, count, byte_count, group_start_index = [], 0, 0, 0
        for index, record in enumerate(records):
            added_count = len(record[1])
            added_bytes = sum(metadata.byte_length for _, metadata, _ in record[1])
            if group and (count + added_count > MAX_CODEC_BATCH_TOTAL_ITEMS or byte_count + added_bytes > MAX_CODEC_BATCH_INPUT_BYTES):
                for value in await normalize_group(group, group_start_index):
                    yield value
                group, count, byte_count, group_start_index = [], 0, 0, index
            group.append(record)
            count, byte_count = count + added_count, byte_count + added_bytes
        if group:
            for value in await normalize_group(group, group_start_index):
                yield value

    async def _normalize_record_group(self, records, *, start_index: int, reused_by_result=None, checked_reused_sources=None, checked_geometry_sources=None, retain_staged=False):
        from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution_protocol import (
            MAX_CODEC_BATCH_TOTAL_ITEMS, MAX_CODEC_BATCH_INPUT_BYTES, GeometryCodecItemFailure,
            MAX_CODEC_CONTROL_BYTES, normalize_control,
        )
        # One group or one large instance is bounded by Product output limits.
        # Native/byte results from earlier consumed groups are not accumulated.
        entries = [entry for _, outputs, _, _ in records for entry in outputs]
        normalized, pending, pending_bytes = [], [], 0
        checked_geometry_sources = {} if checked_geometry_sources is None else checked_geometry_sources
        unique_keys = []
        seen_keys = set(checked_geometry_sources)
        def geometry_key(handle, metadata):
            return (metadata.format, metadata.sha256, handle is None or metadata.summary is not None)
        group_diagnostic = {"phase": "codec", "group_start_index": start_index,
                            "group_item_count": len(records)}
        async def flush():
            if pending:
                values = await self._codec.normalize_batch(tuple(pending))
                if any(isinstance(value, GeometryCodecItemFailure) for value in values):
                    raise GeometryRuntimeValidationError("product BREP returned a non-BREP recoverable failure")
                normalized.extend(value[0] for value in values)
                pending.clear()
        try:
            for handle, metadata, path in entries:
                payload = _read_bytes(path, metadata.byte_length)
                if len(payload) != metadata.byte_length or hashlib.sha256(payload).hexdigest() != metadata.sha256:
                    raise GeometryRuntimeValidationError("product batch staged payload changed")
                key = geometry_key(handle, metadata)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                unique_keys.append(key)
                request = GeometryNormalizeRequest(format=metadata.format, source_format=GeometrySourceFormat.OCCT_BREP,
                                                   payload=payload, include_summary=key[2])
                # Reserve a full control frame per item. The actual eight-item
                # frames share headers, so this safely bounds framed input too.
                framed_bytes = len(payload) + len(encode_json_frame(
                    normalize_control((request,), include_raw_brep_summary=False),
                    limit=MAX_CODEC_CONTROL_BYTES))
                if pending and (len(pending) >= MAX_CODEC_BATCH_TOTAL_ITEMS or pending_bytes + framed_bytes > MAX_CODEC_BATCH_INPUT_BYTES):
                    await flush()
                    pending_bytes = 0
                if framed_bytes > MAX_CODEC_BATCH_INPUT_BYTES:
                    normalized.extend(value[0] for value in await self._codec.normalize_many((request,)))
                else:
                    pending.append(request)
                    pending_bytes += framed_bytes
            await flush()
            if len(normalized) != len(unique_keys):
                raise GeometryRuntimeValidationError("product batch codec result count mismatch")
            checked_geometry_sources.update(zip(unique_keys, normalized, strict=True))
            results = []
            from CAD2BIM_backend.domain.ifc_geometry_construction import GeometryConstruction
            for result, outputs, constructions, output_need in records:
                world, instances = None, {} if output_need not in (ProductRealizationOutputNeed.SOLVE_ONLY, ProductRealizationOutputNeed.INTERFACES) else None
                instance_constructions = {} if output_need is ProductRealizationOutputNeed.LOCAL_FRAGMENTS else None
                for handle, metadata, path in outputs:
                    geometry = checked_geometry_sources[geometry_key(handle, metadata)]
                    if geometry.content_type != metadata.content_type:
                        raise GeometryRuntimeValidationError("product batch codec metadata mismatch")
                    if metadata.summary is not None and geometry.summary != metadata.summary:
                        raise GeometryRuntimeValidationError("product batch codec summary does not match its receipt")
                    if handle is None:
                        world = geometry
                    else:
                        assert instances is not None
                        instances[handle] = geometry
                    if not retain_staged:
                        path.unlink()
                if instance_constructions is not None:
                    for fragment_key, path, length in constructions:
                        content = _read_bytes(path, length)
                        if len(content) != length:
                            raise GeometryRuntimeValidationError("product batch construction payload changed")
                        instance_constructions[fragment_key] = GeometryConstruction.model_validate_json(content)
                        if not retain_staged:
                            path.unlink()
                for handle, (geometry, construction) in (reused_by_result or {}).get(id(result), {}).items():
                    assert instances is not None and instance_constructions is not None
                    if isinstance(geometry, _PendingGeometrySource):
                        checked = (checked_reused_sources or {}).get(geometry.origin)
                        if checked is None:
                            raise GeometryRuntimeValidationError("reused source has no earlier independent Codec result")
                        geometry, construction = checked
                    instances[handle] = geometry
                    if construction is not None:
                        instance_constructions[handle] = construction.model_copy(deep=True)
                if checked_reused_sources is not None and instances is not None:
                    for handle, geometry in instances.items():
                        construction = (instance_constructions or {}).get(handle)
                        checked_reused_sources[(id(result), handle)] = (geometry, construction.model_copy(deep=True) if construction is not None else None)
                results.append(ProductRealizationGeometryExecution(realization=result, output_need=output_need,
                    geometry=world, instance_geometries=instances,
                    instance_constructions=instance_constructions))
            return results
        except GeometryRuntimeResourceLimitError as exc:
            raise ProductRealizationRuntimeResourceLimitError("product batch codec exceeded its supervised limit",
                diagnostic={**group_diagnostic, "reason": str(exc)[:2_000],
                    **({"codec_diagnostic": exc.diagnostic.model_dump(mode="json")}
                       if exc.diagnostic else {})}, retryable=False) from exc
        except GeometryRuntimeUnavailableError as exc:
            raise ProductRealizationRuntimeUnavailableError("product batch codec is unavailable",
                diagnostic={**group_diagnostic, "reason": str(exc)[:2_000],
                    **({"codec_diagnostic": exc.diagnostic} if exc.diagnostic else {})},
                retryable=exc.retryable) from exc
        except (GeometryRuntimeError, ValueError, TypeError) as exc:
            raise ProductRealizationRuntimeValidationError("REALIZATION_OUTPUT_INVALID",
                "Product batch geometry failed independent codec or receipt verification",
                phase="output_validation",
                diagnostic={**group_diagnostic, "reason": str(exc)[:2_000]}) from exc


    async def close(self) -> None:
        self._closed = True

    def _isolated_module_args(self, module_name: str) -> tuple[str, ...]:
        return (
            "-I",
            "-B",
            "-c",
            _ISOLATED_MODULE_BOOTSTRAP,
            str(self._source_root),
            module_name,
        )

    def _require_open(self) -> None:
        if self._closed:
            raise ProductRealizationRuntimeUnavailableError("product realization runtime is closed")


def _raise_child_failure(terminal: dict[str, Any]) -> None:
    code = str(terminal.get("code", "REALIZATION_RUNTIME_FAILED"))
    phase = str(terminal.get("phase", "execution"))
    message = str(terminal.get("message", "product realization worker failed"))[:2_000]
    index = terminal.get("item_index")
    item_index = index if type(index) is int and index >= 0 else None
    diagnostic = terminal.get("diagnostic")
    diagnostic = diagnostic if isinstance(diagnostic, dict) else None
    if code in {
        "REALIZATION_SOURCE_INVALID",
        "REALIZATION_BINDING_INVALID",
        "REALIZATION_EXECUTION_FAILED",
        "REALIZATION_OUTPUT_INVALID",
    }:
        raise ProductRealizationRuntimeValidationError(code, message, phase=phase,
            item_index=item_index, diagnostic=diagnostic)
    raise ProductRealizationRuntimeUnavailableError(message, item_index=item_index, diagnostic=diagnostic)


def _read_json(path: Path, limit: int, *, unavailable: bool) -> dict[str, Any]:
    try:
        if path.stat().st_size > limit:
            raise ValueError("worker output exceeds its byte budget")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("worker output must be an object")
        return value
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        error = (
            ProductRealizationRuntimeUnavailableError
            if unavailable
            else ProductRealizationRuntimeValidationError
        )
        if unavailable:
            raise error("product realization worker exited without a trusted terminal") from exc
        raise error(
            "REALIZATION_OUTPUT_INVALID",
            "product realization worker returned an invalid result",
            phase="output_validation",
        ) from exc






def _read_bytes(path: Path, limit: int) -> bytes:
    size = path.stat().st_size
    if not 1 <= size <= limit:
        raise ValueError("worker binary output exceeds its byte budget")
    payload = path.read_bytes()
    if len(payload) != size:
        raise ValueError("worker binary output changed while being read")
    return payload


def _environment(root: Path) -> dict[str, str]:
    threads = str(PRODUCT_REALIZATION_BUDGET.threads)
    return {
        "BLIS_NUM_THREADS": threads,
        "HOME": str(root),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "MKL_NUM_THREADS": threads,
        "NUMEXPR_NUM_THREADS": threads,
        "OMP_NUM_THREADS": threads,
        "OPENBLAS_NUM_THREADS": threads,
        "PATH": str(Path(sys.executable).parent),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(root),
        "VECLIB_MAXIMUM_THREADS": threads,
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


def _readiness_request() -> ProductRealizationRequest:
    """Build a private technical probe; it has no Catalog or product identity."""

    import hashlib

    from CAD2BIM_backend.domain.canonical import sha256_digest
    from CAD2BIM_backend.domain.product_family import (
        ProductFamilyComponentContract,
        ProductFamilyContract,
        ProductFamilyFragmentBindingOption,
        ProductFamilyGeometryFragmentContract,
        ProductFamilyIfcClass,
        ProductFamilyStructureKind,
    )
    from CAD2BIM_backend.domain.ifc_geometry_basis import IfcLengthUnit
    from CAD2BIM_backend.domain.product_realization import (
        ProductRealizationInvocation,
        ResolvedModelingFunctionBinding,
    )
    from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle

    family = ProductFamilyContract(
        product_structure_kind=ProductFamilyStructureKind.SINGLE_PRODUCT,
        components=(
            ProductFamilyComponentContract(
                component_key="probe",
                ifc_class=ProductFamilyIfcClass.DISCRETE_ACCESSORY,
            ),
        ),
        geometry_fragments=(
            ProductFamilyGeometryFragmentContract(
                fragment_key="probe_shape",
                owner_component_key="probe",
                binding_options=(ProductFamilyFragmentBindingOption(
                    binding_key="probe_geometry",
                    parameter_sources={"overrides": {}},
                ),),
            ),
        ),
    )
    geometry_source = pack_source_bundle(
        {
            "main.py": (
                b"import cadquery as cq\n\n"
                b"def build(ctx):\n"
                b"    return cq.Workplane('XY').box(0.001, 0.001, 0.001)\n"
            ),
            "interfaces.py": b"def evaluate(ctx):\n    return None\n",
        }
    )
    program_source = pack_source_bundle(
        {
            "main.py": (
                b"def realize(ctx):\n"
                b"    return {\n"
                b"        'status': 'SOLVED',\n"
                b"        'components': [{'component_key': 'probe'}],\n"
                b"        'fragments': [{'fragment_key': 'probe_shape', "
                b"'binding_key': 'probe_geometry'}],\n"
                b"    }\n"
            )
        }
    )
    return ProductRealizationRequest(
        family_contract=family,
        family_contract_digest=sha256_digest(family),
        source_bundle=program_source,
        source_bundle_sha256=hashlib.sha256(program_source).hexdigest(),
        geometry_bindings=(
            ResolvedModelingFunctionBinding(
                binding_key="probe_geometry",
                geometry_content_digest=hashlib.sha256(geometry_source).hexdigest(),
                source_bundle=geometry_source,
                source_bundle_sha256=hashlib.sha256(geometry_source).hexdigest(),
                parameter_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            ),
        ),
        invocation=ProductRealizationInvocation(
            product_origin=(0.0, 0.0, 0.0), length_unit=IfcLengthUnit(),
        ),
    )


__all__ = ["OcpCadQueryProductRealizationRuntime", "PRODUCT_REALIZATION_BUDGET"]
