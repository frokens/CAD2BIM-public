"""Narrow compiler boundary for IFC Project business metadata."""

from __future__ import annotations

from typing import Protocol

from CAD2BIM_backend.domain.ifc_model import EncodedIfcRevision
from CAD2BIM_backend.domain.ifc_project import IfcProjectSummary, UpdateIfcProjectCommand


class IfcProjectCompilerPort(Protocol):
    """Compile and inspect Project metadata without exposing mutable IFC entities."""

    def apply(
        self,
        base_content: bytes,
        command: UpdateIfcProjectCommand,
    ) -> EncodedIfcRevision: ...

    def inspect_project(self, content: bytes) -> IfcProjectSummary: ...


__all__ = ["IfcProjectCompilerPort"]
