"""Strict IfcOpenShell compiler for the IFC project spatial structure."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import ifcopenshell.api.aggregate
import ifcopenshell.api.root
import ifcopenshell.guid
import numpy as np

from CAD2BIM_backend.domain.ifc_model import EncodedIfcRevision
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    CreateIfcSpatialOperation,
    DeleteIfcSpatialOperation,
    IfcClientRefSpatialParentReference,
    IfcCompositionType,
    IfcExistingSpatialParentReference,
    IfcProjectParentReference,
    IfcSpatialAttributes,
    IfcSpatialBlockerSummary,
    IfcSpatialClass,
    IfcSpatialClassMismatchError,
    IfcSpatialCycleError,
    IfcSpatialNoChangeError,
    IfcSpatialNodeSummary,
    IfcSpatialNotEmptyError,
    IfcSpatialOperationConflictError,
    IfcSpatialParentInvalidError,
    IfcSpatialReferenceInvalidError,
    IfcSpatialStructureBusinessError,
    IfcSpatialStructureCatalog,
    IfcSpatialStructureCompilation,
    IfcSpatialStructureCompilationError,
    IfcSpatialCreatedBinding,
    IfcSpatialStructureIntegrityError,
    IfcSpatialStructureListQuery,
    IfcSpatialStructurePage,
    IfcSpatialTargetNotFoundError,
    MAX_IFC_SPATIAL_BLOCKER_SAMPLES,
    MAX_IFC_SPATIAL_NODES,
    ReparentIfcSpatialOperation,
    SetIfcSpatialPlacementOperation,
    UpdateIfcSpatialOperation,
    ifc_spatial_parent_child_violation,
    project_spatial_catalog,
)
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.ifc.placement import (
    capture_world_matrices,
    cleanup_isolated_placement_anchors,
    detach_shared_object_placement,
    matrices_close,
    move_semantic_subtree,
    placement_matrix,
    project_world_placement,
    semantic_descendants,
    isolate_shared_closure_placements,
    set_project_world_matrix,
    validate_semantic_placement_closure,
)


_SPATIAL_TO_IFC: dict[IfcSpatialClass, str] = {
    "site": "IfcSite",
    "building": "IfcBuilding",
    "building_storey": "IfcBuildingStorey",
    "space": "IfcSpace",
}
_IFC_TO_SPATIAL = {value: key for key, value in _SPATIAL_TO_IFC.items()}
_PROJECT = "__project__"
_GLOBAL_ID_CHARS = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$" )
_PRIVATE_DELETE_RELATIONSHIPS = frozenset({"IfcRelDefinesByProperties"})
_CLASSIFIED_BUSINESS_RELATIONSHIPS = frozenset(
    {
        "IfcRelContainedInSpatialStructure",
        "IfcRelReferencedInSpatialStructure",
        "IfcRelServicesBuildings",
        "IfcRelSpaceBoundary",
        "IfcRelCoversSpaces",
        "IfcRelInterferesElements",
        "IfcRelAssignsToGroup",
    }
)


@dataclass(frozen=True, slots=True)
class _NodeState:
    global_id: str
    spatial_class: IfcSpatialClass
    composition_type: IfcCompositionType
    parent_global_id: str
    entity: Any | None
    client_ref: str | None = None


@dataclass(frozen=True, slots=True)
class _Preflight:
    project: Any
    states: dict[str, _NodeState]
    create_global_ids: dict[str, str]
    delete_ids: frozenset[str]
    placement_impact: dict[str, tuple[Any, ...]]


@dataclass(frozen=True, slots=True)
class _BlockerClassification:
    child_spatial_count: int
    children: set[Any]
    contained: set[Any]
    referenced: set[Any]
    systems: set[Any]
    boundaries: set[Any]
    coverings: set[Any]
    interferences: set[Any]
    groups: set[Any]
    external_placements: set[Any]
    other_business_references: set[Any]


class IfcOpenShellSpatialStructureCompiler:
    """Apply one fully preflighted spatial batch to a private IFC copy."""

    def __init__(self, codec: IfcOpenShellModelCodec | None = None) -> None:
        self._codec = codec or IfcOpenShellModelCodec()

    def apply(
        self,
        base_content: bytes,
        command: ApplyIfcSpatialStructureCommand,
    ) -> IfcSpatialStructureCompilation:
        if not isinstance(command, ApplyIfcSpatialStructureCommand):
            raise TypeError("command must be ApplyIfcSpatialStructureCommand")
        private = self._codec.private_copy(base_content)
        model = private.model
        try:
            # Inspection and all business validation precede the first mutation.
            before_catalog = self.inspect_spatial_structure(base_content)
            preflight = _preflight(model, base_content, command)
            _apply_batch(model, command, preflight)
            _canonicalize_aggregate_members(model)
            content = private.to_bytes()
            if content == base_content:
                raise IfcSpatialNoChangeError(
                    "spatial structure command does not change the exact IFC revision"
                )

            # Reopen exact serialized bytes.  The generic codec verifies schema,
            # unique Project, and global durable invariants; the strict spatial
            # inspector verifies relations and project-world placements.
            inspection = self._codec.inspect(content)
            saved = self.inspect_spatial_structure(content)
            _verify_command_postconditions(base_content, content, command, preflight, saved)
            before_by_id = {item.global_id: item for item in before_catalog.items}
            affected_global_ids = tuple(
                sorted(
                    item.global_id
                    for item in saved.items
                    if before_by_id.get(item.global_id) != item
                )
            )
            return IfcSpatialStructureCompilation(
                candidate=EncodedIfcRevision(content=content, inspection=inspection),
                created=tuple(
                    IfcSpatialCreatedBinding(client_ref=client_ref, global_id=global_id)
                    for client_ref, global_id in sorted(preflight.create_global_ids.items())
                ),
                affected_global_ids=affected_global_ids,
            )
        except (IfcSpatialStructureBusinessError, IfcSpatialStructureIntegrityError):
            raise
        except Exception as exc:
            raise IfcSpatialStructureCompilationError(
                "typed spatial structure could not be compiled"
            ) from exc

    def inspect_spatial_structure(self, content: bytes) -> IfcSpatialStructureCatalog:
        return self.inspect_spatial_structure_from_model(self._codec.private_copy(content).model)

    def inspect_spatial_structure_from_model(self, model: Any) -> IfcSpatialStructureCatalog:
        try:
            project = _require_project(model)
            states = _inspect_states(model, project)
            items = tuple(
                _summarize_node(model, project, state)
                for state in sorted(states.values(), key=lambda item: (_SPATIAL_TO_IFC[item.spatial_class], item.global_id))
            )
            return IfcSpatialStructureCatalog(
                project_global_id=_require_global_id(project, "IfcProject"),
                items=items,
            )
        except IfcSpatialStructureIntegrityError:
            raise
        except Exception as exc:
            raise IfcSpatialStructureIntegrityError(
                "IFC spatial structure could not be inspected"
            ) from exc

    def list_spatial_structure(
        self,
        content: bytes,
        query: IfcSpatialStructureListQuery,
    ) -> IfcSpatialStructurePage:
        if not isinstance(query, IfcSpatialStructureListQuery):
            raise TypeError("query must be IfcSpatialStructureListQuery")
        return project_spatial_catalog(self.inspect_spatial_structure(content), query)


def _preflight(
    model: Any,
    base_content: bytes,
    command: ApplyIfcSpatialStructureCommand,
) -> _Preflight:
    project = _require_project(model)
    states = _inspect_states(model, project)
    create_operations = tuple(
        operation for operation in command.operations if isinstance(operation, CreateIfcSpatialOperation)
    )
    create_global_ids = {
        operation.client_ref: _stable_global_id(
            f"spatial:{hashlib.sha256(base_content).hexdigest()}:{operation.client_ref}"
        )
        for operation in create_operations
    }
    if set(create_global_ids.values()).intersection(states):
        raise IfcSpatialOperationConflictError("deterministic create GlobalId already exists")

    for operation in create_operations:
        parent_id = _resolve_parent_reference(
            operation.parent,
            project=project,
            states=states,
            create_global_ids=create_global_ids,
        )
        states[create_global_ids[operation.client_ref]] = _NodeState(
            global_id=create_global_ids[operation.client_ref],
            spatial_class=operation.spatial_class,
            composition_type=operation.composition_type,
            parent_global_id=parent_id,
            entity=None,
            client_ref=operation.client_ref,
        )
        _validate_class_specific_attributes(operation.spatial_class, operation.attributes())

    delete_ids: set[str] = set()
    placement_impact: dict[str, tuple[Any, ...]] = {}
    for operation in command.operations:
        target_id = getattr(operation, "target_global_id", None)
        if target_id is None:
            continue
        state = states.get(target_id)
        if state is None or state.entity is None:
            existing = _by_guid(model, target_id)
            if existing is not None and not _is_supported_spatial(existing):
                raise IfcSpatialClassMismatchError(
                    f"target {target_id!r} is {existing.is_a()}, not a supported spatial class"
                )
            raise IfcSpatialTargetNotFoundError(f"spatial target {target_id!r} was not found")

        if isinstance(operation, UpdateIfcSpatialOperation):
            attributes = _attributes_after_update(state.entity, operation)
            _validate_class_specific_attributes(state.spatial_class, attributes)
            states[target_id] = replace(
                state,
                composition_type=attributes.composition_type,
            )
        elif isinstance(operation, ReparentIfcSpatialOperation):
            if target_id not in placement_impact:
                placement_impact[target_id] = validate_semantic_placement_closure(
                    state.entity
                )
            parent_id = _resolve_parent_reference(
                operation.new_parent,
                project=project,
                states=states,
                create_global_ids=create_global_ids,
            )
            states[target_id] = replace(state, parent_global_id=parent_id)
        elif isinstance(operation, SetIfcSpatialPlacementOperation):
            # The DTO already proves a rigid project-world frame.  Capture now
            # to fail before mutation on unsupported semantic descendants.
            if target_id not in placement_impact:
                placement_impact[target_id] = validate_semantic_placement_closure(
                    state.entity
                )
            capture_world_matrices(placement_impact[target_id])
        elif isinstance(operation, DeleteIfcSpatialOperation):
            delete_ids.add(target_id)

    final_states = {key: value for key, value in states.items() if key not in delete_ids}
    # A still-nonempty delete target intentionally makes its children appear
    # parentless in final_states.  Diagnose that business condition as the
    # bounded IFC_SPATIAL_NOT_EMPTY blocker before general graph integrity;
    # once children are reparented/deleted in the same batch this count is zero
    # and the resulting candidate graph is validated normally below.
    for target_id in delete_ids:
        state = states[target_id]
        blockers = _delete_blockers(
            model,
            state.entity,
            final_states=final_states,
            delete_ids=frozenset(delete_ids),
        )
        if blockers.total_count:
            raise IfcSpatialNotEmptyError(target_id, blockers)

    _validate_candidate_graph(project, final_states)
    _preflight_relationship_global_ids(model, project, final_states)

    return _Preflight(
        project=project,
        states=states,
        create_global_ids=create_global_ids,
        delete_ids=frozenset(delete_ids),
        placement_impact=placement_impact,
    )


def _apply_batch(model: Any, command: ApplyIfcSpatialStructureCommand, preflight: _Preflight) -> None:
    entities: dict[str, Any] = {
        state.global_id: state.entity for state in preflight.states.values() if state.entity is not None
    }
    creates = sorted(
        (operation for operation in command.operations if isinstance(operation, CreateIfcSpatialOperation)),
        key=lambda operation: preflight.create_global_ids[operation.client_ref],
    )
    for operation in creates:
        global_id = preflight.create_global_ids[operation.client_ref]
        entity = model.create_entity(
            _SPATIAL_TO_IFC[operation.spatial_class],
            GlobalId=global_id,
            Name=operation.name,
            Description=operation.description,
            ObjectType=operation.object_type,
            LongName=operation.long_name,
            CompositionType=operation.composition_type,
        )
        _write_class_specific_attributes(entity, operation.attributes())
        entities[global_id] = entity

    # Build create relationships in graph depth order; forward client_ref
    # references are already resolved by preflight.
    final_states = {
        key: value for key, value in preflight.states.items() if key not in preflight.delete_ids
    }
    for operation in sorted(creates, key=lambda item: (_depth(preflight.create_global_ids[item.client_ref], final_states), preflight.create_global_ids[item.client_ref])):
        global_id = preflight.create_global_ids[operation.client_ref]
        parent_id = final_states[global_id].parent_global_id
        parent = preflight.project if parent_id == _PROJECT else entities[parent_id]
        _assign_aggregate(model, entities[global_id], parent)

    for operation in sorted(
        (item for item in command.operations if isinstance(item, UpdateIfcSpatialOperation)),
        key=lambda item: item.target_global_id,
    ):
        _apply_update(entities[operation.target_global_id], operation)

    reparent_before: dict[str, dict[int, np.ndarray]] = {}
    for operation in sorted(
        (item for item in command.operations if isinstance(item, ReparentIfcSpatialOperation)),
        key=lambda item: item.target_global_id,
    ):
        target = entities[operation.target_global_id]
        affected = preflight.placement_impact[operation.target_global_id]
        before = capture_world_matrices(affected)
        reparent_before[operation.target_global_id] = before
        isolated_anchors = isolate_shared_closure_placements(model, affected)
        parent_id = final_states[operation.target_global_id].parent_global_id
        parent = preflight.project if parent_id == _PROJECT else entities[parent_id]
        detach_shared_object_placement(model, target)
        _assign_aggregate_without_placement_rewrite(model, target, parent)
        set_project_world_matrix(
            model,
            target,
            before[target.id()],
            should_transform_children=False,
        )
        for descendant in affected[1:]:
            set_project_world_matrix(
                model,
                descendant,
                before[descendant.id()],
                should_transform_children=False,
            )
        cleanup_isolated_placement_anchors(model, isolated_anchors)

    # New objects are placed only after all parent relations exist.
    for operation in sorted(creates, key=lambda item: (_depth(preflight.create_global_ids[item.client_ref], final_states), preflight.create_global_ids[item.client_ref])):
        set_project_world_matrix(
            model,
            entities[preflight.create_global_ids[operation.client_ref]],
            placement_matrix(operation.project_world_placement),
        )

    for operation in sorted(
        (item for item in command.operations if isinstance(item, SetIfcSpatialPlacementOperation)),
        key=lambda item: (_depth(item.target_global_id, final_states), item.target_global_id),
    ):
        move_semantic_subtree(
            model,
            entities[operation.target_global_id],
            operation.project_world_placement,
            preserve_descendant_project_world=operation.preserve_descendant_project_world,
            prevalidated_affected=preflight.placement_impact[operation.target_global_id],
        )

    for operation in sorted(
        (item for item in command.operations if isinstance(item, DeleteIfcSpatialOperation)),
        key=lambda item: (-_depth(item.target_global_id, preflight.states), item.target_global_id),
    ):
        entity = entities[operation.target_global_id]
        ifcopenshell.api.aggregate.unassign_object(model, products=[entity])
        ifcopenshell.api.root.remove_product(model, product=entity)
        entities.pop(operation.target_global_id, None)


def _inspect_states(model: Any, project: Any) -> dict[str, _NodeState]:
    _inspect_root_global_ids(model)
    spatial_entities: list[Any] = []
    for ifc_class in _IFC_TO_SPATIAL:
        spatial_entities.extend(model.by_type(ifc_class, include_subtypes=False))
    if len(spatial_entities) > MAX_IFC_SPATIAL_NODES:
        raise IfcSpatialStructureIntegrityError("IFC spatial structure exceeds the bounded limit")

    states: dict[str, _NodeState] = {}
    parent_relations: dict[int, list[Any]] = defaultdict(list)
    spatial_relations_by_parent: dict[int, list[Any]] = defaultdict(list)
    for relation in tuple(model.by_type("IfcRelAggregates")):
        _require_global_id(relation, "IfcRelAggregates")
        related = tuple(relation.RelatedObjects or ())
        if not related or relation.RelatingObject in related:
            raise IfcSpatialStructureIntegrityError("IfcRelAggregates has invalid membership")
        if len(set(related)) != len(related):
            raise IfcSpatialStructureIntegrityError("IfcRelAggregates repeats a related object")
        if any(_is_supported_spatial(child) for child in related):
            spatial_relations_by_parent[relation.RelatingObject.id()].append(relation)
        for child in related:
            if _is_supported_spatial(child):
                parent_relations[child.id()].append(relation)

    if any(len(relations) > 1 for relations in spatial_relations_by_parent.values()):
        raise IfcSpatialStructureIntegrityError(
            "a spatial aggregate parent must use one canonical IfcRelAggregates"
        )

    for entity in spatial_entities:
        global_id = _require_global_id(entity, entity.is_a())
        if global_id in states:
            raise IfcSpatialStructureIntegrityError("spatial GlobalId values must be unique")
        relations = parent_relations.get(entity.id(), [])
        if len(relations) != 1:
            raise IfcSpatialStructureIntegrityError(
                f"spatial object {global_id!r} must have exactly one aggregate parent"
            )
        parent = relations[0].RelatingObject
        if parent == project:
            parent_id = _PROJECT
        elif _is_supported_spatial(parent):
            parent_id = _require_global_id(parent, parent.is_a())
        else:
            raise IfcSpatialStructureIntegrityError(
                f"spatial object {global_id!r} has an unsupported aggregate parent"
            )
        composition = str(entity.CompositionType or "ELEMENT")
        if composition not in {"COMPLEX", "ELEMENT", "PARTIAL"}:
            raise IfcSpatialStructureIntegrityError("invalid spatial CompositionType")
        _validate_entity_class_specific(entity, integrity=True)
        states[global_id] = _NodeState(
            global_id=global_id,
            spatial_class=_IFC_TO_SPATIAL[str(entity.is_a())],
            composition_type=composition,  # type: ignore[arg-type]
            parent_global_id=parent_id,
            entity=entity,
        )

    _validate_candidate_graph(project, states, integrity=True)
    for state in states.values():
        entity = state.entity
        placement = getattr(entity, "ObjectPlacement", None)
        if placement is None or not placement.is_a("IfcLocalPlacement"):
            raise IfcSpatialStructureIntegrityError("every spatial object requires IfcLocalPlacement")
        # Resolve first so a placement cycle is diagnosed as such rather than
        # being obscured by the consequent parent-placement mismatch.
        project_world_placement(entity)
        expected_parent_placement = (
            None if state.parent_global_id == _PROJECT else states[state.parent_global_id].entity.ObjectPlacement
        )
        if placement.PlacementRelTo != expected_parent_placement:
            raise IfcSpatialStructureIntegrityError(
                "spatial IfcLocalPlacement must be relative to its aggregate parent"
            )
    return states


def _validate_candidate_graph(
    project: Any,
    states: dict[str, _NodeState],
    *,
    integrity: bool = False,
) -> None:
    error_parent = IfcSpatialStructureIntegrityError if integrity else IfcSpatialParentInvalidError
    error_cycle = IfcSpatialStructureIntegrityError if integrity else IfcSpatialCycleError
    _require_global_id(project, "IfcProject")
    # Resolve all parent identities first, then diagnose a transitive cycle
    # before the more local class/composition matrix.  A cycle is the stronger
    # graph failure even when one edge would also be a class mismatch.
    for state in states.values():
        if state.parent_global_id == _PROJECT:
            continue
        parent = states.get(state.parent_global_id)
        if parent is None:
            raise error_parent(
                f"spatial parent {state.parent_global_id!r} does not exist in the final graph"
            )

    colors: dict[str, int] = {}
    for start in states:
        current = start
        trail: list[str] = []
        while current != _PROJECT and colors.get(current, 0) != 2:
            if colors.get(current) == 1:
                raise error_cycle("final spatial aggregate graph contains a cycle")
            colors[current] = 1
            trail.append(current)
            current = states[current].parent_global_id
        for item in trail:
            colors[item] = 2

    for state in states.values():
        if state.parent_global_id == _PROJECT:
            violation = ifc_spatial_parent_child_violation(
                "project", None, state.spatial_class, state.composition_type
            )
        else:
            parent = states[state.parent_global_id]
            violation = ifc_spatial_parent_child_violation(
                parent.spatial_class,
                parent.composition_type,
                state.spatial_class,
                state.composition_type,
            )
        if violation is not None:
            raise error_parent(violation)


def _summarize_node(model: Any, project: Any, state: _NodeState) -> IfcSpatialNodeSummary:
    entity = state.entity
    assert entity is not None
    parent = project if state.parent_global_id == _PROJECT else _by_guid(model, state.parent_global_id)
    blockers = _relationship_counts(model, entity)
    return IfcSpatialNodeSummary(
        global_id=state.global_id,
        ifc_class=str(entity.is_a()),
        spatial_class=state.spatial_class,
        name=_required_name(entity),
        description=_optional_text(entity.Description),
        object_type=_optional_text(entity.ObjectType),
        long_name=_optional_text(entity.LongName),
        composition_type=state.composition_type,
        predefined_type=(str(entity.PredefinedType) if entity.is_a("IfcSpace") and entity.PredefinedType is not None else None),
        ref_latitude=(_compound_angle_to_decimal(entity.RefLatitude) if entity.is_a("IfcSite") and entity.RefLatitude is not None else None),
        ref_longitude=(_compound_angle_to_decimal(entity.RefLongitude) if entity.is_a("IfcSite") and entity.RefLongitude is not None else None),
        ref_elevation=(float(entity.RefElevation) if entity.is_a("IfcSite") and entity.RefElevation is not None else None),
        elevation_with_flooring=(float(entity.ElevationWithFlooring) if entity.is_a("IfcSpace") and entity.ElevationWithFlooring is not None else None),
        parent_global_id=_require_global_id(parent, parent.is_a()),
        parent_ifc_class=str(parent.is_a()),
        project_world_placement=project_world_placement(entity),
        blockers=blockers,
    )


def _relationship_counts(model: Any, entity: Any) -> IfcSpatialBlockerSummary:
    return _blocker_summary(_classify_business_blockers(model, entity))


def _classify_business_blockers(
    model: Any,
    entity: Any,
    *,
    child_spatial_count: int | None = None,
    child_samples: set[Any] | None = None,
    allowed_placement_ids: set[int] | None = None,
) -> _BlockerClassification:
    children = {
        child
        for relationship in tuple(getattr(entity, "IsDecomposedBy", ()) or ())
        if relationship.is_a("IfcRelAggregates")
        for child in tuple(relationship.RelatedObjects or ())
        if _is_supported_spatial(child)
    }
    contained = {
        child
        for relationship in tuple(getattr(entity, "ContainsElements", ()) or ())
        for child in tuple(relationship.RelatedElements or ())
    }
    referenced = {
        child
        for relationship in tuple(getattr(entity, "ReferencesElements", ()) or ())
        for child in tuple(relationship.RelatedElements or ())
    }
    systems = {
        relationship.RelatingSystem
        for relationship in tuple(getattr(entity, "ServicedBySystems", ()) or ())
    }
    boundaries = set(tuple(getattr(entity, "BoundedBy", ()) or ()))
    coverings = set(tuple(getattr(entity, "HasCoverings", ()) or ()))
    interferences = set(tuple(getattr(entity, "InterferesElements", ()) or ())) | set(
        tuple(getattr(entity, "IsInterferedByElements", ()) or ())
    )
    groups = {
        relation.RelatingGroup
        for relation in tuple(getattr(entity, "HasAssignments", ()) or ())
        if relation.is_a("IfcRelAssignsToGroup")
    }
    if allowed_placement_ids is None:
        semantic = (entity, *semantic_descendants(entity))
        allowed_placement_ids = {product.id() for product in semantic}
    external_placements = _external_placement_users(entity, allowed_ids=allowed_placement_ids)

    other: set[Any] = set()
    for inverse in model.get_inverse(entity):
        if not inverse.is_a("IfcRelationship") or inverse.is_a("IfcRelAggregates"):
            continue
        name = str(inverse.is_a())
        if name in _PRIVATE_DELETE_RELATIONSHIPS or any(
            inverse.is_a(ifc_class) for ifc_class in _CLASSIFIED_BUSINESS_RELATIONSHIPS
        ):
            continue
        other.add(inverse)

    return _BlockerClassification(
        child_spatial_count=(len(children) if child_spatial_count is None else child_spatial_count),
        children=(children if child_samples is None else child_samples),
        contained=contained,
        referenced=referenced,
        systems=systems,
        boundaries=boundaries,
        coverings=coverings,
        interferences=interferences,
        groups=groups,
        external_placements=external_placements,
        other_business_references=other,
    )


def _blocker_summary(classification: _BlockerClassification) -> IfcSpatialBlockerSummary:
    samples = _sample_global_ids(
        (
            *classification.children,
            *classification.contained,
            *classification.referenced,
            *classification.systems,
            *classification.boundaries,
            *classification.coverings,
            *classification.interferences,
            *classification.groups,
            *classification.external_placements,
            *classification.other_business_references,
        )
    )
    return IfcSpatialBlockerSummary(
        child_spatial_count=classification.child_spatial_count,
        contained_element_count=len(classification.contained),
        referenced_element_count=len(classification.referenced),
        servicing_system_count=len(classification.systems),
        boundary_count=len(classification.boundaries),
        covering_count=len(classification.coverings),
        interference_count=len(classification.interferences),
        group_count=len(classification.groups),
        external_placement_count=len(classification.external_placements),
        other_business_reference_count=len(classification.other_business_references),
        sample_global_ids=samples,
    )


def _delete_blockers(
    model: Any,
    entity: Any,
    *,
    final_states: dict[str, _NodeState],
    delete_ids: frozenset[str],
) -> IfcSpatialBlockerSummary:
    child_count = sum(1 for state in final_states.values() if state.parent_global_id == entity.GlobalId)
    child_samples = {
        state.entity
        for state in final_states.values()
        if state.parent_global_id == entity.GlobalId and state.entity is not None
    }
    allowed_placement_ids = {entity.id()}
    allowed_placement_ids.update(product.id() for product in semantic_descendants(entity))
    # Supported spatial children are represented by child_spatial_count in the
    # final candidate graph.  A child reparented earlier in the same batch no
    # longer depends on this placement when deletion actually executes.
    allowed_placement_ids.update(
        state.entity.id()
        for state in final_states.values()
        if state.entity is not None
    )
    for global_id in delete_ids:
        candidate = _by_guid(model, global_id)
        if candidate is not None:
            allowed_placement_ids.add(candidate.id())
    return _blocker_summary(
        _classify_business_blockers(
            model,
            entity,
            child_spatial_count=child_count,
            child_samples=child_samples,
            allowed_placement_ids=allowed_placement_ids,
        )
    )


def _external_placement_users(entity: Any, *, allowed_ids: set[int]) -> set[Any]:
    placement = getattr(entity, "ObjectPlacement", None)
    if placement is None:
        return set()
    users: set[Any] = set()
    for referenced in tuple(getattr(placement, "ReferencedByPlacements", ()) or ()):
        for product in tuple(getattr(referenced, "PlacesObject", ()) or ()):
            if product.id() not in allowed_ids:
                users.add(product)
    return users


def _apply_update(entity: Any, operation: UpdateIfcSpatialOperation) -> None:
    attributes = _attributes_after_update(entity, operation)
    direct = {
        "name": "Name", "description": "Description", "object_type": "ObjectType",
        "long_name": "LongName", "composition_type": "CompositionType",
    }
    for field_name in operation.patch.model_fields_set:
        if field_name in direct:
            setattr(entity, direct[field_name], getattr(attributes, field_name))
    for field_name in operation.clear_fields:
        if field_name in direct:
            setattr(entity, direct[field_name], None)
    _write_class_specific_attributes(entity, attributes)


def _attributes_after_update(entity: Any, operation: UpdateIfcSpatialOperation) -> IfcSpatialAttributes:
    current = _attributes_from_entity(entity)
    values = current.model_dump()
    for field_name in operation.patch.model_fields_set:
        values[field_name] = getattr(operation.patch, field_name)
    for field_name in operation.clear_fields:
        values[field_name] = None
    return IfcSpatialAttributes(**values)


def _attributes_from_entity(entity: Any) -> IfcSpatialAttributes:
    return IfcSpatialAttributes(
        name=_required_name(entity),
        description=_optional_text(entity.Description),
        object_type=_optional_text(entity.ObjectType),
        long_name=_optional_text(entity.LongName),
        composition_type=str(entity.CompositionType or "ELEMENT"),
        ref_latitude=(_compound_angle_to_decimal(entity.RefLatitude) if entity.is_a("IfcSite") and entity.RefLatitude is not None else None),
        ref_longitude=(_compound_angle_to_decimal(entity.RefLongitude) if entity.is_a("IfcSite") and entity.RefLongitude is not None else None),
        ref_elevation=(float(entity.RefElevation) if entity.is_a("IfcSite") and entity.RefElevation is not None else None),
        predefined_type=(str(entity.PredefinedType) if entity.is_a("IfcSpace") and entity.PredefinedType is not None else None),
        elevation_with_flooring=(float(entity.ElevationWithFlooring) if entity.is_a("IfcSpace") and entity.ElevationWithFlooring is not None else None),
    )


def _validate_class_specific_attributes(spatial_class: IfcSpatialClass, attributes: IfcSpatialAttributes) -> None:
    site_fields = (attributes.ref_latitude, attributes.ref_longitude, attributes.ref_elevation)
    space_fields = (attributes.predefined_type, attributes.elevation_with_flooring)
    if spatial_class != "site" and any(value is not None for value in site_fields):
        raise IfcSpatialClassMismatchError("WGS84 fields are valid only for IfcSite")
    if spatial_class != "space" and any(value is not None for value in space_fields):
        raise IfcSpatialClassMismatchError("space-specific fields are valid only for IfcSpace")
    if spatial_class == "space":
        if attributes.predefined_type == "USERDEFINED" and not attributes.object_type:
            raise IfcSpatialClassMismatchError(
                "IfcSpace USERDEFINED PredefinedType requires ObjectType"
            )
        if attributes.predefined_type != "USERDEFINED" and attributes.predefined_type is not None and attributes.object_type is not None:
            # ObjectType remains a valid functional category for normal spaces;
            # it is therefore deliberately not rejected.
            pass


def _validate_entity_class_specific(entity: Any, *, integrity: bool) -> None:
    try:
        attributes = _attributes_from_entity(entity)
        _validate_class_specific_attributes(_IFC_TO_SPATIAL[str(entity.is_a())], attributes)
    except Exception as exc:
        if integrity:
            raise IfcSpatialStructureIntegrityError(str(exc)) from exc
        raise


def _write_class_specific_attributes(entity: Any, attributes: IfcSpatialAttributes) -> None:
    if entity.is_a("IfcSite"):
        entity.RefLatitude = _decimal_to_compound_angle(attributes.ref_latitude)
        entity.RefLongitude = _decimal_to_compound_angle(attributes.ref_longitude)
        entity.RefElevation = attributes.ref_elevation
    elif entity.is_a("IfcSpace"):
        entity.PredefinedType = attributes.predefined_type
        entity.ElevationWithFlooring = attributes.elevation_with_flooring


def _resolve_parent_reference(
    reference: Any,
    *,
    project: Any,
    states: dict[str, _NodeState],
    create_global_ids: dict[str, str],
) -> str:
    if isinstance(reference, IfcProjectParentReference):
        return _PROJECT
    if isinstance(reference, IfcExistingSpatialParentReference):
        if reference.global_id not in states:
            raise IfcSpatialReferenceInvalidError(
                f"existing spatial parent {reference.global_id!r} was not found"
            )
        return reference.global_id
    if isinstance(reference, IfcClientRefSpatialParentReference):
        try:
            return create_global_ids[reference.client_ref]
        except KeyError as exc:
            raise IfcSpatialReferenceInvalidError(
                f"client_ref parent {reference.client_ref!r} is not declared in this command"
            ) from exc
    raise IfcSpatialReferenceInvalidError("unsupported spatial parent reference")


def _assign_aggregate(model: Any, product: Any, parent: Any) -> None:
    existing = next(
        (rel for rel in tuple(getattr(parent, "IsDecomposedBy", ()) or ()) if rel.is_a("IfcRelAggregates")),
        None,
    )
    relationship = ifcopenshell.api.aggregate.assign_object(
        model,
        products=[product],
        relating_object=parent,
    )
    if relationship is None:
        relationship = existing
    if relationship is None:
        raise IfcSpatialStructureCompilationError("IfcRelAggregates was not created")
    if existing is None:
        desired = _stable_global_id(f"aggregate:{_require_global_id(parent, parent.is_a())}")
        collision = _by_guid(model, desired)
        if collision is not None and collision != relationship:
            raise IfcSpatialOperationConflictError(
                "deterministic IfcRelAggregates GlobalId collides with an existing IfcRoot"
            )
        relationship.GlobalId = desired


def _assign_aggregate_without_placement_rewrite(
    model: Any, product: Any, parent: Any
) -> None:
    """Change only aggregation; the caller restores world placement explicitly.

    IfcOpenShell's aggregate usecase otherwise invokes
    ``edit_object_placement`` with its default child-compensation recursion.
    Temporarily detaching the already captured placement keeps this bulk path
    bounded and lets the compiler perform exactly one explicit target visit.
    """

    placement = product.ObjectPlacement
    product.ObjectPlacement = None
    try:
        _assign_aggregate(model, product, parent)
    finally:
        product.ObjectPlacement = placement


def _preflight_relationship_global_ids(
    model: Any,
    project: Any,
    states: dict[str, _NodeState],
) -> None:
    parent_ids = {state.parent_global_id for state in states.values()}
    for parent_id in parent_ids:
        parent = project if parent_id == _PROJECT else states[parent_id].entity
        existing = () if parent is None else tuple(
            rel
            for rel in tuple(getattr(parent, "IsDecomposedBy", ()) or ())
            if rel.is_a("IfcRelAggregates")
        )
        if existing:
            continue
        parent_global_id = (
            _require_global_id(project, "IfcProject")
            if parent_id == _PROJECT
            else parent_id
        )
        desired = _stable_global_id(f"aggregate:{parent_global_id}")
        if _by_guid(model, desired) is not None:
            raise IfcSpatialOperationConflictError(
                "deterministic IfcRelAggregates GlobalId collides with an existing IfcRoot"
            )


def _canonicalize_aggregate_members(model: Any) -> None:
    for relationship in tuple(model.by_type("IfcRelAggregates")):
        related = tuple(relationship.RelatedObjects or ())
        relationship.RelatedObjects = tuple(
            sorted(related, key=lambda item: (str(item.is_a()), str(getattr(item, "GlobalId", "") or ""), item.id()))
        )


def _verify_command_postconditions(
    base_content: bytes,
    content: bytes,
    command: ApplyIfcSpatialStructureCommand,
    preflight: _Preflight,
    saved: IfcSpatialStructureCatalog,
) -> None:
    items = {item.global_id: item for item in saved.items}
    for client_ref, global_id in preflight.create_global_ids.items():
        if global_id not in items:
            raise IfcSpatialStructureIntegrityError(
                f"created client_ref {client_ref!r} is absent after save"
            )
    for global_id in preflight.delete_ids:
        if global_id in items:
            raise IfcSpatialStructureIntegrityError("deleted spatial object remains after save")
    set_targets = {
        operation.target_global_id
        for operation in command.operations
        if isinstance(operation, SetIfcSpatialPlacementOperation)
    }
    before_catalog: IfcSpatialStructureCatalog | None = None
    for operation in command.operations:
        if isinstance(operation, ReparentIfcSpatialOperation):
            if operation.target_global_id in set_targets:
                continue
            # The exact parent is already checked by strict inspection.  Compare
            # the target world frame across independently reopened revisions.
            if before_catalog is None:
                before_catalog = IfcOpenShellSpatialStructureCompiler().inspect_spatial_structure(base_content)
            old = next(item for item in before_catalog.items if item.global_id == operation.target_global_id)
            new = items[operation.target_global_id]
            if not matrices_close(placement_matrix(old.project_world_placement), placement_matrix(new.project_world_placement)):
                raise IfcSpatialStructureIntegrityError("reparent changed target project-world placement")


def _depth(global_id: str, states: dict[str, _NodeState]) -> int:
    depth = 0
    current = global_id
    seen: set[str] = set()
    while current != _PROJECT:
        if current in seen:
            raise IfcSpatialCycleError("spatial graph is cyclic")
        seen.add(current)
        state = states.get(current)
        if state is None:
            break
        current = state.parent_global_id
        depth += 1
    return depth


def _require_project(model: Any) -> Any:
    projects = tuple(model.by_type("IfcProject"))
    if len(projects) != 1:
        raise IfcSpatialStructureIntegrityError("IFC revision must contain exactly one IfcProject")
    _require_global_id(projects[0], "IfcProject")
    return projects[0]


def _inspect_root_global_ids(model: Any) -> None:
    seen: set[str] = set()
    for entity in tuple(model.by_type("IfcRoot")):
        global_id = _require_global_id(entity, entity.is_a())
        if global_id in seen:
            raise IfcSpatialStructureIntegrityError("IfcRoot GlobalId values must be unique")
        seen.add(global_id)


def _require_global_id(entity: Any, label: str) -> str:
    value = str(getattr(entity, "GlobalId", "") or "")
    if len(value) != 22 or any(character not in _GLOBAL_ID_CHARS for character in value):
        raise IfcSpatialStructureIntegrityError(f"{label} requires a valid GlobalId")
    return value


def _stable_global_id(seed: str) -> str:
    return ifcopenshell.guid.compress(uuid5(NAMESPACE_URL, f"cad2bim:ifc:{seed}").hex)


def _by_guid(model: Any, global_id: str) -> Any | None:
    try:
        return model.by_guid(global_id)
    except RuntimeError:
        return None


def _is_supported_spatial(entity: Any) -> bool:
    return entity is not None and str(entity.is_a()) in _IFC_TO_SPATIAL


def _required_name(entity: Any) -> str:
    value = _optional_text(getattr(entity, "Name", None))
    if not value:
        raise IfcSpatialStructureIntegrityError(f"{entity.is_a()} requires Name")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _decimal_to_compound_angle(value: float | None) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    sign = -1 if value < 0 else 1
    total_microseconds = int(
        (Decimal(str(abs(value))) * Decimal(3600_000_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    degrees, remainder = divmod(total_microseconds, 3600_000_000)
    minutes, remainder = divmod(remainder, 60_000_000)
    seconds, millionths = divmod(remainder, 1_000_000)
    values = [int(degrees), int(minutes), int(seconds), int(millionths)]
    if sign < 0:
        for index, item in enumerate(values):
            if item:
                values[index] = -item
                break
    return tuple(values)  # type: ignore[return-value]


def _compound_angle_to_decimal(value: Any) -> float:
    items = [int(item) for item in tuple(value)]
    if len(items) not in {3, 4}:
        raise IfcSpatialStructureIntegrityError("IfcCompoundPlaneAngleMeasure is malformed")
    sign = -1 if any(item < 0 for item in items) else 1
    absolute = [abs(item) for item in items]
    millionths = absolute[3] if len(absolute) == 4 else 0
    if absolute[1] >= 60 or absolute[2] >= 60 or millionths >= 1_000_000:
        raise IfcSpatialStructureIntegrityError("IfcCompoundPlaneAngleMeasure is out of range")
    return sign * (
        absolute[0] + absolute[1] / 60.0 + (absolute[2] + millionths / 1_000_000.0) / 3600.0
    )


def _sample_global_ids(entities: Any) -> tuple[str, ...]:
    values = sorted(
        {
            str(getattr(entity, "GlobalId", "") or "")
            for entity in entities
            if len(str(getattr(entity, "GlobalId", "") or "")) == 22
        }
    )
    return tuple(values[:MAX_IFC_SPATIAL_BLOCKER_SAMPLES])


__all__ = ["IfcOpenShellSpatialStructureCompiler"]
