"""Authenticated Runner HTTP projection over the existing IFC Application Core."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from time import monotonic
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from CAD2BIM_backend.application.ifc_query_service import (
    IfcQueryApplicationError,
)
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    IfcGeometryBasisBusinessError,
)
from CAD2BIM_backend.domain.ifc_model import (
    IfcModelAlreadyInitializedError,
    IfcModelConflictError,
    IfcModelNotInitializedError,
    IfcSettledOperationError,
    IfcRunIdentity,
    InitializeIfcModelCommand,
)
from CAD2BIM_backend.domain.ifc_project import (
    IfcProjectBusinessError,
    UpdateIfcProjectCommand,
)
from CAD2BIM_backend.domain.ifc_query import IfcQueryInputBinding
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    IfcSpatialStructureBusinessError,
    IfcSpatialStructureListQuery,
)
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.runtime_execution import IFC_QUERY_EXECUTION
from CAD2BIM_backend.domain.text_source import SourceEdit
from CAD2BIM_backend.ifc_application_resources import IfcApplicationResources
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.model.application import _ifc_model_info_result
from CAD2BIM_mcp.direct.ifc.products.application import (
    execute_products_apply, execute_products_edit, execute_products_read,
)
from CAD2BIM_mcp.direct.ifc.products.contracts import (
    ProductsApplyInput, ProductsEditInput, ProductsReadInput,
)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _InitializeRequest(_Request):
    project_name: str = Field(min_length=1, max_length=255)


class _GeometryBasisRequest(_Request):
    command: ConfigureIfcGeometryBasisCommand


class _ProjectUpdateRequest(_Request):
    command: UpdateIfcProjectCommand


class _SpatialApplyRequest(_Request):
    command: ApplyIfcSpatialStructureCommand


class _SpatialListRequest(_Request):
    query: IfcSpatialStructureListQuery | None = None
    cursor: str | None = None

    @model_validator(mode="after")
    def require_one_page_mode(self):
        if (self.query is None) == (self.cursor is None):
            raise ValueError("submit either first-page filters or a cursor")
        return self


_QUERY_FIELDS = {
    "fresh": (frozenset({"mode", "code", "inputs", "page_size"}), frozenset({"code"})),
    "repair": (
        frozenset({"mode", "parent_attempt_alias", "edits", "page_size"}),
        frozenset({"parent_attempt_alias", "edits"}),
    ),
    "cursor": (frozenset({"mode", "cursor"}), frozenset({"cursor"})),
}


def _require_query_branch(body: dict[str, Any]) -> str:
    mode = body.get("mode")
    contract = _QUERY_FIELDS.get(mode) if isinstance(mode, str) else None
    if contract is None or not contract[1] <= body.keys() or body.keys() - contract[0]:
        raise HTTPException(status_code=422, detail="submit one complete IFC query branch")
    return mode


def create_internal_ifc_router(
    *,
    authorized: Callable[..., Awaitable[tuple[Any, TenantExecutionContext]]],
    bind_identity: Callable[[TenantExecutionContext], AbstractContextManager[None]],
    ifc: IfcApplicationResources,
    data_runtime=None,
    family_catalog=None,
    references=None,
) -> APIRouter:
    """Use the parent router's single authorization callback for every IFC call."""

    router = APIRouter(include_in_schema=False)

    async def scopes(request: Request, tool_name: str):
        access, identity = await authorized(request, tool_name=tool_name)
        ifc_scope = IfcRunIdentity(
            owner_id=access.owner_id, tenant_id=identity.tenant_id,
            task_id=access.task_id, run_id=access.run_id,
        )
        query_scope = InvocationScope(
            identity=identity, owner_id=access.owner_id, run_id=access.run_id,
            execution_mode=ExecutionMode.GRAPH, catalog_frontier=None,
        )
        return access, identity, ifc_scope, query_scope

    def fail(error: Exception) -> None:
        if isinstance(error, IfcSettledOperationError):
            detail = {
                "code": error.code, "message": str(error), "phase": error.phase,
                "retryable": False, "operation_alias": error.operation_alias,
                "operation_status": "failed",
            }
            if error.diagnostic is not None:
                detail["diagnostic"] = error.diagnostic
            raise HTTPException(status_code=409, detail=detail) from None
        if isinstance(error, IfcQueryApplicationError):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": error.code, "message": str(error), "phase": error.phase,
                    "retryable": error.retryable, "attempt_alias": error.attempt_alias,
                },
            ) from None
        if isinstance(error, IfcModelNotInitializedError):
            code, message = "IFC_MODEL_NOT_INITIALIZED", "The current Run has no IFC model"
        elif isinstance(error, IfcModelAlreadyInitializedError):
            code, message = "IFC_MODEL_ALREADY_INITIALIZED", "The current Run already has an IFC model"
        elif isinstance(error, IfcModelConflictError):
            code, message = "IFC_CURRENT_CONFLICT", "The current IFC model changed concurrently"
        elif isinstance(error, (
            IfcGeometryBasisBusinessError, IfcProjectBusinessError,
            IfcSpatialStructureBusinessError,
        )):
            code = error.code
            message = str(error)
        else:
            raise error
        detail = {"code": code, "message": message, "phase": "preflight", "retryable": False}
        operation_alias = getattr(error, "operation_alias", None)
        if operation_alias is not None:
            detail["operation_alias"] = operation_alias
            if isinstance(error, (
                IfcGeometryBasisBusinessError, IfcProjectBusinessError,
                IfcSpatialStructureBusinessError,
            )):
                # These aliases are attached by bind_failed_operation only
                # after the writer persists the original FAILED Operation.
                detail["operation_status"] = "failed"
        raise HTTPException(status_code=409, detail=detail) from None

    @router.post("/ifc/products/apply")
    async def products_apply(body: ProductsApplyInput, request: Request) -> dict[str, Any]:
        access, identity, _ifc_scope, scope = await scopes(request, "ifc_products_apply")
        try:
            with bind_identity(identity):
                result = await execute_products_apply(scope, body,
                    request_identity=access.action_id, core=ifc.products_apply,
                    data_runtime=data_runtime, family_catalog=family_catalog, references=references)
        except DirectApplicationError as exc:
            raise HTTPException(status_code=409, detail={
                "code": exc.code, "message": str(exc), "phase": exc.phase,
                "retryable": exc.retryable, "operation_ref": exc.operation_ref,
                "operation_alias": exc.operation_alias, "operation_status": exc.operation_status,
                "diagnostic": exc.diagnostic,
            }) from None
        return result.model_dump(mode="json")

    @router.post("/ifc/products/edit")
    async def products_edit(body: ProductsEditInput, request: Request) -> dict[str, Any]:
        access, identity, _ifc_scope, scope = await scopes(request, "ifc_products_edit")
        try:
            with bind_identity(identity):
                result = await execute_products_edit(scope, body,
                    request_identity=access.action_id, core=ifc.products_edit,
                    data_runtime=data_runtime, references=references)
        except DirectApplicationError as exc:
            raise HTTPException(status_code=409, detail={
                "code": exc.code, "message": str(exc), "phase": exc.phase,
                "retryable": exc.retryable, "operation_ref": exc.operation_ref,
                "operation_alias": exc.operation_alias, "operation_status": exc.operation_status,
                "diagnostic": exc.diagnostic,
            }) from None
        return result.model_dump(mode="json")

    @router.post("/ifc/products/read")
    async def products_read(body: ProductsReadInput, request: Request) -> dict[str, Any]:
        _access, identity, _ifc_scope, scope = await scopes(request, "ifc_products_read")
        try:
            with bind_identity(identity):
                result = await execute_products_read(scope, body, core=ifc.products_read,
                    family_catalog=family_catalog, references=references)
        except DirectApplicationError as exc:
            raise HTTPException(status_code=409, detail={
                "code": exc.code, "message": str(exc), "phase": exc.phase,
                "retryable": exc.retryable, "operation_ref": exc.operation_ref,
                "diagnostic": exc.diagnostic,
            }) from None
        return result.model_dump(mode="json")

    @router.post("/ifc/model/initialize")
    async def initialize(body: _InitializeRequest, request: Request) -> dict[str, Any]:
        _access, identity, scope, _query_scope = await scopes(request, "ifc_model_initialize")
        try:
            with bind_identity(identity):
                result = await ifc.model.initialize(
                    scope, InitializeIfcModelCommand(project_name=body.project_name),
                )
        except Exception as exc:
            fail(exc)
        return result.model_dump(mode="json")

    @router.post("/ifc/model/info")
    async def model_info(request: Request) -> dict[str, Any]:
        _access, identity, scope, _query_scope = await scopes(request, "ifc_model_info")
        try:
            with bind_identity(identity):
                result = await ifc.model.info(scope)
                projected = _ifc_model_info_result(result, references=references, identity=identity)
        except Exception as exc:
            fail(exc)
        return projected.model_dump(mode="json")

    @router.post("/ifc/model/geometry-basis/configure")
    async def configure_basis(body: _GeometryBasisRequest, request: Request) -> dict[str, Any]:
        access, identity, scope, _query_scope = await scopes(
            request, "ifc_model_configure_geometry_basis"
        )
        try:
            with bind_identity(identity):
                result = await ifc.geometry_basis.configure(
                    scope, body.command, idempotency_key=access.action_id,
                )
        except Exception as exc:
            fail(exc)
        return result.model_dump(mode="json")

    @router.post("/ifc/model/representation-contexts/list")
    async def list_contexts(request: Request) -> dict[str, Any]:
        _access, identity, scope, _query_scope = await scopes(
            request, "ifc_representation_context_list"
        )
        try:
            with bind_identity(identity):
                result = await ifc.geometry_basis.list_representation_contexts(scope)
        except Exception as exc:
            fail(exc)
        return result.model_dump(mode="json")

    @router.post("/ifc/project/update")
    async def update_project(body: _ProjectUpdateRequest, request: Request) -> dict[str, Any]:
        access, identity, scope, _query_scope = await scopes(request, "ifc_project_update")
        try:
            with bind_identity(identity):
                result = await ifc.project.update(
                    scope, body.command, idempotency_key=access.action_id,
                )
        except Exception as exc:
            fail(exc)
        return result.model_dump(mode="json")

    @router.post("/ifc/spatial-structure/apply")
    async def apply_spatial(body: _SpatialApplyRequest, request: Request) -> dict[str, Any]:
        access, identity, scope, _query_scope = await scopes(
            request, "ifc_spatial_structure_apply"
        )
        try:
            with bind_identity(identity):
                result = await ifc.spatial_structure.apply(
                    scope, body.command, idempotency_key=access.action_id,
                )
        except Exception as exc:
            fail(exc)
        return result.model_dump(mode="json")

    @router.post("/ifc/spatial-structure/list")
    async def list_spatial(body: _SpatialListRequest, request: Request) -> dict[str, Any]:
        _access, identity, scope, _query_scope = await scopes(
            request, "ifc_spatial_structure_list"
        )
        try:
            with bind_identity(identity):
                result = await ifc.spatial_structure.list(scope, body.query, body.cursor)
        except Exception as exc:
            fail(exc)
        return result.model_dump(mode="json")

    @router.post("/ifc/query/python")
    async def query_python(body: dict[str, Any], request: Request) -> dict[str, Any]:
        access, identity, _ifc_scope, scope = await scopes(request, "ifc_query_python")
        deadline = monotonic() + IFC_QUERY_EXECUTION.total_seconds
        mode = _require_query_branch(body)
        try:
            with bind_identity(identity):
                if mode == "fresh":
                    page = await ifc.query.fresh(
                        scope, body["code"], access.action_id,
                        tuple(IfcQueryInputBinding.model_validate(item)
                              for item in body.get("inputs", ())),
                        body.get("page_size", 100), deadline=deadline,
                    )
                elif mode == "repair":
                    page = await ifc.query.repair_from_alias(
                        scope, body["parent_attempt_alias"],

                        tuple(SourceEdit(
                            old_text=item["old_text"], new_text=item.get("new_text", ""),
                        ) for item in body["edits"]),
                        access.action_id, body.get("page_size", 100), deadline=deadline,
                    )
                elif mode == "cursor":
                    page = await ifc.query.continue_page(scope, body["cursor"])
        except (KeyError, TypeError, ValueError, ValidationError):
            raise HTTPException(status_code=422, detail="invalid IFC query input") from None
        except Exception as exc:
            fail(exc)
        result_json = canonical_json(list(page.results))
        if len(result_json.encode("utf-8")) > 65_536:
            raise HTTPException(
                status_code=409,
                detail={"code": "IFC_QUERY_PAGE_TOO_LARGE", "message": "Use a smaller page_size"},
            )
        return {
            "attempt_alias": page.attempt_alias,
            "parent_attempt_alias": page.parent_attempt_alias,
            "source_sha256": page.source_sha256,
            "status": "succeeded",
            "summary": (
                f"Returned {page.returned_count} of {page.total_count} bounded IFC-query "
                f"results from {page.result_bytes} persisted canonical JSON bytes."
            ),
            "result_json": result_json,
            "next_cursor": page.next_cursor,
        }

    return router


__all__ = ["create_internal_ifc_router"]
