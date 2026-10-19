"""One child-local selector and exact material-section calculation for Query/Preview.

Saved bytes remain the source.  User computation copies and drawing polylines are
never used to update the source, its interfaces, or exact material measurements.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import cadquery as cq
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Section
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepTools import BRepTools_WireExplorer
from OCP.GeomAbs import GeomAbs_Plane
from OCP.TopAbs import TopAbs_REVERSED
from OCP.TopoDS import TopoDS_Iterator
from OCP.gp import gp_Ax3, gp_Dir, gp_Pln, gp_Pnt

from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.geometry_inspection import (
    InspectionResultCoordinates, InspectionWholeSelection,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    GeometryRuntimeValidationError,
    GeometrySectionBoundary,
    GeometrySectionCurve,
    GeometrySectionDiagnostic,
    GeometrySectionMeasurement,
    GeometrySectionObjectMeasurement,
    GeometrySectionRegion,
    GeometrySectionResolvedPlane,
)
from CAD2BIM_backend.infrastructure.geometry_runtime import codec
from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import transform_occt_shape


@dataclass(frozen=True, slots=True)
class _SelectedObject:
    shape: Any
    fragment_key: str | None = None
    component_key: str | None = None
    binding_key: str | None = None


class InspectionGeometry:
    """Bounded same-worker source parsing and rigid placement, without fusion."""

    def __init__(self, geometry, fragment_inputs=(), selection=None, coordinates=None):
        self.geometry = geometry
        self.fragments = {item.fragment_key: item for item in fragment_inputs}
        self.selection = selection or InspectionWholeSelection()
        self.coordinates = coordinates or InspectionResultCoordinates()
        self._parsed: dict[tuple, Any] = {}
        self._placed: dict[tuple, Any] = {}
        self._selected = None

    def _parse(self, geometry):
        key = (geometry.format, geometry.sha256)
        if key not in self._parsed:
            self._parsed[key] = codec.read_geometry_payload(geometry)
        return self._parsed[key]

    def fragment(self, key: str, coordinate_space: str = "result"):
        if key not in self.fragments:
            raise GeometryRuntimeValidationError("fragment key is not in this exact inspection result")
        if coordinate_space not in {"result", "fragment_local"}:
            raise GeometryRuntimeValidationError("fragment coordinate space must be result or fragment_local")
        item = self.fragments[key]
        if coordinate_space == "fragment_local":
            return self._parse(item.geometry)
        cache_key = (key, coordinate_space)
        if cache_key not in self._placed:
            source = self._parse(item.geometry)
            # Same-build instance bytes already include item.uniform_scale.
            transform = item.result_from_local
            if isinstance(source, codec.MeshData):
                vertices = [
                    [transform.translation_m[r] + sum(transform.rotation[r][c] * point[c] for c in range(3)) for r in range(3)]
                    for point in source.vertices
                ]
                self._placed[cache_key] = codec.MeshData(vertices, [list(t) for t in source.triangles], source.display_quality_warnings)
            else:
                self._placed[cache_key] = transform_occt_shape(source, transform)
        return self._placed[cache_key]

    def selected_fragment_keys(self) -> tuple[str, ...]:
        kind = self.selection.selection_kind
        if kind == "fragment":
            return (self.selection.fragment_key,)
        if kind == "component":
            return tuple(key for key, item in self.fragments.items() if item.component_key == self.selection.component_key)
        return tuple(self.fragments)

    def selected(self):
        if self._selected is not None:
            return self._selected
        if self.selection.selection_kind == "whole":
            self._selected = self._parse(self.geometry)
            return self._selected
        shapes = [self.fragment(key, self.coordinates.coordinate_space) for key in self.selected_fragment_keys()]
        if not shapes:
            raise GeometryRuntimeValidationError("inspection selection has no actual geometry")
        if all(isinstance(shape, codec.MeshData) for shape in shapes):
            vertices, triangles, warnings = [], [], []
            for shape in shapes:
                offset = len(vertices)
                vertices.extend([list(p) for p in shape.vertices])
                triangles.extend([[i + offset for i in t] for t in shape.triangles])
                warnings.extend(shape.display_quality_warnings)
            self._selected = codec.MeshData(vertices, triangles, tuple(warnings))
        elif any(isinstance(shape, codec.MeshData) for shape in shapes):
            raise GeometryRuntimeValidationError("mixed BREP/mesh component selection cannot be represented as one native shape")
        else:
            self._selected = shapes[0] if len(shapes) == 1 else cq.Compound.makeCompound(shapes)
        return self._selected

    def objects(self) -> tuple[_SelectedObject, ...]:
        if not self.fragments:
            return tuple(_SelectedObject(shape) for shape in _atomic_shapes(self.selected()))
        result = []
        for key in self.selected_fragment_keys():
            item = self.fragments[key]
            shape = self.fragment(key, self.coordinates.coordinate_space)
            result.extend(_SelectedObject(part, key, item.component_key, item.binding_key) for part in _atomic_shapes(shape))
        return tuple(result)

    def display_objects(self) -> tuple[_SelectedObject, ...]:
        """Preview/Query display traversal; whole never becomes fragments."""
        if self.selection.selection_kind == "whole":
            return (_SelectedObject(self.selected()),)
        return tuple(_SelectedObject(self.fragment(key, self.coordinates.coordinate_space),
                                     key, self.fragments[key].component_key, self.fragments[key].binding_key)
                     for key in self.selected_fragment_keys())

    def close(self) -> None:
        self._selected = None
        self._placed.clear()
        self._parsed.clear()
        self.fragments.clear()


def _atomic_shapes(shape):
    if isinstance(shape, codec.MeshData):
        yield shape
        return
    if shape.ShapeType() in {"Compound", "CompSolid"}:
        iterator = TopoDS_Iterator(shape.wrapped)
        while iterator.More():
            yield from _atomic_shapes(cq.Shape.cast(iterator.Value()))
            iterator.Next()
    else:
        yield shape


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(vector):
    scale = max(abs(x) for x in vector)
    scaled = tuple(x / scale for x in vector)
    length = math.hypot(*scaled)
    return tuple(x / length for x in scaled)


def resolve_plane(plane, offset_m):
    normal = _unit(plane.normal)
    helper = min(((1., 0., 0.), (0., 1., 0.), (0., 0., 1.)), key=lambda axis: abs(_dot(axis, normal)))
    u = _unit(_cross(helper, normal))
    v = _cross(normal, u)
    origin = tuple(p + offset_m * n for p, n in zip(plane.origin_m, normal))
    if not all(math.isfinite(x) for x in origin):
        raise GeometryRuntimeValidationError("resolved section origin exceeds finite coordinate arithmetic")
    return GeometrySectionResolvedPlane(origin_m=origin, unit_normal=normal, u_axis=u, v_axis=v)


def _uv(point, plane):
    vector = tuple(point[i] - plane.origin_m[i] for i in range(3))
    return (_dot(vector, plane.u_axis), _dot(vector, plane.v_axis))


def _native_plane(plane):
    return gp_Pln(gp_Ax3(gp_Pnt(*plane.origin_m), gp_Dir(*plane.unit_normal), gp_Dir(*plane.u_axis)))


def _plane_face(shape, plane):
    bounds = shape.BoundingBox()
    corners = [_uv((x, y, z), plane)
               for x in (bounds.xmin, bounds.xmax)
               for y in (bounds.ymin, bounds.ymax)
               for z in (bounds.zmin, bounds.zmax)]
    umin, umax = min(p[0] for p in corners), max(p[0] for p in corners)
    vmin, vmax = min(p[1] for p in corners), max(p[1] for p in corners)
    padding = max(8e-7, 1e-6 * max(umax - umin, vmax - vmin))
    if not all(math.isfinite(x) for x in (umin, umax, vmin, vmax, padding)):
        raise GeometryRuntimeValidationError("section target projection exceeds finite native coordinates")
    maker = BRepBuilderAPI_MakeFace(_native_plane(plane), umin - padding, umax + padding, vmin - padding, vmax + padding)
    if not maker.IsDone():
        raise GeometryRuntimeValidationError("section coverage plane construction failed")
    return maker.Face()


def _diagnostic(code, message):
    return GeometrySectionDiagnostic(code=code, message=message)


def _drawing_points(adaptor, start, end, plane, kind):
    count = 2 if kind == "LINE" else 65
    points = tuple(_uv(tuple(adaptor.Value(start + (end - start) * i / (count - 1)).Coord()), plane) for i in range(count))
    if any(not math.isfinite(value) for point in points for value in point):
        raise GeometryRuntimeValidationError("native drawing samples exceed finite coordinates")
    return points


def _curve(edge, plane, diagnostics):
    adaptor = BRepAdaptor_Curve(edge.wrapped)
    start, end = adaptor.FirstParameter(), adaptor.LastParameter()
    if edge.wrapped.Orientation() == TopAbs_REVERSED:
        start, end = end, start
    kind = str(edge.geomType())
    values = {"curve_type": kind, "length_m": edge.Length()}
    if kind == "CIRCLE":
        circle = adaptor.Circle()
        values.update(radius_m=circle.Radius(), center_uv_m=_uv(tuple(circle.Location().Coord()), plane))
    elif kind == "ELLIPSE":
        ellipse = adaptor.Ellipse()
        values.update(major_radius_m=ellipse.MajorRadius(), minor_radius_m=ellipse.MinorRadius(), center_uv_m=_uv(tuple(ellipse.Location().Coord()), plane))
    try:
        values["drawing_points_uv_m"] = _drawing_points(adaptor, start, end, plane, kind)
    except Exception as exc:
        diagnostics.append(_diagnostic("BOUNDARY_DRAWING_FAILED", f"Boundary drawing samples are unavailable: {str(exc)[:1800]}; native areas and analytic curve dimensions remain available."))
    return GeometrySectionCurve(**values)


def _region(face, index, plane, diagnostics):
    outer = face.outerWire()
    boundaries = []
    for wire_index, wire in enumerate(face.Wires()):
        explorer = BRepTools_WireExplorer(wire.wrapped, face.wrapped)
        curves = []
        while explorer.More():
            curves.append(_curve(cq.Edge(explorer.Current()), plane, diagnostics))
            explorer.Next()
        if len(curves) != len(wire.Edges()):
            raise GeometryRuntimeValidationError("section boundary traversal is incomplete")
        boundaries.append(GeometrySectionBoundary(
            boundary_index=wire_index, role="outer" if wire.wrapped.IsSame(outer.wrapped) else "hole",
            closed=wire.IsClosed(), length_m=wire.Length(), curves=tuple(curves),
        ))
    return GeometrySectionRegion(region_index=index, area_m2=face.Area(), boundaries=tuple(boundaries))


def _coplanar_boundary(shape, plane):
    for face in shape.Faces():
        surface = BRepAdaptor_Surface(face.wrapped)
        if surface.GetType() != GeomAbs_Plane:
            continue
        native = surface.Plane()
        direction = tuple(native.Axis().Direction().Coord())
        point = tuple(native.Location().Coord())
        if abs(abs(_dot(direction, plane.unit_normal)) - 1) <= 1e-9 and abs(_dot(tuple(point[i] - plane.origin_m[i] for i in range(3)), plane.unit_normal)) <= 1e-7:
            return True
    return False


def _intersection_bounds(shape, plane):
    transform = RigidTransform3D(
        rotation=(plane.u_axis, plane.v_axis, plane.unit_normal),
        translation_m=tuple(-_dot(plane.origin_m, axis) for axis in (plane.u_axis, plane.v_axis, plane.unit_normal)),
    )
    bounds = transform_occt_shape(shape, transform).BoundingBox()
    return (bounds.xmin, bounds.xmax, bounds.ymin, bounds.ymax)


def measure_sections(source: InspectionGeometry, observation):
    """Parse each distinct source once and reuse it for every requested plane."""
    objects = source.objects()
    measurements = []
    for section_index, offset in enumerate(observation.offsets_m):
        plane = resolve_plane(observation.plane, offset)
        measured, projected = [], []
        for object_index, item in enumerate(objects):
            base = dict(object_index=object_index, fragment_key=item.fragment_key,
                        component_key=item.component_key, binding_key=item.binding_key)
            shape = item.shape
            if isinstance(shape, codec.MeshData):
                measured.append(GeometrySectionObjectMeasurement(
                    **base, source_shape_kind="indexed_triangle_mesh", status="unsupported",
                    diagnostics=(_diagnostic("UNSUPPORTED_GEOMETRY_FORMAT", "Exact material regions require BREP solid geometry; existing mesh Query remains available."),),
                ))
                continue
            try:
                operation = BRepAlgoAPI_Common(shape.wrapped, _plane_face(shape, plane))
                operation.Build()
                if not operation.IsDone():
                    raise GeometryRuntimeValidationError("native material-plane common operation did not complete")
                intersection = cq.Shape.cast(operation.Shape())
                faces = intersection.Faces()
                edges, vertices = intersection.Edges(), intersection.Vertices()
                if not faces and not edges and not vertices:
                    # Common reports material.  Boundary Section also observes
                    # pure contact that a zero-area Common may omit.
                    boundary = BRepAlgoAPI_Section(shape.wrapped, _native_plane(plane), False)
                    boundary.Build()
                    if not boundary.IsDone():
                        raise GeometryRuntimeValidationError("native boundary-plane section operation did not complete")
                    intersection = cq.Shape.cast(boundary.Shape())
                    edges, vertices = intersection.Edges(), intersection.Vertices()
                diagnostics = []
                has_solid_material = bool(shape.Solids())
                regions = tuple(_region(face, i, plane, diagnostics) for i, face in enumerate(faces)) if has_solid_material else ()
                intersection_curves = tuple(_curve(edge, plane, diagnostics) for edge in edges) if not faces else ()
                intersection_points = tuple(_uv(vertex.toTuple(), plane) for vertex in vertices) if not faces else ()
                if not faces and not edges and not vertices:
                    status = "empty"
                    diagnostics.append(_diagnostic("EMPTY_SECTION", "No intersection at this exact plane; this alone does not identify a cause or prove missing material."))
                elif not faces:
                    status = "degenerate"
                    diagnostics.append(_diagnostic("TANGENT_OR_DEGENERATE", "Intersection has edges or points but no material face; tangent/degenerate contact needs appropriately chosen neighboring observations."))
                else:
                    status = "computed"
                if _coplanar_boundary(shape, plane):
                    status = "degenerate"
                    diagnostics.append(_diagnostic("COPLANAR_BOUNDARY", "The observation plane coincides with a source boundary plane; distinguish a boundary face from an interior material section."))
                if not has_solid_material:
                    status = "degenerate"
                    diagnostics.append(_diagnostic("NO_SOLID_MATERIAL", "The selected source has no solid interior; surface intersection is not evidence of volume material."))
                if any(not boundary.closed for region in regions for boundary in region.boundaries):
                    diagnostics.append(_diagnostic("OPEN_BOUNDARY", "An intersection boundary is not closed; area and material interpretation need that limitation."))
                if regions:
                    diagnostics.append(_diagnostic("CURVE_SAMPLE_APPROXIMATION", "Drawing curves use finite native-parameter samples; exact areas and analytic radii come from the kernel, not these samples."))
                if edges or vertices:
                    projected.append(_intersection_bounds(intersection, plane))
                measured.append(GeometrySectionObjectMeasurement(
                    **base, source_shape_kind=shape.ShapeType(), status=status,
                    material_regions=regions, material_area_m2=sum(region.area_m2 for region in regions) if has_solid_material else None,
                    intersection_edge_count=len(edges), intersection_vertex_count=len(vertices),
                    intersection_curves=intersection_curves, intersection_points_uv_m=intersection_points,
                    diagnostics=tuple(diagnostics),
                ))
            except Exception as exc:
                measured.append(GeometrySectionObjectMeasurement(
                    **base, source_shape_kind=shape.ShapeType(), status="failed",
                    diagnostics=(_diagnostic("SECTION_OPERATION_FAILED", f"Native section/analysis failed: {str(exc)[:1800]}; retain the exact target and correct the observation or investigate the kernel failure."),),
                ))
        statuses = {item.status for item in measured}
        status = next((s for s in ("failed", "unsupported", "degenerate", "computed", "empty") if s in statuses), "empty")
        bounds = None if not projected else (min(b[0] for b in projected), max(b[1] for b in projected), min(b[2] for b in projected), max(b[3] for b in projected))
        diagnostics = () if len(measured) <= 1 else (_diagnostic("UNION_AREA_NOT_COMPUTED", "Objects remain independent: summed material area includes any overlap and does not measure a fused union or prove gap/contact."),)
        measurements.append(GeometrySectionMeasurement(
            section_index=section_index, offset_m=offset, plane=plane, status=status,
            objects=tuple(measured), projected_bounds_uv_m=bounds,
            material_area_sum_m2=None if any(item.material_area_m2 is None for item in measured) else sum(item.material_area_m2 for item in measured),
            diagnostics=diagnostics,
        ))
    return tuple(measurements)
