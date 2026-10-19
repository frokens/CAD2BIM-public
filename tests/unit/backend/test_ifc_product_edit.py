from __future__ import annotations

import hashlib
import sys
import ifcopenshell
import ifcopenshell.guid
import ifcopenshell.util.placement
import numpy as np
import pytest

from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.ifc_product_compilation import IfcProductCompilationBlock
from CAD2BIM_backend.domain.product_edit import (
    CompileIfcProductsEditCommand, DeleteProductEditRow, PlacementProductEditRow, ReplaceProductEditRow,
    IfcProductsEditTarget, IfcProductsEditPreflightRequest,
)
from CAD2BIM_backend.infrastructure.ifc.product_edit import IfcOpenShellProductEditor, preflight_edit_targets, _axis
from CAD2BIM_backend.infrastructure.ifc_revision_runtime.adapter import IfcOpenShellRevisionRuntime
from CAD2BIM_backend.ports.ifc_revision_runtime import InspectExactIfcRevisionRequest, EditIfcProductsRevisionRequest
from tests.process_support.ifc_revision_runtime import BytesIfcCandidate, read_source


def root(model, kind="IfcWall", **attrs):
    return model.create_entity(kind, GlobalId=ifcopenshell.guid.new(), **attrs)


def placed(model, *, parent=None, xyz=(0.,0.,0.), kind="IfcWall", **attrs):
    value = np.eye(4); value[:3,3] = xyz
    return root(model, kind, ObjectPlacement=model.create_entity("IfcLocalPlacement", PlacementRelTo=parent,
        RelativePlacement=_axis(model, value)), **attrs)


def command(model, rows, families=None):
    targets = tuple(IfcProductsEditTarget(input_row_index=r.input_row_index, product_key=r.product_key,
        target_global_id=r.target_global_id, action=r.action,
        replacement_ifc_class=(next(c.ifc_class.value for c in families[r.family_version_id].components
            if c.parent_component_key is None) if r.action == "replace" else None)) for r in rows)
    preflight = preflight_edit_targets(model, IfcProductsEditPreflightRequest(targets=targets))
    assert preflight.accepted, preflight.checks
    return CompileIfcProductsEditCommand(rows=tuple(rows), families=families or {}, preflight=preflight)


def delete_row(entity, index=0):
    return DeleteProductEditRow(input_row_index=index, product_key=f"p{index+1}", target_global_id=entity.GlobalId)


@pytest.mark.parametrize("pset_mode", ("separate", "shared_members"))
def test_delete_protects_shared_pds_pset_type_and_material(pset_mode):
    m = ifcopenshell.file(schema="IFC4X3_ADD2")
    a,b = root(m),root(m)
    rep = m.create_entity("IfcShapeRepresentation", RepresentationIdentifier="Body", Items=[m.create_entity("IfcCartesianPoint",Coordinates=(0.,0.,0.))])
    pds = m.create_entity("IfcProductDefinitionShape", Representations=[rep]); a.Representation=pds;b.Representation=pds
    prop=m.create_entity("IfcPropertySingleValue",Name="Shared",NominalValue=m.create_entity("IfcLabel","keep"))
    pset=root(m,"IfcPropertySet",Name="Shared",HasProperties=[prop])
    if pset_mode=="separate":
        root(m,"IfcRelDefinesByProperties",RelatedObjects=[a],RelatingPropertyDefinition=pset)
        root(m,"IfcRelDefinesByProperties",RelatedObjects=[b],RelatingPropertyDefinition=pset)
    else: root(m,"IfcRelDefinesByProperties",RelatedObjects=[a,b],RelatingPropertyDefinition=pset)
    typ=root(m,"IfcWallType",PredefinedType="STANDARD")
    root(m,"IfcRelDefinesByType",RelatedObjects=[a,b],RelatingType=typ)
    mat=m.create_entity("IfcMaterial",Name="keep material")
    root(m,"IfcRelAssociatesMaterial",RelatedObjects=[a,b],RelatingMaterial=mat)
    ids=(a.GlobalId,b.GlobalId,pset.GlobalId,typ.GlobalId)
    editor=IfcOpenShellProductEditor(m,command(m,[delete_row(a)]));editor.verify_saved(ifcopenshell.file.from_string(m.to_string()))
    peer=m.by_guid(ids[1]); assert peer.Representation==pds and pds.Representations==(rep,)
    assert m.by_guid(ids[2]).HasProperties[0].NominalValue.wrappedValue=="keep"
    assert len(peer.IsDefinedBy)==1 and peer.IsDefinedBy[0].RelatingPropertyDefinition==pset
    assert peer.IsTypedBy[0].RelatingType==typ and typ.is_a("IfcWallType")
    assert mat in m.by_type("IfcMaterial") and len(m.by_type("IfcWall"))==1


