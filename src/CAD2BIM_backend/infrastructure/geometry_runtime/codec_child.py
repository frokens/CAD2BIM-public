"""Installed private child for one server-owned geometry codec operation."""

from __future__ import annotations

import time
_MODULE_ENTERED_NS = time.monotonic_ns()
import hashlib
import sys
import os
from contextlib import ExitStack
from functools import partial
from typing import Any, BinaryIO

from pydantic import ValidationError

from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution_protocol import (
    GeometryCodecProtocolError,
    MAX_CODEC_CONTROL_BYTES,
    MAX_CODEC_PAYLOAD_BYTES,
    MAX_CODEC_PREVIEW_BYTES,
    MAX_CODEC_TERMINAL_BYTES,
    canonical_geometry_metadata,
    require_descriptor_counts,
    require_exact_keys,
    unpack_fragment_inputs,
    MAX_CODEC_BATCH_ITEMS,
    MAX_CODEC_BATCH_TOTAL_ITEMS,
    MAX_CODEC_BATCH_INPUT_BYTES,
    MAX_CODEC_BATCH_OUTPUT_BYTES,
)
from CAD2BIM_backend.infrastructure.worker_runtime import (
    open_binary_fd,
    read_json_frame,
    write_json_frame,
)
from CAD2BIM_backend.infrastructure.worker_runtime.protocol import encode_json_frame, write_all
from CAD2BIM_backend.infrastructure.worker_runtime.timing import WorkerTiming, timed_phase


_CONTROL_ENV = "CAD2BIM_GEOMETRY_CODEC_CONTROL_FD"
_TERMINAL_ENV = "CAD2BIM_GEOMETRY_CODEC_TERMINAL_FD"
_INPUTS_ENV = "CAD2BIM_GEOMETRY_CODEC_INPUT_FDS"
_OUTPUTS_ENV = "CAD2BIM_GEOMETRY_CODEC_OUTPUT_FDS"


class _ChildError(RuntimeError):
    def __init__(self, code: str, message: str, *, phase: str = "execution") -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase


class _BatchFatalError(RuntimeError):
    def __init__(self, index: int, count: int, digest: str, cause: Exception) -> None:
        self.validation = isinstance(cause, (GeometryCodecProtocolError, ValueError, TypeError))
        detail = str(cause) if self.validation else "native or unknown execution failure; worker discarded"
        super().__init__(f"batch_item_index={index} item={index + 1}/{count} input_sha256={digest}: {detail}")


def main() -> int:
    timing = WorkerTiming("geometry_codec", _MODULE_ENTERED_NS)
    try:
        return _main()
    finally:
        timing.close()


def _main() -> int:
    terminal_descriptor = _descriptor(_TERMINAL_ENV)
    with open_binary_fd(terminal_descriptor, "wb") as terminal_stream:
        started = False
        operation: str | None = None
        try:
            input_descriptors = _descriptor_list(_INPUTS_ENV)
            output_descriptors = _descriptor_list(_OUTPUTS_ENV)
            all_descriptors = (
                _descriptor(_CONTROL_ENV),
                terminal_descriptor,
                *input_descriptors,
                *output_descriptors,
            )
            if len(all_descriptors) != len(set(all_descriptors)):
                raise _ChildError(
                    "GEOMETRY_CODEC_PROTOCOL_INVALID",
                    "geometry codec descriptors must be unique",
                    phase="preflight",
                )
            with ExitStack() as stack:
                control_stream = stack.enter_context(open_binary_fd(all_descriptors[0], "rb"))
                inputs = tuple(
                    stack.enter_context(open_binary_fd(descriptor, "rb"))
                    for descriptor in input_descriptors
                )
                outputs = tuple(
                    stack.enter_context(open_binary_fd(descriptor, "wb"))
                    for descriptor in output_descriptors
                )
                control = read_json_frame(control_stream, limit=MAX_CODEC_CONTROL_BYTES)
                if control_stream.read(1):
                    raise GeometryCodecProtocolError("geometry codec control has trailing bytes")
                require_descriptor_counts(
                    control,
                    input_count=len(inputs),
                    output_count=len(outputs),
                )
                operation = control.get("operation")
                started = True
                result = _execute(control, inputs, outputs)
            write_json_frame(
                terminal_stream,
                {
                    "status": "succeeded",
                    "operation": control["operation"],
                    "execution_started": True,
                    "result": result,
                },
                limit=MAX_CODEC_TERMINAL_BYTES,
            )
            return 0
        except BaseException as exc:
            _write_failure(
                terminal_stream,
                exc,
                operation=operation,
                execution_started=started,
            )
            return 1


