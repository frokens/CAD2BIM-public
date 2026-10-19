from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import replace
from pathlib import Path

import cadquery as cq
import ifcopenshell
import pytest
from cadquery import exporters
from PIL import Image

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometryFormat,
    GeometryRenderCoordinateSpace,
    GeometryRenderHostOverlay,
    GeometryRenderInterfaceMarkerKind,
    GeometryRenderInterfaceOverlay,
    GeometryRenderScene,
    GeometryRenderSourcePathOverlay,
    GeometryRenderStraightProfileOverlay,
    GeometryNormalizeRequest,
    GeometryPreviewView,
    GeometryQueryRequest,
    GeometryRenderRequest,
    GeometryRuntimeValidationError,
    GeometryRuntimeResourceLimitError,
    GeometrySourceFormat,
)
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.geometry_interface import CircularInterfaceProfile
from CAD2BIM_backend.infrastructure.geometry_runtime import codec as geometry_runtime_codec
from CAD2BIM_backend.infrastructure.geometry_runtime import user_geometry as user_geometry_policy
from CAD2BIM_backend.infrastructure.geometry_runtime.codec_child import execute_render_request
from CAD2BIM_backend.infrastructure.geometry_runtime.adapter import (
    GEOMETRY_QUERY_BUDGET,
    OcpCadQueryGeometryRuntime,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.codec import (
    mesh_for_render,
    normalize_geometry,
    reopen_geometry,
    summarize_shape,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.render import render_mesh



_CANONICAL_REOPEN_FIXTURES = (
    Path(__file__).resolve().parents[2] / "fixtures" / "geometry_runtime" / "canonical_reopen"
)
def _mesh_payload(*, reordered: bool = False) -> bytes:
    if reordered:
        document = {
            "triangles": [[1, 0, 3], [1, 2, 0]],
            "vertices": [[1, 1, 0], [0, 0, 0], [1, 0, 0], [0, 1, 0]],
            "unit": "METRE",
            "schema": "indexed_triangle_mesh_v1",
        }
    else:
        document = {
            "schema": "indexed_triangle_mesh_v1",
            "unit": "METRE",
            "vertices": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]],
            "triangles": [[0, 1, 2], [0, 2, 3]],
        }
    return json.dumps(document, indent=2).encode()


@pytest.mark.parametrize("case", ["curved", "located", "reversed", "compound"])
def test_indexed_triangulation_matches_cadquery_vertices_and_oriented_indices(case: str, monkeypatch) -> None:
    cylinder = cq.Solid.makeCylinder(0.04, 0.1)
    if case == "curved":
        shape = cylinder
    elif case == "located":
        shape = cylinder.moved(cq.Location(cq.Vector(1.25, -0.5, 2), cq.Vector(1, 2, 3), 37))
    elif case == "reversed":
        shape = cq.Shape.cast(cylinder.Faces()[0].wrapped.Reversed())
    else:
        shape = cq.Compound.makeCompound(
            [cylinder, cq.Solid.makeBox(0.2, 0.3, 0.4).translate((0.3, -0.7, 0.2))]
        )

    reference_vertices, reference_triangles = shape.tessellate(0.001, 0.1)
    face_reads = []
    original_faces = cq.Shape.Faces

    def faces(value):
        face_reads.append(value)
        return original_faces(value)

    monkeypatch.setattr(cq.Shape, "Faces", faces)
    actual = mesh_for_render(shape)

    assert len({id(value) for value in face_reads}) == len(face_reads)
    assert actual.vertices == [[v.x, v.y, v.z] for v in reference_vertices]
    assert actual.triangles == [list(triangle) for triangle in reference_triangles]
    assert summarize_shape(shape).topology.triangle_count is None


@pytest.mark.parametrize("mapping_error", ["missing_face", "wrong_identity"])
def test_display_copy_rejects_unconfirmed_face_mapping(monkeypatch, mapping_error) -> None:
    shape = cq.Solid.makeBox(1, 1, 1)
    original_faces = cq.Shape.Faces

    def faces(value):
        result = original_faces(value)
        if value is shape:
            return result
        if mapping_error == "missing_face":
            return result[:-1]
        return [result[0].moved(cq.Location(cq.Vector(1, 0, 0))), *result[1:]]

    monkeypatch.setattr(cq.Shape, "Faces", faces)
    with pytest.raises(GeometryRuntimeValidationError, match="display copy face mapping could not be confirmed"):
        mesh_for_render(shape)


def test_edge_only_shape_retains_summary_without_becoming_renderable() -> None:
    shape = cq.Edge.makeLine((0, 0, 0), (1, 2, 3))

    summary = summarize_shape(shape)

    assert summary.topology.edge_count == 1
    assert summary.topology.triangle_count is None
    with pytest.raises(GeometryRuntimeValidationError, match="no renderable faces"):
        mesh_for_render(shape)


@pytest.mark.parametrize("placement", ["original", "located", "reversed"])
def test_trimmed_extrusion_preview_has_no_spurious_spanning_triangles(placement: str) -> None:
    fixture = Path(__file__).resolve().parents[2] / "fixtures/geometry/trimmed-extrusion-face.brep"
    payload = fixture.read_bytes()
    face = cq.Shape.importBrep(str(fixture))
    if placement == "located":
        face = face.moved(cq.Location(cq.Vector(2, -3, 4), cq.Vector(1, 2, 3), 37))
    elif placement == "reversed":
        face = cq.Shape.cast(face.wrapped.Reversed())
    assert face.isValid()
    mesh = mesh_for_render(face)
    areas_and_centres = []
    for triangle in mesh.triangles:
        a, b, c = [cq.Vector(*mesh.vertices[i]) for i in triangle]
        areas_and_centres.append(((b - a).cross(c - a).Length / 2, (a + b + c) / 3))
    assert sum(area for area, _ in areas_and_centres) == pytest.approx(face.Area(), rel=0.002)
    # Original corruption spans about 0.4 m away from the authoritative face.
    for _, centre in sorted(areas_and_centres, key=lambda value: value[0])[-8:]:
        assert face.distance(cq.Vertex.makeVertex(*centre.toTuple())) < 0.002
    assert fixture.read_bytes() == payload