def test_owned_delete_disconnects_ports_and_preserves_parent_sibling_and_peer():
    m=ifcopenshell.file(schema="IFC4X3_ADD2")
    parent=root(m,"IfcElementAssembly");a,b,peer=root(m),root(m),root(m)
    rel=root(m,"IfcRelAggregates",RelatingObject=parent,RelatedObjects=[a,b])
    ownport=root(m,"IfcDistributionPort");peerport=root(m,"IfcDistributionPort")
    root(m,"IfcRelNests",RelatingObject=a,RelatedObjects=[ownport])
    root(m,"IfcRelNests",RelatingObject=peer,RelatedObjects=[peerport])
    root(m,"IfcRelConnectsPorts",RelatingPort=ownport,RelatedPort=peerport)
    ids=(a.GlobalId,ownport.GlobalId,peerport.GlobalId)
    editor=IfcOpenShellProductEditor(m,command(m,[delete_row(a)]));editor.verify_saved(ifcopenshell.file.from_string(m.to_string()))
    assert rel.RelatedObjects==(b,) and m.by_guid(peer.GlobalId)==peer
    assert m.by_guid(ids[2])==peerport and not m.by_type("IfcRelConnectsPorts")
    assert {c.global_id for c in editor.effect().changes if c.change_kind=="deleted"}==set(ids[:2])
    assert any(c.related_global_id==ids[2] for c in editor.effect().changes if c.change_kind=="relation_detached")


def test_placement_moves_owned_tree_and_compensates_external_placement():
    from tests.unit.backend.test_ifc_product_compilation import model
    m=model(millimetres=True)
    a=placed(m,kind="IfcElementAssembly",xyz=(1000.,2000.,0.))
    b=placed(m,parent=a.ObjectPlacement,xyz=(200.,0.,0.))
    root(m,"IfcRelNests",RelatingObject=a,RelatedObjects=[b])
    outside=placed(m,parent=a.ObjectPlacement,xyz=(500.,300.,0.))
    before={e.GlobalId:ifcopenshell.util.placement.get_local_placement(e.ObjectPlacement).copy() for e in (a,b,outside)}
    frame=RigidTransform3D(rotation=((0.,-1.,0.),(1.,0.,0.),(0.,0.,1.)),translation_m=(4.,5.,0.))
    row=PlacementProductEditRow(input_row_index=0,product_key="p1",target_global_id=a.GlobalId,placement=frame)
    editor=IfcOpenShellProductEditor(m,command(m,[row]));saved=ifcopenshell.file.from_string(m.to_string());editor.verify_saved(saved)
    after={e.GlobalId:ifcopenshell.util.placement.get_local_placement(e.ObjectPlacement) for e in (a,b,outside)}
    assert np.allclose(after[outside.GlobalId],before[outside.GlobalId])
    assert np.allclose(np.linalg.inv(after[a.GlobalId])@after[b.GlobalId],np.linalg.inv(before[a.GlobalId])@before[b.GlobalId])
    assert np.allclose(after[a.GlobalId][:3,3],(4000.,5000.,0.))
    assert {c.global_id for c in editor.effect().changes if c.change_kind=="placement_changed"}=={a.GlobalId,b.GlobalId}


def test_preflight_rejects_overlap_and_external_connections_without_editing():
    m=ifcopenshell.file(schema="IFC4X3_ADD2")
    a,b=placed(m),placed(m)
    root(m,"IfcRelAggregates",RelatingObject=a,RelatedObjects=[b])
    peer=placed(m)
    root(m,"IfcRelConnectsElements",RelatingElement=b,RelatedElement=peer)
    before=m.to_string()
    request=IfcProductsEditPreflightRequest(targets=(
        IfcProductsEditTarget(input_row_index=0,product_key="p1",target_global_id=a.GlobalId,action="placement"),
        IfcProductsEditTarget(input_row_index=1,product_key="p2",target_global_id=b.GlobalId,action="delete")))
    result=preflight_edit_targets(m,request)
    assert not result.accepted and m.to_string()==before
    assert {c.error_code for c in result.checks}>={"IFC_EDIT_TARGET_OVERLAP","IFC_EDIT_EXTERNAL_CONNECTION"}


@pytest.mark.parametrize("kind", ["duplicate", "ancestor", "shared_port", "missing_duplicate"])
def test_preflight_reports_every_overlap_in_original_request_order(kind):
    m=ifcopenshell.file(schema="IFC4X3_ADD2")
    a,b,c=placed(m),placed(m),placed(m)
    if kind == "ancestor":
        root(m,"IfcRelAggregates",RelatingObject=a,RelatedObjects=[b])
        root(m,"IfcRelAggregates",RelatingObject=b,RelatedObjects=[c])
        guids=[a.GlobalId,b.GlobalId,c.GlobalId]
    elif kind == "shared_port":
        port=root(m,"IfcDistributionPort")
        for owner in (a,b,c):
            root(m,"IfcRelConnectsPortToElement",RelatingPort=port,RelatedElement=owner)
        guids=[a.GlobalId,b.GlobalId,c.GlobalId]
    else:
        guids=[a.GlobalId if kind == "duplicate" else "0"*22]*3
    before=m.to_string()
    request=IfcProductsEditPreflightRequest(targets=tuple(
        IfcProductsEditTarget(input_row_index=index,product_key=f"p{index}",target_global_id=guid,action="delete")
        for index,guid in zip((7,3,11),guids)))
    result=preflight_edit_targets(m,request)
    assert not result.accepted and m.to_string()==before
    overlaps=[check for check in result.checks if check.error_code == "IFC_EDIT_TARGET_OVERLAP"]
    assert [(check.input_row_index,check.message,check.related_global_id) for check in overlaps] == [
        (3,"Target overlaps input row 7",guids[0]),
        (11,"Target overlaps input row 7",guids[0]),
        (11,"Target overlaps input row 3",guids[1])]


