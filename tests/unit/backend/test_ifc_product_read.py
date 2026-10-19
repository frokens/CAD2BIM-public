"""Real, small IFC facts for the bounded products inspection projection."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import time
import sys
from dataclasses import asdict
from pathlib import Path

import ifcopenshell
import ifcopenshell.guid
import pytest

from CAD2BIM_backend.domain.ifc_products_read import IfcProductsReadRuntimeRequest, IfcProductsReadError
from CAD2BIM_backend.infrastructure.ifc.product_read import read_products
from CAD2BIM_backend.ports.ifc_revision_runtime import InspectExactIfcRevisionRequest


def _fixture():
    model = ifcopenshell.file(schema="IFC4X3_ADD2")
    def root(kind, **fields):
        return model.create_entity(kind, GlobalId=ifcopenshell.guid.new(), **fields)
    units = model.create_entity("IfcUnitAssignment", Units=[model.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Prefix="MILLI", Name="METRE")])
    project = root("IfcProject", Name="Products", UnitsInContext=units)
    point = model.create_entity("IfcCartesianPoint", Coordinates=(1000., 2000., 3000.))
    placement = model.create_entity("IfcLocalPlacement", RelativePlacement=model.create_entity("IfcAxis2Placement3D", Location=point))
    assembly = root("IfcElementAssembly", Name="Assembly", ObjectPlacement=placement)
    pipe = root("IfcPipeSegment", Name="Pipe", ObjectPlacement=placement, Description="Selected", Tag="P1")
    other = root("IfcPipeSegment", Name="Other", ObjectPlacement=placement)
    root("IfcRelAggregates", RelatingObject=assembly, RelatedObjects=[pipe])
    root("IfcRelNests", RelatingObject=assembly, RelatedObjects=[other])
    typ = root("IfcPipeSegmentType", Name="Pipe Type", PredefinedType="RIGIDSEGMENT")
    root("IfcRelDefinesByType", RelatedObjects=[pipe], RelatingType=typ)
    prop = model.create_entity("IfcPropertySingleValue", Name="Pressure", NominalValue=model.create_entity("IfcPressureMeasure", 600.))
    pset = root("IfcPropertySet", Name="Pset_Actual", HasProperties=[prop])
    root("IfcRelDefinesByProperties", RelatedObjects=[pipe], RelatingPropertyDefinition=pset)
    type_prop = model.create_entity("IfcPropertySingleValue", Name="Pressure", NominalValue=model.create_entity("IfcPressureMeasure", 700.))
    typ.HasPropertySets = [root("IfcPropertySet", Name="Pset_Actual", HasProperties=[type_prop])]
    quantity = model.create_entity("IfcQuantityLength", Name="Length", LengthValue=2000.)
    qto = root("IfcElementQuantity", Name="Qto_Actual", Quantities=[quantity])
    root("IfcRelDefinesByProperties", RelatedObjects=[pipe], RelatingPropertyDefinition=qto)
    mat = model.create_entity("IfcMaterial", Name="Steel")
    layer = model.create_entity("IfcMaterialLayer", Material=mat, LayerThickness=12., Name="Wall")
    layers = model.create_entity("IfcMaterialLayerSet", MaterialLayers=[layer], LayerSetName="Pipe Wall")
    usage = model.create_entity("IfcMaterialLayerSetUsage", ForLayerSet=layers, LayerSetDirection="AXIS3", DirectionSense="POSITIVE", OffsetFromReferenceLine=4.)
    root("IfcRelAssociatesMaterial", RelatedObjects=[pipe], RelatingMaterial=usage)
    root("IfcRelAssociatesMaterial", RelatedObjects=[typ], RelatingMaterial=mat)
    building = root("IfcBuilding", Name="Building", ObjectPlacement=placement)
    storey = root("IfcBuildingStorey", Name="Storey", ObjectPlacement=placement)
    root("IfcRelAggregates", RelatingObject=building, RelatedObjects=[storey])
    root("IfcRelContainedInSpatialStructure", RelatedElements=[pipe], RelatingStructure=storey)
    root("IfcRelReferencedInSpatialStructure", RelatedElements=[pipe], RelatingStructure=building)
    system = root("IfcDistributionSystem", Name="Gas", PredefinedType="GAS")
    root("IfcRelAssignsToGroup", RelatedObjects=[pipe, other], RelatingGroup=system)
    root("IfcRelServicesBuildings", RelatingSystem=system, RelatedBuildings=[building])
    port = root("IfcDistributionPort", Name="Out", ObjectPlacement=placement, FlowDirection="SOURCE")
    peer = root("IfcDistributionPort", Name="In", ObjectPlacement=placement, FlowDirection="SINK")
    root("IfcRelNests", RelatingObject=pipe, RelatedObjects=[port])
    root("IfcRelNests", RelatingObject=other, RelatedObjects=[peer])
    root("IfcRelConnectsPorts", RelatingPort=port, RelatedPort=peer)
    context = model.create_entity("IfcGeometricRepresentationContext", ContextIdentifier="Model", ContextType="Model", CoordinateSpaceDimension=3, Precision=1e-5,
        WorldCoordinateSystem=model.create_entity("IfcAxis2Placement3D", Location=model.create_entity("IfcCartesianPoint", Coordinates=(0., 0., 0.)),
            Axis=model.create_entity("IfcDirection", DirectionRatios=(0., 0., 1.)),
            RefDirection=model.create_entity("IfcDirection", DirectionRatios=(1., 0., 0.))))
    project.RepresentationContexts = [context]
    rep = model.create_entity("IfcShapeRepresentation", ContextOfItems=context, RepresentationIdentifier="Body", RepresentationType="Point", Items=[point])
    pipe.Representation = model.create_entity("IfcProductDefinitionShape", Representations=[rep])
    return model, {"project": project, "assembly": assembly, "pipe": pipe, "other": other, "port": port, "peer": peer,
                   "layer": layer, "material": mat, "property": prop, "type": typ}


def _read(model, product, **groups):
    return read_products(model, IfcProductsReadRuntimeRequest(global_ids=(product.GlobalId,), groups=groups)).products[0]


def test_basic_reads_metres_and_actual_parent_without_extracting_groups(monkeypatch):
    model, items = _fixture()
    before = model.to_string()
    import CAD2BIM_backend.infrastructure.ifc.product_read as module
    def unused(*args):
        raise AssertionError("unrequested group executed")
    for name in ("structure", "properties", "materials", "spatial", "systems", "ports", "representations"):
        monkeypatch.setattr(module, "_" + name, unused)
    result = _read(model, items["pipe"])
    assert result.basic.placement.translation_m == (1., 2., 3.)
    assert result.basic.aggregate_parent.parents[0].global_id == items["assembly"].GlobalId
    assert result.properties is None and result.ports is None
    assert model.to_string() == before


def test_selected_semantics_are_actual_direct_facts_with_name_discovery():
    model, items = _fixture()
    result = _read(model, items["pipe"], structure={}, properties={"mode": "names"}, materials={"mode": "names"},
        spatial={}, systems={}, ports={"mode": "names"}, representations={})
    assert {(s.source, s.set_kind, s.name.value) for s in result.properties.sets} == {
        ("instance", "pset", "Pset_Actual"), ("type", "pset", "Pset_Actual"), ("instance", "qto", "Qto_Actual")}
    assert all(p.value is None for s in result.properties.sets for p in s.properties)
    assert {a.kind for a in result.materials.assignments} == {"single", "layer_set_usage"}
    assert {a.source for a in result.materials.assignments} == {"instance", "type"}
    assert {a.kind for a in result.spatial.assignments} == {"containment", "reference"}
    assert result.spatial.assignments[0].hierarchy[0].name.value == "Building"
    assert len(result.systems.assignments) == 1
    assert result.systems.assignments[0].service_relationships[0].related.name.value == "Building"
    assert len(result.ports.ports) == 1 and result.ports.ports[0].port.global_id == items["port"].GlobalId
    assert not result.ports.ports[0].connections
    assert result.representations.body_status == "found"
    assert result.structure.aggregation[0].direction == "parent"
    assert [(r.related.global_id, r.direction) for r in result.structure.nesting] == [(items["port"].GlobalId, "child")]
    shared = result.structure.placement_dependencies[0]
    assert shared.owner_count == len(shared.owners)
    assert {o.global_id for o in shared.owners} >= {items["pipe"].GlobalId, items["other"].GlobalId}
    assert _read(model, items["assembly"], representations={}).representations.body_status == "empty"


def test_named_values_keep_instance_type_unit_and_actual_port_connection():
    model, items = _fixture()
    result = _read(model, items["pipe"], properties={"mode": "values", "selectors": [
        {"source": "instance", "set_kind": "pset", "set_name": "Pset_Actual", "property_names": ["Pressure"]},
        {"source": "type", "set_kind": "pset", "set_name": "Pset_Actual", "property_names": ["Pressure"]},
        {"source": "instance", "set_kind": "qto", "set_name": "Qto_Actual", "property_names": ["Length"]}]},
        materials={"mode": "values", "selectors": [{"source": "instance", "set_name": "Pipe Wall", "member_names": ["Wall"]}]},
        ports={"mode": "values", "global_ids": [items["port"].GlobalId]})
    values = {(s.source, s.set_kind): s.properties[0].value for s in result.properties.sets}
    assert values[("instance", "pset")].scalar == 600.
    assert values[("type", "pset")].scalar == 700.
    assert values[("instance", "qto")].scalar == 2000.
    assert values[("instance", "qto")].unit.name.value == "METRE"
    assert values[("instance", "qto")].unit.symbol.value == "mm"
    assignment = next(a for a in result.materials.assignments if a.kind == "layer_set_usage")
    assert assignment.members[0].thickness_m == .012
    assert assignment.offset_m == .004
    port = result.ports.ports[0]
    assert port.flow_direction.value == "SOURCE"
    assert port.connections[0].peer_port.global_id == items["peer"].GlobalId
    assert port.connections[0].peer_owners[0].global_id == items["other"].GlobalId
    assert len(port.connections) == 1


def test_unknown_duplicate_and_non_product_are_different():
    model, items = _fixture()
    unknown = ifcopenshell.guid.new()
    model.create_entity("IfcPipeSegment", GlobalId=items["pipe"].GlobalId)
    results = read_products(model, IfcProductsReadRuntimeRequest(global_ids=(unknown, items["pipe"].GlobalId, items["project"].GlobalId))).products
    assert [r.status for r in results] == ["not_found", "ambiguous", "not_product"]
    assert all(r.basic is None for r in results)


def test_unplaced_unsupported_cycle_and_multiple_parents_never_become_zero_frames():
    model, items = _fixture()
    pipe = items["pipe"]
    pipe.ObjectPlacement = None
    assert _read(model, pipe).basic.placement.status == "empty"
    pipe.ObjectPlacement = model.create_entity("IfcLinearPlacement")
    assert _read(model, pipe).basic.placement.status == "unsupported"
    placement = items["other"].ObjectPlacement
    pipe.ObjectPlacement = placement
    placement.PlacementRelTo = placement
    result = _read(model, pipe)
    assert result.basic.placement.status == "unsupported"
    assert result.basic.placement.translation_m is None
    model.create_entity("IfcRelAggregates", GlobalId=ifcopenshell.guid.new(), RelatingObject=items["other"], RelatedObjects=[pipe])
    result = _read(model, pipe)
    assert result.basic.aggregate_parent.status == "ambiguous"
    assert len(result.basic.aggregate_parent.parents) == 2


def test_collection_limit_is_explicit_and_names_filter_does_not_fall_back():
    model, items = _fixture()
    result = _read(model, items["pipe"], properties={"mode": "values", "selectors": [
        {"source": "instance", "set_kind": "pset", "set_name": "Missing", "property_names": ["X"]}]})
    assert not result.properties.sets
    assert "not found" in result.properties.diagnostic.value
    pset = model.by_type("IfcPropertySet")[0]
    pset.HasProperties = [model.create_entity("IfcPropertySingleValue", Name=f"P{i}") for i in range(129)]
    with pytest.raises(IfcProductsReadError, match="129 entries"):
        _read(model, items["pipe"], properties={"mode": "names"})


def test_runtime_projection_is_closed_and_representative_size_is_bounded():
    model, items = _fixture()
    request = IfcProductsReadRuntimeRequest(global_ids=(items["pipe"].GlobalId,))
    assert InspectExactIfcRevisionRequest(projection="products", products_request=request).products_request == request
    with pytest.raises(ValueError, match="typed selectors"):
        InspectExactIfcRevisionRequest(projection="products")
    with pytest.raises(ValueError, match="typed selectors"):
        InspectExactIfcRevisionRequest(products_request=request)
    result = read_products(model, request)
    assert len(result.model_dump_json().encode()) < 256 * 1024
    assert json.loads(result.model_dump_json())["products"][0]["status"] == "found"


def test_requested_property_limits_ignore_unselected_type_and_other_fields(monkeypatch):
    model, items = _fixture()
    pset = model.by_type("IfcPropertySet")[0]
    pset.HasProperties = [items["property"], *[model.create_entity("IfcPropertySingleValue", Name=f"Unrelated{i}") for i in range(130)]]
    import CAD2BIM_backend.infrastructure.ifc.product_read as module
    def no_type(*args):
        raise AssertionError("instance-only selection must not inspect Type")
    monkeypatch.setattr(module, "_types", no_type)
    result = _read(model, items["pipe"], properties={"mode": "values", "selectors": [
        {"source": "instance", "set_kind": "pset", "set_name": "Pset_Actual", "property_names": ["Pressure"]}]})
    assert result.properties.sets[0].properties[0].value.scalar == 600.
    assert not result.properties.types


def test_discovered_full_names_can_be_copied_and_duplicate_material_names_are_explicit():
    model, items = _fixture()
    long_name = "压力" * 1100
    items["property"].Name = long_name
    discovered = _read(model, items["pipe"], properties={"mode": "names", "sources": ["instance"]})
    prop = next(s for s in discovered.properties.sets if s.set_kind == "pset").properties[0]
    assert prop.name.truncated
    assert prop.selector_name == long_name
    selected = _read(model, items["pipe"], properties={"mode": "values", "selectors": [
        {"source": "instance", "set_kind": "pset", "set_name": "Pset_Actual", "property_names": [prop.selector_name]}]})
    assert selected.properties.sets[0].properties[0].value.scalar == 600.
    layer_set = model.by_type("IfcMaterialLayerSet")[0]
    layer_set.MaterialLayers = [items["layer"], model.create_entity("IfcMaterialLayer", Name="Wall", Material=items["material"], LayerThickness=8.)]
    result = _read(model, items["pipe"], materials={"mode": "values", "selectors": [
        {"source": "instance", "set_name": "Pipe Wall", "member_names": ["Wall"]}]})
    assert result.materials.status == "ambiguous"
    assert len(result.materials.assignments[0].members) == 2
    missing = _read(model, items["pipe"], materials={"mode": "values", "selectors": [
        {"source": "instance", "set_name": "Pipe Wall", "member_names": ["Missing"]}]})
    assert missing.materials.status == "unsupported"


def test_material_profile_constituent_and_list_keep_real_types_and_local_ids():
    model, items = _fixture()
    profile = model.create_entity("IfcCircleProfileDef", ProfileType="AREA", ProfileName="Round", Radius=10.)
    member = model.create_entity("IfcMaterialProfile", Name="Profile", Material=items["material"], Profile=profile)
    profile_set = model.create_entity("IfcMaterialProfileSet", Name="Profiles", MaterialProfiles=[member])
    constituent = model.create_entity("IfcMaterialConstituent", Name="Constituent", Material=items["material"], Fraction=.6)
    constituents = model.create_entity("IfcMaterialConstituentSet", Name="Constituents", MaterialConstituents=[constituent])
    material_list = model.create_entity("IfcMaterialList", Materials=[items["material"]])
    for material in (profile_set, constituents, material_list):
        model.create_entity("IfcRelAssociatesMaterial", GlobalId=ifcopenshell.guid.new(), RelatedObjects=[items["pipe"]], RelatingMaterial=material)
    result = _read(model, items["pipe"], materials={"mode": "names"})
    assert {a.kind for a in result.materials.assignments} >= {"profile_set", "constituent_set", "list"}
    assert result.materials.status == "unsupported"  # IfcMaterialList has no stable set name.
    result = _read(model, items["pipe"], materials={"mode": "values", "selectors": [
        {"source": "instance", "set_name": "Profiles", "member_names": ["Profile"]},
        {"source": "instance", "set_name": "Constituents", "member_names": ["Constituent"]}]})
    profiles = next(a for a in result.materials.assignments if a.kind == "profile_set")
    assert profiles.members[0].profile.step_id == profile.id()
    assert next(a for a in result.materials.assignments if a.kind == "constituent_set").members[0].fraction == .6


def test_unsupported_property_structure_and_response_limit_are_explicit():
    model, items = _fixture()
    bound = model.create_entity("IfcPropertyBoundedValue", Name="Bounds", UpperBoundValue=model.create_entity("IfcReal", 5.))
    model.by_type("IfcPropertySet")[0].HasProperties = [bound]
    result = _read(model, items["pipe"], properties={"mode": "values", "selectors": [
        {"source": "instance", "set_kind": "pset", "set_name": "Pset_Actual", "property_names": ["Bounds"]}]})
    assert result.properties.sets[0].properties[0].value.status == "unsupported"
    bound.Name = "LongName" * 40000
    with pytest.raises(IfcProductsReadError, match="bytes"):
        _read(model, items["pipe"], properties={"mode": "names", "sources": ["instance"]})


def test_tapering_material_usage_is_explicitly_unsupported_and_original_deadline_is_checked():
    model, items = _fixture()
    profile = model.create_entity("IfcCircleProfileDef", ProfileType="AREA", Radius=10.)
    member = model.create_entity("IfcMaterialProfile", Name="Start", Material=items["material"], Profile=profile)
    start = model.create_entity("IfcMaterialProfileSet", Name="Start Set", MaterialProfiles=[member])
    end = model.create_entity("IfcMaterialProfileSet", Name="End Set", MaterialProfiles=[member])
    usage = model.create_entity("IfcMaterialProfileSetUsageTapering", ForProfileSet=start, ForProfileEndSet=end)
    model.create_entity("IfcRelAssociatesMaterial", GlobalId=ifcopenshell.guid.new(), RelatedObjects=[items["pipe"]], RelatingMaterial=usage)
    result = _read(model, items["pipe"], materials={"mode": "names"})
    assert result.materials.status == "unsupported"
    assert next(a for a in result.materials.assignments if a.material.ifc_class == "IfcMaterialProfileSetUsageTapering").kind == "unsupported"
    checks = 0
    def check_budget():
        nonlocal checks
        checks += 1
        if checks >= 3:
            raise TimeoutError("original call deadline")
    with pytest.raises(TimeoutError, match="original call deadline"):
        read_products(model, IfcProductsReadRuntimeRequest(global_ids=(items["pipe"].GlobalId,)), check_budget=check_budget)


def test_child_dispatch_parses_once_and_projects_all_groups_without_a_second_worker(monkeypatch):
    from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
    from CAD2BIM_backend.infrastructure.ifc_revision_runtime.child import _execute
    model, items = _fixture()
    content = model.to_string().encode()
    request = IfcProductsReadRuntimeRequest(global_ids=(items["pipe"].GlobalId,), groups={
        "structure": {}, "properties": {"mode": "names"}, "materials": {"mode": "names"},
        "spatial": {}, "systems": {}, "ports": {"mode": "names"}, "representations": {}})
    original = IfcOpenShellModelCodec.private_copy_from_source_fd
    parsed = []
    def record_parse(self, *args, **kwargs):
        parsed.append(1)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(IfcOpenShellModelCodec, "private_copy_from_source_fd", record_parse)
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "exact.ifc"
        source.write_bytes(content)
        descriptor = os.open(source, os.O_RDONLY)
        source.unlink()
        result, attestation = _execute({"kind": "inspect_exact", "projection": "products",
            "products_request": request.model_dump(mode="json"), "deadline_monotonic": time.monotonic() + 10.,
            "source": {"fd": descriptor, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}})
    assert parsed == [1]
    assert set(result) == {"inspection", "products"}
    assert result["products"]["products"][0]["properties"]["status"] == "found"
    assert attestation["source_sha256"] == hashlib.sha256(content).hexdigest()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_revision_runtime_projects_seven_groups_and_preserves_exact_source(tmp_path):
    from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
    from CAD2BIM_backend.infrastructure.ifc_revision_runtime import IfcOpenShellRevisionRuntime
    from CAD2BIM_backend.infrastructure.worker_runtime import WorkerProcessSupervisor
    from CAD2BIM_backend.domain.runtime_execution import IFC_REVISION_EXECUTION

    class ObservedSupervisor(WorkerProcessSupervisor):
        terminal = None
        async def run(self, *args, **kwargs):
            self.terminal = await super().run(*args, **kwargs)
            if self.terminal.returncode:
                print("PRODUCT_READ_WORKER_STDERR=" + self.terminal.stderr.decode("utf-8", errors="replace"))
            return self.terminal

    model, items = _fixture()
    content = model.to_string().encode()
    expected = IfcOpenShellModelCodec().inspect(content)
    class Source:
        size_bytes = len(content)
        sha256 = hashlib.sha256(content).hexdigest()
        async def iter_chunks(self, *, chunk_size=1024 * 1024):
            for offset in range(0, len(content), chunk_size):
                yield content[offset:offset + chunk_size]
    supervisor = ObservedSupervisor()
    runtime = IfcOpenShellRevisionRuntime(python_executable=sys.executable, capacity_root=tmp_path / "capacity", supervisor=supervisor)
    request = IfcProductsReadRuntimeRequest(global_ids=(items["pipe"].GlobalId,), groups={
        "structure": {}, "properties": {"mode": "names"}, "materials": {"mode": "names"},
        "spatial": {}, "systems": {}, "ports": {"mode": "names"}, "representations": {}})
    wall, cpu = time.perf_counter(), time.process_time()
    try:
        async with runtime.invocation_scope(IFC_REVISION_EXECUTION, deadline=time.monotonic() + IFC_REVISION_EXECUTION.total_seconds):
            result = await runtime.execute(InspectExactIfcRevisionRequest(projection="products", products_request=request), source=Source())
        measurement = {"case": "real_revision_runtime_products", "source_bytes": len(content),
            "selected": 1, "wall_seconds": time.perf_counter() - wall, "parent_cpu_seconds": time.process_time() - cpu,
            "typed_products_json_bytes": len(result.products.model_dump_json().encode()),
            "worker_observations": asdict(supervisor.terminal.observations)}
        print("PRODUCT_READ_RUNTIME_MEASUREMENT=" + json.dumps(measurement, sort_keys=True))
        assert result.inspection == expected
        facts = result.products.products[0]
        assert facts.basic.placement.translation_m == (1., 2., 3.)
        assert all(getattr(facts, name) is not None for name in type(request.groups).model_fields)
        assert facts.ports.ports[0].port.global_id == items["port"].GlobalId
        assert model.to_string().encode() == content
    finally:
        await runtime.close()


def test_shared_type_material_and_peer_network_are_not_recursively_expanded():
    model, items = _fixture()
    pipe, other = items["pipe"], items["other"]
    pipe.IsTypedBy[0].RelatedObjects = (pipe, other)
    pipe.HasAssociations[0].RelatedObjects = (pipe, other)
    third = model.create_entity("IfcPipeSegment", GlobalId=ifcopenshell.guid.new(), Name="Third")
    last = model.create_entity("IfcDistributionPort", GlobalId=ifcopenshell.guid.new(), Name="Third Port")
    model.create_entity("IfcRelNests", GlobalId=ifcopenshell.guid.new(), RelatingObject=third, RelatedObjects=[last])
    model.create_entity("IfcRelConnectsPorts", GlobalId=ifcopenshell.guid.new(), RelatingPort=items["peer"], RelatedPort=last)
    result = _read(model, pipe, properties={"mode": "names"}, materials={"mode": "names"}, systems={},
        ports={"mode": "values", "global_ids": [items["port"].GlobalId]})
    assert len(result.properties.types) == 1
    assert len(result.materials.assignments) == 2
    assert len(result.systems.assignments) == 1
    assert result.ports.ports[0].connections[0].peer_owners[0].global_id == other.GlobalId
    assert len(result.ports.ports[0].connections) == 1
    serialized = result.model_dump_json()
    assert third.GlobalId not in serialized and last.GlobalId not in serialized


def test_response_bytes_accumulate_across_many_members_and_selected_groups():
    model, items = _fixture()
    pset = model.by_type("IfcPropertySet")[0]
    pset.HasProperties = [model.create_entity("IfcPropertySingleValue", Name=f"P{i}" + "x" * 1050) for i in range(80)]
    layers = model.by_type("IfcMaterialLayerSet")[0]
    layers.MaterialLayers = [model.create_entity("IfcMaterialLayer", Name=f"L{i}" + "y" * 1050,
        Material=items["material"], LayerThickness=1.) for i in range(80)]
    properties = {"mode": "names", "sources": ["instance"]}
    materials = {"mode": "names", "sources": ["instance"]}
    assert len(_read(model, items["pipe"], properties=properties).model_dump_json().encode()) < 256 * 1024
    assert len(_read(model, items["pipe"], materials=materials).model_dump_json().encode()) < 256 * 1024
    with pytest.raises(IfcProductsReadError, match="bytes"):
        _read(model, items["pipe"], properties=properties, materials=materials)


def measure_product_read_fixture():
    """Reproducible small-model sizing entry; no worker or database is started."""
    model, items = _fixture()
    pipe = items["pipe"]
    selected = [items["assembly"].GlobalId, pipe.GlobalId, items["other"].GlobalId]
    for index in range(29):
        placement = model.create_entity("IfcLocalPlacement", RelativePlacement=pipe.ObjectPlacement.RelativePlacement)
        obj = model.create_entity("IfcPipeSegment", GlobalId=ifcopenshell.guid.new(), Name=f"P{index}", ObjectPlacement=placement)
        for rel in (*pipe.IsDefinedBy, *pipe.IsTypedBy, *pipe.HasAssociations, *pipe.HasAssignments):
            rel.RelatedObjects = (*rel.RelatedObjects, obj)
        for rel in (*pipe.ContainedInStructure, *pipe.ReferencedInStructures):
            rel.RelatedElements = (*rel.RelatedElements, obj)
        model.create_entity("IfcRelAggregates", GlobalId=ifcopenshell.guid.new(), RelatingObject=items["assembly"], RelatedObjects=[obj])
        port = model.create_entity("IfcDistributionPort", GlobalId=ifcopenshell.guid.new(), Name=f"Port{index}", ObjectPlacement=placement, FlowDirection="NOTDEFINED")
        model.create_entity("IfcRelNests", GlobalId=ifcopenshell.guid.new(), RelatingObject=obj, RelatedObjects=[port])
        selected.append(obj.GlobalId)
    groups = {"structure": {}, "properties": {"mode": "names"}, "materials": {"mode": "names"},
              "spatial": {}, "systems": {}, "ports": {"mode": "names"}, "representations": {}}
    measurements = []
    for label, ids, selection in (("one_all", selected[1:2], groups), ("32_basic", selected, {}), ("32_all", selected, groups)):
        request = IfcProductsReadRuntimeRequest(global_ids=ids, groups=selection)
        wall, cpu = time.perf_counter(), time.process_time()
        try:
            result = read_products(model, request)
            outcome = {"status": "returned", "typed_json_bytes": len(result.model_dump_json().encode())}
        except IfcProductsReadError as error:
            outcome = {"status": "rejected", "error_code": error.code, "error_message": str(error)}
        measurements.append({"case": label, "selected": len(ids), "wall_seconds": time.perf_counter() - wall,
            "cpu_seconds": time.process_time() - cpu, "ifc_bytes": len(model.to_string().encode()), **outcome})
    model, items = _fixture()
    model.by_type("IfcPropertySet")[0].HasProperties = [model.create_entity("IfcPropertySingleValue", Name=f"Field{i}", NominalValue=model.create_entity("IfcReal", float(i))) for i in range(128)]
    request = IfcProductsReadRuntimeRequest(global_ids=(items["pipe"].GlobalId,), groups={"properties": {"mode": "names", "sources": ["instance"]}})
    wall, cpu = time.perf_counter(), time.process_time()
    result = read_products(model, request)
    measurements.append({"case": "128_property_names", "selected": 1, "wall_seconds": time.perf_counter() - wall,
        "cpu_seconds": time.process_time() - cpu, "typed_json_bytes": len(result.model_dump_json().encode())})
    return measurements
