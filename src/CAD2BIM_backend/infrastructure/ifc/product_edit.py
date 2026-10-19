"""Bounded product ownership, dependency protection and edits in one IFC candidate."""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import nullcontext
import time

import ifcopenshell
import ifcopenshell.util.element
import ifcopenshell.util.placement
import ifcopenshell.util.unit
import numpy as np

from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.ifc_product_compilation import (
    CompileIfcProductsCommand, IfcProductCompilationBlock, IfcProductCompilationError,
)
from CAD2BIM_backend.domain.product_edit import (
    CompileIfcProductsEditCommand, IfcProductEditExecutionProduct, IfcProductEditOwnedProduct,
    IfcProductEditPreflightProduct, IfcProductsEditPreflightRequest, IfcProductsEditPreflightResult,
    IfcProductsEditRevisionEffect, ProductEditChange, ProductEditCheck,
)
from CAD2BIM_backend.infrastructure.ifc.placement import validate_rigid_matrix, matrices_close
from CAD2BIM_backend.infrastructure.ifc.product_compilation import IfcOpenShellProductCompiler
from CAD2BIM_backend.infrastructure.worker_runtime.timing import timed_phase


def _guid(value):
    return getattr(value, "GlobalId", None)


def _parents(product):
    return tuple(r for r in (*getattr(product, "Decomposes", ()), *getattr(product, "Nests", ()))
                 if r.is_a("IfcRelAggregates") or r.is_a("IfcRelNests"))


def _owned(model, root, *, inverses=None):
    result, seen, active = [], set(), set()

    def visit(entity, parent=None, relation=None):
        if entity.id() in active or entity.id() in seen:
            raise ValueError("ownership has a cycle or multiple paths to the same product")
        if not entity.is_a("IfcProduct") or entity.is_a("IfcSpatialElement"):
            raise ValueError("ownership contains an unsupported non-product or spatial object")
        if len(_parents(entity)) > 1:
            raise ValueError("product has contradictory aggregate/nest parents")
        seen.add(entity.id()); active.add(entity.id())
        result.append(IfcProductEditOwnedProduct(global_id=entity.GlobalId, ifc_class=entity.is_a(),
            parent_global_id=_guid(parent), ownership_relation_class=relation))
        children = []
        for rel in (*getattr(entity, "IsDecomposedBy", ()), *getattr(entity, "IsNestedBy", ())):
            if rel.is_a("IfcRelAggregates") or rel.is_a("IfcRelNests"):
                children.extend((child, rel.is_a()) for child in rel.RelatedObjects)
        for rel in (inverses(entity) if inverses else model.get_inverse(entity)):
            if rel.is_a("IfcRelConnectsPortToElement") and rel.RelatedElement == entity:
                children.append((rel.RelatingPort, rel.is_a()))
            elif rel.is_a("IfcRelVoidsElement") and rel.RelatingBuildingElement == entity:
                children.append((rel.RelatedOpeningElement, rel.is_a()))
            elif rel.is_a("IfcRelProjectsElement") and rel.RelatingElement == entity:
                children.append((rel.RelatedFeatureElement, rel.is_a()))
            elif rel.is_a("IfcRelAdheresToElement") and rel.RelatingElement == entity:
                children.extend((child, rel.is_a()) for child in rel.RelatedSurfaceFeatures)
        unique = {}
        for child, kind in children:
            unique[child.id()] = (child, kind)
        for child, kind in sorted(unique.values(), key=lambda x: x[0].GlobalId):
            visit(child, entity, kind)
        active.remove(entity.id())
    visit(root)
    return tuple(result)


def _product_references(relation):
    found = []
    def collect(value):
        if isinstance(value, tuple):
            for item in value: collect(item)
        elif isinstance(value, ifcopenshell.entity_instance) and value.is_a("IfcProduct"):
            found.append(value)
    for value in relation: collect(value)
    return found


def _reference_occurrences(entity):
    """Count forward entity references, including repeated aggregate members.

    IfcOpenShell's get_total_inverses counts reference occurrences, not distinct
    inverse entities. This supplies a safe upper bound for allowed references.
    """
    references = Counter()
    def collect(value):
        if isinstance(value, ifcopenshell.entity_instance):
            if value.id(): references[value.id()] += 1
        elif isinstance(value, tuple):
            for item in value: collect(item)
    for value in entity: collect(value)
    return references