@pytest.mark.parametrize("presentation", ("style", "aspect", "item_layer", "body_layer"))
def test_preflight_shared_context_filters_once_and_keeps_presentation_guards(presentation, monkeypatch):
    from tests.unit.backend.test_ifc_product_compilation import model
    m=model();context=next(c for c in m.by_type("IfcGeometricRepresentationSubContext") if c.ContextIdentifier=="Body")
    products=[]
    for _ in range(3):
        entity=placed(m);point=m.create_entity("IfcCartesianPoint",Coordinates=(0.,0.,0.))
        shape=m.create_entity("IfcShapeRepresentation",ContextOfItems=context,RepresentationIdentifier="Body",Items=[point])
        entity.Representation=m.create_entity("IfcProductDefinitionShape",Representations=[shape]);products.append(entity)
    selected=products[1];shape=selected.Representation.Representations[0];point=shape.Items[0]
    if presentation=="style":
        m.create_entity("IfcStyledItem",Item=point,Styles=[m.create_entity("IfcCurveStyle",Name="style")])
    elif presentation=="aspect":
        m.create_entity("IfcShapeAspect",ShapeRepresentations=[shape],Name="aspect",ProductDefinitional=True,
            PartOfProductDefinitionShape=selected.Representation)
    else:
        m.create_entity("IfcPresentationLayerAssignment",Name="layer",AssignedItems=[point if presentation=="item_layer" else shape])
    calls=[];original=m.get_inverse
    def tracked(entity,*args,**kwargs):
        calls.append(entity.id());return original(entity,*args,**kwargs)
    monkeypatch.setattr(m,"get_inverse",tracked)
    before=m.to_string()
    request=IfcProductsEditPreflightRequest(targets=tuple(IfcProductsEditTarget(input_row_index=i,
        product_key=f"p{i}",target_global_id=p.GlobalId,action="replace") for i,p in zip((7,3,11),products)))
    result=preflight_edit_targets(m,request)
    assert calls.count(context.id())==1 and m.to_string()==before
    failures=[c for c in result.checks if c.error_code=="IFC_EDIT_PRESENTATION_UNSUPPORTED"]
    assert result.accepted==(presentation=="body_layer")
    assert not failures if presentation=="body_layer" else {c.input_row_index for c in failures}=={3}


def test_preflight_retired_child_reference_keeps_first_external(monkeypatch):
    from CAD2BIM_backend.infrastructure.ifc import product_edit
    m=ifcopenshell.file(schema="IFC4X3_ADD2");a=placed(m,kind="IfcElementAssembly");b,c,peer,owner=(placed(m) for _ in range(4))
    root(m,"IfcRelNests",RelatingObject=a,RelatedObjects=[b,c])
    rel=root(m,"IfcRelAssignsToProduct",RelatedObjects=[b,c,peer],RelatingProduct=owner)
    original=product_edit._product_references;calls=[]
    def tracked(relation):
        calls.append(relation.id());return original(relation)
    monkeypatch.setattr(product_edit,"_product_references",tracked)
    before=m.to_string()
    result=preflight_edit_targets(m,IfcProductsEditPreflightRequest(targets=(IfcProductsEditTarget(
        input_row_index=7,product_key="assembly",target_global_id=a.GlobalId,action="replace"),)))
    failures=[c for c in result.checks if c.error_code=="IFC_EDIT_RETIRED_CHILD_REFERENCE"]
    assert not result.accepted and m.to_string()==before and calls.count(rel.id())==1
    assert [c.related_global_id for c in failures]==[peer.GlobalId,rel.GlobalId,peer.GlobalId,rel.GlobalId]


def test_reference_occurrences_match_native_inverse_multiplicity():
    from CAD2BIM_backend.infrastructure.ifc.product_edit import _reference_occurrences
    m=ifcopenshell.file(schema="IFC4X3_ADD2");point=m.create_entity("IfcCartesianPoint",Coordinates=(0.,0.,0.))
    direction=m.create_entity("IfcDirection",DirectionRatios=(1.,0.,0.))
    line=m.create_entity("IfcPolyline",Points=[point,point,point])
    axis=m.create_entity("IfcAxis2Placement3D",Location=point,Axis=direction,RefDirection=direction)
    internal=_reference_occurrences(line)+_reference_occurrences(axis)
    assert internal[point.id()]==m.get_total_inverses(point)==4
    assert internal[direction.id()]==m.get_total_inverses(direction)==2
    assert len(m.get_inverse(point))==2 and len(m.get_inverse(direction))==1
    m.create_entity("IfcPolyline",Points=[point,point])
    assert m.get_total_inverses(point)>internal[point.id()]


