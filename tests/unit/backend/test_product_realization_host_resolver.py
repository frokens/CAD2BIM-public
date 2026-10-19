from __future__ import annotations

import hashlib

import ifcopenshell.api.project
import numpy as np
import pytest
from pydantic import ValidationError

from CAD2BIM_backend.infrastructure.ifc_revision_runtime import IfcOpenShellRevisionRuntime
from tests.process_support.ifc_revision_runtime import BytesIfcCandidate
from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.product_realization_host import (
    CurrentIfcAxisBoundary,
    CurrentIfcBodyEndpointEditPermission,
    CurrentIfcHostFactRequest,
    CurrentIfcProductBoundarySelector,
    ProductRealizationHostError,
    ProductRealizationHostErrorCode,
)
from CAD2BIM_backend.infrastructure.ifc.product_realization_host import (
    IfcOpenShellProductRealizationHostFactExtractor,
    inspect_current_straight_body,
)


PROJECT_GLOBAL_ID = "0AAAAAAAAAAAAAAAAAAAAA"
PIPE_GLOBAL_ID = "0BBBBBBBBBBBBBBBBBBBBB"
OTHER_GLOBAL_ID = "0CCCCCCCCCCCCCCCCCCCCC"


def _model(
    *,
    product_class: str = "IfcPipeSegment",
    include_axis: bool = True,
    duplicate_axis: bool = False,
    include_body: bool = True,
    duplicate_body_item: bool = False,
    duplicate_global_id: bool = False,
    vertical: bool = False,
    millimetres: bool = False,
    include_placement: bool = True,
    coincident_axis: bool = False,
    indexed_axis: bool = False,
) -> bytes:
    model = ifcopenshell.api.project.create_file(version="IFC4X3")
    project = model.create_entity(
        "IfcProject",
        GlobalId=PROJECT_GLOBAL_ID,
        Name="Host fact test",
    )
    unit = model.create_entity(
        "IfcSIUnit",
        UnitType="LENGTHUNIT",
        Prefix="MILLI" if millimetres else None,
        Name="METRE",
    )
    project.UnitsInContext = model.create_entity("IfcUnitAssignment", Units=(unit,))

    context_placement = _axis_placement(model, (0.0, 0.0, 0.0))
    context = model.create_entity(
        "IfcGeometricRepresentationContext",
        ContextIdentifier="Model",
        ContextType="Model",
        CoordinateSpaceDimension=3,
        Precision=1e-5,
        WorldCoordinateSystem=context_placement,
    )
    project.RepresentationContexts = (context,)
    local_placement = model.create_entity(
        "IfcLocalPlacement",
        PlacementRelTo=None,
        RelativePlacement=_axis_placement(
            model,
            (10_000.0, 20_000.0, 30_000.0) if millimetres else (10.0, 20.0, 30.0),
            ref_direction=(0.0, 1.0, 0.0),
        ),
    )
    distance = 2_000.0 if millimetres else 2.0
    endpoint = (
        (0.0, 0.0, 0.0)
        if coincident_axis
        else ((0.0, 0.0, distance) if vertical else (distance, 0.0, 0.0))
    )
    axis_curve = (
        _indexed_polycurve(model, (0.0, 0.0, 0.0), endpoint)
        if indexed_axis
        else _polyline(model, (0.0, 0.0, 0.0), endpoint)
    )
    representations = []
    if include_axis:
        representations.append(_shape_representation(model, context, "Axis", "Curve3D", axis_curve))
        if duplicate_axis:
            representations.append(
                _shape_representation(model, context, "Axis", "Curve3D", axis_curve)
            )
    if include_body:
        radius = 50.0 if millimetres else 0.05
        solid = model.create_entity("IfcSweptDiskSolid", Directrix=axis_curve, Radius=radius)
        items = [solid]
        if duplicate_body_item:
            items.append(
                model.create_entity("IfcSweptDiskSolid", Directrix=axis_curve, Radius=radius)
            )
        representations.append(
            model.create_entity(
                "IfcShapeRepresentation",
                ContextOfItems=context,
                RepresentationIdentifier="Body",
                RepresentationType="SweptSolid",
                Items=tuple(items),
            )
        )
    product_shape = model.create_entity(
        "IfcProductDefinitionShape",
        Representations=tuple(representations),
    )
    model.create_entity(
        product_class,
        GlobalId=PIPE_GLOBAL_ID,
        ObjectPlacement=local_placement if include_placement else None,
        Representation=product_shape,
    )
    if duplicate_global_id:
        model.create_entity(
            product_class,
            GlobalId=PIPE_GLOBAL_ID,
            ObjectPlacement=local_placement,
            Representation=product_shape,
        )
    return model.to_string().encode("utf-8")


