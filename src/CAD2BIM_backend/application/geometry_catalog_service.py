"""Ready-only Geometry Catalog reads with authenticated opaque references."""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError

from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService
from CAD2BIM_backend.application.catalog_scope import catalog_scope_digest
from CAD2BIM_backend.domain.geometry_catalog import (
    GeometryCatalogAssetSummary,
    GeometryCatalogInfoResult,
    GeometryCatalogIntegrityError,
    GeometryCatalogListQuery,
    GeometryCatalogListResult,
    GeometryCatalogNotFoundError,
    GeometryCatalogReferenceError,
    GeometryCatalogResourceSummary,
    GeometryCatalogRootInfo,
    GeometryCatalogVersionInfo,
    GeometryCatalogVersionSummary,
    GeometryResourceVersionListQuery,
)
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
    GeometryResourceListQuery,
    GeometryResourceRecord,
    GeometryResourceVersionBundle,
)
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.ports.opaque_cursor import OpaqueCursorCodecError, OpaqueCursorCodecPort


_REFERENCE_DOMAIN = "geometry_catalog_reference"
_CURSOR_DOMAIN = "geometry_catalog_list_cursor"
_RECENT_VERSION_LIMIT = 20


class GeometryCatalogService:
    """Project durable RLS-visible resource identities into provider-safe references."""

    def __init__(
        self,
        resources: GeometryResourceService,
        cursor_codec: OpaqueCursorCodecPort,
    ) -> None:
        self._resources = resources
        self._cursor_codec = cursor_codec

    async def delete(self, scope: TenantExecutionContext, target_ref: str) -> None:
        claims = self._decode_reference(scope, target_ref)
        if claims["reference_kind"] != "resource_root":
            raise ValueError("deletion requires a Geometry root reference")
        if not await self._resources.delete_resource(scope, resource_id=claims["resource_id"]):
            raise GeometryCatalogNotFoundError("geometry resource was not found")

    async def list(
        self,
        scope: TenantExecutionContext,
        query: GeometryCatalogListQuery | None,
        cursor: str | None,
    ) -> GeometryCatalogListResult:
        if (query is None) == (cursor is None):
            raise ValueError("exactly one of geometry Catalog query or cursor is required")
        if cursor is None:
            assert query is not None
            boundary_created_at = None
            boundary_resource_id = None
            effective_query = query
        else:
            effective_query, boundary_created_at, boundary_resource_id = self._decode_list_cursor(
                scope, cursor
            )

        page = await self._resources.list_ready_resources(
            scope,
            query=GeometryResourceListQuery(
                resource_kinds=effective_query.resource_kinds,
                before_created_at=boundary_created_at,
                before_resource_id=boundary_resource_id,
                page_size=effective_query.page_size,
            ),
        )
        summaries: list[GeometryCatalogResourceSummary] = []
        for resource in page.resources:
            latest = await self._resources.get_latest_version(
                scope,
                resource_id=resource.resource_id,
            )
            if latest is None:
                raise GeometryCatalogIntegrityError(
                    "a ready geometry resource root has no readable version"
                )
            summaries.append(self._resource_summary(scope, resource, latest))

        next_cursor = None
        if not page.complete:
            assert page.next_before_created_at is not None
            assert page.next_before_resource_id is not None
            next_cursor = self._encode(
                {
                    "domain": _CURSOR_DOMAIN,
                    "scope": geometry_catalog_scope_digest(scope),
                    "query": effective_query.model_dump(mode="json"),
                    "before_created_at": page.next_before_created_at.isoformat(),
                    "before_resource_id": page.next_before_resource_id,
                }
            )
        return GeometryCatalogListResult(
            resources=tuple(summaries),
            returned_count=len(summaries),
            next_cursor=next_cursor,
        )

    async def info(
        self,
        scope: TenantExecutionContext,
        target_ref: str,
    ) -> GeometryCatalogInfoResult:
        claims = self._decode_reference(scope, target_ref)
        resource_id = claims["resource_id"]
        if claims["reference_kind"] == "resource_root":
            resource = await self._resources.get_resource(scope, resource_id=resource_id)
            if resource is None:
                raise GeometryCatalogNotFoundError("geometry resource was not found")
            versions = await self._resources.list_versions(
                scope,
                query=GeometryResourceVersionListQuery(
                    resource_id=resource_id,
                    page_size=_RECENT_VERSION_LIMIT,
                ),
            )
            if not versions.versions:
                raise GeometryCatalogNotFoundError("geometry resource was not found")
            summaries = tuple(self._version_summary(scope, item) for item in versions.versions)
            return GeometryCatalogRootInfo(
                resource=self._resource_summary(scope, resource, versions.versions[0]),
                recent_versions=summaries,
                recent_versions_complete=versions.complete,
            )

        bundle = await self._resources.get_exact_version(
            scope,
            resource_id=resource_id,
            resource_version_id=claims["resource_version_id"],
        )
        if bundle is None:
            raise GeometryCatalogNotFoundError("geometry resource version was not found")
        return self._version_info(scope, bundle)

    async def resolve_exact_version(
        self,
        scope: TenantExecutionContext,
        resource_version_ref: str,
    ) -> GeometryResourceVersionBundle:
        """Resolve a safe exact-version reference through a fresh RLS exact read."""

        claims = self._decode_reference(scope, resource_version_ref)
        if claims["reference_kind"] != "resource_version":
            raise GeometryCatalogReferenceError(
                "an exact geometry resource version reference is required"
            )
        bundle = await self._resources.get_exact_version(
            scope,
            resource_id=claims["resource_id"],
            resource_version_id=claims["resource_version_id"],
        )
        if bundle is None:
            raise GeometryCatalogNotFoundError("geometry resource version was not found")
        return bundle

    def version_ref(
        self,
        scope: TenantExecutionContext,
        bundle: GeometryResourceVersionBundle,
    ) -> str:
        """Issue a safe projection for a version already resolved in this scope."""

        return self._reference(
            scope,
            reference_kind="resource_version",
            resource_id=bundle.resource.resource_id,
            resource_version_id=bundle.version.resource_version_id,
        )

    def _resource_summary(
        self,
        scope: TenantExecutionContext,
        resource: GeometryResourceRecord,
        latest: GeometryResourceVersionBundle,
    ) -> GeometryCatalogResourceSummary:
        if latest.resource != resource:
            raise GeometryCatalogIntegrityError(
                "latest geometry version belongs to a different resource root"
            )
        return GeometryCatalogResourceSummary(
            resource_root_ref=self._reference(
                scope,
                reference_kind="resource_root",
                resource_id=resource.resource_id,
            ),
            resource_kind=resource.resource_kind,
            owner_scope=resource.owner_scope.scope_kind,
            display_name=resource.display_name,
            latest_version=self._version_summary(scope, latest),
            created_at=resource.created_at,
        )

    def _version_summary(
        self,
        scope: TenantExecutionContext,
        bundle: GeometryResourceVersionBundle,
    ) -> GeometryCatalogVersionSummary:
        roles = {asset.asset_role for asset in bundle.assets}
        if bundle.resource.resource_kind is GeometryResourceKind.MAPPED_MODEL:
            preview_available = GeometryResourceAssetRole.CANONICAL_GEOMETRY in roles
        else:
            preview_available = {
                GeometryResourceAssetRole.SOURCE_BUNDLE,
                GeometryResourceAssetRole.PARAMETER_SCHEMA,
            }.issubset(roles)
        return GeometryCatalogVersionSummary(
            resource_version_ref=self.version_ref(scope, bundle),
            version_no=bundle.version.version_no,
            origin_kind=bundle.version.origin_kind,
            content_digest=bundle.version.content_digest,
            manifest_digest=bundle.version.manifest_digest,
            source_available=GeometryResourceAssetRole.SOURCE_BUNDLE in roles,
            preview_available=preview_available,
            created_at=bundle.version.created_at,
        )

    def _version_info(
        self,
        scope: TenantExecutionContext,
        bundle: GeometryResourceVersionBundle,
    ) -> GeometryCatalogVersionInfo:
        return GeometryCatalogVersionInfo(
            resource_root_ref=self._reference(
                scope,
                reference_kind="resource_root",
                resource_id=bundle.resource.resource_id,
            ),
            resource_kind=bundle.resource.resource_kind,
            owner_scope=bundle.resource.owner_scope.scope_kind,
            display_name=bundle.resource.display_name,
            version=self._version_summary(scope, bundle),
            manifest=bundle.version.manifest,
            lineage=bundle.version.lineage,
            assets=tuple(
                GeometryCatalogAssetSummary(
                    asset_role=asset.asset_role.value,
                    asset_format=asset.asset_format,
                    filename=asset.filename,
                    content_type=asset.content_type,
                    size_bytes=asset.size_bytes,
                    sha256=asset.sha256,
                )
                for asset in bundle.assets
            ),
        )

    def _reference(
        self,
        scope: TenantExecutionContext,
        *,
        reference_kind: str,
        resource_id: str,
        resource_version_id: str | None = None,
    ) -> str:
        claims: dict[str, object] = {
            "domain": _REFERENCE_DOMAIN,
            "scope": geometry_catalog_scope_digest(scope),
            "reference_kind": reference_kind,
            "resource_id": resource_id,
        }
        if resource_version_id is not None:
            claims["resource_version_id"] = resource_version_id
        return self._encode(claims)

    def _decode_reference(
        self,
        scope: TenantExecutionContext,
        token: str,
    ) -> dict[str, str]:
        try:
            claims = self._cursor_codec.decode(token)
            reference_kind = claims.get("reference_kind")
            expected = {"domain", "scope", "reference_kind", "resource_id"}
            if reference_kind == "resource_version":
                expected.add("resource_version_id")
            if (
                set(claims) != expected
                or claims.get("domain") != _REFERENCE_DOMAIN
                or claims.get("scope") != geometry_catalog_scope_digest(scope)
                or reference_kind not in {"resource_root", "resource_version"}
                or not isinstance(claims.get("resource_id"), str)
                or not claims["resource_id"]
                or (
                    reference_kind == "resource_version"
                    and (
                        not isinstance(claims.get("resource_version_id"), str)
                        or not claims["resource_version_id"]
                    )
                )
            ):
                raise ValueError
        except (OpaqueCursorCodecError, KeyError, TypeError, ValueError) as exc:
            raise GeometryCatalogReferenceError(
                "geometry resource reference is invalid; copy resource_alias or "
                "resource_version_alias unchanged from a current-scope geometry "
                "write, list, or info response"
            ) from exc
        return claims  # type: ignore[return-value]

    def _decode_list_cursor(
        self,
        scope: TenantExecutionContext,
        token: str,
    ) -> tuple[GeometryCatalogListQuery, datetime, str]:
        try:
            claims = self._cursor_codec.decode(token)
            if set(claims) != {
                "domain",
                "scope",
                "query",
                "before_created_at",
                "before_resource_id",
            }:
                raise ValueError
            if (
                claims["domain"] != _CURSOR_DOMAIN
                or claims["scope"] != geometry_catalog_scope_digest(scope)
                or not isinstance(claims["before_created_at"], str)
                or not isinstance(claims["before_resource_id"], str)
                or not claims["before_resource_id"]
            ):
                raise ValueError
            query = GeometryCatalogListQuery.model_validate(claims["query"])
            before_created_at = datetime.fromisoformat(claims["before_created_at"])
            if before_created_at.tzinfo is None or before_created_at.utcoffset() is None:
                raise ValueError
        except (
            OpaqueCursorCodecError,
            KeyError,
            TypeError,
            ValueError,
            ValidationError,
        ) as exc:
            raise GeometryCatalogReferenceError("geometry Catalog cursor is invalid") from exc
        return query, before_created_at, claims["before_resource_id"]

    def _encode(self, claims: dict[str, object]) -> str:
        try:
            return self._cursor_codec.encode(claims)
        except OpaqueCursorCodecError as exc:
            raise GeometryCatalogReferenceError(
                "geometry Catalog reference could not be issued"
            ) from exc


def geometry_catalog_scope_digest(scope: TenantExecutionContext) -> str:
    """Compatibility name for the shared reusable-catalog authority digest."""

    return catalog_scope_digest(scope)


__all__ = ["GeometryCatalogService", "geometry_catalog_scope_digest"]
