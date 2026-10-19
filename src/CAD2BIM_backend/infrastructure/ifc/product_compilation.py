"""Exact-source representation maps and product assembly in one private IFC model."""
from __future__ import annotations

import hashlib
import math
import time
from typing import Any

import ifcopenshell.guid
import ifcopenshell.util.unit
import numpy as np

from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometryFormat
from CAD2BIM_backend.domain.ifc_geometry_basis import (
    IfcRepresentationContextNotFoundError, IfcRepresentationContextIncompatibleError,
)
from CAD2BIM_backend.domain.ifc_model import IFC_TARGET_SCHEMA, IfcRevisionInspection
from CAD2BIM_backend.domain.ifc_product_compilation import (
    CompileIfcProductsCommand, IfcProductBodyPreflight, IfcProductCompilationBlock,
    IFC_PRODUCT_LINEAR_DEFLECTION_M, preflight_product_body_facts,
    IfcProductCompilationError, IfcProductComponentBinding, IfcProductRevisionEffect,
)
from CAD2BIM_backend.domain.ifc_geometry_construction import IfcGeometryRepresentationRoute
from CAD2BIM_backend.infrastructure.ifc.geometry_basis import IfcOpenShellGeometryBasisCompiler
from CAD2BIM_backend.infrastructure.worker_runtime.timing import timed_phase


def _indexed_native_shells(representation: Any, project_length_unit_m: float) -> list[tuple[list[tuple[float, ...]], list[tuple[int, ...]]]]:
    """Keep every native triangle and point identity within its original shell.

    Native FBSM is an intermediate mesh only. Unsupported loops fail rather
    than introducing a second triangulator or changing the native mesh.
    """
    items = representation.Items
    if (representation.RepresentationType != "SurfaceModel" or not items
            or any(not item.is_a("IfcFaceBasedSurfaceModel") for item in items)):
        raise ValueError("native tesselate returned an unsupported intermediate mesh")
    shells = []
    # Read the native forward attributes directly. The public attribute wrapper
    # otherwise recreates Python entity objects recursively for each traversal.
    # The checked IFC classes fix these attribute positions; keep the original
    # representation alive while traversing its detached native mesh entities.
    for item in items:
        native_shells = item.wrapped_data.get_argument(0)  # FbsmFaces
        if not native_shells:
            raise ValueError("native mesh item has no shells")
        for shell in native_shells:
            if not shell.is_a("IfcConnectedFaceSet"):
                raise ValueError("native mesh shell has no faces")
            faces = shell.get_argument(0)  # CfsFaces
            if not faces:
                raise ValueError("native mesh shell has no faces")
            lookup: dict[int, int] = {}
            coordinates: list[tuple[float, ...]] = []
            triangles: list[tuple[int, ...]] = []
            for face in faces:
                if not face.is_a("IfcFace"):
                    raise ValueError("native mesh face must have exactly one triangle bound")
                bounds = face.get_argument(0)  # Bounds
                if len(bounds or ()) != 1:
                    raise ValueError("native mesh face must have exactly one triangle bound")
                bound = bounds[0]
                if not bound.is_a("IfcFaceBound"):
                    raise ValueError("native mesh bound must be an oriented three-point polyloop")
                loop = bound.get_argument(0)  # Bound
                orientation = bound.get_argument(1)  # Orientation
                if (loop is None or not loop.is_a("IfcPolyLoop")
                        or orientation is None):
                    raise ValueError("native mesh bound must be an oriented three-point polyloop")
                points = loop.get_argument(0)  # Polygon
                if len(points or ()) != 3:
                    raise ValueError("native mesh bound must be an oriented three-point polyloop")
                if not orientation:
                    points = tuple(reversed(points))
                indices = []
                for point in points:
                    # Native entities need not belong to a file: their STEP
                    # numbers may all be zero, while wrapped identities differ.
                    identity = point.identity()
                    index = lookup.get(identity)
                    if index is None:
                        if not point.is_a("IfcCartesianPoint"):
                            raise ValueError("native mesh point must contain three finite coordinates")
                        values = tuple(float(v) for v in point.get_argument(0))  # Coordinates
                        if len(values) != 3 or not all(math.isfinite(v) for v in values):
                            raise ValueError("native mesh point must contain three finite coordinates")
                        values = tuple(v / project_length_unit_m for v in values)
                        if not all(math.isfinite(v) for v in values):
                            raise ValueError("native mesh point exceeds project coordinate range")
                        index = len(coordinates) + 1
                        lookup[identity] = index
                        coordinates.append(values)
                    indices.append(index)
                # Repeated indices and degenerate triangles are native facts.
                triangles.append(tuple(indices))
            shells.append((coordinates, triangles))
    return shells