def _axis_placement(
    model,
    origin: tuple[float, float, float],
    *,
    ref_direction: tuple[float, float, float] = (1.0, 0.0, 0.0),
):
    return model.create_entity(
        "IfcAxis2Placement3D",
        Location=model.create_entity("IfcCartesianPoint", Coordinates=origin),
        Axis=model.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
        RefDirection=model.create_entity("IfcDirection", DirectionRatios=ref_direction),
    )


def _polyline(model, start, end):
    return model.create_entity(
        "IfcPolyline",
        Points=(
            model.create_entity("IfcCartesianPoint", Coordinates=start),
            model.create_entity("IfcCartesianPoint", Coordinates=end),
        ),
    )


def _indexed_polycurve(model, start, end):
    return model.create_entity(
        "IfcIndexedPolyCurve",
        Points=model.create_entity("IfcCartesianPointList3D", CoordList=(start, end)),
        Segments=None,
        SelfIntersect=False,
    )


def _shape_representation(model, context, identifier, representation_type, item):
    return model.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=context,
        RepresentationIdentifier=identifier,
        RepresentationType=representation_type,
        Items=(item,),
    )


def _selector(
    boundary: CurrentIfcAxisBoundary,
    *,
    global_id: str = PIPE_GLOBAL_ID,
    attachment_key: str = "side_a",
    interface_key: str = "inlet",
) -> CurrentIfcProductBoundarySelector:
    return CurrentIfcProductBoundarySelector(
        product_global_id=global_id,
        boundary=boundary,
        attachment_key=attachment_key,
        interface_key=interface_key,
    )


def _extract(
    content: bytes,
    *selectors: CurrentIfcProductBoundarySelector,
):
    return IfcOpenShellProductRealizationHostFactExtractor().extract(content, selectors)


def _frame_axis(frame) -> tuple[float, float, float]:
    return tuple(frame.rotation[index][2] for index in range(3))


def _sectioned_model(intervals, *, reverse=False, millimetres=False, vertical=False, inner=None):
    model = ifcopenshell.file.from_string(_model(millimetres=millimetres, vertical=vertical).decode())
    product = model.by_guid(PIPE_GLOBAL_ID)
    body = next(rep for rep in product.Representation.Representations if rep.RepresentationIdentifier == "Body")
    scale = 1000.0 if millimetres else 1.0
    items = []
    for low, high in intervals:
        points = [(0.0, 0.0, value * scale) if vertical else (value * scale, 0.0, 0.0) for value in (low, high)]
        if reverse:
            points.reverse()
        items.append(model.create_entity("IfcSweptDiskSolid", Directrix=_polyline(model, *points),
                                         Radius=0.05 * scale, InnerRadius=inner * scale if inner else None))
    body.Items = tuple(items)
    return model


def _inspect_body(model):
    content = model.to_string().encode()
    return inspect_current_straight_body(model, PIPE_GLOBAL_ID, ifc_sha256=hashlib.sha256(content).hexdigest())


