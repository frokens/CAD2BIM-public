from __future__ import annotations

from datetime import datetime, timezone

import ifcopenshell.api.geometry
import ifcopenshell.api.aggregate
import ifcopenshell.api.nest
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.guid
import ifcopenshell.util.placement
import numpy as np
import pytest

import CAD2BIM_backend.infrastructure.ifc.placement as placement_support
import CAD2BIM_backend.infrastructure.ifc.spatial_structure as spatial_structure_support
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    CreateIfcSpatialOperation,
    DeleteIfcSpatialOperation,
    IfcClientRefSpatialParentReference,
    IfcExistingSpatialParentReference,
    IfcProjectParentReference,
    IfcProjectWorldPlacement,
    IfcSpatialCycleError,
    IfcSpatialNotEmptyError,
    IfcSpatialParentInvalidError,
    IfcSpatialPlacementInvalidError,
    IfcSpatialResourceLimitError,
    IfcSpatialStructureIntegrityError,
    IfcSpatialStructureListQuery,
    ReparentIfcSpatialOperation,
    SetIfcSpatialPlacementOperation,
    UpdateIfcSpatialOperation,
)
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.ifc.spatial_structure import (
    IfcOpenShellSpatialStructureCompiler,
)
from CAD2BIM_backend.infrastructure.ifc.placement import placement_matrix


PROJECT_GLOBAL_ID = "1xvJu1wbjMywJMjRBpLw$1"
HEADER_TIMESTAMP = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


def _codec() -> IfcOpenShellModelCodec:
    return IfcOpenShellModelCodec()


def _compiler() -> IfcOpenShellSpatialStructureCompiler:
    return IfcOpenShellSpatialStructureCompiler(_codec())


def _revision1() -> bytes:
    return _codec().create_revision1(
        project_global_id=PROJECT_GLOBAL_ID,
        project_name="Spatial Test",
        header_timestamp=HEADER_TIMESTAMP,
    ).content


def _placement(
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
    ref_direction: tuple[float, float, float] = (1.0, 0.0, 0.0),
) -> IfcProjectWorldPlacement:
    return IfcProjectWorldPlacement(
        origin=origin,
        axis=(0.0, 0.0, 1.0),
        ref_direction=ref_direction,
    )


def _complex_tree_command() -> ApplyIfcSpatialStructureCommand:
    # Intentionally in reverse dependency order to exercise forward refs.
    return ApplyIfcSpatialStructureCommand(
        operations=(
            CreateIfcSpatialOperation(
                client_ref="room",
                spatial_class="space",
                parent=IfcClientRefSpatialParentReference(client_ref="storey"),
                project_world_placement=_placement((114.0, 53.0, 9.0), (0.0, -1.0, 0.0)),
                name="Room",
                composition_type="ELEMENT",
                predefined_type="INTERNAL",
            ),
            CreateIfcSpatialOperation(
                client_ref="storey",
                spatial_class="building_storey",
                parent=IfcClientRefSpatialParentReference(client_ref="building"),
                project_world_placement=_placement((110.0, 50.0, 9.0), (-1.0, 0.0, 0.0)),
                name="Storey",
                composition_type="ELEMENT",
            ),
            CreateIfcSpatialOperation(
                client_ref="building",
                spatial_class="building",
                parent=IfcClientRefSpatialParentReference(client_ref="site"),
                project_world_placement=_placement((110.0, 50.0, 3.0), (0.0, 1.0, 0.0)),
                name="Building",
                composition_type="ELEMENT",
            ),
            CreateIfcSpatialOperation(
                client_ref="site",
                spatial_class="site",
                parent=IfcProjectParentReference(),
                project_world_placement=_placement((100.0, 50.0, 3.0), (0.0, 1.0, 0.0)),
                name="Site",
                composition_type="ELEMENT",
                ref_latitude=31.2304,
                ref_longitude=121.4737,
            ),
        )
    )


def _catalog_by_name(content: bytes) -> dict[str, object]:
    return {item.name: item for item in _compiler().inspect_spatial_structure(content).items}


