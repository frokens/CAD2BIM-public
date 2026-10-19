from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import json
import logging
import math
import os
import struct
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import cadquery as cq
import psutil
import pytest
from PIL import Image


from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometryBuildDisplayContextRequest,
    GeometryNormalizeRequest,
    GeometryRenderRequest,
    GeometryRuntimeResourceLimitError,
    GeometryRuntimeUnavailableError,
    GeometryRuntimeValidationError,
    GeometrySummary,
)
from CAD2BIM_backend.domain.runtime_execution import (
    GEOMETRY_CODEC_EXECUTION,
    GEOMETRY_PREVIEW_EXECUTION,
    PRODUCT_PREVIEW_EXECUTION,
    RuntimeExecutionPolicy,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.codec import normalize_geometry
from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution import (
    OcpGeometryCodecExecutor,
)
from CAD2BIM_backend.infrastructure.geometry_runtime import codec_execution
from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution_protocol import (
    GeometryCodecProtocolError,
    canonical_geometry_metadata,
    normalize_control,
    render_control,
    export_stl_control,
    require_descriptor_counts,
    require_exact_keys,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.straight_profile import (
    build_display_context,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import serialize_user_shape
from CAD2BIM_backend.infrastructure.execution_observability import (
    execution_observation_scope,
)
from CAD2BIM_backend.infrastructure.worker_runtime import (
    WorkerProcessBudget,
    WorkerProcessObservations,
    WorkerProcessSupervisor,
    WorkerSupervisorError,
    WorkerTerminal,
    write_json_frame,
)


TEST_BUDGET = WorkerProcessBudget(
    wall_seconds=30.0,
    cpu_seconds=30.0,
    rss_bytes=1024 * 1024**2,
    scratch_bytes=128 * 1024**2,
    threads=8,
    open_file_descriptors=64,
    stdout_bytes=16 * 1024,
    stderr_bytes=16 * 1024,
    cleanup_seconds=1.0,
)

_CANONICAL_REOPEN_FIXTURES = (
    Path(__file__).resolve().parents[2] / "fixtures" / "geometry_runtime" / "canonical_reopen"
)
_PERSISTED_PREVIEW_FIXTURES = (
    Path(__file__).resolve().parents[2] / "fixtures" / "geometry_runtime" / "persisted_preview"
)


def _raw_box(length: float) -> bytes:
    return serialize_user_shape(cq.Workplane("XY").box(length, 1, 1).val())


def _request(length: float = 1) -> GeometryNormalizeRequest:
    return GeometryNormalizeRequest(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        payload=_raw_box(length),
    )


def _mixed_shape() -> cq.Compound:
    return cq.Compound.makeCompound([
        cq.Face.makePlane(1, 1, (0, 0, 2)), cq.Solid.makeBox(1, 1, 1),
    ])


@pytest.mark.parametrize("make_shape, boundary_edges", [
    pytest.param(lambda: cq.Solid.makeBox(1, 1, 1), 0, id="closed-box"),
    pytest.param(lambda: cq.Face.makePlane(1, 1), 4, id="open-face"),
    pytest.param(lambda: cq.Shell.makeShell(cq.Solid.makeBox(1, 1, 1).Faces()[:-1]), 4, id="open-shell"),
    pytest.param(lambda: cq.Solid.makeCylinder(1, 2), 0, id="closed-cylinder-seam"),
    pytest.param(lambda: next(f for f in cq.Solid.makeCylinder(1, 2).Faces() if f.geomType() == "CYLINDER"), 2, id="open-cylinder-seam-and-circular-rims"),
    pytest.param(lambda: cq.Solid.makeSphere(1, angleDegrees1=-90, angleDegrees2=90), 0, id="sphere-degenerate-poles"),
    pytest.param(lambda: cq.Wire.makePolygon([cq.Vector(0, 0, 0), cq.Vector(1, 0, 0), cq.Vector(1, 1, 0)]), 0, id="free-open-wire"),
    pytest.param(lambda: cq.Workplane("XY").circle(1).val(), 0, id="free-closed-wire"),
    pytest.param(_mixed_shape, 4, id="box-plus-free-plane"),
])
def test_brep_summary_counts_actual_surface_boundary_edges(make_shape, boundary_edges):
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec import summarize_shape

    shape = make_shape()
    assert shape.isValid()
    assert summarize_shape(shape).topology.boundary_edge_count == boundary_edges


def test_current_boundary_summary_reaches_normalize_reopen_and_query_without_rewriting_history():
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec import reopen_geometry
    from CAD2BIM_backend.infrastructure.geometry_runtime.query_child import _GeometryQueryContext

    current = normalize_geometry(GeometryNormalizeRequest(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        payload=serialize_user_shape(_mixed_shape()),
    ))
    assert current.summary.topology.boundary_edge_count == 4
    historical = current.model_copy(update={"summary": current.summary.model_copy(update={
        "topology": current.summary.topology.model_copy(update={"boundary_edge_count": 0}),
    })})
    shape, measured = reopen_geometry(historical)
    assert measured.topology.boundary_edge_count == 4
    assert historical.summary.topology.boundary_edge_count == 0
    assert _GeometryQueryContext(shape, None).summary()["topology"]["boundary_edge_count"] == 4


def test_normalize_optional_summary_keeps_actual_validation_and_closed_wire(monkeypatch):
    from CAD2BIM_backend.infrastructure.geometry_runtime import codec
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec_child import _normalize
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution_protocol import normalize_control
    import io

    request = _request().model_copy(update={"include_summary": False})
    calls = []
    original = codec._validate_brep
    def validate(shape):
        calls.append("validate")
        original(shape)
    monkeypatch.setattr(codec, "_validate_brep", validate)
    def unexpected(_shape):
        pytest.fail("unrequested complete summary was computed")
    monkeypatch.setattr(codec, "summarize_shape", unexpected)
    output = io.BytesIO()
    result = _normalize(normalize_control((request,), include_raw_brep_summary=False),
                        (io.BytesIO(request.payload),), (output,))
    assert calls == ["validate"]
    assert result["items"][0]["summary"] is None
    assert output.getvalue() == request.payload
    assert (OcpGeometryCodecExecutor._normalization_key(request, False)
            != OcpGeometryCodecExecutor._normalization_key(request.model_copy(update={"include_summary": True}), False))


@pytest.mark.parametrize("requested", [True, False])
def test_codec_rejects_summary_presence_not_matching_demand(tmp_path, requested):
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution_protocol import canonical_geometry_metadata
    request = _request().model_copy(update={"include_summary": requested})
    geometry = normalize_geometry(request.model_copy(update={"include_summary": not requested}))
    metadata = {**canonical_geometry_metadata(geometry, payload_index=0), "raw_summary": None}
    with pytest.raises(GeometryRuntimeValidationError, match="measurements do not match"):
        _executor(tmp_path)._decode_normalized((request,), {"items": [metadata]}, (geometry.payload,), False)


def _executor(tmp_path: Path, **kwargs: Any) -> OcpGeometryCodecExecutor:
    return OcpGeometryCodecExecutor(
        budget=TEST_BUDGET,
        capacity_root=tmp_path / "capacity",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_saved_brep_exports_finite_metre_stl_and_png_from_same_source(tmp_path):
    # Translation and unequal extents expose accidental centring or unit conversion.
    saved = normalize_geometry(GeometryNormalizeRequest(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        payload=serialize_user_shape(cq.Workplane("XY").box(.4, .2, .1).val().translate((2, 3, 4)))))
    runtime = _executor(tmp_path)
    try:
        mesh = await runtime.export_stl(saved)
        preview = await runtime.render(GeometryRenderRequest(geometry=saved, width_px=96, height_px=96))
        count = struct.unpack_from("<I", mesh.payload, 80)[0]
        assert count > 0 and len(mesh.payload) == 84 + count * 50
        vertices = []
        for index in range(count):
            values = struct.unpack_from("<12fH", mesh.payload, 84 + 50 * index)
            assert all(math.isfinite(value) for value in values[:-1])
            vertices.extend(tuple(values[start:start + 3]) for start in (3, 6, 9))
        bounds = tuple((min(item[axis] for item in vertices), max(item[axis] for item in vertices)) for axis in range(3))
        for actual, expected in zip(bounds, ((1.8, 2.2), (2.9, 3.1), (3.95, 4.05)), strict=True):
            assert actual == pytest.approx(expected)
        assert mesh.unit == "METRE" and mesh.geometry_sha256 == saved.sha256
        assert mesh.sha256 == hashlib.sha256(mesh.payload).hexdigest()
        image = Image.open(io.BytesIO(preview.payload))
        image.load()
        assert image.format == "PNG" and image.size == (96, 96)
    finally:
        await runtime.close()


def test_stl_control_has_closed_single_saved_geometry_transport():
    geometry = normalize_geometry(_request())
    control = export_stl_control(geometry)
    require_exact_keys(control, {"operation", "geometry", "input_count", "output_count"}, label="STL control")
    require_descriptor_counts(control, input_count=1, output_count=1)
    assert control["geometry"]["sha256"] == geometry.sha256
    with pytest.raises(GeometryCodecProtocolError):
        require_descriptor_counts(control, input_count=2, output_count=1)


def test_stl_rejects_triangles_collapsed_by_its_real_float32_format():
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec import MeshData, write_stl
    mesh = MeshData(vertices=[[1e9, 1e9, 1e9], [1e9 + 1, 1e9, 1e9], [1e9, 1e9 + 1, 1e9]], triangles=[[0, 1, 2]])
    with pytest.raises(GeometryRuntimeValidationError, match="float32 precision"):
        write_stl(mesh)


def _phase_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        record.cad2bim_execution_phase
        for record in caplog.records
        if hasattr(record, "cad2bim_execution_phase")
    ]


@pytest.mark.asyncio
async def test_real_multiframe_codec_batch_binds_items_and_continues_only_pre_native_failure(tmp_path, caplog):
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution_protocol import GeometryCodecItemFailure
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    runtime = _executor(tmp_path)
    requests = tuple(_request(length).model_copy(update={"include_summary": length != 2})
                     for length in (1, 2, 3))
    invalid_mesh = GeometryNormalizeRequest(format="indexed_triangle_mesh_v1", payload=b"invalid-json")
    batch = (requests[0], invalid_mesh, requests[1], requests[2], *([requests[0]] * 5))
    try:
        values = await runtime.normalize_batch(batch)
        assert len(values) == 9
        assert isinstance(values[1], GeometryCodecItemFailure)
        assert values[1].item_index == 1 and values[1].code == "MESH_INPUT_INVALID"
        assert values[1].input_sha256 == hashlib.sha256(invalid_mesh.payload).hexdigest()
        for index, request in enumerate(batch):
            if index != 1:
                assert values[index][0] == normalize_geometry(request)
        processes = [r for r in _phase_records(caplog) if r["component"] == "worker_runtime" and r["phase"] == "process" and r["transition"] == "end"]
        assert len(processes) == 1
        events = processes[0]["details"]["child_timing"]
        assert len({e["pid"] for e in events}) == 1
        assert len([e for e in events if e["phase"] == "geometry_dependencies_import"
                    and e["details"].get("initial_load") and e["details"].get("transition") == "end"]) == 1
        assert len([e for e in events if e["phase"] == "batch_item"]) == 8
        # Three distinct valid requests in the first frame, then one in the
        # second. Repeated occurrences keep their wire/result identities.
        assert len([e for e in events if e["phase"] == "normalize_item"
                    and e["details"].get("transition") == "end"]) == 4
        assert len([e for e in events if e["phase"] == "reuse_normalized_item"]) == 4
        bootstrap = next(e for e in events if e["phase"] == "bootstrap")
        imports = next(e for e in events if e["phase"] == "module_import")
        assert processes[0]["details"]["launch_started_ns"] <= bootstrap["start_ns"] <= bootstrap["end_ns"] <= imports["end_ns"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_multiframe_codec_native_failure_aborts_without_partial_results(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    runtime = _executor(tmp_path)
    valid = _request().model_copy(update={"include_summary": False})
    invalid_brep = GeometryNormalizeRequest(format="occt_brep_v1", payload=b"invalid-native-brep", include_summary=False)
    try:
        with pytest.raises(GeometryRuntimeValidationError, match="batch_item_index=1 item=2/3"):
            await runtime.normalize_batch((valid, invalid_brep, valid))
        processes = [r for r in _phase_records(caplog) if r["component"] == "worker_runtime" and r["phase"] == "process" and r["transition"] == "end"]
        assert len(processes) == 1
        items = [e["details"]["item_index"] for e in processes[0]["details"]["child_timing"] if e["phase"] == "batch_item"]
        assert items == [0, 1]
        # The original capacity owner can immediately admit the next bounded batch.
        result = await runtime.normalize_batch((valid,))
        assert result[0][0] == normalize_geometry(valid)
    finally:
        await runtime.close()


@pytest.mark.parametrize("all_recoverable", [False, True])
def test_codec_batch_uses_shared_kernel_without_forced_gc_or_single_request_rewrap(monkeypatch, all_recoverable):
    import gc
    import io
    from CAD2BIM_backend.infrastructure.geometry_runtime import codec_child
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution_protocol import normalize_batch_control
    request = (GeometryNormalizeRequest(format="indexed_triangle_mesh_v1", payload=b"{}", include_summary=False)
               if all_recoverable else _request().model_copy(update={"include_summary": False}))
    control, payload = normalize_batch_control((request,) * 33, include_raw_brep_summary=False)
    def forbidden(*args, **kwargs):
        pytest.fail("batch called a removed per-item GC or single-request wrapper")
    monkeypatch.setattr(gc, "collect", forbidden)
    monkeypatch.setattr(codec_child, "_normalize", forbidden)
    result = codec_child._normalize_batch(control, io.BytesIO(b"".join(payload)), io.BytesIO())
    assert result == {"item_count": 33, "frame_count": 5, "recoverable_failures": 33 if all_recoverable else 0}


@pytest.mark.parametrize("include_raw", [False, True])
def test_codec_frame_memo_preserves_summary_transform_and_raw_results(monkeypatch, include_raw):
    import io
    from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
    from CAD2BIM_backend.infrastructure.geometry_runtime import codec_child
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution_protocol import MAX_CODEC_CONTROL_BYTES, normalize_batch_control
    from CAD2BIM_backend.infrastructure.worker_runtime.protocol import read_json_frame

    base = _request()
    no_summary = base.model_copy(update={"include_summary": False})
    moved = base.model_copy(update={"transform": RigidTransform3D(translation_m=(1., 0., 0.))})
    requests = (base, base, no_summary, no_summary, moved, moved)
    control, payload = normalize_batch_control(requests, include_raw_brep_summary=include_raw)
    original = codec_child._normalize_request
    computed = []
    def observe(request, **kwargs):
        computed.append((request, kwargs["include_raw"]))
        return original(request, **kwargs)
    monkeypatch.setattr(codec_child, "_normalize_request", observe)
    output = io.BytesIO()
    result = codec_child._normalize_batch(control, io.BytesIO(b"".join(payload)), output)
    assert result["item_count"] == 6 and result["recoverable_failures"] == 0
    assert computed == [(base, include_raw), (no_summary, include_raw), (moved, include_raw)]
    output.seek(0)
    frame = read_json_frame(output, limit=MAX_CODEC_CONTROL_BYTES)
    for index, item in enumerate(frame["items"]):
        assert item["item_index"] == index
        geometry = item["geometry"]
        assert (geometry["summary"] is not None) == requests[index].include_summary
        assert (geometry["raw_summary"] is not None) == include_raw
        content = output.read(geometry["payload_size"])
        assert hashlib.sha256(content).hexdigest() == geometry["sha256"]
        if index % 2:
            assert geometry == frame["items"][index-1]["geometry"]
    assert not output.read(1)


def test_codec_batch_protocol_bounds_count_bytes_and_frames():
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution_protocol import normalize_batch_control
    request = GeometryNormalizeRequest(format="indexed_triangle_mesh_v1", payload=b"{}")
    with pytest.raises(GeometryCodecProtocolError, match="1..1024"):
        normalize_batch_control((request,) * 1025, include_raw_brep_summary=False)
    extended, _ = normalize_batch_control((request,) * 33, include_raw_brep_summary=False)
    assert extended["frame_count"] == 5 and extended["item_count"] == 33
    with pytest.raises(GeometryCodecProtocolError, match="byte budget"):
        normalize_batch_control((request.model_copy(update={"payload": b"x" * (33 * 1024 * 1024)}),), include_raw_brep_summary=False)
    control, payload = normalize_batch_control((request,) * 9, include_raw_brep_summary=False)
    assert control["frame_count"] == 2 and control["item_count"] == 9
    assert control["input_bytes"] == sum(map(len, payload))
    assert payload[1] is request.payload


@pytest.mark.asyncio
async def test_codec_batch_does_not_cache_partial_decode_after_forged_identity(tmp_path, monkeypatch):
    request = _request()
    geometry = normalize_geometry(request)
    metadata = {**canonical_geometry_metadata(geometry, payload_index=0), "raw_summary": None}
    output = io.BytesIO()
    write_json_frame(output, {"frame_index": 0, "items": [
        {"item_index": 0, "input_sha256": hashlib.sha256(request.payload).hexdigest(), "status": "succeeded", "geometry": metadata},
        {"item_index": 1, "input_sha256": "0" * 64, "status": "succeeded", "geometry": metadata},
    ]}, limit=256 * 1024)
    output.write(geometry.payload * 2)
    terminal = {"status": "succeeded", "operation": "normalize_batch", "execution_started": True,
                "result": {"item_count": 2, "frame_count": 1, "recoverable_failures": 0}}
    runtime = _executor(tmp_path)
    async def run(_control, _inputs, _limits, *, output_consumer):
        return terminal, (output_consumer(terminal, io.BytesIO(output.getvalue()), len(output.getvalue())),)
    monkeypatch.setattr(runtime, "_run", run)
    try:
        async with runtime.execution_scope():
            with pytest.raises(GeometryRuntimeValidationError, match="identity/hash"):
                await runtime.normalize_batch((request, request))
            assert runtime._verified_values() == {}
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fits", [True, False])
async def test_cached_codec_batch_retains_output_budget_and_worker_accounting(tmp_path, monkeypatch, fits):
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution_protocol import MAX_CODEC_CONTROL_BYTES

    payload = b'{"schema":"indexed_triangle_mesh_v1","unit":"METRE","vertices":[[0,0,0],[1,0,0],[0,1,0]],"triangles":[[0,1,2]]}'
    request = GeometryNormalizeRequest(format="indexed_triangle_mesh_v1", payload=payload, include_summary=False)
    geometry = CanonicalGeometry(format=request.format, content_type=request.format.content_type,
        payload=payload, sha256=hashlib.sha256(payload).hexdigest(), summary=None)
    requests = (request,) * 9
    values = ((geometry, None),) * len(requests)
    upper_bound = len(payload) * len(requests) + 2 * (MAX_CODEC_CONTROL_BYTES + 4)
    monkeypatch.setattr(codec_execution, "MAX_CODEC_BATCH_OUTPUT_BYTES", upper_bound if fits else upper_bound-1)
    runtime = _executor(tmp_path)
    run = AsyncMock(return_value=({}, (values,)))
    monkeypatch.setattr(runtime, "_run", run)
    try:
        async with runtime.execution_scope():
            runtime._remember(request, values[0])
            assert await runtime.normalize_batch(requests) == values
        assert run.await_count == (0 if fits else 1)
        if not fits:
            assert run.call_args.args[0]["item_count"] == 9
            assert run.call_args.args[0]["frame_count"] == 2
            assert run.call_args.args[2] == (upper_bound-1,)
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["growth", "truncation"])
async def test_batch_stream_rejects_changed_file_before_reading_beyond_initial_extent(tmp_path, monkeypatch, mutation):
    request = _request()
    geometry = normalize_geometry(request)
    metadata = {**canonical_geometry_metadata(geometry, payload_index=0), "raw_summary": None}
    if mutation == "growth":
        metadata["payload_size"] += 4
    output = io.BytesIO()
    write_json_frame(output, {"frame_index": 0, "items": [{
        "item_index": 0, "input_sha256": hashlib.sha256(request.payload).hexdigest(),
        "status": "succeeded", "geometry": metadata,
    }]}, limit=256 * 1024)
    output.write(geometry.payload)
    path = tmp_path / "output.bin"
    path.write_bytes(output.getvalue())
    terminal = {"status": "succeeded", "operation": "normalize_batch", "execution_started": True,
                "result": {"item_count": 1, "frame_count": 1, "recoverable_failures": 0}}
    runtime = _executor(tmp_path)
    if mutation == "growth":
        def forbidden(*args):
            pytest.fail("grew past the initial file extent before rejecting metadata")
        monkeypatch.setattr(runtime, "_decode_normalized_item", forbidden)
    def consume(stream, byte_count):
        if mutation == "growth":
            with path.open("ab") as writer:
                writer.write(b"xxxx")
        else:
            with path.open("r+b") as writer:
                writer.truncate(byte_count - 4)
        return runtime._decode_batch_output((request,), {"frame_count": 1}, terminal, stream, False, byte_count)
    try:
        async with runtime.execution_scope():
            with pytest.raises(GeometryRuntimeValidationError, match="payload.*(size|metadata)"):
                codec_execution._read_bounded_regular(path, 64 * 1024 * 1024, consumer=consume)
            assert runtime._verified_values() == {}
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="the approved codec child uses Seatbelt")
@pytest.mark.parametrize("batch", [False, True])
async def test_codec_reuses_verified_bytes_only_within_one_owned_scope(tmp_path: Path, batch: bool) -> None:
    class CountingSupervisor(WorkerProcessSupervisor):
        def __init__(self):
            self.calls = 0

        async def run(self, *args, **kwargs):
            self.calls += 1
            return await super().run(*args, **kwargs)

    supervisor = CountingSupervisor()
    runtime = _executor(tmp_path, supervisor=supervisor)
    request = _request()
    async def normalize():
        if not batch:
            return await runtime.normalize(request)
        values = await runtime.normalize_batch((request, request))
        assert values[0] == values[1]
        return values[0][0]

    async with runtime.execution_scope():
        geometry = await normalize()
        assert await normalize() == geometry
        if batch:
            # A cache hit does not grant a larger input contract or bypass the
            # framed request's raw-summary flag validation.
            with pytest.raises(GeometryCodecProtocolError, match="1..1024"):
                await runtime.normalize_batch((request,) * 1025)
            with pytest.raises(GeometryCodecProtocolError, match="boolean"):
                await runtime.normalize_batch((request,), include_raw_brep_summary=1)
        assert await runtime.reopen(geometry) == geometry.summary
        assert supervisor.calls == 1
        wrong = geometry.model_copy(update={"summary": geometry.summary.model_copy(update={"volume_m3": 999.0})})
        assert await runtime.reopen(wrong) == geometry.summary
        assert supervisor.calls == 1
        runtime.release_execution()
        assert await normalize() == geometry
        assert supervisor.calls == 1
    assert await normalize() == geometry
    assert supervisor.calls == 2
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="the approved codec child uses Seatbelt")
async def test_reopen_does_not_cache_historical_incorrect_mixed_shape_summary(tmp_path: Path) -> None:
    mixed = _mixed_shape()
    request = GeometryNormalizeRequest(format=CanonicalGeometryFormat.OCCT_BREP_V1, payload=serialize_user_shape(mixed))
    current = normalize_geometry(request)
    historical = current.model_copy(update={"summary": current.summary.model_copy(update={"volume_m3": 7.0})})
    runtime = _executor(tmp_path)
    try:
        async with runtime.execution_scope():
            for _ in range(2):
                measured = await runtime.reopen(historical)
                assert measured.volume_m3 is None
                assert historical.summary.volume_m3 == 7.0
            normalized = await runtime.normalize(request)
            assert normalized.summary.volume_m3 is None
            assert await runtime.reopen(normalized) == normalized.summary
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_codec_entrypoints_emit_one_closed_phase_for_each_exact_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    runtime = _executor(tmp_path)
    marker = object()
    normalize = AsyncMock(return_value=marker)
    reopen = AsyncMock(return_value=marker)
    render = AsyncMock(return_value=marker)
    display_context = AsyncMock(return_value=marker)
    monkeypatch.setattr(runtime, "_normalize_many", normalize)
    monkeypatch.setattr(runtime, "_reopen", reopen)
    monkeypatch.setattr(runtime, "_render", render)
    monkeypatch.setattr(runtime, "_build_display_context", display_context)

    with execution_observation_scope(
        operation="ifc_product_realization_preview",
        input_sha256="b" * 64,
    ) as correlation_id:
        assert await runtime.normalize_many((_request(1), _request(2))) is marker
        assert await runtime.reopen(marker) is marker
        assert await runtime.render(marker) is marker
        assert await runtime.build_display_context(marker) is marker

    normalize.assert_awaited_once()
    phases = [
        record for record in _phase_records(caplog) if record["component"] == "geometry_codec"
    ]
    assert [(record["phase"], record["transition"], record["status"]) for record in phases] == [
        ("normalize", "start", "running"),
        ("normalize", "end", "succeeded"),
        ("reopen", "start", "running"),
        ("reopen", "end", "succeeded"),
        ("render", "start", "running"),
        ("render", "end", "succeeded"),
        ("build_display_context", "start", "running"),
        ("build_display_context", "end", "succeeded"),
    ]
    assert {record["correlation_id"] for record in phases} == {correlation_id}
    assert {record["operation"] for record in phases} == {"ifc_product_realization_preview"}
    assert {record["input_sha256"] for record in phases} == {"b" * 64}


def _walk_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_walk_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_walk_keys(item) for item in value))
    return set()