def _execute(
    control: dict[str, Any],
    inputs: tuple[BinaryIO, ...],
    outputs: tuple[BinaryIO, ...],
) -> dict[str, Any]:
    operation = control.get("operation")
    if operation == "normalize":
        return _normalize(control, inputs, outputs)
    if operation == "normalize_batch":
        return _normalize_batch(control, inputs[0], outputs[0])
    if operation == "reopen":
        return _reopen(control, inputs)
    if operation == "render":
        with timed_phase("render", flush=True):
            return _render(control, inputs, outputs)
    if operation == "export_stl":
        with timed_phase("export_stl", flush=True):
            return _export_stl(control, inputs, outputs)
    if operation == "section":
        return _section(control, inputs, outputs)
    if operation == "build_display_context":
        return _build_display_context(control, outputs)
    raise GeometryCodecProtocolError("geometry codec operation is invalid")


def _normalize_batch(control: dict[str, Any], source: BinaryIO, target: BinaryIO) -> dict[str, Any]:
    """A bounded sequence of existing normalize frames in one codec process.

    Only pure mesh parsing failures can continue. Native/unknown/protocol errors
    escape to the process terminal; no partially returned batch is trusted.
    """
    from CAD2BIM_backend.domain.geometry_runtime import GeometryNormalizeRequest, GeometrySourceFormat, GeometryRuntimeValidationError
    with timed_phase("geometry_dependencies_import", flush=True, initial_load=True):
        from CAD2BIM_backend.infrastructure.geometry_runtime.codec import parse_mesh

    require_exact_keys(control, {"operation", "input_count", "output_count", "frame_count", "item_count", "input_bytes"}, label="normalize batch")
    count, frames, byte_count = control["item_count"], control["frame_count"], control["input_bytes"]
    if (type(count) is not int or not 1 <= count <= MAX_CODEC_BATCH_TOTAL_ITEMS
            or type(frames) is not int or frames != (count + MAX_CODEC_BATCH_ITEMS - 1) // MAX_CODEC_BATCH_ITEMS
            or type(byte_count) is not int or not 1 <= byte_count <= MAX_CODEC_BATCH_INPUT_BYTES):
        raise GeometryCodecProtocolError("normalize batch limits are invalid")
    item_index = 0
    failures = 0
    for frame_index in range(frames):
        frame = read_json_frame(source, limit=MAX_CODEC_CONTROL_BYTES)
        require_exact_keys(frame, {"operation", "include_raw_brep_summary", "inputs", "output_count"}, label="normalize batch frame")
        documents = frame["inputs"]
        expected = min(MAX_CODEC_BATCH_ITEMS, count - item_index)
        if (frame["operation"] != "normalize" or not isinstance(documents, list)
                or len(documents) != expected or type(frame["include_raw_brep_summary"]) is not bool):
            raise GeometryCodecProtocolError("normalize batch frame order/count is invalid")
        require_descriptor_counts(frame, input_count=expected, output_count=expected)
        metadata, payloads = [], []
        # Memoize only successful computation within this already bounded
        # frame. Every occurrence still verifies its actual wire bytes and
        # retains its own ordered result identity and output-budget charge.
        normalized_in_frame = {}
        for local_index, document in enumerate(documents):
            require_exact_keys(document, {"format", "source_format", "unit", "transform", "include_summary", "payload_index", "payload_size", "payload_sha256"}, label="normalize batch item")
            if type(document["payload_index"]) is not int or document["payload_index"] != local_index:
                raise GeometryCodecProtocolError("normalize batch item index is invalid")
            size = document["payload_size"]
            if type(size) is not int or not 1 <= size <= MAX_CODEC_PAYLOAD_BYTES or source.tell() + size > byte_count:
                raise GeometryCodecProtocolError("normalize batch item size is invalid")
            with timed_phase("codec_input_hash_dto", item_index=item_index):
                payload = source.read(size)
                if len(payload) != size or hashlib.sha256(payload).hexdigest() != document["payload_sha256"]:
                    raise GeometryCodecProtocolError("normalize batch input hash/length mismatch")
                request = GeometryNormalizeRequest.model_validate({
                    "format": document["format"], "source_format": document["source_format"],
                    "unit": document["unit"], "transform": document["transform"], "include_summary": document["include_summary"], "payload": payload,
                })
            identity = {"item_index": item_index, "input_sha256": document["payload_sha256"]}
            # This is the entire recoverable set. All exceptions after entering
            # native geometry processing abort and destroy the batch worker.
            failure = None
            if request.effective_source_format is GeometrySourceFormat.INDEXED_TRIANGLE_MESH:
                try:
                    parse_mesh(payload)
                except GeometryRuntimeValidationError as exc:
                    failure = str(exc)[:400]
            if failure is not None:
                metadata.append({**identity, "status": "failed", "code": "MESH_INPUT_INVALID", "message": failure})
                failures += 1
            else:
                try:
                    with timed_phase("batch_item", item_index=item_index):
                        key = (request, frame["include_raw_brep_summary"])
                        if key in normalized_in_frame:
                            with timed_phase("reuse_normalized_item", item_index=item_index):
                                item, output_payload = normalized_in_frame[key]
                        else:
                            item, output_payload = _normalize_request(request, payload_index=0,
                                item_index=item_index, item_count=count, input_sha256=document["payload_sha256"],
                                include_raw=frame["include_raw_brep_summary"])
                            normalized_in_frame[key] = item, output_payload
                        del key
                except Exception as exc:
                    # Preserve global identity and abort; this is never recovery.
                    raise _BatchFatalError(item_index, count, identity["input_sha256"], exc) from exc
                if target.tell() + sum(map(len, payloads)) + len(output_payload) > MAX_CODEC_BATCH_OUTPUT_BYTES:
                    raise GeometryCodecProtocolError(f"normalize batch output byte budget exceeded at item={item_index}")
                metadata.append({**identity, "status": "succeeded", "geometry": item})
                payloads.append(output_payload)
                del item, output_payload
            item_index += 1
            del request, payload
        header = encode_json_frame({"frame_index": frame_index, "items": metadata}, limit=MAX_CODEC_CONTROL_BYTES)
        if target.tell() + len(header) + sum(map(len, payloads)) > MAX_CODEC_BATCH_OUTPUT_BYTES:
            raise GeometryCodecProtocolError("normalize batch output byte budget exceeded")
        write_all(target, header)
        for payload in payloads:
            write_all(target, payload)
        target.flush()
        if payloads:
            del payload
        del payloads, metadata, header, normalized_in_frame
        from CAD2BIM_backend.infrastructure.worker_runtime.timing import sample_worker_memory
        sample_worker_memory("batch_frame_released", frame_index=frame_index, item_count=item_index)
    if source.tell() != byte_count or source.read(1):
        raise GeometryCodecProtocolError("normalize batch input has trailing bytes")
    return {"item_count": count, "frame_count": frames, "recoverable_failures": failures}


