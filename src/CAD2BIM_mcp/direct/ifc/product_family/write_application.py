"""Direct projection over the existing ProductFamily authoring Application Core."""

from __future__ import annotations

from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftError

from typing import TYPE_CHECKING, Protocol

from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogError
from CAD2BIM_backend.domain.product_family_catalog import (
    ProductFamilyCatalogError,
)
from CAD2BIM_backend.domain.text_source import SourceBundleChange
from CAD2BIM_mcp.direct.application_support import _direct_error
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.product_family.write_contracts import (
    DirectIfcProductFamilyWriteInput,
    IfcProductFamilyWriteResult,
)

_SOURCE_CHANGE_JSON = TypeAdapter(SourceBundleChange)

if TYPE_CHECKING:
    from CAD2BIM_backend.application.geometry_catalog_service import GeometryCatalogService
    from CAD2BIM_backend.application.product_family_authoring_service import (
        ProductFamilyAuthoringService,
    )
    from CAD2BIM_backend.application.product_family_catalog_service import (
        ProductFamilyCatalogService,
    )
    from CAD2BIM_backend.domain.invocation import InvocationScope


class ProductFamilyWriteProviderApplication(Protocol):
    async def write_ifc_product_family(
        self,
        request: DirectIfcProductFamilyWriteInput,
        *,
        request_identity: str,
    ) -> IfcProductFamilyWriteResult: ...


class IfcProductFamilyWriteApplicationMixin:
    """Save one mutable TASK draft through the shared authoring application."""

    async def write_ifc_product_family(
        self, request: DirectIfcProductFamilyWriteInput, *, request_identity: str,
    ) -> IfcProductFamilyWriteResult:
        async def action(scope: InvocationScope) -> IfcProductFamilyWriteResult:
            service = self._require_product_family_authoring_service()
            committed = await service.save_draft_from_aliases(
                scope, declaration=request.to_domain_declaration(), request_identity=request_identity,
            )
            return IfcProductFamilyWriteResult(
                draft_ref=service.draft_reference(scope, committed.draft),
                edit_token=committed.draft.generation,
                operation_alias=committed.operation.catalog_entry.alias,
            )
        return await self._product_family_write_call(request.direct_session_alias, action)

    async def _product_family_write_call(self, direct_session_alias: str, operation):
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )
        from CAD2BIM_backend.application.product_family_authoring_service import (
            ProductFamilyAuthoringApplicationError,
        )

        try:
            scope = await self._resolve_scope(
                direct_session_alias,
                "ifc_product_family_write",
            )
            with self._scope_binder(scope.identity):
                return await operation(scope)
        except DirectApplicationError:
            raise
        except AuthoringDraftError as exc:
            raise DirectApplicationError(exc.code, str(exc), phase=exc.phase,
                operation_alias=exc.operation_alias, retryable=False) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except ProductFamilyAuthoringApplicationError as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                phase=exc.phase,
                retryable=exc.retryable,
                operation_alias=exc.operation_alias,
                diagnostic=exc.diagnostic,
            ) from None
        except (GeometryCatalogError, ProductFamilyCatalogError) as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                phase="preflight",
                retryable=False,
            ) from None
        except (KeyError, TypeError, ValueError, ValidationError):
            raise DirectApplicationError(
                "PRODUCT_FAMILY_WRITE_INTEGRITY_ERROR",
                "The ProductFamily write result could not be safely projected",
                phase="output_validation",
            ) from None

    def _require_product_family_authoring_service(self) -> ProductFamilyAuthoringService:
        service = getattr(self, "_product_family_authoring_service", None)
        if service is None:
            raise DirectApplicationError(
                "SYSTEM_PAUSED",
                "The IFC ProductFamily authoring service is unavailable; wait for server recovery",
                phase="preflight",
            )
        return service

    def _require_product_family_catalog_service(self) -> ProductFamilyCatalogService:
        service = getattr(self, "_product_family_catalog_service", None)
        if service is None:
            raise DirectApplicationError(
                "SYSTEM_PAUSED",
                "The IFC ProductFamily Catalog service is unavailable; wait for server recovery",
                phase="preflight",
            )
        return service

    def _require_geometry_catalog_service(self) -> GeometryCatalogService:
        service = getattr(self, "_geometry_catalog_service", None)
        if service is None:
            raise DirectApplicationError(
                "SYSTEM_PAUSED",
                "The IFC geometry Catalog service is unavailable; wait for server recovery",
                phase="preflight",
            )
        return service


__all__ = [
    "IfcProductFamilyWriteApplicationMixin",
    "ProductFamilyWriteProviderApplication",
]
