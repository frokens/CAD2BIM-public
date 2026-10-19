from __future__ import annotations

import hashlib
import io
import json
import math
import sys
import asyncio
import base64
from collections import Counter
from pathlib import Path

import cadquery as cq
import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.ifcopenshell_wrapper as wrapper
import ifcopenshell.validate
import ifcopenshell.util.placement
import ifcopenshell.util.unit
import numpy as np
import pytest
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRep import BRep_Tool
from OCP.BRepTools import BRepTools_WireExplorer
from OCP.GeomAbs import (GeomAbs_Circle, GeomAbs_Line, GeomAbs_Plane,
                         GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Torus)
from OCP.TopAbs import TopAbs_FORWARD
from OCP.TopoDS import TopoDS_Iterator

from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.ifc_geometry_basis import (
    IfcLengthUnit, IfcPlaneAngleUnit, IfcProjectUnitChange, IfcRepresentationContextIncompatibleError,
)
from CAD2BIM_backend.domain.ifc_geometry_construction import (
    GeometryConstruction, GeometryConstructionNode, IfcGeometryRepresentationRoute,
)
from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry, CanonicalGeometryFormat
from CAD2BIM_backend.domain.ifc_product_compilation import (
    CompileIfcProductsCommand, IfcProductCompilationBlock, IfcProductCompilationItem,
    IfcProductFragmentSource, IfcProductCompilationError,
)
from CAD2BIM_backend.domain.product_family import ProductFamilyContract
from CAD2BIM_backend.domain.product_realization import ProductRealizationResult
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationRequest, ProductRealizationInvocation, ResolvedMappedGeometryBinding,
    product_realization_binding_digest,
)
from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.ifc.product_compilation import (
    IfcOpenShellProductCompiler, preflight_product_body, _indexed_native_shells,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
from CAD2BIM_backend.infrastructure.product_realization_runtime.child import _compile_geometry
from CAD2BIM_backend.infrastructure.ifc_revision_runtime.adapter import IfcOpenShellRevisionRuntime
from CAD2BIM_backend.ports.ifc_revision_runtime import CompileIfcProductsRevisionRequest, InspectExactIfcRevisionRequest


def family(*, assembly=False, two_fragments=False):
    components = [{"component_key": "body", "ifc_class": "IfcPipeSegment"}]
    if assembly:
        components = [{"component_key": "root", "ifc_class": "IfcElementAssembly"},
                      {"component_key": "body", "ifc_class": "IfcPipeSegment", "parent_component_key": "root"}]
    fragments = [{"fragment_key": "shape", "owner_component_key": "body",
                  "binding_options": [{"binding_key": "source", "parameter_sources": {}}]}]
    if two_fragments:
        fragments.append({**fragments[0], "fragment_key": "second"})
    return ProductFamilyContract.model_validate({"product_structure_kind": "semantic_assembly" if assembly else "single_product",
        "components": components, "geometry_fragments": fragments})


def canonical(shape):
    raw = io.BytesIO()
    shape.exportBrep(raw)
    content = raw.getvalue()
    return CanonicalGeometry(format=CanonicalGeometryFormat.OCCT_BREP_V1,
        content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
        payload=content, sha256=hashlib.sha256(content).hexdigest())


def item(contract, geometry, *, key="p1", source_key="exact-source", scale=1.0,
         route=IfcGeometryRepresentationRoute.TFS, construction=None,
         fragment_transform=None, source_transform=None):
    frame = RigidTransform3D(translation_m=(3., 4., 5.), rotation=((0., -1., 0.), (1., 0., 0.), (0., 0., 1.)))
    source = pack_source_bundle({"main.py": ("def realize(ctx):\n    return " + repr({
        "status":"SOLVED", "components":[{"component_key":c.component_key} for c in contract.components],
        "fragments":[{"fragment_key":f.fragment_key,"binding_key":"source"} for f in contract.geometry_fragments]}) + "\n").encode()})
    request = ProductRealizationRequest(family_contract=contract, family_contract_digest=sha256_digest(contract),
        source_bundle=source, source_bundle_sha256=hashlib.sha256(source).hexdigest(),
        geometry_bindings=(ResolvedMappedGeometryBinding(binding_key="source", geometry=geometry,
            geometry_content_digest=geometry.sha256 if geometry is not None else sha256_digest({"exact_source_reference":source_key})),),
        invocation=ProductRealizationInvocation(product_origin=frame.translation_m, length_unit=IfcLengthUnit()))
    result = ProductRealizationResult.model_validate({
        "invocation":request.invocation.model_dump(), "policy":request.policy.model_dump(),
        "family_contract_digest":request.family_contract_digest,
        "binding_digest": product_realization_binding_digest(request), "source_bundle_sha256": request.source_bundle_sha256,
        "normalized_public_parameters": {}, "proposal": {"status": "SOLVED",
            "components": [{"component_key": c.component_key} for c in contract.components],
            "fragments": [{"fragment_key": f.fragment_key, "binding_key": "source", "geometry_parameters": {},
                           **({"local_transform": fragment_transform.model_dump()} if fragment_transform else {})}
                          for f in contract.geometry_fragments]},
        "component_project_world_transforms": [{"component_key": c.component_key,
                                               "project_world_transform": frame.model_dump()}
                                              for c in contract.components],
    })
    return IfcProductCompilationItem.from_realization(product_key=key, input_row_index=int(key[1:]) - 1,
        realization=result,
        source_to_fragment_transforms=({f.fragment_key: source_transform for f in contract.geometry_fragments}
                                       if source_transform else None),
        fragment_sources=tuple(IfcProductFragmentSource(
            fragment_key=f.fragment_key, source_key=source_key,
            geometry=geometry if i == 0 else None, mapping_scale=scale,
            representation_route=route, construction=construction if i == 0 else None)
            for i, f in enumerate(contract.geometry_fragments)))


def model(*, millimetres=False, angle_prefix=None):
    from tests.unit.backend.test_ifc_geometry_basis_compiler import _revision1, _local_basis_command
    from CAD2BIM_backend.infrastructure.ifc.geometry_basis import IfcOpenShellGeometryBasisCompiler
    basis = _local_basis_command(prefix="MILLI" if millimetres else None)
    if angle_prefix is not None:
        basis = basis.model_copy(update={"project_unit_change": IfcProjectUnitChange(
            length_unit=IfcLengthUnit(prefix="MILLI" if millimetres else None),
            plane_angle_unit=IfcPlaneAngleUnit(prefix=angle_prefix),
        )})
    content = IfcOpenShellGeometryBasisCompiler().apply(_revision1(),
        basis).content
    opened = IfcOpenShellModelCodec().private_copy(content).model
    # The valid parent identifier is deliberately not 'Model'.
    opened.by_type("IfcGeometricRepresentationContext", include_subtypes=False)[0].ContextIdentifier = "Engineering"
    return opened


def _oriented_triangles(shells):
    return Counter(min(tuple(triangle[i:] + triangle[:i]) for i in range(3))
                   for coordinates, indices in shells for index in indices
                   for triangle in [[tuple(coordinates[i - 1]) for i in index]])


def test_body_preflight_reports_missing_project_units_with_valid_body_context():
    opened = model()
    body, _ = preflight_product_body(opened)
    assert body.ContextType == "Model" and body.TargetView == "MODEL_VIEW"
    assert body.ParentContext.ContextType == "Model"
    assert body.ParentContext.CoordinateSpaceDimension == 3
    assignment = opened.by_type("IfcProject")[0].UnitsInContext
    opened.by_type("IfcProject")[0].UnitsInContext = None
    opened.remove(assignment)
    with pytest.raises(IfcRepresentationContextIncompatibleError) as caught:
        preflight_product_body(opened)
    assert caught.value.code == "IFC_REPRESENTATION_CONTEXT_INCOMPATIBLE"
    message = str(caught.value)
    assert "project geometry_basis is incomplete" in message
    assert "LENGTHUNIT and PLANEANGLEUNIT" in message
    assert "exactly one 3D Model context" in message
    assert "ifc_model_configure_geometry_basis" in message


def _saved_triangles(saved):
    return _oriented_triangles([(item.Coordinates.CoordList, item.CoordIndex)
                               for item in saved.by_type("IfcTriangulatedFaceSet")])


def _native_shell_triangles(representation):
    shells = []
    for item in representation.Items:
        for shell in item.FbsmFaces:
            triangles = Counter()
            for face in shell.CfsFaces:
                bound = face.Bounds[0]
                vertices = [tuple(point.Coordinates) for point in bound.Bound.Polygon]
                if not bound.Orientation:
                    vertices.reverse()
                triangles[min(tuple(vertices[i:] + vertices[:i]) for i in range(3))] += 1
            shells.append(triangles)
    return shells


def _native_triangles(representation):
    triangles = Counter()
    for shell in _native_shell_triangles(representation):
        triangles.update(shell)
    return triangles


def _native_representation(*, extra_bound=False, point_count=3, coordinates=(0., 0., 0.), detached=False,
                           no_faces=False):
    native = ifcopenshell.file(schema="IFC4X3_ADD2")
    def create(kind, **attributes):
        return (ifcopenshell.create_entity(kind, schema="IFC4X3_ADD2", **attributes)
                if detached else native.create_entity(kind, **attributes))
    points = [create("IfcCartesianPoint", Coordinates=coordinates),
              create("IfcCartesianPoint", Coordinates=(1., 0., 0.)),
              create("IfcCartesianPoint", Coordinates=(0., 1., 0.)),
              create("IfcCartesianPoint", Coordinates=(0., 0., 0.))]
    def face(indices, orientation=True):
        loop = create("IfcPolyLoop", Polygon=[points[i] for i in indices])
        bound = create("IfcFaceOuterBound", Bound=loop, Orientation=orientation)
        return create("IfcFace", Bounds=[bound, bound] if extra_bound else [bound])
    first = face(tuple(range(point_count)), False)
    faces = [first, face((0, 1, 2)), face((3, 1, 2)), face((0, 0, 1))]
    shells = [create("IfcConnectedFaceSet", CfsFaces=[] if no_faces else faces),
              create("IfcConnectedFaceSet", CfsFaces=[face((0, 1, 2))])]
    item = create("IfcFaceBasedSurfaceModel", FbsmFaces=shells)
    return create("IfcShapeRepresentation", RepresentationType="SurfaceModel", Items=[item])


@pytest.mark.parametrize("detached", [False, True])
def test_indexing_preserves_shell_point_identity_orientation_duplicate_and_degenerate_triangles(detached):
    representation = _native_representation(detached=detached)
    if detached:
        points = representation.Items[0].FbsmFaces[0].CfsFaces[0].Bounds[0].Bound.Polygon
        assert {p.id() for p in points} == {0}
        assert len({p.wrapped_data.identity() for p in points}) == 3
    shells = _indexed_native_shells(representation, .001)
    assert len(shells) == 2
    coordinates, indices = shells[0]
    assert len(coordinates) == 4  # Two distinct native points share the origin.
    assert coordinates == [(0., 1000., 0.), (1000., 0., 0.), (0., 0., 0.), (0., 0., 0.)]
    assert indices == [(1, 2, 3), (3, 2, 1), (4, 2, 1), (3, 3, 2)]
    assert shells[1][1] == [(1, 2, 3)]  # Indexing restarts for each shell.
    assert sum(_oriented_triangles(shells).values()) == 5


@pytest.mark.parametrize("kwargs", [{"extra_bound": True}, {"point_count": 4},
    {"coordinates": (0., 0.)}, {"no_faces": True}])
def test_indexing_rejects_unsupported_native_mesh_without_triangulating(kwargs):
    with pytest.raises(ValueError, match="native mesh"):
        _indexed_native_shells(_native_representation(**kwargs), 1.)


def test_indexing_rejects_nonfinite_project_coordinates_from_finite_native_points():
    with pytest.raises(ValueError, match="exceeds project coordinate range"):
        _indexed_native_shells(_native_representation(), 1e-309)


@pytest.mark.parametrize("millimetres", [False, True])
def test_advanced_source_has_one_outer_map_and_exact_inner_solid_maps(millimetres, monkeypatch):
    source = cq.Compound.makeCompound([
        cq.Workplane().box(.1, .2, .3).val(),
        cq.Workplane().box(.05, .04, .03).val().translate((.4, 0., 0.)),
    ])
    geometry = canonical(source)
    opened = model(millimetres=millimetres)
    contract = family()
    compiler = IfcOpenShellProductCompiler(opened, CompileIfcProductsCommand(family_contract=contract))
    def no_tessellation(*args):
        raise AssertionError("Advanced route must never try TFS")
    monkeypatch.setattr(ifcopenshell.geom, "tesselate", no_tessellation)
    compiler.consume(IfcProductCompilationBlock(items=(
        item(contract, geometry, scale=1.75,
             route=IfcGeometryRepresentationRoute.ADVANCED_BREP),)))
    compiler.consume(IfcProductCompilationBlock(items=(
        item(contract, None, key="p2", scale=1.75,
             route=IfcGeometryRepresentationRoute.ADVANCED_BREP),)))
    saved = ifcopenshell.file.from_string(opened.to_string())
    compiler.verify_saved(saved)
    assert compiler.effect().source_count == 1
    assert len(saved.by_type("IfcAdvancedBrep")) == 2
    assert len(saved.by_type("IfcTriangulatedFaceSet")) == 0
    assert len(saved.by_type("IfcRepresentationMap")) == 3
    outer_maps = [m for m in saved.by_type("IfcRepresentationMap")
                  if m.MappedRepresentation.RepresentationType == "MappedRepresentation"]
    assert len(outer_maps) == 1
    assert len(outer_maps[0].MappedRepresentation.Items) == 2
    assert all(mapped.MappingTarget.Scale == 1.0
               for mapped in outer_maps[0].MappedRepresentation.Items)
    product_maps = [item for product in saved.by_type("IfcPipeSegment")
                    for representation in product.Representation.Representations
                    for item in representation.Items]
    assert len(product_maps) == 2
    assert all(mapped.MappingTarget.Scale == 1.75 for mapped in product_maps)
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    settings.set("reorient-shells", True)
    recovered = ifcopenshell.geom.create_shape(settings, saved.by_type("IfcPipeSegment")[0])
    vertices = np.asarray(recovered.geometry.verts).reshape(-1, 3)
    assert vertices.min(axis=0) == pytest.approx((2.825, 3.9125, 4.7375), abs=1e-5)
    assert vertices.max(axis=0) == pytest.approx((3.175, 4.74375, 5.2625), abs=1e-5)
    if millimetres:
        points = [tuple(point.Coordinates) for point in saved.by_type("IfcCartesianPoint")]
        assert any(abs(coordinate) >= 100. for point in points for coordinate in point)


def test_advanced_encoding_failure_does_not_fall_back_to_tfs(monkeypatch):
    geometry = canonical(cq.Workplane().box(.1, .2, .3).val())
    opened = model()
    contract = family()
    compiler = IfcOpenShellProductCompiler(opened, CompileIfcProductsCommand(family_contract=contract))
    def no_tessellation(*args):
        raise AssertionError("technical Advanced failure must never try TFS")
    def failed_encoder(*args):
        raise ValueError("selected Advanced encoder failed")
    monkeypatch.setattr(ifcopenshell.geom, "tesselate", no_tessellation)
    from CAD2BIM_backend.infrastructure.ifc import advanced_brep_encoding
    monkeypatch.setattr(advanced_brep_encoding, "encode_advanced_brep_maps", failed_encoder)
    with pytest.raises(IfcProductCompilationError, match="selected Advanced encoder failed"):
        compiler.consume(IfcProductCompilationBlock(items=(
            item(contract, geometry, route=IfcGeometryRepresentationRoute.ADVANCED_BREP),)))


def test_advanced_rejects_independent_topology_outside_source_solids(monkeypatch):
    source = cq.Compound.makeCompound([
        cq.Workplane().box(.1, .2, .3).val(),
        cq.Edge.makeLine(cq.Vector(0, 0, 0), cq.Vector(1, 0, 0)),
    ])
    geometry = canonical(source)
    opened = model()
    contract = family()
    compiler = IfcOpenShellProductCompiler(opened, CompileIfcProductsCommand(family_contract=contract))
    monkeypatch.setattr(ifcopenshell.geom, "tesselate",
                        lambda *args: (_ for _ in ()).throw(AssertionError("Advanced route tried TFS")))
    with pytest.raises(IfcProductCompilationError, match="independent Edge topology"):
        compiler.consume(IfcProductCompilationBlock(items=(item(contract, geometry,
            route=IfcGeometryRepresentationRoute.ADVANCED_BREP),)))


def test_saved_advanced_rejects_changed_outer_and_inner_map_placement():
    geometry = canonical(cq.Workplane().box(.1, .2, .3).val())
    opened = model()
    contract = family()
    compiler = IfcOpenShellProductCompiler(opened, CompileIfcProductsCommand(family_contract=contract))
    compiler.consume(IfcProductCompilationBlock(items=(item(contract, geometry,
        route=IfcGeometryRepresentationRoute.ADVANCED_BREP),)))
    serialized = opened.to_string()
    for kind in ("outer_target_origin", "outer_target_axis", "outer_map_origin", "inner_map_origin"):
        saved = ifcopenshell.file.from_string(serialized)
        product = saved.by_type("IfcPipeSegment")[0]
        outer_item = product.Representation.Representations[0].Items[0]
        outer_map = outer_item.MappingSource
        inner_map = outer_map.MappedRepresentation.Items[0].MappingSource
        if kind == "outer_target_origin":
            outer_item.MappingTarget.LocalOrigin.Coordinates = (3.01, 4., 5.)
        elif kind == "outer_target_axis":
            outer_item.MappingTarget.Axis1.DirectionRatios = (0., 1., 0.)
        elif kind == "outer_map_origin":
            outer_map.MappingOrigin.Location.Coordinates = (.01, 0., 0.)
        else:
            inner_map.MappingOrigin.Location.Coordinates = (.01, 0., 0.)
        try:
            compiler.verify_saved(saved)
        except ValueError:
            continue
        pytest.fail(f"saved candidate accepted changed {kind}")


@pytest.mark.parametrize("millimetres", [False, True])
def test_same_execution_cylinder_construction_encodes_project_units(millimetres, monkeypatch):
    shape = cq.Solid.makeCylinder(.1, .5)
    geometry = canonical(shape)
    construction = GeometryConstruction(root=0, nodes=(GeometryConstructionNode(
        id=0, op="Solid.makeCylinder", args=(.1, .5),
        shape_brep=base64.b64encode(geometry.payload).decode("ascii")),))
    opened = model(millimetres=millimetres)
    contract = family()
    compiler = IfcOpenShellProductCompiler(opened, CompileIfcProductsCommand(family_contract=contract))
    monkeypatch.setattr(ifcopenshell.geom, "tesselate",
                        lambda *args: (_ for _ in ()).throw(AssertionError("parametric route tried TFS")))
    compiler.consume(IfcProductCompilationBlock(items=(item(contract, geometry,
        route=IfcGeometryRepresentationRoute.PARAMETRIC, construction=construction),)))
    saved = ifcopenshell.file.from_string(opened.to_string())
    compiler.verify_saved(saved)
    assert len(saved.by_type("IfcExtrudedAreaSolid")) == 1
    assert len(saved.by_type("IfcTriangulatedFaceSet")) == 0
    solid = saved.by_type("IfcExtrudedAreaSolid")[0]
    unit = .001 if millimetres else 1.
    assert solid.Depth == pytest.approx(.5/unit)
    assert solid.SweptArea.Radius == pytest.approx(.1/unit)
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    recovered = ifcopenshell.geom.create_shape(settings, saved.by_type("IfcPipeSegment")[0])
    vertices = np.asarray(recovered.geometry.verts).reshape(-1, 3)
    assert vertices.min(axis=0) == pytest.approx((2.9, 3.9, 5.), abs=1e-3)
    assert vertices.max(axis=0) == pytest.approx((3.1, 4.1, 5.5), abs=1e-3)


def test_parametric_topology_enumeration_preserves_degenerate_edges_and_inner_wires():
    from CAD2BIM_backend.infrastructure.ifc.parametric_encoding import _subshapes

    # A sphere contains degenerate pole edges; CadQuery deliberately omits them.
    sphere = cq.Solid.makeSphere(.2, angleDegrees1=-90, angleDegrees2=90)
    native_edges = sphere._entities("Edge")
    assert native_edges.Extent() > len(sphere.Edges())
    outer = cq.Wire.makeCircle(.2, cq.Vector(), cq.Vector(0, 0, 1))
    inner = cq.Wire.makeCircle(.1, cq.Vector(), cq.Vector(0, 0, 1))
    annulus = cq.Face.makeFromWires(outer, [inner])
    for shape in (sphere, annulus):
        for kind in ("Face", "Wire", "Edge", "Vertex"):
            expected = getattr(shape, kind + "s")() if kind != "Vertex" else shape.Vertices()
            actual = _subshapes(shape.wrapped, kind)
            assert len(actual) == len(expected)
            assert all(left.IsEqual(right.wrapped)
                       for left, right in zip(actual, expected))
    actual_inner = [wire for wire in _subshapes(annulus.wrapped, "Wire")
                    if not wire.IsSame(annulus.outerWire().wrapped)]
    assert len(actual_inner) == 1
    assert actual_inner[0].IsEqual(annulus.innerWires()[0].wrapped)


def test_parametric_profile_keeps_closed_and_coplanar_wire_guards():
    from CAD2BIM_backend.infrastructure.ifc.parametric_encoding import (
        _face_from_wires, UnsupportedParametricConstruction,
    )

    outer = cq.Wire.makeCircle(.2, cq.Vector(), cq.Vector(0, 0, 1))
    inner = cq.Wire.makeCircle(.1, cq.Vector(0, 0, .1), cq.Vector(0, 0, 1))
    with pytest.raises(UnsupportedParametricConstruction, match="not planar"):
        _face_from_wires(outer.wrapped, [inner.wrapped])
    open_wire = cq.Workplane().polyline(((0, 0), (.1, 0), (.1, .1))).wire().val()
    with pytest.raises(UnsupportedParametricConstruction, match="inner wire is not closed"):
        _face_from_wires(outer.wrapped, [open_wire.wrapped])
    with pytest.raises(UnsupportedParametricConstruction, match="outer wire is not closed"):
        _face_from_wires(open_wire.wrapped, [outer.wrapped])


@pytest.mark.parametrize("millimetres", [False, True])
def test_actual_rotated_box_encodes_one_block_without_clipping(millimetres, monkeypatch):
    shape = cq.Workplane().box(.2, .1, .3).val().rotate((0, 0, 0), (0, 0, 1), 90)
    shape = shape.translate((.4, -.2, .05))
    geometry = canonical(shape)
    construction = GeometryConstruction(root=0, nodes=(GeometryConstructionNode(
        id=0, op="box", shape_brep=base64.b64encode(geometry.payload).decode("ascii")),))
    opened = model(millimetres=millimetres)
    contract = family()
    compiler = IfcOpenShellProductCompiler(opened, CompileIfcProductsCommand(family_contract=contract))
    monkeypatch.setattr(ifcopenshell.geom, "tesselate",
                        lambda *args: (_ for _ in ()).throw(AssertionError("box tried TFS")))
    compiler.consume(IfcProductCompilationBlock(items=(item(contract, geometry,
        route=IfcGeometryRepresentationRoute.PARAMETRIC, construction=construction),)))
    saved = ifcopenshell.file.from_string(opened.to_string())
    compiler.verify_saved(saved)
    assert len(saved.by_type("IfcBlock")) == 1
    assert len(saved.by_type("IfcCsgSolid")) == 1
    assert not saved.by_type("IfcHalfSpaceSolid")
    assert not saved.by_type("IfcBooleanClippingResult")
    unit = .001 if millimetres else 1.
    dimensions = sorted((saved.by_type("IfcBlock")[0].XLength,
                         saved.by_type("IfcBlock")[0].YLength,
                         saved.by_type("IfcBlock")[0].ZLength))
    assert dimensions == pytest.approx([.1/unit, .2/unit, .3/unit])
    settings = ifcopenshell.geom.settings()
    settings.set("iterator-output", wrapper.SERIALIZED)
    settings.set("use-world-coords", True)
    settings.set("reorient-shells", True)
    returned = ifcopenshell.geom.create_shape(settings, saved.by_type("IfcPipeSegment")[0])
    target = cq.Shape.importBrep(io.BytesIO(returned.geometry.brep_data.encode()))
    expected = shape.rotate((0, 0, 0), (0, 0, 1), 90).translate((3, 4, 5))
    assert target.isValid()
    assert expected.cut(target).Volume() == pytest.approx(0., abs=1e-12)
    assert target.cut(expected).Volume() == pytest.approx(0., abs=1e-12)


@pytest.mark.parametrize("angle_prefix", [None, "MILLI"])
def test_axisymmetric_fillet_section_clips_serially_and_preserves_material(monkeypatch, angle_prefix):
    from CAD2BIM_backend.infrastructure.ifc import parametric_encoding

    shape = cq.Workplane(obj=cq.Solid.makeCylinder(.1, .3)).edges().fillet(.01).val()
    geometry = canonical(shape)
    construction = GeometryConstruction(root=0, nodes=(GeometryConstructionNode(
        id=0, op="fillet", shape_brep=base64.b64encode(geometry.payload).decode("ascii")),))
    calls = []

    def recorded(original, label):
        class Recorder:
            def __init__(self, *args):
                self.native = original(*args)

            def SetRunParallel(self, enabled):
                calls.append((label, enabled))
                self.native.SetRunParallel(enabled)

            def __getattr__(self, name):
                return getattr(self.native, name)
        return Recorder

    monkeypatch.setattr(parametric_encoding, "BRepAlgoAPI_Section",
                        recorded(parametric_encoding.BRepAlgoAPI_Section, "section"))
    monkeypatch.setattr(parametric_encoding, "BRepAlgoAPI_Common",
                        recorded(parametric_encoding.BRepAlgoAPI_Common, "common"))
    monkeypatch.setattr(ifcopenshell.geom, "tesselate",
                        lambda *args: (_ for _ in ()).throw(AssertionError("fillet tried TFS")))
    opened = model(angle_prefix=angle_prefix)
    contract = family()
    compiler = IfcOpenShellProductCompiler(opened, CompileIfcProductsCommand(family_contract=contract))
    compiler.consume(IfcProductCompilationBlock(items=(item(contract, geometry,
        route=IfcGeometryRepresentationRoute.PARAMETRIC, construction=construction),)))
    saved = ifcopenshell.file.from_string(opened.to_string())
    compiler.verify_saved(saved)
    assert ifcopenshell.util.unit.calculate_unit_scale(saved, "PLANEANGLEUNIT") == pytest.approx(
        .001 if angle_prefix == "MILLI" else 1.)
    assert saved.by_type("IfcRevolvedAreaSolid")
    assert all(solid.Angle == pytest.approx(2*math.pi / (.001 if angle_prefix == "MILLI" else 1.))
               for solid in saved.by_type("IfcRevolvedAreaSolid"))
    logger = ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(saved, logger, express_rules=True)
    assert not logger.statements
    assert ("section", False) in calls and ("common", False) in calls
    assert all(enabled is False for _, enabled in calls)
    settings = ifcopenshell.geom.settings()
    settings.set("iterator-output", wrapper.SERIALIZED)
    settings.set("use-world-coords", True)
    returned = ifcopenshell.geom.create_shape(settings, saved.by_type("IfcPipeSegment")[0])
    target = cq.Shape.importBrep(io.BytesIO(returned.geometry.brep_data.encode()))
    expected = shape.rotate((0, 0, 0), (0, 0, 1), 90).translate((3, 4, 5))
    assert target.isValid() and len(target.Solids()) == 1 and target.Volume() > 0
    assert expected.cut(target).Volume() == pytest.approx(0., abs=1e-12)
    assert target.cut(expected).Volume() == pytest.approx(0., abs=1e-12)


def _curved_two_port_operand_boundary(shape):
    """Complete oriented analytic faces for this source's three revolve operands."""
    def xyz(value):
        return tuple(float(v) for v in (value.X(), value.Y(), value.Z()))

    def axis(value):
        return (xyz(value.Location()), xyz(value.Direction()),
                xyz(value.XDirection()), xyz(value.YDirection()))

    faces = []
    for face in shape.Faces():
        adaptor = BRepAdaptor_Surface(face.wrapped)
        kind = adaptor.GetType()
        if kind == GeomAbs_Plane:
            plane = adaptor.Plane()
            support = ("plane", axis(plane.Position()))
        elif kind == GeomAbs_Cylinder:
            cylinder = adaptor.Cylinder()
            position = cylinder.Position()
            origin, direction = np.asarray(xyz(position.Location())), np.asarray(xyz(position.Direction()))
            support = ("cylinder", tuple(origin-direction*np.dot(origin, direction)),
                       tuple(direction), float(cylinder.Radius()))
        elif kind == GeomAbs_Cone:
            cone = adaptor.Cone()
            # OCCT may choose a different axial reference point and RefRadius
            # for the same cone; apex, axis and signed semi-angle fix its support.
            support = ("cone", xyz(cone.Apex()), xyz(cone.Axis().Direction()),
                       float(cone.SemiAngle()))
        elif kind == GeomAbs_Torus:
            torus = adaptor.Torus()
            support = ("torus", axis(torus.Position()), float(torus.MajorRadius()),
                       float(torus.MinorRadius()))
        else:
            raise AssertionError(f"two-port source/reader has a nonanalytic face: {kind}")
        outer = face.outerWire()
        loops = []
        for wire in face.Wires():
            walk = BRepTools_WireExplorer(wire.wrapped, face.wrapped)
            edges = []
            while walk.More():
                native = walk.Current()
                curve = BRepAdaptor_Curve(native)
                first, last = float(curve.FirstParameter()), float(curve.LastParameter())
                curve_kind = curve.GetType()
                if curve_kind == GeomAbs_Circle:
                    circle = curve.Circle()
                    basis = ("circle", axis(circle.Position()), float(circle.Radius()))
                elif curve_kind == GeomAbs_Line:
                    line = curve.Line()
                    basis = ("line", xyz(line.Location()), xyz(line.Direction()))
                else:
                    raise AssertionError(f"two-port source/reader has a nonanalytic trim: {curve_kind}")
                forward = native.Orientation() == TopAbs_FORWARD
                start, end = xyz(curve.Value(first)), xyz(curve.Value(last))
                oriented_start, oriented_end = (start, end) if forward else (end, start)
                edges.append((str(native.Orientation()), basis, first, last,
                              oriented_start, oriented_end))
                walk.Next()
            usages = TopoDS_Iterator(wire.wrapped)
            count = 0
            while usages.More():
                count += 1
                usages.Next()
            assert len(edges) == count and wire.IsClosed()
            assert all(math.dist(edges[i][5], edges[(i+1) % len(edges)][4]) < 1e-10
                       for i in range(len(edges)))
            loops.append((wire.isSame(outer), str(wire.wrapped.Orientation()),
                          len(edges), len(wire.Edges()), tuple(edges)))
        u0, u1, v0, v1 = map(float, face.uvBounds())
        u_period = float(adaptor.UPeriod()) if adaptor.IsUPeriodic() else None
        v_period = float(adaptor.VPeriod()) if adaptor.IsVPeriodic() else None
        # A periodic face may report the same complete interval on adjacent
        # branches separated by one period. Retain start modulo the period and
        # the entire directed span; ordered trim edges still determine the cut.
        def periodic_start(value, period):
            result = value % period
            return 0. if abs(result-period) < 1e-12 else result

        uv = ((periodic_start(u0, u_period), u1-u0) if u_period else (u0, u1),
              (periodic_start(v0, v_period), v1-v0) if v_period else (v0, v1))
        faces.append((support, str(face.wrapped.Orientation()), uv,
                      bool(adaptor.IsUPeriodic()), u_period,
                      bool(adaptor.IsVPeriodic()), v_period,
                      tuple(loops)))
    return tuple(faces)


def _assert_curved_two_port_analytic_match(source, target):
    """Compare every oriented support and trim; serialization noise is sub-picometre."""
    source_faces = _curved_two_port_operand_boundary(source)
    target_faces = _curved_two_port_operand_boundary(target)
    assert len(source_faces) == len(target_faces)

    def equal(expected, actual, path="face"):
        if isinstance(expected, tuple):
            if not isinstance(actual, tuple) or len(actual) != len(expected):
                raise AssertionError(f"{path}: tuple length {len(expected)} != {len(actual)}")
            for index, (left, right) in enumerate(zip(expected, actual)):
                equal(left, right, f"{path}[{index}]")
        elif isinstance(expected, bool):
            if actual is not expected:
                raise AssertionError(f"{path}: {expected!r} != {actual!r}")
        elif isinstance(expected, (int, float)) and expected is not None:
            if actual != pytest.approx(expected, abs=1e-12, rel=0):
                raise AssertionError(f"{path}: {expected!r} != {actual!r}")
        else:
            if actual != expected:
                raise AssertionError(f"{path}: {expected!r} != {actual!r}")

    # Boolean results can enumerate the same faces in a different order.
    # Consume every target face exactly once; every oriented edge remains in
    # its original wire order within the face record.
    remaining = list(target_faces)
    for source_face in source_faces:
        mismatches = []
        for index, target_face in enumerate(remaining):
            if source_face[0][0] != target_face[0][0]:
                continue
            try:
                equal(source_face, target_face)
            except AssertionError as exc:
                mismatches.append(str(exc))
                continue
            remaining.pop(index)
            break
        else:
            raise AssertionError(
                f"no matching oriented analytic face for {source_face[0][0]}; "
                f"same-support first differences: {mismatches}; "
                f"remaining reader supports: {[face[0][0] for face in remaining]}")
    assert not remaining


@pytest.mark.parametrize("millimetres", [False, True])
def test_distant_revolution_axis_uses_original_circle_support(millimetres, tmp_path):
    """A real near-straight transition has a 12,100 km revolution radius.

    Reconstructing its circle's face changes the support normal by one ulp;
    the resulting false plane residual used to reject this valid source.
    """
    from CAD2BIM_backend.infrastructure.ifc.parametric_encoding import (
        _Encoder, _face_from_wires, UnsupportedParametricConstruction,
    )
    source = b'''import cadquery as cq
def build(ctx):
    center = (-0.007230024511357682, -0.0021727065361796393, 0.)
    normal = (0.9576913187455535, 0.2877973905361249, 0.)
    first = (-3482348.4327171356, 11588064.954648491, 0.)
    last = (first[0], first[1], 1.)
    outer = cq.Wire.makeCircle(.0605, center, normal)
    inner = cq.Wire.makeCircle(.05203, center, normal)
    return cq.Solid.revolve(outer, [inner], 7.149595585121164e-08, first, last)
'''
    program = _compile_geometry(pack_source_bundle({"main.py": source}), "distant_axis", "build")
    shape, _, construction = execute_geometry_build(program, {}, capture_construction=True)
    opened = model(millimetres=millimetres)
    encoder = _Encoder(opened, construction, .001 if millimetres else 1.)
    node = next(node for node in construction.nodes if node.op == "Solid.revolve")
    args, _ = encoder._values(node)
    face = _face_from_wires(args[0], args[1])
    entity = encoder.encode()[0]
    candidate = tmp_path / "distant-axis.ifc"
    opened.write(str(candidate))
    saved = ifcopenshell.open(str(candidate))
    settings = ifcopenshell.geom.settings()
    settings.set("iterator-output", wrapper.SERIALIZED)
    returned = ifcopenshell.geom.create_shape(settings, saved.by_id(entity.id()))
    geometry = returned.geometry if hasattr(returned, "geometry") else returned
    target = cq.Shape.importBrep(io.BytesIO(geometry.brep_data.encode()))
    assert target.isValid() and len(target.Solids()) == len(shape.Solids()) == 1
    assert target.Volume() == pytest.approx(shape.Volume(), rel=1e-6)
    source_bounds, target_bounds = shape.BoundingBox(), target.BoundingBox()
    for coordinate in ("xmin", "ymin", "zmin", "xmax", "ymax", "zmax"):
        assert getattr(target_bounds, coordinate) == pytest.approx(
            getattr(source_bounds, coordinate), rel=0, abs=1e-9)
    # A real offset or a tilted axis still fails the unchanged plane guard.
    normal = np.asarray((0.9576913187455535, 0.2877973905361249, 0.))
    first, last = np.asarray(args[3]), np.asarray(args[4])
    for bad_first, bad_last in ((first+normal*1e-5, last+normal*1e-5),
                               (first, last+normal*.001)):
        with pytest.raises(UnsupportedParametricConstruction, match="axis leaves actual profile plane"):
            encoder._revolve_face(face, args[2], bad_first, bad_last, np.eye(4))


def _assert_curved_two_port_revolve_operands(saved, construction, observed_revolves):
    nodes = {node.id: node for node in construction.nodes}
    fuse = [node for node in construction.nodes if node.op == "fuse"]
    assert len(fuse) == 1 and fuse[0].receiver is not None and len(fuse[0].args) == 2
    source_ids = (fuse[0].receiver, fuse[0].args[0]["$ref"], fuse[0].args[1]["$ref"])
    source_nodes = [nodes[node_id] for node_id in source_ids]
    assert all(node.op == "Solid.revolve" and node.id in observed_revolves for node in source_nodes)
    assert [float(node.args[2]) for node in source_nodes] == pytest.approx([90., 360., 360.])
    csg = saved.by_type("IfcCsgSolid")
    assert len(csg) == 1
    last_union = csg[0].TreeRootExpression
    first_union = last_union.FirstOperand
    assert last_union.is_a("IfcBooleanResult") and last_union.Operator == "UNION"
    assert first_union.is_a("IfcBooleanResult") and first_union.Operator == "UNION"
    operands = (first_union.FirstOperand, first_union.SecondOperand,
                last_union.SecondOperand)
    assert all(entity.is_a() == "IfcRevolvedAreaSolid" for entity in operands)
    assert [float(entity.Angle) for entity in operands] == pytest.approx(
        [math.pi/2, 2*math.pi, 2*math.pi], abs=1e-12)
    settings = ifcopenshell.geom.settings()
    settings.set("iterator-output", wrapper.SERIALIZED)
    settings.set("use-world-coords", False)
    settings.set("reorient-shells", True)
    for node, entity in zip(source_nodes, operands):
        source = observed_revolves[node.id]
        returned = ifcopenshell.geom.create_shape(settings, entity)
        geometry = returned.geometry if hasattr(returned, "geometry") else returned
        target = cq.Shape.importBrep(io.BytesIO(geometry.brep_data.encode()))
        assert source.isValid() and target.isValid()
        assert len(source.Solids()) == len(target.Solids()) == 1
        _assert_curved_two_port_analytic_match(source, target)


@pytest.mark.parametrize("folder,sample_index,wall_override", (
    ("pipe", 0, None),          # Square-end solid needs no clipping Boolean.
    ("pipe", 0, 0.004),         # Same exact end planes, retained hollow section.
    ("pipe", 1, None),          # Two segments, known wall, actual miter clipping and union.
    ("riser", 1, None),         # Oblique-axis hollow extrusion.
    ("connector_body", 0, None), # Straight, descending end diameters.
    ("curved_two_port", 0, None), # Straight, equal end diameters.
    ("curved_two_port", 1, None), # Actual quarter-turn from the current Geometry source.
))
def test_current_exact_geometry_boundary_saved_parametric_reader(
        folder, sample_index, wall_override, tmp_path, monkeypatch):
    """Exercise the current source files, through ModelingArena and the saved IFC reader."""
    source_root = Path(__file__).resolve().parents[2] / "scenarios/engineering_product_families/geometry" / folder
    source = pack_source_bundle({name: (source_root / name).read_bytes()
                                 for name in ("interfaces.py", "main.py", "parameters.py")})
    parameters = json.loads((source_root / "validation_samples.json").read_text())[sample_index]
    if wall_override is not None:
        parameters["section"] = {"known_wall_thickness_m": [wall_override]}
    observed_revolves = {}
    if folder == "curved_two_port" and sample_index == 1:
        # Observe the actual native build operands before they leave the
        # controlled arena. They need not be shipped in the production IPC:
        # the IFC encoder consumes their construction, not duplicate BREPs.
        from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import _ConstructionTrace
        record = _ConstructionTrace.record

        def observe_record(trace, op, receiver, args, kwargs, wrapped, receiver_state=None):
            record(trace, op, receiver, args, kwargs, wrapped, receiver_state)
            if op.name == "Solid.revolve":
                node_id = trace._ids[wrapped._token]
                native = trace._arena._object(wrapped)
                observed_revolves[node_id] = cq.Shape.cast(native.wrapped.Moved(cq.Location().wrapped))

        monkeypatch.setattr(_ConstructionTrace, "record", observe_record)
    program = _compile_geometry(source, f"ifc_boundary_{folder}", "build")
    shape, _, construction = execute_geometry_build(program, parameters, capture_construction=True)
    geometry = canonical(shape)
    opened = model()
    contract = family()
    compiler = IfcOpenShellProductCompiler(opened, CompileIfcProductsCommand(family_contract=contract))
    monkeypatch.setattr(ifcopenshell.geom, "tesselate",
                        lambda *args: (_ for _ in ()).throw(AssertionError("parametric route tried TFS")))
    compiler.consume(IfcProductCompilationBlock(items=(item(contract, geometry,
        route=IfcGeometryRepresentationRoute.PARAMETRIC, construction=construction),)))
    candidate = tmp_path / f"{folder}-source-sample-{sample_index}.ifc"
    opened.write(str(candidate))
    saved = ifcopenshell.open(str(candidate))
    compiler.verify_saved(saved)
    logger = ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(saved, logger, express_rules=True)
    assert not logger.statements
    assert not saved.by_type("IfcTriangulatedFaceSet")
    if folder == "curved_two_port" and sample_index == 1:
        angle_units = [unit for unit in saved.by_type("IfcSIUnit")
                       if unit.UnitType == "PLANEANGLEUNIT"]
        assert len(angle_units) == 1 and angle_units[0].Name == "RADIAN"
        assert angle_units[0].Prefix is None
        assert any(solid.Angle == pytest.approx(np.pi / 2)
                   for solid in saved.by_type("IfcRevolvedAreaSolid"))
    if folder in ("pipe", "riser"):
        if parameters["section"]["known_wall_thickness_m"]:
            assert saved.by_type("IfcCircleHollowProfileDef")
        else:
            assert not saved.by_type("IfcCircleHollowProfileDef")
    if folder == "pipe":
        operations = {entity.Operator for entity in saved.by_type("IfcBooleanResult")}
        if sample_index == 1:
            assert {"INTERSECTION", "UNION"} <= operations
        else:
            assert not operations
    settings = ifcopenshell.geom.settings()
    settings.set("iterator-output", wrapper.SERIALIZED)
    settings.set("use-world-coords", True)
    settings.set("reorient-shells", True)
    returned = ifcopenshell.geom.create_shape(settings, saved.by_type("IfcPipeSegment")[0])
    target = cq.Shape.importBrep(io.BytesIO(returned.geometry.brep_data.encode()))
    expected = shape.rotate((0, 0, 0), (0, 0, 1), 90).translate((3, 4, 5))
    assert target.isValid() and len(target.Solids()) == len(expected.Solids())
    if folder == "curved_two_port" and sample_index == 1:
        # The frozen failing cut returned two full differences for a quarter-
        # turn whose complete analytic faces and trims correspond. Verify each
        # actual construction operand and both CSG unions instead.
        def bounds(solid):
            box = solid.BoundingBox()
            return (box.xmin, box.ymin, box.zmin, box.xmax, box.ymax, box.zmax)

        def checked_candidate(saved_model, saved_compiler):
            saved_compiler.verify_saved(saved_model)
            angle_units = [unit for unit in saved_model.by_type("IfcSIUnit")
                           if unit.UnitType == "PLANEANGLEUNIT"]
            assert len(angle_units) == 1 and angle_units[0].Name == "RADIAN"
            assert angle_units[0].Prefix is None
            assert not saved_model.by_type("IfcTriangulatedFaceSet")
            _assert_curved_two_port_revolve_operands(saved_model, construction, observed_revolves)
            product = saved_model.by_type("IfcPipeSegment")[0]
            placement = ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)
            length_unit_m = ifcopenshell.util.unit.calculate_unit_scale(saved_model)
            assert np.allclose(placement[:3, :3], ((0., -1., 0.), (1., 0., 0.), (0., 0., 1.)), atol=1e-12)
            assert tuple(placement[:3, 3] * length_unit_m) == pytest.approx((3., 4., 5.), abs=1e-12)
            settings = ifcopenshell.geom.settings()
            settings.set("iterator-output", wrapper.SERIALIZED)
            settings.set("use-world-coords", True)
            settings.set("reorient-shells", True)
            returned = ifcopenshell.geom.create_shape(settings, product)
            actual = cq.Shape.importBrep(io.BytesIO(returned.geometry.brep_data.encode()))
            assert actual.isValid() and len(actual.Solids()) == len(expected.Solids()) == 1
            assert actual.Volume() == pytest.approx(expected.Volume(), abs=1e-12)
            _assert_curved_two_port_analytic_match(expected, actual)
            # CadQuery's source bound includes the original vertex tolerance;
            # the IFC reader reports its analytic bound. Keep the bound check
            # tied to that measured source tolerance as an envelope check,
            # never as the geometric equivalence tolerance above.
            source_bound_tolerance = max(BRep_Tool.Tolerance_s(vertex.wrapped)
                                         for vertex in expected.Vertices())
            assert bounds(actual) == pytest.approx(bounds(expected),
                                                    abs=source_bound_tolerance + 1e-12)

        checked_candidate(saved, compiler)
        # Reuse the same captured Geometry shape and construction for the mm
        # project; only the IFC project's length basis changes.
        millimetre_model = model(millimetres=True)
        mm_compiler = IfcOpenShellProductCompiler(millimetre_model,
            CompileIfcProductsCommand(family_contract=contract))
        mm_compiler.consume(IfcProductCompilationBlock(items=(item(contract, geometry,
            route=IfcGeometryRepresentationRoute.PARAMETRIC, construction=construction),)))
        mm_candidate = tmp_path / "curved_two_port-source-sample-1-mm.ifc"
        millimetre_model.write(str(mm_candidate))
        mm_saved = ifcopenshell.open(str(mm_candidate))
        mm_logger = ifcopenshell.validate.json_logger()
        ifcopenshell.validate.validate(mm_saved, mm_logger, express_rules=True)
        assert not mm_logger.statements
        checked_candidate(mm_saved, mm_compiler)

        # A real changed sweep angle must fail the analytic boundary proof.
        altered = ifcopenshell.file.from_string(saved.to_string())
        altered_core = next(entity for entity in altered.by_type("IfcRevolvedAreaSolid")
                            if entity.Angle < 2 * math.pi - 1e-8)
        altered_core.Angle *= 0.8
        local = ifcopenshell.geom.settings()
        local.set("iterator-output", wrapper.SERIALIZED)
        local.set("use-world-coords", False)
        local.set("reorient-shells", True)
        altered_shape = ifcopenshell.geom.create_shape(local, altered_core)
        altered_geometry = altered_shape.geometry if hasattr(altered_shape, "geometry") else altered_shape
        altered_solid = cq.Shape.importBrep(io.BytesIO(altered_geometry.brep_data.encode()))
        core_node = next(node for node in construction.nodes
                         if node.op == "Solid.revolve" and abs(float(node.args[2])-90.) < 1e-8)
        source_core = observed_revolves[core_node.id]
        with pytest.raises(AssertionError):
            _assert_curved_two_port_analytic_match(source_core, altered_solid)
        return
    assert expected.cut(target).Volume() == pytest.approx(0., abs=1e-12)
    assert target.cut(expected).Volume() == pytest.approx(0., abs=1e-12)