def _normalize(
    control: dict[str, Any],
    inputs: tuple[BinaryIO, ...],
    outputs: tuple[BinaryIO, ...],
) -> dict[str, Any]:
    from CAD2BIM_backend.domain.geometry_runtime import (
        GeometryNormalizeRequest,
    )

    require_exact_keys(
        control,
        {"operation", "include_raw_brep_summary", "inputs", "output_count"},
        label="normalize control",
    )
    documents = control["inputs"]
    include_raw = control["include_raw_brep_summary"]
    if not isinstance(documents, list) or not isinstance(include_raw, bool):
        raise GeometryCodecProtocolError("normalize control values are invalid")
    results: list[dict[str, Any]] = []
    for index, (document, input_stream, output_stream) in enumerate(
        zip(documents, inputs, outputs, strict=True)
    ):
        if not isinstance(document, dict):
            raise GeometryCodecProtocolError("normalize input metadata must be an object")
        require_exact_keys(
            document,
            {
                "format",
                "source_format",
                "unit",
                "transform",
                "include_summary",
                "payload_index",
                "payload_size",
                "payload_sha256",
            },
            label="normalize input metadata",
        )
        if type(document["payload_index"]) is not int or document["payload_index"] != index:
            raise GeometryCodecProtocolError("normalize payload order is invalid")
        payload = _read_payload(input_stream, document)
        request = GeometryNormalizeRequest.model_validate(
            {
                "format": document["format"],
                "source_format": document["source_format"],
                "unit": document["unit"],
                "transform": document["transform"],
                "include_summary": document["include_summary"],
                "payload": payload,
            }
        )
        item, output_payload = _normalize_request(request, payload_index=index, item_index=index,
            item_count=len(documents), input_sha256=document["payload_sha256"], include_raw=include_raw)
        with timed_phase("canonical_payload_output", item_index=index):
            _write_payload(output_stream, output_payload, limit=MAX_CODEC_PAYLOAD_BYTES)
        results.append(item)
        del output_payload, payload, request
    return {"items": results}


