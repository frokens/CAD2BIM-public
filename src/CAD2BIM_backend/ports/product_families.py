"""Persistence boundary for ready immutable ProductFamily catalog reads."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.product_family_catalog import (
    ProductFamilyProgramSource, ProductFamilyProgramSourcePage, ProductFamilyDiscoveryPage,
    ProductFamilyMetadataPage, ProductFamilyVersionMetadata,
)
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyListQuery,
    ProductFamilyRecord,
    ProductFamilyVersionBundle,
    ProductFamilyVersionListQuery,
)


class ProductFamilyRepository(Protocol):
    """Read side of the shared ProductFamily repository; B3.06 adds atomic publication."""

    async def delete_product_family(
        self, context: TenantExecutionContext, *, family_id: str,
    ) -> bool:
        """Remove an owned root from online use, retaining immutable history."""
        ...

    async def get_product_family(
        self,
        context: TenantExecutionContext,
        *,
        family_id: str,
    ) -> ProductFamilyRecord | None: ...

    async def get_exact_product_family_version(
        self,
        context: TenantExecutionContext,
        *,
        family_id: str,
        family_version_id: str,
    ) -> ProductFamilyVersionBundle | None: ...

    async def get_product_family_version_by_id(
        self,
        context: TenantExecutionContext,
        *,
        family_version_id: str,
    ) -> ProductFamilyVersionBundle | None: ...

    async def get_latest_product_family_version(
        self,
        context: TenantExecutionContext,
        *,
        family_id: str,
    ) -> ProductFamilyVersionBundle | None: ...

    async def list_product_family_versions(
        self,
        context: TenantExecutionContext,
        *,
        query: ProductFamilyVersionListQuery,
    ) -> ProductFamilyMetadataPage: ...

    async def get_product_family_version_metadata(
        self, context: TenantExecutionContext, *, family_id: str, family_version_id: str,
    ) -> ProductFamilyVersionMetadata | None: ...

    async def get_product_family_version_identity(
        self, context: TenantExecutionContext, *, family_id: str, family_version_id: str,
    ) -> str | None: ...

    async def get_latest_product_family_version_id(
        self, context: TenantExecutionContext, *, family_id: str,
    ) -> str | None: ...

    async def list_product_families(
        self,
        context: TenantExecutionContext,
        *,
        query: ProductFamilyListQuery,
    ) -> ProductFamilyDiscoveryPage: ...

    async def get_product_family_program_source(
        self, context: TenantExecutionContext, *, family_version_id: str,
        family_id: str | None = None,
    ) -> ProductFamilyProgramSource | None: ...

    async def list_product_family_program_sources(
        self,
        context: TenantExecutionContext,
        *,
        before_created_at: datetime | None,
        before_family_version_id: str | None,
        limit: int,
    ) -> ProductFamilyProgramSourcePage: ...


__all__ = ["ProductFamilyRepository"]