@pytest.mark.parametrize("millimetres", [False, True])
def test_reused_source_pose_composes_with_fragment_and_component_at_scaled_size(millimetres):
    contract = family()
    geometry = canonical(cq.Workplane().box(1., 2., 3., centered=False).val())
    fragment = RigidTransform3D(translation_m=(2., -1., 3.),
        rotation=((1., 0., 0.), (0., 0., -1.), (0., 1., 0.)))
    source = RigidTransform3D(translation_m=(1., 2., -2.),
        rotation=((0., 1., 0.), (-1., 0., 0.), (0., 0., 1.)))
    projected = item(contract, geometry, scale=2.5,
        fragment_transform=fragment, source_transform=source)
    opened = model(millimetres=millimetres)
    compiler = IfcOpenShellProductCompiler(opened, CompileIfcProductsCommand(family_contract=contract))
    compiler.consume(IfcProductCompilationBlock(items=(projected,)))
    saved = ifcopenshell.file.from_string(opened.to_string())
    compiler.verify_saved(saved)
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    shape = ifcopenshell.geom.create_shape(settings, saved.by_type("IfcPipeSegment")[0])
    actual = np.asarray(shape.geometry.verts).reshape(-1, 3)
    expected = np.asarray([(x, y, z) for x in (0., 1.) for y in (0., 2.) for z in (0., 3.)]) * 2.5
    component = projected.component_project_world_transforms[0].project_world_transform
    for transform in (source, fragment, component):
        expected = expected @ np.asarray(transform.rotation).T + np.asarray(transform.translation_m)
    assert actual.min(axis=0) == pytest.approx(expected.min(axis=0), abs=1e-8)
    assert actual.max(axis=0) == pytest.approx(expected.max(axis=0), abs=1e-8)