def test_protocol_uses_closed_metadata_without_inline_binary_or_paths() -> None:
    requests = (_request(1), _request(2), _request(3))
    control = normalize_control(requests, include_raw_brep_summary=True)

    assert control["operation"] == "normalize"
    assert [item["payload_index"] for item in control["inputs"]] == [0, 1, 2]
    assert [item["payload_sha256"] for item in control["inputs"]] == [
        hashlib.sha256(request.payload).hexdigest() for request in requests
    ]
    assert not {"payload", "path", "base64"}.intersection(_walk_keys(control))
    with pytest.raises(GeometryCodecProtocolError, match="closed key set"):
        require_exact_keys({"status": "succeeded", "extra": True}, {"status"}, label="terminal")
    with pytest.raises(GeometryCodecProtocolError, match="descriptor counts"):
        require_descriptor_counts(control, input_count=2, output_count=3)
    with pytest.raises(ValueError, match="1..8"):
        normalize_control((), include_raw_brep_summary=False)
    with pytest.raises(ValueError, match="1..8"):
        normalize_control(tuple(_request() for _ in range(9)), include_raw_brep_summary=False)


@pytest.mark.asyncio
async def test_real_child_executes_all_four_operations_and_preserves_batch_order(
    tmp_path: Path,
) -> None:
    runtime = _executor(tmp_path)
    requests = tuple(_request(length) for length in range(1, 9))
    try:
        await runtime.verify_ready()
        normalized = await runtime.normalize_many(
            requests,
            include_raw_brep_summary=True,
        )
        assert [item[0].summary.volume_m3 for item in normalized] == pytest.approx(range(1, 9))
        assert [item[1].volume_m3 for item in normalized if item[1] is not None] == pytest.approx(
            range(1, 9)
        )
        assert await runtime.reopen(normalized[1][0]) == normalized[1][0].summary

        display_request = GeometryBuildDisplayContextRequest.model_validate(
            {
                "recipes": [
                    {
                        "recipe_key": "candidate:test",
                        "frame": {"translation_m": [0, 0, 1]},
                        "profile": {"kind": "circular", "diameter_m": 0.2},
                        "length_m": 0.5,
                    }
                ]
            }
        )
        display = await runtime.build_display_context(display_request)
        assert display == build_display_context(display_request)

        preview = await runtime.render(
            GeometryRenderRequest.model_validate(
                {
                    "geometry": normalized[0][0],
                    "display_context": display.geometry,
                    "scene": {
                        "coordinate_space": "geometry_coordinates",
                        "geometry_sha256": normalized[0][0].sha256,
                        "display_context_sha256": display.geometry.sha256,
                        "interface_overlays": [
                            {
                                "overlay_id": "host:test",
                                "label": "original",
                                "marker_kind": "host_attachment",
                                "frame": {},
                                "profile": {"kind": "circular", "diameter_m": 0.2},
                            },
                            {
                                "overlay_id": "candidate:test",
                                "label": "candidate",
                                "marker_kind": "host_candidate",
                                "frame": {"translation_m": [0, 0, 1]},
                                "profile": {"kind": "circular", "diameter_m": 0.2},
                            },
                        ],
                        "host_overlays": [
                            {
                                "overlay_id": "host:test",
                                "construction_kind": "retained_straight",
                            }
                        ],
                        "straight_profile_overlays": [
                            {
                                "recipe_key": "host:test",
                                "frame": {},
                                "profile": {"kind": "circular", "diameter_m": 0.2},
                                "length_m": 1,
                                "display_kind": "original",
                            },
                            {
                                "recipe_key": "candidate:test",
                                "frame": {"translation_m": [0, 0, 1]},
                                "profile": {"kind": "circular", "diameter_m": 0.2},
                                "length_m": 0.5,
                                "display_kind": "candidate",
                            },
                        ],
                    },
                    "width_px": 160,
                    "height_px": 128,
                    "view": "isometric",
                }
            )
        )
        assert Image.open(io.BytesIO(preview.payload)).size == (160, 128)
        assert preview.sha256 == hashlib.sha256(preview.payload).hexdigest()
        assert tuple(panel.panel_key for panel in preview.resolved_panels) == ("isometric",)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_child_preserves_legacy_and_oblique_brep_in_one_ordered_batch(
    tmp_path: Path,
) -> None:
    legacy_payload = (_CANONICAL_REOPEN_FIXTURES / "mapped_body_canonical.brep").read_bytes()
    fallback_payload = (_CANONICAL_REOPEN_FIXTURES / "oblique_boolean_raw.brep").read_bytes()
    assert hashlib.sha256(legacy_payload).hexdigest() == (
        "50c1e2f497b0fce8e26cc24cbf6b810b5120d6adf7cf7f3e3ec4395abc6d40f9"
    )
    assert hashlib.sha256(fallback_payload).hexdigest() == (
        "3bfda8db82d9f6aeb0739f40ef339aad4da24e3286feb2b50a2595a448019df1"
    )
    requests = tuple(
        GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.OCCT_BREP_V1,
            payload=payload,
        )
        for payload in (legacy_payload, fallback_payload)
    )
    runtime = _executor(tmp_path)
    try:
        normalized = await runtime.normalize_many(
            requests,
            include_raw_brep_summary=True,
        )
        assert len(normalized) == 2
        legacy, fallback = normalized
        assert legacy[0].payload == legacy_payload
        assert legacy[0].sha256 == (
            "50c1e2f497b0fce8e26cc24cbf6b810b5120d6adf7cf7f3e3ec4395abc6d40f9"
        )
        assert legacy[1] == legacy[0].summary
        assert fallback[0].sha256 == (
            "3bfda8db82d9f6aeb0739f40ef339aad4da24e3286feb2b50a2595a448019df1"
        )
        assert fallback[1] == fallback[0].summary
        assert await runtime.reopen(legacy[0]) == legacy[0].summary
        assert await runtime.reopen(fallback[0]) == fallback[0].summary

        replay = await runtime.normalize_many(
            tuple(
                GeometryNormalizeRequest(format=geometry.format, payload=geometry.payload)
                for geometry, _raw_summary in normalized
            ),
            include_raw_brep_summary=True,
        )
        assert tuple(item[0].payload for item in replay) == tuple(
            item[0].payload for item in normalized
        )
        assert tuple(item[0].sha256 for item in replay) == tuple(
            item[0].sha256 for item in normalized
        )
        assert tuple(item[1] for item in replay) == tuple(item[0].summary for item in normalized)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_child_reopens_persisted_mapped_brep_with_literal_summary(
    tmp_path: Path,
) -> None:
    payload_path = _CANONICAL_REOPEN_FIXTURES / "mapped_body_canonical.brep"
    payload = payload_path.read_bytes()
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    assert payload_sha256 == ("50c1e2f497b0fce8e26cc24cbf6b810b5120d6adf7cf7f3e3ec4395abc6d40f9")
    # Literal persisted value from the frozen Scene A publication audit
    # (audit SHA-256 c86ebff6b749006922e97ed85107b31a5b1ea54e9aec8f952913849b2e452d45).
    persisted_summary = GeometrySummary.model_validate(
        {
            "format": "occt_brep_v1",
            "unit": "METRE",
            "bounds": {
                "minimum_m": [-0.7, -0.7, -2.0],
                "maximum_m": [0.7, 0.7, 0.0],
            },
            "topology": {
                "vertex_count": 20,
                "edge_count": 30,
                "wire_count": 30,
                "face_count": 22,
                "shell_count": 6,
                "solid_count": 6,
                "triangle_count": 5032,
                "boundary_edge_count": 0,
            },
            "surface_area_m2": 22.337424373406,
            "volume_m3": 0.548814238991,
        }
    )
    persisted = CanonicalGeometry(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
        payload=payload,
        sha256=payload_sha256,
        summary=persisted_summary,
    )

    runtime = _executor(tmp_path)
    try:
        reopened = await runtime.reopen(persisted)
        assert reopened.topology.triangle_count is None
        assert reopened.bounds.minimum_m == pytest.approx(persisted_summary.bounds.minimum_m)
        assert reopened.bounds.maximum_m == pytest.approx(persisted_summary.bounds.maximum_m)
        assert reopened.volume_m3 == pytest.approx(persisted_summary.volume_m3)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_child_reopens_persisted_host_display_context_with_literal_summary(
    tmp_path: Path,
) -> None:
    manifest = json.loads((_PERSISTED_PREVIEW_FIXTURES / "manifest.json").read_bytes())
    fixture = manifest["fixtures"][0]
    payload_path = _PERSISTED_PREVIEW_FIXTURES / fixture["file"]
    payload = payload_path.read_bytes()
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    assert payload_sha256 == ("9448c505bb895cf304ba95b13d085f5befd372350b9b1c0d8a4acde4d418125c")
    assert fixture["payload_sha256"] == payload_sha256
    assert fixture["size_bytes"] == len(payload) == 2711
    assert fixture["source_evidence"]["snapshot_sha256"] == (
        "7d9246bf95c8fbb87be4f5f631ff5ee458e6b5330d5fb55ffea51308c9476349"
    )
    assert fixture["recipe_digest"] == (
        "9f015e01d22ba54c71fb3de590d2dda8124161b6488896af1c704d15c3be1def"
    )
    assert fixture["recipe_keys"] == ["host:main_in_host", "host:main_out_host"]
    persisted_summary = GeometrySummary.model_validate(fixture["literal_summary"])
    persisted = CanonicalGeometry(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
        payload=payload,
        sha256=payload_sha256,
        summary=persisted_summary,
    )

    runtime = _executor(tmp_path)
    try:
        reopened = await runtime.reopen(persisted)
        assert reopened.topology.triangle_count is None
        assert reopened.bounds.minimum_m == pytest.approx(persisted_summary.bounds.minimum_m)
        assert reopened.bounds.maximum_m == pytest.approx(persisted_summary.bounds.maximum_m)
        assert reopened.volume_m3 == pytest.approx(persisted_summary.volume_m3)
        assert payload_path.read_bytes() == payload
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_child_normalizes_and_reopens_spherical_junction_exactly(
    tmp_path: Path,
) -> None:
    payload_path = _CANONICAL_REOPEN_FIXTURES / "spherical_junction_raw.brep"
    raw_payload = payload_path.read_bytes()
    raw_sha256 = hashlib.sha256(raw_payload).hexdigest()
    assert raw_sha256 == ("1a0fbd88b81307ca3bbd644353232b3c48db6eb264db2473014078fde50cc13e")
    request = GeometryNormalizeRequest(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        payload=raw_payload,
    )

    runtime = _executor(tmp_path)
    try:
        first = await runtime.normalize_many(
            (request,),
            include_raw_brep_summary=True,
        )
        assert len(first) == 1
        geometry, raw_summary = first[0]
        assert raw_summary == geometry.summary
        assert geometry.sha256 == (
            "1a0fbd88b81307ca3bbd644353232b3c48db6eb264db2473014078fde50cc13e"
        )
        assert geometry.sha256 == hashlib.sha256(geometry.payload).hexdigest()
        assert await runtime.reopen(geometry) == geometry.summary

        replay = await runtime.normalize_many(
            (
                GeometryNormalizeRequest(
                    format=geometry.format,
                    payload=geometry.payload,
                ),
            ),
            include_raw_brep_summary=True,
        )
        assert replay == ((geometry, geometry.summary),)
        assert payload_path.read_bytes() == raw_payload
    finally:
        await runtime.close()


