"""Bounded facts from one already parsed, private IFC snapshot.

This module does not resolve durable identities, persist results, or traverse
shared resources beyond the explicitly selected direct relationships.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Callable, Iterable
from itertools import chain
from typing import Any

import ifcopenshell.util.unit

from CAD2BIM_backend.infrastructure.ifc.placement import project_world_placement, placement_matrix
from CAD2BIM_backend.domain.ifc_spatial_structure import IfcSpatialStructureIntegrityError
from CAD2BIM_backend.domain.ifc_products_read import (
    IfcProductsReadError, IfcProductsReadRuntimeRequest, IfcProductsReadRuntimeResult,
    MAX_READ_RESPONSE_BYTES, MAX_READ_COLLECTION,
)


class _ReadBudget:
    """Call-local lower bound; final typed serialization checks the exact size."""

    def __init__(self, check_budget: Callable[[], None] | None):
        self.check_budget = check_budget or (lambda: None)
        self.completed_bytes = 0
        self.partial_bytes = 0

    def add(self, value: Any):
        self.check_budget()
        self.partial_bytes += len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        self._check()
        return value

    def complete(self, value: Any):
        self.check_budget()
        self.completed_bytes += len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        self.partial_bytes = 0
        self._check()

    def _check(self):
        if self.completed_bytes + self.partial_bytes > MAX_READ_RESPONSE_BYTES:
            raise IfcProductsReadError("IFC_PRODUCTS_READ_LIMIT_EXCEEDED",
                f"Selected product facts exceed {MAX_READ_RESPONSE_BYTES} bytes; no partial result is returned; select fewer products or groups")


def _text(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    value = str(value)
    return {"value": value[:2048], "truncated": len(value) > 2048, "original_length": len(value)}


def _entity(value: Any) -> dict[str, Any]:
    return {"global_id": getattr(value, "GlobalId", None), "ifc_class": value.is_a(),
            "name": _text(getattr(value, "Name", None))}


def _local(value: Any) -> dict[str, Any]:
    return {"step_id": value.id(), "ifc_class": value.is_a()}


def _bounded(values: Any, group: str) -> tuple[Any, ...]:
    result = []
    for value in values:
        result.append(value)
        if len(result) > MAX_READ_COLLECTION:
            raise IfcProductsReadError("IFC_PRODUCTS_READ_LIMIT_EXCEEDED",
                f"{group} contains at least {len(result)} entries; limit is {MAX_READ_COLLECTION}; "
                "no partial result is returned; use IFC Query for the complete collection")
    return tuple(result)


def _relations(entity: Any, attribute: str, kind: str) -> Iterable[Any]:
    return (r for r in (getattr(entity, attribute, ()) or ()) if r.is_a(kind))


def _types(product: Any) -> Iterable[Any]:
    return (r.RelatingType for r in _relations(product, "IsTypedBy", "IfcRelDefinesByType"))


def _placement(product: Any, unit_scale: float | None) -> dict[str, Any]:
    placement = getattr(product, "ObjectPlacement", None)
    if placement is None:
        return {"status": "empty", "diagnostic": _text("ObjectPlacement is not assigned")}
    current, seen = placement, set()
    while current is not None:
        if current.id() in seen:
            return {"status": "unsupported", "diagnostic": _text("ObjectPlacement chain is cyclic")}
        seen.add(current.id())
        if not current.is_a("IfcLocalPlacement"):
            return {"status": "unsupported", "diagnostic": _text(f"{current.is_a()} is not supported in the placement chain")}
        current = current.PlacementRelTo
    if unit_scale is None:
        return {"status": "unsupported", "diagnostic": _text("Project length unit is missing or ambiguous")}
    try:
        matrix = placement_matrix(project_world_placement(product))
    except IfcSpatialStructureIntegrityError:
        return {"status": "unsupported", "diagnostic": _text("ObjectPlacement cannot be resolved as a finite rigid frame")}
    return {"status": "found", "translation_m": tuple(float(x) * unit_scale for x in matrix[:3, 3]),
            "rotation": tuple(tuple(float(x) for x in row) for row in matrix[:3, :3])}


def _unit_scale(model: Any) -> float | None:
    projects = model.by_type("IfcProject")
    if len(projects) != 1 or projects[0].UnitsInContext is None:
        return None
    units = [u for u in projects[0].UnitsInContext.Units if getattr(u, "UnitType", None) == "LENGTHUNIT"]
    if len(units) != 1:
        return None
    unit, seen = units[0], set()
    while unit.is_a("IfcConversionBasedUnit"):
        if unit.id() in seen or unit.ConversionFactor is None:
            return None
        seen.add(unit.id())
        unit = unit.ConversionFactor.UnitComponent
    if not unit.is_a("IfcSIUnit") or unit.Name != "METRE":
        return None
    try:
        scale = float(ifcopenshell.util.unit.calculate_unit_scale(model))
    except (RuntimeError, TypeError, ValueError):
        return None
    return scale if math.isfinite(scale) and scale > 0 else None


def _aggregate_parent(product: Any) -> dict[str, Any]:
    relations = _bounded(_relations(product, "Decomposes", "IfcRelAggregates"), "aggregate parents")
    parents = tuple(_entity(r.RelatingObject) for r in relations)
    return {"status": "empty" if not parents else "found" if len(parents) == 1 else "ambiguous",
            "parents": parents, "relationship_guids": tuple(r.GlobalId for r in relations)}


def _child_count(product: Any) -> int:
    return sum(len(r.RelatedObjects or ()) for r in _relations(product, "IsDecomposedBy", "IfcRelAggregates"))


def _structure(model: Any, product: Any, _selection: Any, _scale: float | None, budget: _ReadBudget) -> dict[str, Any]:
    def direct(parent_attr: str, child_attr: str, kind: str):
        parents = ({"relationship": _entity(r), "related": _entity(r.RelatingObject), "direction": "parent"}
                   for r in _relations(product, parent_attr, kind))
        children = ({"relationship": _entity(r), "related": _entity(child), "direction": "child"}
                       for r in _relations(product, child_attr, kind) for child in (r.RelatedObjects or ()))
        return _bounded((budget.add(value) for value in chain(parents, children)), kind)

    aggregation = direct("Decomposes", "IsDecomposedBy", "IfcRelAggregates")
    nesting = direct("Nests", "IsNestedBy", "IfcRelNests")
    dependencies = []
    placement = getattr(product, "ObjectPlacement", None)
    if placement is not None:
        owners = _bounded((obj for obj in model.get_inverse(placement)
            if obj.is_a("IfcProduct") and obj.ObjectPlacement == placement), "shared ObjectPlacement owners")
        dependencies.append({"placement": _local(placement),
            "relative_to": _local(placement.PlacementRelTo) if getattr(placement, "PlacementRelTo", None) else None,
            "owners": tuple(_entity(owner) for owner in owners), "owner_count": len(owners),
            "dependents": tuple(_local(p) for p in _bounded(getattr(placement, "ReferencedByPlacements", ()) or (), "placement dependents"))})
        budget.add(dependencies[-1])
    references = Counter(r.is_a() for r in model.get_inverse(product))
    return {"status": "found" if aggregation or nesting or dependencies or references else "empty",
            "aggregation": aggregation, "nesting": nesting, "placement_dependencies": dependencies,
            "references": tuple({"ifc_class": kind, "count": count} for kind, count in sorted(references.items()))}


def _unit(unit: Any) -> dict[str, Any] | None:
    if unit is None:
        return None
    name = getattr(unit, "Name", None)
    return {"ifc_class": unit.is_a(), "name": _text(name or getattr(unit, "UserDefinedType", None)
                or getattr(unit, "UnitType", None) or getattr(unit, "Currency", None)),
            "symbol": _text(ifcopenshell.util.unit.get_unit_symbol(unit)) if isinstance(name, str) else None}


def _property_value(model: Any, prop: Any) -> dict[str, Any]:
    kind = prop.is_a()
    if prop.is_a("IfcPropertySingleValue"):
        raw = prop.NominalValue
        if raw is None:
            return {"status": "empty"}
        values, scalar = (raw,), True
    elif prop.is_a("IfcPropertyEnumeratedValue"):
        values, scalar = tuple(prop.EnumerationValues or ()), False
    elif prop.is_a("IfcPropertyListValue"):
        values, scalar = tuple(prop.ListValues or ()), False
    elif prop.is_a("IfcPhysicalSimpleQuantity"):
        values, scalar = (prop[3],), True
    else:
        return {"status": "unsupported", "value_type": kind,
                "diagnostic": _text("This property structure is not a scalar, list, enumeration or simple quantity; use IFC Query")}
    if not values:
        return {"status": "empty", "value_type": kind}
    _bounded(values, "property values")
    unwrapped = tuple(getattr(value, "wrappedValue", value) for value in values)
    if any(type(value) not in (bool, int, float, str) for value in unwrapped):
        return {"status": "unsupported", "value_type": kind,
                "diagnostic": _text("The property contains a non-scalar value")}
    original_length = max((len(v) for v in unwrapped if isinstance(v, str)), default=0)
    unwrapped = tuple(v[:2048] if isinstance(v, str) else v for v in unwrapped)
    result = {"status": "found", "value_type": values[0].is_a() if hasattr(values[0], "is_a") else kind,
              "unit": _unit(ifcopenshell.util.unit.get_property_unit(prop, model)),
              "truncated": original_length > 2048, "original_length": original_length if original_length else None}
    result["scalar" if scalar else "values"] = unwrapped[0] if scalar else unwrapped
    return result


def _property_sets(product: Any, sources: set[str]):
    if "instance" in sources:
        for rel in _relations(product, "IsDefinedBy", "IfcRelDefinesByProperties"):
            definition = rel.RelatingPropertyDefinition
            for pset in definition if isinstance(definition, tuple) else (definition,):
                yield "instance", None, pset, (rel.GlobalId,)
    if "type" in sources:
        for typ in _types(product):
            for pset in typ.HasPropertySets or ():
                yield "type", typ, pset, ()


def _properties(model: Any, product: Any, selection: Any, _scale: float | None, budget: _ReadBudget) -> dict[str, Any]:
    sets, unsupported, matched = [], False, set()
    sources = set(selection.sources) if selection.mode == "names" else {s.source for s in selection.selectors}
    for source, typ, pset, relations in _property_sets(product, sources):
        budget.check_budget()
        kind = "pset" if pset.is_a("IfcPropertySet") else "qto" if pset.is_a("IfcElementQuantity") else None
        if kind is None:
            unsupported = True
            continue
        selectors = () if selection.mode == "names" else tuple(s for s in selection.selectors
            if s.source == source and s.set_kind == kind and s.set_name == pset.Name)
        if selection.mode == "names":
            if source not in selection.sources or kind not in selection.set_kinds:
                continue
        elif not selectors:
            continue
        props = pset.HasProperties if kind == "pset" else pset.Quantities
        properties = []
        names = {name for s in selectors for name in s.property_names}
        def selected_properties():
            for p in props or ():
                budget.check_budget()
                if selection.mode == "names" or p.Name in names:
                    yield p
        selected_props = selected_properties()
        for prop in _bounded(selected_props, "selected properties in set"):
            matched.add((source, kind, pset.Name, prop.Name))
            properties.append({"object": _local(prop), "name": _text(prop.Name), "selector_name": prop.Name or None,
                "value": _property_value(model, prop) if selection.mode == "values" else None})
            budget.add(properties[-1])
        sets.append({"object": _local(pset), "global_id": getattr(pset, "GlobalId", None), "source": source,
            "source_type": _entity(typ) if typ else None, "set_kind": kind, "name": _text(pset.Name),
            "selector_name": pset.Name or None,
            "relationship_guids": relations, "properties": properties})
        budget.add({k: v for k, v in sets[-1].items() if k != "properties"})
        _bounded(sets, "selected property sets")
    missing = () if selection.mode == "names" else tuple((s.source, s.set_kind, s.set_name, name)
        for s in selection.selectors for name in s.property_names if (s.source, s.set_kind, s.set_name, name) not in matched)
    types = _bounded(_relations(product, "IsTypedBy", "IfcRelDefinesByType"), "product type relationships") if "type" in sources else ()
    counts = Counter((item["source"], item["set_kind"], item["selector_name"]) for item in sets)
    ambiguous = any(n > 1 for n in counts.values()) or any(
        len({p["selector_name"] for p in s["properties"]}) < len(s["properties"]) for s in sets)
    return {"status": "ambiguous" if ambiguous else "found" if sets else "unsupported" if unsupported else "empty",
        "diagnostic": _text(f"{len(missing)} requested properties were not found") if missing else None,
        "description": _text(getattr(product, "Description", None)), "tag": _text(getattr(product, "Tag", None)),
        "object_type": _text(getattr(product, "ObjectType", None)), "predefined_type": _text(getattr(product, "PredefinedType", None)),
        "types": tuple(_entity(r.RelatingType) for r in types), "type_relationship_guids": tuple(r.GlobalId for r in types),
        "sets": sets}


def _material_members(material: Any):
    kind = material.is_a()
    if kind == "IfcMaterial":
        return "single", (material,)
    if kind == "IfcMaterialProfileSetUsageTapering":
        return "unsupported", ()
    if kind in {"IfcMaterialLayerSetUsage", "IfcMaterialProfileSetUsage"}:
        material = material.ForLayerSet if kind == "IfcMaterialLayerSetUsage" else material.ForProfileSet
        members = material.MaterialLayers if material.is_a("IfcMaterialLayerSet") else material.MaterialProfiles
        return "layer_set_usage" if kind == "IfcMaterialLayerSetUsage" else "profile_set_usage", tuple(members or ())
    fields = {"IfcMaterialLayerSet": ("layer_set", "MaterialLayers"),
              "IfcMaterialProfileSet": ("profile_set", "MaterialProfiles"),
              "IfcMaterialConstituentSet": ("constituent_set", "MaterialConstituents"),
              "IfcMaterialList": ("list", "Materials")}
    if kind not in fields:
        return "unsupported", ()
    label, field = fields[kind]
    return label, tuple(getattr(material, field) or ())


def _materials(model: Any, product: Any, selection: Any, scale: float | None, budget: _ReadBudget) -> dict[str, Any]:
    assignments, unsupported, matched, selected_names = [], False, set(), Counter()
    requested_sources = set(selection.sources) if selection.mode == "names" else {s.source for s in selection.selectors}
    sources = [("instance", None, product)]
    if "type" in requested_sources:
        sources.extend(("type", typ, typ) for typ in _bounded(_types(product), "material types"))
    for source, typ, obj in sources:
        if source not in requested_sources:
            continue
        for rel in _relations(obj, "HasAssociations", "IfcRelAssociatesMaterial"):
            budget.check_budget()
            mat = rel.RelatingMaterial
            kind, member_objs = _material_members(mat)
            named_mat = mat.ForLayerSet if kind == "layer_set_usage" else mat.ForProfileSet if kind == "profile_set_usage" else mat
            set_name = getattr(named_mat, "Name", None) or getattr(named_mat, "LayerSetName", None) or getattr(named_mat, "ProfileSetName", None)
            selectors = () if selection.mode == "names" else tuple(s for s in selection.selectors if s.source == source and s.set_name == set_name)
            if selection.mode == "values" and not selectors:
                continue
            unsupported |= kind == "unsupported"
            selected_names[(source, set_name)] += 1
            if not set_name:
                unsupported = True
            members = []
            member_names = {name for s in selectors for name in s.member_names}
            name_counts = Counter()
            for member in member_objs:
                budget.check_budget()
                actual_name = getattr(member, "Name", None) or (getattr(member.Material, "Name", None) if getattr(member, "Material", None) else None)
                if selection.mode == "values" and actual_name not in member_names:
                    continue
                if not actual_name:
                    unsupported = True
                matched.add((source, set_name, actual_name))
                name_counts[actual_name] += 1
                entry = {"object": _local(member), "name": _text(actual_name), "selector_name": actual_name or None,
                         "material": _local(member if member.is_a("IfcMaterial") else member.Material) if (member.is_a("IfcMaterial") or getattr(member, "Material", None)) else None}
                underlying_material = member if member.is_a("IfcMaterial") else getattr(member, "Material", None)
                material_name = getattr(underlying_material, "Name", None)
                entry["material_name"] = _text(material_name) if material_name != actual_name else None
                if selection.mode == "values":
                    entry.update(category=_text(getattr(member, "Category", None)), fraction=getattr(member, "Fraction", None))
                    thickness = getattr(member, "LayerThickness", None)
                    if thickness is not None and scale is None:
                        unsupported = True
                    entry["thickness_m"] = float(thickness) * scale if thickness is not None and scale is not None else None
                    profile = getattr(member, "Profile", None)
                    entry["profile"] = _local(profile) if profile else None
                    entry["unit"] = _unit(ifcopenshell.util.unit.get_project_unit(model, "LENGTHUNIT")) if thickness is not None else None
                members.append(entry)
                budget.add(entry)
                _bounded(members, "selected material members")
            if any(count > 1 for count in name_counts.values()):
                selected_names[(source, set_name)] += 1
            offset = getattr(mat, "OffsetFromReferenceLine", None)
            if offset is not None and scale is None and selection.mode == "values":
                unsupported = True
            assignments.append({"relationship": _entity(rel), "source": source, "source_type": _entity(typ) if typ else None,
                "material": _local(mat), "name": _text(set_name), "selector_name": set_name or None, "kind": kind, "members": members,
                "offset_m": float(offset) * scale if offset is not None and scale is not None and selection.mode == "values" else None,
                "direction_sense": _text(getattr(mat, "DirectionSense", None)) if selection.mode == "values" else None})
            budget.add({k: v for k, v in assignments[-1].items() if k != "members"})
            _bounded(assignments, "selected material assignments")
    missing = {(s.source, s.set_name, name) for s in selection.selectors for name in s.member_names} - matched if selection.mode == "values" else set()
    ambiguous = any(count > 1 for count in selected_names.values())
    return {"status": "ambiguous" if ambiguous else "unsupported" if unsupported or missing else "found" if assignments else "empty", "assignments": assignments,
            "diagnostic": _text(f"{len(missing)} requested material members were not found in this product's selected assignments") if missing else None}


def _spatial(model: Any, product: Any, _selection: Any, _scale: float | None, budget: _ReadBudget) -> dict[str, Any]:
    assignments, ambiguous = [], False
    for attr, kind, label in (("ContainedInStructure", "IfcRelContainedInSpatialStructure", "containment"),
                              ("ReferencedInStructures", "IfcRelReferencedInSpatialStructure", "reference")):
        rels = _bounded(_relations(product, attr, kind), "spatial assignments")
        if label == "containment" and len(rels) > 1:
            ambiguous = True
        for rel in rels:
            container = rel.RelatingStructure
            hierarchy, seen, frontier = [], set(), [container]
            while frontier:
                budget.check_budget()
                node = frontier.pop()
                if node.id() in seen:
                    ambiguous = True
                    continue
                seen.add(node.id())
                parents = _bounded(_relations(node, "Decomposes", "IfcRelAggregates"), "spatial parents")
                if len(parents) > 1:
                    ambiguous = True
                for parent in parents:
                    ancestor = parent.RelatingObject
                    hierarchy.append(_entity(ancestor))
                    budget.add(hierarchy[-1])
                    _bounded(hierarchy, "spatial hierarchy")
                    frontier.append(ancestor)
            assignments.append({"relationship": _entity(rel), "kind": label,
                                "container": _entity(container), "hierarchy": hierarchy})
    _bounded(assignments, "spatial assignments")
    return {"status": "ambiguous" if ambiguous else "found" if assignments else "empty", "assignments": assignments}


def _systems(model: Any, product: Any, _selection: Any, _scale: float | None, budget: _ReadBudget) -> dict[str, Any]:
    assignments = []
    for rel in _bounded(_relations(product, "HasAssignments", "IfcRelAssignsToGroup"), "system/group assignments"):
        group = rel.RelatingGroup
        services = ({"relationship": _entity(service), "related": _entity(building), "direction": "reference"}
            for service in (getattr(group, "ServicesBuildings", ()) or ()) for building in (service.RelatedBuildings or ()))
        assignments.append({"relationship": _entity(rel), "group": _entity(group),
                            "service_relationships": _bounded(services, "system service relationships")})
        budget.add(assignments[-1])
    return {"status": "found" if assignments else "empty", "assignments": assignments}


def _port_owners(port: Any):
    owners = [(r.RelatingObject, r) for r in _relations(port, "Nests", "IfcRelNests")]
    owners.extend((r.RelatedElement, r) for r in _relations(port, "ContainedIn", "IfcRelConnectsPortToElement"))
    return _bounded(owners, "port ownership")


def _ports(model: Any, product: Any, selection: Any, scale: float | None, budget: _ReadBudget) -> dict[str, Any]:
    ports_by_id = {}
    for rel in _relations(product, "IsNestedBy", "IfcRelNests"):
        for port in rel.RelatedObjects or ():
            if port.is_a("IfcPort"):
                ports_by_id[port.id()] = port
    for rel in _relations(product, "HasPorts", "IfcRelConnectsPortToElement"):
        ports_by_id[rel.RelatingPort.id()] = rel.RelatingPort
    ports, ambiguous, matched = [], False, set()
    selected_ports = (p for p in ports_by_id.values() if selection.mode == "names" or p.GlobalId in selection.global_ids)
    for port in _bounded(selected_ports, "selected owned ports"):
        matched.add(port.GlobalId)
        ownership = _port_owners(port)
        ambiguous |= len({obj.id() for obj, _ in ownership}) > 1
        entry = {"port": _entity(port), "owners": tuple(_entity(obj) for obj, _ in ownership),
            "ownership_relationship_guids": tuple(r.GlobalId for _, r in ownership)}
        if selection.mode == "values":
            connections = []
            for rel in _bounded(chain(_relations(port, "ConnectedTo", "IfcRelConnectsPorts"),
                                     _relations(port, "ConnectedFrom", "IfcRelConnectsPorts")), "port connections"):
                peer = rel.RelatedPort if rel.RelatingPort == port else rel.RelatingPort
                connections.append({"relationship": _entity(rel), "peer_port": _entity(peer),
                    "peer_owners": tuple(_entity(obj) for obj, _ in _port_owners(peer)),
                    "realizing_element": _entity(rel.RealizingElement) if rel.RealizingElement else None})
                budget.add(connections[-1])
            entry.update(placement=_placement(port, scale), flow_direction=_text(getattr(port, "FlowDirection", None)),
                predefined_type=_text(getattr(port, "PredefinedType", None)), system_type=_text(getattr(port, "SystemType", None)),
                connections=connections)
        ports.append(entry)
        budget.add({k: v for k, v in entry.items() if k != "connections"})
    missing = set(selection.global_ids) - matched if selection.mode == "values" else set()
    duplicate_guids = len({p["port"]["global_id"] for p in ports}) != len(ports)
    return {"status": "ambiguous" if ambiguous or duplicate_guids else "found" if ports else "empty", "ports": ports,
            "diagnostic": _text(f"{len(missing)} requested ports were not found among this product's directly owned ports") if missing else None}


def _representations(model: Any, product: Any, _selection: Any, _scale: float | None, budget: _ReadBudget) -> dict[str, Any]:
    root = getattr(product, "Representation", None)
    entries = []
    for rep in _bounded((root.Representations or ()) if root else (), "representations"):
        context = rep.ContextOfItems
        entries.append({"object": _local(rep), "identifier": _text(rep.RepresentationIdentifier),
            "representation_type": _text(rep.RepresentationType), "context": _local(context) if context else None,
            "context_identifier": _text(context.ContextIdentifier) if context else None,
            "context_type": _text(context.ContextType) if context else None,
            "target_view": _text(getattr(context, "TargetView", None)) if context else None,
            "item_count": len(rep.Items or ())})
        budget.add(entries[-1])
    body_count = sum(entry["identifier"] is not None and entry["identifier"]["value"] == "Body" for entry in entries)
    return {"status": "found" if entries else "empty", "has_representation": root is not None,
            "body_status": "empty" if not body_count else "found" if body_count == 1 else "ambiguous", "representations": entries}


def read_products(model: Any, request: IfcProductsReadRuntimeRequest, *, check_budget: Callable[[], None] | None = None) -> IfcProductsReadRuntimeResult:
    """Project exactly the requested products and groups without changing IFC."""
    budget = _ReadBudget(check_budget)
    selected = set(request.global_ids)
    index = {global_id: [] for global_id in selected}
    for entity in model.by_type("IfcRoot"):
        budget.check_budget()
        if entity.GlobalId in selected:
            index[entity.GlobalId].append(entity)
    scale = _unit_scale(model)
    groups = {"structure": _structure, "properties": _properties, "materials": _materials,
              "spatial": _spatial, "systems": _systems, "ports": _ports, "representations": _representations}
    results = []
    for global_id in request.global_ids:
        matches = index[global_id]
        result = {"global_id": global_id, "match_count": len(matches)}
        if len(matches) != 1:
            result["status"] = "not_found" if not matches else "ambiguous"
        elif not matches[0].is_a("IfcProduct"):
            result["status"] = "not_product"
        else:
            product = matches[0]
            result.update(status="found", basic={"ifc_class": product.is_a(), "name": _text(product.Name),
                "placement": _placement(product, scale), "aggregate_parent": _aggregate_parent(product),
                "direct_child_count": _child_count(product)})
            budget.complete(result["basic"])
            for name, extractor in groups.items():
                selection = getattr(request.groups, name)
                if selection is not None:
                    result[name] = extractor(model, product, selection, scale, budget)
                    budget.complete(result[name])
        results.append(result)
    payload = {"products": results}
    actual_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    if actual_bytes > MAX_READ_RESPONSE_BYTES:
        raise IfcProductsReadError("IFC_PRODUCTS_READ_LIMIT_EXCEEDED",
            f"Product facts require {actual_bytes} bytes; limit is {MAX_READ_RESPONSE_BYTES}; no partial result is returned; select fewer products or groups")
    return IfcProductsReadRuntimeResult.model_validate(payload)