def test_forward_refs_compile_relative_placements_and_deterministic_global_ids() -> None:
    compiler = _compiler()
    base = _revision1()
    command = _complex_tree_command()
    first = compiler.apply(base, command)
    second = compiler.apply(base, command)

    assert first.content == second.content
    catalog = compiler.inspect_spatial_structure(first.content)
    by_name = {item.name: item for item in catalog.items}
    assert by_name["Site"].parent_ifc_class == "IfcProject"
    assert by_name["Building"].parent_global_id == by_name["Site"].global_id
    assert by_name["Storey"].parent_global_id == by_name["Building"].global_id
    assert by_name["Room"].parent_global_id == by_name["Storey"].global_id
    assert by_name["Room"].project_world_placement.origin == pytest.approx((114.0, 53.0, 9.0))
    assert by_name["Site"].ref_latitude == pytest.approx(31.2304, abs=1e-8)

    model = _codec().private_copy(first.content).model
    relation_ids = [relation.GlobalId for relation in model.by_type("IfcRelAggregates")]
    assert len(relation_ids) == len(set(relation_ids)) == 4
    assert all(isinstance(value, str) and len(value) == 22 for value in relation_ids)
    for item in catalog.items:
        entity = model.by_guid(item.global_id)
        parent = model.by_guid(item.parent_global_id)
        expected = None if parent.is_a("IfcProject") else parent.ObjectPlacement
        assert entity.ObjectPlacement.PlacementRelTo == expected


@pytest.mark.parametrize(
    ("parent_class", "parent_composition", "child_class", "child_composition"),
    [
        ("site", "COMPLEX", "building", "ELEMENT"),
        ("site", "ELEMENT", "building_storey", "ELEMENT"),
        ("building", "ELEMENT", "space", "ELEMENT"),
        ("building", "ELEMENT", "building", "PARTIAL"),
        ("building", "COMPLEX", "building", "ELEMENT"),
        ("building_storey", "ELEMENT", "space", "PARTIAL"),
    ],
)
def test_parent_child_and_composition_matrix_rejects_invalid_pairs(
    parent_class: str,
    parent_composition: str,
    child_class: str,
    child_composition: str,
) -> None:
    parent_parent = (
        IfcProjectParentReference()
        if parent_class == "site"
        else IfcClientRefSpatialParentReference(client_ref="site")
    )
    operations = [
        CreateIfcSpatialOperation(
            client_ref="child",
            spatial_class=child_class,
            parent=IfcClientRefSpatialParentReference(client_ref="parent"),
            project_world_placement=_placement(),
            name="Child",
            composition_type=child_composition,
        ),
        CreateIfcSpatialOperation(
            client_ref="parent",
            spatial_class=parent_class,
            parent=parent_parent,
            project_world_placement=_placement(),
            name="Parent",
            composition_type=parent_composition,
        ),
    ]
    if parent_class != "site":
        operations.append(
            CreateIfcSpatialOperation(
                client_ref="site",
                spatial_class="site",
                parent=IfcProjectParentReference(),
                project_world_placement=_placement(),
                name="Site",
                composition_type="ELEMENT",
            )
        )
    with pytest.raises(IfcSpatialParentInvalidError):
        _compiler().apply(_revision1(), ApplyIfcSpatialStructureCommand(operations=tuple(operations)))


def test_final_candidate_cycle_is_rejected_without_mutating_base() -> None:
    compiler = _compiler()
    encoded = compiler.apply(_revision1(), _complex_tree_command())
    by_name = {item.name: item for item in compiler.inspect_spatial_structure(encoded.content).items}
    command = ApplyIfcSpatialStructureCommand(
        operations=(
            ReparentIfcSpatialOperation(
                target_global_id=by_name["Building"].global_id,
                new_parent=IfcExistingSpatialParentReference(global_id=by_name["Storey"].global_id),
            ),
        )
    )
    with pytest.raises(IfcSpatialCycleError):
        compiler.apply(encoded.content, command)
    assert compiler.inspect_spatial_structure(encoded.content).model_dump() == compiler.inspect_spatial_structure(encoded.content).model_dump()