@pytest.mark.parametrize("shared", ("pds", "map"))
@pytest.mark.parametrize("external_context_users", (False, True))
def test_mutation_rechecks_shared_representation_last_user_and_layer_members(shared,external_context_users,monkeypatch):
    from tests.unit.backend.test_ifc_product_compilation import model
    m=model();a,b=placed(m),placed(m);context=next(c for c in m.by_type("IfcGeometricRepresentationSubContext") if c.ContextIdentifier=="Body")
    point=m.create_entity("IfcCartesianPoint",Coordinates=(0.,0.,0.))
    representation=m.create_entity("IfcShapeRepresentation",ContextOfItems=context,RepresentationIdentifier="Body",Items=[point])
    styled=m.create_entity("IfcStyledItem",Item=point,Styles=[m.create_entity("IfcCurveStyle",Name="keep definition")]);styled_id=styled.id()
    if shared=="pds":
        pds=m.create_entity("IfcProductDefinitionShape",Representations=[representation]);a.Representation=pds;b.Representation=pds
        shapes=[representation]
    else:
        mapping=m.create_entity("IfcRepresentationMap",MappingOrigin=_axis(m,np.eye(4)),MappedRepresentation=representation)
        shapes=[]
        for product in (a,b):
            target=m.create_entity("IfcCartesianTransformationOperator3D",LocalOrigin=point,Scale=1.)
            mapped=m.create_entity("IfcMappedItem",MappingSource=mapping,MappingTarget=target)
            shape=m.create_entity("IfcShapeRepresentation",ContextOfItems=context,RepresentationIdentifier="Body",Items=[mapped])
            product.Representation=m.create_entity("IfcProductDefinitionShape",Representations=[shape]);shapes.append(shape)
    layer=m.create_entity("IfcPresentationLayerAssignment",Name="all selected",AssignedItems=shapes);layer_id=layer.id()
    peers={}
    if external_context_users:
        for _ in range(4):
            peer=placed(m,kind="IfcPipeSegment")
            point=m.create_entity("IfcCartesianPoint",Coordinates=(1.,2.,3.))
            shape=m.create_entity("IfcShapeRepresentation",ContextOfItems=context,RepresentationIdentifier="Body",Items=[point])
            peer.Representation=m.create_entity("IfcProductDefinitionShape",Representations=[shape])
            peers[peer.GlobalId]=tuple(str(n) for n in m.traverse(peer.Representation))
    cmd=command(m,[delete_row(a),delete_row(b,1)])
    original=m.get_inverse;calls=[]
    def tracked(entity,*args,**kwargs):
        calls.append(entity.id());return original(entity,*args,**kwargs)
    monkeypatch.setattr(m,"get_inverse",tracked)
    editor=IfcOpenShellProductEditor(m,cmd)
    saved=ifcopenshell.file.from_string(m.to_string());editor.verify_saved(saved)
    assert not saved.by_type("IfcWall") and not saved.by_type("IfcPresentationLayerAssignment")
    assert not saved.by_type("IfcStyledItem") and saved.by_id(context.id())
    assert editor.effect().timings["remove_representation"]>=0
    # The last user is rechecked; a frozen external-use answer would retain
    # these now-unreferenced assignments or the shared source style.
    assert calls.count(context.id())==(1 if external_context_users else 2)
    assert all(tuple(str(n) for n in saved.traverse(saved.by_guid(guid).Representation))==before
        for guid,before in peers.items())
    with pytest.raises(RuntimeError):saved.by_id(styled_id)
    with pytest.raises(RuntimeError):saved.by_id(layer_id)


def test_feature_cross_boundary_rejected_and_shared_placement_noop_is_truthful():
    from tests.unit.backend.test_ifc_product_compilation import model
    m=model();a=placed(m);b=root(m,ObjectPlacement=a.ObjectPlacement)
    opening=placed(m,kind="IfcOpeningElement");filling=placed(m)
    root(m,"IfcRelVoidsElement",RelatingBuildingElement=a,RelatedOpeningElement=opening)
    root(m,"IfcRelFillsElement",RelatingOpeningElement=opening,RelatedBuildingElement=filling)
    target=IfcProductsEditTarget(input_row_index=0,product_key="p1",target_global_id=a.GlobalId,action="delete")
    result=preflight_edit_targets(m,IfcProductsEditPreflightRequest(targets=(target,)))
    assert not result.accepted and any(c.error_code=="IFC_EDIT_EXTERNAL_FEATURE" for c in result.checks)
    # A separate supported row has an external owner of exactly the same placement.
    row=PlacementProductEditRow(input_row_index=0,product_key="p1",target_global_id=b.GlobalId,placement=RigidTransform3D())
    editor=IfcOpenShellProductEditor(m,command(m,[row]));editor.verify_saved(ifcopenshell.file.from_string(m.to_string()))
    assert not editor.effect().changes and np.allclose(ifcopenshell.util.placement.get_local_placement(a.ObjectPlacement),np.eye(4))


