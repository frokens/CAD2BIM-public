"""IFC solid expressions from one controlled Geometry construction observation.

The Geometry program remains the only modeling source. Recorded native shapes
provide exact profiles, supports and guards; this module does not run that
program or choose another representation when an operation is unsupported.
"""
from __future__ import annotations

import base64
import io
import math
from typing import Any

import ifcopenshell.util.unit
import numpy as np
from OCP.BRep import BRep_Tool, BRep_Builder
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Section
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeWire
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp, BRepGProp_Face
from OCP.BRepLib import BRepLib_FindSurface
from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
from OCP.Bnd import Bnd_Box
from OCP.GeomAbs import (GeomAbs_Circle, GeomAbs_Line, GeomAbs_Plane,
                         GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Torus)
from OCP.GProp import GProp_GProps
from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
from OCP.ShapeFix import ShapeFix_Shape, ShapeFix_Face
from OCP.TopAbs import (TopAbs_FORWARD, TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX,
                       TopAbs_WIRE, TopAbs_SOLID, TopAbs_SHELL, TopAbs_COMPOUND, TopAbs_COMPSOLID)
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_IndexedMapOfShape, TopTools_HSequenceOfShape
from OCP.TopoDS import TopoDS, TopoDS_Shape, TopoDS_Compound
from OCP.gp import gp_Pln, gp_Pnt, gp_Dir, gp_Vec, gp_Ax3, gp_Trsf

from CAD2BIM_backend.domain.ifc_geometry_construction import GeometryConstruction


class UnsupportedParametricConstruction(ValueError):
    """The selected route has no exact expression for an observed operation."""


def _subshapes(shape: Any, kind: str) -> list[Any]:
    """Keep CadQuery topology semantics without the native map's costly iterator.

    FindKey uses the same deduplicated, ordered map as Shape._entities. Explicit
    indexing avoids the binding's implicit iteration overhead; it does not simplify
    topology or bypass any geometric validation.
    """
    topology, downcast = {
        "Face": (TopAbs_FACE, TopoDS.Face_s),
        "Wire": (TopAbs_WIRE, TopoDS.Wire_s),
        "Edge": (TopAbs_EDGE, TopoDS.Edge_s),
        "Vertex": (TopAbs_VERTEX, TopoDS.Vertex_s),
    }[kind]
    mapped = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, topology, mapped)
    values = (mapped.FindKey(index) for index in range(1, mapped.Extent()+1))
    return [downcast(value) for value in values
            if kind != "Edge" or not BRep_Tool.Degenerated_s(TopoDS.Edge_s(value))]


def _read_shape(encoded: str) -> Any:
    # Construction bytes already belong to the bounded IFC compilation frame;
    # this is a native observation, not a new Geometry asset read boundary.
    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, io.BytesIO(base64.b64decode(encoded, validate=True)), BRep_Builder())
    if shape.IsNull():
        raise UnsupportedParametricConstruction("native shape observation could not be reopened")
    downcast = {TopAbs_VERTEX: TopoDS.Vertex_s, TopAbs_EDGE: TopoDS.Edge_s,
                TopAbs_WIRE: TopoDS.Wire_s, TopAbs_FACE: TopoDS.Face_s,
                TopAbs_SHELL: TopoDS.Shell_s, TopAbs_SOLID: TopoDS.Solid_s,
                TopAbs_COMPSOLID: TopoDS.CompSolid_s, TopAbs_COMPOUND: TopoDS.Compound_s}
    return downcast[shape.ShapeType()](shape)


def _shape_kind(shape: Any) -> str:
    return {TopAbs_VERTEX: "Vertex", TopAbs_EDGE: "Edge", TopAbs_WIRE: "Wire",
            TopAbs_FACE: "Face", TopAbs_SHELL: "Shell", TopAbs_SOLID: "Solid",
            TopAbs_COMPSOLID: "CompSolid", TopAbs_COMPOUND: "Compound"}[shape.ShapeType()]


def _vertex_point(vertex: Any) -> np.ndarray:
    return _xyz(BRep_Tool.Pnt_s(vertex))


def _face_properties(face: Any) -> Any:
    properties = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, properties)
    return properties


def _face_center(face: Any) -> np.ndarray:
    return _xyz(_face_properties(face).CentreOfMass())


def _face_area(face: Any) -> float:
    return _face_properties(face).Mass()


def _face_normal(face: Any) -> np.ndarray:
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    point, vector = gp_Pnt(), gp_Vec()
    BRepGProp_Face(face).Normal((u0+u1)*.5, (v0+v1)*.5, point, vector)
    vector.Normalize()
    return _unit(vector)


def _bounds(shape: Any) -> tuple[np.ndarray, np.ndarray, float]:
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box)
    coordinates = box.Get()
    return np.asarray(coordinates[:3]), np.asarray(coordinates[3:]), box.SquareExtent()**.5


def _face_from_wires(outer: Any, inners: list[Any] | tuple[Any, ...] = ()) -> Any:
    """Use the original planar-profile checks and OCCT orientation repair."""
    if inners and not BRep_Tool.IsClosed_s(outer):
        raise UnsupportedParametricConstruction("source profile outer wire is not closed")
    compound, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(compound)
    for wire in (outer, *inners):
        builder.Add(compound, wire)
    if not BRepLib_FindSurface(compound, OnlyPlane=True).Found():
        raise UnsupportedParametricConstruction("source profile wires are not planar")
    repair = ShapeFix_Shape(outer)
    repair.Perform()
    operation = BRepBuilderAPI_MakeFace(TopoDS.Wire_s(repair.Shape()), True)
    for wire in inners:
        if not BRep_Tool.IsClosed_s(wire):
            raise UnsupportedParametricConstruction("source profile inner wire is not closed")
        operation.Add(wire)
    operation.Build()
    if not operation.IsDone():
        raise UnsupportedParametricConstruction(f"source planar profile failed: {operation.Error()}")
    face_repair = ShapeFix_Face(operation.Face())
    face_repair.FixOrientation()
    face_repair.Perform()
    return TopoDS.Face_s(face_repair.Result())


