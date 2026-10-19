"""Direct application adapter for IFC spatial structure."""

from __future__ import annotations

from typing import TYPE_CHECKING

from CAD2BIM_mcp.direct.application_support import _direct_error
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.application_support import ifc_direct_error, ifc_run_identity
from CAD2BIM_mcp.direct.ifc.spatial.contracts import (
    IfcSpatialStructureApplyResult,
    IfcSpatialStructureListResult,
    ifc_spatial_apply_result,
    ifc_spatial_list_result,
)

if TYPE_CHECKING:
    from CAD2BIM_backend.application.ifc_spatial_structure_service import (
        IfcSpatialStructureService,
    )
    from CAD2BIM_backend.domain.ifc_spatial_structure import (
        ApplyIfcSpatialStructureCommand,
        IfcSpatialStructureListQuery,
    )


class IfcSpatialStructureApplicationMixin:
    async def apply_ifc_spatial_structure(
        self,
        direct_session_alias: str,
        command: ApplyIfcSpatialStructureCommand,
        *,
        idempotency_key: str,
    ) -> IfcSpatialStructureApplyResult:
        from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError

        try:
            invocation = await self._resolve_scope(
                direct_session_alias,
                "ifc_spatial_structure_apply",
            )
            with self._scope_binder(invocation.identity):
                result = await self._require_ifc_spatial_structure_service().apply(
                    ifc_run_identity(invocation),
                    command,
                    idempotency_key=idempotency_key,
                )
            return ifc_spatial_apply_result(result)
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except Exception as exc:
            raise ifc_direct_error(exc) from None

    async def list_ifc_spatial_structure(
        self,
        direct_session_alias: str,
        query: IfcSpatialStructureListQuery | None,
        cursor: str | None,
    ) -> IfcSpatialStructureListResult:
        from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError

        try:
            invocation = await self._resolve_scope(
                direct_session_alias,
                "ifc_spatial_structure_list",
            )
            with self._scope_binder(invocation.identity):
                result = await self._require_ifc_spatial_structure_service().list(
                    ifc_run_identity(invocation),
                    query,
                    cursor,
                )
            return ifc_spatial_list_result(result)
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except Exception as exc:
            raise ifc_direct_error(exc) from None

    def _require_ifc_spatial_structure_service(self) -> IfcSpatialStructureService:
        service = getattr(self, "_ifc_spatial_structure_service", None)
        if service is None:
            raise DirectApplicationError(
                "SYSTEM_PAUSED",
                "The IFC spatial-structure service is unavailable; wait for server-side recovery",
                phase="preflight",
                retryable=False,
            )
        return service


__all__ = ["IfcSpatialStructureApplicationMixin"]