def test_delete_shared_representation_map_preserves_other_geometry_and_empty_layers():
    from tests.unit.backend.test_ifc_product_compilation import model
    m=model();a,b=placed(m),placed(m)
    context=next(c for c in m.by_type("IfcGeometricRepresentationSubContext") if c.ContextIdentifier=="Body")
    point=m.create_entity("IfcCartesianPoint",Coordinates=(0.,0.,0.))
    representation=m.create_entity("IfcShapeRepresentation",ContextOfItems=context,RepresentationIdentifier="Body",RepresentationType="Point",Items=[point])
    mapping=m.create_entity("IfcRepresentationMap",MappingOrigin=_axis(m,np.eye(4)),MappedRepresentation=representation)
    style=m.create_entity("IfcCurveStyle",Name="shared source style")
    styled=m.create_entity("IfcStyledItem",Item=point,Styles=[style])
    shapes=[]
    for entity in (a,b):
        operator=m.create_entity("IfcCartesianTransformationOperator3D",LocalOrigin=point,Scale=1.)
        mapped=m.create_entity("IfcMappedItem",MappingSource=mapping,MappingTarget=operator)
        shape=m.create_entity("IfcShapeRepresentation",ContextOfItems=context,RepresentationIdentifier="Body",RepresentationType="MappedRepresentation",Items=[mapped])
        entity.Representation=m.create_entity("IfcProductDefinitionShape",Representations=[shape]);shapes.append(shape)
    layer=m.create_entity("IfcPresentationLayerAssignment",Name="preserve peer",AssignedItems=shapes)
    editor=IfcOpenShellProductEditor(m,command(m,[delete_row(a)]));editor.verify_saved(ifcopenshell.file.from_string(m.to_string()))
    assert b.Representation.Representations==(shapes[1],)
    assert shapes[1].Items[0].MappingSource==mapping and mapping.MappedRepresentation==representation
    assert layer.AssignedItems==(shapes[1],)
    assert styled.Item==point and styled.Styles==(style,)


def test_replace_rebinds_body_layer_and_preserves_instance_pset_invalidates_qto():
    import cadquery as cq
    from tests.unit.backend.test_ifc_product_compilation import model, family, canonical, item
    m=model();a=placed(m,Name="root")
    old=m.create_entity("IfcShapeRepresentation",RepresentationIdentifier="Body",Items=[m.create_entity("IfcCartesianPoint",Coordinates=(0.,0.,0.))])
    a.Representation=m.create_entity("IfcProductDefinitionShape",Representations=[old])
    layer=m.create_entity("IfcPresentationLayerAssignment",Name="root layer",AssignedItems=[old])
    pset=root(m,"IfcPropertySet",Name="ordinary",HasProperties=[m.create_entity("IfcPropertySingleValue",Name="keep",NominalValue=m.create_entity("IfcLabel","not recalculated"))])
    root(m,"IfcRelDefinesByProperties",RelatedObjects=[a],RelatingPropertyDefinition=pset)
    qto=root(m,"IfcElementQuantity",Name="old quantities",Quantities=[m.create_entity("IfcQuantityLength",Name="Length",LengthValue=1.)])
    root(m,"IfcRelDefinesByProperties",RelatedObjects=[a],RelatingPropertyDefinition=qto)
    f=family();row=ReplaceProductEditRow(input_row_index=0,product_key="p1",target_global_id=a.GlobalId,family_version_id="f",invocation={"product_origin":(0.,0.,0.),"length_unit":{}})
    editor=IfcOpenShellProductEditor(m,command(m,[row],{"f":f}))
    editor.consume(IfcProductCompilationBlock(items=(item(f,canonical(cq.Workplane("XY").box(.1,.2,.3).val())),)))
    saved=ifcopenshell.file.from_string(m.to_string());editor.verify_saved(saved)
    target=saved.by_guid(row.target_global_id)
    assert layer.AssignedItems==tuple(m.by_guid(row.target_global_id).Representation.Representations)
    assert {r.RelatingPropertyDefinition.is_a() for r in target.IsDefinedBy}=={"IfcPropertySet"}
    assert any(c.error_code=="IFC_EDIT_PSET_NOT_RECALCULATED" for c in editor.effect().checks)
    assert any(c.change_kind=="relation_rebound" for c in editor.effect().changes)