@pytest.mark.parametrize("kind", ["missing", "nonfinite", "illegal_index"])
def test_unrecoverable_face_triangulation_fails_after_one_bounded_retry(monkeypatch, kind) -> None:
    reads = []

    def missing_mesh(face):
        reads.append(face)
        if kind != "missing":
            return geometry_runtime_codec.MeshData(
                vertices=[[0, 0, 0], [1, 0, 0], [0, float("nan") if kind == "nonfinite" else 1, 0]],
                triangles=[[0, 1, -1 if kind == "illegal_index" else 2]],
            )
        return geometry_runtime_codec.MeshData(vertices=[], triangles=[])

    monkeypatch.setattr(geometry_runtime_codec, "_read_existing_triangulation", missing_mesh)
    with pytest.raises(GeometryRuntimeValidationError, match="no safe renderable triangulation after one retry"):
        mesh_for_render(cq.Face.makePlane(1, 1))
    assert len(reads) == 2


@pytest.mark.parametrize("retry_kind", ["closer", "worse", "missing", "illegal_indices"])
def test_renderable_area_mismatch_returns_warning_png_and_preserves_canonical(tmp_path, monkeypatch, retry_kind):
    face = cq.Face.makePlane(1, 1)
    path = tmp_path / "plane.brep"
    face.exportBrep(str(path))
    payload = path.read_bytes()
    geometry = normalize_geometry(GeometryNormalizeRequest(
        format=CanonicalGeometryFormat.OCCT_BREP_V1, payload=payload,
    ))
    def candidate(height):
        return geometry_runtime_codec.MeshData(
            vertices=[[0, 0, 0], [2, 0, 0], [0, height, 0]], triangles=[[0, 1, 2]],
        )
    original = candidate(2)
    retry = candidate(1.2 if retry_kind == "closer" else 3)
    if retry_kind == "missing":
        retry = geometry_runtime_codec.MeshData(vertices=[], triangles=[])
    if retry_kind == "illegal_indices":
        retry.triangles = [[0, 1, -1]]
    reads = iter((original, retry))
    monkeypatch.setattr(geometry_runtime_codec, "_read_existing_triangulation", lambda face: next(reads))
    preview = execute_render_request(GeometryRenderRequest(geometry=geometry))
    assert preview.payload.startswith(b"\x89PNG")
    warning, = preview.display_quality_warnings
    assert warning.geometry_sha256 == geometry.sha256
    assert warning.native_area_m2 == pytest.approx(1)
    assert warning.initial_relative_difference == pytest.approx(1)
    assert warning.selected_attempt == ("retry" if retry_kind == "closer" else "initial")
    assert warning.retry_count == 1
    if retry_kind == "closer":
        assert warning.retry_relative_difference == pytest.approx(0.2)
    elif retry_kind in {"missing", "illegal_indices"}:
        assert warning.retry_error == "unsafe_or_missing_retry_mesh"
    assert geometry.payload == payload == path.read_bytes()
    assert "DISPLAY_MESH_AREA_WARNING" in preview.model_dump_json(exclude={"payload"})


@pytest.mark.parametrize("points,crossings", [
    ([(0.,0.),(1.,1.),(0.,1.),(1.,0.),(0.,0.)], 1),
    ([(0.,0.),(1.,0.),(1.,1e-10),(0.,1e-10),(0.,0.)], 0),
    ([(0.,0.),(1.,0.),(.5,.5),(1.,1.),(0.,1.),(.5,.5),(0.,0.)], 0),
])
def test_display_boundary_crossing_is_independent_of_area_and_excludes_touch(points, crossings):
    face = cq.Face.makePlane(1, 1)
    facts = {"face_index": 0, "surface_type": "PLANE", "native_area_m2": 1.,
             "initial_relative_difference": 0., "selected_attempt": "initial", "retry_error": None,
             "mesh_available": True, "quality_candidate": False}
    observation = geometry_runtime_codec._DisplayFace(face, face, facts, ((tuple(points), True),))
    mesh = geometry_runtime_codec.MeshData([[0,0,0],[1,0,0],[0,1,0]], [[0,1,2]], display_faces=(observation,))
    # Preview candidate checks do not pretend to be a full scan. Explicit Query
    # checks this area-matching face, then array consumers must honor its result.
    geometry_runtime_codec.require_reliable_display_mesh(mesh)
    report = geometry_runtime_codec.display_diagnostics(mesh)
    assert report["completed_face_count"] == 1
    assert observation.diagnostic["discrete_boundary"]["crossing_segment_pairs"] == crossings
    if crossings:
        assert observation.diagnostic["native_boundary"]["status"] == "no_issue_reported"
        with pytest.raises(GeometryRuntimeValidationError, match="Display boundary failed"):
            geometry_runtime_codec.require_reliable_display_mesh(mesh)
    else:
        geometry_runtime_codec.require_reliable_display_mesh(mesh)


def test_display_copy_preserves_real_holes_narrow_faces_and_source_topology():
    tube = cq.Solid.makeCylinder(.04, .1).cut(cq.Solid.makeCylinder(.03999, .1))
    shape = cq.Compound.makeCompound([tube, cq.Face.makePlane(.2, 1e-5, (0.,0.,.2))])
    before = summarize_shape(shape)
    mesh = mesh_for_render(shape)
    assert summarize_shape(shape) == before
    assert sum(len(face.Wires()) for face in shape.Faces()) == sum(len(o.selected.Wires()) for o in mesh.display_faces)
    # Inner opening survives: no triangle spans the axis on either annular cap.
    for observation in mesh.display_faces:
        if observation.native.geomType() == "PLANE" and len(observation.native.Wires()) == 2:
            cap = geometry_runtime_codec._read_existing_triangulation(observation.selected)
            for triangle in cap.triangles:
                centre = [sum(cap.vertices[i][axis] for i in triangle)/3 for axis in range(3)]
                assert math.hypot(centre[0], centre[1]) > .039
    report = geometry_runtime_codec.display_diagnostics(mesh)
    assert report["status"] == "incomplete"  # Cylinder periodicity is honestly unsupported.
    assert all(f["discrete_boundary"]["status"] != "crossing_reported" for f in report["faces"])
    geometry_runtime_codec.require_reliable_display_mesh(mesh)


def test_native_open_wire_issue_and_incomplete_discrete_boundary_are_separate():
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Face
    from OCP.Geom import Geom_Plane
    from OCP.gp import gp_Pln, gp_Pnt, gp_Dir
    wrapped = TopoDS_Face()
    builder = BRep_Builder()
    builder.MakeFace(wrapped, Geom_Plane(gp_Pln(gp_Pnt(0,0,0), gp_Dir(0,0,1))), 1e-7)
    wire = cq.Wire.makePolygon([cq.Vector(0,0,0),cq.Vector(1,0,0),cq.Vector(1,1,0)], close=False)
    builder.Add(wrapped, wire.wrapped)
    face = cq.Face(wrapped)
    observation = geometry_runtime_codec._DisplayFace(face, face,
        {"face_index": 0, "surface_type": "PLANE", "native_area_m2": .5, "selected_attempt": "initial",
         "retry_error": None, "mesh_available": False, "quality_candidate": True},
        ((((0.,0.),(1.,0.),(1.,1.)), True),))
    result = geometry_runtime_codec._check_display_face(observation)
    assert result["native_boundary"]["status"] == "issue_reported"
    assert any("NotClosed" in status for status in result["native_boundary"]["reported_statuses"])
    assert result["discrete_boundary"]["status"] == "inconclusive"
    assert result["discrete_boundary"]["crossing_segment_pairs"] is None


