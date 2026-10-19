"""Shared Direct adapter helpers for all IFC capability families."""

from __future__ import annotations

from typing import Any

from CAD2BIM_mcp.direct.errors import DirectApplicationError


def ifc_run_identity(invocation: Any):
    """Project one trusted Direct scope into the shared server-only IFC identity."""

    from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity

    task_id = invocation.identity.task_id
    if task_id is None:
        raise ValueError("resolved Direct scope has no Task")
    return IfcRunIdentity(
        owner_id=invocation.owner_id,
        tenant_id=invocation.identity.tenant_id,
        task_id=task_id,
        run_id=invocation.run_id,
    )


def ifc_direct_error(error: Exception) -> DirectApplicationError:
    """Project IFC business/kernel failures without exposing server identities."""

    from CAD2BIM_backend.domain.ifc_geometry_basis import IfcGeometryBasisBusinessError
    from CAD2BIM_backend.ports.ifc_revision_runtime import IfcRevisionRuntimeError
    from CAD2BIM_backend.domain.ifc_model import (
        IfcInvocationIdentityConflictError,
        IfcModelAlreadyInitializedError,
        IfcModelConflictError,
        IfcModelNotInitializedError,
        IfcSettledOperationError,
        IfcCommittedVerificationError,
    )
    from CAD2BIM_backend.domain.ifc_project import IfcProjectBusinessError
    from CAD2BIM_backend.domain.ifc_spatial_structure import (
        IfcSpatialCursorError,
        IfcSpatialCursorExpiredError,
        IfcSpatialStructureBusinessError,
    )

    if isinstance(error, DirectApplicationError):
        return error
    if isinstance(error, IfcCommittedVerificationError):
        return DirectApplicationError(error.code, str(error), phase="storage_verification",
            retryable=False, operation_alias=error.operation_alias, operation_status="succeeded")
    if isinstance(error, IfcSettledOperationError):
        return DirectApplicationError(
            error.code, str(error), phase=error.phase,
            retryable=False, operation_alias=error.operation_alias,
            diagnostic=error.diagnostic,
        )
    if isinstance(error, IfcSpatialCursorExpiredError):
        return DirectApplicationError(
            error.code,
            "The spatial cursor expired; restart the query from its first page",
            phase="read",
            retryable=False,
        )
    if isinstance(error, IfcSpatialCursorError):
        return DirectApplicationError(
            error.code,
            "The spatial cursor is invalid for this query, tool, or Run",
            phase="read",
            retryable=False,
        )
    if isinstance(
        error,
        (
            IfcGeometryBasisBusinessError,
            IfcProjectBusinessError,
            IfcSpatialStructureBusinessError,
        ),
    ):
        messages = {
            "IFC_NO_CHANGES": "The IFC write request makes no effective change",
            "IFC_GEOMETRY_BASIS_INCOMPLETE": (
                "The requested geometry basis would leave required project configuration incomplete"
            ),
            "IFC_REPRESENTATION_CONTEXT_NOT_FOUND": (
                "A referenced representation ContextIdentifier does not exist"
            ),
            "IFC_REPRESENTATION_CONTEXT_ALREADY_EXISTS": (
                "A requested representation ContextIdentifier already exists"
            ),
            "IFC_REPRESENTATION_CONTEXT_INCOMPATIBLE": (
                "A requested representation context is incompatible with the submitted operation"
            ),
            "IFC_SPATIAL_TARGET_NOT_FOUND": "A referenced spatial object does not exist",
            "IFC_SPATIAL_CLASS_MISMATCH": (
                "A referenced IFC object is not the required spatial class"
            ),
            "IFC_SPATIAL_PARENT_INVALID": (
                "The requested spatial parent and composition relationship is invalid"
            ),
            "IFC_SPATIAL_CYCLE": "The requested spatial relationship would create a cycle",
            "IFC_SPATIAL_REFERENCE_INVALID": (
                "A spatial GlobalId or request-local client_ref cannot be resolved"
            ),
            "IFC_SPATIAL_PLACEMENT_INVALID": (
                "The requested project-world placement or placement closure is invalid"
            ),
            "IFC_SPATIAL_OPERATION_CONFLICT": (
                "The spatial batch contains conflicting operations or identities"
            ),
            "IFC_SPATIAL_NOT_EMPTY": (
                "The spatial object still has protected children, products, or relationships"
            ),
            "IFC_SPATIAL_RESOURCE_LIMIT": (
                "The spatial semantic subtree exceeds the bounded write budget"
            ),
        }
        return DirectApplicationError(
            error.code,
            messages.get(error.code, "The IFC write request is not valid for the current model"),
            phase="preflight",
            retryable=False,
            operation_alias=getattr(error, "operation_alias", None),
            diagnostic=getattr(error, "diagnostic", None),
        )
    if isinstance(error, IfcRevisionRuntimeError):
        return DirectApplicationError(
            error.code, error.message, phase=error.phase,
            retryable=error.retryable, diagnostic=error.diagnostic,
        )
    if isinstance(error, TimeoutError):
        return DirectApplicationError(
            "IFC_REVISION_TIMEOUT",
            "The IFC write total deadline expired. Confirm its Operation and current model "
            "through operation_list/operation_get and ifc_model_info before another write; "
            "timeout alone does not establish whether publication committed.",
            phase="publication", retryable=False,
        )
    if isinstance(error, IfcModelNotInitializedError):
        return DirectApplicationError(
            "IFC_MODEL_NOT_INITIALIZED",
            "The current Run has no IFC model; call ifc_model_initialize first",
            phase="preflight",
        )
    if isinstance(error, IfcModelAlreadyInitializedError):
        return DirectApplicationError(
            "IFC_MODEL_ALREADY_INITIALIZED",
            "The current Run already has an IFC model; use ifc_model_info to inspect it",
            phase="preflight",
        )
    if isinstance(error, IfcInvocationIdentityConflictError):
        return DirectApplicationError(
            "SYSTEM_PAUSED",
            "The server detected an inconsistent IFC invocation identity; stop writing pending recovery",
            phase="preflight",
            retryable=False,
        )
    if isinstance(error, IfcModelConflictError):
        return DirectApplicationError(
            "IFC_CURRENT_CONFLICT",
            "The current IFC model changed concurrently; reread it before issuing another write",
            phase="publication",
        )
    return DirectApplicationError(
        "SYSTEM_PAUSED",
        "IFC processing is paused pending server-side verification or recovery",
        phase="publication",
        retryable=False,
    )


__all__ = ["ifc_direct_error", "ifc_run_identity"]
