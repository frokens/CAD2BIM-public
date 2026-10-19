from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import ifcopenshell
import ifcopenshell.util.placement
import numpy as np
import pytest

from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    CreateParentContextOperation,
    IfcLengthUnit,
    IfcLocalCoordinateReferenceChange,
    IfcPlaneAngleUnit,
    IfcProjectUnitChange,
    IfcWorldCoordinateSystem3D,
)
from CAD2BIM_backend.domain.ifc_project import IfcProjectPatch, UpdateIfcProjectCommand
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    CreateIfcSpatialOperation,
    DeleteIfcSpatialOperation,
    IfcClientRefSpatialParentReference,
    IfcExistingSpatialParentReference,
    IfcProjectParentReference,
    IfcProjectWorldPlacement,
    IfcSpatialNotEmptyError,
    ReparentIfcSpatialOperation,
    SetIfcSpatialPlacementOperation,
    UpdateIfcSpatialOperation,
)
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.ifc.geometry_basis import (
    IfcOpenShellGeometryBasisCompiler,
)
from CAD2BIM_backend.infrastructure.ifc.placement import placement_matrix
from CAD2BIM_backend.infrastructure.ifc.project import IfcOpenShellProjectCompiler
from CAD2BIM_backend.infrastructure.ifc.spatial_structure import (
    IfcOpenShellSpatialStructureCompiler,
)


_PROJECT_GLOBAL_ID = "1xvJu1wbjMywJMjRBpLw$1"
_HEADER_TIMESTAMP = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
_SPATIAL_IFC_CLASSES = ("IfcSite", "IfcBuilding", "IfcBuildingStorey", "IfcSpace")


@dataclass(frozen=True, slots=True)
class _IndependentSpatialNode:
    global_id: str
    ifc_class: str
    parent_global_id: str
    matrix: np.ndarray


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _placement(
    origin: tuple[float, float, float],
    ref_direction: tuple[float, float, float],
) -> IfcProjectWorldPlacement:
    return IfcProjectWorldPlacement(
        origin=origin,
        axis=(0.0, 0.0, 1.0),
        ref_direction=ref_direction,
    )


def _foundation() -> bytes:
    codec = IfcOpenShellModelCodec()
    revision1 = codec.create_revision1(
        project_global_id=_PROJECT_GLOBAL_ID,
        project_name="G1 Foundation",
        header_timestamp=_HEADER_TIMESTAMP,
    ).content
    return IfcOpenShellGeometryBasisCompiler(codec).apply(
        revision1,
        ConfigureIfcGeometryBasisCommand(
            project_unit_change=IfcProjectUnitChange(
                length_unit=IfcLengthUnit(),
                plane_angle_unit=IfcPlaneAngleUnit(),
            ),
            coordinate_reference_change=IfcLocalCoordinateReferenceChange(),
            context_operations=(
                CreateParentContextOperation(
                    context_identifier="Model",
                    context_type="Model",
                    coordinate_space_dimension=3,
                    precision=1e-5,
                    world_coordinate_system=IfcWorldCoordinateSystem3D(
                        origin=(0.0, 0.0, 0.0),
                        axis=(0.0, 0.0, 1.0),
                        ref_direction=(1.0, 0.0, 0.0),
                    ),
                ),
            ),
        ),
    ).content


def _parent(reference: str) -> IfcProjectParentReference | IfcClientRefSpatialParentReference:
    if reference == "project":
        return IfcProjectParentReference()
    return IfcClientRefSpatialParentReference(client_ref=reference)