@pytest.mark.parametrize("other_action", ("delete", "replace"))
@pytest.mark.parametrize("reverse_rows", (False, True))
def test_selected_placement_dependant_is_verified_by_its_own_final_action(other_action, reverse_rows):
    from tests.unit.backend.test_ifc_product_compilation import model, family, canonical, item
    m=model();a=placed(m);b=placed(m,parent=a.ObjectPlacement,xyz=(.5,.2,0.))
    unselected=placed(m,parent=a.ObjectPlacement,xyz=(.8,.9,0.))
    unselected_guid=unselected.GlobalId
    unselected_world=ifcopenshell.util.placement.get_local_placement(unselected.ObjectPlacement).copy()
    # PlacementRelTo alone must not become business ownership or make these
    # legitimate disjoint rows overlap.
    a_guid,b_guid=a.GlobalId,b.GlobalId
    moving=PlacementProductEditRow(input_row_index=0,product_key="p1",target_global_id=a_guid,
        placement=RigidTransform3D(translation_m=(2.,3.,4.)))
    f=family()
    other=(delete_row(b,1) if other_action=="delete" else ReplaceProductEditRow(input_row_index=1,
        product_key="p2",target_global_id=b_guid,family_version_id="f",invocation={"product_origin":(0.,0.,0.),"length_unit":{}}))
    rows=[other,moving] if reverse_rows else [moving,other]
    editor=IfcOpenShellProductEditor(m,command(m,rows,{"f":f} if other_action=="replace" else {}))
    if other_action=="replace":
        import cadquery as cq
        prepared=item(f,canonical(cq.Workplane("XY").box(.1,.2,.3).val()),key="p2")
        prepared=prepared.model_copy(update={"component_project_world_transforms":tuple(
            component.model_copy(update={"project_world_transform":RigidTransform3D()})
            for component in prepared.component_project_world_transforms)})
        editor.consume(IfcProductCompilationBlock(items=(prepared,)))
    saved=ifcopenshell.file.from_string(m.to_string());editor.verify_saved(saved)
    assert np.allclose(ifcopenshell.util.placement.get_local_placement(saved.by_guid(unselected_guid).ObjectPlacement),unselected_world)
    assert np.allclose(ifcopenshell.util.placement.get_local_placement(saved.by_guid(a_guid).ObjectPlacement)[:3,3],(2.,3.,4.))
    if other_action=="delete":
        assert b_guid in {c.global_id for c in editor.effect().changes if c.change_kind=="deleted"}
        with pytest.raises(RuntimeError): saved.by_guid(b_guid)
    else:
        assert saved.by_guid(b_guid).is_a("IfcPipeSegment")
        assert np.allclose(ifcopenshell.util.placement.get_local_placement(saved.by_guid(b_guid).ObjectPlacement)[:3,3],(0.,0.,0.))


@pytest.mark.parametrize("material_kind", ("layer", "profile"))
@pytest.mark.parametrize("on_root", (False, True))
def test_geometry_bound_material_blocks_retained_root_but_allows_retired_child(material_kind, on_root):
    import cadquery as cq
    from tests.unit.backend.test_ifc_product_compilation import model, family, canonical, item
    m=model();a=placed(m,kind="IfcElementAssembly");child=placed(m,parent=a.ObjectPlacement);peer=placed(m)
    root(m,"IfcRelNests",RelatingObject=a,RelatedObjects=[child])
    material=m.create_entity("IfcMaterial",Name="shared base")
    profile=m.create_entity("IfcRectangleProfileDef",ProfileType="AREA",XDim=.2,YDim=.3)
    if material_kind=="layer":
        layer=m.create_entity("IfcMaterialLayer",Material=material,LayerThickness=.02)
        definition=m.create_entity("IfcMaterialLayerSet",MaterialLayers=[layer],LayerSetName="shared layers")
        usage=lambda:m.create_entity("IfcMaterialLayerSetUsage",ForLayerSet=definition,
            LayerSetDirection="AXIS2",DirectionSense="POSITIVE",OffsetFromReferenceLine=0.)
    else:
        mp=m.create_entity("IfcMaterialProfile",Name="shared profile",Material=material,Profile=profile)
        definition=m.create_entity("IfcMaterialProfileSet",Name="shared profiles",MaterialProfiles=[mp])
        usage=lambda:m.create_entity("IfcMaterialProfileSetUsage",ForProfileSet=definition)
    selected_usage,peer_usage=usage(),usage()
    root(m,"IfcRelAssociatesMaterial",RelatedObjects=[a if on_root else child],RelatingMaterial=selected_usage)
    root(m,"IfcRelAssociatesMaterial",RelatedObjects=[peer],RelatingMaterial=peer_usage)
    context=next(c for c in m.by_type("IfcGeometricRepresentationSubContext") if c.ContextIdentifier=="Body")
    solid=m.create_entity("IfcExtrudedAreaSolid",SweptArea=profile,Position=_axis(m,np.eye(4)),
        ExtrudedDirection=m.create_entity("IfcDirection",DirectionRatios=(0.,0.,1.)),Depth=.1)
    child.Representation=m.create_entity("IfcProductDefinitionShape",Representations=[m.create_entity(
        "IfcShapeRepresentation",ContextOfItems=context,RepresentationIdentifier="Body",RepresentationType="SweptSolid",Items=[solid])])
    target=IfcProductsEditTarget(input_row_index=0,product_key="p1",target_global_id=a.GlobalId,action="replace",replacement_ifc_class="IfcPipeSegment")
    preflight=preflight_edit_targets(m,IfcProductsEditPreflightRequest(targets=(target,)))
    if on_root:
        assert not preflight.accepted and any(c.error_code=="IFC_EDIT_MATERIAL_GEOMETRY_UNSUPPORTED" for c in preflight.checks)
        return
    assert preflight.accepted, preflight.checks
    selected_usage_id,peer_usage_id=selected_usage.id(),peer_usage.id()
    definition_id,profile_id,material_id=definition.id(),profile.id(),material.id()
    child_guid=child.GlobalId
    f=family();row=ReplaceProductEditRow(input_row_index=0,product_key="p1",target_global_id=a.GlobalId,
        family_version_id="f",invocation={"product_origin":(0.,0.,0.),"length_unit":{}})
    editor=IfcOpenShellProductEditor(m,CompileIfcProductsEditCommand(rows=(row,),families={"f":f},preflight=preflight))
    editor.consume(IfcProductCompilationBlock(items=(item(f,canonical(cq.Workplane("XY").box(.1,.2,.3).val())),)))
    saved=ifcopenshell.file.from_string(m.to_string());editor.verify_saved(saved)
    with pytest.raises(RuntimeError):saved.by_guid(child_guid)
    with pytest.raises(RuntimeError):saved.by_id(selected_usage_id)
    assert saved.by_id(peer_usage_id) and saved.by_id(definition_id)
    assert saved.by_id(profile_id).XDim==.2 and saved.by_id(material_id).Name=="shared base"
    assert next(r for r in saved.by_guid(peer.GlobalId).HasAssociations if r.is_a("IfcRelAssociatesMaterial")).RelatingMaterial.id()==peer_usage_id


