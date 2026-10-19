"""Deterministic OCP/CadQuery codecs for the two geometry runtime formats."""

from __future__ import annotations

import hashlib
import json
import math
import struct
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    import cadquery as cq

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometryBounds,
    GeometryNormalizeRequest,
    GeometryRuntimeValidationError,
    GeometrySourceFormat,
    IfcGeometrySourceProvenance,
    GeometrySummary,
    GeometryTopologySummary,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import (
    reopen_user_shape,
    save_user_shape,
    validate_user_shape,
    transform_occt_shape,
    read_native_shape,
    validate_native_shape,
)


OCCT_BREP_CONTENT_TYPE = CanonicalGeometryFormat.OCCT_BREP_V1.content_type
INDEXED_TRIANGLE_MESH_CONTENT_TYPE = CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1.content_type
_MESH_KEYS = frozenset({"schema", "unit", "vertices", "triangles"})
_ROUND_DIGITS = 12


@dataclass(slots=True)
class MeshData:
    """Private mutable query copy; never crosses the runtime boundary."""

    vertices: list[list[float]]
    triangles: list[list[int]]
    display_quality_warnings: tuple[dict[str, Any], ...] = ()
    display_faces: tuple[Any, ...] = ()


@dataclass(slots=True)
class _DisplayFace:
    """Same-meshing observations, owned and released with this private mesh."""

    native: cq.Face
    selected: cq.Face
    facts: dict[str, Any]
    wires: tuple[Any, ...] | None
    diagnostic: dict[str, Any] | None = None


def normalize_geometry(request: GeometryNormalizeRequest, *, observe_phase=None) -> CanonicalGeometry:
    phase = observe_phase or (lambda _name: nullcontext())
    source_provenance = None
    native_only = (request.effective_source_format is GeometrySourceFormat.OCCT_BREP
                   and request.format is CanonicalGeometryFormat.OCCT_BREP_V1
                   and request.transform is None and not request.include_summary)
    if native_only:
        with phase("brep_open"):
            try:
                shape = read_native_shape(request.payload)
            except Exception as exc:
                raise GeometryRuntimeValidationError("OCCT BREP payload could not be reopened as non-empty geometry") from exc
        with phase("brep_validate"):
            _validate_brep(shape)
        payload, summary, content_type = request.payload, None, OCCT_BREP_CONTENT_TYPE
    elif (request.effective_source_format is GeometrySourceFormat.IFC
            and request.format is CanonicalGeometryFormat.OCCT_BREP_V1):
        shape, source_provenance = _brep_from_ifc(request)
        shape, payload = _save_brep(shape)
        summary = summarize_shape(shape) if request.include_summary else None
        content_type = OCCT_BREP_CONTENT_TYPE
    elif request.effective_source_format is GeometrySourceFormat.IFC:
        mesh = _mesh_from_ifc(request.payload)
        payload = write_mesh(mesh)
        reopened = parse_mesh(payload)
        if write_mesh(reopened) != payload:
            raise GeometryRuntimeValidationError(
                "IFC-derived mesh did not produce stable reopen bytes"
            )
        summary = summarize_mesh(reopened) if request.include_summary else None
        content_type = INDEXED_TRIANGLE_MESH_CONTENT_TYPE
    elif request.format is CanonicalGeometryFormat.OCCT_BREP_V1:
        with phase("brep_open"):
            shape = _read_brep(request.payload)
        if request.transform is not None:
            shape = transform_occt_shape(shape, request.transform)
            with phase("brep_transform_save_validate"):
                shape, payload = _save_brep(shape)
        else:
            payload = request.payload
            with phase("brep_validate"):
                _validate_brep(shape)
        summary = None
        if request.include_summary:
            with phase("brep_summary"):
                summary = summarize_shape(shape)
        content_type = OCCT_BREP_CONTENT_TYPE
    else:
        mesh = parse_mesh(request.payload)
        payload = write_mesh(mesh)
        reopened = parse_mesh(payload)
        if write_mesh(reopened) != payload:
            raise GeometryRuntimeValidationError(
                "indexed triangle mesh did not produce stable reopen bytes"
            )
        summary = summarize_mesh(reopened) if request.include_summary else None
        content_type = INDEXED_TRIANGLE_MESH_CONTENT_TYPE
    with phase("canonical_identity_dto"):
        return CanonicalGeometry(
            format=request.format,
            content_type=content_type,
            payload=payload,
            sha256=_sha256(payload),
            summary=summary,
            source_provenance=source_provenance,
        )


def read_geometry_payload(geometry: CanonicalGeometry) -> Any:
    """Verify and open saved bytes without deriving an unused summary or mesh."""
    if _sha256(geometry.payload) != geometry.sha256:
        raise GeometryRuntimeValidationError("canonical geometry payload SHA-256 mismatch")
    expected_content_type = (
        OCCT_BREP_CONTENT_TYPE
        if geometry.format is CanonicalGeometryFormat.OCCT_BREP_V1
        else INDEXED_TRIANGLE_MESH_CONTENT_TYPE
    )
    if geometry.content_type != expected_content_type:
        raise GeometryRuntimeValidationError("canonical geometry content type mismatch")
    if geometry.format is CanonicalGeometryFormat.OCCT_BREP_V1:
        value = _read_brep(geometry.payload)
    else:
        value = parse_mesh(geometry.payload)
        if write_mesh(value) != geometry.payload:
            raise GeometryRuntimeValidationError("indexed triangle mesh payload is not canonical")
    return value


