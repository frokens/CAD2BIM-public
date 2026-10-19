"""Private file-descriptor protocol for the Product Realization Worker."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.runtime_execution import MAX_PRODUCT_BATCH_OUTPUT_BYTES as MAX_PRODUCT_BATCH_OUTPUT_BYTES
from CAD2BIM_backend.domain.product_realization import (
    MAX_PRODUCT_REALIZATION_CONTROL_BYTES,
    MAX_PRODUCT_REALIZATION_RESULT_BYTES,
    PRODUCT_REALIZATION_SCRATCH_BYTES,
    ProductRealizationRequest,
    ResolvedMappedGeometryBinding,
    ResolvedModelingFunctionBinding,
    MAX_PRODUCT_REALIZATION_BATCH_ITEMS,
    ProductRealizationOutputNeed,
    geometry_source_request_parameters,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometrySummary,
)


MAX_PRODUCT_REALIZATION_TERMINAL_BYTES = 16 * 1024
MAX_PRODUCT_REALIZATION_PREVIEW_METADATA_BYTES = 64 * 1024
MAX_PRODUCT_REALIZATION_PREVIEW_GEOMETRY_BYTES = 64 * 1024 * 1024
MAX_PRODUCT_REALIZATION_INSTANCE_METADATA_BYTES = 1024 * 1024
MAX_PRODUCT_REALIZATION_INSTANCE_GEOMETRY_BYTES = 64 * 1024 * 1024
MAX_PRODUCT_BATCH_INPUT_BYTES = 16 * 1024 * 1024
MAX_PRODUCT_BATCH_RESULT_FRAME_BYTES = 2 * MAX_PRODUCT_REALIZATION_RESULT_BYTES


class ProductRealizationPreviewGeometryMetadata(BaseModel):
    """Small typed descriptor transported separately from canonical BREP bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: CanonicalGeometryFormat
    content_type: str = Field(pattern=r"^application/vnd\.opencascade\.brep$")
    byte_length: int = Field(ge=1, le=MAX_PRODUCT_REALIZATION_PREVIEW_GEOMETRY_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: GeometrySummary | None = None

    @classmethod
    def from_brep(
        cls,
        payload: bytes,
        summary: GeometrySummary | None,
    ) -> "ProductRealizationPreviewGeometryMetadata":
        return cls(
            format=CanonicalGeometryFormat.OCCT_BREP_V1,
            content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
            byte_length=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            summary=summary,
        )


def materialize_request(
    request: ProductRealizationRequest,
    root: Path,
    *,
    include_preview_geometry: bool = False,
    include_instances: bool = False,
) -> tuple[bytes, bytes]:
    """Write opaque exact binding bytes and return canonical control/program bytes."""

    bindings: list[dict[str, Any]] = []
    payloads: list[tuple[str, bytes]] = []
    for index, binding in enumerate(request.geometry_bindings):
        filename = f"binding-{index:03d}.bin"
        payload = (
            (binding.geometry.payload if binding.geometry is not None else None)
            if isinstance(binding, ResolvedMappedGeometryBinding)
            else binding.source_bundle
        )
        if payload is not None:
            payloads.append((filename, payload))
        if isinstance(binding, ResolvedMappedGeometryBinding):
            document = {
                "resource_kind": binding.resource_kind.value,
                "binding_key": binding.binding_key,
                "geometry_content_digest": binding.geometry_content_digest,
                "ifc_representation_route": binding.ifc_representation_route.value,
                **({"payload_file": filename, "geometry": binding.geometry.model_dump(mode="json", exclude={"payload"})}
                   if binding.geometry is not None else {}),
            }
            if binding.interfaces:
                document["interfaces"] = [
                    item.model_dump(mode="json") for item in binding.interfaces
                ]
            bindings.append(document)
        else:
            bindings.append(
                {
                    "resource_kind": binding.resource_kind.value,
                    "binding_key": binding.binding_key,
                    "geometry_content_digest": binding.geometry_content_digest,
                    "ifc_representation_route": binding.ifc_representation_route.value,
                    "payload_file": filename,
                    "source_bundle_sha256": binding.source_bundle_sha256,
                    "source_reuse_mode": binding.source_reuse_mode,
                    "parameter_schema": binding.parameter_schema,
                }
            )
            if binding.geometry_inputs:
                inputs = []
                for input_index, item in enumerate(binding.geometry_inputs):
                    input_file = f"input-{index:03d}-{input_index:02d}.brep"
                    payloads.append((input_file, item.payload))
                    inputs.append({"input_key": item.input_key, "sha256": item.sha256,
                                   "size_bytes": len(item.payload), "payload_file": input_file})
                bindings[-1]["geometry_inputs"] = inputs
    document = {
        "family_contract": request.family_contract.model_dump(mode="json", by_alias=True),
        "family_contract_digest": request.family_contract_digest,
        "source_bundle_sha256": request.source_bundle_sha256,
        "geometry_bindings": bindings,
        "invocation": request.invocation.model_dump(mode="json"),
        "policy": request.policy.model_dump(mode="json"),
    }
    control = canonical_json(document).encode("utf-8")
    if len(control) > MAX_PRODUCT_REALIZATION_CONTROL_BYTES:
        raise ValueError("product realization control document exceeds its byte budget")
    materialized_bytes = (
        len(control)
        + len(request.source_bundle)
        + sum(len(payload) for _, payload in payloads)
        + MAX_PRODUCT_REALIZATION_RESULT_BYTES
        + MAX_PRODUCT_REALIZATION_TERMINAL_BYTES
        + (MAX_PRODUCT_REALIZATION_INSTANCE_METADATA_BYTES
           + MAX_PRODUCT_REALIZATION_INSTANCE_GEOMETRY_BYTES if include_instances else 0)
        + (
            MAX_PRODUCT_REALIZATION_PREVIEW_METADATA_BYTES
            + MAX_PRODUCT_REALIZATION_PREVIEW_GEOMETRY_BYTES
            if include_preview_geometry
            else 0
        )
    )
    if materialized_bytes > PRODUCT_REALIZATION_SCRATCH_BYTES:
        raise ValueError("product realization inputs exceed the private scratch budget")
    for filename, payload in payloads:
        (root / filename).write_bytes(payload)
    return control, request.source_bundle


def restore_request(
    control: bytes,
    source_bundle: bytes,
    root: Path,
) -> ProductRealizationRequest:
    """Rebuild the typed request before any user code starts."""

    if not 1 <= len(control) <= MAX_PRODUCT_REALIZATION_CONTROL_BYTES:
        raise ValueError("product realization control document is invalid")
    document = json.loads(control)
    if not isinstance(document, dict):
        raise ValueError("product realization control document must be an object")
    raw_bindings = document.pop("geometry_bindings", None)
    if not isinstance(raw_bindings, list):
        raise ValueError("product realization binding document is invalid")
    bindings = []
    for raw in raw_bindings:
        if not isinstance(raw, dict):
            raise ValueError("product realization binding document is invalid")
        value = dict(raw)
        if value.get("resource_kind") == "mapped_model" and "geometry" not in value:
            bindings.append(ResolvedMappedGeometryBinding.model_validate(value))
            continue
        filename = value.pop("payload_file", None)
        if (
            not isinstance(filename, str)
            or re.fullmatch(r"binding-[0-9]{3}\.bin", filename) is None
        ):
            raise ValueError("product realization binding payload reference is invalid")
        payload = (root / filename).read_bytes()
        if value.get("resource_kind") == "mapped_model":
            geometry = value.pop("geometry", None)
            if not isinstance(geometry, dict):
                raise ValueError("mapped geometry metadata is invalid")
            geometry["payload"] = payload
            value["geometry"] = CanonicalGeometry.model_validate(geometry)
            bindings.append(ResolvedMappedGeometryBinding.model_validate(value))
        elif value.get("resource_kind") == "modeling_function":
            value["source_bundle"] = payload
            if "geometry_inputs" in value:
                from CAD2BIM_backend.domain.geometry_runtime import MAX_GEOMETRY_BUILD_INPUTS, MAX_GEOMETRY_BUILD_INPUT_BYTES

                raw_inputs = value.pop("geometry_inputs")
                if not isinstance(raw_inputs, list) or len(raw_inputs) > MAX_GEOMETRY_BUILD_INPUTS:
                    raise ValueError("modeling-function geometry inputs are invalid")
                inputs = []
                total_bytes = 0
                for item in raw_inputs:
                    if not isinstance(item, dict) or set(item) != {"input_key", "sha256", "size_bytes", "payload_file"}:
                        raise ValueError("modeling-function geometry input metadata is invalid")
                    size = item["size_bytes"]
                    input_file = item["payload_file"]
                    if (type(size) is not int or not 1 <= size <= MAX_GEOMETRY_BUILD_INPUT_BYTES
                        or not isinstance(input_file, str)
                        or re.fullmatch(r"input-[0-9]{3}-[0-9]{2}\.brep", input_file) is None):
                        raise ValueError("modeling-function geometry input payload reference is invalid")
                    total_bytes += size
                    if total_bytes > MAX_GEOMETRY_BUILD_INPUT_BYTES:
                        raise ValueError("modeling-function geometry inputs exceed their byte budget")
                    with (root / input_file).open("rb") as stream:
                        input_payload = stream.read(size + 1)
                    if len(input_payload) != size:
                        raise ValueError("modeling-function geometry input size changed")
                    inputs.append({"input_key": item["input_key"], "sha256": item["sha256"], "payload": input_payload})
                value["geometry_inputs"] = inputs
            bindings.append(ResolvedModelingFunctionBinding.model_validate(value))
        else:
            raise ValueError("product realization binding kind is invalid")
    document["geometry_bindings"] = tuple(bindings)
    document["source_bundle"] = source_bundle
    return ProductRealizationRequest.model_validate(document)


def normalize_batch_output_needs(output_need, item_count: int) -> tuple[ProductRealizationOutputNeed, ...]:
    if isinstance(output_need, (ProductRealizationOutputNeed, str)):
        return (ProductRealizationOutputNeed(output_need),) * item_count
    if not isinstance(output_need, Sequence) or len(output_need) != item_count:
        raise ValueError("product batch output needs must match its item count")
    return tuple(ProductRealizationOutputNeed(need) for need in output_need)


def validate_batch_result_frame(response, *, item_index: int, output_need: ProductRealizationOutputNeed) -> None:
    from CAD2BIM_backend.infrastructure.worker_runtime.protocol import WorkerProtocolError
    if (not isinstance(response, dict)
        or set(response) not in ({"status", "item_index", "output_need", "result", "geometries", "constructions"},
            {"status", "item_index", "output_need", "result", "geometries", "constructions", "reused_fragments"})
        or response["status"] != "succeeded" or type(response["item_index"]) is not int
        or response["item_index"] != item_index
        or response["output_need"] != output_need.value):
        raise WorkerProtocolError("product batch result order or output need mismatch")
    reused = response.get("reused_fragments", [])
    if (not isinstance(reused, list) or any(not isinstance(key, str) or not key for key in reused)
            or len(reused) != len(set(reused))
            or (reused and output_need is not ProductRealizationOutputNeed.LOCAL_FRAGMENTS)):
        raise WorkerProtocolError("product batch reused fragment declaration is invalid")
    constructions = response["constructions"]
    if not isinstance(constructions, list) or (output_need is not ProductRealizationOutputNeed.LOCAL_FRAGMENTS and constructions):
        raise WorkerProtocolError("product batch construction payload is unexpected")
    seen = set()
    for item in constructions:
        if (not isinstance(item, dict) or set(item) != {"fragment_key", "byte_length", "sha256"}
            or not isinstance(item["fragment_key"], str) or not item["fragment_key"]
            or item["fragment_key"] in seen or type(item["byte_length"]) is not int
            or not 1 <= item["byte_length"] <= MAX_PRODUCT_BATCH_OUTPUT_BYTES
            or not isinstance(item["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None):
            raise WorkerProtocolError("product batch construction metadata is invalid")
        seen.add(item["fragment_key"])


def _geometry_source_request_key(binding, evaluation, *, exact=False) -> str:
    """Exact source inputs; author-observable parameter ordering is preserved."""
    identity = [binding.resource_kind.value, binding.geometry_content_digest,
                binding.ifc_representation_route.value]
    if isinstance(binding, ResolvedMappedGeometryBinding):
        identity.append(binding.geometry.sha256 if binding.geometry is not None else None)
    else:
        identity.extend((binding.source_reuse_mode, binding.source_bundle_sha256, binding.parameter_schema,
                         [(item.input_key, item.sha256) for item in binding.geometry_inputs]))
    return json.dumps((identity, evaluation.normalized_parameters if exact else geometry_source_request_parameters(binding, evaluation.normalized_parameters), evaluation.uniform_scale),
        ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def product_window_sizes(item_count: int, window_size: int | None = None) -> tuple[int, ...]:
    """Bound each IPC window; total requests remain owned by Apply's input budget."""
    if type(item_count) is not int or item_count < 1:
        raise ValueError("product batch requires at least one item")
    if window_size is None:
        if item_count > MAX_PRODUCT_REALIZATION_BATCH_ITEMS:
            raise ValueError("product batch exceeds its bounded input page")
        return (item_count,)
    if type(window_size) is not int or not 1 <= window_size <= MAX_PRODUCT_REALIZATION_BATCH_ITEMS:
        raise ValueError("product window size is invalid")
    return tuple(min(window_size, item_count - offset) for offset in range(0, item_count, window_size))


def materialize_batch(requests: Sequence[ProductRealizationRequest], root: Path, *, output_needs: Sequence[ProductRealizationOutputNeed], window_size: int | None = None):
    """Freeze one closure once; invocation frames contain no repeated assets."""
    windows = product_window_sizes(len(requests), window_size)
    if len(output_needs) != len(requests):
        raise ValueError("product batch output needs must match its item count")
    base = requests[0]
    from CAD2BIM_backend.infrastructure.worker_runtime.protocol import encode_json_frame
    frame_bytes = len(encode_json_frame({"finish": len(requests)}, limit=1024))
    window_ends, cumulative = set(), 0
    for count in windows[:-1]:
        cumulative += count
        window_ends.add(cumulative)
    for index, request in enumerate(requests):
        if index in window_ends:
            frame_bytes += len(encode_json_frame({"window_end": index}, limit=1024))
            if frame_bytes > MAX_PRODUCT_BATCH_INPUT_BYTES:
                raise ValueError("product window invocation frames exceed their byte budget")
            frame_bytes = len(encode_json_frame({"finish": len(requests)}, limit=1024))
        if request.model_copy(update={"invocation": base.invocation}) != base:
            raise ValueError("product batch requires the same exact source, binding, policy and execution closure")
        frame_bytes += len(encode_json_frame(batch_invocation_frame(index, request, output_need=output_needs[index]), limit=MAX_PRODUCT_REALIZATION_CONTROL_BYTES))
    if frame_bytes > MAX_PRODUCT_BATCH_INPUT_BYTES:
        raise ValueError("product batch invocation frames exceed their byte budget")
    control, source = materialize_request(base, root,
        include_preview_geometry=ProductRealizationOutputNeed.PREVIEW_WORLD in output_needs,
        include_instances=any(need is not ProductRealizationOutputNeed.SOLVE_ONLY for need in output_needs))
    header = {"request": json.loads(control),
              "item_count": len(requests),
              **({"window_sizes": list(windows)} if window_size is not None else {}),
              "entry_names": sorted({"standard_model" if r.invocation.realization_kind == "standard_model" else "realize" for r in requests})}
    payload = canonical_json(header).encode()
    if len(payload) > MAX_PRODUCT_REALIZATION_CONTROL_BYTES + 1024:
        raise ValueError("product batch closure exceeds its byte budget")
    return payload, source


def restore_batch_header(payload: bytes, source: bytes, root: Path):
    header = json.loads(payload)
    if not isinstance(header, dict) or set(header) not in ({"request", "item_count", "entry_names"}, {"request", "item_count", "entry_names", "window_sizes"}):
        raise ValueError("product batch header is not a closed document")
    count = header["item_count"]
    windows = header.get("window_sizes", [count])
    if (type(count) is not int or count < 1 or not isinstance(windows, list) or not windows
            or any(type(value) is not int or not 1 <= value <= MAX_PRODUCT_REALIZATION_BATCH_ITEMS for value in windows)
            or sum(windows) != count):
        raise ValueError("product batch window item counts are invalid")
    entries = header["entry_names"]
    if not isinstance(entries, list) or not entries or any(e not in {"realize", "standard_model"} for e in entries) or len(entries) != len(set(entries)):
        raise ValueError("product batch entry names are invalid")
    control = canonical_json(header["request"]).encode()
    return restore_request(control, source, root), header


def batch_invocation_frame(index: int, request: ProductRealizationRequest, *, output_need: ProductRealizationOutputNeed, precheck=None, reused_fragments=None) -> dict[str, Any]:
    return {"item_index": index,
            "output_need": output_need.value,
            "invocation": request.invocation.model_dump(mode="json"),
            **({"precheck": precheck.model_dump(mode="json")} if precheck is not None else {}),
            **({"reused_fragments": {key: value.model_dump(mode="json") for key, value in reused_fragments.items()}} if reused_fragments else {})}


def restore_batch_invocation(base, frame: dict[str, Any], index: int) -> tuple[ProductRealizationRequest, ProductRealizationOutputNeed]:
    if (set(frame) not in ({"item_index", "output_need", "invocation"}, {"item_index", "output_need", "invocation", "precheck"},
                           {"item_index", "output_need", "invocation", "precheck", "reused_fragments"})
            or type(frame["item_index"]) is not int or frame["item_index"] != index):
        raise ValueError("product batch invocation order or fields are invalid")
    if "reused_fragments" in frame:
        if frame["output_need"] != ProductRealizationOutputNeed.LOCAL_FRAGMENTS.value or not isinstance(frame["reused_fragments"], dict):
            raise ValueError("reused sources require local-fragment instantiation")
        for key, metadata in frame["reused_fragments"].items():
            if not isinstance(key, str) or not key:
                raise ValueError("reused fragment key is invalid")
            ProductRealizationPreviewGeometryMetadata.model_validate(metadata)
    from CAD2BIM_backend.domain.product_realization import _PreparedProductRealizationRequest

    prepared = base if isinstance(base, _PreparedProductRealizationRequest) else _PreparedProductRealizationRequest(base)
    request = prepared.restore_invocation(frame["invocation"])
    return request, ProductRealizationOutputNeed(frame["output_need"])


__all__ = [
    "MAX_PRODUCT_REALIZATION_CONTROL_BYTES",
    "MAX_PRODUCT_REALIZATION_PREVIEW_GEOMETRY_BYTES",
    "MAX_PRODUCT_REALIZATION_PREVIEW_METADATA_BYTES",
    "MAX_PRODUCT_REALIZATION_TERMINAL_BYTES",
    "ProductRealizationPreviewGeometryMetadata",
    "materialize_request",
    "restore_request",
]