def test_reparent_preserves_target_and_descendant_world_then_old_parent_can_delete() -> None:
    compiler = _compiler()
    first = compiler.apply(_revision1(), _complex_tree_command())
    by_name = {item.name: item for item in compiler.inspect_spatial_structure(first.content).items}
    with_second = compiler.apply(
        first.content,
        ApplyIfcSpatialStructureCommand(
            operations=(
                CreateIfcSpatialOperation(
                    client_ref="building2",
                    spatial_class="building",
                    parent=IfcExistingSpatialParentReference(global_id=by_name["Site"].global_id),
                    project_world_placement=_placement((80.0, 40.0, 3.0)),
                    name="Building 2",
                    composition_type="ELEMENT",
                ),
            )
        ),
    )
    current = {item.name: item for item in compiler.inspect_spatial_structure(with_second.content).items}
    moved = compiler.apply(
        with_second.content,
        ApplyIfcSpatialStructureCommand(
            operations=(
                ReparentIfcSpatialOperation(
                    target_global_id=current["Storey"].global_id,
                    new_parent=IfcExistingSpatialParentReference(global_id=current["Building 2"].global_id),
                ),
                DeleteIfcSpatialOperation(target_global_id=current["Building"].global_id),
            )
        ),
    )
    final = {item.name: item for item in compiler.inspect_spatial_structure(moved.content).items}
    assert "Building" not in final
    assert final["Storey"].parent_global_id == final["Building 2"].global_id
    assert final["Storey"].project_world_placement == current["Storey"].project_world_placement
    assert final["Room"].project_world_placement == current["Room"].project_world_placement


def test_delete_parent_with_child_reports_not_empty_before_candidate_parent_error() -> None:
    compiler = _compiler()
    encoded = compiler.apply(_revision1(), _complex_tree_command())
    by_name = {item.name: item for item in compiler.inspect_spatial_structure(encoded.content).items}
    with pytest.raises(IfcSpatialNotEmptyError) as captured:
        compiler.apply(
            encoded.content,
            ApplyIfcSpatialStructureCommand(
                operations=(
                    DeleteIfcSpatialOperation(
                        target_global_id=by_name["Building"].global_id
                    ),
                )
            ),
        )
    assert captured.value.code == "IFC_SPATIAL_NOT_EMPTY"
    assert captured.value.blockers.child_spatial_count == 1
    assert captured.value.blockers.total_count == 1
    assert compiler.inspect_spatial_structure(encoded.content).items


def test_default_move_is_rigid_and_preserve_descendants_compensates() -> None:
    compiler = _compiler()
    first = compiler.apply(_revision1(), _complex_tree_command())
    before = {item.name: item for item in compiler.inspect_spatial_structure(first.content).items}
    moved = compiler.apply(
        first.content,
        ApplyIfcSpatialStructureCommand(
            operations=(
                SetIfcSpatialPlacementOperation(
                    target_global_id=before["Site"].global_id,
                    project_world_placement=_placement((200.0, 100.0, 3.0), (-1.0, 0.0, 0.0)),
                ),
            )
        ),
    )
    after_rigid = {item.name: item for item in compiler.inspect_spatial_structure(moved.content).items}
    old_site_matrix = placement_matrix(before["Site"].project_world_placement)
    new_site_matrix = placement_matrix(after_rigid["Site"].project_world_placement)
    delta = new_site_matrix @ np.linalg.inv(old_site_matrix)
    for name in ("Building", "Storey", "Room"):
        expected = delta @ np.array((*before[name].project_world_placement.origin, 1.0))
        assert after_rigid[name].project_world_placement.origin == pytest.approx(expected[:3])

    preserved = compiler.apply(
        moved.content,
        ApplyIfcSpatialStructureCommand(
            operations=(
                SetIfcSpatialPlacementOperation(
                    target_global_id=before["Site"].global_id,
                    project_world_placement=_placement((0.0, 0.0, 0.0)),
                    preserve_descendant_project_world=True,
                ),
            )
        ),
    )
    after_preserve = {item.name: item for item in compiler.inspect_spatial_structure(preserved.content).items}
    for name in ("Building", "Storey", "Room"):
        assert after_preserve[name].project_world_placement == after_rigid[name].project_world_placement