@pytest.mark.parametrize("solid_first", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_mixed_free_face_and_solid_has_no_whole_object_volume(solid_first: bool, nested: bool) -> None:
    solid = cq.Solid.makeBox(1, 1, 1)
    face = cq.Face.makePlane(1, 1, (0, 0, 2))
    parts = [solid, face] if solid_first else [face, solid]
    if nested:
        parts = [cq.Compound.makeCompound([p]) for p in parts]
    summary = summarize_shape(cq.Compound.makeCompound(parts))
    assert summary.topology.solid_count == 1
    assert summary.surface_area_m2 == pytest.approx(7)
    assert summary.volume_m3 is None
    assert solid.Volume() == pytest.approx(1)


def test_nested_pure_solids_keep_volume_but_free_shell_does_not() -> None:
    solid = cq.Solid.makeBox(1, 1, 1)
    pure = cq.Compound.makeCompound([solid, cq.Compound.makeCompound([solid.translate((2, 0, 0))])])
    assert summarize_shape(pure).volume_m3 == pytest.approx(2)
    assert summarize_shape(solid.Shells()[0]).volume_m3 is None
    mixed = cq.Compound.makeCompound([solid, solid.Shells()[0].translate((2, 0, 0))])
    assert summarize_shape(mixed).volume_m3 is None


def _box_brep(tmp_path: Path) -> bytes:
    path = tmp_path / "box.brep"
    exporters.export(cq.Workplane("XY").box(1, 2, 3).val(), str(path), exportType="BREP")
    return path.read_bytes()


def _single_element_ifc(*, rich_metadata: bool, millimetres: bool = False) -> bytes:
    model = ifcopenshell.file(schema="IFC4X3")
    origin = model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))
    world = model.create_entity("IfcAxis2Placement3D", Location=origin)
    context = model.create_entity(
        "IfcGeometricRepresentationContext",
        ContextIdentifier="Body",
        ContextType="Model",
        CoordinateSpaceDimension=3,
        Precision=1e-5,
        WorldCoordinateSystem=world,
    )
    extent = 1000.0 if millimetres else 1.0
    coordinates = model.create_entity(
        "IfcCartesianPointList3D",
        CoordList=(
            (0.0, 0.0, 0.0),
            (extent, 0.0, 0.0),
            (extent, extent, 0.0),
            (0.0, extent, 0.0),
        ),
    )
    faces = model.create_entity(
        "IfcTriangulatedFaceSet",
        Coordinates=coordinates,
        Closed=False,
        CoordIndex=((1, 2, 3), (1, 3, 4)),
    )
    shape = model.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=context,
        RepresentationIdentifier="Body",
        RepresentationType="Tessellation",
        Items=(faces,),
    )
    representation = model.create_entity("IfcProductDefinitionShape", Representations=(shape,))
    local_placement = model.create_entity(
        "IfcLocalPlacement",
        RelativePlacement=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity(
                "IfcCartesianPoint",
                Coordinates=((1000.0, 2000.0, 3000.0) if rich_metadata else (0.0, 0.0, 0.0)),
            ),
        ),
    )
    element = model.create_entity(
        "IfcBuildingElementProxy",
        GlobalId=("0123456789ABCDEFGHIJKL" if rich_metadata else "abcdefghijklmnopqrstuv"),
        Name=("SECRET_PRODUCT_NAME" if rich_metadata else "plain"),
        ObjectPlacement=local_placement,
        Representation=representation,
    )
    if millimetres:
        length_unit = model.create_entity(
            "IfcSIUnit",
            UnitType="LENGTHUNIT",
            Prefix="MILLI",
            Name="METRE",
        )
        units = model.create_entity("IfcUnitAssignment", Units=(length_unit,))
        model.create_entity(
            "IfcProject",
            GlobalId="4234567890ABCDEFGHIJKL",
            RepresentationContexts=(context,),
            UnitsInContext=units,
        )
    if rich_metadata:
        value = model.create_entity(
            "IfcPropertySingleValue",
            Name="SECRET_PROPERTY",
            NominalValue=model.create_entity("IfcLabel", "SECRET_VALUE"),
        )
        pset = model.create_entity(
            "IfcPropertySet",
            GlobalId="1234567890ABCDEFGHIJKL",
            Name="SECRET_PSET",
            HasProperties=(value,),
        )
        model.create_entity(
            "IfcRelDefinesByProperties",
            GlobalId="2234567890ABCDEFGHIJKL",
            RelatedObjects=(element,),
            RelatingPropertyDefinition=pset,
        )
        material = model.create_entity("IfcMaterial", Name="SECRET_MATERIAL")
        model.create_entity(
            "IfcRelAssociatesMaterial",
            GlobalId="3234567890ABCDEFGHIJKL",
            RelatedObjects=(element,),
            RelatingMaterial=material,
        )
    return model.to_string().encode("utf-8")


def test_indexed_mesh_is_canonical_across_vertex_and_face_order() -> None:
    first = normalize_geometry(
        GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1,
            payload=_mesh_payload(),
        )
    )
    second = normalize_geometry(
        GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1,
            payload=_mesh_payload(reordered=True),
        )
    )

    assert first.payload == second.payload
    assert first.sha256 == second.sha256
    assert first.summary == second.summary
    assert first.summary.bounds.minimum_m == (0.0, 0.0, 0.0)
    assert first.summary.bounds.maximum_m == (1.0, 1.0, 0.0)
    assert first.summary.topology.triangle_count == 2
    assert first.summary.surface_area_m2 == 1.0


def test_mesh_rejects_non_finite_and_degenerate_geometry() -> None:
    non_finite = json.dumps(
        {
            "schema": "indexed_triangle_mesh_v1",
            "unit": "METRE",
            "vertices": [[0, 0, 0], [1, 0, 0], [float("inf"), 1, 0]],
            "triangles": [[0, 1, 2]],
        }
    ).encode()
    with pytest.raises(GeometryRuntimeValidationError, match="finite"):
        normalize_geometry(
            GeometryNormalizeRequest(
                format=CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1,
                payload=non_finite,
            )
        )

    degenerate = json.dumps(
        {
            "schema": "indexed_triangle_mesh_v1",
            "unit": "METRE",
            "vertices": [[0, 0, 0], [1, 0, 0], [2, 0, 0]],
            "triangles": [[0, 1, 2]],
        }
    ).encode()
    with pytest.raises(GeometryRuntimeValidationError, match="non-degenerate"):
        normalize_geometry(
            GeometryNormalizeRequest(
                format=CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1,
                payload=degenerate,
            )
        )