def reopen_geometry(geometry: CanonicalGeometry) -> tuple[Any, GeometrySummary]:
    value = read_geometry_payload(geometry)
    summary = (
        summarize_shape(value) if geometry.format is CanonicalGeometryFormat.OCCT_BREP_V1
        else summarize_mesh(value)
    )
    return value, summary


def private_copy_from_canonical_payload(
    format: CanonicalGeometryFormat,
    payload: bytes,
) -> tuple[cq.Shape | MeshData, GeometrySummary]:
    """Open canonical bytes without introducing Catalog or storage identities."""

    if format is CanonicalGeometryFormat.OCCT_BREP_V1:
        value = _read_brep(payload)
        return value, summarize_shape(value)
    value = parse_mesh(payload)
    if write_mesh(value) != payload:
        raise GeometryRuntimeValidationError("indexed triangle mesh payload is not canonical")
    return value, summarize_mesh(value)


def mesh_for_render(value: cq.Shape | MeshData) -> MeshData:
    mesh = display_mesh(value)
    require_reliable_display_mesh(mesh)
    if not mesh.vertices or not mesh.triangles:
        raise GeometryRuntimeValidationError("geometry has no renderable faces")
    return mesh


def write_stl(mesh: MeshData) -> bytes:
    """Binary STL of the existing display triangles in original metre coordinates."""
    if not mesh.triangles or 84 + 50 * len(mesh.triangles) > 64 * 1024 * 1024:
        raise GeometryRuntimeValidationError("display STL exceeds its triangle byte budget")
    payload = bytearray(b"CAD2BIM saved geometry; coordinates in metres".ljust(80, b"\0"))
    payload.extend(struct.pack("<I", len(mesh.triangles)))
    for triangle in mesh.triangles:
        points = [mesh.vertices[index] for index in triangle]
        normal = _cross(_subtract(points[1], points[0]), _subtract(points[2], points[0]))
        length = math.sqrt(_dot(normal, normal))
        if not math.isfinite(length) or length <= 0:
            raise GeometryRuntimeValidationError("display STL has a degenerate triangle")
        values = [value / length for value in normal] + [value for point in points for value in point]
        try:
            record = struct.pack("<12fH", *values, 0)
        except (OverflowError, struct.error) as exc:
            raise GeometryRuntimeValidationError("display STL coordinates exceed finite float32") from exc
        floats = struct.unpack("<12fH", record)[:-1]
        if not all(math.isfinite(value) for value in floats):
            raise GeometryRuntimeValidationError("display STL coordinates exceed finite float32")
        a, b, c = (floats[start:start + 3] for start in (3, 6, 9))
        float_normal = _cross(_subtract(b, a), _subtract(c, a))
        if _dot(float_normal, float_normal) == 0:
            raise GeometryRuntimeValidationError("display STL triangle collapses at float32 precision")
        payload.extend(record)
    return bytes(payload)


def display_mesh(value: cq.Shape | MeshData) -> MeshData:
    """Derive observations even when this selected mesh cannot be rendered."""
    if isinstance(value, MeshData):
        return value
    try:
        mesh = _tessellate_shape(value)
    except GeometryRuntimeValidationError:
        raise
    except Exception as exc:  # pragma: no cover - OCP exception classes vary by build
        raise GeometryRuntimeValidationError("OCP shape could not be tessellated") from exc
    return mesh