def test_bulk_move_visits_closure_once_skips_satisfied_descendants_and_never_recurses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler = _compiler()
    encoded = compiler.apply(_revision1(), _complex_tree_command())
    by_name = {
        item.name: item for item in compiler.inspect_spatial_structure(encoded.content).items
    }
    private = _codec().private_copy(encoded.content)
    model = private.model
    wall = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcWall", name="Absolute contained wall"
    )
    wall_matrix = np.eye(4)
    wall_matrix[:3, 3] = (120.0, 60.0, 9.0)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=wall, matrix=wall_matrix, is_si=False
    )
    room = model.by_guid(by_name["Room"].global_id)
    ifcopenshell.api.spatial.assign_container(
        model, products=[wall], relating_structure=room
    )
    # Deliberately non-local: it is still in the semantic closure by primary
    # containment, but it cannot move merely by following the Site chain.
    wall.ObjectPlacement.PlacementRelTo = None
    imported = private.to_bytes()

    helper_calls: list[tuple[str, bool]] = []
    api_calls: list[tuple[str, bool]] = []
    original_helper = placement_support.set_project_world_matrix
    original_api = ifcopenshell.api.geometry.edit_object_placement

    def counted_helper(
        current_model,
        product,
        matrix,
        *,
        should_transform_children: bool = True,
    ):
        helper_calls.append((product.GlobalId, should_transform_children))
        return original_helper(
            current_model,
            product,
            matrix,
            should_transform_children=should_transform_children,
        )

    def counted_api(current_model, **settings):
        api_calls.append(
            (
                settings["product"].GlobalId,
                settings["should_transform_children"],
            )
        )
        return original_api(current_model, **settings)

    monkeypatch.setattr(
        placement_support, "set_project_world_matrix", counted_helper
    )
    monkeypatch.setattr(
        ifcopenshell.api.geometry, "edit_object_placement", counted_api
    )
    compiler.apply(
        imported,
        ApplyIfcSpatialStructureCommand(
            operations=(
                SetIfcSpatialPlacementOperation(
                    target_global_id=by_name["Site"].global_id,
                    project_world_placement=_placement((105.0, 50.0, 3.0)),
                ),
            )
        ),
    )

    expected_ids = {
        by_name[name].global_id for name in ("Site", "Building", "Storey", "Room")
    } | {wall.GlobalId}
    assert [global_id for global_id, _ in helper_calls] == list(
        dict.fromkeys(global_id for global_id, _ in helper_calls)
    )
    assert {global_id for global_id, _ in helper_calls} == expected_ids
    assert all(flag is False for _, flag in helper_calls)
    # The chained spatial descendants already reached their delta target and
    # are skipped.  The absolute contained wall remains an explicit write.
    assert {global_id for global_id, _ in api_calls} == {
        by_name["Site"].global_id,
        wall.GlobalId,
    }
    assert all(flag is True for _, flag in api_calls)


def test_bulk_reparent_visits_each_descendant_once_without_aggregate_recursion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler = _compiler()
    first = compiler.apply(_revision1(), _complex_tree_command())
    initial = {
        item.name: item for item in compiler.inspect_spatial_structure(first.content).items
    }
    with_second = compiler.apply(
        first.content,
        ApplyIfcSpatialStructureCommand(
            operations=(
                CreateIfcSpatialOperation(
                    client_ref="building2",
                    spatial_class="building",
                    parent=IfcExistingSpatialParentReference(
                        global_id=initial["Site"].global_id
                    ),
                    project_world_placement=_placement((80.0, 40.0, 3.0)),
                    name="Building 2",
                    composition_type="ELEMENT",
                ),
            )
        ),
    )
    current = {
        item.name: item
        for item in compiler.inspect_spatial_structure(with_second.content).items
    }

    helper_calls: list[tuple[str, bool]] = []
    api_calls: list[tuple[str, bool]] = []
    original_helper = spatial_structure_support.set_project_world_matrix
    original_api = ifcopenshell.api.geometry.edit_object_placement

    def counted_helper(
        current_model,
        product,
        matrix,
        *,
        should_transform_children: bool = True,
    ):
        helper_calls.append((product.GlobalId, should_transform_children))
        return original_helper(
            current_model,
            product,
            matrix,
            should_transform_children=should_transform_children,
        )

    def counted_api(current_model, **settings):
        api_calls.append(
            (
                settings["product"].GlobalId,
                settings["should_transform_children"],
            )
        )
        return original_api(current_model, **settings)

    monkeypatch.setattr(
        spatial_structure_support, "set_project_world_matrix", counted_helper
    )
    monkeypatch.setattr(
        ifcopenshell.api.geometry, "edit_object_placement", counted_api
    )
    compiler.apply(
        with_second.content,
        ApplyIfcSpatialStructureCommand(
            operations=(
                ReparentIfcSpatialOperation(
                    target_global_id=current["Storey"].global_id,
                    new_parent=IfcExistingSpatialParentReference(
                        global_id=current["Building 2"].global_id
                    ),
                ),
            )
        ),
    )

    assert helper_calls == [
        (current["Storey"].global_id, False),
        (current["Room"].global_id, False),
    ]
    assert api_calls == [(current["Storey"].global_id, True)]