@pytest.mark.parametrize("millimetres", [False, True])
def test_native_tesselate_hollow_source_rotation_scale_units_and_cross_block_maps(millimetres):
    contract = family(assembly=True, two_fragments=True)
    tube = canonical(cq.Workplane("XY").circle(0.1).circle(0.08).extrude(1.).val())
    opened = model(millimetres=millimetres)
    compiler = IfcOpenShellProductCompiler(opened, CompileIfcProductsCommand(family_contract=contract))
    compiler.consume(IfcProductCompilationBlock(items=(item(contract, tube, scale=2.),)))
    compiler.consume(IfcProductCompilationBlock(items=(item(contract, None, key="p2", scale=2.),)))
    compiler.consume(IfcProductCompilationBlock(items=(item(contract, tube, key="p3", source_key="different-exact-asset"),)))
    saved = ifcopenshell.file.from_string(opened.to_string())
    compiler.verify_saved(saved)
    assert len(saved.by_type("IfcRepresentationMap")) == 2
    assert len(saved.by_type("IfcMappedItem")) == 6
    assert len(saved.by_type("IfcRelAggregates")) == 3
    assert not saved.by_type("IfcDistributionPort")
    assert saved.by_type("IfcTriangulatedFaceSet")
    assert not saved.by_type("IfcFaceBasedSurfaceModel") and not saved.by_type("IfcAdvancedBrep")
    assert all(m.MappedRepresentation.RepresentationType == "Tessellation"
               for m in saved.by_type("IfcRepresentationMap"))
    assert all(t.Closed is None for t in saved.by_type("IfcTriangulatedFaceSet"))
    assert all(p.Representation is None for p in saved.by_type("IfcElementAssembly"))
    body = saved.by_type("IfcPipeSegment")[0]
    assert len(body.Representation.Representations[0].Items) == 2
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    shape = ifcopenshell.geom.create_shape(settings, body)
    vertices = np.asarray(shape.geometry.verts).reshape(-1, 3)
    assert vertices.min(axis=0) == pytest.approx((2.8, 3.8, 5.), abs=0.002)
    assert vertices.max(axis=0) == pytest.approx((3.2, 4.2, 7.), abs=0.002)
    # Reader preserves the tube bore: side vertices are at both radii.
    radii = np.sqrt((vertices[:, 0] - 3.)**2 + (vertices[:, 1] - 4.)**2)
    assert radii.min() == pytest.approx(0.16, abs=0.002)
    assert radii.max() == pytest.approx(0.2, abs=0.002)
    # A ray through the axial bore must not intersect a triangulated cap.
    triangles = vertices[np.asarray(shape.geometry.faces).reshape(-1, 3)]
    cap_triangles = triangles[np.ptp(triangles[:, :, 2], axis=1) < 1e-9]
    for triangle in cap_triangles:
        xy = triangle[:, :2] - (3., 4.)
        following = np.roll(xy, -1, axis=0)
        signs = xy[:, 0] * following[:, 1] - xy[:, 1] * following[:, 0]
        assert not (np.all(signs >= -1e-12) or np.all(signs <= 1e-12))
    assert compiler.effect().source_count == 2
    assert compiler.effect().body.source_coordinate_unit == "METRE"
    assert "representation_map_build" in compiler.effect().timings
    assert "tesselate" not in compiler.effect().timings


