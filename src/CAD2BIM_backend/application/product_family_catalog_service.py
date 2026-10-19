"""Ready-only ProductFamily Catalog reads with scope-bound opaque references."""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError

from CAD2BIM_backend.application.catalog_scope import catalog_scope_digest
from CAD2BIM_backend.application.geometry_catalog_service import GeometryCatalogService
from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService
from CAD2BIM_backend.application.product_family_asset_service import (
    ProductFamilyAssetIntegrityError,
    ProductFamilyAssetService,
)
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyListQuery,
    ProductFamilyRecord,
    ProductFamilyVersionBundle,
    ProductFamilyVersionListQuery,
)
from CAD2BIM_backend.domain.product_family_catalog import (
    ProductFamilyProgramSource,
    ProductFamilyVersionMetadata, ProductFamilyStoredRejection,
    ProductFamilyCatalogRejection, ProductFamilyCatalogProgramSourcePage,
    ProductFamilyCatalogAssetSummary,
    ProductFamilyCatalogFamilySummary,
    ProductFamilyCatalogGeometryBindingSummary,
    ProductFamilyCatalogInfoResult,
    ProductFamilyCatalogIntegrityError,
    ProductFamilyCatalogListQuery,
    ProductFamilyCatalogListResult,
    ProductFamilyCatalogNotFoundError,
    ProductFamilyCatalogReferenceError,
    ProductFamilyCatalogRootInfo,
    ProductFamilyCatalogVersionInfo,
    ProductFamilyCatalogVersionSummary,
)
from CAD2BIM_backend.ports.opaque_cursor import OpaqueCursorCodecError, OpaqueCursorCodecPort
from CAD2BIM_backend.ports.product_families import ProductFamilyRepository


_REFERENCE_DOMAIN = "product_family_catalog_reference"
_CURSOR_DOMAIN = "product_family_catalog_list_cursor"


