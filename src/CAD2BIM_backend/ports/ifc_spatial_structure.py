"""Narrow compiler boundary for IFC project spatial structure."""

from __future__ import annotations

from typing import Protocol

from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    IfcSpatialStructureCompilation,
    IfcSpatialStructureCatalog,
    IfcSpatialStructureListQuery,
    IfcSpatialStructurePage,
)


class IfcSpatialStructureCompilerPort(Protocol):
    """Compile and inspect spatial facts without exposing mutable IFC entities."""

    def apply(
        self,
        base_content: bytes,
        command: ApplyIfcSpatialStructureCommand,
    ) -> IfcSpatialStructureCompilation: ...

    def inspect_spatial_structure(self, content: bytes) -> IfcSpatialStructureCatalog: ...

    def list_spatial_structure(
        self,
        content: bytes,
        query: IfcSpatialStructureListQuery,
    ) -> IfcSpatialStructurePage: ...


__all__ = ["IfcSpatialStructureCompilerPort"]