def _tessellate_shape(shape: cq.Shape) -> MeshData:
    """One mesher/selection owner; canonical topology is never edited or meshed."""

    import cadquery as cq
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.BRepTools import BRepTools
    from OCP.TopTools import TopTools_IndexedMapOfShape

    copier = BRepBuilderAPI_Copy(shape.wrapped, True, False)
    display = cq.Shape.cast(copier.Shape())
    native_faces = shape.Faces()
    mapped_faces = [cq.Face(copier.ModifiedShape(face.wrapped).Oriented(face.wrapped.Orientation()))
                    for face in native_faces]
    BRepTools.RemoveInternals_s(display.wrapped, False)
    display_faces = display.Faces()
    display_face_map = TopTools_IndexedMapOfShape()
    for face in display_faces:
        display_face_map.Add(face.wrapped)
    if len(display_faces) != len(mapped_faces) or any(
        not display_face_map.Contains(mapped.wrapped) for mapped in mapped_faces
    ):
        raise GeometryRuntimeValidationError("display copy face mapping could not be confirmed")
    boundaries = _mesh_with_boundaries(display, 0.1, mapped_faces)
    vertices: list[list[float]] = []
    triangles: list[list[int]] = []
    warnings = []
    observations = []
    for face_index, face in enumerate(mapped_faces):
        original = _read_existing_triangulation(face)
        native = native_faces[face_index]
        area = native.Area()
        initial = _display_mesh_facts(original, area)
        mesh = original
        selected_facts = initial
        selected_face, selected_wires = face, boundaries.get(face_index)
        facts = {"face_index": face_index, "surface_type": native.geomType(),
                 "native_area_m2": area, "selected_attempt": "initial",
                 "initial_mesh_area_m2": None if initial is None else initial[0],
                 "initial_relative_difference": None if initial is None else initial[1],
                 "retry_mesh_area_m2": None, "retry_relative_difference": None,
                 "retry_error": None, "quality_candidate": False}
        if initial is None or initial[1] is None or initial[1] > 0.05:
            # OCCT can emit spurious spans on trimmed extrusion faces even at
            # tight angular deflection. Retry only that detached display face;
            # do not change canonical geometry or shared edge triangulations.
            retry_mesh = None
            retry_error = None
            retry_wires = None
            try:
                detached = face.copy()
                BRepTools.Clean_s(detached.wrapped)
                retry_boundaries = _mesh_with_boundaries(detached, 0.5, [detached])
                retry_wires = retry_boundaries.get(0)
                retry_mesh = _read_existing_triangulation(detached)
            except Exception as exc:  # OCP exceptions vary; a safe original remains usable.
                retry_error = type(exc).__name__
            retry = None if retry_mesh is None else _display_mesh_facts(retry_mesh, area)
            # Area closeness is only a deterministic selection heuristic. Keep
            # both candidates until selection; even a closer retry is warned.
            use_retry = retry is not None and (
                initial is None or (retry[1] is not None and (
                    initial[1] is None or retry[1] < initial[1]
                ))
            )
            if use_retry:
                mesh = retry_mesh
                selected_facts = retry
                selected_face, selected_wires = detached, retry_wires
            warnings.append({
                "face_index": face_index, "native_area_m2": area,
                "message": f"Face {face_index} ({native.geomType()}) display area differs: "
                    f"initial relative difference={None if initial is None else initial[1]}, "
                    f"retry relative difference={None if retry is None else retry[1]}, "
                    f"selected={'retry' if use_retry else 'initial'}, "
                    f"retry={retry_error or ('no safe mesh' if retry is None else 'completed')}. "
                    "Image is diagnostic only; area agreement does not certify geometry. "
                    "Investigate with ctx.display_diagnostics() using this object's exact saved target when available.",
                "initial_mesh_area_m2": None if initial is None else initial[0],
                "retry_mesh_area_m2": None if retry is None else retry[0],
                "initial_relative_difference": None if initial is None else initial[1],
                "retry_relative_difference": None if retry is None else retry[1],
                "selected_attempt": "retry" if use_retry else "initial",
                "retry_error": retry_error if retry_error else (
                    "unsafe_or_missing_retry_mesh" if retry is None else None
                ),
            })
            facts.update({key: warnings[-1][key] for key in (
                "selected_attempt", "retry_mesh_area_m2", "retry_relative_difference", "retry_error")})
            facts["quality_candidate"] = True
        facts["mesh_available"] = selected_facts is not None
        observation = _DisplayFace(native, selected_face, facts, selected_wires)
        observations.append(observation)
        if facts["quality_candidate"]:
            observation.diagnostic = _check_display_face(observation)
        if not facts["mesh_available"]:
            continue
        offset = len(vertices)
        vertices.extend(mesh.vertices)
        triangles.extend([[index + offset for index in triangle] for triangle in mesh.triangles])
    return MeshData(vertices=vertices, triangles=triangles, display_quality_warnings=tuple(warnings),
                    display_faces=tuple(observations))


def _mesh_with_boundaries(shape, angle, faces):
    """Retain the standard mesher's model; only intermediate cleanup differs."""
    from OCP import BRepMesh as bm
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.IMeshTools import IMeshTools_Context
    from OCP.TopAbs import TopAbs_FORWARD, TopAbs_REVERSED

    context = IMeshTools_Context()
    context.SetModelBuilder(bm.BRepMesh_ModelBuilder())
    context.SetEdgeDiscret(bm.BRepMesh_EdgeDiscret())
    context.SetModelHealer(bm.BRepMesh_ModelHealer())
    context.SetPreProcessor(bm.BRepMesh_ModelPreProcessor())
    context.SetPostProcessor(bm.BRepMesh_ModelPostProcessor())
    context.SetFaceDiscret(bm.BRepMesh_FaceDiscret(bm.BRepMesh_MeshAlgoFactory()))
    operation = bm.BRepMesh_IncrementalMesh()
    operation.SetShape(shape.wrapped)
    parameters = operation.ChangeParameters()
    parameters.Deflection, parameters.Angle, parameters.Relative = 0.001, angle, True
    parameters.CleanModel = False
    operation.Perform(context)
    model = context.GetModel()
    result = {}
    if model is None:
        return result
    for index in range(model.FacesNb()):
        meshed = model.GetFace(index)
        matches = [i for i, face in enumerate(faces) if face.wrapped.IsSame(meshed.GetFace())]
        if len(matches) != 1:
            continue
        wires = []
        surface = BRepAdaptor_Surface(meshed.GetFace())
        for wi in range(meshed.WiresNb()):
            wire = meshed.GetWire(wi)
            points, complete = [], True
            previous_tolerance = BRep_Tool.Tolerance_s(meshed.GetFace())
            for ei in range(wire.EdgesNb()):
                edge, orientation = wire.GetEdge(ei), wire.GetEdgeOrientation(ei)
                curves = [edge.GetPCurve(pi) for pi in range(edge.PCurvesNb())
                          if edge.GetPCurve(pi).GetFace().GetFace().IsSame(meshed.GetFace())
                          and edge.GetPCurve(pi).ParametersNb()]
                matching = [curve for curve in curves if curve.IsForward() == (orientation == TopAbs_FORWARD)]
                curve = matching[0] if len(matching) == 1 else curves[0] if len(curves) == 1 else None
                if curve is None:
                    complete = False
                    continue
                part = [(curve.GetPoint(k).X(), curve.GetPoint(k).Y()) for k in range(curve.ParametersNb())]
                if orientation == TopAbs_REVERSED:
                    part.reverse()
                tolerance = max(BRep_Tool.Tolerance_s(meshed.GetFace()), BRep_Tool.Tolerance_s(edge.GetEdge()))
                if points and surface.Value(*points[-1]).Distance(surface.Value(*part[0])) > max(tolerance, previous_tolerance):
                    complete = False
                previous_tolerance = tolerance
                points.extend(part[1:] if points and points[-1] == part[0] else part)
            wires.append((tuple(points), complete))
        result[matches[0]] = tuple(wires)
    return result