@pytest.mark.parametrize("millimetres,vertical,reverse", [(False, False, False), (True, False, True), (False, True, True)])
def test_trimmed_body_uses_actual_endpoints_with_original_axis_preserved(millimetres, vertical, reverse):
    model = _sectioned_model([(0.2, 1.7)], millimetres=millimetres, vertical=vertical, reverse=reverse, inner=0.04)
    before = model.to_string()
    body = _inspect_body(model)
    start, end = _extract(before.encode(), _selector(CurrentIfcAxisBoundary.AXIS_START),
                         _selector(CurrentIfcAxisBoundary.AXIS_END, attachment_key="b", interface_key="outlet"))
    direction = (0, 0, 1) if vertical else (0, 1, 0)
    assert body.centerline_start_m == (10, 20, 30)
    assert body.centerline_end_m == tuple(a + 2 * d for a, d in zip((10, 20, 30), direction))
    assert body.sections[0].station_interval_m == (0.2, 1.7)
    assert body.inner_diameter_m == pytest.approx(0.08)
    for fact, station, sign in ((start, 0.2, -1), (end, 1.7, 1)):
        assert fact.attachment.project_world_frame.translation_m == pytest.approx(tuple(a + station*d for a, d in zip((10, 20, 30), direction)))
        assert _frame_axis(fact.attachment.project_world_frame) == tuple(sign*d for d in direction)
    assert model.to_string() == before


def test_multiple_actual_sections_require_reference_and_are_order_independent():
    intervals = [(0.2, 0.6), (0.9, 1.2), (1.5, 1.8)]
    model = _sectioned_model(intervals)
    body = _inspect_body(model)
    reordered = _inspect_body(_sectioned_model(list(reversed(intervals)), reverse=True))
    assert body.geometry_signature == reordered.geometry_signature
    assert body.body_digest == reordered.body_digest
    assert body.sections == reordered.sections
    with pytest.raises(ProductRealizationHostError) as caught:
        _extract(model.to_string().encode(), _selector(CurrentIfcAxisBoundary.AXIS_START))
    assert caught.value.code == ProductRealizationHostErrorCode.BODY_SECTION_AMBIGUOUS
    for section in body.sections:
        selector = _selector(CurrentIfcAxisBoundary.AXIS_END).model_copy(update={"body_section_ref": body.section_reference(section)})
        fact, = _extract(model.to_string().encode(), selector)
        assert fact.attachment.project_world_frame.translation_m == section.centerline_end_m
        assert not fact.body_reference_remapped


@pytest.mark.parametrize("new_intervals,boundary,accepted", [
    ([(0.2, 1.8)], "axis_start", True),
    ([(0.2, 1.5)], "axis_start", True),
    ([(0.5, 1.8)], "axis_end", True),
    ([(0.2, 0.7), (1.1, 1.8)], "axis_start", True),
    ([(0.2, 0.7), (1.1, 1.8)], "axis_end", True),
    ([(0.3, 1.8)], "axis_start", False),
    ([(0.2, 1.7)], "axis_end", False),
    ([(0.2, 1.9)], "axis_start", False),
])
def test_stale_reference_only_maps_unique_retained_boundary(new_intervals, boundary, accepted):
    old = _inspect_body(_sectioned_model([(0.2, 1.8)]))
    selector = _selector(CurrentIfcAxisBoundary(boundary)).model_copy(update={"body_section_ref": old.section_reference(old.sections[0])})
    model = _sectioned_model(new_intervals)
    model.by_guid(PIPE_GLOBAL_ID).Name = "new revision"
    if accepted:
        fact, = _extract(model.to_string().encode(), selector)
        assert fact.body_reference_remapped
        assert fact.body_section_ref.ifc_sha256 == hashlib.sha256(model.to_string().encode()).hexdigest()
    else:
        with pytest.raises(ProductRealizationHostError) as caught:
            _extract(model.to_string().encode(), selector)
        assert caught.value.code == ProductRealizationHostErrorCode.BODY_REFERENCE_STALE


