"""Provider-facing application projection for ready ProductFamily Catalog reads."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import ValidationError

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.product_family_catalog import (
    ProductFamilyCatalogError,
    ProductFamilyCatalogListQuery,
    ProductFamilyCatalogRootInfo,
    ProductFamilyCatalogVersionInfo,
)
from CAD2BIM_mcp.direct.application_support import _direct_error
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.product_family.contracts import (
    DirectIfcProductFamilyInfoInput,
    DirectIfcProductFamilyListCursorInput,
    DirectIfcProductFamilyListInput,
    IfcProductFamilyAssetResult,
    IfcProductFamilyCard,
    IfcProductFamilyGeometryBindingResult,
    IfcProductFamilyInfoResult,
    IfcProductFamilyListResult,
    IfcProductFamilyRejection,
    IfcProductFamilyRootInfoResult,
    IfcProductFamilyVersionCard,
    IfcProductFamilyVersionInfoResult,
)

if TYPE_CHECKING:
    from CAD2BIM_backend.application.product_family_catalog_service import (
        ProductFamilyCatalogService,
    )
    from CAD2BIM_backend.domain.invocation import InvocationScope
    from CAD2BIM_backend.domain.product_family_catalog import (
        ProductFamilyCatalogFamilySummary,
        ProductFamilyCatalogRejection,
        ProductFamilyCatalogVersionSummary,
    )


class ProductFamilyProviderApplication(Protocol):
    async def list_ifc_product_families(
        self, request: DirectIfcProductFamilyListInput
    ) -> IfcProductFamilyListResult: ...

    async def get_ifc_product_family_info(
        self, request: DirectIfcProductFamilyInfoInput
    ) -> IfcProductFamilyInfoResult: ...


class IfcProductFamilyApplicationMixin:
    """Thin Direct projection over the entry-neutral ProductFamily Catalog service."""

    async def list_ifc_product_families(
        self,
        request: DirectIfcProductFamilyListInput,
    ) -> IfcProductFamilyListResult:
        async def action(scope: InvocationScope) -> IfcProductFamilyListResult:
            catalog = self._require_product_family_catalog_service()
            if isinstance(request, DirectIfcProductFamilyListCursorInput):
                result = await catalog.list(scope.identity, None, request.cursor)
            else:
                result = await catalog.list(
                    scope.identity,
                    ProductFamilyCatalogListQuery(
                        owner_scopes=request.owner_scopes,
                        product_structure_kinds=request.product_structure_kinds,
                        purpose_contains=request.purpose_contains,
                        page_size=request.page_size,
                    ),
                    None,
                )
            return IfcProductFamilyListResult(
                families=tuple(_family_card(item) for item in result.families),
                rejections=tuple(_family_rejection(item) for item in result.rejections),
                complete=result.next_cursor is None,
                next_cursor=result.next_cursor,
            )

        return await self._product_family_read(request.direct_session_alias, "list", action)

    async def get_ifc_product_family_info(
        self,
        request: DirectIfcProductFamilyInfoInput,
    ) -> IfcProductFamilyInfoResult:
        target = request.target
        if target.target_kind == "draft":
            async def read_draft(scope):
                info = await self._require_product_family_authoring_service().draft_info(scope, draft_ref=target.draft_ref)
                return IfcProductFamilyInfoResult(info=info)
            return await self._product_family_read(request.direct_session_alias, "info", read_draft)
        target_ref = (
            target.family_alias
            if target.target_kind == "family_root"
            else target.family_version_alias
        )

        async def action(scope: InvocationScope) -> IfcProductFamilyInfoResult:
            catalog = self._require_product_family_catalog_service()
            if target.target_kind == "family_root":
                result = await catalog.info(
                    scope.identity,
                    target_ref,
                    before_version_no=target.before_version_no,
                    page_size=target.page_size,
                )
            else:
                result = await catalog.info(scope.identity, target_ref)
            if isinstance(result, ProductFamilyCatalogRootInfo):
                info = IfcProductFamilyRootInfoResult(
                    target_kind="family_root",
                    family=_family_card(result.family),
                    versions=tuple(_version_card(item) for item in result.versions),
                    versions_complete=result.versions_complete,
                    next_before_version_no=result.next_before_version_no,
                )
            elif isinstance(result, ProductFamilyCatalogVersionInfo):
                info = _version_info(result)
                bundle = await catalog.resolve_exact_version(scope.identity, target_ref)
                publication = await self._publication_navigation(scope, root_kind="family",
                    version_id=bundle.version.family_version_id)
                standard = bundle.version.family_contract.standard_model
                info = info.model_copy(update={"publication": publication,
                    "standard_model_available": standard is not None,
                    "standard_public_parameters_json": canonical_json(standard.public_parameters)
                        if standard is not None else None})
            else:  # pragma: no cover - closed Backend result union
                raise ValueError("unexpected ProductFamily Catalog result branch")
            return IfcProductFamilyInfoResult(info=info)

        return await self._product_family_read(request.direct_session_alias, "info", action)

    async def _product_family_read(
        self,
        direct_session_alias: str,
        action_name: str,
        operation,
    ):
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )
        from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftError
        from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringApplicationError

        try:
            return await self._run_runtime_read(
                direct_session_alias,
                f"ifc_product_family_{action_name}",
                operation,
            )
        except DirectApplicationError:
            raise
        except (AuthoringDraftError, ProductFamilyAuthoringApplicationError) as exc:
            raise DirectApplicationError(exc.code, str(exc), phase="read", retryable=False) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except ProductFamilyCatalogError as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                phase="read",
                retryable=False,
            ) from None
        except (KeyError, TypeError, ValueError, ValidationError):
            raise DirectApplicationError(
                "PRODUCT_FAMILY_INTEGRITY_ERROR",
                "The exact ProductFamily Catalog record could not be safely projected",
                phase="output_validation",
            ) from None

    def _require_product_family_catalog_service(self) -> ProductFamilyCatalogService:
        service = getattr(self, "_product_family_catalog_service", None)
        if service is None:
            raise DirectApplicationError(
                "SYSTEM_PAUSED",
                "The IFC ProductFamily Catalog service is unavailable; wait for server-side recovery",
                phase="preflight",
            )
        return service


def _version_card(item: ProductFamilyCatalogVersionSummary) -> IfcProductFamilyVersionCard:
    return IfcProductFamilyVersionCard(
        family_version_alias=item.family_version_ref,
        version_no=item.version_no,
        display_name=item.display_name,
        purpose=item.purpose,
        product_structure_kind=item.product_structure_kind.value,
        created_at=item.created_at,
    )


def _family_rejection(item: ProductFamilyCatalogRejection) -> IfcProductFamilyRejection:
    return IfcProductFamilyRejection(
        family_version_alias=item.family_version_ref,
        version_no=item.version_no,
        code=item.code,
        field_path=item.field_path,
        reason=item.reason,
    )


def _family_card(item: ProductFamilyCatalogFamilySummary) -> IfcProductFamilyCard:
    latest = item.latest_version
    return IfcProductFamilyCard(
        family_alias=item.family_root_ref,
        owner_scope=item.owner_scope.value,
        display_name=latest.display_name,
        purpose=latest.purpose,
        product_structure_kind=latest.product_structure_kind.value,
        latest_family_version_alias=latest.family_version_ref,
        latest_version_no=latest.version_no,
        latest_version_created_at=latest.created_at,
        created_at=item.created_at,
    )


def _version_info(item: ProductFamilyCatalogVersionInfo) -> IfcProductFamilyVersionInfoResult:
    family_contract_json = canonical_json(item.family_contract)
    return IfcProductFamilyVersionInfoResult(
        target_kind="family_version",
        family_alias=item.family_root_ref,
        family_version_alias=item.family_version_ref,
        parent_family_version_alias=item.parent_family_version_ref,
        owner_scope=item.owner_scope.value,
        version_no=item.version_no,
        display_name=item.display_name,
        purpose=item.purpose,
        usage_instructions=item.usage_instructions,
        product_structure_kind=item.product_structure_kind.value,
        family_contract_json=family_contract_json,
        program_entry=item.program_entry,
        geometry_bindings=tuple(
            IfcProductFamilyGeometryBindingResult(
                binding_key=binding.binding_key,
                geometry_resource_version_alias=binding.geometry_resource_version_ref,
                expected_resource_kind=binding.expected_resource_kind.value,
                geometry_purpose=binding.geometry_purpose.value,
            )
            for binding in item.geometry_bindings
        ),
        assets=tuple(
            IfcProductFamilyAssetResult(
                asset_role=asset.asset_role,
                asset_format=asset.asset_format,
                filename=asset.filename,
                content_type=asset.content_type,
                size_bytes=asset.size_bytes,
            )
            for asset in item.assets
        ),
        created_at=item.created_at,
    )


__all__ = ["IfcProductFamilyApplicationMixin", "ProductFamilyProviderApplication"]