def _combined_wires(edges: list[Any]) -> list[Any]:
    native_edges, native_wires = TopTools_HSequenceOfShape(), TopTools_HSequenceOfShape()
    for edge in edges:
        native_edges.Append(edge)
    ShapeAnalysis_FreeBounds.ConnectEdgesToWires_s(native_edges, 1e-9, False, native_wires)
    return [TopoDS.Wire_s(native_wires.Value(index)) for index in range(1, native_wires.Length()+1)]


def _xyz(value: Any) -> np.ndarray:
    if hasattr(value, "X"):
        return np.asarray((value.X(), value.Y(), value.Z()), dtype=float)
    return np.asarray(value, dtype=float)


def _unit(value: Any) -> np.ndarray:
    vector = _xyz(value)
    length = np.linalg.norm(vector)
    if length == 0 or not np.isfinite(length):
        raise UnsupportedParametricConstruction("invalid native direction")
    return vector / length


def _reference(axis: Any) -> np.ndarray:
    normal = _unit(axis)
    seed = np.array((0., 0., 1.))
    seed -= normal * np.dot(seed, normal)
    if np.linalg.norm(seed) < 1e-8:
        seed = np.array((1., 0., 0.))
        seed -= normal * np.dot(seed, normal)
    return _unit(seed)


def _rotation(start: Any, end: Any, degrees: float) -> np.ndarray:
    first, last = _xyz(start), _xyz(end)
    axis = _unit(last-first)
    x, y, z = axis
    skew = np.array(((0., -z, y), (z, 0., -x), (-y, x, 0.)))
    angle = math.radians(float(degrees))
    result = np.eye(4)
    result[:3, :3] = (np.eye(3)*math.cos(angle)
                      +(1-math.cos(angle))*np.outer(axis, axis)
                      +math.sin(angle)*skew)
    result[:3, 3] = first-result[:3, :3]@first
    return result