@pytest.mark.parametrize("action", ["reparent", "set_project_world_placement"])
def test_spatial_placement_impact_budget_fails_before_batch_mutation(
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler = _compiler()
    encoded = compiler.apply(_revision1(), _complex_tree_command())
    by_name = {
        item.name: item for item in compiler.inspect_spatial_structure(encoded.content).items
    }
    operation = (
        ReparentIfcSpatialOperation(
            target_global_id=by_name["Site"].global_id,
            new_parent=IfcProjectParentReference(),
        )
        if action == "reparent"
        else SetIfcSpatialPlacementOperation(
            target_global_id=by_name["Site"].global_id,
            project_world_placement=_placement((500.0, 0.0, 0.0)),
        )
    )
    original_validator = placement_support.validate_semantic_placement_closure

    def bounded_validator(root):
        return original_validator(root, max_entities=2, max_work_units=1_000)

    applied = False

    def unexpected_apply(*args, **kwargs):
        nonlocal applied
        applied = True
        raise AssertionError("batch mutation must not begin after impact budget failure")

    monkeypatch.setattr(
        spatial_structure_support,
        "validate_semantic_placement_closure",
        bounded_validator,
    )
    monkeypatch.setattr(spatial_structure_support, "_apply_batch", unexpected_apply)

    with pytest.raises(IfcSpatialResourceLimitError) as captured:
        compiler.apply(
            encoded.content,
            ApplyIfcSpatialStructureCommand(operations=(operation,)),
        )
    assert captured.value.code == "IFC_SPATIAL_RESOURCE_LIMIT"
    assert applied is False


def test_semantic_traversal_has_independent_entity_and_work_unit_early_stops() -> None:
    compiler = _compiler()
    encoded = compiler.apply(_revision1(), _complex_tree_command())
    model = _codec().private_copy(encoded.content).model
    site = model.by_type("IfcSite", include_subtypes=False)[0]

    with pytest.raises(IfcSpatialResourceLimitError, match="entity limit"):
        placement_support.validate_semantic_placement_closure(
            site, max_entities=2, max_work_units=1_000
        )
    with pytest.raises(IfcSpatialResourceLimitError, match="work-unit limit"):
        placement_support.validate_semantic_placement_closure(
            site, max_entities=100, max_work_units=1
        )


def test_primary_contained_product_moves_with_subtree_and_blocks_delete() -> None:
    compiler = _compiler()
    encoded = compiler.apply(_revision1(), _complex_tree_command())
    by_name = {item.name: item for item in compiler.inspect_spatial_structure(encoded.content).items}
    private = _codec().private_copy(encoded.content)
    model = private.model
    wall = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWall", name="Wall")
    wall_matrix = np.eye(4)
    wall_matrix[:3, 3] = (115.0, 53.0, 9.0)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=wall, matrix=wall_matrix, is_si=False
    )
    ifcopenshell.api.spatial.assign_container(
        model, products=[wall], relating_structure=model.by_guid(by_name["Room"].global_id)
    )
    with_wall = private.to_bytes()

    with pytest.raises(IfcSpatialNotEmptyError) as captured:
        compiler.apply(
            with_wall,
            ApplyIfcSpatialStructureCommand(
                operations=(DeleteIfcSpatialOperation(target_global_id=by_name["Room"].global_id),)
            ),
        )
    assert captured.value.blockers.contained_element_count == 1
    assert captured.value.blockers.external_placement_count == 0

    moved = compiler.apply(
        with_wall,
        ApplyIfcSpatialStructureCommand(
            operations=(
                SetIfcSpatialPlacementOperation(
                    target_global_id=by_name["Site"].global_id,
                    project_world_placement=_placement((110.0, 50.0, 3.0), (0.0, 1.0, 0.0)),
                ),
            )
        ),
    )
    moved_model = _codec().private_copy(moved.content).model
    moved_wall = moved_model.by_guid(wall.GlobalId)
    world = ifcopenshell.util.placement.get_local_placement(moved_wall.ObjectPlacement)
    old_site = placement_matrix(by_name["Site"].project_world_placement)
    new_site = placement_matrix(
        next(item for item in compiler.inspect_spatial_structure(moved.content).items if item.name == "Site").project_world_placement
    )
    expected = (new_site @ np.linalg.inv(old_site)) @ np.array((115.0, 53.0, 9.0, 1.0))
    assert tuple(world[:3, 3]) == pytest.approx(expected[:3])