def _supported_relation(rel):
    return any(rel.is_a(kind) for kind in (
        "IfcRelAggregates", "IfcRelNests", "IfcRelContainedInSpatialStructure", "IfcRelReferencedInSpatialStructure",
        "IfcRelAssigns", "IfcRelAssociates", "IfcRelDefinesByProperties", "IfcRelDefinesByType",
        "IfcRelConnectsElements", "IfcRelConnectsPorts", "IfcRelConnectsPortToElement", "IfcRelSpaceBoundary",
        "IfcRelVoidsElement", "IfcRelFillsElement", "IfcRelProjectsElement", "IfcRelAdheresToElement",
    ))


def preflight_edit_targets(model, request: IfcProductsEditPreflightRequest):
    checks, products, closures = [], [], []
    inverse_cache, presentation_cache, relation_cache, placement_cache = {}, {}, {}, {}
    def inverses(entity):
        identifier = entity.id()
        if identifier not in inverse_cache:
            inverse_cache[identifier] = tuple(model.get_inverse(entity))
        return inverse_cache[identifier]
    def presentation(entity):
        identifier = entity.id()
        if identifier not in presentation_cache:
            presentation_cache[identifier] = tuple(inverse for inverse in model.get_inverse(entity)
                if inverse.is_a("IfcStyledItem") or inverse.is_a("IfcShapeAspect")
                or inverse.is_a("IfcPresentationLayerAssignment"))
        return presentation_cache[identifier]
    def relation_facts(rel):
        identifier = rel.id()
        if identifier not in relation_cache:
            relation_cache[identifier] = (
                _supported_relation(rel), tuple(_product_references(rel)),
                frozenset(p.id() for p in (getattr(rel, "RelatedObjects", ()) or ())),
            )
        return relation_cache[identifier]
    def validate_placement(placement):
        identifier = placement.id()
        if identifier not in placement_cache:
            try:
                validate_rigid_matrix(ifcopenshell.util.placement.get_local_placement(placement))
                for dependant in placement.ReferencedByPlacements or ():
                    if not dependant.is_a("IfcLocalPlacement"):
                        raise ValueError("non-local external placement dependant")
                    validate_rigid_matrix(ifcopenshell.util.placement.get_local_placement(dependant))
            except Exception:
                placement_cache[identifier] = False
            else:
                placement_cache[identifier] = True
        return placement_cache[identifier]
    wanted = {row.target_global_id for row in request.targets}
    matches = defaultdict(list)
    for entity in model.by_type("IfcRoot"):
        if entity.GlobalId in wanted: matches[entity.GlobalId].append(entity)

    def fail(row, code, message, related=None):
        checks.append(ProductEditCheck(check_index=len(checks), input_row_index=row.input_row_index,
            product_key=row.product_key, target_global_id=row.target_global_id, related_global_id=related,
            phase="precheck", check_kind="target_dependency", status="FAIL", error_code=code,
            message=message, recommended_action="Inspect the named target and related relationship; choose an unambiguous supported subtree or defer network/feature editing."))

    for row in request.targets:
        candidates = matches[row.target_global_id]
        root = candidates[0] if len(candidates) == 1 else None
        owned = ()
        if root is None:
            fail(row, "IFC_EDIT_TARGET_INVALID", "Target GUID is missing or ambiguous")
        elif not root.is_a("IfcElement") or root.is_a("IfcFeatureElement"):
            fail(row, "IFC_EDIT_TARGET_UNSUPPORTED", "Target must be a physical IfcElement occurrence, not a feature/spatial/type/port")
        else:
            try:
                owned = _owned(model, root, inverses=inverses)
                if len({item.global_id for item in owned}) != len(owned):
                    raise ValueError("owned products have duplicate GlobalIds")
            except ValueError as exc:
                fail(row, "IFC_EDIT_OWNERSHIP_INVALID", str(exc))
        products.append(IfcProductEditPreflightProduct(input_row_index=row.input_row_index,
            target_global_id=row.target_global_id, before_ifc_class=root.is_a() if root else None, owned_products=owned))
        ids = {x.global_id for x in owned}; closures.append(ids)
        external_by_relation = {}
        for fact in owned:
            entity = model.by_guid(fact.global_id)
            if row.action in {"replace", "placement"}:
                placement = getattr(entity, "ObjectPlacement", None)
                if placement is not None and not placement.is_a("IfcLocalPlacement"):
                    fail(row, "IFC_EDIT_PLACEMENT_UNSUPPORTED", "Grid/linear placement is outside the rigid edit support domain", fact.global_id)
                if row.action == "placement" and placement is None:
                    fail(row, "IFC_EDIT_PLACEMENT_UNSUPPORTED", "Owned product has no readable local placement", fact.global_id)
                if placement is not None and placement.is_a("IfcLocalPlacement"):
                    if not validate_placement(placement):
                        fail(row, "IFC_EDIT_PLACEMENT_UNSUPPORTED", "Placement or its external dependency is non-rigid, cyclic or unreadable", fact.global_id)
            for rel in inverses(entity):
                supported, references, members = relation_facts(rel)
                if not supported:
                    fail(row, "IFC_EDIT_REFERENCE_UNSUPPORTED", f"Unsupported reference {rel.is_a()} to product", _guid(rel))
                    continue
                if rel.id() not in external_by_relation:
                    external_by_relation[rel.id()] = next((p for p in references if p.GlobalId not in ids), None)
                external = external_by_relation[rel.id()]
                geometric = rel.is_a("IfcRelConnectsElements") or rel.is_a("IfcRelConnectsPorts") or rel.is_a("IfcRelSpaceBoundary")
                feature = any(rel.is_a(c) for c in ("IfcRelVoidsElement", "IfcRelFillsElement", "IfcRelProjectsElement", "IfcRelAdheresToElement"))
                if external and ((geometric and row.action != "delete") or feature):
                    fail(row, "IFC_EDIT_EXTERNAL_CONNECTION" if geometric else "IFC_EDIT_EXTERNAL_FEATURE",
                         f"{rel.is_a()} crosses the owned subtree; preserving its geometry needs network/feature editing", _guid(external))
                safe_membership = any(rel.is_a(c) for c in (
                    "IfcRelAssignsToGroup", "IfcRelAssociatesClassification", "IfcRelAssociatesDocument",
                    "IfcRelDefinesByProperties", "IfcRelDefinesByType", "IfcRelAssociatesMaterial")) and entity.id() in members
                if row.action == "replace" and entity != root and external and not safe_membership and not (rel.is_a("IfcRelAggregates") or rel.is_a("IfcRelNests")):
                    fail(row, "IFC_EDIT_RETIRED_CHILD_REFERENCE", f"Retired component has external {rel.is_a()} reference", _guid(external))
                if row.action == "replace" and entity != root and not safe_membership and (rel.is_a("IfcRelAssigns") or rel.is_a("IfcRelAssociates")):
                    fail(row, "IFC_EDIT_RETIRED_CHILD_REFERENCE", f"Retired component has {rel.is_a()} semantics that cannot be mapped to a new component", _guid(rel))
                if row.action == "replace" and entity == root and rel.is_a("IfcRelAssociatesMaterial"):
                    material = rel.RelatingMaterial
                    if any(material.is_a(c) for c in ("IfcMaterialLayerSet", "IfcMaterialLayerSetUsage", "IfcMaterialProfileSet", "IfcMaterialProfileSetUsage")):
                        fail(row, "IFC_EDIT_MATERIAL_GEOMETRY_UNSUPPORTED", "Layer/profile material semantics cannot be guaranteed for replacement geometry", fact.global_id)
            if entity.Representation and not entity.Representation.is_a("IfcProductDefinitionShape"):
                fail(row, "IFC_EDIT_REPRESENTATION_UNSUPPORTED", "Product representation requires an explicit ProductDefinitionShape wrapper", fact.global_id)
            if row.action == "replace" and entity.Representation:
                for node in model.traverse(entity.Representation):
                    for inverse in presentation(node):
                        if inverse.is_a("IfcStyledItem") or inverse.is_a("IfcShapeAspect"):
                            fail(row, "IFC_EDIT_PRESENTATION_UNSUPPORTED", f"Cannot map old {inverse.is_a()} to new subgeometry", fact.global_id)
                        if inverse.is_a("IfcPresentationLayerAssignment") and node not in entity.Representation.Representations:
                            fail(row, "IFC_EDIT_PRESENTATION_UNSUPPORTED", "Layer assignment to subgeometry has no exact new item mapping", fact.global_id)
    target_rows, owned_rows = defaultdict(list), defaultdict(list)
    for index, row in enumerate(request.targets):
        overlapping = set(target_rows[row.target_global_id])
        for guid in closures[index]:
            overlapping.update(owned_rows[guid])
        for prior in sorted(overlapping):
            fail(row, "IFC_EDIT_TARGET_OVERLAP", f"Target overlaps input row {request.targets[prior].input_row_index}", request.targets[prior].target_global_id)
        target_rows[row.target_global_id].append(index)
        for guid in closures[index]:
            owned_rows[guid].append(index)
    return IfcProductsEditPreflightResult(products=tuple(products), checks=tuple(checks), accepted=not any(x.status == "FAIL" for x in checks))