class _NoopSandbox:
    def command(self, *, scratch: Path, child_args: tuple[str, ...]) -> tuple[str, ...]:
        assert scratch.is_dir()
        assert child_args[:3] == ("-I", "-B", "-c")
        return (sys.executable, "-c", "pass")


class _ForgedNormalizeSupervisor:
    def __init__(self, geometry) -> None:
        self.geometry = geometry

    async def run(self, spec, *, budget, deadline):
        output_fd = int(spec.env["CAD2BIM_GEOMETRY_CODEC_OUTPUT_FDS"])
        terminal_fd = int(spec.env["CAD2BIM_GEOMETRY_CODEC_TERMINAL_FD"])
        os.write(output_fd, b"forged")
        metadata = canonical_geometry_metadata(self.geometry, payload_index=0)
        metadata["raw_summary"] = None
        with os.fdopen(os.dup(terminal_fd), "wb") as stream:
            write_json_frame(
                stream,
                {
                    "status": "succeeded",
                    "operation": "normalize",
                    "execution_started": True,
                    "result": {"items": [metadata]},
                },
                limit=32 * 1024,
            )
        return WorkerTerminal(
            returncode=0,
            status_payload=None,
            stdout=b"",
            stderr=b"",
            execution_started=True,
            observations=WorkerProcessObservations(),
        )


