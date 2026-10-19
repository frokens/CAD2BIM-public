from __future__ import annotations

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    CreateIfcSpatialOperation,
    DeleteIfcSpatialOperation,
    IfcClientRefSpatialParentReference,
    IfcExistingSpatialParentReference,
    IfcProjectParentReference,
    IfcProjectWorldPlacement,
    IfcSpatialStructureCatalog,
    IfcSpatialStructureListQuery,
    MAX_IFC_SPATIAL_IMPACT_ENTITIES,
    MAX_IFC_SPATIAL_IMPACT_WORK_UNITS,
    ReparentIfcSpatialOperation,
    SetIfcSpatialPlacementOperation,
    UpdateIfcSpatialOperation,
    ifc_spatial_parent_child_violation,
)


GLOBAL_ID = "1xvJu1wbjMywJMjRBpLw$1"


def _placement() -> IfcProjectWorldPlacement:
    return IfcProjectWorldPlacement(
        origin=(0.0, 0.0, 0.0),
        axis=(0.0, 0.0, 1.0),
        ref_direction=(1.0, 0.0, 0.0),
    )


def test_spatial_impact_entity_budget_covers_the_current_golden_closure() -> None:
    assert MAX_IFC_SPATIAL_IMPACT_ENTITIES >= 2 * 73_316
    assert MAX_IFC_SPATIAL_IMPACT_WORK_UNITS >= 6 * 759_078


def test_project_world_placement_is_a_finite_unit_orthogonal_frame() -> None:
    assert _placement().axis == (0.0, 0.0, 1.0)
    for payload in (
        {"origin": (float("inf"), 0, 0), "axis": (0, 0, 1), "ref_direction": (1, 0, 0)},
        {"origin": (0, 0, 0), "axis": (0, 0, 2), "ref_direction": (1, 0, 0)},
        {"origin": (0, 0, 0), "axis": (0, 0, 1), "ref_direction": (0, 0, 1)},
    ):
        with pytest.raises(ValidationError):
            IfcProjectWorldPlacement.model_validate(payload)


def test_parent_references_are_closed_literal_branches() -> None:
    assert IfcProjectParentReference().kind == "project"
    assert IfcExistingSpatialParentReference(global_id=GLOBAL_ID).kind == "existing"
    assert IfcClientRefSpatialParentReference(client_ref="future.site").kind == "client_ref"
    with pytest.raises(ValidationError):
        IfcExistingSpatialParentReference(global_id="STEP#12")
    with pytest.raises(ValidationError, match="Extra inputs"):
        IfcProjectParentReference.model_validate({"kind": "project", "global_id": GLOBAL_ID})


def test_create_is_closed_and_supports_forward_client_ref() -> None:
    command = ApplyIfcSpatialStructureCommand(
        operations=(
            CreateIfcSpatialOperation(
                client_ref="building",
                spatial_class="building",
                parent=IfcClientRefSpatialParentReference(client_ref="site"),
                project_world_placement=_placement(),
                name="B",
                composition_type="ELEMENT",
            ),
            CreateIfcSpatialOperation(
                client_ref="site",
                spatial_class="site",
                parent=IfcProjectParentReference(),
                project_world_placement=_placement(),
                name="S",
                composition_type="ELEMENT",
            ),
        )
    )
    assert [operation.client_ref for operation in command.operations] == ["building", "site"]
    with pytest.raises(ValidationError, match="unique"):
        ApplyIfcSpatialStructureCommand(operations=(command.operations[0], command.operations[0]))


@pytest.mark.parametrize(
    ("parent_class", "parent_composition", "child_class", "child_composition"),
    [
        ("project", None, "site", "ELEMENT"),
        ("site", "ELEMENT", "building", "ELEMENT"),
        ("site", "ELEMENT", "space", "ELEMENT"),
        ("building", "ELEMENT", "building_storey", "ELEMENT"),
        ("building_storey", "ELEMENT", "space", "ELEMENT"),
        ("site", "COMPLEX", "site", "PARTIAL"),
        ("building", "COMPLEX", "building", "PARTIAL"),
        ("building_storey", "COMPLEX", "building_storey", "PARTIAL"),
        ("space", "COMPLEX", "space", "PARTIAL"),
    ],
)
def test_domain_owns_valid_spatial_parent_child_composition_rules(
    parent_class: str,
    parent_composition: str | None,
    child_class: str,
    child_composition: str,
) -> None:
    assert (
        ifc_spatial_parent_child_violation(
            parent_class, parent_composition, child_class, child_composition
        )
        is None
    )


@pytest.mark.parametrize(
    ("parent_class", "parent_composition", "child_class", "child_composition"),
    [
        ("project", None, "building", "ELEMENT"),
        ("project", None, "site", "PARTIAL"),
        ("site", "ELEMENT", "site", "PARTIAL"),
        ("site", "COMPLEX", "building", "ELEMENT"),
        ("building", "ELEMENT", "space", "ELEMENT"),
        ("building_storey", "ELEMENT", "space", "PARTIAL"),
    ],
)
def test_domain_rejects_invalid_spatial_parent_child_composition_rules(
    parent_class: str,
    parent_composition: str | None,
    child_class: str,
    child_composition: str,
) -> None:
    assert ifc_spatial_parent_child_violation(
        parent_class, parent_composition, child_class, child_composition
    )


def test_update_uses_patch_plus_explicit_clear_fields() -> None:
    update = UpdateIfcSpatialOperation.model_validate(
        {
            "action": "update",
            "target_global_id": GLOBAL_ID,
            "patch": {"name": "Room 01"},
            "clear_fields": ["description"],
        }
    )
    assert update.patch.model_fields_set == {"name"}
    with pytest.raises(ValidationError, match="use clear_fields"):
        UpdateIfcSpatialOperation.model_validate(
            {
                "target_global_id": GLOBAL_ID,
                "patch": {"description": None},
            }
        )
    with pytest.raises(ValidationError, match="patched and cleared"):
        UpdateIfcSpatialOperation.model_validate(
            {
                "target_global_id": GLOBAL_ID,
                "patch": {"description": "x"},
                "clear_fields": ["description"],
            }
        )


def test_target_operation_conflicts_are_rejected_before_compilation() -> None:
    with pytest.raises(ValidationError, match="delete_if_empty conflicts"):
        ApplyIfcSpatialStructureCommand(
            operations=(
                DeleteIfcSpatialOperation(target_global_id=GLOBAL_ID),
                ReparentIfcSpatialOperation(
                    target_global_id=GLOBAL_ID,
                    new_parent=IfcProjectParentReference(),
                ),
            )
        )
    command = ApplyIfcSpatialStructureCommand(
        operations=(
            ReparentIfcSpatialOperation(
                target_global_id=GLOBAL_ID,
                new_parent=IfcExistingSpatialParentReference(global_id="0JQoq6ucP7QPjLG_GA8jxj"),
            ),
            SetIfcSpatialPlacementOperation(
                target_global_id=GLOBAL_ID,
                project_world_placement=_placement(),
            ),
        )
    )
    assert len(command.operations) == 2


def test_list_query_is_bounded_and_rejects_invalid_global_ids() -> None:
    assert IfcSpatialStructureListQuery().page_size == 64
    with pytest.raises(ValidationError):
        IfcSpatialStructureListQuery(page_size=257)
    with pytest.raises(ValidationError):
        IfcSpatialStructureListQuery(global_ids=("not-a-guid",))
    assert IfcSpatialStructureCatalog(project_global_id=GLOBAL_ID).items == ()