def test_list_and_delete_share_complete_nonduplicated_blocker_classification() -> None:
    compiler = _compiler()
    encoded = compiler.apply(_revision1(), _complex_tree_command())
    catalog = compiler.inspect_spatial_structure(encoded.content)
    room_summary = next(item for item in catalog.items if item.name == "Room")
    private = _codec().private_copy(encoded.content)
    model = private.model
    room = model.by_guid(room_summary.global_id)

    wall = ifcopenshell.api.root.create_entity(model, ifc_class="IfcWall", name="Boundary")
    covering = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcCovering", name="Covering"
    )
    external = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcBuildingElementProxy", name="External placement user"
    )
    external.ObjectPlacement = model.create_entity(
        "IfcLocalPlacement",
        PlacementRelTo=room.ObjectPlacement,
        RelativePlacement=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity(
                "IfcCartesianPoint", Coordinates=(1.0, 0.0, 0.0)
            ),
        ),
    )
    boundary = model.create_entity(
        "IfcRelSpaceBoundary",
        GlobalId=ifcopenshell.guid.new(),
        RelatingSpace=room,
        RelatedBuildingElement=wall,
        PhysicalOrVirtualBoundary="PHYSICAL",
        InternalOrExternalBoundary="INTERNAL",
    )
    covers = model.create_entity(
        "IfcRelCoversSpaces",
        GlobalId=ifcopenshell.guid.new(),
        RelatingSpace=room,
        RelatedCoverings=(covering,),
    )
    interference = model.create_entity(
        "IfcRelInterferesElements",
        GlobalId=ifcopenshell.guid.new(),
        RelatingElement=room,
        RelatedElement=wall,
    )
    other = model.create_entity(
        "IfcRelAssignsToProduct",
        GlobalId=ifcopenshell.guid.new(),
        RelatedObjects=(room,),
        RelatingProduct=wall,
    )
    imported = private.to_bytes()

    listed = compiler.list_spatial_structure(
        imported,
        IfcSpatialStructureListQuery(global_ids=(room_summary.global_id,)),
    ).items[0]
    assert listed.blockers.boundary_count == 1
    assert listed.blockers.covering_count == 1
    assert listed.blockers.interference_count == 1
    assert listed.blockers.external_placement_count == 1
    assert listed.blockers.other_business_reference_count == 1
    expected_samples = {
        boundary.GlobalId,
        covers.GlobalId,
        interference.GlobalId,
        external.GlobalId,
        other.GlobalId,
    }
    assert expected_samples.issubset(set(listed.blockers.sample_global_ids))
    assert len(listed.blockers.sample_global_ids) == len(
        set(listed.blockers.sample_global_ids)
    )

    with pytest.raises(IfcSpatialNotEmptyError) as captured:
        compiler.apply(
            imported,
            ApplyIfcSpatialStructureCommand(
                operations=(
                    DeleteIfcSpatialOperation(target_global_id=room_summary.global_id),
                )
            ),
        )
    assert captured.value.blockers == listed.blockers


def test_move_rejects_a_placement_dependant_outside_the_semantic_subtree() -> None:
    compiler = _compiler()
    encoded = compiler.apply(_revision1(), _complex_tree_command())
    by_name = {item.name: item for item in compiler.inspect_spatial_structure(encoded.content).items}
    private = _codec().private_copy(encoded.content)
    model = private.model
    unrelated = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcBuildingElementProxy", name="Unrelated"
    )
    room = model.by_guid(by_name["Room"].global_id)
    unrelated.ObjectPlacement = model.create_entity(
        "IfcLocalPlacement",
        PlacementRelTo=room.ObjectPlacement,
        RelativePlacement=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
        ),
    )
    imported = private.to_bytes()
    with pytest.raises(IfcSpatialPlacementInvalidError, match="outside"):
        compiler.apply(
            imported,
            ApplyIfcSpatialStructureCommand(
                operations=(
                    SetIfcSpatialPlacementOperation(
                        target_global_id=by_name["Site"].global_id,
                        project_world_placement=_placement((300.0, 0.0, 0.0)),
                    ),
                )
            ),
        )
    assert compiler.inspect_spatial_structure(imported).items