def test_replace_keeps_root_identity_external_parent_and_shared_type_peer():
    import cadquery as cq
    from tests.unit.backend.test_ifc_product_compilation import model, family, canonical, item
    m=model();parent=placed(m,kind="IfcElementAssembly");a=placed(m,Name="preserved name",Tag="tag");b=placed(m)
    root(m,"IfcRelAggregates",RelatingObject=parent,RelatedObjects=[a,b])
    typ=root(m,"IfcWallType",PredefinedType="STANDARD");root(m,"IfcRelDefinesByType",RelatedObjects=[a,b],RelatingType=typ)
    oldchild=placed(m);root(m,"IfcRelNests",RelatingObject=a,RelatedObjects=[oldchild])
    system=root(m,"IfcDistributionSystem",Name="shared system")
    classification=m.create_entity("IfcClassificationReference",Identification="classification")
    document=m.create_entity("IfcDocumentReference",Identification="document")
    memberships=(root(m,"IfcRelAssignsToGroup",RelatedObjects=[oldchild,b],RelatingGroup=system),
        root(m,"IfcRelAssociatesClassification",RelatedObjects=[oldchild,b],RelatingClassification=classification),
        root(m,"IfcRelAssociatesDocument",RelatedObjects=[oldchild,b],RelatingDocument=document))
    membership_ids=[r.GlobalId for r in memberships]
    a_guid,b_guid=a.GlobalId,b.GlobalId
    f=family()
    row=ReplaceProductEditRow(input_row_index=0,product_key="p1",target_global_id=a_guid,family_version_id="family-one",
        invocation={"product_origin":(0.,0.,0.),"length_unit":{}})
    editor=IfcOpenShellProductEditor(m,command(m,[row],{"family-one":f}))
    editor.consume(IfcProductCompilationBlock(items=(item(f,canonical(cq.Workplane("XY").box(.1,.2,.3).val())),)))
    saved=ifcopenshell.file.from_string(m.to_string());editor.verify_saved(saved)
    target=saved.by_guid(a_guid);assert target.is_a("IfcPipeSegment") and target.Name=="preserved name" and target.Tag=="tag"
    assert target.Decomposes[0].RelatingObject.GlobalId==parent.GlobalId
    assert saved.by_guid(b_guid).is_a("IfcWall") and saved.by_guid(b_guid).IsTypedBy[0].RelatingType.is_a("IfcWallType")
    assert not target.IsTypedBy and target.Representation
    assert all(saved.by_guid(guid).RelatedObjects==(saved.by_guid(b_guid),) for guid in membership_ids)
    assert saved.by_guid(system.GlobalId).Name=="shared system"
    assert saved.by_id(classification.id()).Identification=="classification"
    assert saved.by_id(document.id()).Identification=="document"
    assert {c.relation_global_id for c in editor.effect().changes if c.change_kind=="relation_detached"}>=set(membership_ids)
    assert not any(c.change_kind in {"created","deleted"} and c.global_id==a_guid for c in editor.effect().changes)