def test_multisolid_one_component_and_technical_failure_has_row_identity(monkeypatch):
    contract = family()
    compound = cq.Compound.makeCompound([cq.Workplane().box(.1, .2, .3).val(),
                                         cq.Workplane().sphere(.1).val().translate((1., 0., 0.))])
    opened = model()
    compiler = IfcOpenShellProductCompiler(opened, CompileIfcProductsCommand(family_contract=contract))
    original = ifcopenshell.geom.tesselate
    native_triangles = Counter()
    native_shells = []
    def capture(*args):
        native = original(*args)
        shells = _native_shell_triangles(native.Representations[0])
        native_shells.extend(shells)
        for shell in shells:
            native_triangles.update(shell)
        return native
    monkeypatch.setattr(ifcopenshell.geom, "tesselate", capture)
    compiler.consume(IfcProductCompilationBlock(items=(item(contract, canonical(compound)),)))
    assert len(opened.by_type("IfcPipeSegment")) == 1
    assert len(compound.Solids()) == 2
    assert len(opened.by_type("IfcTriangulatedFaceSet")) == len(native_shells)
    saved = ifcopenshell.file.from_string(opened.to_string())
    compiler.verify_saved(saved)
    assert _saved_triangles(saved) == native_triangles
    assert [_oriented_triangles([(mesh.Coordinates.CoordList, mesh.CoordIndex)])
            for mesh in saved.by_type("IfcTriangulatedFaceSet")] == native_shells
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    shape = ifcopenshell.geom.create_shape(settings, saved.by_type("IfcPipeSegment")[0])
    vertices = np.asarray(shape.geometry.verts).reshape(-1, 3)
    assert vertices.min(axis=0) == pytest.approx((2.9, 3.95, 4.85), abs=.002)
    assert vertices.max(axis=0) == pytest.approx((3.1, 5.1, 5.15), abs=.002)
    assert compiler.effect().triangle_count > 12
    with pytest.raises(IfcProductCompilationError) as caught:
        compiler.consume(IfcProductCompilationBlock(items=(item(contract, None, key="p2", source_key="missing"),)))
    assert caught.value.diagnostic["product_key"] == "p2"
    assert caught.value.diagnostic["input_row_index"] == 1