def test_move_closure_includes_owned_aggregate_nested_and_placement_products() -> None:
    compiler = _compiler()
    encoded = compiler.apply(_revision1(), _complex_tree_command())
    by_name = {item.name: item for item in compiler.inspect_spatial_structure(encoded.content).items}
    private = _codec().private_copy(encoded.content)
    model = private.model
    site = model.by_guid(by_name["Site"].global_id)

    assembly = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcElementAssembly", name="Contained assembly"
    )
    fitting = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcPipeFitting", name="Aggregated fitting"
    )
    valve = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcValve", name="Nested valve"
    )
    segment = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcPipeSegment", name="Placement dependant"
    )
    port = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcDistributionPort", name="Placement port"
    )
    opening = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcOpeningElement", name="Placement feature"
    )
    for product, x in ((assembly, 120.0), (fitting, 121.0), (valve, 122.0)):
        matrix = np.eye(4)
        matrix[:3, 3] = (x, 50.0, 3.0)
        ifcopenshell.api.geometry.edit_object_placement(
            model, product=product, matrix=matrix, is_si=False
        )
    ifcopenshell.api.spatial.assign_container(
        model, products=[assembly], relating_structure=site
    )
    ifcopenshell.api.aggregate.assign_object(
        model, products=[fitting], relating_object=assembly
    )
    ifcopenshell.api.nest.assign_object(
        model, related_objects=[valve], relating_object=assembly
    )
    # Mirrors the Golden regulator pattern: assembly and functional valve own
    # one placement, while ports/products depend on that shared anchor.
    valve.ObjectPlacement = assembly.ObjectPlacement
    for dependant, offset in ((segment, 1.0), (port, 2.0), (opening, 3.0)):
        dependant.ObjectPlacement = model.create_entity(
            "IfcLocalPlacement",
            PlacementRelTo=valve.ObjectPlacement,
            RelativePlacement=model.create_entity(
                "IfcAxis2Placement3D",
                Location=model.create_entity(
                    "IfcCartesianPoint", Coordinates=(offset, 0.0, 0.0)
                ),
            ),
        )
    imported = private.to_bytes()
    product_ids = {
        product.Name: product.GlobalId
        for product in (assembly, fitting, valve, segment, port, opening)
    }
    before = {
        name: ifcopenshell.util.placement.get_local_placement(
            model.by_guid(global_id).ObjectPlacement
        ).copy()
        for name, global_id in product_ids.items()
    }

    rigid = compiler.apply(
        imported,
        ApplyIfcSpatialStructureCommand(
            operations=(
                SetIfcSpatialPlacementOperation(
                    target_global_id=by_name["Site"].global_id,
                    project_world_placement=_placement((105.0, 50.0, 3.0), (0.0, 1.0, 0.0)),
                ),
            )
        ),
    )
    rigid_model = _codec().private_copy(rigid.content).model
    rigid_world = {
        name: ifcopenshell.util.placement.get_local_placement(
            rigid_model.by_guid(global_id).ObjectPlacement
        )
        for name, global_id in product_ids.items()
    }
    delta = placement_matrix(_placement((105.0, 50.0, 3.0), (0.0, 1.0, 0.0))) @ np.linalg.inv(
        placement_matrix(by_name["Site"].project_world_placement)
    )
    for name in product_ids:
        assert rigid_world[name] == pytest.approx(delta @ before[name])

    preserved = compiler.apply(
        rigid.content,
        ApplyIfcSpatialStructureCommand(
            operations=(
                SetIfcSpatialPlacementOperation(
                    target_global_id=by_name["Site"].global_id,
                    project_world_placement=_placement((300.0, 0.0, 3.0)),
                    preserve_descendant_project_world=True,
                ),
            )
        ),
    )
    preserved_model = _codec().private_copy(preserved.content).model
    for name, global_id in product_ids.items():
        world = ifcopenshell.util.placement.get_local_placement(
            preserved_model.by_guid(global_id).ObjectPlacement
        )
        assert world == pytest.approx(rigid_world[name])


