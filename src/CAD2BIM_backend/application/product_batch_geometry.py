"""Shared bounded geometry spool and IFC block projection for product batches."""
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry
from CAD2BIM_backend.domain.ifc_geometry_construction import GeometryConstruction, IfcGeometryRepresentationRoute
from CAD2BIM_backend.domain.ifc_product_compilation import IfcProductCompilationItem, IfcProductCompilationBlock, IfcProductFragmentSource
from CAD2BIM_backend.domain.runtime_execution import MAX_PRODUCT_BATCH_OUTPUT_BYTES
from CAD2BIM_backend.domain.product_realization import ResolvedMappedGeometryBinding
from CAD2BIM_backend.domain.ifc_model import IfcModelIntegrityError

_IFC_BLOCK_ITEMS = 32
_IFC_BLOCK_BYTES = 64 * 1024**2

class ProductBatchGeometryError(RuntimeError):
    def __init__(self, code, message, *, phase):
        self.code, self.phase = code, phase
        super().__init__(message)

@dataclass(slots=True)
class ProductGeometrySource:
    path: Path
    metadata: dict
    size_bytes: int
    route: IfcGeometryRepresentationRoute
    construction_path: Path | None = None
    construction_size_bytes: int = 0


class ProductGeometrySpool:
    """One operation's checked sources; cache and IFC compiler share files."""

    def __init__(self, root: Path):
        self.root, self.byte_count = root, 0
        self._sources, self._requests, self._files = {}, {}, {}
        self._memory, self._memory_bytes = OrderedDict(), 0

    @property
    def spool_directory(self):
        return self.root

    @property
    def spool_size_bytes(self):
        return self.byte_count

    def reserve_spool_file(self, path, size_bytes):
        """Bound a unique physical file before Host writes it; this is not a cache hit."""
        if path.parent != self.root or size_bytes < 0:
            raise ValueError("Invalid private source spool file")
        if path in self._files:
            if self._files[path] != size_bytes or not path.exists() or path.stat().st_size != size_bytes:
                raise ValueError("Private source spool file size changed")
            return False
        exists = path.exists()
        if exists and path.stat().st_size != size_bytes:
            raise ValueError("Private source spool file size differs")
        if self.byte_count + size_bytes > MAX_PRODUCT_BATCH_OUTPUT_BYTES:
            raise ProductBatchGeometryError("PRODUCT_GEOMETRY_SPOOL_LIMIT", "Private canonical and construction spool exceeds its byte budget", phase="geometry_instantiation")
        self._files[path] = size_bytes
        self.byte_count += size_bytes
        return not exists

    def store(self, geometry, construction, route):
        identity = (geometry.sha256, route)
        if identity in self._sources:
            return self._sources[identity]
        content = construction.model_dump_json().encode("utf-8") if construction is not None else b""
        path = self.root / f"geometry-{geometry.sha256}.brep"
        if self.reserve_spool_file(path, len(geometry.payload)):
            path.write_bytes(geometry.payload)
        construction_path = None
        if content:
            construction_path = self.root / f"construction-{hashlib.sha256(content).hexdigest()}.json"
            if self.reserve_spool_file(construction_path, len(content)):
                construction_path.write_bytes(content)
        source = ProductGeometrySource(path, geometry.model_dump(exclude={"payload"}), len(geometry.payload),
            route, construction_path, len(content))
        self._sources[identity] = source
        return source

    def get(self, key):
        entry = self._requests.get(key)
        if entry is None:
            return None
        source, parameters = entry
        cached = self._memory.get(key)
        if cached is not None:
            self._memory.move_to_end(key)
            return cached
        geometry = CanonicalGeometry(**source.metadata, payload=source.path.read_bytes())
        construction = (GeometryConstruction.model_validate_json(source.construction_path.read_bytes())
                        if source.construction_path is not None else None)
        self._remember(key, source, geometry, construction, parameters)
        return geometry, construction, parameters

    def _remember(self, key, source, geometry, construction, parameters):
        size = source.size_bytes + source.construction_size_bytes
        if size > 16 * 1024**2:
            return
        if key in self._memory:
            self._memory.move_to_end(key)
            return
        while self._memory and (len(self._memory) >= 128 or self._memory_bytes + size > 16 * 1024**2):
            evicted, _ = self._memory.popitem(last=False)
            previous, _ = self._requests[evicted]
            self._memory_bytes -= previous.size_bytes + previous.construction_size_bytes
        self._memory[key] = (geometry, construction.model_copy(deep=True) if construction is not None else None, parameters)
        self._memory_bytes += size

    def put(self, key, geometry, construction, route, parameters):
        source = self.store(geometry, construction, route)
        parameters = json.loads(json.dumps(parameters, ensure_ascii=False, allow_nan=False))
        self._requests[key] = (source, parameters)
        self._remember(key, source, geometry, construction, parameters)


