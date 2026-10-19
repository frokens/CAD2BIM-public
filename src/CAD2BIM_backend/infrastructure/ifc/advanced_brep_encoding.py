"""Encode a supported exact OCCT solid closure as nested IFC Advanced BREP maps.

This module is loaded only for an explicitly selected Advanced route. Each
source solid keeps its own reversible frame; one outer representation continues
to belong to the original Geometry fragment and its existing component map.
"""
from __future__ import annotations

import io
import math
from typing import Any

import cadquery as cq
import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
from OCP.Geom import (
    Geom_BSplineCurve, Geom_BSplineSurface, Geom_BezierCurve, Geom_BezierSurface,
    Geom_Circle, Geom_ConicalSurface, Geom_Ellipse, Geom_Line,
    Geom_RectangularTrimmedSurface, Geom_SphericalSurface,
    Geom_SurfaceOfLinearExtrusion, Geom_SurfaceOfRevolution,
    Geom_ToroidalSurface,
)
from OCP.GeomAbs import (GeomAbs_BSplineCurve, GeomAbs_BezierCurve,
                         GeomAbs_Line, GeomAbs_Circle, GeomAbs_Ellipse)
from OCP.GeomConvert import GeomConvert
from OCP.gp import gp_Trsf
from OCP.TopAbs import TopAbs_FORWARD
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS_Iterator


class UnsupportedAdvancedBrep(ValueError):
    """The selected exact route cannot encode this actual source topology."""


def _vector(value: Any) -> tuple[float, float, float]:
    if hasattr(value, "X"):
        return float(value.X()), float(value.Y()), float(value.Z())
    return tuple(float(part) for part in value)


def _support(face: Any) -> Any:
    surface = BRep_Tool.Surface_s(face.wrapped if hasattr(face, "wrapped") else face)
    while isinstance(surface, Geom_RectangularTrimmedSurface):
        surface = surface.BasisSurface()
    return surface


def _local_frame(solid: Any) -> tuple[Any, np.ndarray, np.ndarray]:
    """Put the solid's actual torus/revolution axes in one reader-safe plane."""
    supports = [_support(face) for face in solid.Faces()]
    tori = [s for s in supports if isinstance(s, Geom_ToroidalSurface)]
    revolutions = [s for s in supports if isinstance(s, Geom_SurfaceOfRevolution)]
    if not tori and not revolutions:
        return solid, np.eye(3), np.zeros(3)
    axes = [s.Position().Axis() for s in tori] + [s.Axis() for s in revolutions]
    origins = [np.asarray(_vector(a.Location())) for a in axes]
    directions = [np.asarray(_vector(a.Direction())) for a in axes]
    origin = origins[0]
    rows = directions + [p-origin for p in origins if np.linalg.norm(p-origin) > 1e-12]
    _, singular, basis = np.linalg.svd(np.asarray([v/np.linalg.norm(v) for v in rows]))
    first_axis = directions[0]
    if tori:
        radial = np.asarray(_vector(tori[0].Position().XDirection()))
    else:
        curve = revolutions[0].BasisCurve()
        parameter = curve.FirstParameter()
        point = np.asarray(_vector(curve.Value(parameter if math.isfinite(parameter) else 0.)))
        radial = point-origin-first_axis*np.dot(point-origin, first_axis)
        if np.linalg.norm(radial) < 1e-12:
            seed = np.array((1., 0., 0.)) if abs(first_axis[0]) < .9 else np.array((0., 1., 0.))
            radial = seed-first_axis*np.dot(seed, first_axis)
        radial /= np.linalg.norm(radial)
    preferred = np.cross(radial, first_axis)
    normal = preferred if len(singular) < 2 or singular[1] <= 1e-12 else basis[-1]
    if np.dot(normal, preferred) < 0:
        normal = -normal
    if (max(abs(np.dot(a, normal)) for a in directions) > 1e-10
            or max(abs(np.dot(p-origin, normal)) for p in origins) > 1e-10):
        raise UnsupportedAdvancedBrep("revolution axes are not coplanar")
    x = np.cross(first_axis, normal)
    x /= np.linalg.norm(x)
    normal = np.cross(x, first_axis)
    rotation = np.column_stack((x, first_axis, normal))
    matrix = np.column_stack((rotation.T, -rotation.T @ origin))
    transform = gp_Trsf()
    transform.SetValues(*matrix.reshape(-1).tolist())
    return solid.transformShape(cq.Matrix(transform)), rotation, origin


