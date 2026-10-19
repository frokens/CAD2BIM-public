"""Narrow server-only read boundary for durable IFC revisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from CAD2BIM_backend.domain.ifc_model import (
    ExactIfcRevision,
    IfcRevisionInspection,
    IfcRunIdentity,
)
from CAD2BIM_backend.ports.ifc_revision_runtime import IfcRevisionChunkSource


@dataclass(frozen=True, slots=True)
class VerifiedExactIfcRevision:
    """One exact revision and its independently inspected immutable bytes."""

    exact: ExactIfcRevision
    content: bytes
    inspection: IfcRevisionInspection


class IfcRevisionReaderPort(Protocol):
    """Pin and verify durable IFC revisions without exposing write capabilities."""

    async def pin_current(self, scope: IfcRunIdentity) -> ExactIfcRevision: ...

    async def pin_exact(
        self,
        scope: IfcRunIdentity,
        *,
        revision_id: str,
    ) -> ExactIfcRevision: ...

    async def read_exact(
        self,
        exact: ExactIfcRevision,
    ) -> VerifiedExactIfcRevision: ...

    async def open_exact(
        self,
        exact: ExactIfcRevision,
    ) -> IfcRevisionChunkSource: ...


__all__ = ["IfcRevisionReaderPort", "VerifiedExactIfcRevision"]