def fragment_specs_for_product(family, request, built, source_cache, sources, *, namespace=""):
    local_bindings = {binding.binding_key: binding for binding in request.geometry_bindings}
    exact_ids = {binding.binding_key: binding.geometry_resource_version_id for binding in family.geometry_bindings}
    specs = []
    for fragment in built.realization.proposal.fragments:
        binding = local_bindings[fragment.binding_key]
        route = binding.ifc_representation_route
        receipt = next(item for item in built.realization.instance_receipts if item.fragment_key == fragment.fragment_key)
        if isinstance(binding, ResolvedMappedGeometryBinding):
            geometry, mapping_scale = binding.geometry, receipt.uniform_scale
            key = f"{namespace}mapped:{exact_ids[fragment.binding_key]}:{route.value}"
        else:
            geometry, mapping_scale = built.instance_geometries[fragment.fragment_key], 1.0
            key = f"{namespace}function:{exact_ids[fragment.binding_key]}:{route.value}:{geometry.sha256}"
        if geometry is None:
            raise IfcModelIntegrityError("selected exact representation source is missing")
        construction = (built.instance_constructions or {}).get(fragment.fragment_key)
        if route is IfcGeometryRepresentationRoute.PARAMETRIC and construction is None:
            raise IfcModelIntegrityError("selected parametric source omitted same-execution construction")
        if route is not IfcGeometryRepresentationRoute.PARAMETRIC and construction is not None:
            raise IfcModelIntegrityError("non-parametric source returned unrequested construction")
        if key not in sources:
            sources[key] = source_cache.store(geometry, construction, route)
        specs.append((fragment.fragment_key, key, mapping_scale,
                      (built.instance_source_transforms or {}).get(fragment.fragment_key)))
    return specs


async def compilation_blocks(rows, items, sources, check_budget):
    seen, block, size = set(), [], 0
    for index, result, fragment_specs in items:
        check_budget()
        fragments = []
        for fragment_key, source_key, scale, _ in fragment_specs:
            geometry = None
            source = sources[source_key]
            construction = None
            if source_key not in seen:
                geometry = CanonicalGeometry(**source.metadata, payload=source.path.read_bytes())
                if source.construction_path is not None:
                    construction = GeometryConstruction.model_validate_json(source.construction_path.read_bytes())
                seen.add(source_key)
            fragments.append(IfcProductFragmentSource(fragment_key=fragment_key, source_key=source_key,
                                                      geometry=geometry, mapping_scale=scale,
                                                      representation_route=source.route, construction=construction))
        item = IfcProductCompilationItem.from_realization(product_key=rows["products"][index]["product_key"],
                     input_row_index=index, realization=result, fragment_sources=tuple(fragments),
                     source_to_fragment_transforms={key: transform for key, _, _, transform in fragment_specs if transform is not None})
        item_size = len(item.model_dump_json().encode())
        if block and (len(block) >= _IFC_BLOCK_ITEMS or size + item_size > _IFC_BLOCK_BYTES):
            yield IfcProductCompilationBlock(items=tuple(block))
            block, size = [], 0
        block.append(item)
        size += item_size
    if block:
        yield IfcProductCompilationBlock(items=tuple(block))
