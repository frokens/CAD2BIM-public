"""Material sections, exact selection and the existing supervised runtime seam."""

from __future__ import annotations

import hashlib
import io
import math
import sys
import tempfile
from pathlib import Path

import cadquery as cq
import pytest
from PIL import Image
from pydantic import ValidationError

from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.geometry_inspection import (
    GeometrySectionDisplay,
    GeometrySectionObservation,
    InspectionComponentSelection,
    InspectionFragmentLocalCoordinates,
    InspectionFragmentSelection,
    InspectionResultCoordinates,
    InspectionWholeSelection,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometryInspectionFragmentInput,
    GeometryQueryRequest,
    GeometryRenderRequest,
    GeometryRuntimeValidationError,
    GeometrySectionRequest,
    GeometrySectionResult,
    GeometrySectionRenderFailure,
)
from CAD2BIM_backend.infrastructure.geometry_runtime import codec, section
from CAD2BIM_backend.infrastructure.geometry_runtime.adapter import OcpCadQueryGeometryRuntime
from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution_protocol import (
    GeometryCodecProtocolError,
    pack_fragment_inputs,
    unpack_fragment_inputs,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution import OcpGeometryCodecExecutor, GEOMETRY_CODEC_BUDGET
from CAD2BIM_backend.infrastructure.geometry_runtime.query_child import _GeometryQueryContext
from CAD2BIM_backend.infrastructure.geometry_runtime.render import render_section_measurement
from CAD2BIM_backend.infrastructure.geometry_runtime.section import InspectionGeometry, measure_sections
from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import serialize_user_shape, transform_occt_shape


_AREA = math.pi * (.04**2-.036**2)


def _canonical(shape):
    payload = serialize_user_shape(shape)
    format = CanonicalGeometryFormat.OCCT_BREP_V1
    return CanonicalGeometry(format=format, content_type=format.content_type,
                             payload=payload, sha256=hashlib.sha256(payload).hexdigest())


@pytest.fixture(scope="module")
def tube():
    return _canonical(cq.Solid.makeCylinder(.04, .1).cut(cq.Solid.makeCylinder(.036, .1)))


def _observation(origin=(0., 0., .05), normal=(0., 0., 1.), offsets=(0.,), display=None):
    return GeometrySectionObservation(plane={"origin_m": origin, "normal": normal}, offsets_m=offsets, display=display)


def _request(geometry, observation=None, **kwargs):
    return GeometrySectionRequest(geometry=geometry, selection=InspectionWholeSelection(),
                                  coordinates=InspectionResultCoordinates(),
                                  observation=observation or _observation(), **kwargs)


def _measure(request):
    source = InspectionGeometry(request.geometry, request.fragment_inputs, request.selection, request.coordinates)
    try:
        return measure_sections(source, request.observation)
    finally:
        source.close()


def _placed_family(tube):
    pose = RigidTransform3D(rotation=((0., 0., 1.), (1., 0., 0.), (0., 1., 0.)), translation_m=(12., -7., 2.))
    other = RigidTransform3D(translation_m=(15., -7., 2.))
    local = codec.read_geometry_payload(tube)
    world = _canonical(cq.Compound.makeCompound([transform_occt_shape(local, pose), transform_occt_shape(local, other)]))
    fragments = (
        GeometryInspectionFragmentInput(fragment_key="tube", component_key="body", binding_key="main",
            geometry=tube, result_from_local=pose, uniform_scale=2.),
        GeometryInspectionFragmentInput(fragment_key="other", component_key="other_body", binding_key="aux",
            geometry=tube, result_from_local=other),
    )
    return world, fragments


def test_annular_material_and_offsets_reuse_one_parse_and_crop_only_image(tube, monkeypatch, tmp_path):
    actual = codec.read_geometry_payload
    parsed = []
    def capture(geometry):
        parsed.append(geometry.sha256)
        return actual(geometry)
    monkeypatch.setattr(codec, "read_geometry_payload", capture)
    request = _request(tube, _observation(offsets=(0., .025)))
    measurements = _measure(request)
    assert parsed == [tube.sha256]
    for measurement in measurements:
        assert measurement.status == "computed"
        assert measurement.material_area_sum_m2 == pytest.approx(_AREA, rel=1e-10)
        region = measurement.objects[0].material_regions[0]
        assert {b.role for b in region.boundaries} == {"outer", "hole"}
        assert all(b.closed for b in region.boundaries)
        assert sorted(c.radius_m for b in region.boundaries for c in b.curves) == pytest.approx([.036, .04])
    before = measurements[0].model_dump(mode="json")
    cropped = request.model_copy(update={"observation": _observation(display=GeometrySectionDisplay(
        u_min_m=-.02, u_max_m=.02, v_min_m=-.02, v_max_m=.02))})
    image = render_section_measurement(measurements[0], cropped)
    assert image.display_clipped
    assert measurements[0].model_dump(mode="json") == before
    (tmp_path / "annular-section.png").write_bytes(render_section_measurement(measurements[0], request).preview.payload)
    with Image.open(io.BytesIO(image.preview.payload)) as png:
        assert png.format == "PNG" and png.size == (640, 480)


def test_missing_wall_has_its_actual_material_region(tube):
    opened = codec.read_geometry_payload(tube)
    missing_half = opened.cut(cq.Solid.makeBox(.1, .05, .12, cq.Vector(-.05, 0., -.01)))
    measurement = _measure(_request(_canonical(missing_half)))[0]
    assert measurement.status == "computed"
    assert measurement.material_area_sum_m2 == pytest.approx(_AREA/2, rel=1e-10)
    region = measurement.objects[0].material_regions[0]
    assert [b.role for b in region.boundaries] == ["outer"]
    assert any(c.curve_type == "LINE" for b in region.boundaries for c in b.curves)


def test_empty_boundary_and_tangent_remain_distinct_observations(tube):
    empty = _measure(_request(tube, _observation(origin=(0., 0., .2))))[0]
    boundary = _measure(_request(tube, _observation(origin=(0., 0., 0.))))[0]
    tangent = _measure(_request(tube, _observation(origin=(.04, 0., .05), normal=(1., 0., 0.))))[0]
    assert empty.status == "empty" and empty.material_area_sum_m2 == 0.
    assert {d.code for d in empty.objects[0].diagnostics} == {"EMPTY_SECTION"}
    assert boundary.status == "degenerate"
    assert "COPLANAR_BOUNDARY" in {d.code for d in boundary.objects[0].diagnostics}
    assert tangent.status == "degenerate" and tangent.material_area_sum_m2 == 0.
    assert tangent.objects[0].intersection_edge_count == 1
    assert [c.curve_type for c in tangent.objects[0].intersection_curves] == ["LINE"]
    assert "TANGENT_OR_DEGENERATE" in {d.code for d in tangent.objects[0].diagnostics}


def test_surface_section_does_not_claim_solid_material():
    face = cq.Face.makePlane(.08, .08, cq.Vector(0., 0., .05))
    measurement = _measure(_request(_canonical(face)))[0]
    assert measurement.status == "degenerate"
    assert measurement.material_area_sum_m2 is None
    assert measurement.objects[0].material_regions == ()
    assert "NO_SOLID_MATERIAL" in {d.code for d in measurement.objects[0].diagnostics}


def test_result_and_fragment_local_coordinates_use_already_scaled_bytes(tube):
    world, fragments = _placed_family(tube)
    selection = InspectionFragmentSelection(fragment_key="tube")
    result_request = GeometrySectionRequest(geometry=world, selection=selection, coordinates=InspectionResultCoordinates(),
        fragment_inputs=fragments, observation=_observation(origin=(12.05, -7., 2.), normal=(1., 0., 0.)))
    local_request = result_request.model_copy(update={"coordinates": InspectionFragmentLocalCoordinates(fragment_key="tube"),
                                                     "observation": _observation()})
    for measurement in (_measure(result_request)[0], _measure(local_request)[0]):
        assert measurement.status == "computed"
        assert measurement.material_area_sum_m2 == pytest.approx(_AREA, rel=1e-10)
        assert measurement.objects[0].fragment_key == "tube"
        assert measurement.objects[0].component_key == "body"
        assert measurement.objects[0].binding_key == "main"
        bounds = measurement.projected_bounds_uv_m
        assert bounds[1]-bounds[0] == pytest.approx(.08, abs=3e-7)
        assert bounds[3]-bounds[2] == pytest.approx(.08, abs=3e-7)


@pytest.mark.parametrize("separation", [.12, .02], ids=["gap", "overlap"])
def test_all_compound_objects_and_fragment_ownership_are_kept_without_union(tube, separation):
    local = codec.read_geometry_payload(tube)
    pose = RigidTransform3D(translation_m=(separation, 0., 0.))
    world = _canonical(cq.Compound.makeCompound([local, transform_occt_shape(local, pose)]))
    fragments = (
        GeometryInspectionFragmentInput(fragment_key="left", component_key="left_body", binding_key="one",
            geometry=tube, result_from_local=RigidTransform3D()),
        GeometryInspectionFragmentInput(fragment_key="right", component_key="right_body", binding_key="two",
            geometry=tube, result_from_local=pose),
    )
    bare = _measure(_request(world))[0]
    owned = _measure(_request(world, fragment_inputs=fragments))[0]
    assert len(bare.objects) == len(owned.objects) == 2
    assert owned.material_area_sum_m2 == pytest.approx(2*_AREA, rel=1e-10)
    assert {o.fragment_key for o in owned.objects} == {"left", "right"}
    assert {o.component_key for o in owned.objects} == {"left_body", "right_body"}
    assert owned.area_semantics == "sum_of_object_material_regions_not_union"
    assert "UNION_AREA_NOT_COMPUTED" in {d.code for d in owned.diagnostics}
    component = GeometrySectionRequest(geometry=world, fragment_inputs=fragments,
        selection=InspectionComponentSelection(component_key="left_body"), coordinates=InspectionResultCoordinates(), observation=_observation())
    assert [o.fragment_key for o in _measure(component)[0].objects] == ["left"]


def test_drawing_failure_keeps_independent_native_area_and_curve_dimensions(tube, monkeypatch):
    def fail(*args):
        raise RuntimeError("injected drawing sample failure")
    monkeypatch.setattr(section, "_drawing_points", fail)
    request = _request(tube)
    measurement = _measure(request)[0]
    assert measurement.status == "computed"
    assert measurement.material_area_sum_m2 == pytest.approx(_AREA, rel=1e-10)
    assert "BOUNDARY_DRAWING_FAILED" in {d.code for d in measurement.objects[0].diagnostics}
    assert sorted(c.radius_m for r in measurement.objects[0].material_regions for b in r.boundaries for c in b.curves) == pytest.approx([.036, .04])
    with pytest.raises(GeometryRuntimeValidationError, match="independently computed measurements"):
        render_section_measurement(measurement, request)


def test_private_fragment_transport_and_sample_have_exact_source_identity(tube):
    sample = GeometryInspectionFragmentInput(fragment_key="sample", binding_key="sample", geometry=tube, result_from_local=RigidTransform3D())
    another = sample.model_copy(update={"fragment_key": "other"})
    metadata, payload = pack_fragment_inputs((sample, another))
    assert payload == tube.payload
    decoded = unpack_fragment_inputs(metadata, payload)
    assert decoded == (sample, another)
    assert decoded[0].geometry.payload is decoded[1].geometry.payload
    assert sample.component_key is None
    # Many occurrences of one exact source use one private binary payload.
    padded = tube.payload+b"\n"*(600*1024)
    shared = tube.model_copy(update={"payload": padded, "sha256": hashlib.sha256(padded).hexdigest()})
    occurrences = tuple(sample.model_copy(update={"fragment_key": f"part{i}", "geometry": shared}) for i in range(128))
    assert sum(len(item.geometry.payload) for item in occurrences) > 64*1024*1024
    _request(tube, fragment_inputs=occurrences)
    assert pack_fragment_inputs(occurrences)[1] == padded
    with pytest.raises(ValidationError, match="component selection has no exact fragment"):
        GeometrySectionRequest(geometry=tube, fragment_inputs=(sample,),
            selection=InspectionComponentSelection(component_key="sample"), coordinates=InspectionResultCoordinates(), observation=_observation())
    with pytest.raises(GeometryCodecProtocolError, match="SHA-256"):
        unpack_fragment_inputs(metadata, bytes([payload[0]^1])+payload[1:])
    with pytest.raises(GeometryCodecProtocolError, match="unreferenced"):
        unpack_fragment_inputs(metadata, payload+b"x")
    forged = [{**metadata[0], "source_path": "/not-an-input"}]
    with pytest.raises(GeometryCodecProtocolError, match="closed key set"):
        unpack_fragment_inputs(forged, payload)


def test_query_context_private_copies_cannot_rewrite_source_facts_or_sections(tube):
    fragment = GeometryInspectionFragmentInput(fragment_key="sample", binding_key="sample", geometry=tube, result_from_local=RigidTransform3D())
    source = InspectionGeometry(tube, (fragment,), InspectionFragmentSelection(fragment_key="sample"), InspectionResultCoordinates())
    facts = {"interfaces": {"items": [{"diameter_m": .08}]}, "checks": {"declared": True}, "components": []}
    context = _GeometryQueryContext(source.selected(), None, inspection_source=source, inspection_context=facts)
    try:
        context.interfaces()["items"][0]["diameter_m"] = 99.
        context.checks()["declared"] = False
        context.private_fragment("sample").wrapped = cq.Solid.makeBox(1., 1., 1.).wrapped
        context.private_geometry().wrapped = cq.Solid.makeBox(2., 2., 2.).wrapped
        assert context.interfaces() == facts["interfaces"]
        assert context.checks() == facts["checks"]
        assert context.components() == []
        assert context.fragments()[0]["component_key"] is None
        assert context.source()["geometry_sha256"] == tube.sha256
        assert context.sections({"origin_m": [0., 0., .05], "normal": [0., 0., 1.]})["measurements"][0]["material_area_sum_m2"] == pytest.approx(_AREA, rel=1e-10)
    finally:
        context.close()


@pytest.mark.parametrize("coordinate_space", ["result", "fragment_local"])
def test_display_query_shares_preview_fragment_traversal_and_one_mesh_cache(monkeypatch, coordinate_space):
    local = _canonical(cq.Solid.makeBox(.2, .3, .4))
    fragments = tuple(GeometryInspectionFragmentInput(fragment_key=f"part{i}", component_key="body", binding_key="body",
        geometry=local, result_from_local=RigidTransform3D(translation_m=(i+1.,0.,0.))) for i in range(2))
    whole = _canonical(cq.Solid.makeBox(9.,9.,9.))
    coordinates = InspectionResultCoordinates() if coordinate_space == "result" else InspectionFragmentLocalCoordinates(fragment_key="part1")
    # Component local coordinates are intentionally not legal; exercise the
    # actual legal fragment branch when fragment_local was selected.
    selection = InspectionComponentSelection(component_key="body") if coordinate_space == "result" else InspectionFragmentSelection(fragment_key="part1")
    source = InspectionGeometry(whole, fragments, selection, coordinates)
    actual, calls = codec.display_mesh, []
    def capture(shape):
        calls.append(shape)
        return actual(shape)
    monkeypatch.setattr(codec, "display_mesh", capture)
    context = _GeometryQueryContext(source.selected(), None, inspection_source=source)
    try:
        context.private_geometry().wrapped = cq.Solid.makeBox(8.,8.,8.).wrapped
        context.private_fragment("part1", coordinate_space).wrapped = cq.Solid.makeBox(7.,7.,7.).wrapped
        vertices = context.vertices()
        context.triangles()
        report = context.display_diagnostics()
        keys = [item["selection"]["fragment_key"] for item in report["objects"]]
        assert keys == (["part0", "part1"] if coordinate_space == "result" else ["part1"])
        assert len(calls) == len(keys)
        assert min(point[0] for point in vertices) == pytest.approx(1. if coordinate_space == "result" else 0.)
        assert all(item["face_count"] == 6 and item["completed_face_count"] == 6 for item in report["objects"])
        report["objects"][0]["face_count"] = 999
        assert context.display_diagnostics()["objects"][0]["face_count"] == 6
        assert len(calls) == len(keys)
        whole_source = InspectionGeometry(whole, fragments, InspectionWholeSelection(), InspectionResultCoordinates())
        assert len(whole_source.display_objects()) == 1
        assert whole_source.display_objects()[0].fragment_key is None
        assert whole_source.display_objects()[0].shape.Volume() == pytest.approx(729.)
        whole_source.close()
    finally:
        context.close()
    assert context._display_meshes is None and context._display_report is None


def _track_scratch(monkeypatch, tmp_path):
    actual = tempfile.mkdtemp
    roots = []
    def capture(*args, **kwargs):
        kwargs.setdefault("dir", tmp_path)
        path = actual(*args, **kwargs)
        roots.append(Path(path))
        return path
    monkeypatch.setattr(tempfile, "mkdtemp", capture)
    return roots


@pytest.mark.asyncio
async def test_section_receipt_cannot_substitute_another_source(tube, tmp_path, monkeypatch):
    request = _request(tube)
    forged = GeometrySectionResult(geometry_sha256="0"*64, selection=request.selection, coordinates=request.coordinates,
        measurements=_measure(request), render_errors=(GeometrySectionRenderFailure(section_index=0, message="No image"),))
    facts = canonical_json(forged.model_dump(mode="json", exclude={"images"})).encode("utf-8")
    async def fake_run(*args, **kwargs):
        return ({"status": "succeeded", "operation": "section", "execution_started": True,
                 "result": {"facts_sha256": hashlib.sha256(facts).hexdigest(), "facts_size": len(facts), "images": []}}, (facts, b""))
    runtime = OcpGeometryCodecExecutor(budget=GEOMETRY_CODEC_BUDGET, capacity_root=tmp_path / "capacity")
    monkeypatch.setattr(runtime, "_run", fake_run)
    try:
        with pytest.raises(GeometryRuntimeValidationError, match="exact selected source"):
            await runtime.section(request)
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="approved geometry runtime uses Seatbelt")
async def test_supervised_query_section_and_model_share_exact_selected_fragment(tube, tmp_path, monkeypatch):
    roots = _track_scratch(monkeypatch, tmp_path)
    world, fragments = _placed_family(tube)
    request = GeometrySectionRequest(geometry=world, fragment_inputs=fragments,
        selection=InspectionFragmentSelection(fragment_key="tube"), coordinates=InspectionResultCoordinates(),
        observation=_observation(origin=(12.05, -7., 2.), normal=(1., 0., 0.), offsets=(0., .025)), width_px=400, height_px=320)
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    original = (world.payload, world.sha256, tuple(f.geometry.payload for f in fragments))
    try:
        observed = await runtime.section(request)
        query = await runtime.query(GeometryQueryRequest(geometry=world, fragment_inputs=fragments,
            selection=request.selection, coordinates=request.coordinates,
            inspection_context={"components": [{"component_key": "body"}], "checks": {"declared": True}},
            source_code="""import cadquery as cq
def query(ctx):
    ctx.private_fragment('tube').wrapped = cq.Solid.makeBox(1,1,1).wrapped
    ctx.components()[0]['component_key'] = 'changed'
    ctx.checks()['declared'] = False
    return {'sections': ctx.sections({'origin_m': [12.05,-7,2], 'normal': [1,0,0]}, [0,0.025]),
            'component': ctx.components()[0]['component_key'], 'checks': ctx.checks()}
"""))
        model = await runtime.render(GeometryRenderRequest(geometry=world, fragment_inputs=fragments,
            inspection_selection=request.selection, inspection_coordinates=request.coordinates, width_px=400, height_px=320))
        assert query.result["component"] == "body" and query.result["checks"] == {"declared": True}
        assert query.result["sections"]["measurements"] == [m.model_dump(mode="json") for m in observed.measurements]
        assert [image.section_index for image in observed.images] == [0, 1]
        assert observed.render_errors == ()
        assert all(m.material_area_sum_m2 == pytest.approx(_AREA, rel=1e-10) for m in observed.measurements)
        assert model.resolved_panels[0].camera.target_m == pytest.approx((12.05, -7., 2.), abs=3e-7)
        assert not any(root.exists() for root in roots)
        (tmp_path / "selected-section.png").write_bytes(observed.images[0].preview.payload)
        (tmp_path / "selected-model.png").write_bytes(model.payload)
    finally:
        await runtime.close()
    assert original == (world.payload, world.sha256, tuple(f.geometry.payload for f in fragments))


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="approved geometry runtime uses Seatbelt")
async def test_supervised_render_failure_retains_all_measurements_and_cleans_copies(tube, tmp_path, monkeypatch):
    roots = _track_scratch(monkeypatch, tmp_path)
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        observed = await runtime.section(_request(tube, _observation(offsets=(0., .025)), width_px=64, height_px=64))
        assert observed.images == ()
        assert [error.section_index for error in observed.render_errors] == [0, 1]
        assert all(m.status == "computed" and m.material_area_sum_m2 == pytest.approx(_AREA, rel=1e-10) for m in observed.measurements)
        invalid = tube.model_copy(update={"payload": b"not a BREP", "sha256": hashlib.sha256(b"not a BREP").hexdigest()})
        with pytest.raises(GeometryRuntimeValidationError):
            await runtime.section(_request(invalid))
        assert not any(root.exists() for root in roots)
    finally:
        await runtime.close()
