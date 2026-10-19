"""Authorized exact source reads for Backend SYSTEM publication."""

from typing import Literal, Protocol

from CAD2BIM_backend.domain.geometry_resource import GeometryResourceVersionBundle
from CAD2BIM_backend.domain.product_family import ProductFamilyVersionBundle


class SystemFamilyPublicationSource(Protocol):
    source_kind: Literal["authorized_catalog", "selected_library"]
    source_reference: str

    async def read_family(
        self, *, family_id: str, family_version_id: str,
    ) -> tuple[ProductFamilyVersionBundle, tuple[bytes, ...]]: ...

    async def read_geometry(
        self, *, resource_id: str, resource_version_id: str,
    ) -> tuple[GeometryResourceVersionBundle, tuple[bytes, ...]]: ...