def _normalize_request(request, *, payload_index, item_index, item_count, input_sha256, include_raw):
    """Shared processing kernel after the actual wire input has been verified once."""
    from CAD2BIM_backend.domain.geometry_runtime import GeometrySourceFormat, GeometryRuntimeValidationError
    if "CAD2BIM_backend.infrastructure.geometry_runtime.codec" not in sys.modules:
        with timed_phase("geometry_dependencies_import", flush=True, initial_load=True):
            from CAD2BIM_backend.infrastructure.geometry_runtime import codec
    else:
        from CAD2BIM_backend.infrastructure.geometry_runtime import codec
    from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import reopen_user_shape
    try:
        with timed_phase("normalize_item", flush=True, item_index=item_index):
            geometry = codec.normalize_geometry(request,
                observe_phase=partial(timed_phase, flush=True, item_index=item_index))
    except GeometryRuntimeValidationError as exc:
        raise GeometryRuntimeValidationError(
            f"normalize batch_index={item_index} item={item_index + 1}/{item_count} "
            f"payload_sha256={input_sha256}: {exc}"
        ) from exc
    raw_summary = None
    if include_raw:
        if request.effective_source_format is not GeometrySourceFormat.OCCT_BREP:
            raise GeometryCodecProtocolError("raw summary requires an OCCT BREP source")
        with timed_phase("raw_brep_summary", flush=True, item_index=item_index):
            raw_summary = (codec.summarize_shape(reopen_user_shape(request.payload))
                           if request.transform is not None or geometry.summary is None else geometry.summary)
    with timed_phase("canonical_metadata_encode", item_index=item_index):
        item = canonical_geometry_metadata(geometry, payload_index=payload_index)
        item["raw_summary"] = None if raw_summary is None else raw_summary.model_dump(mode="json")
    return item, geometry.payload


def _reopen(control: dict[str, Any], inputs: tuple[BinaryIO, ...]) -> dict[str, Any]:
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec import reopen_geometry

    require_exact_keys(
        control,
        {"operation", "geometry", "output_count"},
        label="reopen control",
    )
    geometry = _materialize_geometry(control["geometry"], inputs, expected_index=0)
    _value, summary = reopen_geometry(geometry)
    return {"summary": summary.model_dump(mode="json")}