def _complex_tree_command() -> ApplyIfcSpatialStructureCommand:
    # Declaration order is deliberately unrelated to graph depth. Every
    # placement is a public project-world value, never a precomputed local one.
    specs = {
        "s0": ("site", "project", "Site S0", "COMPLEX", (100.0, 200.0, 10.0), (0.0, 1.0, 0.0)),
        "s1": ("site", "s0", "Site S1", "PARTIAL", (130.0, 205.0, 11.0), (-1.0, 0.0, 0.0)),
        "s2": ("site", "s0", "Site S2", "PARTIAL", (70.0, 190.0, 9.0), (1.0, 0.0, 0.0)),
        "b0": ("building", "s1", "Building B0", "COMPLEX", (140.0, 210.0, 12.0), (0.0, -1.0, 0.0)),
        "b1": ("building", "b0", "Building B1", "PARTIAL", (150.0, 220.0, 13.0), (1.0, 0.0, 0.0)),
        "b2": ("building", "b0", "Building B2", "PARTIAL", (120.0, 215.0, 14.0), (0.0, 1.0, 0.0)),
        "b3": ("building", "s2", "Building B3", "ELEMENT", (60.0, 180.0, 8.0), (-1.0, 0.0, 0.0)),
        "l1": ("building_storey", "b1", "Storey L1", "ELEMENT", (151.0, 221.0, 16.0), (0.0, 1.0, 0.0)),
        "l2": ("building_storey", "b1", "Storey L2", "ELEMENT", (151.0, 221.0, 20.0), (-1.0, 0.0, 0.0)),
        "l3": ("building_storey", "b2", "Storey L3", "ELEMENT", (121.0, 216.0, 18.0), (0.0, -1.0, 0.0)),
        "l4": ("building_storey", "b3", "Storey L4", "ELEMENT", (61.0, 181.0, 12.0), (1.0, 0.0, 0.0)),
        "r1": ("space", "l1", "Space R1", "ELEMENT", (152.0, 222.0, 16.0), (1.0, 0.0, 0.0)),
        "r2": ("space", "l1", "Space R2", "ELEMENT", (154.0, 222.0, 16.0), (0.0, 1.0, 0.0)),
        "r3": ("space", "l2", "Space R3", "ELEMENT", (153.0, 223.0, 20.0), (-1.0, 0.0, 0.0)),
        "r4": ("space", "l3", "Space R4", "ELEMENT", (123.0, 218.0, 18.0), (0.0, -1.0, 0.0)),
        "r5": ("space", "l4", "Space R5", "ELEMENT", (63.0, 183.0, 12.0), (1.0, 0.0, 0.0)),
        "r6": ("space", "l4", "Space R6", "ELEMENT", (65.0, 183.0, 12.0), (0.0, 1.0, 0.0)),
    }
    scrambled = (
        "r6", "l4", "b3", "s2", "r4", "l3", "b2", "r3", "l2",
        "r2", "r1", "l1", "b1", "b0", "s1", "s0", "r5",
    )
    operations: list[CreateIfcSpatialOperation] = []
    for reference in scrambled:
        spatial_class, parent, name, composition, origin, direction = specs[reference]
        optional = {"predefined_type": "INTERNAL"} if spatial_class == "space" else {}
        operations.append(
            CreateIfcSpatialOperation(
                client_ref=reference,
                spatial_class=spatial_class,
                parent=_parent(parent),
                project_world_placement=_placement(origin, direction),
                name=name,
                composition_type=composition,
                **optional,
            )
        )
    return ApplyIfcSpatialStructureCommand(operations=tuple(operations))


def _open_exact(content: bytes) -> Any:
    return ifcopenshell.file.from_string(content.decode("utf-8"))


def _independent_spatial_snapshot(content: bytes) -> dict[str, _IndependentSpatialNode]:
    model = _open_exact(content)
    nodes: dict[str, _IndependentSpatialNode] = {}
    for ifc_class in _SPATIAL_IFC_CLASSES:
        for entity in model.by_type(ifc_class, include_subtypes=False):
            relations = tuple(
                relation
                for relation in tuple(entity.Decomposes or ())
                if relation.is_a("IfcRelAggregates")
            )
            assert len(relations) == 1
            parent = relations[0].RelatingObject
            name = str(entity.Name)
            assert name not in nodes
            nodes[name] = _IndependentSpatialNode(
                global_id=str(entity.GlobalId),
                ifc_class=ifc_class,
                parent_global_id=str(parent.GlobalId),
                matrix=np.asarray(
                    ifcopenshell.util.placement.get_local_placement(entity.ObjectPlacement),
                    dtype=float,
                ),
            )
    return nodes


