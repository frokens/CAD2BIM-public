"""Rigid project-world placement helpers for the IFC spatial compiler."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np
import numpy.typing as npt
import ifcopenshell.api.geometry
import ifcopenshell.util.element
import ifcopenshell.util.placement

from CAD2BIM_backend.domain.ifc_spatial_structure import (
    IfcProjectWorldPlacement,
    IfcSpatialPlacementInvalidError,
    IfcSpatialResourceLimitError,
    IfcSpatialStructureIntegrityError,
    MAX_IFC_SPATIAL_IMPACT_ENTITIES,
    MAX_IFC_SPATIAL_IMPACT_WORK_UNITS,
)


Matrix4 = npt.NDArray[np.float64]
_TOLERANCE = 1e-8


@dataclass(slots=True)
class _ImpactBudget:
    max_entities: int
    max_work_units: int
    entity_ids: set[int] = field(default_factory=set)
    work_units: int = 0

    def add_entity(self, entity: Any) -> bool:
        entity_id = entity.id()
        if entity_id in self.entity_ids:
            return False
        if len(self.entity_ids) >= self.max_entities:
            raise IfcSpatialResourceLimitError(
                "spatial placement impact exceeds the entity limit"
            )
        self.entity_ids.add(entity_id)
        return True

    def add_work(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("impact work count must not be negative")
        if self.work_units + count > self.max_work_units:
            raise IfcSpatialResourceLimitError(
                "spatial placement impact exceeds the work-unit limit"
            )
        self.work_units += count


def placement_matrix(value: IfcProjectWorldPlacement) -> Matrix4:
    """Convert the closed public frame to an IfcOpenShell world matrix."""

    if not isinstance(value, IfcProjectWorldPlacement):
        raise TypeError("value must be IfcProjectWorldPlacement")
    return ifcopenshell.util.placement.a2p(value.origin, value.axis, value.ref_direction)


def project_world_placement(product: Any) -> IfcProjectWorldPlacement:
    placement = getattr(product, "ObjectPlacement", None)
    if placement is None or not placement.is_a("IfcLocalPlacement"):
        raise IfcSpatialStructureIntegrityError(
            f"{product.is_a()} {getattr(product, 'GlobalId', '')!r} requires IfcLocalPlacement"
        )
    if _placement_chain_has_cycle(placement):
        raise IfcSpatialStructureIntegrityError("IfcLocalPlacement chain is cyclic")
    try:
        matrix = np.asarray(
            ifcopenshell.util.placement.get_local_placement(placement), dtype=float
        )
        validate_rigid_matrix(matrix, integrity=True)
        return placement_from_matrix(matrix)
    except IfcSpatialStructureIntegrityError:
        raise
    except Exception as exc:
        raise IfcSpatialStructureIntegrityError(
            "IfcLocalPlacement chain could not be resolved"
        ) from exc


def placement_from_matrix(matrix: npt.ArrayLike) -> IfcProjectWorldPlacement:
    value = np.asarray(matrix, dtype=float)
    validate_rigid_matrix(value, integrity=True)
    return IfcProjectWorldPlacement(
        origin=tuple(float(item) for item in value[:3, 3]),
        axis=tuple(float(item) for item in value[:3, 2]),
        ref_direction=tuple(float(item) for item in value[:3, 0]),
    )


def validate_rigid_matrix(matrix: npt.ArrayLike, *, integrity: bool = False) -> Matrix4:
    value = np.asarray(matrix, dtype=float)
    error_type = IfcSpatialStructureIntegrityError if integrity else IfcSpatialPlacementInvalidError
    if value.shape != (4, 4) or not np.isfinite(value).all():
        raise error_type("project-world placement must be a finite 4x4 matrix")
    if not np.allclose(value[3], (0.0, 0.0, 0.0, 1.0), atol=_TOLERANCE):
        raise error_type("project-world placement must be affine")
    rotation = value[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=_TOLERANCE):
        raise error_type("project-world placement axes must be orthonormal")
    if not np.isclose(np.linalg.det(rotation), 1.0, atol=_TOLERANCE):
        raise error_type("project-world placement must be rigid and right-handed")
    return value


def set_project_world_matrix(
    model: Any,
    product: Any,
    matrix: npt.ArrayLike,
    *,
    should_transform_children: bool = True,
) -> bool:
    """Write one absolute project-unit matrix when it is not already satisfied.

    ``should_transform_children=False`` declares the bulk-compiler contract:
    the caller explicitly visits the complete semantic closure in
    parent-before-child order.  IfcOpenShell 0.8.5 gives its identically named
    API argument the inverse *implementation* consequence: ``False`` collects
    placement dependants and recursively edits them, while ``True`` skips that
    recursion.  We therefore always pass ``True`` to the library and let the
    bulk caller make the explicit, counted writes.

    The boolean result reports whether an IfcOpenShell placement edit was
    required.  A matching world matrix is skipped only when the existing
    ``PlacementRelTo`` also matches the product's current semantic parent; this
    preserves the required normalization after a reparent.
    """

    if not isinstance(should_transform_children, bool):
        raise TypeError("should_transform_children must be bool")
    value = validate_rigid_matrix(matrix).copy()
    placement = getattr(product, "ObjectPlacement", None)
    if placement is not None and placement.is_a("IfcLocalPlacement"):
        if _placement_chain_has_cycle(placement):
            raise IfcSpatialPlacementInvalidError("semantic placement chain is cyclic")
        current = np.asarray(
            ifcopenshell.util.placement.get_local_placement(placement), dtype=float
        )
        if matrices_close(current, value) and placement.PlacementRelTo == (
            _semantic_placement_parent(product)
        ):
            return False

    detach_shared_object_placement(model, product)
    try:
        # Do not forward ``should_transform_children``.  In IfcOpenShell 0.8.5
        # False means "recursively compensate children", which is precisely
        # what the explicit bulk traversal must avoid.
        ifcopenshell.api.geometry.edit_object_placement(
            model,
            product=product,
            matrix=value,
            is_si=False,
            should_transform_children=True,
        )
    except Exception as exc:
        raise IfcSpatialPlacementInvalidError(
            f"placement for {getattr(product, 'GlobalId', '')!r} could not be compiled"
        ) from exc
    return True


def detach_shared_object_placement(model: Any, product: Any) -> None:
    """Give one product an equivalent placement when PlacesObject is shared.

    IfcOpenShell's edit usecase intentionally rewires placements referencing
    the edited placement.  Splitting a multiply-owned ObjectPlacement first
    prevents an unrelated product sharing that node from being moved.
    """

    old = getattr(product, "ObjectPlacement", None)
    if old is None or not old.is_a("IfcLocalPlacement"):
        return
    owners = tuple(getattr(old, "PlacesObject", ()) or ())
    if len(owners) <= 1:
        return
    if tuple(getattr(old, "ReferencedByPlacements", ()) or ()):
        raise IfcSpatialPlacementInvalidError(
            "shared ObjectPlacement with placement dependants cannot be safely split"
        )
    product.ObjectPlacement = model.create_entity(
        "IfcLocalPlacement",
        PlacementRelTo=old.PlacementRelTo,
        RelativePlacement=old.RelativePlacement,
    )


def validate_semantic_placement_closure(
    root: Any,
    *,
    max_entities: int = MAX_IFC_SPATIAL_IMPACT_ENTITIES,
    max_work_units: int = MAX_IFC_SPATIAL_IMPACT_WORK_UNITS,
) -> tuple[Any, ...]:
    """Return a validated, deterministically budgeted semantic closure."""

    if max_entities < 1 or max_work_units < 1:
        raise ValueError("spatial impact limits must be positive")
    budget = _ImpactBudget(max_entities=max_entities, max_work_units=max_work_units)
    semantic = (root, *_semantic_descendants(root, budget))
    closure = list(semantic)
    allowed = {product.id() for product in semantic}
    queue = deque(semantic)
    inspected_placements: set[int] = set()
    while queue:
        product = queue.popleft()
        budget.add_work()
        placement = getattr(product, "ObjectPlacement", None)
        if placement is None or not placement.is_a("IfcLocalPlacement"):
            raise IfcSpatialPlacementInvalidError(
                f"semantic object {getattr(product, 'GlobalId', '')!r} has unsupported placement"
            )
        if _placement_chain_has_cycle(placement, budget=budget):
            raise IfcSpatialPlacementInvalidError("semantic placement chain is cyclic")
        if placement.id() in inspected_placements:
            continue
        inspected_placements.add(placement.id())
        owners = getattr(placement, "PlacesObject", ()) or ()
        referenced_placements = getattr(placement, "ReferencedByPlacements", ()) or ()
        if len(owners) > 1:
            for owner in owners:
                budget.add_work()
                if owner.id() not in allowed:
                    raise IfcSpatialPlacementInvalidError(
                        "shared ObjectPlacement crosses the spatial semantic closure"
                    )
            for referenced in referenced_placements:
                budget.add_work()
                for dependant in getattr(referenced, "PlacesObject", ()) or ():
                    budget.add_work()
                    if dependant.id() not in allowed:
                        raise IfcSpatialPlacementInvalidError(
                            "shared ObjectPlacement crosses the spatial semantic closure"
                        )
        for referenced in referenced_placements:
            budget.add_work()
            if not referenced.is_a("IfcLocalPlacement"):
                raise IfcSpatialPlacementInvalidError("non-local placement dependant is unsupported")
            placed_objects = getattr(referenced, "PlacesObject", ()) or ()
            for dependant in placed_objects:
                budget.add_work()
                if dependant.id() in allowed:
                    continue
                if dependant.is_a("IfcDistributionPort") or dependant.is_a("IfcFeatureElement"):
                    budget.add_entity(dependant)
                    allowed.add(dependant.id())
                    closure.append(dependant)
                    queue.append(dependant)
                    continue
                if len(placed_objects) > 1:
                    for owner in placed_objects:
                        budget.add_work()
                        if owner.id() in allowed:
                            raise IfcSpatialPlacementInvalidError(
                                "shared ObjectPlacement crosses the spatial semantic closure"
                            )
                raise IfcSpatialPlacementInvalidError(
                    "placement dependant is outside the spatial semantic subtree"
                )
    return tuple(closure)


def isolate_shared_closure_placements(model: Any, products: Iterable[Any]) -> tuple[Any, ...]:
    """Split fully in-closure shared placement owners before world rewrites.

    IfcOpenShell rewires every placement dependant when it edits one owner of a
    shared ObjectPlacement.  Cloning an equivalent placement for every closure
    owner first prevents the first edited owner from capturing dependants of
    the other owners.  Dependants remain on the old anchor until each closure
    product is explicitly rewritten in semantic parent-before-child order.
    """

    closure = tuple(products)
    allowed = {product.id() for product in closure}
    shared: dict[int, Any] = {}
    for product in closure:
        placement = getattr(product, "ObjectPlacement", None)
        if placement is None or not placement.is_a("IfcLocalPlacement"):
            continue
        owners = tuple(getattr(placement, "PlacesObject", ()) or ())
        if len(owners) > 1:
            shared[placement.id()] = placement

    anchors: list[Any] = []
    for placement in shared.values():
        owners = tuple(getattr(placement, "PlacesObject", ()) or ())
        dependants = tuple(
            dependant
            for referenced in tuple(getattr(placement, "ReferencedByPlacements", ()) or ())
            for dependant in tuple(getattr(referenced, "PlacesObject", ()) or ())
        )
        if any(owner.id() not in allowed for owner in owners) or any(
            dependant.id() not in allowed for dependant in dependants
        ):
            raise IfcSpatialPlacementInvalidError(
                "shared ObjectPlacement crosses the spatial semantic closure"
            )
        anchors.append(placement)
        for owner in owners:
            owner.ObjectPlacement = model.create_entity(
                "IfcLocalPlacement",
                PlacementRelTo=placement.PlacementRelTo,
                RelativePlacement=placement.RelativePlacement,
            )
    return tuple(anchors)


def cleanup_isolated_placement_anchors(model: Any, anchors: Iterable[Any]) -> None:
    for placement in anchors:
        if model.get_total_inverses(placement) == 0:
            ifcopenshell.util.element.remove_deep2(model, placement)


def semantic_descendants(
    root: Any,
    *,
    max_entities: int = MAX_IFC_SPATIAL_IMPACT_ENTITIES,
    max_work_units: int = MAX_IFC_SPATIAL_IMPACT_WORK_UNITS,
) -> tuple[Any, ...]:
    """Return the ordered rigid subtree rooted at one spatial object.

    Spatial descendants come only from the spatial aggregate tree.  Product
    closure starts only from primary containment in that tree, then follows
    product aggregation, nesting, and direct local-placement dependants.  This
    includes assemblies, valves, fittings, ports, and features owned by a
    contained product without admitting arbitrary products placed relative to
    a spatial object or any unrelated model-wide relationship.
    """

    if max_entities < 1 or max_work_units < 1:
        raise ValueError("spatial impact limits must be positive")
    budget = _ImpactBudget(max_entities=max_entities, max_work_units=max_work_units)
    return _semantic_descendants(root, budget)


def _semantic_descendants(root: Any, budget: _ImpactBudget) -> tuple[Any, ...]:
    spatial: list[Any] = []
    budget.add_entity(root)
    queue = deque([root])
    while queue:
        parent = queue.popleft()
        budget.add_work()
        children: list[Any] = []
        for relationship in getattr(parent, "IsDecomposedBy", ()) or ():
            budget.add_work()
            if not relationship.is_a("IfcRelAggregates"):
                continue
            for child in relationship.RelatedObjects or ():
                budget.add_work()
                if child.is_a("IfcSpatialStructureElement") and budget.add_entity(child):
                    children.append(child)
        for child in sorted(children, key=_entity_key):
            spatial.append(child)
            queue.append(child)

    products: list[Any] = []
    product_queue: deque[Any] = deque()
    for container in (root, *spatial):
        budget.add_work()
        for relationship in getattr(container, "ContainsElements", ()) or ():
            budget.add_work()
            candidates: list[Any] = []
            for product in relationship.RelatedElements or ():
                budget.add_work()
                if budget.add_entity(product):
                    candidates.append(product)
            for product in sorted(candidates, key=_entity_key):
                products.append(product)
                product_queue.append(product)

    while product_queue:
        product = product_queue.popleft()
        budget.add_work()
        owned: list[Any] = []
        for relationship in getattr(product, "IsDecomposedBy", ()) or ():
            budget.add_work()
            if relationship.is_a("IfcRelAggregates"):
                for candidate in relationship.RelatedObjects or ():
                    budget.add_work()
                    owned.append(candidate)
        for relationship in getattr(product, "IsNestedBy", ()) or ():
            budget.add_work()
            if relationship.is_a("IfcRelNests"):
                for candidate in relationship.RelatedObjects or ():
                    budget.add_work()
                    owned.append(candidate)

        placement = getattr(product, "ObjectPlacement", None)
        if placement is not None and placement.is_a("IfcLocalPlacement"):
            for referenced in getattr(placement, "ReferencedByPlacements", ()) or ():
                budget.add_work()
                if not referenced.is_a("IfcLocalPlacement"):
                    continue
                for candidate in getattr(referenced, "PlacesObject", ()) or ():
                    budget.add_work()
                    owned.append(candidate)

        for dependant in sorted(set(owned), key=_entity_key):
            if not dependant.is_a("IfcProduct") or dependant.is_a(
                "IfcSpatialStructureElement"
            ):
                continue
            if not budget.add_entity(dependant):
                continue
            products.append(dependant)
            product_queue.append(dependant)
    return tuple(spatial + products)


def capture_world_matrices(products: Iterable[Any]) -> dict[int, Matrix4]:
    result: dict[int, Matrix4] = {}
    for product in products:
        placement = getattr(product, "ObjectPlacement", None)
        if placement is None or not placement.is_a("IfcLocalPlacement"):
            raise IfcSpatialPlacementInvalidError(
                f"semantic descendant {getattr(product, 'GlobalId', '')!r} has unsupported placement"
            )
        if _placement_chain_has_cycle(placement):
            raise IfcSpatialPlacementInvalidError("semantic descendant placement chain is cyclic")
        matrix = np.asarray(ifcopenshell.util.placement.get_local_placement(placement), dtype=float)
        result[product.id()] = validate_rigid_matrix(matrix).copy()
    return result


def move_semantic_subtree(
    model: Any,
    root: Any,
    new_root_world: IfcProjectWorldPlacement,
    *,
    preserve_descendant_project_world: bool,
    prevalidated_affected: tuple[Any, ...] | None = None,
) -> tuple[Any, ...]:
    affected = (
        validate_semantic_placement_closure(root)
        if prevalidated_affected is None
        else prevalidated_affected
    )
    if not affected or affected[0] != root:
        raise IfcSpatialPlacementInvalidError(
            "prevalidated spatial placement impact must begin with its root"
        )
    descendants = affected[1:]
    before = capture_world_matrices(affected)
    isolated_anchors = isolate_shared_closure_placements(model, affected)
    normalized = capture_world_matrices(affected)
    for product in affected:
        if not matrices_close(normalized[product.id()], before[product.id()]):
            raise IfcSpatialPlacementInvalidError(
                "shared ObjectPlacement isolation changed project-world placement"
            )
    target = placement_matrix(new_root_world)
    delta = target @ np.linalg.inv(before[root.id()])

    # Parent-before-child order is essential: every edit computes its local
    # placement from the relationship state that already exists in the model.
    set_project_world_matrix(
        model,
        root,
        target,
        should_transform_children=False,
    )
    for product in descendants:
        desired = (
            before[product.id()]
            if preserve_descendant_project_world
            else delta @ before[product.id()]
        )
        set_project_world_matrix(
            model,
            product,
            desired,
            should_transform_children=False,
        )
    after = capture_world_matrices(affected)
    for product in affected:
        expected = (
            target
            if product == root
            else (
                before[product.id()]
                if preserve_descendant_project_world
                else delta @ before[product.id()]
            )
        )
        if not matrices_close(after[product.id()], expected):
            raise IfcSpatialPlacementInvalidError(
                f"semantic object {getattr(product, 'GlobalId', '')!r} did not reach its required project-world placement"
            )
    cleanup_isolated_placement_anchors(model, isolated_anchors)
    return affected


def matrices_close(left: npt.ArrayLike, right: npt.ArrayLike) -> bool:
    return bool(np.allclose(np.asarray(left), np.asarray(right), atol=_TOLERANCE, rtol=0.0))


def _placement_chain_has_cycle(
    placement: Any, *, budget: _ImpactBudget | None = None
) -> bool:
    seen: set[int] = set()
    current = placement
    while current is not None:
        if budget is not None:
            budget.add_work()
        identity = current.id()
        if identity in seen:
            return True
        seen.add(identity)
        if not current.is_a("IfcLocalPlacement"):
            return True
        current = current.PlacementRelTo
    return False


def _semantic_placement_parent(product: Any) -> Any | None:
    """Match IfcOpenShell's relationship-derived ``PlacementRelTo`` policy."""

    relating_object = None
    if relations := getattr(product, "Decomposes", None):
        relating_object = relations[0].RelatingObject
    elif relations := getattr(product, "Nests", None):
        relating_object = relations[0].RelatingObject
    elif relations := getattr(product, "ContainedIn", None):
        relating_object = relations[0].RelatedElement
    elif relations := getattr(product, "VoidsElements", None):
        relating_object = relations[0].RelatingBuildingElement
    elif relations := getattr(product, "FillsVoids", None):
        relating_object = relations[0].RelatingOpeningElement
    elif relations := getattr(product, "ProjectsElements", None):
        relating_object = relations[0].RelatingElement
    elif relations := getattr(product, "AdheresToElement", None):
        relating_object = relations[0].RelatingElement
    elif relations := getattr(product, "ContainedInStructure", None):
        return relations[0].RelatingStructure.ObjectPlacement
    if relating_object is not None:
        return getattr(relating_object, "ObjectPlacement", None)
    return None


def _entity_key(entity: Any) -> tuple[str, str, int]:
    return (str(entity.is_a()), str(getattr(entity, "GlobalId", "") or ""), entity.id())


__all__ = [
    "Matrix4",
    "capture_world_matrices",
    "cleanup_isolated_placement_anchors",
    "detach_shared_object_placement",
    "matrices_close",
    "move_semantic_subtree",
    "placement_from_matrix",
    "placement_matrix",
    "project_world_placement",
    "semantic_descendants",
    "isolate_shared_closure_placements",
    "set_project_world_matrix",
    "validate_rigid_matrix",
    "validate_semantic_placement_closure",
]