def _render(
    control: dict[str, Any],
    inputs: tuple[BinaryIO, ...],
    outputs: tuple[BinaryIO, ...],
) -> dict[str, Any]:
    from CAD2BIM_backend.domain.geometry_runtime import GeometryRenderRequest

    require_exact_keys(
        control,
        {"operation", "request", "input_count", "output_count"},
        label="render control",
    )
    document = control["request"]
    if not isinstance(document, dict):
        raise GeometryCodecProtocolError("render request must be an object")
    request_document = dict(document)
    request_document["geometry"] = _materialize_geometry(
        request_document["geometry"], inputs, expected_index=0
    )
    if "display_context" in request_document:
        request_document["display_context"] = _materialize_geometry(
            request_document["display_context"], inputs, expected_index=1
        )
    references = request_document.get("reference_geometries", [])
    if not isinstance(references, list) or len(references) > 8:
        raise GeometryCodecProtocolError("render references must be a bounded list")
    offset = 1 + int("display_context" in request_document)
    fragment_documents = request_document.get("fragment_inputs", [])
    if len(inputs) != offset + len(references) + int(bool(fragment_documents)):
        raise GeometryCodecProtocolError("render reference descriptors disagree with their payloads")
    request_document["reference_geometries"] = [
        _materialize_geometry(item, inputs, expected_index=offset + index)
        for index, item in enumerate(references)
    ]
    if fragment_documents:
        payload = inputs[-1].read(MAX_CODEC_PAYLOAD_BYTES + 1)
        request_document["fragment_inputs"] = unpack_fragment_inputs(fragment_documents, payload)
    request = GeometryRenderRequest.model_validate(request_document)
    preview = execute_render_request(request)
    _write_payload(outputs[0], preview.payload, limit=MAX_CODEC_PREVIEW_BYTES)
    metadata = preview.model_dump(mode="json", exclude={"payload"})
    metadata["payload_index"] = 0
    metadata["payload_size"] = len(preview.payload)
    return {"preview": metadata}


def _export_stl(control, inputs, outputs):
    from CAD2BIM_backend.domain.geometry_runtime import GeometryDisplayMesh, GeometryDisplayQualityWarning
    with timed_phase("geometry_dependencies_import", flush=True):
        from CAD2BIM_backend.infrastructure.geometry_runtime import codec

    require_exact_keys(control, {"operation", "geometry", "input_count", "output_count"}, label="STL control")
    geometry = _materialize_geometry(control["geometry"], inputs, expected_index=0)
    with timed_phase("export_stl_display_mesh", flush=True):
        mesh = codec.mesh_for_render(codec.read_geometry_payload(geometry))
    with timed_phase("export_stl_encode", flush=True):
        payload = codec.write_stl(mesh)
    result = GeometryDisplayMesh(payload=payload, sha256=hashlib.sha256(payload).hexdigest(),
        geometry_sha256=geometry.sha256, display_quality_warnings=tuple(
            GeometryDisplayQualityWarning(geometry_sha256=geometry.sha256, **facts)
            for facts in mesh.display_quality_warnings))
    _write_payload(outputs[0], payload, limit=MAX_CODEC_PAYLOAD_BYTES)
    metadata = result.model_dump(mode="json", exclude={"payload"})
    metadata.update(payload_index=0, payload_size=len(payload))
    return {"mesh": metadata}


def execute_render_request(request):
    """Pure child dispatch kept callable for render-chain regression tests."""

    with timed_phase("geometry_dependencies_import", flush=True):
        from CAD2BIM_backend.infrastructure.geometry_runtime import codec
        from CAD2BIM_backend.infrastructure.geometry_runtime.render import render_mesh

    def attributed_mesh(value, label):
        from CAD2BIM_backend.domain.geometry_runtime import GeometryRuntimeValidationError
        try:
            with timed_phase("render_display_mesh", flush=True, source=label):
                return codec.mesh_for_render(value)
        except GeometryRuntimeValidationError as exc:
            raise GeometryRuntimeValidationError(f"{label}: {exc}") from exc

    source = None
    source_mesh_pairs = None
    if request.inspection_selection is None:
        mesh = attributed_mesh(codec.read_geometry_payload(request.geometry), "selected whole")
    else:
        from CAD2BIM_backend.infrastructure.geometry_runtime.section import InspectionGeometry
        source = InspectionGeometry(request.geometry, request.fragment_inputs,
                                    request.inspection_selection, request.inspection_coordinates)
        try:
            objects = source.display_objects()
            if request.inspection_selection.selection_kind == "whole":
                mesh = attributed_mesh(objects[0].shape, "selected whole")
            else:
                source_mesh_pairs = tuple((source.fragments[item.fragment_key].geometry,
                    attributed_mesh(item.shape, f"selected fragment {item.fragment_key!r} in {source.coordinates.coordinate_space}"),
                    f"Selected fragment {item.fragment_key!r} in {source.coordinates.coordinate_space}")
                    for item in objects)
                vertices, triangles = [], []
                for _,part,_ in source_mesh_pairs:
                    offset = len(vertices)
                    vertices.extend(part.vertices)
                    triangles.extend([[index+offset for index in triangle] for triangle in part.triangles])
                mesh = codec.MeshData(vertices, triangles)
        finally:
            source.close()
    context_mesh = (
        None if request.display_context is None else attributed_mesh(codec.read_geometry_payload(request.display_context),
            "display context (no independently queryable target in this response)")
    )
    reference_meshes = tuple(attributed_mesh(codec.read_geometry_payload(item),
        f"reference {request.scene.reference_geometries[index].reference_key!r} in reference-local metres (query its exact resource version separately)")
        for index, item in enumerate(request.reference_geometries))
    options = {} if source_mesh_pairs is None else {"source_mesh_pairs": source_mesh_pairs}
    with timed_phase("render_raster", flush=True):
        return render_mesh(request, mesh, display_context_mesh=context_mesh, reference_meshes=reference_meshes, **options)