def _assert_world_equal(
    actual: _IndependentSpatialNode,
    expected: _IndependentSpatialNode,
) -> None:
    assert np.allclose(actual.matrix, expected.matrix, atol=1e-7, rtol=0.0)


def test_g1_a_complex_spatial_timeline_is_deterministic_and_keeps_seed_copies_immutable() -> None:
    codec = IfcOpenShellModelCodec()
    project_compiler = IfcOpenShellProjectCompiler(codec)
    spatial_compiler = IfcOpenShellSpatialStructureCompiler(codec)
    parent_seed = _foundation()
    copy_a = bytes(bytearray(parent_seed))
    sibling_copy = bytes(bytearray(parent_seed))
    parent_digest = _sha256(parent_seed)
    sibling_digest = _sha256(sibling_copy)

    project_updated = project_compiler.apply(
        copy_a,
        UpdateIfcProjectCommand(
            patch=IfcProjectPatch(
                name="G1 Spatial Scenario",
                description="Deterministic Project and spatial structure regression",
                phase="G1",
            )
        ),
    ).content
    created = spatial_compiler.apply(project_updated, _complex_tree_command())
    created_digest = _sha256(created.content)
    first_snapshot = _independent_spatial_snapshot(created.content)
    assert len(first_snapshot) == 17
    assert first_snapshot["Site S0"].parent_global_id == _PROJECT_GLOBAL_ID
    assert first_snapshot["Site S1"].parent_global_id == first_snapshot["Site S0"].global_id
    assert first_snapshot["Building B1"].parent_global_id == first_snapshot["Building B0"].global_id
    assert first_snapshot["Space R2"].parent_global_id == first_snapshot["Storey L1"].global_id

    evolved = spatial_compiler.apply(
        created.content,
        ApplyIfcSpatialStructureCommand(
            operations=(
                UpdateIfcSpatialOperation.model_validate(
                    {
                        "target_global_id": first_snapshot["Space R1"].global_id,
                        "patch": {"name": "Space R1 Updated", "long_name": "Control room"},
                    }
                ),
                ReparentIfcSpatialOperation(
                    target_global_id=first_snapshot["Space R2"].global_id,
                    new_parent=IfcExistingSpatialParentReference(
                        global_id=first_snapshot["Storey L3"].global_id
                    ),
                ),
                SetIfcSpatialPlacementOperation(
                    target_global_id=first_snapshot["Space R3"].global_id,
                    project_world_placement=_placement(
                        (160.0, 230.0, 21.0),
                        (0.0, -1.0, 0.0),
                    ),
                ),
            )
        ),
    )
    evolved_snapshot = _independent_spatial_snapshot(evolved.content)
    _assert_world_equal(evolved_snapshot["Space R2"], first_snapshot["Space R2"])
    assert evolved_snapshot["Space R2"].parent_global_id == first_snapshot["Storey L3"].global_id
    assert evolved_snapshot["Space R3"].matrix[:3, 3] == pytest.approx((160.0, 230.0, 21.0))

    before_failed_delete = _sha256(evolved.content)
    with pytest.raises(IfcSpatialNotEmptyError) as blocked:
        spatial_compiler.apply(
            evolved.content,
            ApplyIfcSpatialStructureCommand(
                operations=(
                    DeleteIfcSpatialOperation(
                        target_global_id=evolved_snapshot["Building B1"].global_id
                    ),
                )
            ),
        )
    assert blocked.value.blockers.child_spatial_count == 2
    assert _sha256(evolved.content) == before_failed_delete

    migrated = spatial_compiler.apply(
        evolved.content,
        ApplyIfcSpatialStructureCommand(
            operations=(
                DeleteIfcSpatialOperation(
                    target_global_id=evolved_snapshot["Building B1"].global_id
                ),
                ReparentIfcSpatialOperation(
                    target_global_id=evolved_snapshot["Storey L2"].global_id,
                    new_parent=IfcExistingSpatialParentReference(
                        global_id=evolved_snapshot["Building B2"].global_id
                    ),
                ),
                ReparentIfcSpatialOperation(
                    target_global_id=evolved_snapshot["Storey L1"].global_id,
                    new_parent=IfcExistingSpatialParentReference(
                        global_id=evolved_snapshot["Building B2"].global_id
                    ),
                ),
            )
        ),
    )
    migrated_snapshot = _independent_spatial_snapshot(migrated.content)
    assert "Building B1" not in migrated_snapshot
    for name in ("Storey L1", "Storey L2"):
        _assert_world_equal(migrated_snapshot[name], evolved_snapshot[name])
        assert migrated_snapshot[name].parent_global_id == migrated_snapshot["Building B2"].global_id

    before_rigid = migrated_snapshot
    rigid_target = _placement((300.0, -100.0, 20.0), (-1.0, 0.0, 0.0))
    rigid = spatial_compiler.apply(
        migrated.content,
        ApplyIfcSpatialStructureCommand(
            operations=(
                SetIfcSpatialPlacementOperation(
                    target_global_id=before_rigid["Site S0"].global_id,
                    project_world_placement=rigid_target,
                ),
            )
        ),
    )
    after_rigid = _independent_spatial_snapshot(rigid.content)
    delta = placement_matrix(rigid_target) @ np.linalg.inv(before_rigid["Site S0"].matrix)
    for name, before_node in before_rigid.items():
        assert np.allclose(after_rigid[name].matrix, delta @ before_node.matrix, atol=1e-7, rtol=0.0)

    preserved = spatial_compiler.apply(
        rigid.content,
        ApplyIfcSpatialStructureCommand(
            operations=(
                SetIfcSpatialPlacementOperation(
                    target_global_id=after_rigid["Site S0"].global_id,
                    project_world_placement=_placement((10.0, 20.0, 5.0), (0.0, -1.0, 0.0)),
                    preserve_descendant_project_world=True,
                ),
            )
        ),
    )
    after_preserve = _independent_spatial_snapshot(preserved.content)
    assert not np.allclose(after_preserve["Site S0"].matrix, after_rigid["Site S0"].matrix)
    for name in after_rigid.keys() - {"Site S0"}:
        _assert_world_equal(after_preserve[name], after_rigid[name])

    delete_operations = tuple(
        DeleteIfcSpatialOperation(target_global_id=node.global_id)
        for name, node in after_preserve.items()
        if node.ifc_class != "IfcSite"
    )
    cleaned = spatial_compiler.apply(
        preserved.content,
        ApplyIfcSpatialStructureCommand(operations=delete_operations),
    )
    final_snapshot = _independent_spatial_snapshot(cleaned.content)
    assert set(final_snapshot) == {"Site S0", "Site S1", "Site S2"}
    assert final_snapshot["Site S1"].parent_global_id == final_snapshot["Site S0"].global_id
    assert final_snapshot["Site S2"].parent_global_id == final_snapshot["Site S0"].global_id
    assert project_compiler.inspect_project(cleaned.content).name == "G1 Spatial Scenario"

    # Every compiler result is a new immutable byte value. Later writes cannot
    # rewrite the parent foundation, its sibling copy, or an earlier revision.
    assert _sha256(parent_seed) == parent_digest
    assert _sha256(sibling_copy) == sibling_digest == parent_digest
    assert not _independent_spatial_snapshot(sibling_copy)
    assert _sha256(created.content) == created_digest
    assert len(_independent_spatial_snapshot(created.content)) == 17