def _proper_segment_crossing(a, b, c, d):
    """Strict interior crossing; endpoint touch and collinearity do not fail."""
    def orient(p, q, r):
        x, y = (q[0]-p[0])*(r[1]-p[1]), (q[1]-p[1])*(r[0]-p[0])
        determinant = x-y
        error = 8 * (math.ulp(x) + math.ulp(y))
        return 0 if abs(determinant) <= error else 1 if determinant > 0 else -1
    first, second, third, fourth = orient(a,b,c), orient(a,b,d), orient(c,d,a), orient(c,d,b)
    if first * second >= 0 or third * fourth >= 0:
        return None
    ab, cd, ac = (b[0]-a[0], b[1]-a[1]), (d[0]-c[0], d[1]-c[1]), (c[0]-a[0], c[1]-a[1])
    denominator = ab[0]*cd[1]-ab[1]*cd[0]
    t = (ac[0]*cd[1]-ac[1]*cd[0])/denominator
    return (a[0]+t*ab[0], a[1]+t*ab[1])


def _check_display_face(observation):
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepCheck import BRepCheck_Analyzer, BRepCheck_Wire, BRepCheck_NoError
    from OCP.GeomAbs import GeomAbs_Plane

    face, native = observation.selected, observation.native
    surface = BRepAdaptor_Surface(face.wrapped)
    statuses = []
    for wire in native.Wires():
        check = BRepCheck_Wire(wire.wrapped)
        statuses.extend((check.Closed(), check.Closed2d(native.wrapped), check.Orientation(native.wrapped)))
    native_ok = BRepCheck_Analyzer(native.wrapped).IsValid() and all(status == BRepCheck_NoError for status in statuses)
    box = native.BoundingBox()
    result = {key: value for key, value in observation.facts.items() if key != "quality_candidate"}
    result.update(bbox_m={"minimum_m": [box.xmin,box.ymin,box.zmin], "maximum_m": [box.xmax,box.ymax,box.zmax]},
                  native_boundary={"status": "no_issue_reported" if native_ok else "issue_reported",
                                   "checks": ["BRepCheck_Analyzer", "wire_closed", "wire_closed2d", "wire_orientation"],
                                   "reported_statuses": sorted(set(str(status) for status in statuses)),
                                   "limitation": "Native checks are not a strict proof of boundary correctness."})
    if surface.GetType() != GeomAbs_Plane or surface.IsUPeriodic() or surface.IsVPeriodic():
        result["discrete_boundary"] = {"status": "unsupported", "crossing_segment_pairs": None,
                                       "reason": "Deep crossing checks currently support nonperiodic planar parameter domains only."}
        return result
    if observation.wires is None or not observation.wires or any(not complete for _, complete in observation.wires):
        result["discrete_boundary"] = {"status": "inconclusive", "crossing_segment_pairs": None,
                                       "reason": "Same-meshing ordered pcurves or their exact face mapping are incomplete."}
        return result
    rings = []
    tolerance = BRep_Tool.Tolerance_s(face.wrapped)
    for points, _ in observation.wires:
        if len(points) < 3 or any(not math.isfinite(x) for p in points for x in p):
            result["discrete_boundary"] = {"status": "inconclusive", "crossing_segment_pairs": None, "reason": "Incomplete discrete wire."}
            return result
        start, end = surface.Value(*points[0]), surface.Value(*points[-1])
        if start.Distance(end) > tolerance:
            result["discrete_boundary"] = {"status": "inconclusive", "crossing_segment_pairs": None, "reason": "Discrete wire is not closed within the native face tolerance."}
            return result
        ring = list(points)
        if ring[-1] == ring[0]:
            ring.pop()
        rings.append(ring)
    segments = [(ring[i], ring[(i+1)%len(ring)]) for ring in rings for i in range(len(ring))]
    crossing_count, minimum, maximum = 0, None, None
    for i, (a,b) in enumerate(segments):
        for c,d in segments[i+1:]:
            if max(a[0],b[0]) < min(c[0],d[0]) or max(c[0],d[0]) < min(a[0],b[0]) or max(a[1],b[1]) < min(c[1],d[1]) or max(c[1],d[1]) < min(a[1],b[1]):
                continue
            point = _proper_segment_crossing(a,b,c,d)
            if point is not None:
                position = surface.Value(*point).Coord()
                crossing_count += 1
                minimum = list(position) if minimum is None else [min(a,b) for a,b in zip(minimum,position)]
                maximum = list(position) if maximum is None else [max(a,b) for a,b in zip(maximum,position)]
    result["discrete_boundary"] = {"status": "crossing_reported" if crossing_count else "no_crossing_reported",
        "crossing_segment_pairs": crossing_count, "wire_count": len(rings),
        "intersection_bbox_m": None if not crossing_count else {"minimum_m": minimum, "maximum_m": maximum},
        "limitation": "Strict interior segment-pair crossings only; tangency, coincidence and continuous-curve validity are not certified."}
    return result