@pytest.mark.asyncio
async def test_parent_rejects_forged_binary_even_with_typed_terminal(
    tmp_path: Path,
) -> None:
    request = _request()
    geometry = normalize_geometry(request)
    runtime = _executor(
        tmp_path,
        supervisor=_ForgedNormalizeSupervisor(geometry),
        sandbox=_NoopSandbox(),
    )
    with pytest.raises(GeometryRuntimeValidationError, match="payload metadata"):
        await runtime.normalize(request)


class _ResourceLimitSupervisor:
    async def run(self, spec, *, budget, deadline, on_guard_registered=None):
        failure = WorkerSupervisorError(
            "WORKER_RESOURCE_LIMIT",
            "worker exceeded its RSS budget",
            execution_started=True,
            resource_kind="rss",
        )
        failure.budget = budget
        failure.observations = WorkerProcessObservations(peak_rss_bytes=budget.rss_bytes + 1)
        failure.elapsed_seconds = 2.0
        failure.effective_wall_seconds = budget.wall_seconds
        raise failure


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["normalize", "export_stl"])
async def test_executed_resource_limit_uses_root_owned_narrow_domain_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    operation: str,
) -> None:
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    runtime = _executor(
        tmp_path,
        supervisor=_ResourceLimitSupervisor(),
        sandbox=_NoopSandbox(),
    )
    with pytest.raises(GeometryRuntimeResourceLimitError, match="fixed resource limit") as raised:
        if operation == "normalize":
            await runtime.normalize(_request())
        else:
            await runtime.export_stl(normalize_geometry(_request()))
    diagnostic = raised.value.diagnostic
    assert diagnostic.operation == operation
    assert diagnostic.resource_kind == "rss"
    assert diagnostic.configured_limits["rss_bytes"] == TEST_BUDGET.rss_bytes
    assert diagnostic.sampled_peaks["peak_rss_bytes"] == TEST_BUDGET.rss_bytes + 1
    assert diagnostic.elapsed_seconds == 2.0
    assert 0 < diagnostic.effective_wall_seconds <= GEOMETRY_CODEC_EXECUTION.execution_seconds
    assert diagnostic.configured_limits["wall_seconds"] == diagnostic.effective_wall_seconds
    records = [
        record for record in _phase_records(caplog)
        if record["component"] == "geometry_codec" and record["phase"] == operation
    ]
    assert [(record["transition"], record["status"]) for record in records] == [
        ("start", "running"),
        ("end", "failed"),
    ]