def _section(control, inputs, outputs):
    from CAD2BIM_backend.domain.canonical import canonical_json
    from CAD2BIM_backend.domain.geometry_runtime import GeometrySectionRequest, GeometrySectionResult, GeometrySectionRenderFailure
    from CAD2BIM_backend.infrastructure.geometry_runtime.section import InspectionGeometry, measure_sections
    from CAD2BIM_backend.infrastructure.geometry_runtime.render import render_section_measurement

    require_exact_keys(control, {"operation", "request", "input_count", "output_count"}, label="section control")
    document = dict(control["request"])
    document["geometry"] = _materialize_geometry(document["geometry"], inputs, expected_index=0)
    fragment_documents = document.get("fragment_inputs", [])
    if len(inputs) != 1 + int(bool(fragment_documents)):
        raise GeometryCodecProtocolError("section fragment descriptors disagree with their payloads")
    document["fragment_inputs"] = unpack_fragment_inputs(fragment_documents, inputs[1].read(MAX_CODEC_PAYLOAD_BYTES+1) if fragment_documents else b"")
    request = GeometrySectionRequest.model_validate(document)
    source = InspectionGeometry(request.geometry, request.fragment_inputs, request.selection, request.coordinates)
    try:
        with timed_phase("section_measurements"):
            measurements = measure_sections(source, request.observation)
        images, errors = [], []
        for measurement in measurements:
            try:
                with timed_phase("section_render"):
                    image = render_section_measurement(measurement, request)
                _write_payload(outputs[measurement.section_index+1], image.preview.payload, limit=MAX_CODEC_PREVIEW_BYTES)
                images.append(image)
            except Exception as exc:
                errors.append(GeometrySectionRenderFailure(section_index=measurement.section_index, message=f"{str(exc)[:1800]}; measurements remain available; reuse the exact target and correct the display request."))
        result = GeometrySectionResult(geometry_sha256=request.geometry.sha256, selection=request.selection,
                                      coordinates=request.coordinates, measurements=measurements,
                                      images=tuple(images), render_errors=tuple(errors))
        facts = canonical_json(result.model_dump(mode="json", exclude={"images"})).encode("utf-8")
        _write_payload(outputs[0], facts, limit=1024*1024)
        metadata = []
        for image in images:
            item = image.model_dump(mode="json", exclude={"preview"})
            item["preview"] = image.preview.model_dump(mode="json", exclude={"payload"})
            item["payload_index"] = image.section_index+1
            item["payload_size"] = len(image.preview.payload)
            metadata.append(item)
        return {"facts_sha256": hashlib.sha256(facts).hexdigest(), "facts_size": len(facts), "images": metadata}
    finally:
        source.close()


def _build_display_context(
    control: dict[str, Any],
    outputs: tuple[BinaryIO, ...],
) -> dict[str, Any]:
    from CAD2BIM_backend.domain.geometry_runtime import GeometryBuildDisplayContextRequest
    from CAD2BIM_backend.infrastructure.geometry_runtime.straight_profile import (
        build_display_context,
    )

    require_exact_keys(
        control,
        {"operation", "request", "input_count", "output_count"},
        label="display-context control",
    )
    request = GeometryBuildDisplayContextRequest.model_validate(control["request"])
    result = build_display_context(request)
    _write_payload(outputs[0], result.geometry.payload, limit=MAX_CODEC_PAYLOAD_BYTES)
    return {
        "geometry": canonical_geometry_metadata(result.geometry, payload_index=0),
        "recipe_digest": result.recipe_digest,
        "recipe_keys": list(result.recipe_keys),
    }