def display_diagnostics(mesh):
    """Explicit deep check reuses the selected mesh and retained ordered pcurves."""
    details = []
    completed = 0
    for observation in mesh.display_faces:
        if observation.diagnostic is None:
            observation.diagnostic = _check_display_face(observation)
        detail = observation.diagnostic
        status = detail["discrete_boundary"]["status"]
        completed += status in {"no_crossing_reported", "crossing_reported"}
        if status != "no_crossing_reported" or detail["native_boundary"]["status"] != "no_issue_reported" or not detail["mesh_available"] or observation.facts["quality_candidate"]:
            details.append(detail)
    return {"status": "complete" if completed == len(mesh.display_faces) and mesh.display_faces else "incomplete",
            "face_count": len(mesh.display_faces), "completed_face_count": completed,
            "unchecked_face_count": len(mesh.display_faces)-completed, "faces": details,
            "unavailable_reason": None if mesh.display_faces else "No native face observations; indexed mesh has no native pcurves, and face-free geometry has no display boundaries.",
            "limitation": "Current backend diagnosis of the saved input, not the triangles of a historical PNG or business acceptance."}


def require_reliable_display_mesh(mesh):
    for observation in mesh.display_faces:
        if not observation.facts["mesh_available"]:
            raise GeometryRuntimeValidationError(f"OCP face {observation.facts['face_index']} has no safe renderable triangulation after one retry")
        detail = observation.diagnostic
        if detail is not None and (detail["discrete_boundary"]["status"] == "crossing_reported" or detail["native_boundary"]["status"] == "issue_reported"):
            boundary = detail["discrete_boundary"]
            raise GeometryRuntimeValidationError(
                f"Display boundary failed at face {detail['face_index']} ({detail['surface_type']}): "
                f"native={detail['native_boundary']['status']}, discrete={boundary['status']}, "
                f"crossing segment pairs={boundary['crossing_segment_pairs']}, "
                f"region_m={boundary.get('intersection_bbox_m') or detail['bbox_m']}, "
                f"selected={detail['selected_attempt']}, retry={detail['retry_error'] or 'completed'}. "
                "Use ctx.display_diagnostics() on this object's exact saved target when available; inspect your construction at this region, "
                "make a supported correction, then Preview the same business inputs. The native cause is not established."
            )


def _display_mesh_facts(mesh: MeshData, area: float) -> tuple[float, float | None] | None:
    """Return safe display evidence; missing/illegal data is not an area warning."""
    if (
        not mesh.vertices or not mesh.triangles
        or len(mesh.vertices) > 1_000_000 or len(mesh.triangles) > 2_000_000
        or any(len(v) != 3 or any(not math.isfinite(x) for x in v) for v in mesh.vertices)
        or any(len(t) != 3 or any(type(i) is not int or i < 0 or i >= len(mesh.vertices) for i in t)
               for t in mesh.triangles)
    ):
        return None
    mesh_area = math.fsum(
        _triangle_area(tuple(tuple(mesh.vertices[i]) for i in triangle))
        for triangle in mesh.triangles
    )
    if not math.isfinite(mesh_area):
        return None
    difference = abs(mesh_area - area) / area if area > 0 else None
    return mesh_area, difference


def _read_existing_triangulation(shape: cq.Shape) -> MeshData:
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location
    """Read face-local OCCT triangulations without invoking the mesher."""

    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location

    vertices: list[list[float]] = []
    triangles: list[list[int]] = []
    for face in shape.Faces():
        location = TopLoc_Location()
        poly = BRep_Tool.Triangulation_s(face.wrapped, location)
        if poly is None:
            continue
        transform = location.Transformation()
        offset = len(vertices)
        order = (1, 3, 2) if face.wrapped.Orientation() == TopAbs_REVERSED else (1, 2, 3)
        for index in range(1, poly.NbNodes() + 1):
            point = poly.Node(index).Transformed(transform)
            vertices.append([point.X(), point.Y(), point.Z()])
        for index in range(1, poly.NbTriangles() + 1):
            triangle = poly.Triangle(index)
            triangles.append([triangle.Value(i) + offset - 1 for i in order])
    return MeshData(vertices=vertices, triangles=triangles)