def test_no_summary_brep_normalize_avoids_cadquery_render_stack_in_fresh_interpreter():
    import subprocess
    import sys

    source_root = Path(__file__).resolve().parents[3] / "src"
    fixture = _CANONICAL_REOPEN_FIXTURES / "mapped_body_canonical.brep"
    script = """import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from CAD2BIM_backend.domain.geometry_runtime import GeometryNormalizeRequest
from CAD2BIM_backend.infrastructure.geometry_runtime.codec import normalize_geometry
payload = Path(sys.argv[2]).read_bytes()
geometry = normalize_geometry(GeometryNormalizeRequest(format='occt_brep_v1', payload=payload, include_summary=False))
assert geometry.payload == payload and geometry.summary is None
assert 'cadquery' not in sys.modules
assert not any(name.startswith(('vtkmodules', 'PIL')) for name in sys.modules)
print('actual BREP validated without CadQuery/VTK/PIL Python stack')
"""
    completed = subprocess.run([sys.executable, "-I", "-c", script, str(source_root), str(fixture)],
        capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("kind", ["vertex", "edge", "face", "solid", "empty"])
def test_native_read_and_cadquery_wrapper_share_nonempty_validation(kind):
    shape = {"vertex": lambda: cq.Vertex.makeVertex(0, 0, 0),
             "edge": lambda: cq.Edge.makeLine((0, 0, 0), (1, 0, 0)),
             "face": lambda: cq.Face.makePlane(1, 1),
             "solid": lambda: cq.Solid.makeBox(1, 1, 1),
             "empty": lambda: cq.Compound.makeCompound([])}[kind]()
    payload = user_geometry_policy.serialize_user_shape(shape)
    if kind == "empty":
        for reader in (user_geometry_policy.read_native_shape, user_geometry_policy.reopen_user_shape):
            with pytest.raises(ValueError, match="empty shape"):
                reader(payload)
    else:
        native = user_geometry_policy.read_native_shape(payload)
        wrapped = user_geometry_policy.reopen_user_shape(payload)
        user_geometry_policy.validate_native_shape(native)
        user_geometry_policy.validate_user_shape(wrapped)
        user_geometry_policy.validate_user_shape(cq.Workplane(obj=wrapped))
        user_geometry_policy.validate_user_shape(native)


def test_occt_brep_normalize_reopen_and_summary_are_deterministic(tmp_path: Path) -> None:
    request = GeometryNormalizeRequest(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        payload=_box_brep(tmp_path),
    )

    first = normalize_geometry(request)
    second = normalize_geometry(request)
    _, reopened = reopen_geometry(first)

    with pytest.raises(ValueError, match="content type"):
        type(first).model_validate(
            {**first.model_dump(), "content_type": "application/octet-stream"}
        )

    assert first.payload == second.payload
    assert first.sha256 == second.sha256
    assert reopened == first.summary
    assert reopened.bounds.minimum_m == (-0.5, -1.0, -1.5)
    assert reopened.bounds.maximum_m == (0.5, 1.0, 1.5)
    assert reopened.topology.vertex_count == 8
    assert reopened.topology.edge_count == 12
    assert reopened.topology.face_count == 6
    assert reopened.topology.solid_count == 1
    assert reopened.surface_area_m2 == 22.0
    assert reopened.volume_m3 == 6.0


@pytest.mark.parametrize("fixture_name", [
    "oblique_boolean_raw.brep", "spherical_junction_raw.brep",
    "inline_loft_raw.brep", "mapped_body_canonical.brep",
])
def test_saved_brep_accepts_historical_shapes_without_rewriting_or_meshing(
    fixture_name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _CANONICAL_REOPEN_FIXTURES / fixture_name
    payload = path.read_bytes()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("exact BREP read must neither rewrite nor tessellate")

    monkeypatch.setattr(cq.Shape, "mesh", forbidden)
    monkeypatch.setattr(user_geometry_policy, "serialize_user_shape", forbidden)
    geometry = normalize_geometry(GeometryNormalizeRequest(
        format=CanonicalGeometryFormat.OCCT_BREP_V1, payload=payload,
    ))
    shape, summary = reopen_geometry(geometry)
    assert shape.isValid()
    assert geometry.payload == payload == path.read_bytes()
    assert geometry.sha256 == hashlib.sha256(payload).hexdigest()
    assert summary.topology.triangle_count is None
    assert summary.topology.face_count > 0
    # Persisted display counts and floating summaries are historical metadata.
    legacy = geometry.model_copy(update={"summary": geometry.summary.model_copy(update={
        "topology": geometry.summary.topology.model_copy(update={"triangle_count": 12345}),
        "surface_area_m2": geometry.summary.surface_area_m2 + 1e-10,
    })})
    assert reopen_geometry(legacy)[0].isValid()


@pytest.mark.parametrize("payload", [b"not a BREP", b"DBRep_DrawableShape\n", b"CASCADE Topology V3\n"])
def test_brep_rejects_unreadable_payload(payload: bytes) -> None:
    with pytest.raises(GeometryRuntimeValidationError, match="reopened"):
        normalize_geometry(GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.OCCT_BREP_V1, payload=payload,
        ))


def test_brep_rejects_invalid_topology_and_transport_digest(tmp_path: Path) -> None:
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Solid, TopoDS_Shell

    native = TopoDS_Solid()
    builder = BRep_Builder()
    builder.MakeSolid(native)
    # A lone planar face is not a closed shell bounding a valid solid.
    shell = TopoDS_Shell()
    builder.MakeShell(shell)
    builder.Add(shell, cq.Solid.makeBox(1, 1, 1).Faces()[0].wrapped)
    builder.Add(native, shell)
    invalid = cq.Shape(native)
    assert not invalid.isValid()
    payload = user_geometry_policy.serialize_user_shape(invalid)
    with pytest.raises(GeometryRuntimeValidationError, match="invalid"):
        normalize_geometry(GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.OCCT_BREP_V1, payload=payload,
        ))
    geometry = normalize_geometry(GeometryNormalizeRequest(
        format=CanonicalGeometryFormat.OCCT_BREP_V1, payload=_box_brep(tmp_path),
    ))
    with pytest.raises(GeometryRuntimeValidationError, match="SHA-256"):
        reopen_geometry(geometry.model_copy(update={"sha256": "0" * 64}))