class _BlockingSupervisor:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.scratch: Path | None = None

    async def run(self, spec, *, budget, deadline):
        self.scratch = spec.scratch
        self.entered.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_cancellation_releases_capacity_descriptors_and_scratch(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    supervisor = _BlockingSupervisor()
    runtime = _executor(tmp_path, supervisor=supervisor, sandbox=_NoopSandbox())
    task = asyncio.create_task(runtime.normalize(_request()))
    await supervisor.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime._capacity.locked() is False
    assert supervisor.scratch is not None and not supervisor.scratch.exists()
    records = [
        record for record in _phase_records(caplog)
        if record["component"] == "geometry_codec" and record["phase"] == "normalize"
    ]
    assert [(record["transition"], record["status"]) for record in records] == [
        ("start", "running"),
        ("end", "cancelled"),
    ]


def test_render_control_binds_each_canonical_input_to_one_descriptor() -> None:
    geometry = normalize_geometry(_request())
    request = GeometryRenderRequest(geometry=geometry)
    control = render_control(request)

    assert control["input_count"] == 1
    assert control["output_count"] == 1
    assert control["request"]["geometry"]["payload_index"] == 0
    assert "payload" not in control["request"]["geometry"]


def test_pure_child_render_dispatch_derives_mesh_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.infrastructure.geometry_runtime import codec
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec_child import (
        execute_render_request,
    )

    geometry = normalize_geometry(_request())
    expected = codec.mesh_for_render
    calls = []

    def capture(shape):
        calls.append(shape)
        return expected(shape)

    monkeypatch.setattr(codec, "mesh_for_render", capture)
    preview = execute_render_request(GeometryRenderRequest(geometry=geometry, width_px=96))

    assert preview.payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_real_child_returns_area_warning_png_without_rejecting_valid_face(tmp_path):
    fixture = Path(__file__).resolve().parents[2] / "fixtures/geometry/thin-trimmed-face-area-warning.brep"
    payload = fixture.read_bytes()
    geometry = normalize_geometry(GeometryNormalizeRequest(format="occt_brep_v1", payload=payload))
    runtime = _executor(tmp_path)
    try:
        preview = await runtime.render(GeometryRenderRequest(geometry=geometry))
        warning, = preview.display_quality_warnings
        assert warning.geometry_sha256 == geometry.sha256
        assert warning.retry_relative_difference > 0.05
        assert warning.retry_count == 1
        assert preview.payload.startswith(b"\x89PNG\r\n\x1a\n")
        assert fixture.read_bytes() == geometry.payload == payload
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_child_normalize_failure_identifies_original_batch_item(tmp_path):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    shell = cq.Shell.makeShell(cq.Solid.makeBox(1, 1, 1).Faces()[:-1])
    invalid = cq.Shape.cast(BRepBuilderAPI_MakeSolid(shell.wrapped).Solid())
    assert not invalid.isValid()
    stream = io.BytesIO()
    invalid.exportBrep(stream)
    payload = stream.getvalue()
    runtime = _executor(tmp_path)
    try:
        with pytest.raises(GeometryRuntimeValidationError) as caught:
            await runtime.normalize_many((_request(), GeometryNormalizeRequest(format="occt_brep_v1", payload=payload)))
        message = str(caught.value)
        assert "OCCT BREP geometry is invalid" in message
        assert "batch_index=1" in message
        assert "item=2/2" in message
        assert hashlib.sha256(payload).hexdigest() in message
        assert str(tmp_path) not in message
    finally:
        await runtime.close()


class _ScriptedSupervisor:
    def __init__(
        self,
        terminal: dict[str, Any],
        *,
        outputs: tuple[bytes, ...] = (),
        returncode: int = 0,
    ) -> None:
        self.terminal = terminal
        self.outputs = outputs
        self.returncode = returncode

    async def run(self, spec, *, budget, deadline):
        output_fds = tuple(
            int(value)
            for value in spec.env["CAD2BIM_GEOMETRY_CODEC_OUTPUT_FDS"].split(",")
            if value
        )
        assert len(output_fds) == len(self.outputs)
        for descriptor, payload in zip(output_fds, self.outputs, strict=True):
            os.write(descriptor, payload)
        terminal_fd = int(spec.env["CAD2BIM_GEOMETRY_CODEC_TERMINAL_FD"])
        with os.fdopen(os.dup(terminal_fd), "wb") as stream:
            write_json_frame(stream, self.terminal, limit=32 * 1024)
        return WorkerTerminal(
            returncode=self.returncode,
            status_payload=None,
            stdout=b"",
            stderr=b"",
            execution_started=True,
            observations=WorkerProcessObservations(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [GEOMETRY_CODEC_EXECUTION, GEOMETRY_PREVIEW_EXECUTION,
                                   PRODUCT_PREVIEW_EXECUTION])
async def test_codec_wall_uses_remaining_shared_execution_deadline(tmp_path, policy):
    class CapturingSupervisor(_ScriptedSupervisor):
        def __init__(self):
            super().__init__({"status": "succeeded"})
            self.calls = []

        async def run(self, spec, *, budget, deadline):
            self.calls.append((budget, deadline, time.monotonic()))
            return await super().run(spec, budget=budget, deadline=deadline)

    supervisor = CapturingSupervisor()
    runtime = _executor(tmp_path, supervisor=supervisor, sandbox=_NoopSandbox())
    try:
        async with runtime.execution_scope(policy) as lease:
            await runtime._run({"operation": "normalize"}, (), ())
            admitted_deadline = lease.execution_deadline
            await asyncio.sleep(0.01)
            await runtime._run({"operation": "normalize_batch"}, (), ())
            assert lease.execution_deadline == admitted_deadline
        first, second = supervisor.calls
        assert first[1] == second[1] == admitted_deadline
        assert 0 < second[0].wall_seconds < first[0].wall_seconds <= policy.execution_seconds
        assert first[0].wall_seconds > TEST_BUDGET.wall_seconds
        for budget, deadline, launched_at in supervisor.calls:
            assert budget.wall_seconds == pytest.approx(deadline - launched_at, abs=0.05)
            assert replace(budget, wall_seconds=TEST_BUDGET.wall_seconds) == TEST_BUDGET
        assert not runtime._capacity.locked()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_codec_exhausted_execution_deadline_does_not_launch_and_cleans_scratch(tmp_path, monkeypatch):
    scratch = tmp_path / "exhausted-scratch"
    def fixed_scratch(*_args, **_kwargs):
        scratch.mkdir()
        return str(scratch)

    monkeypatch.setattr(codec_execution.tempfile, "mkdtemp", fixed_scratch)
    supervisor = _BlockingSupervisor()
    runtime = _executor(tmp_path, supervisor=supervisor, sandbox=_NoopSandbox())
    try:
        async with runtime.execution_scope(RuntimeExecutionPolicy(1.0, 0.01)):
            await runtime._capacity.admit_current_execution()
            await asyncio.sleep(0.02)
            with pytest.raises(GeometryRuntimeUnavailableError, match="remaining execution budget") as caught:
                await runtime._run({"operation": "normalize_batch"}, (), ())
            assert caught.value.diagnostic["reason"] == "execution_budget_exhausted"
            assert caught.value.diagnostic["current_stage_started"] is False
        assert not supervisor.entered.is_set()
        assert not scratch.exists()
        assert not runtime._capacity.locked()
    finally:
        await runtime.close()


def _normalize_terminal(geometry, *, raw_summary=None) -> dict[str, Any]:
    metadata = canonical_geometry_metadata(geometry, payload_index=0)
    metadata["raw_summary"] = raw_summary
    return {
        "status": "succeeded",
        "operation": "normalize",
        "execution_started": True,
        "result": {"items": [metadata]},
    }


@pytest.mark.asyncio
async def test_partial_descriptor_open_failure_releases_every_owned_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "codec-scratch"
    opened: list[int] = []
    original_read = codec_execution._open_read_only
    original_write = codec_execution._open_write_once
    writes = 0

    def fixed_scratch(*_args, **_kwargs) -> str:
        scratch.mkdir()
        return str(scratch)

    def tracked_read(path: Path) -> int:
        descriptor = original_read(path)
        opened.append(descriptor)
        return descriptor

    def fail_second_write(path: Path) -> int:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError(errno.EMFILE, "controlled descriptor exhaustion")
        descriptor = original_write(path)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(codec_execution.tempfile, "mkdtemp", fixed_scratch)
    monkeypatch.setattr(codec_execution, "_open_read_only", tracked_read)
    monkeypatch.setattr(codec_execution, "_open_write_once", fail_second_write)
    runtime = _executor(tmp_path, supervisor=_BlockingSupervisor(), sandbox=_NoopSandbox())

    with pytest.raises(GeometryRuntimeUnavailableError):
        await runtime.normalize(_request())

    assert opened
    for descriptor in opened:
        with pytest.raises(OSError) as failure:
            os.fstat(descriptor)
        assert failure.value.errno == errno.EBADF
    assert runtime._capacity.locked() is False
    assert not scratch.exists()


@pytest.mark.parametrize("kind", ["symlink", "directory", "truncated", "trailing", "oversized"])
def test_terminal_reader_rejects_unsafe_or_unbounded_files(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "terminal.frame"
    if kind == "symlink":
        target = tmp_path / "target.frame"
        with target.open("wb") as stream:
            write_json_frame(stream, {"status": "succeeded"}, limit=32 * 1024)
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    elif kind == "truncated":
        path.write_bytes(struct.pack("!I", 5) + b"{}")
    elif kind == "trailing":
        with path.open("wb") as stream:
            write_json_frame(stream, {"status": "succeeded"}, limit=32 * 1024)
            stream.write(b"trailing")
    else:
        path.write_bytes(struct.pack("!I", 32 * 1024 + 1))

    with pytest.raises(GeometryRuntimeValidationError):
        codec_execution._read_frame(path, 32 * 1024)


@pytest.mark.asyncio
async def test_malformed_raw_summary_is_nonretryable_validation_failure(tmp_path: Path) -> None:
    request = _request()
    geometry = normalize_geometry(request)
    runtime = _executor(
        tmp_path,
        supervisor=_ScriptedSupervisor(
            _normalize_terminal(geometry, raw_summary={"format": "occt_brep_v1"}),
            outputs=(geometry.payload,),
        ),
        sandbox=_NoopSandbox(),
    )

    with pytest.raises(GeometryRuntimeValidationError):
        await runtime.normalize_many((request,), include_raw_brep_summary=True)


@pytest.mark.asyncio
async def test_malformed_reopen_summary_is_nonretryable_validation_failure(tmp_path: Path) -> None:
    geometry = normalize_geometry(_request())
    terminal = {
        "status": "succeeded",
        "operation": "reopen",
        "execution_started": True,
        "result": {"summary": {"format": "occt_brep_v1"}},
    }
    runtime = _executor(
        tmp_path,
        supervisor=_ScriptedSupervisor(terminal),
        sandbox=_NoopSandbox(),
    )

    with pytest.raises(GeometryRuntimeValidationError):
        await runtime.reopen(geometry)


@pytest.mark.asyncio
async def test_malformed_preview_metadata_is_nonretryable_validation_failure(
    tmp_path: Path,
) -> None:
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec_child import (
        execute_render_request,
    )

    geometry = normalize_geometry(_request())
    request = GeometryRenderRequest(geometry=geometry, width_px=96, height_px=96)
    preview = execute_render_request(request)
    metadata = preview.model_dump(mode="json", exclude={"payload"})
    metadata.update(
        {
            "content_type": "image/jpeg",
            "payload_index": 0,
            "payload_size": len(preview.payload),
        }
    )
    terminal = {
        "status": "succeeded",
        "operation": "render",
        "execution_started": True,
        "result": {"preview": metadata},
    }
    runtime = _executor(
        tmp_path,
        supervisor=_ScriptedSupervisor(terminal, outputs=(preview.payload,)),
        sandbox=_NoopSandbox(),
    )

    with pytest.raises(GeometryRuntimeValidationError):
        await runtime.render(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forgery",
    ["status", "operation", "terminal_extra", "metadata_extra", "count"],
)
async def test_forged_terminal_identity_and_result_count_fail_closed(
    tmp_path: Path,
    forgery: str,
) -> None:
    request = _request()
    geometry = normalize_geometry(request)
    terminal = _normalize_terminal(geometry)
    if forgery == "status":
        terminal["status"] = "complete"
    elif forgery == "operation":
        terminal["operation"] = "render"
    elif forgery == "terminal_extra":
        terminal["untrusted"] = True
    elif forgery == "metadata_extra":
        terminal["result"]["items"][0]["untrusted"] = True
    else:
        terminal["result"] = {"items": []}
    runtime = _executor(
        tmp_path,
        supervisor=_ScriptedSupervisor(terminal, outputs=(geometry.payload,)),
        sandbox=_NoopSandbox(),
    )

    with pytest.raises(GeometryRuntimeValidationError):
        await runtime.normalize(request)


@pytest.mark.parametrize(
    "control,input_count,output_count",
    [
        ({"operation": "render", "input_count": True, "output_count": 1}, 1, 1),
        ({"operation": "render", "input_count": 1, "output_count": False}, 1, 0),
        ({"operation": "render", "input_count": -1, "output_count": 1}, -1, 1),
    ],
)
def test_descriptor_counts_reject_bools_and_nonpositive_values(
    control: dict[str, Any],
    input_count: int,
    output_count: int,
) -> None:
    with pytest.raises(GeometryCodecProtocolError):
        require_descriptor_counts(
            control,
            input_count=input_count,
            output_count=output_count,
        )


def _process_is_gone(identity: tuple[int, float]) -> bool:
    pid, created_at = identity
    try:
        return psutil.Process(pid).create_time() != created_at
    except psutil.NoSuchProcess:
        return True


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="the approved codec child uses Seatbelt")
async def test_real_child_wall_timeout_terminates_process_and_cleans_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    created: list[tuple[int, float]] = []
    scratch = tmp_path / "real-timeout-scratch"
    original_create = asyncio.create_subprocess_exec

    async def capture_process(*args, **kwargs):
        process = await original_create(*args, **kwargs)
        created.append((process.pid, psutil.Process(process.pid).create_time()))
        return process

    def fixed_scratch(*_args, **_kwargs) -> str:
        scratch.mkdir()
        return str(scratch)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_process)
    monkeypatch.setattr(codec_execution.tempfile, "mkdtemp", fixed_scratch)
    runtime = OcpGeometryCodecExecutor(
        budget=WorkerProcessBudget(
            wall_seconds=30.0,
            cpu_seconds=30.0,
            rss_bytes=1536 * 1024**2,
            scratch_bytes=128 * 1024**2,
            threads=8,
            open_file_descriptors=64,
            stdout_bytes=16 * 1024,
            stderr_bytes=16 * 1024,
            cleanup_seconds=1.0,
        ),
        capacity_root=tmp_path / "capacity",
        supervisor=WorkerProcessSupervisor(),
    )
    source = (
        "import sys,time\n"
        f"sys.path.insert(0, {str(runtime._source_root)!r})\n"
        "from CAD2BIM_backend.infrastructure.worker_runtime.timing import WorkerTiming,timed_phase\n"
        "timing=WorkerTiming('test',time.monotonic_ns())\n"
        "with timed_phase('normalize_item',flush=True,item_index=0):\n"
        "    with timed_phase('brep_open',flush=True,item_index=0):\n"
        "        pass\n"
        "    with timed_phase('brep_validate',flush=True,item_index=0):\n"
        "        time.sleep(30)\n"
    )
    monkeypatch.setattr(runtime, "_isolated_module_args", lambda: ("-I", "-c", source))

    async with runtime.execution_scope(RuntimeExecutionPolicy(1.0, 1.0)):
        with pytest.raises(GeometryRuntimeResourceLimitError) as caught:
            await runtime.normalize(_request())

    assert caught.value.diagnostic.resource_kind == "wall"
    assert caught.value.diagnostic.operation == "normalize"
    assert 0 < caught.value.diagnostic.effective_wall_seconds <= 1.0
    assert created and all(_process_is_gone(identity) for identity in created)
    assert runtime._capacity.locked() is False
    assert not scratch.exists()
    process_end = next(record for record in _phase_records(caplog)
                       if record["component"] == "worker_runtime" and record["transition"] == "end")
    events = process_end["details"]["child_timing"]
    assert events[-1]["phase"] == "brep_validate"
    assert events[-1]["details"]["transition"] == "start"
    assert any(event["phase"] == "brep_open" and event["details"].get("transition") == "end"
               for event in events)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="the approved codec child uses Seatbelt")