def _materialize_geometry(
    metadata: Any,
    inputs: tuple[BinaryIO, ...],
    *,
    expected_index: int,
):
    from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry

    if not isinstance(metadata, dict):
        raise GeometryCodecProtocolError("canonical geometry metadata must be an object")
    require_exact_keys(
        metadata,
        {"format", "content_type", "payload_index", "payload_size", "sha256", "summary"}
        | ({"source_provenance"} if "source_provenance" in metadata else set()),
        label="canonical geometry metadata",
    )
    if (
        type(metadata["payload_index"]) is not int
        or metadata["payload_index"] != expected_index
        or expected_index >= len(inputs)
    ):
        raise GeometryCodecProtocolError("canonical geometry payload order is invalid")
    payload = _read_payload(inputs[expected_index], metadata)
    document = dict(metadata)
    document.pop("payload_index")
    document.pop("payload_size")
    document["payload"] = payload
    return CanonicalGeometry.model_validate(document)


def _read_payload(stream: BinaryIO, metadata: dict[str, Any]) -> bytes:
    size = metadata.get("payload_size")
    digest = metadata.get("payload_sha256", metadata.get("sha256"))
    if type(size) is not int or not 1 <= size <= MAX_CODEC_PAYLOAD_BYTES:
        raise GeometryCodecProtocolError("geometry codec input size is invalid")
    payload = stream.read(size + 1)
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
        raise GeometryCodecProtocolError("geometry codec input bytes do not match metadata")
    return payload


def _write_payload(stream: BinaryIO, payload: bytes, *, limit: int) -> None:
    if not 1 <= len(payload) <= limit:
        raise GeometryCodecProtocolError("geometry codec output exceeds its byte budget")
    stream.write(payload)
    stream.flush()


def _descriptor(name: str) -> int:
    try:
        value = int(os.environ.pop(name))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("geometry codec worker descriptor contract is invalid") from exc
    if value < 3:
        raise RuntimeError("geometry codec worker descriptor contract is invalid")
    return value


def _descriptor_list(name: str) -> tuple[int, ...]:
    value = os.environ.pop(name, "")
    if not value:
        return ()
    try:
        descriptors = tuple(int(item) for item in value.split(","))
    except ValueError as exc:
        raise RuntimeError("geometry codec worker descriptor contract is invalid") from exc
    if any(item < 3 for item in descriptors):
        raise RuntimeError("geometry codec worker descriptor contract is invalid")
    return descriptors


def _write_failure(
    stream: BinaryIO,
    exc: BaseException,
    *,
    operation: str | None,
    execution_started: bool,
) -> None:
    invalid = isinstance(
        exc,
        (GeometryCodecProtocolError, ValidationError, TypeError, ValueError),
    )
    if isinstance(exc, _BatchFatalError):
        invalid = exc.validation
    try:
        write_json_frame(
            stream,
            {
                "status": "failed",
                "operation": operation,
                "execution_started": execution_started,
                "phase": "execution" if execution_started else "preflight",
                "code": (
                    "GEOMETRY_CODEC_VALIDATION_FAILED"
                    if invalid
                    else "GEOMETRY_CODEC_EXECUTION_FAILED"
                ),
                "message": (
                    "geometry codec typed request or result is invalid"
                    if isinstance(exc, ValidationError)
                    else _bounded(str(exc), 2_000)
                    if invalid or isinstance(exc, _BatchFatalError)
                    else "geometry codec execution failed"
                ),
            },
            limit=MAX_CODEC_TERMINAL_BYTES,
        )
    except OSError:
        pass


def _bounded(value: str, limit: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return encoded.decode("utf-8")
    return encoded[:limit].decode("utf-8", errors="ignore")


if __name__ == "__main__":
    raise SystemExit(main())
