"""Narrow compiler boundary for IFC project geometry basis."""

from __future__ import annotations

from typing import Protocol

from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    IfcGeometryBasisSummary,
    IfcRepresentationContextCatalog,
)
from CAD2BIM_backend.domain.ifc_model import EncodedIfcRevision


class IfcGeometryBasisCompilerPort(Protocol):
    """Compile typed changes without exposing a mutable IFC entity graph."""

    def apply(
        self,
        base_content: bytes,
        command: ConfigureIfcGeometryBasisCommand,
    ) -> EncodedIfcRevision: ...

    def inspect_geometry_basis(
        self,
        content: bytes,
    ) -> IfcGeometryBasisSummary | None: ...

    def inspect_contexts(
        self,
        content: bytes,
    ) -> IfcRepresentationContextCatalog: ...


__all__ = ["IfcGeometryBasisCompilerPort"]
