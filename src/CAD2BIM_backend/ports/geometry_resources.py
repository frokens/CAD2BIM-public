"""Persistence boundary for durable reusable geometry resources."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from CAD2BIM_backend.domain.geometry_catalog import (
    GeometryResourceVersionListQuery,
    GeometryResourceVersionPage,
)
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceListQuery,
    GeometryResourcePage,
    GeometryResourceRecord,
    GeometryResourceScope,
    GeometryResourceVersionBundle,
)
from CAD2BIM_backend.domain.identity import TenantExecutionContext


class GeometryResourceRepository(Protocol):
    """One atomic root/version/asset repository using the shared DB transaction path."""

    async def delete_geometry_resource(
        self, context: TenantExecutionContext, *, resource_id: str,
    ) -> bool:
        """Remove an owned root from online use, retaining immutable history."""
        ...

    async def require_geometry_resource_publication_allowed(
        self,
        context: TenantExecutionContext,
        *,
        scope: GeometryResourceScope,
    ) -> None:
        """Fail closed before a caller reads candidate bytes from Storage."""

        ...

    async def publish_geometry_resource_version(
        self,
        context: TenantExecutionContext,
        publication: GeometryResourceVersionBundle,
    ) -> GeometryResourceVersionBundle:
        """Create the root if absent and atomically append one immutable exact version."""

        ...

    async def get_geometry_resource(
        self,
        context: TenantExecutionContext,
        *,
        resource_id: str,
    ) -> GeometryResourceRecord | None: ...

    async def get_exact_geometry_resource_version(
        self,
        context: TenantExecutionContext,
        *,
        resource_id: str,
        resource_version_id: str,
    ) -> GeometryResourceVersionBundle | None: ...

    async def get_geometry_resource_version_by_id(
        self,
        context: TenantExecutionContext,
        *,
        resource_version_id: str,
    ) -> GeometryResourceVersionBundle | None:
        """Resolve one globally unique version through its RLS-visible parent root."""

        ...

    async def get_latest_geometry_resource_version(
        self,
        context: TenantExecutionContext,
        *,
        resource_id: str,
    ) -> GeometryResourceVersionBundle | None: ...

    async def list_geometry_resource_versions(
        self,
        context: TenantExecutionContext,
        *,
        query: GeometryResourceVersionListQuery,
    ) -> GeometryResourceVersionPage: ...

    async def list_geometry_resources(
        self,
        context: TenantExecutionContext,
        *,
        query: GeometryResourceListQuery,
    ) -> GeometryResourcePage: ...

    async def list_executable_program_versions(
        self,
        context: TenantExecutionContext,
        *,
        before_created_at: datetime | None,
        before_resource_version_id: str | None,
        limit: int,
    ) -> tuple[GeometryResourceVersionBundle, ...]:
        """List visible immutable Python program versions across resource roots."""

        ...


__all__ = ["GeometryResourceRepository"]
