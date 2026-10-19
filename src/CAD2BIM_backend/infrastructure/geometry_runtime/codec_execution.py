"""One private supervised executor for server-owned OCP codec work."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Mapping, BinaryIO, Callable

from pydantic import ValidationError

_GUARD_REGISTRATION = ContextVar("geometry_codec_guard_registration", default=None)

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.runtime_execution import GEOMETRY_CODEC_EXECUTION, RuntimeExecutionPolicy
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometryBuildDisplayContextRequest,
    GeometryBuildDisplayContextResult,
    GeometryNormalizeRequest,
    GeometryDisplayMesh,
    GeometryPreview,
    GeometryRenderRequest,
    GeometryRuntimeResourceLimitError,
    GeometryRuntimeResourceDiagnostic,
    GeometryRuntimeUnavailableError,
    GeometryRuntimeValidationError,
    GeometrySourceFormat,
    GeometrySummary,
    GeometrySectionRequest,
    GeometrySectionResult,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution_protocol import (
    GeometryCodecProtocolError,
    MAX_CODEC_CONTROL_BYTES,
    MAX_CODEC_PAYLOAD_BYTES,
    MAX_CODEC_PREVIEW_BYTES,
    MAX_CODEC_TERMINAL_BYTES,
    display_context_control,
    normalize_control,
    render_control,
    export_stl_control,
    section_control,
    pack_fragment_inputs,
    reopen_control,
    require_exact_keys,
    normalize_batch_control,
    GeometryCodecItemFailure,
    MAX_CODEC_BATCH_ITEMS,
    MAX_CODEC_BATCH_OUTPUT_BYTES,
)
from CAD2BIM_backend.infrastructure.execution_observability import (
    observe_execution_phase,
)
from CAD2BIM_backend.infrastructure.worker_runtime import (
    DarwinSeatbeltSandbox,
    HostWorkerCapacity,
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
from CAD2BIM_backend.infrastructure.worker_runtime.protocol import write_all


# Server-owned OCP and rendering need their measured native import baseline plus
# bounded headroom; this does not alter Build, Query, or Product worker budgets.
GEOMETRY_CODEC_BUDGET = WorkerProcessBudget(
    wall_seconds=GEOMETRY_CODEC_EXECUTION.execution_seconds,
    cpu_seconds=30.0,
    rss_bytes=2 * 1024**3,
    scratch_bytes=128 * 1024**2,
    threads=8,
    open_file_descriptors=64,
    stdout_bytes=16 * 1024,
    stderr_bytes=16 * 1024,
    cleanup_seconds=1.0,
)
from CAD2BIM_backend.infrastructure.worker_runtime.timing import ISOLATED_TIMED_BOOTSTRAP

_ISOLATED_MODULE_BOOTSTRAP = ISOLATED_TIMED_BOOTSTRAP


class OcpGeometryCodecExecutor:
    """Run each fixed codec operation in one fresh bounded OCP child."""

    def __init__(
        self,
        *,
        budget: WorkerProcessBudget,
        python_executable: str | None = None,
        supervisor: WorkerProcessSupervisor | None = None,
        sandbox: DarwinSeatbeltSandbox | None = None,
        capacity: HostWorkerCapacity | None = None,
        capacity_root: str | Path | None = None,
    ) -> None:
        executable = Path(python_executable or sys.executable)
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("geometry codec Python executable must be an absolute file")
        if budget.wall_seconds <= 0 or budget.scratch_bytes <= 0:
            raise ValueError("geometry codec worker budget must be positive")
        self._python = str(executable)
        self._budget = budget
        runtime_directory = Path(__file__).resolve().parent
        source_root = runtime_directory.parents[2]
        self._source_root = source_root.resolve(strict=True)
        worker_runtime_directory = runtime_directory.parent / "worker_runtime"
        trusted_files = tuple(
            path.resolve(strict=True)
            for path in (
                source_root / "CAD2BIM_backend" / "domain" / "__init__.py",
                source_root / "CAD2BIM_backend" / "domain" / "canonical.py",
                source_root / "CAD2BIM_backend" / "domain" / "geometry_frame.py",
                source_root / "CAD2BIM_backend" / "domain" / "geometry_interface.py",
                source_root / "CAD2BIM_backend" / "domain" / "geometry_runtime.py",
                source_root / "CAD2BIM_backend" / "domain" / "geometry_inspection.py",
                source_root / "CAD2BIM_backend" / "domain" / "validation_diagnostics.py",
                source_root / "CAD2BIM_backend" / "domain" / "run_events.py",
                source_root / "CAD2BIM_backend" / "infrastructure" / "__init__.py",
                runtime_directory / "__init__.py",
                runtime_directory / "codec.py",
                runtime_directory / "codec_child.py",
                runtime_directory / "codec_execution_protocol.py",
                runtime_directory / "render.py",
                runtime_directory / "section.py",
                runtime_directory / "straight_profile.py",
                runtime_directory / "user_geometry.py",
                worker_runtime_directory / "__init__.py",
                worker_runtime_directory / "protocol.py",
                worker_runtime_directory / "timing.py",
            )
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
        self._closed = False
        self._verified: ContextVar[tuple | None] = ContextVar("geometry_codec_verified_execution", default=None)

    @asynccontextmanager
    async def invocation_scope(self, policy: RuntimeExecutionPolicy, *, deadline: float | None = None, on_guard_registered=None):
        token = _GUARD_REGISTRATION.set(on_guard_registered)
        try:
            async with self._capacity.invocation_scope(policy, deadline=deadline) as invocation:
                yield invocation
        finally:
            _GUARD_REGISTRATION.reset(token)

    @asynccontextmanager
    async def execution_scope(self, policy: RuntimeExecutionPolicy = GEOMETRY_CODEC_EXECUTION, *, deadline: float | None = None):
        async with self._capacity.execution_scope(policy, deadline=deadline) as lease:
            prior = self._verified.get()
            if prior is not None and prior[0] is lease:
                yield lease
                return
            verified = {}
            token = self._verified.set((lease, verified))
            try:
                yield lease
            finally:
                verified.clear()
                self._verified.reset(token)

    def release_execution(self) -> None:
        current = self._verified.get()
        if current is not None:
            current[0].release()

    def _verified_values(self) -> dict:
        current = self._verified.get()
        if current is None:
            return {}
        current[0].require_owner()
        return current[1]

    @staticmethod
    def _normalization_key(request: GeometryNormalizeRequest, raw: bool) -> tuple:
        return (request.format, request.effective_source_format, request.payload, raw,
                request.transform, request.include_summary, request.unit)

    def _remember(self, request: GeometryNormalizeRequest, value: tuple, raw: bool = False) -> None:
        verified = self._verified_values()
        key = self._normalization_key(request, raw)
        retained = sum(len(item[2]) + len(result[0].payload) for item, result in verified.items())
        if key not in verified and len(verified) < 16 and retained + len(request.payload) + len(value[0].payload) <= MAX_CODEC_PAYLOAD_BYTES:
            verified[key] = value

    async def verify_ready(self) -> None:
        request = GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1,
            payload=(
                b'{"schema":"indexed_triangle_mesh_v1","triangles":[[0,2,1]],'
                b'"unit":"METRE","vertices":[[0,0,0],[0,1,0],[1,0,0]]}'
            ),
        )
        geometry = await self.normalize(request)
        await self.reopen(geometry)

    async def normalize(self, request: GeometryNormalizeRequest) -> CanonicalGeometry:
        return (await self.normalize_many((request,)))[0][0]

    async def normalize_many(
        self,
        requests: tuple[GeometryNormalizeRequest, ...],
        *,
        include_raw_brep_summary: bool = False,
    ) -> tuple[tuple[CanonicalGeometry, GeometrySummary | None], ...]:
        async with self.execution_scope():
            with observe_execution_phase("geometry_codec", "normalize"):
                return await self._normalize_many(
                    requests,
                    include_raw_brep_summary=include_raw_brep_summary,
                )

    async def _normalize_many(
        self,
        requests: tuple[GeometryNormalizeRequest, ...],
        *,
        include_raw_brep_summary: bool,
    ) -> tuple[tuple[CanonicalGeometry, GeometrySummary | None], ...]:
        self._require_open()
        control = normalize_control(
            requests,
            include_raw_brep_summary=include_raw_brep_summary,
        )
        verified = self._verified_values()
        keys = tuple(self._normalization_key(request, include_raw_brep_summary) for request in requests)
        if all(key in verified for key in keys):
            with observe_execution_phase("geometry_codec", "reuse_normalization"):
                return tuple(verified[key] for key in keys)
        terminal, outputs = await self._run(
            control,
            tuple(request.payload for request in requests),
            tuple(MAX_CODEC_PAYLOAD_BYTES for _ in requests),
        )
        result = self._success_result(terminal, operation="normalize")
        return self._decode_normalized(requests, result, outputs, include_raw_brep_summary)

    def _decode_normalized(self, requests, result, outputs, include_raw_brep_summary, *, remember=True):
        require_exact_keys(result, {"items"}, label="normalize result")
        items = result["items"]
        if not isinstance(items, list) or len(items) != len(requests):
            raise self._untrusted("normalization result count is invalid")
        normalized: list[tuple[CanonicalGeometry, GeometrySummary | None]] = []
        for index, (request, metadata, payload) in enumerate(
            zip(requests, items, outputs, strict=True)
        ):
            normalized.append(self._decode_normalized_item(request, metadata, payload, index, include_raw_brep_summary))
        for request, value in zip(requests, normalized, strict=True):
            if not remember:
                continue
            self._remember(request, value, include_raw_brep_summary)
            canonical = value[0]
            self._remember(GeometryNormalizeRequest(format=canonical.format, payload=canonical.payload,
                                                    include_summary=canonical.summary is not None), (canonical, None))
        return tuple(normalized)

    def _decode_normalized_item(self, request, metadata, payload, index, include_raw_brep_summary):
        if not isinstance(metadata, dict):
            raise self._untrusted("normalization metadata must be an object")
        require_exact_keys(
            metadata,
            {
                "format",
                "content_type",
                "payload_index",
                "payload_size",
                "sha256",
                "summary",
                "raw_summary",
            } | ({"source_provenance"} if "source_provenance" in metadata else set()),
            label="normalization metadata",
        )
        geometry_metadata = dict(metadata)
        geometry_metadata.pop("raw_summary")
        geometry = self._materialize_geometry(
            geometry_metadata,
            payload,
            expected_index=index,
        )
        if geometry.format is not request.format:
            raise self._untrusted("normalization result format does not match request")
        if (geometry.summary is not None) != request.include_summary:
            raise self._untrusted("normalization measurements do not match request")
        expects_source = (request.effective_source_format is GeometrySourceFormat.IFC
                          and request.format is CanonicalGeometryFormat.OCCT_BREP_V1)
        if expects_source:
            if (geometry.source_provenance is None
                    or geometry.source_provenance.source_sha256 != hashlib.sha256(request.payload).hexdigest()
                    or geometry.source_provenance.canonical_from_product_local != (request.transform or RigidTransform3D())):
                raise self._untrusted("IFC normalization source provenance does not match its exact input")
        elif geometry.source_provenance is not None:
            raise self._untrusted("unexpected normalization source provenance")
        raw_document = metadata["raw_summary"]
        if include_raw_brep_summary:
            if request.effective_source_format is not GeometrySourceFormat.OCCT_BREP:
                raise self._untrusted("raw summary was requested for a non-BRep source")
            try:
                raw_summary = GeometrySummary.model_validate(raw_document)
            except ValidationError as exc:
                raise self._untrusted("invalid raw BRep summary") from exc
            if raw_summary.format is not CanonicalGeometryFormat.OCCT_BREP_V1:
                raise self._untrusted("raw BRep summary format is invalid")
        else:
            if raw_document is not None:
                raise self._untrusted("unexpected raw geometry summary")
            raw_summary = None
        return geometry, raw_summary

    async def normalize_batch(
        self, requests: tuple[GeometryNormalizeRequest, ...], *,
        include_raw_brep_summary: bool = False,
    ) -> tuple[tuple[CanonicalGeometry, GeometrySummary | None] | GeometryCodecItemFailure, ...]:
        """Normalize multiple bounded frames in one owned codec process.

        Input order and source digests bind results to callers' product/fragment
        identities. A fatal batch error returns no confirmed partial results.
        """
        async with self.execution_scope():
            self._require_open()
            with observe_execution_phase("geometry_codec", "prepare_batch_input"):
                control, payload = normalize_batch_control(requests, include_raw_brep_summary=include_raw_brep_summary)
            verified = self._verified_values()
            keys = tuple(self._normalization_key(request, include_raw_brep_summary) for request in requests)
            if all(key in verified for key in keys):
                # Charge each occurrence, including a conservative complete
                # metadata frame and its four-byte length prefix. If this
                # upper bound does not fit, retain normal Worker accounting;
                # the actual output can still be smaller and valid.
                output_bound = sum(len(verified[key][0].payload) for key in keys)
                output_bound += control["frame_count"] * (MAX_CODEC_CONTROL_BYTES + 4)
                if output_bound <= MAX_CODEC_BATCH_OUTPUT_BYTES:
                    with observe_execution_phase("geometry_codec", "reuse_normalization",
                                                 details={"item_count": len(requests)}):
                        return tuple(verified[key] for key in keys)
            with observe_execution_phase("geometry_codec", "normalize_batch", details={"item_count": len(requests), "frame_count": control["frame_count"]}):
                _, outputs = await self._run(control, (payload,), (MAX_CODEC_BATCH_OUTPUT_BYTES,),
                    output_consumer=lambda terminal, stream, byte_count: self._decode_batch_output(
                        requests, control, terminal, stream, include_raw_brep_summary, byte_count))
                decoded = outputs[0]
                for request, value in zip(requests, decoded, strict=True):
                    if not isinstance(value, GeometryCodecItemFailure):
                        self._remember(request, value, include_raw_brep_summary)
                return tuple(decoded)

    def _decode_batch_output(self, requests, control, terminal, stream, include_raw_brep_summary, byte_count):
        result = self._success_result(terminal, operation="normalize_batch")
        require_exact_keys(result, {"item_count", "frame_count", "recoverable_failures"}, label="normalize batch result")
        if (type(result["item_count"]) is not int or result["item_count"] != len(requests)
                or type(result["frame_count"]) is not int or result["frame_count"] != control["frame_count"]):
            raise self._untrusted("batch terminal count is invalid")
        decoded = []
        failures = 0
        for frame_index in range(control["frame_count"]):
            try:
                remaining = byte_count - stream.tell()
                if remaining < 5:
                    raise self._untrusted("batch output frame exceeds original file extent")
                frame = read_json_frame(stream, limit=min(MAX_CODEC_CONTROL_BYTES, remaining - 4))
            except WorkerProtocolError as exc:
                raise self._untrusted("malformed batch output frame") from exc
            require_exact_keys(frame, {"frame_index", "items"}, label="normalize batch output frame")
            expected = min(MAX_CODEC_BATCH_ITEMS, len(requests) - len(decoded))
            if (type(frame["frame_index"]) is not int or frame["frame_index"] != frame_index
                    or not isinstance(frame["items"], list) or len(frame["items"]) != expected):
                raise self._untrusted("batch output frame order/count is invalid")
            for item in frame["items"]:
                index = len(decoded)
                request = requests[index]
                if not isinstance(item, dict):
                    raise self._untrusted("batch item is not an object")
                identity = {"item_index", "input_sha256", "status"}
                if (type(item.get("item_index")) is not int or item["item_index"] != index
                        or item.get("input_sha256") != hashlib.sha256(request.payload).hexdigest()):
                    raise self._untrusted("batch item identity/hash is invalid")
                if item.get("status") == "failed":
                    require_exact_keys(item, identity | {"code", "message"}, label="batch item failure")
                    if (item["code"] != "MESH_INPUT_INVALID"
                            or request.effective_source_format is not GeometrySourceFormat.INDEXED_TRIANGLE_MESH
                            or not isinstance(item["message"], str) or len(item["message"]) > 400):
                        raise self._untrusted("batch item failure is outside recoverable set")
                    decoded.append(GeometryCodecItemFailure(index, item["input_sha256"], item["code"], item["message"]))
                    failures += 1
                elif item.get("status") == "succeeded":
                    require_exact_keys(item, identity | {"geometry"}, label="batch item success")
                    metadata = item["geometry"]
                    if not isinstance(metadata, dict):
                        raise self._untrusted("batch geometry metadata is invalid")
                    size = metadata.get("payload_size")
                    if (type(size) is not int or not 1 <= size <= MAX_CODEC_PAYLOAD_BYTES
                            or size > byte_count - stream.tell()):
                        raise self._untrusted("batch geometry payload size is invalid")
                    geometry_payload = stream.read(size)
                    decoded.append(self._decode_normalized_item(request, metadata, geometry_payload, 0, include_raw_brep_summary))
                else:
                    raise self._untrusted("batch item status is invalid")
        if (stream.read(1) or type(result["recoverable_failures"]) is not int
                or result["recoverable_failures"] != failures):
            raise self._untrusted("batch output has trailing bytes or inconsistent failures")
        return tuple(decoded)

    async def reopen(self, geometry: CanonicalGeometry) -> GeometrySummary:
        async with self.execution_scope():
            with observe_execution_phase("geometry_codec", "reopen"):
                return await self._reopen(geometry)

    async def _reopen(self, geometry: CanonicalGeometry) -> GeometrySummary:
        self._require_open()
        self._verify_canonical_input(geometry)
        request = GeometryNormalizeRequest(format=geometry.format, payload=geometry.payload)
        verified = self._verified_values().get(self._normalization_key(request, False))
        if verified is not None:
            with observe_execution_phase("geometry_codec", "reuse_reopen"):
                return verified[0].summary
        terminal, outputs = await self._run(
            reopen_control(geometry),
            (geometry.payload,),
            (),
        )
        if outputs:
            raise self._untrusted("reopen returned an unexpected binary output")
        result = self._success_result(terminal, operation="reopen")
        require_exact_keys(result, {"summary"}, label="reopen result")
        try:
            summary = GeometrySummary.model_validate(result["summary"])
        except ValidationError as exc:
            raise self._untrusted("invalid reopened summary") from exc
        # Historical immutable summaries may differ from current measurement. Do not
        # cache their old values as verified normalization results for the next read.
        if summary == geometry.summary:
            self._remember(request, (geometry, None))
        return summary

    async def render(self, request: GeometryRenderRequest) -> GeometryPreview:
        async with self.execution_scope():
            with observe_execution_phase("geometry_codec", "render"):
                return await self._render(request)

    async def _render(self, request: GeometryRenderRequest) -> GeometryPreview:
        self._require_open()
        self._verify_canonical_input(request.geometry)
        inputs = [request.geometry.payload]
        if request.display_context is not None:
            self._verify_canonical_input(request.display_context)
            inputs.append(request.display_context.payload)
        for reference in request.reference_geometries:
            self._verify_canonical_input(reference)
            inputs.append(reference.payload)
        for fragment in request.fragment_inputs:
            self._verify_canonical_input(fragment.geometry)
        if request.fragment_inputs:
            inputs.append(pack_fragment_inputs(request.fragment_inputs)[1])
        terminal, outputs = await self._run(
            render_control(request),
            tuple(inputs),
            (MAX_CODEC_PREVIEW_BYTES,),
        )
        result = self._success_result(terminal, operation="render")
        require_exact_keys(result, {"preview"}, label="render result")
        metadata = result["preview"]
        if not isinstance(metadata, dict):
            raise self._untrusted("render metadata must be an object")
        expected_keys = {
            "width_px",
            "height_px",
            "content_type",
            "sha256",
            "resolved_panels",
            "payload_index",
            "payload_size",
        }
        if "view" in metadata:
            expected_keys.add("view")
        if "display_quality_warnings" in metadata:
            expected_keys.add("display_quality_warnings")
        require_exact_keys(metadata, expected_keys, label="render metadata")
        if (
            type(metadata["payload_index"]) is not int
            or type(metadata["payload_size"]) is not int
            or metadata["payload_index"] != 0
            or metadata["payload_size"] != len(outputs[0])
        ):
            raise self._untrusted("render payload metadata is invalid")
        document = dict(metadata)
        document.pop("payload_index")
        document.pop("payload_size")
        document["payload"] = outputs[0]
        try:
            preview = GeometryPreview.model_validate(document)
        except ValidationError as exc:
            raise self._untrusted("invalid preview metadata") from exc
        if hashlib.sha256(preview.payload).hexdigest() != preview.sha256:
            raise self._untrusted("render payload SHA-256 mismatch")
        source_geometries = (request.geometry, *request.reference_geometries)
        source_geometries += tuple(item.geometry for item in request.fragment_inputs)
        if request.display_context is not None:
            source_geometries += (request.display_context,)
        source_faces = {geometry.sha256: geometry.summary.topology.face_count if geometry.summary is not None else None
                        for geometry in source_geometries}
        if any(warning.geometry_sha256 not in source_faces or (
            source_faces[warning.geometry_sha256] is not None
            and warning.face_index >= source_faces[warning.geometry_sha256]
        ) for warning in preview.display_quality_warnings):
            raise self._untrusted("display warning is not bound to a request geometry face")
        expected_view = request.view if request.presentation is None else None
        expected_panel_keys = (
            (request.view.value,)
            if request.presentation is None
            else tuple(panel.panel_key for panel in request.presentation.panels)
        )
        if (
            preview.width_px != request.width_px
            or preview.height_px != request.height_px
            or preview.view != expected_view
            or tuple(panel.panel_key for panel in preview.resolved_panels) != expected_panel_keys
        ):
            raise self._untrusted("render result is not bound to the request")
        return preview

    async def export_stl(self, geometry: CanonicalGeometry) -> GeometryDisplayMesh:
        async with self.execution_scope():
            self._require_open()
            self._verify_canonical_input(geometry)
            with observe_execution_phase("geometry_codec", "export_stl"):
                terminal, outputs = await self._run(export_stl_control(geometry),
                    (geometry.payload,), (MAX_CODEC_PAYLOAD_BYTES,))
            result = self._success_result(terminal, operation="export_stl")
            require_exact_keys(result, {"mesh"}, label="STL result")
            metadata = result["mesh"]
            require_exact_keys(metadata, {"content_type", "unit", "sha256", "geometry_sha256",
                "display_quality_warnings", "payload_index", "payload_size"}, label="STL metadata")
            if (type(metadata["payload_index"]) is not int or metadata["payload_index"] != 0
                    or type(metadata["payload_size"]) is not int or metadata["payload_size"] != len(outputs[0])):
                raise self._untrusted("STL payload metadata is invalid")
            document = {key: value for key, value in metadata.items() if key not in {"payload_index", "payload_size"}}
            try:
                mesh = GeometryDisplayMesh.model_validate({**document, "payload": outputs[0]})
            except ValidationError as exc:
                raise self._untrusted("STL metadata is invalid") from exc
            if mesh.geometry_sha256 != geometry.sha256 or mesh.sha256 != hashlib.sha256(mesh.payload).hexdigest():
                raise self._untrusted("STL is not bound to its saved geometry bytes")
            if any(warning.geometry_sha256 != geometry.sha256 or (geometry.summary is not None
                    and warning.face_index >= geometry.summary.topology.face_count)
                    for warning in mesh.display_quality_warnings):
                raise self._untrusted("STL warning is not bound to a source geometry face")
            return mesh

    async def section(self, request: GeometrySectionRequest) -> GeometrySectionResult:
        async with self.execution_scope():
            self._require_open()
            self._verify_canonical_input(request.geometry)
            for fragment in request.fragment_inputs:
                self._verify_canonical_input(fragment.geometry)
            inputs = (request.geometry.payload,)
            if request.fragment_inputs:
                inputs += (pack_fragment_inputs(request.fragment_inputs)[1],)
            with observe_execution_phase("geometry_codec", "section"):
                terminal, outputs = await self._run(
                    section_control(request), inputs,
                    (1024*1024,) + (MAX_CODEC_PREVIEW_BYTES,)*len(request.observation.offsets_m),
                    allow_empty_outputs=frozenset(range(1, 1+len(request.observation.offsets_m))),
                )
            result = self._success_result(terminal, operation="section")
            require_exact_keys(result, {"facts_sha256", "facts_size", "images"}, label="section result")
            if result["facts_size"] != len(outputs[0]) or result["facts_sha256"] != hashlib.sha256(outputs[0]).hexdigest():
                raise self._untrusted("section measurement bytes do not match their receipt")
            try:
                document = json.loads(outputs[0])
                images = []
                for metadata in result["images"]:
                    require_exact_keys(metadata, {"section_index", "resolved_display", "display_clipped", "diagnostics", "preview", "payload_index", "payload_size"}, label="section image")
                    index = metadata["payload_index"]
                    if type(index) is not int or index != metadata["section_index"]+1 or not 1 <= index < len(outputs) or metadata["payload_size"] != len(outputs[index]):
                        raise self._untrusted("section image is not bound to its measurement index")
                    item = dict(metadata)
                    item.pop("payload_index")
                    item.pop("payload_size")
                    item["preview"] = {**item["preview"], "payload": outputs[index]}
                    if item["preview"]["sha256"] != hashlib.sha256(outputs[index]).hexdigest():
                        raise self._untrusted("section PNG does not match its receipt")
                    images.append(item)
                document["images"] = images
                decoded = GeometrySectionResult.model_validate(document)
            except (ValidationError, ValueError, TypeError, KeyError, IndexError) as exc:
                raise self._untrusted("invalid section measurement/image result") from exc
            if decoded.geometry_sha256 != request.geometry.sha256 or decoded.selection != request.selection or decoded.coordinates != request.coordinates:
                raise self._untrusted("section result is not bound to the exact selected source")
            expected = tuple(range(len(request.observation.offsets_m)))
            if tuple(m.section_index for m in decoded.measurements) != expected or tuple(m.offset_m for m in decoded.measurements) != request.observation.offsets_m:
                raise self._untrusted("section planes do not match their ordered request")
            rendered = [image.section_index for image in decoded.images]
            failed = [error.section_index for error in decoded.render_errors]
            if len(set(rendered+failed)) != len(rendered+failed) or set(rendered+failed) != set(expected):
                raise self._untrusted("section render outcomes do not cover the measurements exactly once")
            if any(image.preview.width_px != request.width_px or image.preview.height_px != request.height_px for image in decoded.images):
                raise self._untrusted("section image dimensions do not match the request")
            return decoded

    async def build_display_context(
        self,
        request: GeometryBuildDisplayContextRequest,
    ) -> GeometryBuildDisplayContextResult:
        async with self.execution_scope():
            with observe_execution_phase("geometry_codec", "build_display_context"):
                return await self._build_display_context(request)

    async def _build_display_context(
        self,
        request: GeometryBuildDisplayContextRequest,
    ) -> GeometryBuildDisplayContextResult:
        self._require_open()
        terminal, outputs = await self._run(
            display_context_control(request),
            (),
            (MAX_CODEC_PAYLOAD_BYTES,),
        )
        result = self._success_result(terminal, operation="build_display_context")
        require_exact_keys(
            result,
            {"geometry", "recipe_digest", "recipe_keys"},
            label="display-context result",
        )
        metadata = result["geometry"]
        if not isinstance(metadata, dict):
            raise self._untrusted("display-context geometry metadata must be an object")
        geometry = self._materialize_geometry(metadata, outputs[0], expected_index=0)
        expected_keys = tuple(recipe.recipe_key for recipe in request.recipes)
        if (
            result["recipe_digest"] != sha256_digest(request)
            or not isinstance(result["recipe_keys"], list)
            or tuple(result["recipe_keys"]) != expected_keys
        ):
            raise self._untrusted("display-context result is not bound to its recipe request")
        try:
            result = GeometryBuildDisplayContextResult(
                geometry=geometry,
                recipe_digest=result["recipe_digest"],
                recipe_keys=tuple(result["recipe_keys"]),
            )
        except ValidationError as exc:
            raise self._untrusted("invalid display-context result") from exc
        self._remember(GeometryNormalizeRequest(format=geometry.format, payload=geometry.payload), (geometry, None))
        return result

    async def close(self) -> None:
        self._closed = True

    async def _run(
        self,
        control: dict[str, Any],
        input_payloads: tuple[bytes | tuple[bytes, ...], ...],
        output_limits: tuple[int, ...],
        *,
        allow_empty_outputs: frozenset[int] = frozenset(),
        output_consumer: Callable[[dict[str, Any], BinaryIO, int], Any] | None = None,
    ) -> tuple[dict[str, Any], tuple[Any, ...]]:
        descriptors: list[int] = []
        root: Path | None = None
        execution_budget = self._budget
        try:
            with observe_execution_phase("geometry_codec", "capacity_wait"):
                lease = await self._capacity.admit_current_execution()
            host_descriptor = lease.descriptor
            deadline = lease.execution_deadline
            assert host_descriptor is not None and deadline is not None
            root = Path(tempfile.mkdtemp(prefix="cad2bim-geometry-codec-")).resolve(strict=True)
            os.chmod(root, 0o700)
            control_path = root / "control.frame"
            with observe_execution_phase("geometry_codec", "stage_payloads"):
                with control_path.open("xb") as stream:
                    write_json_frame(stream, control, limit=MAX_CODEC_CONTROL_BYTES)
                input_paths = []
                for index, payload in enumerate(input_payloads):
                    chunks = (payload,) if isinstance(payload, bytes) else payload
                    if not 1 <= sum(map(len, chunks)) <= MAX_CODEC_PAYLOAD_BYTES:
                        raise GeometryRuntimeValidationError(
                            "geometry codec input exceeds its byte budget"
                        )
                    path = root / f"input-{index:02d}.bin"
                    with path.open("xb") as stream:
                        for chunk in chunks:
                            write_all(stream, chunk)
                    input_paths.append(path)
            if _directory_regular_bytes(root) > self._budget.scratch_bytes:
                raise GeometryRuntimeValidationError(
                    "geometry codec inputs exceed the private scratch budget"
                )
            control_fd = _open_read_only(control_path)
            descriptors.append(control_fd)
            terminal_fd = _open_write_once(root / "terminal.frame")
            descriptors.append(terminal_fd)
            input_fds = []
            for path in input_paths:
                descriptor = _open_read_only(path)
                descriptors.append(descriptor)
                input_fds.append(descriptor)
            output_paths = tuple(
                root / f"output-{index:02d}.bin" for index in range(len(output_limits))
            )
            output_fds = []
            for path in output_paths:
                descriptor = _open_write_once(path)
                descriptors.append(descriptor)
                output_fds.append(descriptor)
            environment = _environment(root, threads=self._budget.threads)
            environment.update(
                {
                    "CAD2BIM_WORKER_TIMING": "1",
                    "CAD2BIM_GEOMETRY_CODEC_CONTROL_FD": str(control_fd),
                    "CAD2BIM_GEOMETRY_CODEC_TERMINAL_FD": str(terminal_fd),
                    "CAD2BIM_GEOMETRY_CODEC_INPUT_FDS": ",".join(map(str, input_fds)),
                    "CAD2BIM_GEOMETRY_CODEC_OUTPUT_FDS": ",".join(map(str, output_fds)),
                }
            )
            remaining_wall = deadline - time.monotonic()
            if remaining_wall <= 0:
                raise WorkerCapacityBusy(
                    "geometry codec invocation has insufficient remaining execution budget",
                    reason="execution_budget_exhausted",
                )
            # Staging and prior codec groups share the admitted call deadline.
            # Only server-owned wall follows that remainder; native resource
            # caps and the supervisor's absolute launch deadline stay intact.
            execution_budget = replace(self._budget, wall_seconds=remaining_wall)
            terminal = await self._supervisor.run(
                WorkerLaunchSpec(
                    command=self._sandbox.command(
                        scratch=root,
                        child_args=self._isolated_module_args(),
                    ),
                    cwd=root,
                    env=environment,
                    pass_fds=tuple(descriptors),
                    capacity_fds=(host_descriptor,),
                    scratch=root,
                ),
                budget=execution_budget,
                deadline=deadline,
                on_guard_registered=_GUARD_REGISTRATION.get(),
            )
            for descriptor in descriptors:
                os.close(descriptor)
            descriptors.clear()
            child_terminal = _read_frame(root / "terminal.frame", MAX_CODEC_TERMINAL_BYTES)
            if terminal.returncode != 0 or child_terminal.get("status") != "succeeded":
                self._raise_child_failure(
                    child_terminal,
                    operation=str(control["operation"]),
                )
            with observe_execution_phase("geometry_codec", "read_outputs"):
                if output_consumer is not None and len(output_paths) != 1:
                    raise self._untrusted("streamed codec output requires one descriptor")
                outputs = tuple(
                    _read_bounded_regular(path, limit, allow_empty=index in allow_empty_outputs,
                        consumer=None if output_consumer is None else lambda stream, size: output_consumer(child_terminal, stream, size))
                    for index,(path, limit) in enumerate(zip(output_paths, output_limits, strict=True))
                )
            return child_terminal, outputs
        except TimeoutError as exc:
            raise GeometryRuntimeUnavailableError("geometry codec worker capacity is busy") from exc
        except (WorkerCapacityBusy, WorkerCapacityUnavailable) as exc:
            raise GeometryRuntimeUnavailableError(
                str(exc), diagnostic=self._capacity.failure_diagnostic(exc),
                retryable=isinstance(exc, WorkerCapacityBusy) and exc.retryable,
            ) from exc
        except OSError as exc:
            raise GeometryRuntimeUnavailableError(
                "geometry codec worker infrastructure is unavailable"
            ) from exc
        except WorkerSupervisorError as exc:
            if exc.execution_started and exc.code in {"WORKER_TIMEOUT", "WORKER_RESOURCE_LIMIT"}:
                raise GeometryRuntimeResourceLimitError(
                    "geometry codec execution exceeded its fixed resource limit",
                    diagnostic=GeometryRuntimeResourceDiagnostic(
                        operation=control["operation"],
                        resource_kind=exc.resource_kind or "unknown",
                        configured_limits=asdict(exc.budget or execution_budget),
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
                with observe_execution_phase("geometry_codec", "cleanup"):
                    await asyncio.to_thread(shutil.rmtree, root, True)

    def _success_result(
        self,
        terminal: Mapping[str, Any],
        *,
        operation: str,
    ) -> dict[str, Any]:
        require_exact_keys(
            terminal,
            {"status", "operation", "execution_started", "result"},
            label="geometry codec terminal",
        )
        if (
            terminal["status"] != "succeeded"
            or terminal["operation"] != operation
            or terminal["execution_started"] is not True
            or not isinstance(terminal["result"], dict)
        ):
            raise self._untrusted("geometry codec terminal is not bound to the request")
        return terminal["result"]

    def _materialize_geometry(
        self,
        metadata: Mapping[str, Any],
        payload: bytes,
        *,
        expected_index: int,
    ) -> CanonicalGeometry:
        require_exact_keys(
            metadata,
            {"format", "content_type", "payload_index", "payload_size", "sha256", "summary"}
            | ({"source_provenance"} if "source_provenance" in metadata else set()),
            label="canonical geometry result metadata",
        )
        if (
            type(metadata["payload_index"]) is not int
            or type(metadata["payload_size"]) is not int
            or metadata["payload_index"] != expected_index
            or metadata["payload_size"] != len(payload)
        ):
            raise self._untrusted("canonical geometry payload metadata is invalid")
        if hashlib.sha256(payload).hexdigest() != metadata["sha256"]:
            raise self._untrusted("canonical geometry result SHA-256 mismatch")
        document = dict(metadata)
        document.pop("payload_index")
        document.pop("payload_size")
        document["payload"] = payload
        try:
            return CanonicalGeometry.model_validate(document)
        except ValidationError as exc:
            raise self._untrusted("canonical geometry result is invalid") from exc

    @staticmethod
    def _verify_canonical_input(geometry: CanonicalGeometry) -> None:
        if hashlib.sha256(geometry.payload).hexdigest() != geometry.sha256:
            raise GeometryRuntimeValidationError("canonical geometry payload SHA-256 mismatch")

    @staticmethod
    def _raise_child_failure(
        terminal: Mapping[str, Any],
        *,
        operation: str,
    ) -> None:
        require_exact_keys(
            terminal,
            {"status", "operation", "execution_started", "phase", "code", "message"},
            label="geometry codec failure terminal",
        )
        if (
            terminal["status"] != "failed"
            or terminal["operation"] not in (None, operation)
            or type(terminal["execution_started"]) is not bool
            or terminal["phase"] != ("execution" if terminal["execution_started"] else "preflight")
            or not isinstance(terminal["message"], str)
            or len(terminal["message"].encode("utf-8")) > 2_000
            or (terminal["operation"] is None and terminal["execution_started"])
            or terminal["code"]
            not in ("GEOMETRY_CODEC_VALIDATION_FAILED", "GEOMETRY_CODEC_EXECUTION_FAILED")
        ):
            raise GeometryCodecProtocolError(
                "geometry codec failure terminal is not bound to the request"
            )
        message = terminal["message"]
        if terminal["code"] == "GEOMETRY_CODEC_VALIDATION_FAILED":
            raise GeometryRuntimeValidationError(message)
        if operation == "normalize_batch":
            raise GeometryRuntimeUnavailableError(message, retryable=False)
        raise GeometryRuntimeValidationError("geometry codec execution failed")

    @staticmethod
    def _untrusted(detail: str) -> GeometryRuntimeValidationError:
        return GeometryRuntimeValidationError(f"geometry codec child returned {detail}")

    def _isolated_module_args(self) -> tuple[str, ...]:
        return (
            "-I",
            "-B",
            "-c",
            _ISOLATED_MODULE_BOOTSTRAP,
            str(self._source_root),
            "CAD2BIM_backend.infrastructure.geometry_runtime.codec_child",
        )

    def _require_open(self) -> None:
        if self._closed:
            raise GeometryRuntimeUnavailableError("geometry codec executor is closed")


def _read_frame(path: Path, limit: int) -> dict[str, Any]:
    try:
        with io.BytesIO(_read_bounded_regular(path, limit + 4)) as stream:
            result = read_json_frame(stream, limit=limit)
            if stream.read(1):
                raise WorkerProtocolError("worker protocol frame has trailing bytes")
            return result
    except (OSError, WorkerProtocolError) as exc:
        raise GeometryCodecProtocolError(
            "geometry codec child exited without a trusted terminal"
        ) from exc


def _read_bounded_regular(path: Path, limit: int, *, allow_empty: bool = False,
                          consumer: Callable[[BinaryIO, int], Any] | None = None) -> Any:
    descriptor = -1
    try:
        descriptor = _open_read_only(path)
        record = os.fstat(descriptor)
        if not stat.S_ISREG(record.st_mode) or not int(not allow_empty) <= record.st_size <= limit:
            raise GeometryCodecProtocolError("geometry codec output is not a bounded regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(limit + 1) if consumer is None else consumer(stream, record.st_size)
            consumed = stream.tell()
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise GeometryCodecProtocolError("geometry codec output could not be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if consumed != record.st_size or (
        record.st_dev,
        record.st_ino,
        record.st_size,
        record.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise GeometryCodecProtocolError("geometry codec output changed while being read")
    return payload


def _directory_regular_bytes(root: Path) -> int:
    return sum(
        path.stat(follow_symlinks=False).st_size
        for path in root.iterdir()
        if path.is_file() and not path.is_symlink()
    )


def _environment(root: Path, *, threads: int) -> dict[str, str]:
    thread_limit = str(threads)
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


__all__ = ["GEOMETRY_CODEC_BUDGET", "OcpGeometryCodecExecutor"]
