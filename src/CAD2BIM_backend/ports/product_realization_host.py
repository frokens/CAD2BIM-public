"""Outbound read-only parser boundary for Product Realization host facts."""

from __future__ import annotations

from typing import Protocol

from CAD2BIM_backend.domain.product_realization_host import (
    CurrentIfcProductBoundarySelector,
    ResolvedCurrentIfcHostFact,
)


class ProductRealizationHostFactExtractorPort(Protocol):
    """Extract bounded host facts from one already pinned exact IFC payload."""

    def extract(
        self,
        content: bytes,
        selectors: tuple[CurrentIfcProductBoundarySelector, ...],
    ) -> tuple[ResolvedCurrentIfcHostFact, ...]: ...


__all__ = ["ProductRealizationHostFactExtractorPort"]