def test_actual_open_face_native_triangles_preserved_in_saved_tessellation(monkeypatch):
    shape = cq.Face.makePlane(.2, .3)
    geometry = canonical(shape)
    original = ifcopenshell.geom.tesselate
    native_triangles = Counter()
    def capture(*args):
        native = original(*args)
        native_triangles.update(_native_triangles(native.Representations[0]))
        return native
    monkeypatch.setattr(ifcopenshell.geom, "tesselate", capture)
    opened = model()
    contract = family()
    compiler = IfcOpenShellProductCompiler(opened, CompileIfcProductsCommand(family_contract=contract))
    compiler.consume(IfcProductCompilationBlock(items=(item(contract, geometry),)))
    saved = ifcopenshell.file.from_string(opened.to_string())
    compiler.verify_saved(saved)
    assert _saved_triangles(saved) == native_triangles
    assert compiler.effect().triangle_count == 2
    assert len(saved.by_type("IfcTriangulatedFaceSet")) == 1
    assert saved.by_type("IfcTriangulatedFaceSet")[0].Closed is None


class ByteSource:
    def __init__(self, content):
        self.content, self.size_bytes = content, len(content)
        self.sha256 = hashlib.sha256(content).hexdigest()
    async def iter_chunks(self, *, chunk_size=1024 * 1024):
        yield self.content


