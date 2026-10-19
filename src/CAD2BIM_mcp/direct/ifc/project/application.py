"""Direct application adapter for IFC Project metadata updates."""

from __future__ import annotations

from typing import TYPE_CHECKING

from CAD2BIM_mcp.direct.application_support import _direct_error
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.application_support import (
    ifc_direct_error,
    ifc_run_identity,
)
from CAD2BIM_mcp.direct.ifc.project.contracts import (
    IfcProjectUpdateResult,
    ifc_project_result,
)

if TYPE_CHECKING:
    from CAD2BIM_backend.application.ifc_project_service import IfcProjectService
    from CAD2BIM_backend.domain.ifc_project import UpdateIfcProjectCommand


class IfcProjectApplicationMixin:
    async def update_ifc_project(
        self,
        direct_session_alias: str,
        command: UpdateIfcProjectCommand,
        *,
        idempotency_key: str,
    ) -> IfcProjectUpdateResult:
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )

        try:
            invocation = await self._resolve_scope(
                direct_session_alias,
                "ifc_project_update",
            )
            with self._scope_binder(invocation.identity):
                result = await self._require_ifc_project_service().update(
                    ifc_run_identity(invocation),
                    command,
                    idempotency_key=idempotency_key,
                )
            return IfcProjectUpdateResult(
                operation_alias=result.operation_alias,
                project=ifc_project_result(result.project),
            )
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except Exception as exc:
            raise ifc_direct_error(exc) from None

    def _require_ifc_project_service(self) -> IfcProjectService:
        service = getattr(self, "_ifc_project_service", None)
        if service is None:
            raise DirectApplicationError(
                "SYSTEM_PAUSED",
                "The IFC Project service is unavailable; wait for server-side recovery",
                phase="preflight",
                retryable=False,
            )
        return service


__all__ = ["IfcProjectApplicationMixin"]