def parse_mesh(payload: bytes) -> MeshData:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GeometryRuntimeValidationError("indexed triangle mesh must be UTF-8 JSON") from exc
    if not isinstance(document, dict) or frozenset(document) != _MESH_KEYS:
        raise GeometryRuntimeValidationError("indexed triangle mesh has an invalid closed document")
    if document.get("schema") != CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1.value:
        raise GeometryRuntimeValidationError("indexed triangle mesh schema is unsupported")
    if document.get("unit") != "METRE":
        raise GeometryRuntimeValidationError("indexed triangle mesh unit must be METRE")
    raw_vertices = document.get("vertices")
    raw_triangles = document.get("triangles")
    if not isinstance(raw_vertices, list) or not raw_vertices:
        raise GeometryRuntimeValidationError("indexed triangle mesh requires vertices")
    if not isinstance(raw_triangles, list) or not raw_triangles:
        raise GeometryRuntimeValidationError("indexed triangle mesh requires triangles")
    if len(raw_vertices) > 1_000_000 or len(raw_triangles) > 2_000_000:
        raise GeometryRuntimeValidationError("indexed triangle mesh exceeds topology budget")

    vertices: list[tuple[float, float, float]] = []
    for raw in raw_vertices:
        if (
            not isinstance(raw, list)
            or len(raw) != 3
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw)
        ):
            raise GeometryRuntimeValidationError("mesh vertices must be finite float64 triples")
        vertex = tuple(_finite_float(value) for value in raw)
        vertices.append(vertex)

    coordinate_to_index: dict[tuple[float, float, float], int] = {}
    ordered_coordinates = sorted(set(vertices))
    for index, vertex in enumerate(ordered_coordinates):
        coordinate_to_index[vertex] = index

    normalized_triangles: list[tuple[int, int, int]] = []
    for raw in raw_triangles:
        if (
            not isinstance(raw, list)
            or len(raw) != 3
            or any(isinstance(value, bool) or not isinstance(value, int) for value in raw)
        ):
            raise GeometryRuntimeValidationError("mesh triangles must be uint32 index triples")
        indices = tuple(raw)
        if any(index < 0 or index >= len(vertices) or index > 0xFFFFFFFF for index in indices):
            raise GeometryRuntimeValidationError("mesh triangle index is outside the vertex array")
        coordinates = tuple(vertices[index] for index in indices)
        if len(set(coordinates)) != 3 or _triangle_area(coordinates) == 0.0:
            raise GeometryRuntimeValidationError("mesh triangle must be non-degenerate")
        rotations = (
            coordinates,
            (coordinates[1], coordinates[2], coordinates[0]),
            (coordinates[2], coordinates[0], coordinates[1]),
        )
        canonical_coordinates = min(rotations)
        normalized_triangles.append(
            tuple(coordinate_to_index[coordinate] for coordinate in canonical_coordinates)
        )
    normalized_triangles.sort()
    if len(set(normalized_triangles)) != len(normalized_triangles):
        raise GeometryRuntimeValidationError("indexed triangle mesh contains duplicate triangles")
    return MeshData(
        vertices=[list(vertex) for vertex in ordered_coordinates],
        triangles=[list(triangle) for triangle in normalized_triangles],
    )


def write_mesh(mesh: MeshData) -> bytes:
    # Reparse the mutable private copy so validation and canonicalization cannot
    # be bypassed by query code.
    raw = {
        "schema": CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1.value,
        "unit": "METRE",
        "vertices": mesh.vertices,
        "triangles": mesh.triangles,
    }
    try:
        payload = canonical_json(raw).encode("utf-8")
    except ValueError as exc:
        raise GeometryRuntimeValidationError("mesh is outside canonical JSON domain") from exc
    reparsed = _parse_mesh_without_recursion(payload)
    return canonical_json(
        {
            "schema": CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1.value,
            "unit": "METRE",
            "vertices": reparsed.vertices,
            "triangles": reparsed.triangles,
        }
    ).encode("utf-8")


def _parse_mesh_without_recursion(payload: bytes) -> MeshData:
    """Normalize through ``parse_mesh`` without calling ``write_mesh``."""

    return parse_mesh(payload)


def summarize_mesh(mesh: MeshData) -> GeometrySummary:
    vertices = [tuple(vertex) for vertex in mesh.vertices]
    triangles = [tuple(triangle) for triangle in mesh.triangles]
    edge_counts: Counter[tuple[int, int]] = Counter()
    area = 0.0
    signed_volume = 0.0
    for triangle in triangles:
        a, b, c = (vertices[index] for index in triangle)
        area += _triangle_area((a, b, c))
        signed_volume += _dot(a, _cross(b, c)) / 6.0
        for start, end in (
            (triangle[0], triangle[1]),
            (triangle[1], triangle[2]),
            (triangle[2], triangle[0]),
        ):
            edge_counts[tuple(sorted((start, end)))] += 1
    boundary_edges = sum(count == 1 for count in edge_counts.values())
    non_manifold = any(count > 2 for count in edge_counts.values())
    volume = None if boundary_edges or non_manifold else abs(signed_volume)
    return GeometrySummary(
        format=CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1,
        bounds=_bounds(vertices),
        topology=GeometryTopologySummary(
            vertex_count=len(vertices),
            edge_count=len(edge_counts),
            wire_count=0,
            face_count=0,
            shell_count=0,
            solid_count=0,
            triangle_count=len(triangles),
            boundary_edge_count=boundary_edges,
        ),
        surface_area_m2=_clean(area),
        volume_m3=None if volume is None else _clean(volume),
    )