def _deep_cleanup(model, node):
    if node is None or model.get_total_inverses(node): return
    protected = {x for x in model.traverse(node) if any(x.is_a(c) for c in (
        "IfcTypeObject", "IfcMaterialDefinition", "IfcProfileDef", "IfcPresentationStyle", "IfcRepresentationContext", "IfcPropertySetTemplate"))}
    ifcopenshell.util.element.remove_deep2(model, node, do_not_delete=protected)


def _world(product):
    return validate_rigid_matrix(ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)).copy()


def _axis(model, matrix):
    return model.create_entity("IfcAxis2Placement3D",
        Location=model.create_entity("IfcCartesianPoint", Coordinates=tuple(float(x) for x in matrix[:3, 3])),
        Axis=model.create_entity("IfcDirection", DirectionRatios=tuple(float(x) for x in matrix[:3, 2])),
        RefDirection=model.create_entity("IfcDirection", DirectionRatios=tuple(float(x) for x in matrix[:3, 0])))


def _frame_json(matrix, unit):
    return RigidTransform3D(rotation=tuple(tuple(float(x) for x in row) for row in matrix[:3,:3]),
        translation_m=tuple(float(x) * unit for x in matrix[:3,3])).model_dump_json()


class IfcOpenShellProductEditor:
    """Consume checked build rows only after the complete target preflight passed."""

    def __init__(self, model, command: CompileIfcProductsEditCommand):
        if not command.preflight.accepted:
            raise IfcProductCompilationError("target preflight rejected the complete edit batch", phase="precheck")
        self.model, self.command = model, command
        self.rows = {r.input_row_index: r for r in command.rows}
        self.facts = {p.input_row_index: p for p in command.preflight.products}
        if len(self.rows) != len(command.rows) or set(self.rows) != set(self.facts):
            raise IfcProductCompilationError("edit rows do not match the fixed target preflight", phase="precheck")
        self.changes, self.checks, self.products, self.bindings = [], list(command.preflight.checks), [], []
        self.compilers, self.maps, self.received = {}, {}, set()
        self.expected_worlds, self.deleted = {}, set()
        self._root_targets, self._layers = {}, {}
        self._presentation_candidates = {}
        self._remove_calls, self._remove_seconds = 0, 0.
        # Freeze all affected handles/roles before the first action modifies relationships.
        self.owned = {index: tuple(model.by_guid(f.global_id) for f in p.owned_products)
                      for index, p in self.facts.items()}
        all_owned = {entity.id() for entities in self.owned.values() for entity in entities}
        self.selected_global_ids = {entity.GlobalId for entities in self.owned.values() for entity in entities}
        self.protected = {}
        definition_users, pds_users, world_snapshots = {}, {}, {}
        def snapshot_world(product):
            identifier = product.ObjectPlacement.id()
            if identifier not in world_snapshots:
                world_snapshots[identifier] = _world(product)
            return world_snapshots[identifier].copy()
        for entities in self.owned.values():
            for entity in entities:
                for rel in model.get_inverse(entity):
                    for field in ("RelatingType", "RelatingMaterial", "RelatingPropertyDefinition"):
                        definition = getattr(rel, field, None)
                        if definition is None: continue
                        if definition.id() not in definition_users:
                            definition_users[definition.id()] = frozenset(p.id() for link in model.get_inverse(definition)
                                for p in getattr(link, "RelatedObjects", ()) or ())
                        users = definition_users[definition.id()]
                        if field == "RelatingMaterial" and definition.is_a("IfcMaterialUsageDefinition") and not users - all_owned:
                            for resource in model.traverse(definition):
                                if resource.is_a("IfcMaterialDefinition") or resource.is_a("IfcProfileDef"):
                                    self.protected[resource.id()] = str(resource)
                            continue
                        if field != "RelatingPropertyDefinition" or users - all_owned:
                            self.protected[definition.id()] = str(definition)
                            if definition.is_a("IfcPropertySet"):
                                self.protected.update({p.id(): str(p) for p in definition.HasProperties})
                pds = entity.Representation
                if pds and pds.id() not in pds_users:
                    pds_users[pds.id()] = any(p.id() not in all_owned for p in pds.ShapeOfProduct)
                if pds and pds_users[pds.id()]:
                    self.protected[pds.id()] = str(pds)
                    self.protected.update({rep.id(): str(rep) for rep in pds.Representations})
                placement = entity.ObjectPlacement
                if placement is not None and placement.is_a("IfcLocalPlacement"):
                    external = [p for p in placement.PlacesObject or () if p.id() not in all_owned]
                    for referenced in placement.ReferencedByPlacements or ():
                        external.extend(p for p in referenced.PlacesObject or () if p.id() not in all_owned)
                    self.expected_worlds.update({p.GlobalId: snapshot_world(p) for p in external})
        for index, row in self.rows.items():
            fact = self.facts[index]
            if row.target_global_id != fact.target_global_id or not self.owned[index]:
                raise IfcProductCompilationError("edit target differs from its prepared exact preflight", input_row_index=index)
            if row.action == "replace":
                family = command.families[row.family_version_id]
                roots = [c for c in family.components if c.parent_component_key is None]
                if len(roots) != 1:
                    raise IfcProductCompilationError("replacement Family must have one root", input_row_index=index)
                self._run_row(row, self._prepare_replace, row, self.owned[index], roots[0].ifc_class.value)
            elif row.action == "delete":
                for entity in reversed(self.owned[index]): self._run_row(row, self._delete, row, entity)
                self.products.append(IfcProductEditExecutionProduct(input_row_index=index,
                    target_global_id=row.target_global_id, before_ifc_class=fact.before_ifc_class))
            else:
                self._run_row(row, self._move, row, self.owned[index])
                self.products.append(IfcProductEditExecutionProduct(input_row_index=index,
                    target_global_id=row.target_global_id, before_ifc_class=fact.before_ifc_class,
                    after_ifc_class=fact.before_ifc_class, result_global_id=row.target_global_id))
        # Preparation can only remove old presentation references. consume may
        # rebind retained layers to new geometry, so no candidate cache survives.
        self._presentation_candidates = None

    @staticmethod
    def _run_row(row, operation, *args):
        try:
            return operation(*args)
        except IfcProductCompilationError:
            raise
        except Exception as exc:
            raise IfcProductCompilationError(str(exc), product_key=row.product_key,
                input_row_index=row.input_row_index, phase="edit") from exc

    def _change(self, row, kind, entity, **values):
        self.changes.append(ProductEditChange(change_index=len(self.changes), input_row_index=row.input_row_index,
            change_kind=kind, global_id=entity.GlobalId, ifc_class=entity.is_a(), **values))

    def _warning(self, row, code, message):
        self.checks.append(ProductEditCheck(check_index=len(self.checks), input_row_index=row.input_row_index,
            product_key=row.product_key, target_global_id=row.target_global_id, phase="execution",
            check_kind="preserved_semantics", status="WARN", error_code=code, message=message,
            recommended_action="Read the original checks and current product facts before relying on inherited or geometry-derived metadata."))

    def _relation_change(self, row, entity, rel, role, removed, related=None, kind="relation_detached"):
        self._change(row, kind, entity, related_global_id=related, relation_class=rel.is_a(),
            relation_global_id=_guid(rel), relation_role=role, relation_name=getattr(rel, "Name", None),
            relation_removed=removed)

    def _detach(self, row, entity, rel):
        model = self.model
        for field in ("RelatedObjects", "RelatedElements"):
            values = getattr(rel, field, None)
            if isinstance(values, tuple) and entity in values:
                rest = tuple(x for x in values if x != entity)
                related = next((_guid(getattr(rel, name, None)) for name in (
                    "RelatingObject", "RelatingStructure", "RelatingGroup", "RelatingType")
                    if _guid(getattr(rel, name, None))), None)
                self._relation_change(row, entity, rel, field, not rest, related)
                dependency = getattr(rel, "RelatingPropertyDefinition", None)
                material = getattr(rel, "RelatingMaterial", None)
                if rest: setattr(rel, field, rest)
                else: model.remove(rel)
                if dependency is not None: _deep_cleanup(model, dependency)
                if material is not None and material.is_a("IfcMaterialUsageDefinition"):
                    _deep_cleanup(model, material)
                return
        # Optional realizing references do not authorize removing a surviving connection.
        if rel.is_a("IfcRelConnectsPorts") and rel.RealizingElement == entity and entity not in (rel.RelatingPort, rel.RelatedPort):
            self._relation_change(row, entity, rel, "RealizingElement", False)
            rel.RealizingElement = None
            return
        if rel.is_a("IfcRelConnectsWithRealizingElements") and entity in rel.RealizingElements and entity not in (rel.RelatingElement, rel.RelatedElement):
            rest = tuple(x for x in rel.RealizingElements if x != entity)
            self._relation_change(row, entity, rel, "RealizingElements", False)
            # RealizingElements is mandatory/nonempty; retain the ordinary endpoint connection.
            if rest: rel.RealizingElements = rest
            else:
                import ifcopenshell.util.schema
                ifcopenshell.util.schema.reassign_class(model, rel, "IfcRelConnectsElements")
            return
        others = [x for x in _product_references(rel) if x != entity and x.GlobalId not in self.deleted]
        related = _guid(others[0]) if others else None
        role = next((rel.attribute_name(i) for i, value in enumerate(rel) if value == entity), "endpoint")
        self._relation_change(row, entity, rel, role, True, related)
        model.remove(rel)

    def _remove_representation(self, row, entity, *, retain_layers=False):
        started = time.monotonic()
        phase = timed_phase("ifc_edit_first_representation_remove") if self._remove_calls == 0 else nullcontext()
        self._remove_calls += 1
        try:
            with phase:
                self._remove_representation_current(row, entity, retain_layers=retain_layers)
        finally:
            self._remove_seconds += time.monotonic() - started

    def _remove_representation_current(self, row, entity, *, retain_layers=False):
        pds = entity.Representation
        if pds is None: return
        roots = tuple(pds.Representations or ())
        layers = []
        graph = tuple(self.model.traverse(pds))
        graph_ids = {node.id() for node in graph}
        internal_references = Counter()
        for node in graph:
            internal_references.update(_reference_occurrences(node))
        root_references = _reference_occurrences(entity)
        presentation = {}
        externally_used = set()
        for node in graph:
            identifier = node.id()
            cached = self._presentation_candidates
            candidates = cached.get(identifier) if cached is not None else None
            inverses = None
            if candidates is None:
                inverses = tuple(self.model.get_inverse(node))
                candidates = tuple(inverse.id() for inverse in inverses
                    if inverse.is_a("IfcStyledItem") or inverse.is_a("IfcPresentationLayerAssignment"))
                if cached is not None: cached[identifier] = candidates
            live = []
            for candidate in candidates:
                try: inverse = self.model.by_id(candidate)
                except RuntimeError: continue
                if ((inverse.is_a("IfcStyledItem") and inverse.Item == node)
                        or (inverse.is_a("IfcPresentationLayerAssignment") and node in inverse.AssignedItems)):
                    live.append(inverse)
            presentation[identifier] = tuple(inverse.id() for inverse in live)
            # Current occurrence counts give only a sufficient external-use
            # proof. Allowed references may be counted twice, never omitted.
            # Equality or uncertainty always takes the original inverse path.
            allowed = internal_references[identifier] + root_references[identifier]
            allowed += sum(_reference_occurrences(inverse)[identifier] for inverse in live)
            external = self.model.get_total_inverses(node) > allowed
            if not external:
                if inverses is None: inverses = tuple(self.model.get_inverse(node))
                external = any(other.id() not in graph_ids and other != entity
                    and not (other.is_a("IfcStyledItem") or other.is_a("IfcPresentationLayerAssignment"))
                    for other in inverses)
            if external:
                externally_used.update(child.id() for child in self.model.traverse(node))
        for identifier in presentation:
            try: node = self.model.by_id(identifier)
            except RuntimeError: continue
            for candidate in presentation[identifier]:
                try: inverse = self.model.by_id(candidate)
                except RuntimeError: continue
                if inverse.is_a("IfcStyledItem") and inverse.Item != node: continue
                if inverse.is_a("IfcPresentationLayerAssignment") and node not in inverse.AssignedItems: continue
                if inverse.is_a("IfcStyledItem") and not retain_layers:
                    if node.id() not in externally_used:
                        self.model.remove(inverse)
                    continue
                if inverse.is_a("IfcPresentationLayerAssignment"):
                    if retain_layers and node in roots:
                        layers.append(inverse)
                    # Shared representation users keep the original assignment.
                    product_users = set()
                    for definition in getattr(node, "OfProductRepresentation", ()) or ():
                        product_users.update(x.id() for x in definition.ShapeOfProduct or ())
                    if product_users - {entity.id()} or node.id() in externally_used:
                        continue
                    rest = tuple(x for x in inverse.AssignedItems if x != node)
                    self._relation_change(row, entity, inverse, "AssignedItems", not rest)
                    if rest: inverse.AssignedItems = rest
                    elif not retain_layers: self.model.remove(inverse)
                    else:
                        # Keep it valid until the new root Body is available.
                        continue
        if layers: self._layers[row.input_row_index] = tuple(set(layers))
        entity.Representation = None
        # Retained layers still refer to the old root Body until consume rebinds it.
        if not retain_layers or not layers: _deep_cleanup(self.model, pds)
        else: self._layers[row.input_row_index] = (tuple(set(layers)), pds, roots)

    def _delete(self, row, entity):
        model = self.model
        self._remove_representation(row, entity)
        placement = entity.ObjectPlacement
        for identifier in [r.id() for r in model.get_inverse(entity)]:
            try: rel = model.by_id(identifier)
            except RuntimeError: continue
            self._detach(row, entity, rel)
        self._change(row, "deleted", entity)
        self.deleted.add(entity.GlobalId)
        entity.ObjectPlacement = None
        model.remove(entity)
        _deep_cleanup(model, placement)

    def _prepare_replace(self, row, owned, new_class):
        model, root = self.model, owned[0]
        for entity in reversed(owned[1:]): self._delete(row, entity)
        for rel in tuple(model.get_inverse(root)):
            if rel.is_a("IfcRelDefinesByType"):
                self._detach(row, root, rel)
                self._warning(row, "IFC_EDIT_TYPE_DETACHED", "Previous IFC Type association was removed; inherited Type properties/material are no longer effective.")
            elif rel.is_a("IfcRelDefinesByProperties"):
                definition = rel.RelatingPropertyDefinition
                if definition.is_a("IfcElementQuantity"):
                    self._detach(row, root, rel)
                else:
                    self._warning(row, "IFC_EDIT_PSET_NOT_RECALCULATED", "Ordinary instance Pset values were preserved and were not recalculated for replacement geometry.")
        self._remove_representation(row, root, retain_layers=True)
        old_placement = root.ObjectPlacement
        if old_placement is not None and (old_placement.ReferencedByPlacements or len(old_placement.PlacesObject or ()) > 1):
            # The compiler can change the root world frame. Keep external users
            # on the unchanged old anchor instead of letting the library rewire them.
            root.ObjectPlacement = model.create_entity("IfcLocalPlacement", PlacementRelTo=old_placement.PlacementRelTo,
                RelativePlacement=old_placement.RelativePlacement)
        if root.is_a() != new_class:
            attributes = {name: getattr(root, name) for name in ("GlobalId", "OwnerHistory", "Name", "Description", "ObjectPlacement")}
            original_guid = attributes["GlobalId"]
            # Never register two live entities under one GUID: removing the old
            # entity would also evict the replacement from IfcOpenShell's index.
            attributes["GlobalId"] = ifcopenshell.guid.new()
            replacement = model.create_entity(new_class, **attributes)
            if hasattr(replacement, "Tag"): replacement.Tag = getattr(root, "Tag", None)
            for inverse in tuple(model.get_inverse(root)):
                ifcopenshell.util.element.replace_attribute(inverse, root, replacement)
            model.remove(root)
            replacement.GlobalId = original_guid
            root = replacement
        self._root_targets[row.product_key] = root

    def _move(self, row, owned):
        model = self.model
        unit = ifcopenshell.util.unit.calculate_unit_scale(model)
        if not np.isfinite(unit) or unit <= 0: raise ValueError("invalid project length unit")
        before = {x.GlobalId: _world(x) for x in owned}
        target = np.eye(4); target[:3,:3] = row.placement.rotation
        target[:3,3] = np.asarray(row.placement.translation_m) / unit
        delta = target @ np.linalg.inv(before[owned[0].GlobalId])
        old_nodes = {x.ObjectPlacement.id(): x.ObjectPlacement for x in owned}
        external_nodes = {}
        for old in old_nodes.values():
            for node in old.ReferencedByPlacements or ():
                if node.id() not in old_nodes:
                    if not node.is_a("IfcLocalPlacement"): raise ValueError("external placement dependant is not local")
                    external_nodes[node.id()] = (node, validate_rigid_matrix(ifcopenshell.util.placement.get_local_placement(node)).copy())
        external_products = {x.GlobalId: _world(x) for old in old_nodes.values()
            for x in old.PlacesObject or () if x.GlobalId not in self.selected_global_ids}
        for node, _ in external_nodes.values():
            external_products.update({x.GlobalId: _world(x) for x in node.PlacesObject or () if x.GlobalId not in self.selected_global_ids})
        replacements, pending = {}, list(owned)
        while pending:
            progressed = False
            for entity in tuple(pending):
                old = entity.ObjectPlacement; parent = old.PlacementRelTo
                if parent is not None and parent.id() in old_nodes and parent.id() not in replacements: continue
                new_parent = replacements.get(parent.id(), parent) if parent else None
                parent_world = ifcopenshell.util.placement.get_local_placement(new_parent)
                desired = delta @ before[entity.GlobalId]
                new = model.create_entity("IfcLocalPlacement", PlacementRelTo=new_parent,
                    RelativePlacement=_axis(model, np.linalg.inv(parent_world) @ desired))
                entity.ObjectPlacement = new
                replacements.setdefault(old.id(), new)
                self.expected_worlds[entity.GlobalId] = desired
                if not matrices_close(before[entity.GlobalId], desired):
                    self._change(row, "placement_changed", entity,
                        before_placement_json=_frame_json(before[entity.GlobalId], unit), after_placement_json=_frame_json(desired, unit))
                pending.remove(entity); progressed = True
            if not progressed: raise ValueError("owned local-placement chain is cyclic")
        for node, world in external_nodes.values():
            parent = node.PlacementRelTo
            if parent is not None and parent.id() in replacements:
                new_parent = replacements[parent.id()]
                node.RelativePlacement = _axis(model, np.linalg.inv(ifcopenshell.util.placement.get_local_placement(new_parent)) @ world)
                node.PlacementRelTo = new_parent
        self.expected_worlds.update(external_products)
        for identifier in old_nodes:
            try: old = model.by_id(identifier)
            except RuntimeError: continue
            _deep_cleanup(model, old)

    def consume(self, block):
        for item in block.items:
            row = self.rows.get(item.input_row_index)
            if row is None or row.action != "replace" or row.product_key != item.product_key or item.input_row_index in self.received:
                raise IfcProductCompilationError("build stream contains an extra, duplicate or mismatched replacement row", input_row_index=item.input_row_index)
            compiler = self.compilers.get(row.family_version_id)
            if compiler is None:
                compiler = IfcOpenShellProductCompiler(self.model,
                    CompileIfcProductsCommand(family_contract=self.command.families[row.family_version_id]),
                    target_roots=self._root_targets, source_maps=self.maps)
                self.compilers[row.family_version_id] = compiler
            compiler.consume(IfcProductCompilationBlock(items=(item,)))
            new_bindings = [b for b in compiler.effect().bindings if b.input_row_index == item.input_row_index]
            self.bindings.extend(new_bindings)
            root = self._root_targets[row.product_key]
            for binding in new_bindings:
                entity = self.model.by_guid(binding.global_id)
                self._change(row, "geometry_replaced" if binding.global_id == row.target_global_id else "created", entity,
                    component_key=binding.component_key, parent_component_key=binding.parent_component_key)
            layer_data = self._layers.get(row.input_row_index)
            if layer_data:
                layers, pds, roots = layer_data
                new_representations = tuple(root.Representation.Representations)
                for layer in layers:
                    old_kept = tuple(x for x in layer.AssignedItems if x not in roots or any(
                        definition.ShapeOfProduct for definition in getattr(x, "OfProductRepresentation", ()) or ()))
                    layer.AssignedItems = tuple(dict.fromkeys((*old_kept, *new_representations)))
                    self._relation_change(row, root, layer, "AssignedItems", False, kind="relation_rebound")
                _deep_cleanup(self.model, pds)
            self.products.append(IfcProductEditExecutionProduct(input_row_index=row.input_row_index,
                target_global_id=row.target_global_id, before_ifc_class=self.facts[row.input_row_index].before_ifc_class,
                after_ifc_class=root.is_a(), result_global_id=root.GlobalId))
            self.received.add(item.input_row_index)

    def check_complete(self):
        expected = {i for i, row in self.rows.items() if row.action == "replace"}
        if self.received != expected:
            raise IfcProductCompilationError("replacement build stream omitted required rows", phase="compile")

    def effect(self, *, timings=None):
        self.check_complete()
        return IfcProductsEditRevisionEffect(products=tuple(sorted(self.products, key=lambda p:p.input_row_index)),
            changes=tuple(self.changes), checks=tuple(self.checks), bindings=tuple(self.bindings),
            timings={"remove_representation": self._remove_seconds, **(timings or {})})

    def verify_saved(self, model):
        self.check_complete()
        for identifier, before in self.protected.items():
            if str(model.by_id(identifier)) != before:
                raise ValueError("saved edit changed a protected shared definition")
        for compiler in self.compilers.values(): compiler.verify_saved(model)
        for guid in self.deleted:
            try: model.by_guid(guid)
            except RuntimeError: continue
            raise ValueError("saved edit retained an exited product identity")
        for product in self.products:
            if product.result_global_id:
                actual = model.by_guid(product.result_global_id)
                if actual.is_a() != product.after_ifc_class: raise ValueError("saved edit changed target class")
        for guid, expected in self.expected_worlds.items():
            if not matrices_close(_world(model.by_guid(guid)), expected):
                raise ValueError("saved edit moved an owned or protected external product incorrectly")