@pytest.mark.parametrize("mutation,code", [
    ("profile", "BODY_REFERENCE_STALE"), ("axis", "BODY_REFERENCE_STALE"),
    ("trim_parameter", "BODY_UNSUPPORTED"), ("mixed_profile", "PROFILE_AMBIGUOUS"),
    ("zero", "BODY_INVALID"), ("overlap", "PROFILE_AMBIGUOUS"),
    ("touch", "PROFILE_AMBIGUOUS"), ("outside", "PROFILE_INVALID"), ("off_axis", "PROFILE_INVALID"),
])
def test_current_body_rejects_unsupported_or_changed_geometry(mutation, code):
    model = _sectioned_model([(0.2, 0.7), (1.1, 1.8)])
    body = _inspect_body(model)
    selector = _selector(CurrentIfcAxisBoundary.AXIS_START).model_copy(update={"body_section_ref": body.section_reference(body.sections[0])})
    solids = model.by_type("IfcSweptDiskSolid")[-2:]
    if mutation == "profile":
        for solid in solids:
            solid.Radius = 0.06
    elif mutation == "mixed_profile":
        solids[1].Radius = 0.05000000001
    elif mutation == "axis":
        axis = model.by_guid(PIPE_GLOBAL_ID).Representation.Representations[0].Items[0]
        axis.Points[1].Coordinates = (2.1, 0.0, 0.0)
    elif mutation == "trim_parameter":
        solids[0].StartParam = 0.0
    elif mutation == "zero":
        solids[0].Directrix = _polyline(model, (0.2, 0.0, 0.0), (0.2, 0.0, 0.0))
    elif mutation in ("overlap", "touch"):
        solids[1].Directrix = _polyline(model, (0.6 if mutation == "overlap" else 0.7, 0.0, 0.0), (1.8, 0.0, 0.0))
    else:
        solids[0].Directrix = _polyline(model, (-0.1, 0.0, 0.0) if mutation == "outside" else (0.2, 0.01, 0.0), (0.7, 0.0, 0.0))
    with pytest.raises(ProductRealizationHostError) as caught:
        _extract(model.to_string().encode(), selector)
    assert caught.value.code == getattr(ProductRealizationHostErrorCode, code)


def test_editable_current_body_keeps_exact_sections_and_independent_boundary_permissions():
    model = _sectioned_model([(0.2, 0.8), (1.2, 1.8)])
    body = _inspect_body(model)
    selectors = tuple(CurrentIfcProductBoundarySelector(
        product_global_id=PIPE_GLOBAL_ID, boundary=boundary, attachment_key="pipe", interface_key=key,
        body_section_ref=body.section_reference(section),
        edit_permission=CurrentIfcBodyEndpointEditPermission(connection_station_interval_m=permission,
            required_retained_station_intervals_m=((0.2, 0.5), (1.5, 1.8))),
    ) for section, boundary, key, permission in (
        (body.sections[0], "axis_end", "inlet", (0.5, 0.8)),
        (body.sections[1], "axis_start", "outlet", (1.2, 1.5)),
    ))
    CurrentIfcHostFactRequest(selectors=selectors)
    facts = _extract(model.to_string().encode(), *selectors)
    assert facts[0].attachment == facts[1].attachment
    host = facts[0].attachment
    assert host.body_station_intervals_m == ((0.2, 0.8), (1.2, 1.8))
    # Extraction preserves actual material gaps and granted bounds as facts.
    # Current Family execution does not consume them or propose Host edits.
    assert host.required_retained_station_intervals_m == ((0.2, 0.5), (1.5, 1.8))
    boundaries = {boundary.boundary_key: boundary for boundary in host.boundaries}
    assert len(boundaries) == 2
    assert {
        boundary.connection_station_interval_m for boundary in boundaries.values()
    } == {(0.5, 0.8), (1.2, 1.5)}


