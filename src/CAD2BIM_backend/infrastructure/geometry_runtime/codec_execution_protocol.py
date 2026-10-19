"""Private closed protocol for server-owned geometry codec execution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    GeometryBuildDisplayContextRequest,
    GeometryNormalizeRequest,
    GeometryRenderRequest,
    GeometrySectionRequest,
    GeometryInspectionFragmentInput,
    GeometryRuntimeValidationError,
)
from CAD2BIM_backend.infrastructure.worker_runtime.protocol import (
    MAX_CONTROL_BYTES as MAX_CODEC_CONTROL_BYTES,
    MAX_STATUS_BYTES as MAX_CODEC_TERMINAL_BYTES,
    encode_json_frame,
)


CodecOperation = Literal["normalize", "normalize_batch", "reopen", "render", "export_stl", "section", "build_display_context"]
MAX_CODEC_BATCH_ITEMS = 8
MAX_CODEC_BATCH_TOTAL_ITEMS = 1024
MAX_CODEC_BATCH_INPUT_BYTES = 32 * 1024 * 1024
MAX_CODEC_BATCH_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_CODEC_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_CODEC_PREVIEW_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class GeometryCodecItemFailure:
    """One recoverable, pre-native mesh validation failure in an ordered batch."""

    item_index: int
    input_sha256: str
    code: Literal["MESH_INPUT_INVALID"]
    message: str


def normalize_batch_control(requests: Sequence[GeometryNormalizeRequest], *,
                            include_raw_brep_summary: bool) -> tuple[dict[str, Any], tuple[bytes, ...]]:
    if not 1 <= len(requests) <= MAX_CODEC_BATCH_TOTAL_ITEMS:
        raise GeometryCodecProtocolError("geometry codec batch requires 1..1024 items")
    if sum(len(request.payload) for request in requests) > MAX_CODEC_BATCH_INPUT_BYTES:
        raise GeometryCodecProtocolError("geometry codec batch input byte budget exceeded")
    chunks = []
    size = 0
    frames = 0
    for start in range(0, len(requests), MAX_CODEC_BATCH_ITEMS):
        group = requests[start:start + MAX_CODEC_BATCH_ITEMS]
        control = normalize_control(group, include_raw_brep_summary=include_raw_brep_summary)
        header = encode_json_frame(control, limit=MAX_CODEC_CONTROL_BYTES)
        chunks.append(header)
        chunks.extend(request.payload for request in group)
        size += len(header) + sum(len(request.payload) for request in group)
        frames += 1
    if size > MAX_CODEC_BATCH_INPUT_BYTES:
        raise GeometryCodecProtocolError("geometry codec batch framing exceeds input byte budget")
    return {
        "operation": "normalize_batch", "input_count": 1, "output_count": 1,
        "frame_count": frames, "item_count": len(requests), "input_bytes": size,
    }, tuple(chunks)


class GeometryCodecProtocolError(GeometryRuntimeValidationError):
    """The private child or Host violated the closed codec transport."""


def normalize_control(
    requests: Sequence[GeometryNormalizeRequest],
    *,
    include_raw_brep_summary: bool,
) -> dict[str, Any]:
    if not 1 <= len(requests) <= MAX_CODEC_BATCH_ITEMS:
        raise GeometryCodecProtocolError(
            "geometry codec normalization batch must contain 1..8 items"
        )
    if type(include_raw_brep_summary) is not bool:
        raise GeometryCodecProtocolError("geometry codec raw-summary flag must be boolean")
    return {
        "operation": "normalize",
        "include_raw_brep_summary": include_raw_brep_summary,
        "inputs": [
            {
                "format": request.format.value,
                "source_format": (
                    None if request.source_format is None else request.source_format.value
                ),
                "unit": request.unit,
                "include_summary": request.include_summary,
                "transform": None if request.transform is None else request.transform.model_dump(mode="json"),
                "payload_index": index,
                "payload_size": len(request.payload),
                "payload_sha256": hashlib.sha256(request.payload).hexdigest(),
            }
            for index, request in enumerate(requests)
        ],
        "output_count": len(requests),
    }


def reopen_control(geometry: CanonicalGeometry) -> dict[str, Any]:
    return {
        "operation": "reopen",
        "geometry": canonical_geometry_metadata(geometry, payload_index=0),
        "output_count": 0,
    }


def render_control(request: GeometryRenderRequest) -> dict[str, Any]:
    document = request.model_dump(mode="json", exclude={"geometry", "display_context", "reference_geometries", "fragment_inputs"})
    document["geometry"] = canonical_geometry_metadata(request.geometry, payload_index=0)
    if request.display_context is not None:
        document["display_context"] = canonical_geometry_metadata(
            request.display_context,
            payload_index=1,
        )
    offset = 1 + int(request.display_context is not None)
    if request.reference_geometries:
        document["reference_geometries"] = [
            canonical_geometry_metadata(item, payload_index=offset + index)
            for index, item in enumerate(request.reference_geometries)
        ]
    if request.fragment_inputs:
        document["fragment_inputs"] = pack_fragment_inputs(request.fragment_inputs)[0]
    return {
        "operation": "render",
        "request": document,
        "input_count": offset + len(request.reference_geometries) + int(bool(request.fragment_inputs)),
        "output_count": 1,
    }


def export_stl_control(geometry: CanonicalGeometry) -> dict[str, Any]:
    return {
        "operation": "export_stl",
        "geometry": canonical_geometry_metadata(geometry, payload_index=0),
        "input_count": 1,
        "output_count": 1,
    }


def pack_fragment_inputs(fragments) -> tuple[list[dict[str, Any]], bytes]:
    """One private binary descriptor, with exact offsets; no bytes in control JSON."""
    documents, chunks, offsets = [], [], {}
    length = 0
    for fragment in fragments:
        geometry = fragment.geometry
        key = (geometry.format, geometry.sha256)
        if key not in offsets:
            offsets[key] = length
            chunks.append(geometry.payload)
            length += len(geometry.payload)
        document = fragment.model_dump(mode="json", exclude={"geometry"})
        metadata = geometry.model_dump(mode="json", exclude={"payload"})
        metadata.update(payload_offset=offsets[key], payload_size=len(geometry.payload))
        document["geometry"] = metadata
        documents.append(document)
    if length > MAX_CODEC_PAYLOAD_BYTES:
        raise GeometryCodecProtocolError("fragment bundle exceeds the 64 MiB private transport budget")
    return documents, b"".join(chunks)


def unpack_fragment_inputs(documents, payload: bytes):
    if not isinstance(documents, list) or len(documents) > 128 or len(payload) > MAX_CODEC_PAYLOAD_BYTES:
        raise GeometryCodecProtocolError("fragment bundle transport is invalid")
    result, ranges, verified_payloads = [], set(), {}
    for document in documents:
        require_exact_keys(document, {"fragment_key", "component_key", "binding_key", "geometry", "result_from_local", "uniform_scale"}, label="fragment input")
        metadata = document["geometry"]
        require_exact_keys(metadata, {"format", "content_type", "sha256", "summary", "payload_offset", "payload_size"} | ({"source_provenance"} if "source_provenance" in metadata else set()), label="fragment geometry")
        offset, size = metadata["payload_offset"], metadata["payload_size"]
        if type(offset) is not int or type(size) is not int or offset < 0 or size < 1 or offset + size > len(payload):
            raise GeometryCodecProtocolError("fragment bundle payload range is invalid")
        range_key = (offset, size)
        if range_key not in verified_payloads:
            content = payload[offset:offset + size]
            verified_payloads[range_key] = (content, hashlib.sha256(content).hexdigest())
        content, digest = verified_payloads[range_key]
        if digest != metadata["sha256"]:
            raise GeometryCodecProtocolError("fragment canonical payload SHA-256 mismatch")
        geometry = dict(metadata)
        geometry.pop("payload_offset")
        geometry.pop("payload_size")
        geometry["payload"] = content
        result.append(GeometryInspectionFragmentInput.model_validate({**document, "geometry": geometry}))
        ranges.add((offset, offset + size))
    previous = 0
    for start, end in sorted(ranges):
        if start != previous:
            raise GeometryCodecProtocolError("fragment bundle ranges do not exactly cover its bytes")
        previous = end
    if previous != len(payload):
        raise GeometryCodecProtocolError("fragment bundle has unreferenced bytes")
    return tuple(result)


def section_control(request: GeometrySectionRequest) -> dict[str, Any]:
    document = request.model_dump(mode="json", exclude={"geometry", "fragment_inputs"})
    document["geometry"] = canonical_geometry_metadata(request.geometry, payload_index=0)
    document["fragment_inputs"] = pack_fragment_inputs(request.fragment_inputs)[0]
    return {
        "operation": "section", "request": document,
        "input_count": 1 + int(bool(request.fragment_inputs)),
        "output_count": 1 + len(request.observation.offsets_m),
    }


def display_context_control(
    request: GeometryBuildDisplayContextRequest,
) -> dict[str, Any]:
    return {
        "operation": "build_display_context",
        "request": request.model_dump(mode="json"),
        "input_count": 0,
        "output_count": 1,
    }


def canonical_geometry_metadata(
    geometry: CanonicalGeometry,
    *,
    payload_index: int,
) -> dict[str, Any]:
    document = {
        "format": geometry.format.value,
        "content_type": geometry.content_type,
        "payload_index": payload_index,
        "payload_size": len(geometry.payload),
        "sha256": geometry.sha256,
        "summary": geometry.summary.model_dump(mode="json") if geometry.summary is not None else None,
    }
    if geometry.source_provenance is not None:
        document["source_provenance"] = geometry.source_provenance.model_dump(mode="json")
    return document


def require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise GeometryCodecProtocolError(f"{label} has an invalid closed key set")


def require_descriptor_counts(
    control: Mapping[str, Any],
    *,
    input_count: int,
    output_count: int,
) -> None:
    operation = control.get("operation")
    if operation == "normalize":
        inputs = control.get("inputs")
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= MAX_CODEC_BATCH_ITEMS:
            raise GeometryCodecProtocolError("geometry codec batch descriptor count is invalid")
        declared_input_count = len(inputs)
        expected_output_count = declared_input_count
    elif operation == "normalize_batch":
        if type(control.get("input_count")) is not int or control["input_count"] != 1:
            raise GeometryCodecProtocolError("normalize batch input descriptor count is invalid")
        declared_input_count = 1
        expected_output_count = 1
    elif operation == "reopen":
        declared_input_count = 1
        expected_output_count = 0
    elif operation == "export_stl":
        declared_input_count = control.get("input_count")
        if type(declared_input_count) is not int or declared_input_count != 1:
            raise GeometryCodecProtocolError("geometry codec STL descriptor count is invalid")
        expected_output_count = 1
    elif operation == "render":
        declared_input_count = control.get("input_count")
        if type(declared_input_count) is not int or not 1 <= declared_input_count <= 11:
            raise GeometryCodecProtocolError("geometry codec render descriptor count is invalid")
        expected_output_count = 1
    elif operation == "section":
        declared_input_count = control.get("input_count")
        request = control.get("request")
        observation = request.get("observation") if isinstance(request, Mapping) else None
        offsets = observation.get("offsets_m") if isinstance(observation, Mapping) else None
        if type(declared_input_count) is not int or declared_input_count not in {1, 2} or not isinstance(offsets, list) or not 1 <= len(offsets) <= 4:
            raise GeometryCodecProtocolError("geometry codec section descriptor count is invalid")
        expected_output_count = 1 + len(offsets)
    elif operation == "build_display_context":
        declared_input_count = control.get("input_count")
        if type(declared_input_count) is not int or declared_input_count != 0:
            raise GeometryCodecProtocolError("geometry codec context descriptor count is invalid")
        expected_output_count = 1
    else:
        raise GeometryCodecProtocolError("geometry codec operation is invalid")
    if (
        type(input_count) is not int
        or type(output_count) is not int
        or type(control.get("output_count")) is not int
        or declared_input_count != input_count
        or control["output_count"] != output_count
        or output_count != expected_output_count
    ):
        raise GeometryCodecProtocolError("geometry codec descriptor counts do not match control")


__all__ = [
    "CodecOperation",
    "GeometryCodecProtocolError",
    "MAX_CODEC_BATCH_ITEMS",
    "MAX_CODEC_CONTROL_BYTES",
    "MAX_CODEC_PAYLOAD_BYTES",
    "MAX_CODEC_PREVIEW_BYTES",
    "MAX_CODEC_TERMINAL_BYTES",
    "canonical_geometry_metadata",
    "display_context_control",
    "export_stl_control",
    "normalize_control",
    "render_control",
    "reopen_control",
    "require_descriptor_counts",
    "require_exact_keys",
]