def test_occt_brep_serializer_variation_does_not_block_saved_geometry(
    tmp_path: Path,
) -> None:
    path = (
        cq.Workplane("XZ")
        .moveTo(0.0, 0.0)
        .lineTo(0.161, 0.0)
        .lineTo(0.161, 0.16)
        .lineTo(0.46, 0.16)
    )
    angle_degrees = math.degrees(math.atan2(1.2, 1.5))
    tube = (
        cq.Workplane("YZ")
        .circle(0.006)
        .sweep(path, transition="round")
        .rotate((0, 0, 0), (1, 0, 0), angle_degrees)
    )
    source_path = tmp_path / "two-cycle-source.brep"
    exporters.export(tube.val(), str(source_path), exportType="BREP")
    request = GeometryNormalizeRequest(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        source_format=GeometrySourceFormat.OCCT_BREP,
        payload=source_path.read_bytes(),
    )

    first = normalize_geometry(request)
    second = normalize_geometry(request)
    reopened_shape, reopened_summary = reopen_geometry(first)
    normalized_reopen = normalize_geometry(
        GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.OCCT_BREP_V1,
            source_format=GeometrySourceFormat.OCCT_BREP,
            payload=first.payload,
        )
    )

    assert first == second == normalized_reopen
    assert reopened_summary == first.summary
    assert len(reopened_shape.Solids()) == 1
    assert first.summary.topology.solid_count == 1
    assert first.summary.topology.boundary_edge_count == 0


def test_ifc_source_extracts_only_local_geometry_into_canonical_mesh() -> None:
    plain = normalize_geometry(
        GeometryNormalizeRequest(
            source_format=GeometrySourceFormat.IFC,
            format=CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1,
            payload=_single_element_ifc(rich_metadata=False),
        )
    )
    rich = normalize_geometry(
        GeometryNormalizeRequest(
            source_format=GeometrySourceFormat.IFC,
            format=CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1,
            payload=_single_element_ifc(rich_metadata=True),
        )
    )

    assert rich.payload == plain.payload
    assert rich.sha256 == plain.sha256
    assert rich.summary == plain.summary
    assert rich.summary.bounds.minimum_m == (0.0, 0.0, 0.0)
    assert rich.summary.bounds.maximum_m == (1.0, 1.0, 0.0)
    for forbidden in (
        b"SECRET_PRODUCT_NAME",
        b"SECRET_PROPERTY",
        b"SECRET_VALUE",
        b"SECRET_PSET",
        b"SECRET_MATERIAL",
        b"0123456789ABCDEFGHIJKL",
    ):
        assert forbidden not in rich.payload


def test_ifc_source_units_are_normalized_to_metres_at_importer_root() -> None:
    geometry = normalize_geometry(
        GeometryNormalizeRequest(
            source_format=GeometrySourceFormat.IFC,
            format=CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1,
            payload=_single_element_ifc(rich_metadata=False, millimetres=True),
        )
    )

    assert geometry.summary.bounds.minimum_m == (0.0, 0.0, 0.0)
    assert geometry.summary.bounds.maximum_m == (1.0, 1.0, 0.0)
    assert geometry.summary.surface_area_m2 == 1.0


def _native_element_ifc() -> bytes:
    model = ifcopenshell.file.from_string(
        _single_element_ifc(rich_metadata=True, millimetres=True).decode()
    )
    element = model.by_type("IfcBuildingElementProxy")[0]
    body = element.Representation.Representations[0]
    profile = model.create_entity("IfcRectangleProfileDef", ProfileType="AREA", XDim=1000.0, YDim=2000.0)
    direction = model.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0))
    box = model.create_entity("IfcExtrudedAreaSolid", SweptArea=profile, ExtrudedDirection=direction, Depth=3000.0)
    circle = model.create_entity("IfcCircleProfileDef", ProfileType="AREA", Radius=200.0)
    position = model.create_entity("IfcAxis2Placement3D", Location=model.create_entity("IfcCartesianPoint", Coordinates=(2000.0, 0.0, 0.0)))
    cylinder = model.create_entity("IfcExtrudedAreaSolid", SweptArea=circle, Position=position, ExtrudedDirection=direction, Depth=500.0)
    body.Items = (box, cylinder)
    body.RepresentationType = "SweptSolid"
    element.ObjectPlacement.RelativePlacement.RefDirection = model.create_entity("IfcDirection", DirectionRatios=(0.0, 1.0, 0.0))
    return model.to_string().encode()


def test_ifc_native_brep_keeps_all_items_units_and_original_placement() -> None:
    source = _native_element_ifc()
    geometry = normalize_geometry(GeometryNormalizeRequest(
        source_format=GeometrySourceFormat.IFC,
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        payload=source,
    ))
    assert geometry.summary.bounds.minimum_m == pytest.approx((-0.5, -1.0, 0.0), abs=1e-6)
    assert geometry.summary.bounds.maximum_m == pytest.approx((2.2, 1.0, 3.0), abs=1e-6)
    assert geometry.summary.topology.solid_count == 2
    assert geometry.summary.volume_m3 == pytest.approx(6 + math.pi * 0.2**2 * 0.5)
    assert reopen_geometry(geometry)[1] == geometry.summary
    provenance = geometry.source_provenance
    assert provenance is not None
    assert provenance.source_sha256 == hashlib.sha256(source).hexdigest()
    assert provenance.length_unit_scale_to_metre == 0.001
    assert provenance.product_placement_m.translation_m == (1.0, 2.0, 3.0)
    assert provenance.product_placement_m.rotation == ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    assert len(provenance.representation_items) == 2
    assert {x.ifc_class for x in provenance.representation_items} == {"IfcExtrudedAreaSolid"}
    assert b"SECRET_PRODUCT_NAME" not in geometry.payload


def test_ifc_native_brep_rejects_tessellation_instead_of_reverse_engineering_mesh() -> None:
    with pytest.raises(GeometryRuntimeValidationError, match="tessellated"):
        normalize_geometry(GeometryNormalizeRequest(
            source_format=GeometrySourceFormat.IFC,
            format=CanonicalGeometryFormat.OCCT_BREP_V1,
            payload=_single_element_ifc(rich_metadata=False),
        ))