@pytest.mark.parametrize("permission,retained,valid", [
    ((0.2, 0.2), ((0.2, 0.8), (1.2, 1.8)), True),
    ((0.2, 0.3), ((0.3, 0.8), (1.2, 1.8)), True),
    ((0.1, 0.3), ((0.3, 0.8),), False),
    ((0.2, 1.3), ((1.3, 1.8),), False),
    ((0.2, 0.3), ((0.7, 1.3),), False),
])
def test_current_edit_permissions_are_bound_to_actual_material(permission, retained, valid):
    model = _sectioned_model([(0.2, 0.8), (1.2, 1.8)])
    body = _inspect_body(model)
    selector = CurrentIfcProductBoundarySelector(
        product_global_id=PIPE_GLOBAL_ID, boundary="axis_start", attachment_key="pipe", interface_key="inlet",
        body_section_ref=body.section_reference(body.sections[0]),
        edit_permission=CurrentIfcBodyEndpointEditPermission(connection_station_interval_m=permission,
            required_retained_station_intervals_m=retained),
    )
    if valid:
        fact, = _extract(model.to_string().encode(), selector)
        if permission[0] == permission[1]:
            assert fact.attachment.editable_station_intervals_m == ()
    else:
        with pytest.raises(ProductRealizationHostError) as caught:
            _extract(model.to_string().encode(), selector)
        assert caught.value.code == ProductRealizationHostErrorCode.EDIT_PERMISSION_INVALID


def _frame_reference(frame) -> tuple[float, float, float]:
    return tuple(frame.rotation[index][0] for index in range(3))


def test_start_and_end_boundaries_use_project_world_transform_and_outward_normals() -> None:
    content = _model()
    original_sha = hashlib.sha256(content).hexdigest()

    start, end = _extract(
        content,
        _selector(CurrentIfcAxisBoundary.AXIS_START),
        _selector(
            CurrentIfcAxisBoundary.AXIS_END,
            attachment_key="side_b",
            interface_key="outlet",
        ),
    )

    assert start.attachment.project_world_frame.translation_m == pytest.approx((10.0, 20.0, 30.0))
    assert end.attachment.project_world_frame.translation_m == pytest.approx((10.0, 22.0, 30.0))
    assert _frame_axis(start.attachment.project_world_frame) == pytest.approx((0.0, -1.0, 0.0))
    assert _frame_axis(end.attachment.project_world_frame) == pytest.approx((0.0, 1.0, 0.0))
    assert _frame_reference(start.attachment.project_world_frame) == pytest.approx((0.0, 0.0, 1.0))
    assert _frame_reference(end.attachment.project_world_frame) == pytest.approx((0.0, 0.0, 1.0))
    assert np.linalg.det(
        np.asarray(start.attachment.project_world_frame.rotation)
    ) == pytest.approx(1.0)
    assert start.attachment.profile.outer_diameter_m == pytest.approx(0.1)
    assert end.attachment.profile.outer_diameter_m == pytest.approx(0.1)
    assert start.interface_binding.interface_key == "inlet"
    assert start.interface_binding.attachment_key == "side_a"
    assert hashlib.sha256(content).hexdigest() == original_sha


def test_vertical_axis_uses_stable_project_x_reference_fallback() -> None:
    (fact,) = _extract(
        _model(vertical=True),
        _selector(CurrentIfcAxisBoundary.AXIS_END),
    )

    assert _frame_axis(fact.attachment.project_world_frame) == pytest.approx((0.0, 0.0, 1.0))
    assert _frame_reference(fact.attachment.project_world_frame) == pytest.approx((1.0, 0.0, 0.0))


def test_project_units_are_normalized_to_metres() -> None:
    (fact,) = _extract(
        _model(millimetres=True),
        _selector(CurrentIfcAxisBoundary.AXIS_END),
    )

    assert fact.attachment.project_world_frame.translation_m == pytest.approx((10.0, 22.0, 30.0))
    assert fact.attachment.profile.outer_diameter_m == pytest.approx(0.1)