class _Encoder:
    def __init__(self, model: Any, project_length_unit_m: float) -> None:
        self.model = model
        self.unit = project_length_unit_m
        self.vertices: list[tuple[Any, Any]] = []
        self.edges: list[tuple[Any, Any]] = []

    def point(self, value: Any) -> Any:
        return self.model.create_entity("IfcCartesianPoint",
            tuple(float(x) / self.unit for x in _vector(value)))

    def point2(self, value: Any) -> Any:
        return self.model.create_entity("IfcCartesianPoint",
            tuple(float(x) / self.unit for x in value))

    def direction(self, value: Any) -> Any:
        return self.model.create_entity("IfcDirection", _vector(value))

    def direction2(self, value: Any) -> Any:
        return self.model.create_entity("IfcDirection", tuple(float(x) for x in value))

    def placement(self, origin: Any, normal: Any = (0., 0., 1.), x: Any = (1., 0., 0.)) -> Any:
        return self.model.create_entity("IfcAxis2Placement3D", self.point(origin),
                                        self.direction(normal), self.direction(x))

    def axis(self, axis: Any) -> Any:
        return self.placement(_vector(axis.Location()), _vector(axis.Direction()),
                              _vector(axis.XDirection()))

    def vertex(self, source: Any) -> Any:
        for original, encoded in self.vertices:
            if original.IsSame(source):
                return encoded
        result = self.model.create_entity("IfcVertexPoint", self.point(BRep_Tool.Pnt_s(source)))
        self.vertices.append((source, result))
        return result

    def spline(self, curve: Any, project: Any = _vector) -> Any:
        if curve.IsPeriodic():
            raise UnsupportedAdvancedBrep("periodic source curve is outside the selected Advanced closure")
        if curve.IsRational():
            raise UnsupportedAdvancedBrep("rational source curve is outside the selected Advanced closure")
        args = [curve.Degree(), [self.point2(project(curve.Pole(i))) if len(project(curve.Pole(i))) == 2
                                  else self.point(project(curve.Pole(i)))
                                  for i in range(1, curve.NbPoles()+1)],
                "UNSPECIFIED", curve.IsClosed(), False,
                [curve.Multiplicity(i) for i in range(1, curve.NbKnots()+1)],
                [curve.Knot(i) for i in range(1, curve.NbKnots()+1)], "UNSPECIFIED"]
        return self.model.create_entity("IfcBSplineCurveWithKnots", *args)

    def curve(self, source: Any) -> Any:
        if BRep_Tool.Degenerated_s(source):
            raise UnsupportedAdvancedBrep("degenerate source edge needs a supported pole boundary")
        adaptor = BRepAdaptor_Curve(source)
        if adaptor.GetType() == GeomAbs_BSplineCurve:
            return self.spline(adaptor.BSpline())
        if adaptor.GetType() == GeomAbs_BezierCurve:
            return self.spline(GeomConvert.CurveToBSplineCurve_s(adaptor.Bezier()))
        kind = adaptor.GetType()
        if kind == GeomAbs_Line:
            curve = adaptor.Line()
            return self.model.create_entity("IfcLine", self.point(curve.Location()),
                self.model.create_entity("IfcVector", self.direction(curve.Direction()), 1.0))
        if kind == GeomAbs_Circle:
            curve = adaptor.Circle()
            return self.model.create_entity("IfcCircle", self.axis(curve.Position()),
                                            curve.Radius() / self.unit)
        if kind == GeomAbs_Ellipse:
            curve = adaptor.Ellipse()
            return self.model.create_entity("IfcEllipse", self.axis(curve.Position()),
                curve.MajorRadius() / self.unit, curve.MinorRadius() / self.unit)
        raise UnsupportedAdvancedBrep(f"unsupported exact edge curve kind {kind}")

    def edge(self, source: Any) -> Any:
        for original, encoded in self.edges:
            if original.IsSame(source):
                return encoded
        forward = cq.Shape.cast(source.Oriented(TopAbs_FORWARD)).wrapped
        result = self.model.create_entity("IfcEdgeCurve",
            self.vertex(TopExp.FirstVertex_s(forward)),
            self.vertex(TopExp.LastVertex_s(forward)), self.curve(forward), True)
        self.edges.append((source, result))
        return result

    def revolution(self, surface: Any, face: Any) -> Any:
        axis = surface.Axis()
        origin = np.asarray(_vector(axis.Location()))
        direction = np.asarray(_vector(axis.Direction()))
        if abs(origin[2]) > 1e-10 or abs(direction[2]) > 1e-10:
            raise UnsupportedAdvancedBrep("revolution axis leaves its selected XY plane")
        curve = surface.BasisCurve()
        _, _, v0, v1 = BRepTools.UVBounds_s(face)
        if isinstance(curve, Geom_BezierCurve):
            curve = GeomConvert.CurveToBSplineCurve_s(curve)
        points = ([np.asarray(_vector(curve.Pole(i))) for i in range(1, curve.NbPoles()+1)]
                  if isinstance(curve, Geom_BSplineCurve) else
                  [np.asarray(_vector(curve.Value(v))) for v in (v0, (v0+v1)/2, v1)])
        offsets = [p-origin-direction*np.dot(p-origin, direction) for p in points]
        radial = max(offsets, key=np.linalg.norm)
        if np.linalg.norm(radial) < 1e-12:
            raise UnsupportedAdvancedBrep("revolution generatrix lies on its axis")
        radial /= np.linalg.norm(radial)
        if max(np.linalg.norm(o-radial*np.dot(o, radial)) for o in offsets) > 1e-10:
            raise UnsupportedAdvancedBrep("revolution generatrix is not planar")
        target_radial = np.asarray((direction[1], -direction[0], 0.))
        target_radial /= np.linalg.norm(target_radial)
        def project(value: Any) -> tuple[float, float]:
            offset = np.asarray(_vector(value))-origin
            return tuple((origin+direction*np.dot(offset, direction)
                          +target_radial*np.dot(offset, radial))[:2])
        if isinstance(curve, Geom_BSplineCurve):
            encoded = self.spline(curve, project)
            segment = self.model.create_entity("IfcCompositeCurveSegment", "CONTINUOUS", True, encoded)
            profile_curve = self.model.create_entity("IfcCompositeCurve", [segment], False)
            profile = self.model.create_entity(
                "IfcArbitraryClosedProfileDef" if curve.IsClosed() else "IfcArbitraryOpenProfileDef",
                "CURVE", None, profile_curve)
        elif isinstance(curve, Geom_Line):
            profile = self.model.create_entity("IfcArbitraryOpenProfileDef", "CURVE", None,
                self.model.create_entity("IfcPolyline", [self.point2(project(curve.Value(v))) for v in (v0, v1)]))
        elif isinstance(curve, (Geom_Circle, Geom_Ellipse)):
            position = curve.Position()
            center = np.asarray(project(position.Location()))
            x = np.asarray(_vector(position.XDirection()))
            projected_x = np.asarray(project(np.asarray(_vector(position.Location()))+x))-center
            place = self.model.create_entity("IfcAxis2Placement2D", self.point2(center),
                self.direction2(projected_x/np.linalg.norm(projected_x)))
            profile_curve = (self.model.create_entity("IfcCircle", place, curve.Radius()/self.unit)
                if isinstance(curve, Geom_Circle) else self.model.create_entity("IfcEllipse", place,
                    curve.MajorRadius()/self.unit, curve.MinorRadius()/self.unit))
            profile = self.model.create_entity("IfcArbitraryClosedProfileDef", "CURVE", None, profile_curve)
        else:
            raise UnsupportedAdvancedBrep(f"unsupported revolution generatrix {type(curve).__name__}")
        axis_entity = self.model.create_entity("IfcAxis1Placement", self.point(origin), self.direction(direction))
        return self.model.create_entity("IfcSurfaceOfRevolution", profile,
                                        self.placement((0., 0., 0.)), axis_entity)

    def surface(self, face: Any) -> Any:
        surface = _support(face)
        if type(surface).__name__ == "Geom_Plane":
            return self.model.create_entity("IfcPlane", self.axis(surface.Position()))
        if type(surface).__name__ == "Geom_CylindricalSurface":
            return self.model.create_entity("IfcCylindricalSurface", self.axis(surface.Position()),
                                            surface.Radius()/self.unit)
        if isinstance(surface, Geom_SphericalSurface):
            return self.model.create_entity("IfcSphericalSurface", self.axis(surface.Position()),
                                            surface.Radius()/self.unit)
        if isinstance(surface, Geom_ToroidalSurface):
            axis = surface.Position()
            origin = np.asarray(_vector(axis.Location()))
            direction = np.asarray(_vector(axis.Direction()))
            if abs(origin[2]) > 1e-10 or abs(direction[2]) > 1e-10:
                raise UnsupportedAdvancedBrep("torus axis leaves selected generating plane")
            radial = np.asarray((direction[1], -direction[0], 0.))
            radial /= np.linalg.norm(radial)
            center = origin+surface.MajorRadius()*radial
            circle_position = self.model.create_entity("IfcAxis2Placement2D", self.point2(center[:2]), None)
            circle = self.model.create_entity("IfcCircle", circle_position, surface.MinorRadius()/self.unit)
            profile = self.model.create_entity("IfcArbitraryClosedProfileDef", "CURVE", None, circle)
            revolve_axis = self.model.create_entity("IfcAxis1Placement", self.point(origin), self.direction(direction))
            return self.model.create_entity("IfcSurfaceOfRevolution", profile,
                                            self.placement((0., 0., 0.)), revolve_axis)
        if isinstance(surface, Geom_ConicalSurface):
            _, _, v0, v1 = BRepTools.UVBounds_s(face)
            z0, z1 = v0*math.cos(surface.SemiAngle()), v1*math.cos(surface.SemiAngle())
            points = [(surface.RefRadius()+z*math.tan(surface.SemiAngle()), z)
                      for z in (z0, (z0+z1)/2, z1)]
            profile = self.model.create_entity("IfcArbitraryOpenProfileDef", "CURVE", None,
                self.model.create_entity("IfcPolyline", [self.point2(p) for p in points]))
            axis = surface.Position()
            normal = cq.Vector(*_vector(axis.XDirection())).cross(cq.Vector(*_vector(axis.Direction())))
            position = self.placement(axis.Location(), normal.toTuple(), axis.XDirection())
            revolve_axis = self.model.create_entity("IfcAxis1Placement", self.point((0., 0., 0.)),
                                                    self.direction((0., 1., 0.)))
            return self.model.create_entity("IfcSurfaceOfRevolution", profile, position, revolve_axis)
        if isinstance(surface, Geom_SurfaceOfRevolution):
            return self.revolution(surface, face)
        if isinstance(surface, Geom_SurfaceOfLinearExtrusion):
            u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
            surface = GeomConvert.SurfaceToBSplineSurface_s(
                Geom_RectangularTrimmedSurface(surface, u0, u1, v0, v1))
        if isinstance(surface, Geom_BezierSurface):
            surface = GeomConvert.SurfaceToBSplineSurface_s(surface)
        if isinstance(surface, Geom_BSplineSurface):
            if surface.IsURational() or surface.IsVRational():
                raise UnsupportedAdvancedBrep("installed AdvancedFace reader ignores rational surface weights")
            if surface.IsUPeriodic() or surface.IsVPeriodic():
                raise UnsupportedAdvancedBrep("periodic source surface is outside the selected Advanced closure")
            points = [[self.point(surface.Pole(u, v)) for v in range(1, surface.NbVPoles()+1)]
                      for u in range(1, surface.NbUPoles()+1)]
            return self.model.create_entity("IfcBSplineSurfaceWithKnots",
                surface.UDegree(), surface.VDegree(), points, "UNSPECIFIED",
                surface.IsUClosed(), surface.IsVClosed(), False,
                [surface.UMultiplicity(i) for i in range(1, surface.NbUKnots()+1)],
                [surface.VMultiplicity(i) for i in range(1, surface.NbVKnots()+1)],
                [surface.UKnot(i) for i in range(1, surface.NbUKnots()+1)],
                [surface.VKnot(i) for i in range(1, surface.NbVKnots()+1)], "UNSPECIFIED")
        raise UnsupportedAdvancedBrep(f"unsupported support surface {type(surface).__name__}")

    def encode(self, shape: Any) -> Any:
        faces = []
        for face in shape.Faces():
            bounds = []
            outer = BRepTools.OuterWire_s(face.wrapped)
            for wire in face.Wires():
                edges = []
                walk = BRepTools_WireExplorer(wire.wrapped, face.wrapped)
                while walk.More():
                    edge = walk.Current()
                    if BRep_Tool.Degenerated_s(edge):
                        raise UnsupportedAdvancedBrep("degenerate source edge is outside the selected Advanced closure")
                    edges.append(self.model.create_entity("IfcOrientedEdge", None, None,
                        self.edge(edge), edge.Orientation() == TopAbs_FORWARD))
                    walk.Next()
                if not edges:
                    raise UnsupportedAdvancedBrep("point-only boundary lacks supported vertex loop")
                loop = self.model.create_entity("IfcEdgeLoop", edges)
                bounds.append(self.model.create_entity(
                    "IfcFaceOuterBound" if wire.wrapped.IsSame(outer) else "IfcFaceBound", loop, True))
            faces.append(self.model.create_entity("IfcAdvancedFace", bounds,
                self.surface(face.wrapped), face.wrapped.Orientation() == TopAbs_FORWARD))
        return self.model.create_entity("IfcAdvancedBrep", self.model.create_entity("IfcClosedShell", faces))


