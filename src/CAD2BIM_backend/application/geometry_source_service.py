"""Exact source-bundle discovery and bounded UTF-8 reads for ready geometry versions."""

from __future__ import annotations

from CAD2BIM_backend.application.geometry_catalog_service import GeometryCatalogService
from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService
from CAD2BIM_backend.application.text_source_view import (
    TextSourceViewError,
    TextSourceViewPolicy,
    TextSourceViewService,
)
from CAD2BIM_backend.domain.geometry_catalog import (
    GeometryCatalogIntegrityError,
    GeometrySourceBundleError,
    GeometrySourceMember,
    GeometrySourceNotTextError,
    GeometrySourceReadError,
    GeometrySourceReadResult,
    GeometrySourceUnavailableError,
)
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.text_source import TextSourceView
from CAD2BIM_backend.infrastructure.source_text.source_bundle import (
    SourceBundleError,
    SourceBundleMemberNotFoundError,
    SourceBundleMemberNotTextError,
    MAX_SOURCE_BUNDLE_MEMBER_BYTES,
    inspect_source_bundle,
    normalize_source_bundle_path,
)


class GeometrySourceService:
    """Read only source bytes bound to one RLS-visible immutable resource version."""

    def __init__(
        self,
        catalog: GeometryCatalogService,
        resources: GeometryResourceService,
        text_views: TextSourceViewService | None = None,
    ) -> None:
        self._catalog = catalog
        self._resources = resources
        self._text_views = text_views or TextSourceViewService(
            TextSourceViewPolicy(
                maximum_source_bytes=MAX_SOURCE_BUNDLE_MEMBER_BYTES,
                maximum_full_bytes=64 * 1024,
                maximum_projected_bytes=96 * 1024,
            )
        )

    async def read_source(
        self,
        scope: TenantExecutionContext,
        exact_version_ref: str,
        source_path: str | None,
        view: TextSourceView | None,
    ) -> GeometrySourceReadResult:
        if source_path is None and view is not None:
            raise GeometrySourceReadError("source_path is required when a source view is requested")
        effective_view = None if source_path is None else (view or TextSourceView(view_mode="full"))

        bundle = await self._catalog.resolve_exact_version(scope, exact_version_ref)
        if not any(
            asset.asset_role is GeometryResourceAssetRole.SOURCE_BUNDLE for asset in bundle.assets
        ):
            raise GeometrySourceUnavailableError(
                "this geometry resource version has no source bundle"
            )
        exact_asset = await self._resources.read_exact_asset(
            scope,
            resource_id=bundle.resource.resource_id,
            resource_version_id=bundle.version.resource_version_id,
            asset_role=GeometryResourceAssetRole.SOURCE_BUNDLE,
        )
        if exact_asset is None:
            raise GeometryCatalogIntegrityError(
                "the source asset disappeared from an exact geometry resource version"
            )
        asset, content = exact_asset
        if len(content) != asset.size_bytes:
            raise GeometryCatalogIntegrityError(
                "the exact geometry source bundle failed digest verification"
            )

        try:
            sources = inspect_source_bundle(content)
        except SourceBundleError as exc:
            raise GeometrySourceBundleError(str(exc)) from exc
        members = tuple(
            GeometrySourceMember(
                source_path=member.path,
                size_bytes=member.size_bytes,
                sha256=member.sha256,
                text_available=member.text_available,
            )
            for member in sources.members
        )
        projected = None
        if source_path is not None:
            try:
                normalized_path = normalize_source_bundle_path(source_path)
                text = sources.read_text(normalized_path)
            except SourceBundleMemberNotTextError as exc:
                raise GeometrySourceNotTextError(str(exc)) from exc
            except SourceBundleMemberNotFoundError as exc:
                raise GeometrySourceReadError(str(exc)) from exc
            except SourceBundleError as exc:
                raise GeometrySourceReadError(str(exc)) from exc
            assert effective_view is not None
            try:
                projected = self._text_views.view(text, effective_view)
            except TextSourceViewError as exc:
                raise GeometrySourceReadError(str(exc)) from exc
            source_path = normalized_path

        return GeometrySourceReadResult(
            resource_version_ref=exact_version_ref,
            bundle_sha256=asset.sha256,
            members=members,
            selected_path=source_path,
            view=projected,
        )


__all__ = ["GeometrySourceService"]
