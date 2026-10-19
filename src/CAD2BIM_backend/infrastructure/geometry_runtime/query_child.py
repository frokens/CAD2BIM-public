"""Private child for one isolated, read-only geometry query."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import traceback
from typing import Any, BinaryIO


def main() -> int:
    from CAD2BIM_backend.infrastructure.worker_runtime import open_binary_fd

    request_stream = open_binary_fd(_descriptor("CAD2BIM_GEOMETRY_REQUEST_FD"), "rb")
    geometry_stream = open_binary_fd(_descriptor("CAD2BIM_GEOMETRY_PAYLOAD_FD"), "rb")
    result_stream = open_binary_fd(_descriptor("CAD2BIM_GEOMETRY_RESULT_FD"), "wb")
    terminal_stream = open_binary_fd(_descriptor("CAD2BIM_GEOMETRY_TERMINAL_FD"), "wb")
    fragment_stream = None
    if "CAD2BIM_GEOMETRY_FRAGMENTS_FD" in os.environ:
        fragment_stream = open_binary_fd(_descriptor("CAD2BIM_GEOMETRY_FRAGMENTS_FD"), "rb")
    started = False
    try:
        request = _read_request(request_stream)
        payload = geometry_stream.read(64 * 1024 * 1024 + 1)
        if not payload or len(payload) > 64 * 1024 * 1024:
            raise _ChildError("INVALID_ARGUMENT", "private geometry payload is invalid", "preflight")
        started = True
        fragment_payload = b"" if fragment_stream is None else fragment_stream.read(64*1024*1024+1)
        if len(fragment_payload) != request["fragment_payload_bytes"]:
            raise _ChildError("INVALID_ARGUMENT", "private fragment payload size disagrees with its metadata", "preflight")
        _execute(request, payload, result_stream, fragment_payload)
        _write_json(
            terminal_stream,
            {
                "status": "succeeded",
                "phase": "execution",
                "execution_started": True,
            },
        )
        return 0
    except BaseException as exc:
        code = str(getattr(exc, "code", "QUERY_FAILED"))
        phase = str(getattr(exc, "phase", "execution" if started else "preflight"))
        _write_json(
            terminal_stream,
            {
                "status": "failed",
                "code": code,
                "phase": phase,
                "execution_started": started,
                "message": _bounded(str(exc), 2_000),
            },
        )
        return 1
    finally:
        for stream in (request_stream, geometry_stream, result_stream, terminal_stream, fragment_stream):
            if stream is not None:
                stream.close()


def _execute(request: dict[str, Any], payload: bytes, result_stream: BinaryIO, fragment_payload: bytes = b"") -> None:
    from CAD2BIM_backend.domain.canonical import canonical_json
    from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry, CanonicalGeometryFormat, GeometryQueryRequest
    from CAD2BIM_backend.infrastructure.worker_runtime.python_user_code import (
        RestrictedImportGuard,
        compile_entry,
        install_readonly_audit_hook,
        safe_builtins,
    )
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution_protocol import unpack_fragment_inputs
    from CAD2BIM_backend.infrastructure.geometry_runtime.section import InspectionGeometry

    expected_sha256 = str(request["expected_geometry_sha256"])
    before_sha256 = hashlib.sha256(payload).hexdigest()
    if before_sha256 != expected_sha256:
        raise _ChildError("GEOMETRY_DIGEST_MISMATCH", "private geometry SHA-256 mismatch")
    format = CanonicalGeometryFormat(str(request["format"]))
    canonical = CanonicalGeometry(format=format, content_type=format.content_type, payload=payload, sha256=before_sha256)
    fragments = unpack_fragment_inputs(request["fragment_inputs"], fragment_payload)
    runtime_request = GeometryQueryRequest(geometry=canonical, source_code=request["source_code"], max_result_bytes=request["max_result_bytes"],
                                          selection=request["selection"], coordinates=request["coordinates"], inspection_context=request["inspection_context"], fragment_inputs=fragments)
    inspection = InspectionGeometry(canonical, fragments, runtime_request.selection, runtime_request.coordinates)
    context = None
    try:
        geometry = inspection.selected()
        source = str(request["source_code"])
        _validate_geometry_query_source(source)
        allowed_import_roots = frozenset({"math", "cadquery", "OCP"})
        compiled = compile_entry(
            source, "query", allowed_import_roots=allowed_import_roots,
            allowed_context_io_attributes=(),
        )
        import_guard = RestrictedImportGuard(
            allowed_import_roots=allowed_import_roots,
            policy={"threads": 2, "rss_bytes": 1024 * 1024**2, "scratch_bytes": 128 * 1024**2},
            worker_label="geometry query worker",
        )
        install_readonly_audit_hook(import_guard)
        scope: dict[str, Any] = {"__builtins__": safe_builtins(import_guard), "__name__": "__cad2bim_geometry_query__"}
        context = _GeometryQueryContext(geometry, None, inspection_source=inspection, inspection_context=runtime_request.inspection_context)
        try:
            exec(compiled, scope)
            entry = scope.get("query")
            if not callable(entry):
                raise TypeError("query(ctx) entry point is unavailable")
            result = entry(context)
        except BaseException as exc:
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            raise _ChildError("QUERY_EXECUTION_FAILED", _bounded(detail, 2_000)) from exc
        result_document = {"result": result, "geometry_sha256": before_sha256}
        try:
            result_bytes = canonical_json(result_document).encode("utf-8")
        except (ValueError, TypeError) as exc:
            raise _ChildError("QUERY_RESULT_INVALID", "query result must be canonical JSON") from exc
        if len(result_bytes) > int(request["max_result_bytes"]):
            raise _ChildError("QUERY_RESULT_BUDGET_EXCEEDED", "query result exceeds byte budget")
        result_stream.write(result_bytes)
        result_stream.flush()
    finally:
        if context is not None:
            context.close()
        else:
            inspection.close()


class _GeometryQueryContext:
    """Read-only query API over one process-private OCP or mesh copy."""

    __slots__ = ("_geometry", "_summary", "_computation_copy", "_mesh", "_inspection_source", "_inspection_context", "_fragment_copies", "_display_meshes", "_display_report")

    def __init__(self, geometry: Any, summary: Any, *, inspection_source=None, inspection_context=None) -> None:
        self._geometry = geometry
        self._summary = summary
        self._computation_copy = None
        self._mesh = None
        self._inspection_source = inspection_source
        self._inspection_context = inspection_context or {}
        self._fragment_copies = {}
        self._display_meshes = None
        self._display_report = None

    def private_geometry(self) -> Any:
        """Return a memoized detached computation copy; source facts remain unchanged."""
        if self._computation_copy is None:
            from copy import deepcopy
            from CAD2BIM_backend.infrastructure.geometry_runtime.codec import MeshData

            self._computation_copy = (
                deepcopy(self._geometry) if isinstance(self._geometry, MeshData)
                else self._geometry.copy(mesh=False)
            )
        return self._computation_copy

    def summary(self) -> dict[str, Any]:
        if self._summary is None:
            from CAD2BIM_backend.infrastructure.geometry_runtime.codec import MeshData, summarize_mesh, summarize_shape
            self._summary = summarize_mesh(self._geometry) if isinstance(self._geometry, MeshData) else summarize_shape(self._geometry)
        return self._summary.model_dump(mode="json")

    def _context_fact(self, key, default=None):
        return json.loads(json.dumps(self._inspection_context.get(key, default), allow_nan=False))

    def source(self):
        fallback = {"geometry_sha256": self._inspection_source.geometry.sha256, "format": self._inspection_source.geometry.format.value, "unit": "METRE"}
        return self._context_fact("source", fallback)

    def components(self):
        return self._context_fact("components", [])

    def fragments(self):
        fallback = [{"fragment_key": item.fragment_key, "component_key": item.component_key, "binding_key": item.binding_key,
                     "geometry_sha256": item.geometry.sha256, "result_from_local": item.result_from_local.model_dump(mode="json"),
                     "uniform_scale": item.uniform_scale} for item in self._inspection_source.fragments.values()]
        return self._context_fact("fragments", fallback)

    def interfaces(self):
        return self._context_fact("interfaces")

    def checks(self):
        return self._context_fact("checks")

    def private_fragment(self, fragment_key, coordinate_space="result"):
        from copy import deepcopy
        from CAD2BIM_backend.infrastructure.geometry_runtime.codec import MeshData
        key = (fragment_key, coordinate_space)
        if key not in self._fragment_copies:
            shape = self._inspection_source.fragment(fragment_key, coordinate_space)
            self._fragment_copies[key] = deepcopy(shape) if isinstance(shape, MeshData) else shape.copy(mesh=False)
        return self._fragment_copies[key]

    def sections(self, plane, offsets_m=(0.0,)):
        from CAD2BIM_backend.domain.geometry_inspection import GeometrySectionObservation
        from CAD2BIM_backend.infrastructure.geometry_runtime.section import measure_sections
        observation = GeometrySectionObservation(plane=plane, offsets_m=offsets_m)
        measurements = measure_sections(self._inspection_source, observation)
        return {"geometry_sha256": self._inspection_source.geometry.sha256,
                "selection": self._inspection_source.selection.model_dump(mode="json"),
                "coordinates": self._inspection_source.coordinates.model_dump(mode="json"),
                "measurements": [m.model_dump(mode="json") for m in measurements]}

    def close(self):
        self._fragment_copies.clear()
        if self._inspection_source is not None:
            self._inspection_source.close()
        self._geometry = self._summary = self._mesh = self._computation_copy = None
        self._display_meshes = self._display_report = None
        self._inspection_context = {}

    def vertices(self) -> tuple[tuple[float, float, float], ...]:
        return tuple(tuple(vertex) for vertex in self._render_mesh().vertices)

    def triangles(self) -> tuple[tuple[int, int, int], ...]:
        return tuple(tuple(triangle) for triangle in self._render_mesh().triangles)

    def face_types(self) -> tuple[str, ...]:
        faces = getattr(self._geometry, "Faces", None)
        if faces is None:
            return ()
        return tuple(sorted(str(face.geomType()) for face in faces()))

    def edge_types(self) -> tuple[str, ...]:
        edges = getattr(self._geometry, "Edges", None)
        if edges is None:
            return ()
        return tuple(sorted(str(edge.geomType()) for edge in edges()))

    def _render_mesh(self) -> Any:
        from CAD2BIM_backend.infrastructure.geometry_runtime.codec import require_reliable_display_mesh
        if self._mesh is None:
            from CAD2BIM_backend.infrastructure.geometry_runtime.codec import MeshData
            vertices, triangles = [], []
            for _, part in self._selected_display_meshes():
                offset = len(vertices)
                vertices.extend(part.vertices)
                triangles.extend([[i + offset for i in t] for t in part.triangles])
            self._mesh = MeshData(vertices, triangles)
        for _, mesh in self._selected_display_meshes():
            require_reliable_display_mesh(mesh)
        return self._mesh

    def _selected_display_meshes(self):
        from CAD2BIM_backend.infrastructure.geometry_runtime.codec import display_mesh
        if self._display_meshes is None:
            self._display_meshes = tuple((item, display_mesh(item.shape)) for item in self._inspection_source.display_objects())
        return self._display_meshes

    def display_diagnostics(self):
        from CAD2BIM_backend.infrastructure.geometry_runtime.codec import display_diagnostics
        if self._display_report is None:
            objects = []
            for item, mesh in self._selected_display_meshes():
                selection = ({"selection_kind": "whole"} if item.fragment_key is None else
                             {"selection_kind": "fragment", "fragment_key": item.fragment_key})
                objects.append({"selection": selection, "component_key": item.component_key,
                                **display_diagnostics(mesh)})
            self._display_report = {"selection": self._inspection_source.selection.model_dump(mode="json"),
                "coordinates": self._inspection_source.coordinates.model_dump(mode="json"), "objects": objects,
                "status": "complete" if objects and all(item["status"] == "complete" for item in objects) else "incomplete",
                "limitation": "Native no_issue_reported and completed discrete checks are not mathematical or business acceptance."}
        return json.loads(json.dumps(self._display_report, allow_nan=False))


def _validate_geometry_query_source(source: str) -> None:
    try:
        tree = ast.parse(source, filename="<cad2bim-query>", mode="exec")
    except SyntaxError:
        # The shared compiler produces the bounded syntax diagnostic.
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            if any(name.split(".", 1)[0] not in {"math", "cadquery", "OCP"} for name in names):
                raise _ChildError(
                    "QUERY_SOURCE_INVALID",
                    "geometry queries may import only math, cadquery, and OCP",
                    "preflight",
                )
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise _ChildError(
                "QUERY_SOURCE_INVALID",
                "geometry queries cannot access private runtime attributes",
                "preflight",
            )


def _read_request(stream: BinaryIO) -> dict[str, Any]:
    content = stream.read(512 * 1024 + 1)
    if len(content) > 512 * 1024:
        raise _ChildError("INVALID_ARGUMENT", "geometry query request exceeds 512 KiB", "preflight")
    value = json.loads(content)
    if not isinstance(value, dict) or set(value) != {
        "format",
        "expected_geometry_sha256",
        "source_code",
        "max_result_bytes",
        "selection", "coordinates", "inspection_context", "fragment_inputs", "fragment_payload_bytes",
    }:
        raise _ChildError("INVALID_ARGUMENT", "geometry query request is invalid", "preflight")
    if not isinstance(value["source_code"], str) or not value["source_code"]:
        raise _ChildError("INVALID_ARGUMENT", "geometry query source is invalid", "preflight")
    if len(value["source_code"].encode("utf-8")) > 65_536:
        raise _ChildError("INVALID_ARGUMENT", "geometry query source exceeds 65536 bytes", "preflight")
    if not isinstance(value["max_result_bytes"], int):
        raise _ChildError("INVALID_ARGUMENT", "geometry query result budget is invalid", "preflight")
    if type(value["fragment_payload_bytes"]) is not int or not 0 <= value["fragment_payload_bytes"] <= 64*1024*1024:
        raise _ChildError("INVALID_ARGUMENT", "geometry query fragment byte budget is invalid", "preflight")
    return value


class _ChildError(RuntimeError):
    def __init__(self, code: str, message: str, phase: str = "execution") -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase


def _write_json(stream: BinaryIO, value: dict[str, Any]) -> None:
    try:
        stream.write(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        stream.flush()
    except OSError:
        pass


def _descriptor(name: str) -> int:
    try:
        value = int(os.environ.pop(name))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("geometry query worker descriptor contract is invalid") from exc
    if value < 3:
        raise RuntimeError("geometry query worker descriptor contract is invalid")
    return value


def _bounded(value: str, limit: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return encoded.decode("utf-8")
    return encoded[:limit].decode("utf-8", errors="ignore")


if __name__ == "__main__":
    raise SystemExit(main())