@pytest.mark.asyncio
async def test_real_runtime_preflight_then_mixed_two_family_candidate_and_missing_block(tmp_path):
    import cadquery as cq
    from tests.unit.backend.test_ifc_product_compilation import model, family, canonical, item
    from CAD2BIM_backend.ports.ifc_revision_runtime import IfcRevisionRuntimeError

    m=model();a=placed(m,Name="a");b=placed(m,Name="b");moving=placed(m,xyz=(1.,2.,3.));gone=placed(m)
    oldchild=placed(m);root(m,"IfcRelAggregates",RelatingObject=a,RelatedObjects=[oldchild])
    ids=[x.GlobalId for x in (a,b,moving,gone,oldchild)]
    f1,f2=family(),family(assembly=True)
    rows=(ReplaceProductEditRow(input_row_index=0,product_key="p1",target_global_id=ids[0],family_version_id="f1",invocation={"product_origin":(0.,0.,0.),"length_unit":{}}),
          ReplaceProductEditRow(input_row_index=1,product_key="p2",target_global_id=ids[1],family_version_id="f2",invocation={"product_origin":(0.,0.,0.),"length_unit":{}}),
          PlacementProductEditRow(input_row_index=2,product_key="p3",target_global_id=ids[2],placement=RigidTransform3D(translation_m=(4.,5.,6.))),
          delete_row(gone,3))
    local=command(m,rows,{"f1":f1,"f2":f2})
    targets=IfcProductsEditPreflightRequest(targets=tuple(IfcProductsEditTarget(input_row_index=r.input_row_index,
        product_key=r.product_key,target_global_id=r.target_global_id,action=r.action) for r in rows))
    content=m.to_string().encode();source=BytesIfcCandidate(content,sha256=hashlib.sha256(content).hexdigest())
    runtime=IfcOpenShellRevisionRuntime(python_executable=sys.executable,capacity_root=tmp_path/"capacity")
    async def empty():
        if False: yield
    try:
        preflight=await runtime.execute(InspectExactIfcRevisionRequest(projection="edit_targets",edit_targets_request=targets),source=source)
        assert preflight.edit_targets.accepted
        cmd=local.model_copy(update={"preflight":preflight.edit_targets})
        # Build starts only after the whole target/relationship preflight completed.
        geometry=canonical(cq.Workplane("XY").box(.1,.2,.3).val())
        async def blocks():
            yield IfcProductCompilationBlock(items=(item(f1,geometry,key="p1"),))
            yield IfcProductCompilationBlock(items=(item(f2,None,key="p2"),))
        result=await runtime.execute(EditIfcProductsRevisionRequest(command=cmd,blocks=blocks()),source=source)
        try:
            saved=ifcopenshell.file.from_string((await read_source(result.candidate)).decode())
            assert saved.by_guid(ids[0]).is_a("IfcPipeSegment")
            assert saved.by_guid(ids[1]).is_a("IfcElementAssembly")
            assert len(result.effect.bindings)==3
            root_bindings={binding.input_row_index:binding.global_id for binding in result.effect.bindings
                if binding.parent_component_key is None}
            assert root_bindings=={0:ids[0],1:ids[1]}
            child_bindings=[binding for binding in result.effect.bindings if binding.parent_component_key is not None]
            assert len(child_bindings)==1 and child_bindings[0].global_id not in ids
            assert saved.by_guid(child_bindings[0].global_id).is_a("IfcPipeSegment")
            assert {binding.input_row_index for binding in result.effect.bindings}=={0,1}
            assert len(saved.by_type("IfcRepresentationMap"))==1
            assert {p.input_row_index for p in result.effect.products}=={0,1,2,3}
            assert {c.global_id for c in result.effect.changes if c.change_kind=="deleted"}=={ids[3],ids[4]}
            assert np.allclose(ifcopenshell.util.placement.get_local_placement(saved.by_guid(ids[2]).ObjectPlacement)[:3,3],(4.,5.,6.))
        finally: await result.candidate.aclose()
        with pytest.raises(IfcRevisionRuntimeError) as caught:
            await runtime.execute(EditIfcProductsRevisionRequest(command=cmd,blocks=empty()),source=source)
        assert caught.value.code=="IFC_PRODUCT_COMPILATION_FAILED"
        assert not runtime._handles
        deletion=command(m,[delete_row(gone)])
        deletion_result=await runtime.execute(EditIfcProductsRevisionRequest(command=deletion,blocks=empty()),source=source)
        try:
            assert len(deletion_result.effect.products)==1 and not deletion_result.effect.bindings
            assert [c.global_id for c in deletion_result.effect.changes if c.change_kind=="deleted"]==[ids[3]]
        finally: await deletion_result.candidate.aclose()
    finally: await runtime.close()