def _mapping_target_signature(mapped: Any) -> tuple[Any, ...]:
    target = mapped.MappingTarget
    return (mapped.MappingSource.id(), float(target.Scale),
            tuple(target.LocalOrigin.Coordinates),
            tuple(target.Axis1.DirectionRatios), tuple(target.Axis2.DirectionRatios),
            tuple(target.Axis3.DirectionRatios))


def _mapping_origin_signature(origin: Any) -> tuple[Any, ...]:
    return (tuple(origin.Location.Coordinates), tuple(origin.Axis.DirectionRatios),
            tuple(origin.RefDirection.DirectionRatios))


def preflight_product_body(model: Any, inspection: IfcRevisionInspection | None = None) -> tuple[Any, IfcProductBodyPreflight]:
    if model.schema_identifier != IFC_TARGET_SCHEMA:
        raise IfcRepresentationContextIncompatibleError("Body", "target must be IFC4X3_ADD2")
    basis = (inspection.geometry_basis if inspection is not None
             else IfcOpenShellGeometryBasisCompiler().inspect_geometry_basis_from_model(model))
    facts = (inspection.product_body_contexts if inspection is not None else
             IfcOpenShellGeometryBasisCompiler.inspect_product_body_contexts_from_model(model))
    unit = (inspection.project_length_unit_m if inspection is not None else
            float(ifcopenshell.util.unit.calculate_unit_scale(model)) if basis is not None else None)
    preflight = preflight_product_body_facts(facts, basis, unit)
    body = next(c for c in model.by_type("IfcGeometricRepresentationSubContext")
                if c.ContextIdentifier == "Body")
    return body, preflight