def test_unsegmented_two_point_indexed_axis_is_supported() -> None:
    (fact,) = _extract(
        _model(indexed_axis=True),
        _selector(CurrentIfcAxisBoundary.AXIS_END),
    )

    assert fact.attachment.project_world_frame.translation_m == pytest.approx((10.0, 22.0, 30.0))


@pytest.mark.parametrize(
    ("model_values", "expected_code"),
    [
        ({"include_axis": False}, ProductRealizationHostErrorCode.AXIS_MISSING),
        ({"duplicate_axis": True}, ProductRealizationHostErrorCode.AXIS_AMBIGUOUS),
        ({"include_body": False}, ProductRealizationHostErrorCode.PROFILE_MISSING),
        ({"duplicate_body_item": True}, ProductRealizationHostErrorCode.PROFILE_AMBIGUOUS),
        ({"duplicate_global_id": True}, ProductRealizationHostErrorCode.PRODUCT_AMBIGUOUS),
        ({"include_placement": False}, ProductRealizationHostErrorCode.PLACEMENT_INVALID),
        ({"coincident_axis": True}, ProductRealizationHostErrorCode.AXIS_INVALID),
    ],
)
def test_missing_or_ambiguous_host_facts_fail_closed(model_values, expected_code) -> None:
    with pytest.raises(ProductRealizationHostError) as caught:
        _extract(
            _model(**model_values),
            _selector(CurrentIfcAxisBoundary.AXIS_START),
        )

    assert caught.value.code is expected_code


def test_wrong_global_id_and_wrong_product_class_fail_closed() -> None:
    with pytest.raises(ProductRealizationHostError) as missing:
        _extract(
            _model(),
            _selector(CurrentIfcAxisBoundary.AXIS_START, global_id=OTHER_GLOBAL_ID),
        )
    assert missing.value.code is ProductRealizationHostErrorCode.PRODUCT_NOT_FOUND

    with pytest.raises(ProductRealizationHostError) as wrong_class:
        _extract(
            _model(product_class="IfcValve"),
            _selector(CurrentIfcAxisBoundary.AXIS_START),
        )
    assert wrong_class.value.code is ProductRealizationHostErrorCode.PRODUCT_CLASS_UNSUPPORTED


def test_request_rejects_duplicate_family_mapping_before_execution() -> None:
    with pytest.raises(ValidationError, match="attachment keys must be unique"):
        CurrentIfcHostFactRequest(
            selectors=(
                _selector(CurrentIfcAxisBoundary.AXIS_START),
                _selector(
                    CurrentIfcAxisBoundary.AXIS_END,
                    interface_key="outlet",
                ),
            )
        )


def _scope() -> InvocationScope:
    return InvocationScope(
        identity=TenantExecutionContext(
            tenant_id="tenant-host",
            principal_id="principal-host",
            task_id="task-host",
            actor_kind=TenantActorKind.USER,
            command_id="command-host",
        ),
        owner_id="tenant-host",
        run_id="run-host",
        execution_mode=ExecutionMode.DIRECT_MCP,
        catalog_frontier=None,
    )



@pytest.mark.asyncio
async def test_supervised_host_business_failure_keeps_original_code(tmp_path):
    from CAD2BIM_backend.ports.ifc_revision_runtime import InspectExactIfcRevisionRequest

    content = _model(product_class="IfcBuildingElementProxy")
    source = BytesIfcCandidate(content, sha256=hashlib.sha256(content).hexdigest())
    runtime = IfcOpenShellRevisionRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(ProductRealizationHostError) as caught:
            await runtime.execute(
                InspectExactIfcRevisionRequest(
                    projection="host_facts",
                    host_request=CurrentIfcHostFactRequest(
                        selectors=(_selector(CurrentIfcAxisBoundary.AXIS_END),),
                    ),
                ),
                source=source,
            )
        assert caught.value.code is ProductRealizationHostErrorCode.PRODUCT_CLASS_UNSUPPORTED
    finally:
        await source.aclose()
        await runtime.close()