def encode_advanced_brep_maps(model: Any, context: Any, payload: bytes,
                              project_length_unit_m: float) -> tuple[Any, ...]:
    """Build all source solids into the caller's private model, without a new writer."""
    source = cq.Shape.importBrep(io.BytesIO(payload))
    if not source.isValid():
        raise UnsupportedAdvancedBrep("canonical source is invalid")
    solids = []
    def visit(shape: Any) -> None:
        kind = shape.ShapeType()
        if kind == "Solid":
            solids.append(shape)
            return
        if kind not in ("Compound", "CompSolid"):
            raise UnsupportedAdvancedBrep(f"source contains independent {kind} topology outside its solids")
        children = TopoDS_Iterator(shape.wrapped)
        while children.More():
            visit(cq.Shape.cast(children.Value()))
            children.Next()
    visit(source)
    if not solids or len(solids) != len(source.Solids()) or sum(
            len(solid.Faces()) for solid in solids) != len(source.Faces()):
        raise UnsupportedAdvancedBrep("source contains topology outside its solids")
    items = []
    for index, solid in enumerate(solids):
        if len(solid.Shells()) != 1:
            raise UnsupportedAdvancedBrep(f"solid {index} has unproven multiple shells")
        local, rotation, origin = _local_frame(solid)
        encoder = _Encoder(model, project_length_unit_m)
        brep = encoder.encode(local)
        inner = model.create_entity("IfcShapeRepresentation", context, "Body", "AdvancedBrep", [brep])
        inner_map = model.create_entity("IfcRepresentationMap", encoder.placement((0., 0., 0.)), inner)
        target = model.create_entity("IfcCartesianTransformationOperator3D",
            encoder.direction(rotation[:, 0]), encoder.direction(rotation[:, 1]),
            encoder.point(origin), 1., encoder.direction(rotation[:, 2]))
        items.append(model.create_entity("IfcMappedItem", inner_map, target))
    return tuple(items)