def _contains_only_solids(shape: cq.Shape) -> bool:
    # Stop at solids: their own boundary shells/faces are not free lower-dimensional geometry.
    if shape.ShapeType() == "Solid":
        return True
    if shape.ShapeType() in {"Compound", "CompSolid"}:
        return all(_contains_only_solids(child) for child in shape)
    return False


def _brep_surface_boundary_edge_count(shape: cq.Shape) -> int:
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    from OCP.TopoDS import TopoDS
    edge_faces = TopTools_IndexedDataMapOfShapeListOfShape()
    # Keep face occurrences: a periodic seam is used twice by the same face.
    # Free wires have no face occurrences; collapsed pole edges are not boundaries.
    TopExp.MapShapesAndAncestors_s(shape.wrapped, TopAbs_EDGE, TopAbs_FACE, edge_faces)
    return sum(
        edge_faces.FindFromIndex(index).Size() == 1
        and not BRep_Tool.Degenerated_s(TopoDS.Edge_s(edge_faces.FindKey(index)))
        for index in range(1, edge_faces.Extent() + 1)
    )


def summarize_shape(shape: cq.Shape) -> GeometrySummary:
    from OCP.BRepBndLib import BRepBndLib
    from OCP.Bnd import Bnd_Box

    try:
        box = Bnd_Box()
        # Mesh caches must not change the exact geometry summary on a later read.
        BRepBndLib.AddOptimal_s(shape.wrapped, box, False, False)
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
        bounds = GeometryBounds(
            minimum_m=(_clean(xmin), _clean(ymin), _clean(zmin)),
            maximum_m=(_clean(xmax), _clean(ymax), _clean(zmax)),
        )
        topology = GeometryTopologySummary(
            vertex_count=len(shape.Vertices()),
            edge_count=len(shape.Edges()),
            wire_count=len(shape.Wires()),
            face_count=len(shape.Faces()),
            shell_count=len(shape.Shells()),
            solid_count=len(shape.Solids()),
            triangle_count=None,
            boundary_edge_count=_brep_surface_boundary_edge_count(shape),
        )
        area = _clean(float(shape.Area()))
        # CadQuery chooses a Compound's mass integral from its first non-Compound child.
        # Free faces/shells make that result order-dependent and not a physical volume.
        volume = _clean(float(shape.Volume())) if topology.solid_count and _contains_only_solids(shape) else None
    except Exception as exc:  # pragma: no cover - OCP exception classes vary by build
        raise GeometryRuntimeValidationError("OCP shape summary failed") from exc
    if not topology.vertex_count and not topology.edge_count and not topology.face_count:
        raise GeometryRuntimeValidationError("OCP shape is empty")
    return GeometrySummary(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        bounds=bounds,
        topology=topology,
        surface_area_m2=area,
        volume_m3=volume,
    )


def _read_brep(payload: bytes) -> cq.Shape:
    try:
        return reopen_user_shape(payload)
    except Exception as exc:
        raise GeometryRuntimeValidationError("OCCT BREP payload could not be reopened as non-empty geometry") from exc


def _validate_brep(shape: cq.Shape) -> None:
    try:
        from OCP.TopoDS import TopoDS_Shape
        if isinstance(shape, TopoDS_Shape):
            validate_native_shape(shape)
        else:
            validate_user_shape(shape)
    except ValueError as exc:
        raise GeometryRuntimeValidationError(str(exc)) from exc


def _save_brep(shape: cq.Shape) -> tuple[cq.Shape, bytes]:
    try:
        return save_user_shape(shape)
    except Exception as exc:
        raise GeometryRuntimeValidationError(f"OCCT BREP save/reopen validation failed: {exc}") from exc