class ProductFamilyCatalogService:
    """Project ProductFamily records without exposing persistent internal identities."""

    def __init__(
        self,
        repository: ProductFamilyRepository,
        geometry_resources: GeometryResourceService,
        geometry_catalog: GeometryCatalogService,
        family_assets: ProductFamilyAssetService,
        cursor_codec: OpaqueCursorCodecPort,
    ) -> None:
        self._repository = repository
        self._geometry_resources = geometry_resources
        self._geometry_catalog = geometry_catalog
        self._family_assets = family_assets
        self._cursor_codec = cursor_codec

    async def delete(self, scope: TenantExecutionContext, target_ref: str) -> None:
        claims = self._decode_reference(scope, target_ref)
        if claims["reference_kind"] != "family_root":
            raise ValueError("deletion requires a ProductFamily root reference")
        if not await self._repository.delete_product_family(scope, family_id=claims["family_id"]):
            raise ProductFamilyCatalogNotFoundError("product family was not found")

    async def list(
        self,
        scope: TenantExecutionContext,
        query: ProductFamilyCatalogListQuery | None,
        cursor: str | None,
    ) -> ProductFamilyCatalogListResult:
        if (query is None) == (cursor is None):
            raise ValueError("exactly one product family query or cursor is required")
        if cursor is None:
            assert query is not None
            effective = query
            before_created_at = None
            before_family_id = None
        else:
            effective, before_created_at, before_family_id = self._decode_list_cursor(scope, cursor)
        page = await self._repository.list_product_families(
            scope,
            query=ProductFamilyListQuery(
                owner_scopes=effective.owner_scopes,
                product_structure_kinds=effective.product_structure_kinds,
                purpose_contains=effective.purpose_contains,
                before_created_at=before_created_at,
                before_family_id=before_family_id,
                page_size=effective.page_size,
            ),
        )
        summaries: list[ProductFamilyCatalogFamilySummary] = []
        for entry in page.entries:
            await self._verify_version_assets(entry.latest_version)
            summaries.append(self._family_summary(scope, entry.family, entry.latest_version))
        next_cursor = None
        if not page.complete:
            assert page.next_before_created_at is not None
            assert page.next_before_family_id is not None
            next_cursor = self._encode(
                {
                    "domain": _CURSOR_DOMAIN,
                    "scope": catalog_scope_digest(scope),
                    "query": effective.model_dump(mode="json"),
                    "before_created_at": page.next_before_created_at.isoformat(),
                    "before_family_id": page.next_before_family_id,
                }
            )
        return ProductFamilyCatalogListResult(
            families=tuple(summaries),
            returned_count=len(summaries),
            next_cursor=next_cursor,
            rejections=self._rejections(scope, page.rejections),
        )

    async def info(
        self,
        scope: TenantExecutionContext,
        target_ref: str,
        *,
        before_version_no: int | None = None,
        page_size: int = 20,
    ) -> ProductFamilyCatalogInfoResult:
        claims = self._decode_reference(scope, target_ref)
        family_id = claims["family_id"]
        if claims["reference_kind"] == "family_root":
            family = await self._repository.get_product_family(scope, family_id=family_id)
            if family is None:
                raise ProductFamilyCatalogNotFoundError("product family was not found")
            versions = await self._repository.list_product_family_versions(
                scope,
                query=ProductFamilyVersionListQuery(
                    family_id=family_id,
                    before_version_no=before_version_no,
                    page_size=page_size,
                ),
            )
            if not versions.versions:
                raise ProductFamilyCatalogNotFoundError("product family was not found")
            if before_version_no is None:
                latest = versions.versions[0]
            else:
                latest_id = await self._repository.get_latest_product_family_version_id(
                    scope, family_id=family_id
                )
                latest = None if latest_id is None else await self._repository.get_product_family_version_metadata(
                    scope, family_id=family_id, family_version_id=latest_id,
                )
                if latest is None:
                    raise ProductFamilyCatalogIntegrityError(
                        "a ready product family root has no readable latest version"
                    )
            return ProductFamilyCatalogRootInfo(
                family=self._metadata_family_summary(scope, family, latest),
                versions=tuple(self._metadata_version_summary(scope, item) for item in versions.versions),
                versions_complete=versions.complete,
                next_before_version_no=versions.next_before_version_no,
            )
        if before_version_no is not None or page_size != 20:
            raise ProductFamilyCatalogReferenceError(
                "version pagination applies only to a family root target"
            )
        bundle = await self._repository.get_exact_product_family_version(
            scope,
            family_id=family_id,
            family_version_id=claims["family_version_id"],
        )
        if bundle is None:
            raise ProductFamilyCatalogNotFoundError("product family version was not found")
        await self._verify_version_assets(bundle)
        return await self._version_info(scope, bundle)

    async def resolve_exact_version(
        self,
        scope: TenantExecutionContext,
        family_version_ref: str,
        *, verify_assets: bool = True,
    ) -> ProductFamilyVersionBundle:
        claims = self._decode_reference(scope, family_version_ref)
        if claims["reference_kind"] != "family_version":
            raise ProductFamilyCatalogReferenceError(
                "an exact product family version reference is required"
            )
        bundle = await self._repository.get_exact_product_family_version(
            scope,
            family_id=claims["family_id"],
            family_version_id=claims["family_version_id"],
        )
        if bundle is None:
            raise ProductFamilyCatalogNotFoundError("product family version was not found")
        if verify_assets:
            await self._verify_version_assets(bundle)
        return bundle

    async def version_ref_for_visible_id(
        self,
        scope: TenantExecutionContext,
        family_version_id: str,
        *, verify_assets: bool = True,
    ) -> str:
        """Reauthorize one stored version identity before issuing an opaque exact reference."""
        bundle = await self.resolve_visible_version(scope, family_version_id, verify_assets=verify_assets)
        return self.version_ref(scope, bundle)

    async def resolve_visible_version(
        self, scope: TenantExecutionContext, family_version_id: str,
        *, verify_assets: bool = True,
    ) -> ProductFamilyVersionBundle:
        """Resolve a persisted exact identity through the same catalog visibility boundary."""
        bundle = await self._repository.get_product_family_version_by_id(
            scope,
            family_version_id=family_version_id,
        )
        if bundle is None:
            raise ProductFamilyCatalogNotFoundError("product family version was not found")
        if verify_assets:
            await self._verify_version_assets(bundle)
        return bundle

    async def resolve_program_source(
        self, scope: TenantExecutionContext, family_version_ref: str,
    ) -> ProductFamilyProgramSource:
        claims = self._decode_reference(scope, family_version_ref)
        if claims["reference_kind"] != "family_version":
            raise ProductFamilyCatalogReferenceError("an exact product family version reference is required")
        source = await self._repository.get_product_family_program_source(
            scope, family_id=claims["family_id"], family_version_id=claims["family_version_id"],
        )
        if source is None:
            raise ProductFamilyCatalogNotFoundError("product family source was not found")
        return source

    def program_source_ref(self, scope: TenantExecutionContext, source: ProductFamilyProgramSource) -> str:
        return self._reference(scope, reference_kind="family_version",
                               family_id=source.family.family_id, family_version_id=source.family_version_id)

    async def program_source_ref_for_visible_id(self, scope: TenantExecutionContext, family_version_id: str) -> str:
        source = await self._repository.get_product_family_program_source(scope, family_version_id=family_version_id)
        if source is None:
            raise ProductFamilyCatalogNotFoundError("product family source was not found")
        return self.program_source_ref(scope, source)

    async def list_program_sources(
        self,
        scope: TenantExecutionContext,
        *,
        before_created_at: datetime | None,
        before_family_version_id: str | None,
        limit: int,
    ) -> ProductFamilyCatalogProgramSourcePage:
        """List authorized current source owners."""

        page = await self._repository.list_product_family_program_sources(
            scope,
            before_created_at=before_created_at,
            before_family_version_id=before_family_version_id,
            limit=limit,
        )
        return ProductFamilyCatalogProgramSourcePage(
            sources=page.sources, rejections=self._rejections(scope, page.rejections),
            complete=page.complete, next_before_created_at=page.next_before_created_at,
            next_before_family_version_id=page.next_before_family_version_id,
        )

    def _rejections(self, scope, rejections: tuple[ProductFamilyStoredRejection, ...]):
        return tuple(ProductFamilyCatalogRejection(
            family_version_ref=self._reference(scope, reference_kind="family_version",
                family_id=item.family.family_id, family_version_id=item.family_version_id),
            version_no=item.version_no, field_path=issue.field_path, reason=issue.reason,
        ) for item in rejections for issue in item.issues)

    def _metadata_version_summary(self, scope, metadata: ProductFamilyVersionMetadata):
        return ProductFamilyCatalogVersionSummary(
            family_version_ref=self._reference(scope, reference_kind="family_version",
                family_id=metadata.family.family_id, family_version_id=metadata.family_version_id),
            version_no=metadata.version_no, display_name=metadata.display_name, purpose=metadata.purpose,
            product_structure_kind=metadata.product_structure_kind,
            contract_digest=metadata.contract_digest, content_digest=metadata.content_digest,
            created_at=metadata.created_at,
        )

    def _metadata_family_summary(self, scope, family, metadata):
        if metadata.family != family:
            raise ProductFamilyCatalogIntegrityError("latest Family metadata belongs to a different root")
        return ProductFamilyCatalogFamilySummary(
            family_root_ref=self._reference(scope, reference_kind="family_root", family_id=family.family_id),
            owner_scope=family.owner_scope.scope_kind,
            latest_version=self._metadata_version_summary(scope, metadata), created_at=family.created_at,
        )


    async def _verify_version_assets(self, bundle: ProductFamilyVersionBundle) -> None:
        try:
            await self._family_assets.verify_bundle(bundle)
        except ProductFamilyAssetIntegrityError as exc:
            raise ProductFamilyCatalogIntegrityError(str(exc)) from exc

    def version_ref(
        self,
        scope: TenantExecutionContext,
        bundle: ProductFamilyVersionBundle,
    ) -> str:
        return self._reference(
            scope,
            reference_kind="family_version",
            family_id=bundle.family.family_id,
            family_version_id=bundle.version.family_version_id,
        )


    def _family_summary(
        self,
        scope: TenantExecutionContext,
        family: ProductFamilyRecord,
        latest: ProductFamilyVersionBundle,
    ) -> ProductFamilyCatalogFamilySummary:
        if latest.family != family:
            raise ProductFamilyCatalogIntegrityError(
                "latest product family version belongs to a different root"
            )
        return ProductFamilyCatalogFamilySummary(
            family_root_ref=self._reference(
                scope, reference_kind="family_root", family_id=family.family_id
            ),
            owner_scope=family.owner_scope.scope_kind,
            latest_version=self._version_summary(scope, latest),
            created_at=family.created_at,
        )

    def _version_summary(
        self,
        scope: TenantExecutionContext,
        bundle: ProductFamilyVersionBundle,
    ) -> ProductFamilyCatalogVersionSummary:
        return ProductFamilyCatalogVersionSummary(
            family_version_ref=self.version_ref(scope, bundle),
            version_no=bundle.version.version_no,
            display_name=bundle.version.display_name,
            purpose=bundle.version.purpose,
            product_structure_kind=bundle.version.product_structure_kind,
            contract_digest=bundle.version.contract_digest,
            content_digest=bundle.version.content_digest,
            created_at=bundle.version.created_at,
        )

    async def _version_info(
        self,
        scope: TenantExecutionContext,
        bundle: ProductFamilyVersionBundle,
    ) -> ProductFamilyCatalogVersionInfo:
        parent_ref = None
        if bundle.version.parent_family_version_id is not None:
            parent_id = await self._repository.get_product_family_version_identity(
                scope,
                family_id=bundle.family.family_id,
                family_version_id=bundle.version.parent_family_version_id,
            )
            if parent_id != bundle.version.parent_family_version_id:
                raise ProductFamilyCatalogIntegrityError(
                    "product family parent version is unreadable"
                )
            parent_ref = self._reference(scope, reference_kind="family_version",
                family_id=bundle.family.family_id, family_version_id=parent_id)
        bindings: list[ProductFamilyCatalogGeometryBindingSummary] = []
        for binding in bundle.geometry_bindings:
            geometry = await self._geometry_resources.get_version_by_id(
                scope,
                resource_version_id=binding.geometry_resource_version_id,
            )
            if (
                geometry is None
                or geometry.resource.resource_id != binding.geometry_resource_id
                or geometry.resource.resource_kind is not binding.expected_resource_kind
                or geometry.version.content_digest != binding.geometry_content_digest
            ):
                raise ProductFamilyCatalogIntegrityError(
                    "product family exact geometry dependency is unreadable or changed"
                )
            bindings.append(
                ProductFamilyCatalogGeometryBindingSummary(
                    binding_key=binding.binding_key,
                    geometry_resource_version_ref=self._geometry_catalog.version_ref(
                        scope, geometry
                    ),
                    geometry_content_digest=binding.geometry_content_digest,
                    expected_resource_kind=binding.expected_resource_kind,
                    geometry_purpose=binding.geometry_purpose,
                )
            )
        return ProductFamilyCatalogVersionInfo(
            family_root_ref=self._reference(
                scope,
                reference_kind="family_root",
                family_id=bundle.family.family_id,
            ),
            family_version_ref=self.version_ref(scope, bundle),
            parent_family_version_ref=parent_ref,
            owner_scope=bundle.family.owner_scope.scope_kind,
            version_no=bundle.version.version_no,
            display_name=bundle.version.display_name,
            purpose=bundle.version.purpose,
            usage_instructions=bundle.version.usage_instructions,
            product_structure_kind=bundle.version.product_structure_kind,
            family_contract=bundle.version.family_contract,
            contract_digest=bundle.version.contract_digest,
            content_digest=bundle.version.content_digest,
            program_entry=bundle.version.program_entry,
            geometry_bindings=tuple(bindings),
            assets=tuple(
                ProductFamilyCatalogAssetSummary(
                    asset_role=asset.asset_role.value,
                    asset_format=asset.asset_format,
                    filename=asset.filename,
                    content_type=asset.content_type,
                    size_bytes=asset.size_bytes,
                    sha256=asset.sha256,
                )
                for asset in bundle.assets
            ),
            created_at=bundle.version.created_at,
        )

    def _reference(
        self,
        scope: TenantExecutionContext,
        *,
        reference_kind: str,
        family_id: str,
        family_version_id: str | None = None,
    ) -> str:
        claims: dict[str, object] = {
            "domain": _REFERENCE_DOMAIN,
            "scope": catalog_scope_digest(scope),
            "reference_kind": reference_kind,
            "family_id": family_id,
        }
        if family_version_id is not None:
            claims["family_version_id"] = family_version_id
        return self._encode(claims)

    def _decode_reference(
        self,
        scope: TenantExecutionContext,
        token: str,
    ) -> dict[str, str]:
        try:
            claims = self._cursor_codec.decode(token)
            kind = claims.get("reference_kind")
            expected = {"domain", "scope", "reference_kind", "family_id"}
            if kind == "family_version":
                expected.add("family_version_id")
            if (
                set(claims) != expected
                or claims.get("domain") != _REFERENCE_DOMAIN
                or claims.get("scope") != catalog_scope_digest(scope)
                or kind not in {"family_root", "family_version"}
                or not isinstance(claims.get("family_id"), str)
                or not claims["family_id"]
                or (
                    kind == "family_version"
                    and (
                        not isinstance(claims.get("family_version_id"), str)
                        or not claims["family_version_id"]
                    )
                )
            ):
                raise ValueError
        except (OpaqueCursorCodecError, KeyError, TypeError, ValueError) as exc:
            raise ProductFamilyCatalogReferenceError(
                "product family reference is invalid; copy family_alias or family_version_alias "
                "unchanged from a current-scope family write, list, or info response"
            ) from exc
        return claims  # type: ignore[return-value]

    def _decode_list_cursor(
        self,
        scope: TenantExecutionContext,
        token: str,
    ) -> tuple[ProductFamilyCatalogListQuery, datetime, str]:
        try:
            claims = self._cursor_codec.decode(token)
            if set(claims) != {
                "domain",
                "scope",
                "query",
                "before_created_at",
                "before_family_id",
            }:
                raise ValueError
            if (
                claims["domain"] != _CURSOR_DOMAIN
                or claims["scope"] != catalog_scope_digest(scope)
                or not isinstance(claims["before_created_at"], str)
                or not isinstance(claims["before_family_id"], str)
                or not claims["before_family_id"]
            ):
                raise ValueError
            query = ProductFamilyCatalogListQuery.model_validate(claims["query"])
            created_at = datetime.fromisoformat(claims["before_created_at"])
            if created_at.tzinfo is None or created_at.utcoffset() is None:
                raise ValueError
        except (
            OpaqueCursorCodecError,
            KeyError,
            TypeError,
            ValueError,
            ValidationError,
        ) as exc:
            raise ProductFamilyCatalogReferenceError(
                "product family Catalog cursor is invalid"
            ) from exc
        return query, created_at, claims["before_family_id"]

    def _encode(self, claims: dict[str, object]) -> str:
        try:
            return self._cursor_codec.encode(claims)
        except OpaqueCursorCodecError as exc:
            raise ProductFamilyCatalogReferenceError(
                "product family Catalog reference could not be issued"
            ) from exc


__all__ = ["ProductFamilyCatalogService"]