@pytest.mark.parametrize("batch", [False, True])
async def test_real_child_cancellation_terminates_process_and_cleans_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    batch: bool,
) -> None:
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    created: list[tuple[int, float]] = []
    launched = asyncio.Event()
    scratch = tmp_path / "real-cancel-scratch"
    original_create = asyncio.create_subprocess_exec

    async def capture_process(*args, **kwargs):
        process = await original_create(*args, **kwargs)
        created.append((process.pid, psutil.Process(process.pid).create_time()))
        launched.set()
        return process

    def fixed_scratch(*_args, **_kwargs) -> str:
        scratch.mkdir()
        return str(scratch)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_process)
    monkeypatch.setattr(codec_execution.tempfile, "mkdtemp", fixed_scratch)
    runtime = _executor(tmp_path, supervisor=WorkerProcessSupervisor())
    request = _request()
    task = asyncio.create_task(runtime.normalize_batch((request,) * 9) if batch else runtime.normalize(request))
    await asyncio.wait_for(launched.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as failure:
        await task

    assert getattr(failure.value, "worker_failure").code == "WORKER_TIMEOUT"
    assert created and all(_process_is_gone(identity) for identity in created)
    assert runtime._capacity.locked() is False
    assert not scratch.exists()
    terminal_records = [
        record
        for record in _phase_records(caplog)
        if record["transition"] == "end"
        and record["component"] in {"geometry_codec", "worker_runtime"}
        and record["phase"] in {"normalize", "normalize_batch", "process"}
    ]
    assert {(record["component"], record["status"]) for record in terminal_records} == {
        ("geometry_codec", "cancelled"),
        ("worker_runtime", "cancelled"),
    }
    assert any(record["phase"] == "cleanup" and record["status"] == "succeeded"
               for record in _phase_records(caplog) if record["transition"] == "end")
    codec_start = next(
        record
        for record in _phase_records(caplog)
        if record["component"] == "geometry_codec" and record["transition"] == "start"
    )
    worker_end = next(
        record for record in terminal_records if record["component"] == "worker_runtime"
    )
    stage = next(record for record in _phase_records(caplog)
                 if record["component"] == "worker_capacity" and record["phase"] == "stage"
                 and record["transition"] == "end")
    assert worker_end["parent_span_id"] == stage["span_id"]
    assert stage["parent_span_id"] == codec_start["span_id"]


def test_worker_timing_flushes_completed_item_before_next_failure(tmp_path, monkeypatch):
    from CAD2BIM_backend.infrastructure.worker_runtime.timing import WorkerTiming
    from CAD2BIM_backend.infrastructure.worker_runtime.protocol import read_json_frame

    target = tmp_path / 'timing.frames'
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    monkeypatch.setenv('CAD2BIM_WORKER_TIMING_FD', str(descriptor))
    timing = WorkerTiming('test', 0)
    startup_bytes = target.stat().st_size
    timing.mark('small_phase', 1, 2)
    assert target.stat().st_size == startup_bytes
    timing.mark('batch_item_released', 2, 3, item_index=0)
    events = []
    with target.open('rb') as stream:
        while stream.tell() < target.stat().st_size:
            events.append(read_json_frame(stream, limit=1020))
    assert [e['phase'] for e in events][-2:] == ['small_phase', 'batch_item_released']
    timing.mark('failure_detail', 3, 4)
    timing.close()
    with target.open('rb') as stream:
        events = []
        while stream.tell() < target.stat().st_size:
            events.append(read_json_frame(stream, limit=1020))
    assert any(e['phase'] == 'failure_detail' for e in events)


def test_worker_timing_flushes_phase_entry_and_failure_without_close(tmp_path, monkeypatch):
    from CAD2BIM_backend.infrastructure.worker_runtime.timing import WorkerTiming, timed_phase
    from CAD2BIM_backend.infrastructure.worker_runtime.protocol import read_json_frame

    target = tmp_path / "timing.frames"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    monkeypatch.setenv("CAD2BIM_WORKER_TIMING_FD", str(descriptor))
    timing = WorkerTiming("test", 0)

    def recorded_events():
        with target.open("rb") as stream:
            events = []
            while stream.tell() < target.stat().st_size:
                events.append(read_json_frame(stream, limit=1020))
        return events

    try:
        with pytest.raises(ValueError, match="native stage failed"):
            with timed_phase("normalize_item", flush=True, item_index=12):
                entry = recorded_events()[-1]
                assert entry["details"] == {"item_index": 12, "transition": "start"}
                assert entry["start_ns"] == entry["end_ns"]
                raise ValueError("native stage failed")
        events = recorded_events()
        assert events[-1]["details"] == {"item_index": 12, "transition": "end", "status": "failed"}
        assert timing._aggregates["normalize_item"][0] == 1
    finally:
        timing.close()


@pytest.mark.parametrize('budget_kind', ['count', 'bytes'])
def test_worker_timing_release_flushes_pending_details_after_budget(tmp_path, monkeypatch, budget_kind):
    from CAD2BIM_backend.infrastructure.worker_runtime import timing as module

    target = tmp_path / 'timing.frames'
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    monkeypatch.setenv('CAD2BIM_WORKER_TIMING_FD', str(descriptor))
    timing = module.WorkerTiming('test', 0)
    if budget_kind == 'count':
        for index in range(1023):
            timing.mark('small_phase', index, index + 1)
        assert timing._count == 1024
    else:
        monkeypatch.setattr(module, 'MAX_TIMING_BYTES', 65 * 1024 + 300)
        while timing._stream.tell() < 300:
            timing.mark('small_phase', 1, 2)
        assert timing._count < 1024
    logical_bytes = timing._stream.tell()
    assert target.stat().st_size < logical_bytes
    timing.mark('batch_item_released', 1024, 1025, item_index=0)
    assert target.stat().st_size == logical_bytes
    timing.close()


def test_worker_timing_optional_close_failure_preserves_business_exception(tmp_path, monkeypatch):
    from CAD2BIM_backend.infrastructure.worker_runtime.timing import WorkerTiming

    monkeypatch.delenv('CAD2BIM_WORKER_TIMING_FD', raising=False)
    timing = WorkerTiming('test', 0)
    raw = (tmp_path / 'failed-observer').open('wb', buffering=0)

    class FailingStream:
        def __init__(self, raw):
            self.raw = raw
        def write(self, value):
            raise OSError('optional write failure')
        def close(self):
            raise OSError('optional close failure')

    timing._stream = FailingStream(raw)
    with pytest.raises(ValueError, match='original business failure'):
        try:
            raise ValueError('original business failure')
        finally:
            timing.close()
    assert raw.closed


def test_worker_timing_aggregates_all_events_after_detail_budget(tmp_path, monkeypatch):
    from CAD2BIM_backend.infrastructure.worker_runtime.timing import WorkerTiming, MAX_TIMING_BYTES
    from CAD2BIM_backend.infrastructure.worker_runtime.protocol import read_json_frame
    target = tmp_path / "timing.frames"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    monkeypatch.setenv("CAD2BIM_WORKER_TIMING_FD", str(descriptor))
    timing = WorkerTiming("test", 0)
    for index in range(1500):
        timing.mark("item", index * 10, index * 10 + 5, item=index)
    timing.close()
    assert target.stat().st_size <= MAX_TIMING_BYTES
    events = []
    with target.open("rb") as stream:
        while stream.tell() < target.stat().st_size:
            events.append(read_json_frame(stream, limit=1020))
    aggregate = next(event for event in events if event["phase"] == "timing_aggregate" and event["details"]["phase"] == "item")
    assert aggregate["details"]["count"] == 1500
    assert aggregate["details"]["total_duration_ns"] == 7500
    coverage = next(event for event in events if event["phase"] == "timing_coverage")
    assert coverage["details"]["omitted_details"] > 0
    assert coverage["details"]["unclassified_events"] == 0