def _brep_from_ifc(request: GeometryNormalizeRequest) -> tuple[cq.Shape, IfcGeometrySourceProvenance]:
    """Consume the complete product-local native representation without mesh conversion."""
    from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D

    try:
        import ifcopenshell
        import ifcopenshell.geom
        import ifcopenshell.ifcopenshell_wrapper as wrapper
        import ifcopenshell.util.placement
        import ifcopenshell.util.unit

        model = ifcopenshell.file.from_string(request.payload.decode("utf-8"))
        elements = [element for element in model.by_type("IfcElement") if element.Representation]
        if len(elements) != 1:
            raise GeometryRuntimeValidationError("IFC geometry source must contain exactly one represented IfcElement")
        element = elements[0]
        closure = model.traverse(element.Representation)
        if any(item.is_a("IfcTessellatedItem") for item in closure):
            raise GeometryRuntimeValidationError("IFC tessellated items are mesh inputs; native BREP import does not reverse-engineer mesh")
        units = [unit for project in model.by_type("IfcProject") if project.UnitsInContext
                 for unit in project.UnitsInContext.Units if getattr(unit, "UnitType", None) == "LENGTHUNIT"]
        if len(units) != 1:
            raise GeometryRuntimeValidationError("IFC native BREP import requires one explicit project length unit")
        scale = ifcopenshell.util.unit.calculate_unit_scale(model)
        placement = ifcopenshell.util.placement.get_local_placement(element.ObjectPlacement)
        placement[:3, 3] *= scale
        items = {item.id(): item for representation in closure if representation.is_a("IfcRepresentation")
                 for item in representation.Items}
        provenance = IfcGeometrySourceProvenance(
            source_sha256=_sha256(request.payload),
            product_entity_id=element.id(), product_ifc_class=element.is_a(),
            length_unit_scale_to_metre=scale,
            product_placement_m=RigidTransform3D(rotation=placement[:3, :3].tolist(), translation_m=placement[:3, 3].tolist()),
            canonical_from_product_local=request.transform or RigidTransform3D(),
            representation_items=tuple({"entity_id": key, "ifc_class": item.is_a()} for key, item in sorted(items.items())),
        )
        settings = ifcopenshell.geom.settings()
        settings.set("iterator-output", wrapper.SERIALIZED)
        settings.set("use-world-coords", False)
        created = ifcopenshell.geom.create_shape(settings, element)
        shape = _read_brep(created.geometry.brep_data.encode("utf-8"))
        if request.transform is not None:
            shape = transform_occt_shape(shape, request.transform)
        return shape, provenance
    except GeometryRuntimeValidationError:
        raise
    except (UnicodeDecodeError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
        raise GeometryRuntimeValidationError(f"IFC native BREP could not be extracted: {type(exc).__name__}") from exc


def _mesh_from_ifc(payload: bytes) -> MeshData:
    """Extract only local representation geometry from one IFC element."""

    try:
        import ifcopenshell
        import ifcopenshell.geom

        model = ifcopenshell.file.from_string(payload.decode("utf-8"))
        elements = [element for element in model.by_type("IfcElement") if element.Representation]
        if len(elements) != 1:
            raise GeometryRuntimeValidationError(
                "IFC geometry source must contain exactly one represented IfcElement"
            )
        settings = ifcopenshell.geom.settings()
        settings.set(settings.USE_WORLD_COORDS, False)
        settings.set(settings.WELD_VERTICES, True)
        created = ifcopenshell.geom.create_shape(settings, elements[0])
        raw_vertices = list(created.geometry.verts)
        raw_faces = list(created.geometry.faces)
    except GeometryRuntimeValidationError:
        raise
    except (UnicodeDecodeError, RuntimeError, ValueError, TypeError) as exc:
        raise GeometryRuntimeValidationError("IFC source geometry could not be extracted") from exc
    if not raw_vertices or len(raw_vertices) % 3 or not raw_faces or len(raw_faces) % 3:
        raise GeometryRuntimeValidationError("IFC element has no valid triangulated representation")
    document = {
        "schema": CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1.value,
        "unit": "METRE",
        "vertices": [raw_vertices[index : index + 3] for index in range(0, len(raw_vertices), 3)],
        "triangles": [raw_faces[index : index + 3] for index in range(0, len(raw_faces), 3)],
    }
    return parse_mesh(canonical_json(document).encode("utf-8"))


def _bounds(vertices: Iterable[tuple[float, float, float]]) -> GeometryBounds:
    values = tuple(vertices)
    if not values:
        raise GeometryRuntimeValidationError("geometry has no vertices")
    return GeometryBounds(
        minimum_m=tuple(_clean(min(vertex[axis] for vertex in values)) for axis in range(3)),
        maximum_m=tuple(_clean(max(vertex[axis] for vertex in values)) for axis in range(3)),
    )


def _finite_float(value: int | float) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise GeometryRuntimeValidationError("mesh coordinates must be finite float64 values")
    return 0.0 if result == 0.0 else result


def _triangle_area(
    points: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ],
) -> float:
    a, b, c = points
    return 0.5 * math.sqrt(sum(value * value for value in _cross(_subtract(b, a), _subtract(c, a))))


def _subtract(
    left: tuple[float, float, float], right: tuple[float, float, float]
) -> tuple[float, float, float]:
    return tuple(a - b for a, b in zip(left, right, strict=True))


def _cross(
    left: tuple[float, float, float], right: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _clean(value: float) -> float:
    if not math.isfinite(value):
        raise GeometryRuntimeValidationError("geometry summary contains a non-finite value")
    rounded = round(value, _ROUND_DIGITS)
    return 0.0 if rounded == 0.0 else rounded


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "INDEXED_TRIANGLE_MESH_CONTENT_TYPE",
    "MeshData",
    "OCCT_BREP_CONTENT_TYPE",
    "mesh_for_render",
    "normalize_geometry",
    "parse_mesh",
    "private_copy_from_canonical_payload",
    "read_geometry_payload",
    "reopen_geometry",
    "summarize_mesh",
    "summarize_shape",
    "write_mesh",
    "write_stl",
]
