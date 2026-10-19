"""Provider-facing protocol for Direct TASK geometry authoring."""

from __future__ import annotations

from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftError
from CAD2BIM_backend.domain.control_plane import ConflictError
from CAD2BIM_backend.domain.authoring_draft import AuthoringDraftChanged
from CAD2BIM_backend.ports.geometry_authoring import GeometryAuthoringSourceError

from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringApplicationError

from CAD2BIM_backend.domain.text_source import SourceBundleChange

import json
from typing import TYPE_CHECKING, Protocol

from pydantic import TypeAdapter, ValidationError

from CAD2BIM_mcp.direct.application_support import _direct_error
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.geometry.authoring_contracts import (
    DirectIfcGeometryResourceWriteInput,
    IfcGeometryResourceWriteResult,
)

_SOURCE_CHANGE_JSON = TypeAdapter(SourceBundleChange)

if TYPE_CHECKING:
    from CAD2BIM_backend.application.geometry_authoring_service import GeometryAuthoringService
    from CAD2BIM_backend.domain.invocation import InvocationScope


class GeometryAuthoringProviderApplication(Protocol):
    async def write_ifc_geometry_resource(
        self,
        request: DirectIfcGeometryResourceWriteInput,
        *,
        request_identity: str,
    ) -> IfcGeometryResourceWriteResult: ...


class IfcGeometryAuthoringApplicationMixin:
    """Thin Direct projection over the shared GeometryAuthoringService."""

    async def write_ifc_geometry_resource(
        self, request: DirectIfcGeometryResourceWriteInput, *, request_identity: str,
    ) -> IfcGeometryResourceWriteResult:
        async def action(scope: InvocationScope):
            service = self._require_geometry_authoring_service()
            committed = await service.save_draft_from_aliases(
                scope, declaration=request.to_domain_declaration(), request_identity=request_identity,
            )
            return IfcGeometryResourceWriteResult(
                draft_ref=service.draft_reference(scope, committed.draft),
                edit_token=committed.draft.generation,
                operation_alias=committed.operation.catalog_entry.alias,
            )
        return await self._authoring_call(
            request.direct_session_alias, "ifc_geometry_resource_write", action,
        )

    async def _authoring_call(
        self,
        direct_session_alias: str,
        action_name: str,
        operation,
    ):
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )
        from CAD2BIM_backend.application.geometry_authoring_service import (
            GeometryAuthoringApplicationError,
        )
        from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogError

        try:
            scope = await self._resolve_scope(direct_session_alias, action_name)
            with self._scope_binder(scope.identity):
                return await operation(scope)
        except DirectApplicationError:
            raise
        except AuthoringDraftChanged as exc:
            raise DirectApplicationError(exc.code,
                "Draft changed; read current Info before submitting a corrected request.",
                phase="preflight", retryable=False) from None
        except GeometryAuthoringSourceError as exc:
            raise DirectApplicationError(f"GEOMETRY_SOURCE_{exc.failure.value.upper()}", str(exc),
                phase="preflight", retryable=False,
                diagnostic={"failure": exc.failure.value}) from None
        except ConflictError:
            if action_name == "ifc_authoring_publish":
                raise DirectApplicationError("AUTHORING_CONFLICT",
                    "Publication conflicted with another edit or publication. Read the original Operation and current draft before submitting a corrected new request.",
                    phase="preflight", retryable=False) from None
            raise DirectApplicationError("GEOMETRY_AUTHORING_INTEGRITY_ERROR",
                "The geometry-authoring result could not be safely projected", phase="output_validation") from None
        except AuthoringDraftError as exc:
            raise DirectApplicationError(exc.code, str(exc), phase=exc.phase,
                operation_alias=exc.operation_alias, retryable=False) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except (GeometryAuthoringApplicationError, ProductFamilyAuthoringApplicationError) as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                phase=exc.phase,
                retryable=exc.retryable,
                operation_alias=exc.operation_alias,
                diagnostic=exc.diagnostic,
            ) from None
        except GeometryCatalogError as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                phase="read",
                retryable=False,
            ) from None
        except (KeyError, TypeError, ValueError, ValidationError, json.JSONDecodeError):
            raise DirectApplicationError(
                "GEOMETRY_AUTHORING_INTEGRITY_ERROR",
                "The geometry-authoring result could not be safely projected",
                phase="output_validation",
            ) from None

    def _require_geometry_authoring_service(self) -> GeometryAuthoringService:
        service = getattr(self, "_geometry_authoring_service", None)
        if service is None:
            raise DirectApplicationError(
                "SYSTEM_PAUSED",
                "The IFC geometry authoring service is unavailable; wait for server recovery",
                phase="preflight",
            )
        return service


__all__ = [
    "GeometryAuthoringProviderApplication",
    "IfcGeometryAuthoringApplicationMixin",
]