def test_ifc_native_brep_rejects_ambiguous_multiple_products() -> None:
    model = ifcopenshell.file.from_string(_native_element_ifc().decode())
    original = model.by_type("IfcBuildingElementProxy")[0]
    model.create_entity("IfcBuildingElementProxy", GlobalId=ifcopenshell.guid.new(), Representation=original.Representation)
    with pytest.raises(GeometryRuntimeValidationError, match="exactly one represented"):
        normalize_geometry(GeometryNormalizeRequest(
            source_format=GeometrySourceFormat.IFC,
            format=CanonicalGeometryFormat.OCCT_BREP_V1,
            payload=model.to_string().encode(),
        ))


@pytest.mark.asyncio
async def test_ifc_native_brep_provenance_survives_supervised_codec(tmp_path: Path) -> None:
    source = _native_element_ifc()
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        request = GeometryNormalizeRequest(source_format=GeometrySourceFormat.IFC,
            format=CanonicalGeometryFormat.OCCT_BREP_V1, payload=source,
            transform=RigidTransform3D(translation_m=(0.0, 0.0, 2.0)))
        geometry = await runtime.normalize(request)
        assert geometry.source_provenance.source_sha256 == hashlib.sha256(source).hexdigest()
        assert geometry.source_provenance.canonical_from_product_local == request.transform
        assert geometry.summary.bounds.minimum_m == pytest.approx((-0.5, -1.0, 2.0), abs=1e-6)
        assert await runtime.reopen(geometry) == geometry.summary
        assert await runtime.normalize(request) == geometry
    finally:
        await runtime.close()


def test_query_source_budget_counts_utf8_bytes_not_characters() -> None:
    geometry = normalize_geometry(
        GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1,
            payload=_mesh_payload(),
        )
    )

    accepted = GeometryQueryRequest(geometry=geometry, source_code="界" * 21_845)
    assert len(accepted.source_code.encode("utf-8")) == 65_535
    with pytest.raises(ValueError, match="65536 UTF-8 bytes"):
        GeometryQueryRequest(geometry=geometry, source_code="界" * 21_846)


def test_query_worker_budget_covers_bounded_ocp_measurement() -> None:
    assert GEOMETRY_QUERY_BUDGET.wall_seconds == 180.0
    assert GEOMETRY_QUERY_BUDGET.cpu_seconds == 180.0
    assert GEOMETRY_QUERY_BUDGET.rss_bytes == 3 * 1024**3
    assert GEOMETRY_QUERY_BUDGET.threads == 8
    assert GEOMETRY_QUERY_BUDGET.scratch_bytes == 128 * 1024**2


@pytest.mark.asyncio
@pytest.mark.parametrize("shape_kind", ["box", "cylinder"])
async def test_query_efficiency_test_examples_execute_in_actual_runtime(
    tmp_path: Path, shape_kind: str,
) -> None:
    examples = Path(__file__).resolve().parents[2] / "fixtures/geometry_runtime/query_efficiency"

    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    payload = _box_brep(tmp_path)
    if shape_kind == "cylinder":
        path = tmp_path / "analytic-cylinder.brep"
        exporters.export(cq.Workplane("XY").cylinder(2, 1).val(), str(path), "BREP")
        payload = path.read_bytes()
    geometry = normalize_geometry(GeometryNormalizeRequest(
        format=CanonicalGeometryFormat.OCCT_BREP_V1, payload=payload,
    ))
    try:
        if shape_kind == "box":
            with pytest.raises(GeometryRuntimeValidationError):
                await runtime.query(GeometryQueryRequest(
                    geometry=geometry,
                    source_code="def query(ctx):\n    return ctx.private_geometry().Edges()[0].radius()\n",
                ))
        analytic = await runtime.query(GeometryQueryRequest(
            geometry=geometry, source_code=(examples / "analytic_inspection.py").read_text(),
        ))
        sampled = await runtime.query(GeometryQueryRequest(
            geometry=geometry, source_code=(examples / "parameter_sampling.py").read_text(),
        ))
    finally:
        await runtime.close()
    expected_edges = 12 if shape_kind == "box" else 3
    assert analytic.result["edge_count"] == expected_edges
    assert analytic.result["analytic_circle_count"] == (0 if shape_kind == "box" else 2)
    assert analytic.result["radius_range_m"] == (None if shape_kind == "box" else [1.0, 1.0])
    assert len(sampled.result["sampled"]) == expected_edges
    assert all(len(edge["points_m"]) == 9 for edge in sampled.result["sampled"])
    assert "exploratory" in sampled.result["scope"]


@pytest.mark.asyncio
async def test_query_actual_supervisor_resource_diagnostic_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.infrastructure.geometry_runtime import adapter

    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    geometry = normalize_geometry(GeometryNormalizeRequest(
        format=CanonicalGeometryFormat.OCCT_BREP_V1, payload=_box_brep(tmp_path),
    ))
    monkeypatch.setattr(adapter, "GEOMETRY_QUERY_BUDGET", replace(GEOMETRY_QUERY_BUDGET, rss_bytes=1))
    try:
        with pytest.raises(GeometryRuntimeResourceLimitError) as caught:
            await runtime.query(GeometryQueryRequest(
                geometry=geometry, source_code="def query(ctx):\n    return ctx.summary()\n",
            ))
        diagnostic = caught.value.diagnostic
        assert diagnostic is not None
        assert diagnostic.resource_kind == "rss"
        assert diagnostic.configured_limits["rss_bytes"] == 1
        assert diagnostic.sampled_peaks["peak_rss_bytes"] > 1
        assert diagnostic.elapsed_seconds > 0
        assert "source_code" not in diagnostic.model_dump_json()
        monkeypatch.setattr(adapter, "GEOMETRY_QUERY_BUDGET", GEOMETRY_QUERY_BUDGET)
        recovered = await runtime.query(GeometryQueryRequest(
            geometry=geometry, source_code="def query(ctx):\n    return {'ok': True}\n",
        ))
        assert recovered.result == {"ok": True}
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_seatbelt_query_supports_bounded_summary_and_ocp_section(tmp_path: Path) -> None:
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    geometry = await runtime.normalize(
        GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.OCCT_BREP_V1,
            payload=_box_brep(tmp_path),
        )
    )
    source = """from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
from OCP.gp import gp_Dir, gp_Pln, gp_Pnt
import cadquery as cq

def query(ctx):
    shape = ctx.private_geometry()
    section = BRepAlgoAPI_Section(
        shape.wrapped,
        gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)),
        False,
    )
    section.Build()
    return {
        "faces": len(shape.Faces()),
        "section_edges": len(cq.Shape(section.Shape()).Edges()),
        "summary_faces": ctx.summary()["topology"]["face_count"],
    }
"""

    try:
        result = await runtime.query(GeometryQueryRequest(geometry=geometry, source_code=source))
    finally:
        await runtime.close()

    assert result.result == {"faces": 6, "section_edges": 4, "summary_faces": 6}
    assert result.result_bytes < 256
    assert result.geometry_sha256 == geometry.sha256