class _Encoder:
    def __init__(self, model: Any, construction: GeometryConstruction,
                 project_length_unit_m: float) -> None:
        self.model = model
        self.nodes = {node.id: node for node in construction.nodes}
        self.root = construction.root
        self.unit = project_length_unit_m
        self.angle_unit = float(ifcopenshell.util.unit.calculate_unit_scale(model, "PLANEANGLEUNIT"))
        if not math.isfinite(self.angle_unit) or self.angle_unit <= 0:
            raise UnsupportedParametricConstruction("invalid project plane angle unit")
        self._shapes: dict[int, Any] = {}

    def _plane_angle(self, radians: float) -> float:
        return float(radians) / self.angle_unit

    def point(self, value: Any) -> Any:
        return self.model.create_entity("IfcCartesianPoint",
            tuple(float(x)/self.unit for x in value))

    def direction(self, value: Any) -> Any:
        return self.model.create_entity("IfcDirection", tuple(float(x) for x in value))

    def place(self, origin: Any, normal: Any = (0., 0., 1.), x: Any = (1., 0., 0.)) -> Any:
        return self.model.create_entity("IfcAxis2Placement3D", self.point(origin),
            self.direction(_unit(normal)), self.direction(_unit(x)))

    def _shape(self, node_id: int) -> Any:
        if node_id not in self._shapes:
            encoded = self.nodes[node_id].shape_brep
            if encoded is None:
                raise UnsupportedParametricConstruction("required native result has no exact shape snapshot")
            self._shapes[node_id] = _read_shape(encoded)
        return self._shapes[node_id]

    @staticmethod
    def _import_shape(value: dict[str, Any]) -> Any:
        if set(value) != {"$shape_brep", "$shape_type"}:
            raise UnsupportedParametricConstruction("invalid native shape observation")
        shape = _read_shape(value["$shape_brep"])
        if _shape_kind(shape) != value["$shape_type"]:
            raise UnsupportedParametricConstruction("native shape observation type changed")
        return shape

    def _value(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._value(v) for v in value]
        if isinstance(value, dict):
            if set(value) == {"$ref"}:
                return self._shape(value["$ref"])
            if set(value) == {"$vector"}:
                return tuple(value["$vector"])
            if "$shape_brep" in value:
                return self._import_shape(value)
            return {key: self._value(item) for key, item in value.items()}
        return value

    def _values(self, node: Any) -> tuple[list[Any], dict[str, Any]]:
        return ([self._value(v) for v in node.args],
                {key: self._value(v) for key, v in node.kwargs.items()
                 if not key.startswith("$observed_") and key != "$receiver_state"})

    def _position(self, value: Any, transform: np.ndarray) -> np.ndarray:
        return transform[:3, :3]@_xyz(value)+transform[:3, 3]

    def _normal(self, value: Any, transform: np.ndarray) -> np.ndarray:
        return _unit(transform[:3, :3]@_xyz(value))

    def _length(self, value: float, transform: np.ndarray) -> float:
        axes = [np.linalg.norm(transform[:3, i]) for i in range(3)]
        if max(axes)-min(axes) > 1e-9*max(axes):
            raise UnsupportedParametricConstruction("nonuniform transform has no exact solid projection")
        return float(float(value)*axes[0]/self.unit)

    def _circle(self, radius: float, thickness: float | None, transform: np.ndarray) -> Any:
        r = self._length(radius, transform)
        if thickness is None:
            return self.model.create_entity("IfcCircleProfileDef", "AREA", None, None, r)
        return self.model.create_entity("IfcCircleHollowProfileDef", "AREA", None, None,
                                        r, self._length(thickness, transform))

    def _wire(self, wire: Any, transform: np.ndarray, frame: tuple[np.ndarray, np.ndarray]) -> Any:
        curves = []
        walk = BRepTools_WireExplorer(wire)
        while walk.More():
            edge = walk.Current()
            adaptor = BRepAdaptor_Curve(edge)
            first, last = adaptor.FirstParameter(), adaptor.LastParameter()
            def local(value):
                return (self._position((value.X(), value.Y(), value.Z()), transform)-frame[0])@frame[1]
            if adaptor.GetType() == GeomAbs_Line:
                curve = self.model.create_entity("IfcPolyline", [
                    self.point(local(adaptor.Value(first))[:2]),
                    self.point(local(adaptor.Value(last))[:2]),
                ])
            elif adaptor.GetType() == GeomAbs_Circle:
                circle = adaptor.Circle()
                center = local(circle.Location())[:2]
                direction = self._normal((circle.Position().XDirection().X(),
                    circle.Position().XDirection().Y(), circle.Position().XDirection().Z()), transform)
                local_x = direction@frame[1]
                circle_normal = self._normal(_xyz(circle.Axis().Direction()), transform)
                orientation = float(np.dot(circle_normal, frame[1][:, 2]))
                if abs(abs(orientation)-1.) > 1e-8:
                    raise UnsupportedParametricConstruction("profile circle leaves its observed plane")
                place = self.model.create_entity("IfcAxis2Placement2D", self.point(center),
                    self.direction(local_x[:2]))
                basis = self.model.create_entity("IfcCircle", place,
                    self._length(circle.Radius(), transform))
                if abs(abs(last-first)-2*math.pi) < 1e-10:
                    curve = basis
                else:
                    curve = self.model.create_entity("IfcTrimmedCurve", basis,
                        [self.point(local(adaptor.Value(first))[:2])],
                        [self.point(local(adaptor.Value(last))[:2])],
                        orientation > 0., "CARTESIAN")
            else:
                raise UnsupportedParametricConstruction(f"unsupported exact profile edge kind {adaptor.GetType()}")
            curves.append((curve, edge.Orientation() == TopAbs_FORWARD))
            walk.Next()
        if not curves:
            raise UnsupportedParametricConstruction("empty source profile wire")
        if len(curves) == 1 and curves[0][1] and curves[0][0].is_a("IfcCircle"):
            return curves[0][0]
        return self.model.create_entity("IfcCompositeCurve", [
            self.model.create_entity("IfcCompositeCurveSegment", "CONTINUOUS", sense, curve)
            for curve, sense in curves], False)

    @staticmethod
    def _complete_circle(wire: Any) -> tuple[np.ndarray, float] | None:
        edges = [BRepAdaptor_Curve(edge) for edge in _subshapes(wire, "Edge")]
        if not edges or any(edge.GetType() != GeomAbs_Circle for edge in edges):
            return None
        center = _xyz(edges[0].Circle().Location())
        radius = edges[0].Circle().Radius()
        if (any(np.linalg.norm(_xyz(edge.Circle().Location())-center) > 1e-10
                or abs(edge.Circle().Radius()-radius) > 1e-10 for edge in edges)
                or abs(sum(abs(edge.LastParameter()-edge.FirstParameter()) for edge in edges)-2*math.pi) > 1e-8):
            return None
        return center, radius

    def _profile(self, face: Any, transform: np.ndarray) -> tuple[Any, Any, tuple[np.ndarray, np.ndarray]]:
        origin = _face_center(face)
        normal = _face_normal(face)
        outer_wire = BRepTools.OuterWire_s(face)
        outer_circle = self._complete_circle(outer_wire)
        if outer_circle is not None:
            # A fitted face plane can perturb the circle's support normal by an
            # ulp. A distant revolution axis amplifies that into a false plane
            # mismatch. Keep the exact source curve support and face orientation.
            circle = BRepAdaptor_Curve(_subshapes(outer_wire, "Edge")[0]).Circle()
            support_normal = _xyz(circle.Axis().Direction())
            normal = support_normal if np.dot(support_normal, normal) >= 0 else -support_normal
        x = _reference(normal)
        axes = np.column_stack((x, np.cross(normal, x), normal))
        target_origin = self._position(origin, transform)
        target_axes = transform[:3, :3]@axes
        target_axes /= np.linalg.norm(target_axes, axis=0)
        frame = target_origin, target_axes
        inners = [wire for wire in _subshapes(face, "Wire") if not wire.IsSame(outer_wire)]
        inner_circle = self._complete_circle(inners[0]) if len(inners) == 1 else None
        if outer_circle is not None and (not inners or inner_circle is not None
                and np.linalg.norm(outer_circle[0]-inner_circle[0]) < 1e-10):
            center, radius = outer_circle
            local_center = (self._position(center, transform)-target_origin)@target_axes
            position = self.model.create_entity("IfcAxis2Placement2D",
                self.point(local_center[:2]), None)
            if inner_circle is None:
                profile = self.model.create_entity("IfcCircleProfileDef", "AREA", None,
                    position, self._length(radius, transform))
            else:
                profile = self.model.create_entity("IfcCircleHollowProfileDef", "AREA", None,
                    position, self._length(radius, transform),
                    self._length(radius-inner_circle[1], transform))
        else:
            outer = self._wire(outer_wire, transform, frame)
            inner = [self._wire(wire, transform, frame) for wire in inners]
            profile = (self.model.create_entity("IfcArbitraryProfileDefWithVoids", "AREA", None, outer, inner)
                       if inner else self.model.create_entity("IfcArbitraryClosedProfileDef", "AREA", None, outer))
        return profile, self.place(target_origin, target_axes[:, 2], target_axes[:, 0]), frame

    def _convex(self, shape: Any, transform: np.ndarray) -> Any:
        faces = _subshapes(shape, "Face")
        if not faces or any(BRepAdaptor_Surface(face).GetType() != GeomAbs_Plane for face in faces):
            raise UnsupportedParametricConstruction("source box/chamfer is not all planar")
        vertices = [_vertex_point(vertex) for vertex in _subshapes(shape, "Vertex")]
        low, high, extent = _bounds(shape)
        dimensions = high-low
        normals = []
        for face in faces:
            origin, normal = _face_center(face), _face_normal(face)
            if max(np.dot(point-origin, normal) for point in vertices) > max(1e-8*extent, 1e-10):
                raise UnsupportedParametricConstruction("source planar solid is not convex")
            normals.append((origin, -normal))
        lower = low-extent
        scale = self._length(1., transform)*self.unit
        points = ((0., 0.), (dimensions[0]+2*extent, 0.),
                  (dimensions[0]+2*extent, dimensions[1]+2*extent), (0., dimensions[1]+2*extent))
        polyline = self.model.create_entity("IfcPolyline",
            [self.point((x*scale, y*scale)) for x, y in (*points, points[0])])
        profile = self.model.create_entity("IfcArbitraryClosedProfileDef", "AREA", None, polyline)
        item = self.model.create_entity("IfcExtrudedAreaSolid", profile,
            self.place(self._position(lower, transform), self._normal((0., 0., 1.), transform),
                self._normal((1., 0., 0.), transform)),
            self.direction((0., 0., 1.)), self._length(dimensions[2]+2*extent, transform))
        for origin, normal in normals:
            plane = self.model.create_entity("IfcPlane", self.place(
                self._position(origin, transform), self._normal(normal, transform),
                _reference(self._normal(normal, transform))))
            half = self.model.create_entity("IfcHalfSpaceSolid", plane, True)
            item = self.model.create_entity("IfcBooleanClippingResult", "DIFFERENCE", item, half)
        return item

    def _block(self, shape: Any, transform: np.ndarray) -> Any:
        """Recognize an actual orthogonal box, including its observed Location."""
        if shape.ShapeType() != TopAbs_SOLID or not BRepCheck_Analyzer(shape).IsValid():
            raise UnsupportedParametricConstruction("source box is not one valid solid")
        faces, vertices, edges = (_subshapes(shape, kind) for kind in ("Face", "Vertex", "Edge"))
        if len(faces) != 6 or len(vertices) != 8 or len(edges) != 12:
            raise UnsupportedParametricConstruction("source is not a six-face/eight-corner box")
        face_vertices = []
        for face in faces:
            if BRepAdaptor_Surface(face).GetType() != GeomAbs_Plane or len(_subshapes(face, "Wire")) != 1:
                raise UnsupportedParametricConstruction("source is not a six-face/eight-corner box")
            corners = _subshapes(face, "Vertex")
            if len(corners) != 4:
                raise UnsupportedParametricConstruction("source is not a six-face/eight-corner box")
            face_vertices.append(corners)
        face_normals = [_face_normal(face) for face in faces]
        x = face_normals[0]
        y = next((normal for normal in face_normals
                  if abs(np.dot(normal, x)) < 1e-10), None)
        if y is None:
            raise UnsupportedParametricConstruction("source box lacks a second orthogonal axis")
        z = _unit(np.cross(x, y))
        axes = np.column_stack((x, y, z))
        projected = np.asarray([_vertex_point(vertex)@axes for vertex in vertices])
        low, high = projected.min(axis=0), projected.max(axis=0)
        dimensions = high-low
        extent = float(max(dimensions))
        tolerance = max(1e-8*extent, 1e-10)
        if any(length <= tolerance for length in dimensions):
            raise UnsupportedParametricConstruction("source box has a zero-width axis")
        corners = set()
        for point in projected:
            bits = []
            for coordinate, first, last in zip(point, low, high):
                if abs(coordinate-first) <= tolerance:
                    bits.append(0)
                elif abs(coordinate-last) <= tolerance:
                    bits.append(1)
                else:
                    raise UnsupportedParametricConstruction("source has a non-corner vertex")
            corners.add(tuple(bits))
        if len(corners) != 8:
            raise UnsupportedParametricConstruction("source box corners are not distinct")
        side_counts = {(index, side): 0 for index in range(3) for side in (-1, 1)}
        for face, face_corners, face_normal in zip(faces, face_vertices, face_normals):
            normal = face_normal@axes
            index = int(np.argmax(np.abs(normal)))
            if abs(abs(normal[index])-1.) > 1e-9:
                raise UnsupportedParametricConstruction("source box faces are not axis-normal")
            side = 1 if normal[index] > 0 else -1
            side_counts[index, side] += 1
            expected = high[index] if side > 0 else low[index]
            face_coordinates = np.asarray([_vertex_point(v)@axes for v in face_corners])
            if (np.max(np.abs(face_coordinates[:, index]-expected)) > tolerance
                    or abs(_face_area(face)-dimensions[(index+1)%3]*dimensions[(index+2)%3])
                       > max(1e-8*extent*extent, 1e-14)):
                raise UnsupportedParametricConstruction("source box face does not fill its corner plane")
        if any(count != 1 for count in side_counts.values()):
            raise UnsupportedParametricConstruction("source box does not have three opposite face pairs")
        origin = self._position(axes@low, transform)
        normal, reference = self._normal(z, transform), self._normal(x, transform)
        block = self.model.create_entity("IfcBlock", self.place(origin, normal, reference),
            *(self._length(length, transform) for length in dimensions))
        return self.model.create_entity("IfcCsgSolid", block)

    def _combine(self, operation: str, items: list[Any]) -> Any:
        if not items:
            raise UnsupportedParametricConstruction("empty construction operation")
        result = items[0]
        for item in items[1:]:
            result = self.model.create_entity("IfcBooleanResult", operation, result, item)
        return result

    def _clip(self, item: Any, origin: Any, kept_normal: Any) -> Any:
        normal = _unit(kept_normal)
        plane = self.model.create_entity("IfcPlane", self.place(origin, normal, _reference(normal)))
        half = self.model.create_entity("IfcHalfSpaceSolid", plane, True)
        return self.model.create_entity("IfcBooleanClippingResult", "DIFFERENCE", item, half)

    def _revolve_face(self, face: Any, angle_degrees: float, axis_start: Any,
                      axis_end: Any, transform: np.ndarray) -> Any:
        angle = float(angle_degrees)
        first = self._position(axis_start, transform)
        direction = _unit(self._position(axis_end, transform)-first)
        profile, position, frame = self._profile(face, transform)
        local_origin = (first-frame[0])@frame[1]
        local_axis = direction@frame[1]
        _, _, diagonal = _bounds(face)
        extent = diagonal*self._length(1., transform)*self.unit
        if abs(local_origin[2]) > max(1e-8*extent, 1e-10) or abs(local_axis[2]) > 1e-8:
            raise UnsupportedParametricConstruction("source revolution axis leaves actual profile plane")
        local_origin[2] = local_axis[2] = 0.
        axis = self.model.create_entity("IfcAxis1Placement", self.point(local_origin),
                                         self.direction(local_axis))
        if not 0 < angle <= 360:
            raise UnsupportedParametricConstruction("source revolution angle is outside exact supported range")
        return self.model.create_entity("IfcRevolvedAreaSolid", profile, position,
                                        axis, self._plane_angle(math.radians(angle)))

    def _polygon_loft(self, wires: list[Any], transform: np.ndarray) -> Any:
        if len(wires) < 2 or any(wire.ShapeType() != TopAbs_WIRE for wire in wires):
            raise UnsupportedParametricConstruction("loft requires at least two exact source wires")
        if any(BRepAdaptor_Curve(edge).GetType() != GeomAbs_Line
               for wire in wires for edge in _subshapes(wire, "Edge")):
            raise UnsupportedParametricConstruction("loft has a nonpolygon source profile")
        faces = [_face_from_wires(wire) for wire in wires]
        parts = []
        for bottom, top in zip(faces, faces[1:]):
            direction = _face_center(top)-_face_center(bottom)
            if (abs(abs(np.dot(_face_normal(bottom), _face_normal(top)))-1) > 1e-8
                    or abs(abs(np.dot(_face_normal(bottom), _unit(direction)))-1) > 1e-8):
                raise UnsupportedParametricConstruction("ruled loft profile planes are not parallel")
            profile, position, frame = self._profile(bottom, transform)
            ratio = math.sqrt(_face_area(top)/_face_area(bottom))
            first = np.array(sorted(tuple(np.round(
                ((self._position(_vertex_point(vertex), transform)-frame[0])@frame[1])[:2]*ratio, 12))
                for vertex in _subshapes(bottom, "Vertex")))
            top_origin = self._position(_face_center(top), transform)
            second = np.array(sorted(tuple(np.round(
                ((self._position(_vertex_point(vertex), transform)-top_origin)@frame[1])[:2], 12))
                for vertex in _subshapes(top, "Vertex")))
            if first.shape != second.shape or not np.array_equal(first, second):
                raise UnsupportedParametricConstruction("ruled polygon profiles are not homothetic")
            operator = self.model.create_entity("IfcCartesianTransformationOperator2D", None, None,
                self.model.create_entity("IfcCartesianPoint", (0., 0.)), float(ratio))
            end_profile = self.model.create_entity("IfcDerivedProfileDef", "AREA", None,
                                                   profile, operator, None)
            depth = self._length(np.linalg.norm(direction), transform)
            local_direction = _unit(self._normal(direction, transform)@frame[1])
            parts.append(self.model.create_entity("IfcExtrudedAreaSolidTapered", profile, position,
                self.direction(local_direction), depth, end_profile))
        return self._combine("UNION", parts)

    def _circular_path_sweep(self, path: Any, radius: float,
                             inner_radius: float | None, transform: np.ndarray) -> Any:
        if path.ShapeType() != TopAbs_WIRE:
            raise UnsupportedParametricConstruction("source sweep directrix is not an exact Wire")
        parts = []
        walk = BRepTools_WireExplorer(path)
        while walk.More():
            edge = walk.Current()
            curve = BRepAdaptor_Curve(edge)
            first_parameter, last_parameter = curve.FirstParameter(), curve.LastParameter()
            forward = edge.Orientation() == TopAbs_FORWARD
            if not forward:
                first_parameter, last_parameter = last_parameter, first_parameter
            first = self._position(_xyz(curve.Value(first_parameter)), transform)
            last = self._position(_xyz(curve.Value(last_parameter)), transform)
            profile = self._circle(radius,
                None if inner_radius is None else radius-inner_radius, transform)
            if curve.GetType() == GeomAbs_Line:
                normal = _unit(last-first)
                parts.append(self.model.create_entity("IfcExtrudedAreaSolid", profile,
                    self.place(first, normal, _reference(normal)), self.direction((0., 0., 1.)),
                    np.linalg.norm(last-first)/self.unit))
            elif curve.GetType() == GeomAbs_Circle:
                circle = curve.Circle()
                axis_origin = self._position(_xyz(circle.Location()), transform)
                axis_direction = self._normal(_xyz(circle.Axis().Direction()), transform)
                if not forward:
                    axis_direction = -axis_direction
                tangent = _unit(np.cross(axis_direction, first-axis_origin))
                radial = _reference(tangent)
                axes = np.column_stack((radial, np.cross(tangent, radial), tangent))
                local_origin = (axis_origin-first)@axes
                local_axis = axis_direction@axes
                if abs(local_origin[2]) > 1e-8 or abs(local_axis[2]) > 1e-8:
                    raise UnsupportedParametricConstruction("circular sweep axis leaves profile plane")
                local_origin[2] = local_axis[2] = 0.
                axis = self.model.create_entity("IfcAxis1Placement", self.point(local_origin),
                                                self.direction(local_axis))
                item = self.model.create_entity("IfcRevolvedAreaSolid", profile,
                    self.place(first, tangent, radial), axis, self._plane_angle(2*math.pi))
                angle = abs(last_parameter-first_parameter)
                if 0 < angle < math.pi:
                    end_normal = _rotation(axis_origin, axis_origin+axis_direction,
                                           math.degrees(angle))[:3, :3]@tangent
                    item = self._clip(self._clip(item, axis_origin, tangent),
                                      axis_origin, -end_normal)
                elif abs(angle-2*math.pi) > 1e-10:
                    raise UnsupportedParametricConstruction("circular sweep arc has unsupported angle")
                parts.append(item)
            else:
                raise UnsupportedParametricConstruction("source sweep has a non-line/non-circle directrix")
            walk.Next()
        return self._combine("UNION", parts)

    def _axisymmetric_solid(self, shape: Any, transform: np.ndarray) -> Any:
        axis = None
        faces = _subshapes(shape, "Face")
        for face in faces:
            adaptor = BRepAdaptor_Surface(face)
            kind = adaptor.GetType()
            if kind == GeomAbs_Plane:
                continue
            surface = (adaptor.Cylinder() if kind == GeomAbs_Cylinder else
                       adaptor.Cone() if kind == GeomAbs_Cone else
                       adaptor.Torus() if kind == GeomAbs_Torus else None)
            if surface is None:
                raise UnsupportedParametricConstruction("fillet has an unsupported actual support surface")
            candidate = _xyz(surface.Axis().Location()), _unit(surface.Axis().Direction())
            if axis is None:
                axis = candidate
            elif (np.linalg.norm(np.cross(axis[1], candidate[1])) > 1e-9
                    or np.linalg.norm(np.cross(candidate[0]-axis[0], axis[1])) > 1e-8):
                raise UnsupportedParametricConstruction("fillet source has multiple rotation axes")
        if axis is None:
            raise UnsupportedParametricConstruction("fillet source has no rotation axis")
        origin, direction = axis
        for face in faces:
            if (BRepAdaptor_Surface(face).GetType() == GeomAbs_Plane
                    and abs(abs(np.dot(_face_normal(face), direction))-1) > 1e-8):
                raise UnsupportedParametricConstruction("fillet source has a nonaxial planar support")
        radial = _reference(direction)
        normal = np.cross(direction, radial)
        section = BRepAlgoAPI_Section(shape, gp_Pln(gp_Pnt(*origin), gp_Dir(*normal)), False)
        section.SetRunParallel(False)
        section.Build()
        wires = _combined_wires(_subshapes(section.Shape(), "Edge"))
        if not wires:
            raise UnsupportedParametricConstruction("axisymmetric source has no exact section")
        _, _, diagonal = _bounds(shape)
        extent = diagonal*2
        coordinates = [np.dot(_vertex_point(vertex)-origin, direction)
                       for vertex in _subshapes(shape, "Vertex")]
        low, high = min(coordinates)-extent, max(coordinates)+extent
        polygon = BRepBuilderAPI_MakePolygon()
        plane_to_world = gp_Trsf()
        plane_to_world.SetTransformation(
            gp_Ax3(gp_Pnt(*origin), gp_Dir(*normal), gp_Dir(*direction)), gp_Ax3())
        for x, y in ((low, 0.), (high, 0.), (high, extent), (low, extent)):
            polygon.Add(gp_Pnt(x, y, 0.).Transformed(plane_to_world))
        polygon.Close()
        if not polygon.IsDone():
            raise UnsupportedParametricConstruction("axisymmetric section clipping wire failed")
        clip_face = _face_from_wires(polygon.Wire())
        parts = []
        for wire in wires:
            common = BRepAlgoAPI_Common(_face_from_wires(wire), clip_face)
            common.SetRunParallel(False)
            common.Build()
            if not common.IsDone():
                raise UnsupportedParametricConstruction("axisymmetric section clipping failed")
            clipped = common.Shape()
            for face in _subshapes(clipped, "Face"):
                if _face_area(face) <= 1e-18:
                    continue
                profile, position, frame = self._profile(face, transform)
                local_origin = (self._position(origin, transform)-frame[0])@frame[1]
                local_axis = self._normal(direction, transform)@frame[1]
                if abs(local_origin[2]) > 1e-8 or abs(local_axis[2]) > 1e-8:
                    raise UnsupportedParametricConstruction("section revolution axis leaves profile plane")
                local_origin[2] = local_axis[2] = 0.
                axis_entity = self.model.create_entity("IfcAxis1Placement", self.point(local_origin),
                                                        self.direction(local_axis))
                parts.append(self.model.create_entity("IfcRevolvedAreaSolid", profile, position,
                                                      axis_entity, self._plane_angle(2*math.pi)))
        return self._combine("UNION", parts)

    def _profile_from_state(self, node: Any, transform: np.ndarray) -> tuple[Any, Any, tuple[np.ndarray, np.ndarray]]:
        state = node.kwargs.get("$receiver_state")
        if not isinstance(state, dict) or not isinstance(state.get("pending_wires"), list):
            raise UnsupportedParametricConstruction("Workplane source profile was not observed")
        wires = [self._import_shape(value) for value in state["pending_wires"]]
        if not wires or any(wire.ShapeType() != TopAbs_WIRE for wire in wires):
            raise UnsupportedParametricConstruction("Workplane pending profile is not an exact Wire set")
        return self._profile(_face_from_wires(wires[0], wires[1:]), transform)

    def _encode(self, node_id: int, transform: np.ndarray) -> list[Any]:
        node = self.nodes[node_id]
        op = node.op
        if op in ("val", "clean", "copy") and node.receiver is not None:
            return self._encode(node.receiver, transform)
        if op == "Compound.makeCompound":
            refs = [value["$ref"] for value in node.args[0] if isinstance(value, dict) and set(value) == {"$ref"}]
            if len(refs) != len(node.args[0]):
                raise UnsupportedParametricConstruction("compound contains a non-observed source")
            return [item for ref in refs for item in self._encode(ref, transform)]
        if op in ("translate", "rotate", "scale") and node.receiver is not None:
            args, _ = self._values(node)
            changed = np.eye(4)
            if op == "translate":
                changed[:3, 3] = _xyz(args[0])
            elif op == "rotate":
                changed = _rotation(args[0], args[1], args[2])
            else:
                changed[:3, :3] *= float(args[0])
            return self._encode(node.receiver, transform@changed)
        if op in ("cut", "fuse", "intersect", "union"):
            refs = list(node.children)
            if len(refs) < 2:
                raise UnsupportedParametricConstruction("boolean lacks observed operands")
            encoded = [self._encode(ref, transform) for ref in refs]
            if any(len(items) != 1 for items in encoded):
                raise UnsupportedParametricConstruction("boolean has a multisolid operand")
            return [self._combine({"cut": "DIFFERENCE", "fuse": "UNION",
                "union": "UNION", "intersect": "INTERSECTION"}[op], [items[0] for items in encoded])]
        if op in ("box", "Solid.makeBox"):
            shape = self._shape(node_id)
            try:
                return [self._block(shape, transform)]
            except UnsupportedParametricConstruction:
                return [self._convex(shape, transform)]
        if op == "chamfer":
            return [self._convex(self._shape(node_id), transform)]
        if op == "fillet":
            return [self._axisymmetric_solid(self._shape(node_id), transform)]
        args, kwargs = self._values(node)
        if op == "Solid.makeCylinder":
            radius, height = args[:2]
            origin = args[2] if len(args) > 2 else kwargs.get("pnt", (0., 0., 0.))
            direction = args[3] if len(args) > 3 else kwargs.get("dir", (0., 0., 1.))
            if kwargs.get("angleDegrees", args[4] if len(args) > 4 else 360) != 360:
                raise UnsupportedParametricConstruction("partial source cylinder unsupported")
            normal = self._normal(direction, transform)
            return [self.model.create_entity("IfcExtrudedAreaSolid",
                self._circle(radius, None, transform),
                self.place(self._position(origin, transform), normal, _reference(normal)),
                self.direction((0., 0., 1.)), self._length(height, transform))]
        if op == "Solid.makeCone":
            r1, r2, height = map(float, args[:3])
            origin = args[3] if len(args) > 3 else kwargs.get("pnt", (0., 0., 0.))
            direction = args[4] if len(args) > 4 else kwargs.get("dir", (0., 0., 1.))
            normal = self._normal(direction, transform)
            radial = _reference(normal)
            length = self._length(height, transform)
            profile_points = ((0., 0.), (length, 0.),
                              (length, self._length(r2, transform)),
                              (0., self._length(r1, transform)))
            polyline = self.model.create_entity("IfcPolyline", [
                self.model.create_entity("IfcCartesianPoint", tuple(point))
                for point in (*profile_points, profile_points[0])])
            profile = self.model.create_entity("IfcArbitraryClosedProfileDef", "AREA", None, polyline)
            position = self.place(self._position(origin, transform), np.cross(normal, radial), normal)
            axis = self.model.create_entity("IfcAxis1Placement",
                self.model.create_entity("IfcCartesianPoint", (0., 0., 0.)), self.direction((1., 0., 0.)))
            return [self.model.create_entity("IfcRevolvedAreaSolid", profile, position, axis,
                                             self._plane_angle(2*math.pi))]
        if op == "Solid.makeSphere":
            radius = args[0]
            origin = args[1] if len(args) > 1 else kwargs.get("pnt", (0., 0., 0.))
            first = kwargs.get("angleDegrees1", args[3] if len(args) > 3 else -90)
            last = kwargs.get("angleDegrees2", args[4] if len(args) > 4 else 90)
            if first != -90 or last != 90:
                raise UnsupportedParametricConstruction("partial source sphere unsupported")
            return [self.model.create_entity("IfcSphere", self.place(self._position(origin, transform)),
                                             self._length(radius, transform))]
        if op == "Solid.makeTorus":
            major, minor = map(float, args[:2])
            origin = args[2] if len(args) > 2 else kwargs.get("pnt", (0., 0., 0.))
            direction = args[3] if len(args) > 3 else kwargs.get("dir", (0., 0., 1.))
            axis_direction = self._normal(direction, transform)
            radial = _reference(axis_direction)
            circle_position = self.model.create_entity("IfcAxis2Placement2D",
                self.model.create_entity("IfcCartesianPoint", (self._length(major, transform), 0.)), None)
            profile = self._circle(minor, None, transform)
            profile.Position = circle_position
            position = self.place(self._position(origin, transform),
                                  np.cross(radial, axis_direction), radial)
            axis = self.model.create_entity("IfcAxis1Placement",
                self.model.create_entity("IfcCartesianPoint", (0., 0., 0.)), self.direction((0., 1., 0.)))
            return [self.model.create_entity("IfcRevolvedAreaSolid", profile, position, axis,
                                             self._plane_angle(2*math.pi))]
        if op == "Solid.revolve":
            if len(args) < 5 or args[0].ShapeType() != TopAbs_WIRE:
                raise UnsupportedParametricConstruction("Solid.revolve lacks its exact Wire and axis")
            face = _face_from_wires(args[0], args[1])
            return [self._revolve_face(face, args[2], args[3], args[4], transform)]
        if op == "Solid.makeLoft":
            ruled = args[1] if len(args) > 1 else kwargs.get("ruled", False)
            if not ruled:
                raise UnsupportedParametricConstruction("smooth source loft lacks an exact supported expression")
            return [self._polygon_loft(args[0], transform)]
        if op == "extrude":
            if node.receiver is None:
                raise UnsupportedParametricConstruction("extrude has no observed Workplane")
            if kwargs.get("both", False) or kwargs.get("taper", 0) != 0:
                raise UnsupportedParametricConstruction("source extrude uses an unsupported both/taper mode")
            profile, position, frame = self._profile_from_state(node, transform)
            state = node.kwargs["$receiver_state"]
            plane = state.get("plane")
            if not isinstance(plane, dict) or set(plane) != {"$plane"}:
                raise UnsupportedParametricConstruction("source Workplane extrusion plane was not observed")
            direction = self._normal(plane["$plane"]["z_dir"], transform)
            distance = float(args[0] if args else kwargs["until"])
            if distance < 0:
                direction = -direction
            return [self.model.create_entity("IfcExtrudedAreaSolid", profile, position,
                self.direction(direction@frame[1]), self._length(abs(distance), transform))]
        if op == "revolve":
            state = node.kwargs.get("$receiver_state")
            if not isinstance(state, dict) or "revolve_axis_world" not in state:
                raise UnsupportedParametricConstruction("source Workplane revolution axis was not observed")
            wires = [self._import_shape(value) for value in state.get("pending_wires", ())]
            if not wires or any(wire.ShapeType() != TopAbs_WIRE for wire in wires):
                raise UnsupportedParametricConstruction("source revolution profile has no exact Wire set")
            face = _face_from_wires(wires[0], wires[1:])
            first, last = state["revolve_axis_world"]
            angle = float(args[0] if args else kwargs.get("angleDegrees", 360.))
            return [self._revolve_face(face, angle, first, last, transform)]
        if op == "loft":
            state = node.kwargs.get("$receiver_state")
            if not isinstance(state, dict):
                raise UnsupportedParametricConstruction("loft lacks its observed Workplane profiles")
            wires = [self._import_shape(value) for value in state.get("pending_wires", ())]
            ruled = bool(kwargs.get("ruled", args[0] if args else False))
            if not ruled:
                raise UnsupportedParametricConstruction("smooth Workplane loft lacks an exact supported expression")
            return [self._polygon_loft(wires, transform)]
        if op == "sweep":
            state = node.kwargs.get("$receiver_state")
            if not isinstance(state, dict):
                raise UnsupportedParametricConstruction("sweep lacks observed source profile")
            wires = [self._import_shape(value) for value in state.get("pending_wires", ())]
            if not wires or len(wires) > 2 or any(wire.ShapeType() != TopAbs_WIRE for wire in wires):
                raise UnsupportedParametricConstruction("sweep profile is not one circle with at most one bore")
            outer = self._complete_circle(wires[0])
            inner = self._complete_circle(wires[1]) if len(wires) == 2 else None
            if outer is None or len(wires) == 2 and (inner is None or np.linalg.norm(outer[0]-inner[0]) > 1e-10):
                raise UnsupportedParametricConstruction("sweep profile is not exact concentric circles")
            if kwargs.get("multisection", False) or kwargs.get("auxSpine") is not None:
                raise UnsupportedParametricConstruction("multisection/auxiliary source sweep unsupported")
            if not args:
                raise UnsupportedParametricConstruction("sweep lacks actual path")
            path = args[0]
            if path.ShapeType() == TopAbs_EDGE:
                wire_builder = BRepBuilderAPI_MakeWire(TopoDS.Edge_s(path))
                wire_builder.Build()
                if not wire_builder.IsDone():
                    raise UnsupportedParametricConstruction("source sweep edge could not form a wire")
                path = wire_builder.Wire()
            return [self._circular_path_sweep(path, outer[1], inner[1] if inner else None, transform)]
        raise UnsupportedParametricConstruction(f"unsupported observed construction operation {op}")

    def encode(self) -> tuple[Any, ...]:
        items = self._encode(self.root, np.eye(4))
        result = []
        for item in items:
            if item.is_a("IfcBooleanResult") or item.is_a("IfcCsgPrimitive3D"):
                item = self.model.create_entity("IfcCsgSolid", item)
            if not item.is_a("IfcSolidModel"):
                raise UnsupportedParametricConstruction("top-level expression is not an IFC solid")
            result.append(item)
        if not result:
            raise UnsupportedParametricConstruction("empty parametric solid expression")
        return tuple(result)


def encode_parametric_items(model: Any, construction: GeometryConstruction,
                            project_length_unit_m: float) -> tuple[Any, ...]:
    return _Encoder(model, construction, project_length_unit_m).encode()