@pytest.mark.asyncio
async def test_real_revision_worker_streams_blocks_preflights_and_releases_candidate(tmp_path):
    contract = family()
    source = ByteSource(model(millimetres=True).to_string().encode())
    geometry = canonical(cq.Workplane().box(.1, .2, .3).val())
    closed = []
    async def blocks():
        try:
            yield IfcProductCompilationBlock(items=(item(contract, geometry),))
            yield IfcProductCompilationBlock(items=(item(contract, None, key="p2"),))
        finally:
            closed.append(True)
    runtime = IfcOpenShellRevisionRuntime(python_executable=sys.executable, capacity_root=tmp_path / "capacity")
    try:
        body = await runtime.execute(InspectExactIfcRevisionRequest(projection="body"), source=source)
        assert body.body.project_length_unit_m == .001
        result = await runtime.execute(CompileIfcProductsRevisionRequest(
            command=CompileIfcProductsCommand(family_contract=contract), blocks=blocks()), source=source)
        assert result.effect.source_count == 1 and result.effect.block_count == 2
        assert len(result.effect.bindings) == 2
        content = b"".join([c async for c in result.candidate.iter_chunks()])
        saved = ifcopenshell.file.from_string(content.decode())
        assert len(saved.by_type("IfcRepresentationMap")) == 1
        assert len(saved.by_type("IfcPipeSegment")) == 2
        assert len(saved.by_type("IfcTriangulatedFaceSet")) == 1
        assert not saved.by_type("IfcFaceBasedSurfaceModel")
        assert closed == [True]
        candidate_root = result.candidate._root
        await result.candidate.aclose()
        assert not candidate_root.exists()
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_block_index", [0, 1])
async def test_worker_first_or_later_block_error_stops_without_candidate_and_closes_stream(tmp_path, fault_block_index):
    from CAD2BIM_backend.ports.ifc_revision_runtime import IfcRevisionRuntimeError
    contract = family()
    source = ByteSource(model().to_string().encode())
    geometry = canonical(cq.Workplane().box(.1, .2, .3).val())
    closed = []
    async def blocks():
        try:
            if fault_block_index:
                yield IfcProductCompilationBlock(items=(item(contract, geometry),))
            yield IfcProductCompilationBlock(items=(item(contract, None, key=f"p{fault_block_index + 1}", source_key="unavailable"),))
            await asyncio.Event().wait()
        finally:
            closed.append(True)
    runtime = IfcOpenShellRevisionRuntime(python_executable=sys.executable, capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(IfcRevisionRuntimeError) as caught:
            await runtime.execute(CompileIfcProductsRevisionRequest(
                command=CompileIfcProductsCommand(family_contract=contract), blocks=blocks()), source=source)
        assert caught.value.code == "IFC_PRODUCT_COMPILATION_FAILED"
        assert caught.value.retryable is False
        assert caught.value.diagnostic["input_row_index"] == fault_block_index
        assert caught.value.diagnostic["product_key"] == f"p{fault_block_index + 1}"
        assert closed == [True] and not runtime._handles
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_worker_cancellation_releases_backpressured_stream_and_private_files(tmp_path, monkeypatch):
    import CAD2BIM_backend.infrastructure.ifc_revision_runtime.adapter as adapter
    roots = []
    original = adapter.tempfile.mkdtemp
    def private_directory(*args, **kwargs):
        value = original(*args, **kwargs)
        roots.append(Path(value))
        return value
    monkeypatch.setattr(adapter.tempfile, "mkdtemp", private_directory)
    contract = family()
    entered, closed = asyncio.Event(), []
    geometry = canonical(cq.Workplane().box(.1, .2, .3).val())
    async def blocks():
        try:
            yield IfcProductCompilationBlock(items=(item(contract, geometry),))
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.append(True)
    runtime = IfcOpenShellRevisionRuntime(python_executable=sys.executable, capacity_root=tmp_path / "capacity")
    task = asyncio.create_task(runtime.execute(CompileIfcProductsRevisionRequest(
        command=CompileIfcProductsCommand(family_contract=contract), blocks=blocks()),
        source=ByteSource(model().to_string().encode())))
    try:
        await asyncio.wait_for(entered.wait(), timeout=15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True] and not runtime._handles
        assert roots and all(not path.exists() for path in roots)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_product_block_byte_budget_rejects_before_serialization_without_candidate(tmp_path, monkeypatch):
    import CAD2BIM_backend.infrastructure.ifc_revision_runtime.adapter as adapter
    from CAD2BIM_backend.ports.ifc_revision_runtime import IfcRevisionRuntimeError
    contract = family()
    geometry = canonical(cq.Workplane().box(.1, .2, .3).val())
    monkeypatch.setattr(adapter, "MAX_IFC_PRODUCT_BLOCK_BYTES", len(geometry.payload) - 1)
    async def blocks():
        yield IfcProductCompilationBlock(items=(item(contract, geometry),))
    runtime = IfcOpenShellRevisionRuntime(python_executable=sys.executable, capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(IfcRevisionRuntimeError) as caught:
            await runtime.execute(CompileIfcProductsRevisionRequest(
                command=CompileIfcProductsCommand(family_contract=contract), blocks=blocks()),
                source=ByteSource(model().to_string().encode()))
        assert caught.value.code == "IFC_PRODUCT_INPUT_BUDGET_EXCEEDED"
        assert caught.value.diagnostic["block_index"] == 0
        assert not runtime._handles
    finally:
        await runtime.close()