@pytest.mark.asyncio
async def test_seatbelt_query_detaches_mesh_and_rejects_imports_and_path_access(tmp_path: Path) -> None:
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    geometry = await runtime.normalize(
        GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1,
            payload=_mesh_payload(),
        )
    )
    try:
        result = await runtime.query(GeometryQueryRequest(
            geometry=geometry,
            source_code=(
                "def query(ctx):\n"
                "    mesh = ctx.private_geometry()\n"
                "    mesh.vertices[0][0] = 99.0\n"
                "    return {'private': ctx.private_geometry().vertices[0][0], "
                "'source': ctx.vertices()[0][0]}\n"
            ),
        ))
        assert result.result == {"private": 99.0, "source": 0.0}
        with pytest.raises(GeometryRuntimeValidationError, match="QUERY_SOURCE_INVALID"):
            await runtime.query(
                GeometryQueryRequest(
                    geometry=geometry,
                    source_code="import os\n\ndef query(ctx):\n    return {'cwd': os.getcwd()}\n",
                )
            )
        with pytest.raises(GeometryRuntimeValidationError, match="blocked runtime capability"):
            await runtime.query(
                GeometryQueryRequest(
                    geometry=geometry,
                    source_code="def query(ctx):\n    return open('/private/etc/passwd').read()\n",
                )
            )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_seatbelt_query_brep_computation_copy_cannot_replace_source(tmp_path: Path) -> None:
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    geometry = await runtime.normalize(
        GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.OCCT_BREP_V1,
            payload=_box_brep(tmp_path),
        )
    )
    original_payload, original_sha = geometry.payload, geometry.sha256
    source = """import cadquery as cq

def query(ctx):
    shape = ctx.private_geometry()
    shape.wrapped = cq.Solid.makeCylinder(0.3, 0.7).wrapped
    return {"faces": len(ctx.private_geometry().Faces()), "source_faces": ctx.summary()["topology"]["face_count"], "source_types": len(ctx.face_types())}
"""

    try:
        result = await runtime.query(GeometryQueryRequest(geometry=geometry, source_code=source))
        assert result.result == {"faces": 3, "source_faces": 6, "source_types": 6}
    finally:
        await runtime.close()

    assert geometry.payload == original_payload
    assert geometry.sha256 == original_sha
    reopened_shape, reopened_summary = reopen_geometry(geometry)
    assert len(reopened_shape.Faces()) == 6
    assert reopened_summary == geometry.summary


@pytest.mark.asyncio
async def test_seatbelt_query_allows_computation_mesh_cache_changes(tmp_path: Path) -> None:
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    payload = user_geometry_policy.serialize_user_shape(cq.Solid.makeSphere(0.1))
    geometry = await runtime.normalize(GeometryNormalizeRequest(
        format=CanonicalGeometryFormat.OCCT_BREP_V1, payload=payload,
    ))
    source = '''from OCP.BRepTools import BRepTools
def query(ctx):
    shape = ctx.private_geometry()
    before = len(shape.tessellate(0.001, 0.1)[1])
    BRepTools.Clean_s(shape.wrapped)
    after = len(shape.tessellate(0.0002, 0.05)[1])
    return {"before": before, "after": after, "source_triangles": ctx.summary()["topology"]["triangle_count"]}
'''
    try:
        result = await runtime.query(GeometryQueryRequest(geometry=geometry, source_code=source))
    finally:
        await runtime.close()
    assert result.result["after"] > result.result["before"]
    assert result.result["source_triangles"] is None
    assert geometry.payload == payload
    assert result.geometry_sha256 == hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_seatbelt_query_accepts_exact_65536_utf8_source_bytes(tmp_path: Path) -> None:
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    geometry = await runtime.normalize(
        GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.INDEXED_TRIANGLE_MESH_V1,
            payload=_mesh_payload(),
        )
    )
    prefix = "def query(ctx):\n    return {'triangles': len(ctx.triangles())}\n#"
    remaining = 65_536 - len(prefix.encode("utf-8"))
    source = prefix + "界" * (remaining // 3) + "x" * (remaining % 3)
    assert len(source.encode("utf-8")) == 65_536

    try:
        result = await runtime.query(GeometryQueryRequest(geometry=geometry, source_code=source))
    finally:
        await runtime.close()

    assert result.result == {"triangles": 2}


def test_codec_render_derives_main_and_context_meshes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    geometry = normalize_geometry(
        GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.OCCT_BREP_V1,
            payload=_box_brep(tmp_path),
        )
    )
    profile = CircularInterfaceProfile(diameter_m=0.1)
    scene = GeometryRenderScene(
        coordinate_space=GeometryRenderCoordinateSpace.PROJECT_WORLD,
        geometry_sha256=geometry.sha256,
        display_context_sha256=geometry.sha256,
        interface_overlays=(
            GeometryRenderInterfaceOverlay(
                overlay_id="host:pipe",
                label="pipe",
                marker_kind=GeometryRenderInterfaceMarkerKind.HOST_ATTACHMENT,
                frame=RigidTransform3D(),
                profile=profile,
            ),
        ),
        host_overlays=(
            GeometryRenderHostOverlay(
                overlay_id="host:pipe",
                construction_kind="illustrative",
            ),
        ),
        straight_profile_overlays=(
            GeometryRenderStraightProfileOverlay(
                recipe_key="host:pipe",
                frame=RigidTransform3D(),
                profile=profile,
                length_m=0.2,
                display_kind="illustrative",
            ),
        ),
    )
    calls = []
    original = geometry_runtime_codec.mesh_for_render

    def capture(shape):
        calls.append(shape)
        return original(shape)

    monkeypatch.setattr(geometry_runtime_codec, "mesh_for_render", capture)
    result = execute_render_request(
        GeometryRenderRequest(
            geometry=geometry,
            scene=scene,
            display_context=geometry,
            width_px=128,
            height_px=128,
        )
    )

    assert result.payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(calls) == 2


