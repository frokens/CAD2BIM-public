"""Direct application adapter for durable IFC model capabilities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from CAD2BIM_mcp.direct.application_support import _direct_error
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.application_support import (
    ifc_direct_error as _ifc_direct_error,
    ifc_run_identity as _ifc_run_identity,
)
from CAD2BIM_mcp.direct.ifc.model.contracts import (
    IfcModelConfigureGeometryBasisResult,
    IfcModelInfoResult,
    IfcModelInitializeResult,
    IfcProjectResult,
    IfcRepresentationContextCatalogResult,
    geometry_basis_result,
    representation_context_catalog_result,
)

if TYPE_CHECKING:
    from CAD2BIM_backend.application.ifc_geometry_basis_service import (
        IfcGeometryBasisService,
    )
    from CAD2BIM_backend.application.ifc_model_service import IfcModelService
    from CAD2BIM_backend.domain.ifc_geometry_basis import (
        ConfigureIfcGeometryBasisCommand,
    )


class IfcModelApplicationMixin:
    async def initialize_ifc_model(
        self,
        direct_session_alias: str,
        project_name: str,
    ) -> IfcModelInitializeResult:
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )
        from CAD2BIM_backend.domain.ifc_model import InitializeIfcModelCommand

        try:
            command = InitializeIfcModelCommand(project_name=project_name)
        except ValidationError as exc:
            raise DirectApplicationError(
                "INVALID_ARGUMENT",
                "project_name must be 1 to 255 characters after trimming and contain no control characters",
                phase="preflight",
            ) from exc
        try:
            invocation = await self._resolve_scope(
                direct_session_alias,
                "ifc_model_initialize",
            )
            with self._scope_binder(invocation.identity):
                result = await self._require_ifc_model_service().initialize(
                    _ifc_run_identity(invocation),
                    command,
                )
            return IfcModelInitializeResult(
                operation_alias=result.operation_alias,
                model=_ifc_model_info_result(result.model),
            )
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except Exception as exc:
            raise _ifc_direct_error(exc) from None

    async def get_ifc_model_info(
        self,
        direct_session_alias: str,
    ) -> IfcModelInfoResult:
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )

        try:
            invocation = await self._resolve_scope(
                direct_session_alias,
                "ifc_model_info",
            )
            with self._scope_binder(invocation.identity):
                result = await self._require_ifc_model_service().info(
                    _ifc_run_identity(invocation)
                )
            return _ifc_model_info_result(result, references=self.reference_service, identity=invocation.identity)
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except Exception as exc:
            raise _ifc_direct_error(exc) from None

    async def configure_ifc_geometry_basis(
        self,
        direct_session_alias: str,
        command: ConfigureIfcGeometryBasisCommand,
        *,
        idempotency_key: str,
    ) -> IfcModelConfigureGeometryBasisResult:
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )

        try:
            invocation = await self._resolve_scope(
                direct_session_alias,
                "ifc_model_configure_geometry_basis",
            )
            with self._scope_binder(invocation.identity):
                result = await self._require_ifc_geometry_basis_service().configure(
                    _ifc_run_identity(invocation),
                    command,
                    idempotency_key=idempotency_key,
                )
            return IfcModelConfigureGeometryBasisResult(
                operation_alias=result.operation_alias,
                model=_ifc_model_info_result(result.model),
            )
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except Exception as exc:
            raise _ifc_direct_error(exc) from None

    async def list_ifc_representation_contexts(
        self,
        direct_session_alias: str,
    ) -> IfcRepresentationContextCatalogResult:
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )

        try:
            invocation = await self._resolve_scope(
                direct_session_alias,
                "ifc_representation_context_list",
            )
            with self._scope_binder(invocation.identity):
                result = await self._require_ifc_geometry_basis_service().list_representation_contexts(
                    _ifc_run_identity(invocation)
                )
            return representation_context_catalog_result(result)
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except Exception as exc:
            raise _ifc_direct_error(exc) from None

    def _require_ifc_model_service(self) -> IfcModelService:
        if self._ifc_model_service is None:
            raise DirectApplicationError(
                "SYSTEM_PAUSED",
                "The IFC model service is unavailable; wait for server-side recovery",
                phase="preflight",
                retryable=False,
            )
        return self._ifc_model_service

    def _require_ifc_geometry_basis_service(self) -> IfcGeometryBasisService:
        if self._ifc_geometry_basis_service is None:
            raise DirectApplicationError(
                "SYSTEM_PAUSED",
                "The IFC geometry basis service is unavailable; wait for server-side recovery",
                phase="preflight",
                retryable=False,
            )
        return self._ifc_geometry_basis_service


def source_revision_result(exact, *, references, identity):
    from CAD2BIM_mcp.direct.ifc.model.contracts import SourceRevision
    if exact is None:
        return None
    if references is None or identity is None:
        raise ValueError("The exact IFC read basis cannot be projected without its authorized reference owner")
    return SourceRevision(revision_ref=references.ifc_revision_reference(identity, exact), revision_no=exact.revision_no)


def _ifc_model_info_result(info: Any, *, references=None, identity=None) -> IfcModelInfoResult:
    project = info.project
    return IfcModelInfoResult(
        ifc_schema=info.ifc_schema,
        project=IfcProjectResult(
            global_id=project.global_id,
            name=project.name,
        ),
        size_bytes=info.size_bytes,
        geometry_basis_configured=info.geometry_basis_configured,
        geometry_basis=geometry_basis_result(getattr(info, "geometry_basis", None)),
        site_count=info.site_count,
        element_count=info.element_count,
        source_revision=source_revision_result(getattr(info, "source_exact", None), references=references, identity=identity),
    )