def test_move_rejects_shared_placement_with_an_owner_outside_closure() -> None:
    compiler = _compiler()
    encoded = compiler.apply(_revision1(), _complex_tree_command())
    by_name = {item.name: item for item in compiler.inspect_spatial_structure(encoded.content).items}
    private = _codec().private_copy(encoded.content)
    model = private.model
    site = model.by_guid(by_name["Site"].global_id)
    contained = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcElementAssembly", name="Contained"
    )
    external = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcValve", name="External shared owner"
    )
    port = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcDistributionPort", name="Contained port"
    )
    matrix = np.eye(4)
    matrix[:3, 3] = (125.0, 50.0, 3.0)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=contained, matrix=matrix, is_si=False
    )
    ifcopenshell.api.spatial.assign_container(
        model, products=[contained], relating_structure=site
    )
    external.ObjectPlacement = contained.ObjectPlacement
    port.ObjectPlacement = model.create_entity(
        "IfcLocalPlacement",
        PlacementRelTo=contained.ObjectPlacement,
        RelativePlacement=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity(
                "IfcCartesianPoint", Coordinates=(1.0, 0.0, 0.0)
            ),
        ),
    )
    ifcopenshell.api.nest.assign_object(
        model, related_objects=[port], relating_object=contained
    )
    # nest.assign_object preserves the port world placement but rewrites its
    # placement, so restore the shared-anchor dependency under investigation.
    port.ObjectPlacement.PlacementRelTo = contained.ObjectPlacement
    imported = private.to_bytes()
    external_before = ifcopenshell.util.placement.get_local_placement(
        external.ObjectPlacement
    ).copy()
    with pytest.raises(IfcSpatialPlacementInvalidError, match="crosses"):
        compiler.apply(
            imported,
            ApplyIfcSpatialStructureCommand(
                operations=(
                    SetIfcSpatialPlacementOperation(
                        target_global_id=by_name["Site"].global_id,
                        project_world_placement=_placement((500.0, 0.0, 3.0)),
                    ),
                )
            ),
        )
    external_after = ifcopenshell.util.placement.get_local_placement(
        _codec().private_copy(imported).model.by_guid(external.GlobalId).ObjectPlacement
    )
    assert external_after == pytest.approx(external_before)


def test_update_clear_and_bounded_list_projection_round_trip_after_save() -> None:
    compiler = _compiler()
    encoded = compiler.apply(_revision1(), _complex_tree_command())
    before = {item.name: item for item in compiler.inspect_spatial_structure(encoded.content).items}
    updated = compiler.apply(
        encoded.content,
        ApplyIfcSpatialStructureCommand(
            operations=(
                UpdateIfcSpatialOperation.model_validate(
                    {
                        "target_global_id": before["Room"].global_id,
                        "patch": {"name": "Room 101", "object_type": "Control"},
                        "clear_fields": ["predefined_type"],
                    }
                ),
            )
        ),
    )
    page1 = compiler.list_spatial_structure(
        updated.content, IfcSpatialStructureListQuery(page_size=2)
    )
    assert page1.returned_count == 2
    assert page1.next_after is not None
    page2 = compiler.list_spatial_structure(
        updated.content,
        IfcSpatialStructureListQuery(page_size=2, after=page1.next_after),
    )
    assert page2.returned_count == 2
    room = compiler.list_spatial_structure(
        updated.content,
        IfcSpatialStructureListQuery(global_ids=(before["Room"].global_id,)),
    ).items[0]
    assert room.name == "Room 101"
    assert room.object_type == "Control"
    assert room.predefined_type is None


def test_strict_inspection_rejects_missing_relationship_global_id_and_placement_cycle() -> None:
    compiler = _compiler()
    encoded = compiler.apply(_revision1(), _complex_tree_command())

    private = _codec().private_copy(encoded.content)
    private.model.by_type("IfcRelAggregates")[0].GlobalId = "invalid"
    with pytest.raises(IfcSpatialStructureIntegrityError, match="GlobalId"):
        compiler.inspect_spatial_structure(private.to_bytes())

    private = _codec().private_copy(encoded.content)
    room = private.model.by_type("IfcSpace")[0]
    room.ObjectPlacement.PlacementRelTo = room.ObjectPlacement
    with pytest.raises(IfcSpatialStructureIntegrityError, match="cyclic"):
        compiler.inspect_spatial_structure(private.to_bytes())