def test_source_path_scene_keeps_order_identity_and_old_empty_serialization() -> None:
    base = GeometryRenderScene(
        coordinate_space=GeometryRenderCoordinateSpace.PROJECT_WORLD,
        geometry_sha256="a" * 64,
    )
    assert "source_path_overlays" not in base.model_dump(mode="json")
    points = [(float(i), float(i % 2), 0.0) for i in range(19)]
    scene = GeometryRenderScene(
        coordinate_space=GeometryRenderCoordinateSpace.PROJECT_WORLD,
        geometry_sha256="a" * 64,
        source_path_overlays=(GeometryRenderSourcePathOverlay(
            overlay_id="source:pipe_input", source_label="synthetic PIPE input path",
            points_m=tuple(points),
        ),),
    )
    restored = GeometryRenderScene.model_validate_json(scene.model_dump_json())
    assert restored.source_path_overlays[0].points_m == tuple(points)
    assert restored.host_overlays == ()
    assert sha256_digest(scene) != sha256_digest(base)
    moved = scene.model_copy(update={"source_path_overlays": (
        scene.source_path_overlays[0].model_copy(update={"points_m": tuple(points[:-1] + [(99.0, 0.0, 0.0)])}),
    )})
    assert sha256_digest(moved) != sha256_digest(scene)


@pytest.mark.parametrize("points", [
    [(0, 0, 0)],
    [(0, 0, 0), (float("nan"), 0, 0)],
    [(0, 0, 0), (float("inf"), 0, 0)],
    [(float(i), 0, 0) for i in range(257)],
])
def test_source_path_rejects_invalid_or_over_capacity_points(points) -> None:
    with pytest.raises(ValueError):
        GeometryRenderSourcePathOverlay(
            overlay_id="source:test", source_label="test source", points_m=points,
        )


def test_source_path_rejects_duplicate_id_and_total_point_budget() -> None:
    one = GeometryRenderSourcePathOverlay(
        overlay_id="source:one", source_label="first", points_m=((0, 0, 0), (1, 0, 0)),
    )
    with pytest.raises(ValueError, match="ids must be unique"):
        GeometryRenderScene(
            coordinate_space=GeometryRenderCoordinateSpace.PROJECT_WORLD,
            geometry_sha256="a" * 64, source_path_overlays=(one, one),
        )
    many = tuple(GeometryRenderSourcePathOverlay(
        overlay_id=f"source:path_{index}", source_label=f"path {index}",
        points_m=tuple((float(point), float(index), 0.0) for point in range(256)),
    ) for index in range(5))
    with pytest.raises(ValueError, match="point budget"):
        GeometryRenderScene(
            coordinate_space=GeometryRenderCoordinateSpace.PROJECT_WORLD,
            geometry_sha256="a" * 64, source_path_overlays=many,
        )


def test_source_path_draws_dashed_line_and_participates_in_fit(tmp_path: Path) -> None:
    geometry = normalize_geometry(GeometryNormalizeRequest(
        format=CanonicalGeometryFormat.OCCT_BREP_V1, payload=_box_brep(tmp_path),
    ))
    shape, _ = reopen_geometry(geometry)
    source_scene = GeometryRenderScene(
        coordinate_space=GeometryRenderCoordinateSpace.PROJECT_WORLD,
        geometry_sha256=geometry.sha256,
        source_path_overlays=(GeometryRenderSourcePathOverlay(
            overlay_id="source:measured_path", source_label="measured path",
            points_m=((0, 0, 0), (10, 0, 0), (10, 10, 0)),
        ),),
    )
    request = GeometryRenderRequest(geometry=geometry, scene=source_scene,
                                    width_px=400, height_px=300)
    rendered = render_mesh(request, mesh_for_render(shape))
    assert rendered.resolved_panels[0].geometry_clipped is False
    with Image.open(io.BytesIO(rendered.payload)) as image:
        assert sum(pixel == (119, 146, 184) for pixel in image.get_flattened_data()) > 0
    plain = render_mesh(GeometryRenderRequest(geometry=geometry, width_px=400, height_px=300),
                        mesh_for_render(shape))
    assert rendered.sha256 != plain.sha256


def test_source_path_legend_uses_a_separate_row_from_interface_legend(tmp_path: Path) -> None:
    geometry = normalize_geometry(GeometryNormalizeRequest(
        format=CanonicalGeometryFormat.OCCT_BREP_V1, payload=_box_brep(tmp_path),
    ))
    shape, _ = reopen_geometry(geometry)
    marker = GeometryRenderInterfaceOverlay(
        overlay_id="family:test", label="test",
        marker_kind=GeometryRenderInterfaceMarkerKind.FAMILY_EXTERNAL,
        frame=RigidTransform3D(), profile=CircularInterfaceProfile(diameter_m=0.1),
    )
    plain_scene = GeometryRenderScene(
        coordinate_space=GeometryRenderCoordinateSpace.PROJECT_WORLD,
        geometry_sha256=geometry.sha256, interface_overlays=(marker,),
    )
    source_scene = GeometryRenderScene(
        coordinate_space=GeometryRenderCoordinateSpace.PROJECT_WORLD,
        geometry_sha256=geometry.sha256, interface_overlays=(marker,),
        source_path_overlays=(GeometryRenderSourcePathOverlay(
            overlay_id="source:measured_path", source_label="measured path",
            points_m=((0.0, 0.0, 0.0), (0.1, 0.0, 0.0)),
        ),),
    )
    def picture(scene):
        rendered = render_mesh(
            GeometryRenderRequest(geometry=geometry, scene=scene,
                                  width_px=400, height_px=300,
                                  presentation={"panels": [{"panel_key": "joint", "camera": {
                                      "camera_kind": "preset_fit", "view": "isometric",
                                  }}], "overlay": {"overlay_kind": "interfaces"}}),
            mesh_for_render(shape),
        )
        return Image.open(io.BytesIO(rendered.payload)).convert("RGB")

    plain, with_source = picture(plain_scene), picture(source_scene)
    # The existing interface legend stays in its bottom row; source text has a distinct row.
    assert plain.crop((0, 282, 400, 300)).tobytes() == with_source.crop((0, 282, 400, 300)).tobytes()
    assert plain.crop((0, 264, 400, 282)).tobytes() != with_source.crop((0, 264, 400, 282)).tobytes()


@pytest.mark.parametrize("view", tuple(GeometryPreviewView))
def test_standard_view_preview_is_bounded_valid_and_deterministic(
    tmp_path: Path, view: GeometryPreviewView
) -> None:
    geometry = normalize_geometry(
        GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.OCCT_BREP_V1,
            payload=_box_brep(tmp_path),
        )
    )
    value, _ = reopen_geometry(geometry)
    request = GeometryRenderRequest(
        geometry=geometry,
        view=view,
        width_px=320,
        height_px=240,
    )

    first = render_mesh(request, mesh_for_render(value))
    second = render_mesh(request, mesh_for_render(value))

    assert first == second
    assert first.payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(first.payload) < 128 * 1024
    image_path = tmp_path / f"{view.value}.png"
    image_path.write_bytes(first.payload)
    with Image.open(image_path) as image:
        assert image.size == (320, 240)
        assert image.mode == "RGB"