class IfcOpenShellProductCompiler:
    """One candidate owns one context, model, source-map and occurrence index."""

    def __init__(self, model: Any, command: CompileIfcProductsCommand, *,
                 target_roots: dict[str, Any] | None = None,
                 source_maps: dict[str, Any] | None = None) -> None:
        self.model = model
        self.command = command
        self.body_context, self.body = preflight_product_body(model)
        self._maps: dict[str, tuple[str, IfcGeometryRepresentationRoute, Any]] = source_maps if source_maps is not None else {}
        self._target_roots = target_roots or {}
        self._expected_names: dict[str, str | None] = {}
        self._expected_parents: dict[str, tuple[tuple[str, str], ...]] = {}
        self._products: set[str] = set()
        self._bindings: list[IfcProductComponentBinding] = []
        self._world_frames: dict[str, RigidTransform3D] = {}
        self._expected_bodies: dict[str, tuple[tuple[Any, ...], ...]] = {}
        self._components = {c.component_key: c for c in command.family_contract.components}
        self._owners = {f.fragment_key: f.owner_component_key
                        for f in command.family_contract.geometry_fragments}
        self._mapped_count = self._triangles = self._blocks = 0
        self._representation_map_seconds = self._assembly_seconds = 0.0
        self._origin = self._axis_placement(RigidTransform3D())

    def consume(self, block: IfcProductCompilationBlock) -> None:
        self._blocks += 1
        for item in block.items:
            try:
                with timed_phase("product_assembly_including_tesselate", input_row_index=item.input_row_index,
                                 component_count=len(self._components), fragment_count=len(item.fragment_sources)):
                    self._consume_item(item)
            except IfcProductCompilationError:
                raise
            except Exception as exc:
                raise IfcProductCompilationError(
                    str(exc), product_key=item.product_key,
                    input_row_index=item.input_row_index, phase="representation_or_assembly",
                ) from exc

    def _consume_item(self, item: Any) -> None:
        from CAD2BIM_backend.infrastructure.ifc.placement import set_project_world_matrix

        started = time.monotonic()
        if item.product_key in self._products:
            raise ValueError("product_key is duplicated in this candidate")
        self._products.add(item.product_key)
        worlds = {c.component_key: c.project_world_transform
                  for c in item.component_project_world_transforms}
        sources = {f.fragment_key: f for f in item.fragment_sources}
        if (len(worlds) != len(item.component_project_world_transforms)
                or set(worlds) != set(self._components) or set(sources) != set(self._owners)):
            raise ValueError("realization component/fragment closure differs from the Family")
        if len(sources) != len(item.fragment_sources):
            raise ValueError("representation sources do not exactly match realization fragments")
        entities: dict[str, Any] = {}
        for key, contract in self._components.items():
            entity = self._target_roots.get(item.product_key) if contract.parent_component_key is None else None
            if entity is None:
                entity = self.model.create_entity(contract.ifc_class.value,
                    GlobalId=ifcopenshell.guid.new(), Name=key)
            elif entity.is_a() != contract.ifc_class.value or entity.Representation is not None:
                raise ValueError("replacement root must have the Family class and cleared old representation")
            self._expected_names[entity.GlobalId] = entity.Name
            entities[key] = entity
            self._bindings.append(IfcProductComponentBinding(
                product_key=item.product_key, input_row_index=item.input_row_index,
                component_key=key, parent_component_key=contract.parent_component_key,
                global_id=entity.GlobalId))
        children: dict[str, list[Any]] = {}
        for key, contract in self._components.items():
            if contract.parent_component_key is not None:
                children.setdefault(contract.parent_component_key, []).append(entities[key])
        for parent, owned in children.items():
            self.model.create_entity("IfcRelAggregates", GlobalId=ifcopenshell.guid.new(),
                                     RelatingObject=entities[parent], RelatedObjects=owned)
        for entity in entities.values():
            self._expected_parents[entity.GlobalId] = tuple(sorted(
                (r.is_a(), r.RelatingObject.GlobalId)
                for r in (*entity.Decomposes, *entity.Nests)
                if r.is_a("IfcRelAggregates") or r.is_a("IfcRelNests")))
        pending = set(entities)
        while pending:
            ready = [key for key in pending if self._components[key].parent_component_key not in pending]
            if not ready:
                raise ValueError("Family component tree is cyclic")
            for key in ready:
                frame = worlds[key]
                self._world_frames[entities[key].GlobalId] = frame
                matrix = np.eye(4)
                matrix[:3, :3] = frame.rotation
                matrix[:3, 3] = np.asarray(frame.translation_m) / self.body.project_length_unit_m
                set_project_world_matrix(self.model, entities[key], matrix,
                                         should_transform_children=False)
                pending.remove(key)
        items_by_owner: dict[str, list[Any]] = {}
        for key, source in sources.items():
            representation_map = self._representation_map(source)
            frame = source.local_transform or RigidTransform3D()
            rotation = np.asarray(frame.rotation)
            target = self.model.create_entity("IfcCartesianTransformationOperator3D",
                Axis1=self._direction(rotation[:, 0]), Axis2=self._direction(rotation[:, 1]),
                LocalOrigin=self.model.create_entity("IfcCartesianPoint",
                    Coordinates=tuple(float(v) / self.body.project_length_unit_m for v in frame.translation_m)),
                Scale=float(source.mapping_scale), Axis3=self._direction(rotation[:, 2]))
            mapped = self.model.create_entity("IfcMappedItem", MappingSource=representation_map,
                                             MappingTarget=target)
            items_by_owner.setdefault(self._owners[key], []).append(mapped)
            self._mapped_count += 1
        for key, mapped_items in items_by_owner.items():
            representation = self.model.create_entity("IfcShapeRepresentation",
                ContextOfItems=self.body_context, RepresentationIdentifier="Body",
                RepresentationType="MappedRepresentation", Items=mapped_items)
            entities[key].Representation = self.model.create_entity("IfcProductDefinitionShape",
                                                                    Representations=[representation])
            self._expected_bodies[entities[key].GlobalId] = tuple(
                _mapping_target_signature(mapped) for mapped in mapped_items)
        self._assembly_seconds += time.monotonic() - started

    def _representation_map(self, source: Any) -> Any:
        existing = self._maps.get(source.source_key)
        geometry = source.geometry
        if existing is not None:
            if source.representation_route is not existing[1]:
                raise ValueError("same exact representation source_key changed IFC route")
            if geometry is not None and (geometry.sha256 != existing[0]
                    or hashlib.sha256(geometry.payload).hexdigest() != existing[0]):
                raise ValueError("same exact representation source_key changed canonical bytes")
            return existing[2]
        if geometry is None:
            raise ValueError("first use of a representation source requires canonical bytes")
        if geometry.format is not CanonicalGeometryFormat.OCCT_BREP_V1:
            raise ValueError("native IFC product compiler supports canonical OCCT BREP only")
        if hashlib.sha256(geometry.payload).hexdigest() != geometry.sha256:
            raise ValueError("canonical BREP does not match its exact byte identity")
        started = time.monotonic()
        route = source.representation_route
        if route is IfcGeometryRepresentationRoute.TFS:
            if source.construction is not None:
                raise ValueError("TFS source cannot carry a parametric construction")
            imported = self._tessellation(geometry.payload)
        elif route is IfcGeometryRepresentationRoute.ADVANCED_BREP:
            if source.construction is not None:
                raise ValueError("Advanced source cannot carry a parametric construction")
            # CadQuery/OCP load only for this selected route. The encoder adds
            # each solid's inner map to this same private IFC model.
            from CAD2BIM_backend.infrastructure.ifc.advanced_brep_encoding import encode_advanced_brep_maps
            with timed_phase("ifc_advanced_brep_encode", brep_bytes=len(geometry.payload)):
                items = encode_advanced_brep_maps(self.model, self.body_context,
                    geometry.payload, self.body.project_length_unit_m)
            imported = self.model.create_entity("IfcShapeRepresentation",
                ContextOfItems=self.body_context, RepresentationIdentifier="Body",
                RepresentationType="MappedRepresentation", Items=items)
        elif route is IfcGeometryRepresentationRoute.PARAMETRIC:
            if source.construction is None:
                raise ValueError("parametric source requires its same-execution construction")
            with timed_phase("ifc_parametric_dependencies"):
                from CAD2BIM_backend.infrastructure.ifc.parametric_encoding import encode_parametric_items
            with timed_phase("ifc_parametric_encode", node_count=len(source.construction.nodes)):
                items = encode_parametric_items(self.model, source.construction,
                    self.body.project_length_unit_m)
            imported = self.model.create_entity("IfcShapeRepresentation",
                ContextOfItems=self.body_context, RepresentationIdentifier="Body",
                RepresentationType="SolidModel", Items=items)
        else:
            raise ValueError("unknown IFC representation route")
        mapping = self.model.create_entity("IfcRepresentationMap", MappingOrigin=self._origin,
                                           MappedRepresentation=imported)
        self._maps[source.source_key] = (geometry.sha256, route, mapping)
        self._representation_map_seconds += time.monotonic() - started
        return mapping

    def _tessellation(self, payload: bytes) -> Any:
        import ifcopenshell.geom

        with timed_phase("ifc_native_tesselate", brep_bytes=len(payload),
                         source_coordinate_unit="METRE", linear_deflection=IFC_PRODUCT_LINEAR_DEFLECTION_M):
            native = ifcopenshell.geom.tesselate(IFC_TARGET_SCHEMA, payload.decode("utf-8"),
                                                 IFC_PRODUCT_LINEAR_DEFLECTION_M)
        if native is None or len(native.Representations or ()) != 1:
            raise ValueError("native tesselate returned no unique representation")
        with timed_phase("ifc_indexed_mesh_encode"):
            shells = _indexed_native_shells(native.Representations[0], self.body.project_length_unit_m)
            items = []
            for coordinates, triangles in shells:
                points = self.model.create_entity("IfcCartesianPointList3D", CoordList=coordinates)
                items.append(self.model.create_entity("IfcTriangulatedFaceSet",
                    Coordinates=points, CoordIndex=triangles, Closed=None))
                self._triangles += len(triangles)
            return self.model.create_entity("IfcShapeRepresentation",
                ContextOfItems=self.body_context, RepresentationIdentifier="Body",
                RepresentationType="Tessellation", Items=items)

    def _direction(self, values: Any) -> Any:
        return self.model.create_entity("IfcDirection", DirectionRatios=tuple(float(v) for v in values))

    def _axis_placement(self, frame: RigidTransform3D) -> Any:
        rotation = np.asarray(frame.rotation)
        return self.model.create_entity("IfcAxis2Placement3D",
            Location=self.model.create_entity("IfcCartesianPoint", Coordinates=frame.translation_m),
            Axis=self._direction(rotation[:, 2]), RefDirection=self._direction(rotation[:, 0]))

    def effect(self, *, timings: dict[str, float] | None = None) -> IfcProductRevisionEffect:
        return IfcProductRevisionEffect(bindings=tuple(self._bindings), source_count=len(self._maps),
            mapped_item_count=self._mapped_count, triangle_count=self._triangles,
            block_count=self._blocks, body=self.body,
            timings={"representation_map_build": self._representation_map_seconds,
                     "assembly_including_tesselate": self._assembly_seconds, **(timings or {})})

    def verify_saved(self, model: Any) -> None:
        """Read actual saved products once, without geometry summary or meshing."""
        index = {p.GlobalId: p for p in model.by_type("IfcProduct")}
        bindings = {(b.product_key, b.component_key): b.global_id for b in self._bindings}
        for binding in self._bindings:
            product = index.get(binding.global_id)
            if product is None or product.Name != self._expected_names[binding.global_id]:
                raise ValueError("saved candidate lost a product/component binding")
            if product.ObjectPlacement is None:
                raise ValueError("saved candidate lost a component placement")
            from ifcopenshell.util.placement import get_local_placement
            actual = np.asarray(get_local_placement(product.ObjectPlacement))
            expected = self._world_frames[binding.global_id]
            if (not np.allclose(actual[:3, :3], expected.rotation, rtol=0., atol=1e-9)
                    or not np.allclose(actual[:3, 3] * self.body.project_length_unit_m,
                                       expected.translation_m, rtol=0., atol=1e-9)):
                raise ValueError("saved candidate changed component project-world placement")
            parents = tuple(sorted((r.is_a(), r.RelatingObject.GlobalId)
                            for r in (*product.Decomposes, *product.Nests)
                            if r.is_a("IfcRelAggregates") or r.is_a("IfcRelNests")))
            expected_parent = self._expected_parents[binding.global_id]
            if parents != expected_parent:
                raise ValueError("saved candidate changed the product component aggregate tree")
            expected_body = self._expected_bodies.get(binding.global_id)
            if expected_body is None:
                if product.Representation is not None:
                    raise ValueError("saved candidate duplicated child Body on its aggregate root")
                continue
            representations = tuple(product.Representation.Representations or ()) if product.Representation else ()
            if len(representations) != 1:
                raise ValueError("saved candidate lost its component Body")
            representation = representations[0]
            actual_body = tuple(_mapping_target_signature(i)
                                for i in representation.Items if i.is_a("IfcMappedItem"))
            if (representation.RepresentationIdentifier != "Body"
                    or representation.RepresentationType != "MappedRepresentation"
                    or representation.ContextOfItems.id() != self.body_context.id()
                    or len(actual_body) != len(representation.Items) or actual_body != expected_body):
                raise ValueError("saved candidate changed its mapped component Body")
        for _, route, source_map in self._maps.values():
            saved = model.by_id(source_map.id())
            if (not saved.is_a("IfcRepresentationMap")
                    or _mapping_origin_signature(saved.MappingOrigin) !=
                       _mapping_origin_signature(source_map.MappingOrigin)
                    or saved.MappedRepresentation.RepresentationIdentifier != "Body"
                    or saved.MappedRepresentation.ContextOfItems.id() != self.body_context.id()):
                raise ValueError("saved candidate lost its exact source representation map")
            actual_items = tuple(saved.MappedRepresentation.Items)
            expected_items = tuple(source_map.MappedRepresentation.Items)
            if len(actual_items) != len(expected_items):
                raise ValueError("saved candidate changed its exact source item count")
            if route is IfcGeometryRepresentationRoute.TFS:
                if saved.MappedRepresentation.RepresentationType != "Tessellation":
                    raise ValueError("saved candidate changed its Tessellation route")
                # Exact saved bytes were checked before opening this model.
                # Native triangle/coordinate roundtrip fidelity belongs to the
                # encoder's owner tests, not a full Python mesh copy per Apply.
                # Retain the actual source identities and representation links.
                for actual, expected in zip(actual_items, expected_items):
                    if (not actual.is_a("IfcTriangulatedFaceSet")
                            or actual.id() != expected.id()
                            or not actual.Coordinates.is_a("IfcCartesianPointList3D")
                            or actual.Coordinates.id() != expected.Coordinates.id()):
                        raise ValueError("saved candidate changed its indexed mesh source")
            elif route is IfcGeometryRepresentationRoute.ADVANCED_BREP:
                if saved.MappedRepresentation.RepresentationType != "MappedRepresentation":
                    raise ValueError("saved candidate changed its Advanced mapped route")
                for actual, expected in zip(actual_items, expected_items):
                    if (not actual.is_a("IfcMappedItem") or not expected.is_a("IfcMappedItem")
                            or actual.MappingSource.id() != expected.MappingSource.id()
                            or actual.MappingTarget.Scale != 1.0
                            or actual.MappingTarget.LocalOrigin.Coordinates != expected.MappingTarget.LocalOrigin.Coordinates
                            or actual.MappingTarget.Axis1.DirectionRatios != expected.MappingTarget.Axis1.DirectionRatios
                            or actual.MappingTarget.Axis2.DirectionRatios != expected.MappingTarget.Axis2.DirectionRatios
                            or actual.MappingTarget.Axis3.DirectionRatios != expected.MappingTarget.Axis3.DirectionRatios
                            or _mapping_origin_signature(actual.MappingSource.MappingOrigin) !=
                               _mapping_origin_signature(expected.MappingSource.MappingOrigin)
                            or not actual.MappingSource.MappedRepresentation.is_a("IfcShapeRepresentation")
                            or actual.MappingSource.MappedRepresentation.ContextOfItems.id() != self.body_context.id()
                            or actual.MappingSource.MappedRepresentation.RepresentationType != "AdvancedBrep"
                            or len(actual.MappingSource.MappedRepresentation.Items) != 1
                            or not actual.MappingSource.MappedRepresentation.Items[0].is_a("IfcAdvancedBrep")
                            or len(actual.MappingSource.MappedRepresentation.Items[0].Outer.CfsFaces) !=
                               len(expected.MappingSource.MappedRepresentation.Items[0].Outer.CfsFaces)):
                        raise ValueError("saved candidate changed its nested Advanced source map")
            elif route is IfcGeometryRepresentationRoute.PARAMETRIC:
                if saved.MappedRepresentation.RepresentationType != "SolidModel":
                    raise ValueError("saved candidate changed its parametric SolidModel route")
                for actual, expected in zip(actual_items, expected_items):
                    if (not actual.is_a("IfcSolidModel") or actual.is_a() != expected.is_a()
                            or actual.id() != expected.id()):
                        raise ValueError("saved candidate changed its exact parametric solid items")
