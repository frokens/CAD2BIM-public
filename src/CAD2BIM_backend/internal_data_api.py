"""Protected Runner Data and owner-backed Code/Operation Application routes."""

from __future__ import annotations

from contextlib import AbstractContextManager
from time import monotonic
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.application.code_discovery_service import (
    CodeDiscoveryApplicationError, CodeDiscoveryService, CodeListQuery, CodeReadSelection,
)
from CAD2BIM_backend.application.data_json_service import DataJsonApplicationError, DataJsonService
from CAD2BIM_backend.application.data_query_python_service import (
    DataQueryPythonApplicationError, DataQueryPythonService,
)
from CAD2BIM_backend.application.data_query_service import DataQueryApplicationError, DataQueryService
from CAD2BIM_backend.application.data_query_sql_service import (
    DataQuerySqlApplicationError, DataQuerySqlService,
)
from CAD2BIM_backend.application.data_runtime_service import (
    DataRuntimeApplicationError, DataRuntimeService, OperationInspection,
)
from CAD2BIM_backend.application.data_transform_service import (
    DataTransformApplicationError, DataTransformService,
)
from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionApplicationError
from CAD2BIM_backend.ports.reference_index import ReferenceIndexError
from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.data_json import JsonReadQuery, JsonReadRequest
from CAD2BIM_backend.domain.data_query import (
    DataDescribePackageQuery,
    DataQueryRowsRequest,
)
from CAD2BIM_backend.domain.data_query_python import DataQueryPythonQuery, DataQueryPythonRequest
from CAD2BIM_backend.domain.data_query_sql import DataQuerySqlQuery, DataQuerySqlRequest
from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationKind,
    CriticalOperationStatus,
    IfcProjectUpdateOperationDetail,
    IfcSpatialStructureApplyOperationDetail,
)
from CAD2BIM_backend.domain.data_transform import (
    DataRepairTransformCommand,
    PackageCodeReference,
    TransformSourceEdit,
    resolve_data_transform_create,
)
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.runtime_execution import DATA_PYTHON_EXECUTION, DATA_SQL_EXECUTION
from CAD2BIM_backend.domain.text_source import SourceEdit, TextSourceView
from CAD2BIM_backend.infrastructure.runner_domain_tokens import VerifiedRunnerDomainAccess


def _fields(body: dict[str, Any], allowed: set[str], required: set[str] = frozenset()) -> None:
    if set(body) - allowed or required - set(body):
        raise HTTPException(status_code=422, detail="invalid domain request fields")


def _parse(model, value: object):
    try:
        return TypeAdapter(model).validate_python(value)
    except ValidationError:
        raise HTTPException(status_code=422, detail="invalid domain request") from None


def _scope(access: VerifiedRunnerDomainAccess, identity: TenantExecutionContext) -> InvocationScope:
    return InvocationScope(
        identity=identity,
        owner_id=access.owner_id,
        run_id=access.run_id,
        execution_mode=ExecutionMode.GRAPH,
        catalog_frontier=None,
    )


def _json(result: object) -> dict[str, object]:
    encoded = jsonable_encoder(result)
    if not isinstance(encoded, dict):
        raise ValueError("Application result must be an object")
    return encoded


def _operation_inspection_json(inspection: OperationInspection) -> dict[str, object]:
    """Preserve the original typed IFC request's unset fields over Runner HTTP."""

    encoded = _json({
        "operation": inspection.operation,
        "result_package": inspection.result_package,
        "input_packages": dict(inspection.input_packages),
        "repair_parent_operation": inspection.repair_parent_operation,
        "committed_ifc_revision": inspection.committed_ifc_revision,
    })
    detail = inspection.operation.operation.detail
    if isinstance(detail, (IfcProjectUpdateOperationDetail, IfcSpatialStructureApplyOperationDetail)):
        encoded["operation"]["operation"]["detail"] = detail.persisted_payload()
    return encoded


def _domain_error(exc: Exception) -> HTTPException:
    from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogError
    from CAD2BIM_mcp.direct.errors import DirectApplicationError
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, DirectApplicationError):
        return HTTPException(status_code=409, detail={
            "code": exc.code, "message": str(exc), "phase": exc.phase,
            "retryable": exc.retryable, "operation_ref": exc.operation_ref,
            "operation_alias": exc.operation_alias, "operation_status": exc.operation_status,
            "diagnostic": exc.diagnostic,
        })
    if isinstance(exc, ReferenceIndexError):
        return HTTPException(status_code=409, detail={"code": "OPERATION_NOT_FOUND",
            "message": "The exact Operation is invalid or unavailable in this scope",
            "phase": "read", "retryable": False})
    if not isinstance(exc, (
        CodeDiscoveryApplicationError,
        DataJsonApplicationError,
        DataQueryPythonApplicationError,
        DataQueryApplicationError,
        DataQuerySqlApplicationError,
        DataRuntimeApplicationError,
        DataTransformApplicationError,
        GeometryInspectionApplicationError,
        ProductFamilyCatalogError,
    )):
        return HTTPException(
            status_code=500,
            detail={
                "code": "BACKEND_DOMAIN_FAILURE",
                "message": "Domain request failed",
                "phase": "read",
                "retryable": False,
            },
        )
    code = exc.code
    diagnostic = getattr(exc, "diagnostic", None)
    return HTTPException(
        status_code=409,
        detail={
            "code": code,
            "message": str(exc),
            "phase": getattr(exc, "phase", "read"),
            "retryable": bool(getattr(exc, "retryable", False)),
            "operation_alias": getattr(exc, "operation_alias", None),
            "operation_ref": getattr(exc, "operation_ref", None),
            "operation_status": getattr(exc, "operation_status", None),
            "attempt_alias": getattr(exc, "attempt_alias", None),
            **({"diagnostic": diagnostic} if diagnostic is not None else {}),
        },
    )


def create_internal_data_router(
    *,
    authorized: Callable,
    bind_identity: Callable[[TenantExecutionContext], AbstractContextManager[None]],
    data_runtime: DataRuntimeService,
    data_query: DataQueryService,
    data_query_sql: DataQuerySqlService,
    data_query_python: DataQueryPythonService,
    data_json: DataJsonService,
    data_transform: DataTransformService,
    code_discovery: CodeDiscoveryService,
    authoring_operation_result: Callable[..., Awaitable[object]],
    geometry_inspection_operation_result: Callable[..., Awaitable[object]],
    geometry_inspection,
    references,
    family_version_reference: Callable[..., Awaitable[str]] | None = None,
) -> APIRouter:
    """Use the parent router's one authorization callback for each exact tool name."""

    router = APIRouter(include_in_schema=False)

    async def context(request: Request, tool_name: str) -> InvocationScope:
        access, identity = await authorized(request, tool_name=tool_name)
        return _scope(access, identity)

    @router.post("/data/data_package_list")
    async def data_package_list(body: dict[str, Any], request: Request) -> dict[str, object]:
        scope = await context(request, "data_package_list")
        _fields(body, {"before_sequence", "page_size"})
        try:
            with bind_identity(scope.identity):
                catalog = await data_runtime.list_package_catalog(
                    scope, before_sequence=body.get("before_sequence"),
                    page_size=body.get("page_size", 20),
                )
            return _json(catalog)
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/data/data_describe_package")
    async def data_describe_package(body: dict[str, Any], request: Request) -> dict[str, object]:
        scope = await context(request, "data_describe_package")
        _fields(body, {"package_alias", "payload_name"}, {"package_alias"})
        try:
            with bind_identity(scope.identity):
                result = await data_query.describe_package(
                    scope, DataDescribePackageQuery(
                        package_alias=body["package_alias"],
                        payload_name=body.get("payload_name"),
                    )
                )
            return _json(result)
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/data/data_get_package_description")
    async def data_get_package_description(
        body: dict[str, Any], request: Request
    ) -> dict[str, object]:
        scope = await context(request, "data_get_package_description")
        _fields(body, {"package_alias"}, {"package_alias"})
        try:
            with bind_identity(scope.identity):
                result = await data_runtime.get_package_description(scope, body["package_alias"])
            return _json(result)
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/data/data_query_rows")
    async def data_query_rows(body: dict[str, Any], request: Request) -> dict[str, object]:
        scope = await context(request, "data_query_rows")
        _fields(body, {"query", "cursor"})
        try:
            with bind_identity(scope.identity):
                result = await data_query.query_rows(
                    scope, _parse(DataQueryRowsRequest, body)
                )
            return _json(result)
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/data/data_query_sql")
    async def data_query_sql_route(body: dict[str, Any], request: Request) -> dict[str, object]:
        scope = await context(request, "data_query_sql")
        try:
            with bind_identity(scope.identity):
                if body.get("mode") == "fresh":
                    _fields(body, {"mode", "query"}, {"mode", "query"})
                    result = await data_query_sql.query(
                        scope, DataQuerySqlRequest(query=_parse(DataQuerySqlQuery, body["query"])),
                        request_identity=scope.identity.command_id,
                    )
                elif body.get("mode") == "cursor":
                    _fields(body, {"mode", "cursor"}, {"mode", "cursor"})
                    result = await data_query_sql.query(
                        scope, DataQuerySqlRequest(cursor=body["cursor"])
                    )
                elif body.get("mode") == "repair":
                    _fields(body, {"mode", "parent_attempt_alias", "edits", "page_size"}, {"mode", "parent_attempt_alias", "edits"})
                    result = await data_query_sql.repair(
                        scope,
                        parent_attempt_alias=body["parent_attempt_alias"],

                        edits=tuple(
                            SourceEdit(edit.old_text, edit.new_text)
                            for edit in _parse(tuple[TransformSourceEdit, ...], body["edits"])
                        ),
                        page_size=body.get("page_size", 100),
                        request_identity=scope.identity.command_id,
                    )
                else:
                    raise HTTPException(status_code=422, detail="invalid SQL mode")
            return _json(result)
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/data/data_query_python")
    async def data_query_python_route(body: dict[str, Any], request: Request) -> dict[str, object]:
        scope = await context(request, "data_query_python")
        try:
            with bind_identity(scope.identity):
                if body.get("mode") == "fresh":
                    _fields(body, {"mode", "query"}, {"mode", "query"})
                    result = await data_query_python.query(
                        scope, DataQueryPythonRequest(query=_parse(DataQueryPythonQuery, body["query"])),
                        request_identity=scope.identity.command_id,
                    )
                elif body.get("mode") == "cursor":
                    _fields(body, {"mode", "cursor"}, {"mode", "cursor"})
                    result = await data_query_python.query(
                        scope, DataQueryPythonRequest(cursor=body["cursor"])
                    )
                elif body.get("mode") == "repair":
                    _fields(body, {"mode", "parent_attempt_alias", "edits", "page_size"}, {"mode", "parent_attempt_alias", "edits"})
                    result = await data_query_python.repair(
                        scope,
                        parent_attempt_alias=body["parent_attempt_alias"],

                        edits=tuple(
                            SourceEdit(edit.old_text, edit.new_text)
                            for edit in _parse(tuple[TransformSourceEdit, ...], body["edits"])
                        ),
                        page_size=body.get("page_size", 100),
                        request_identity=scope.identity.command_id,
                    )
                else:
                    raise HTTPException(status_code=422, detail="invalid Python mode")
            return _json(result)
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/data/data_read_json")
    async def data_read_json(body: dict[str, Any], request: Request) -> dict[str, object]:
        scope = await context(request, "data_read_json")
        _fields(body, {"query", "cursor"})
        try:
            with bind_identity(scope.identity):
                result = await data_json.read_json(
                    scope, JsonReadRequest(
                        query=_parse(JsonReadQuery, body["query"]) if body.get("query") else None,
                        cursor=body.get("cursor"),
                    )
                )
            return _json(result)
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/data/data_derive")
    async def data_transform_route(body: dict[str, Any], request: Request) -> dict[str, object]:
        started = monotonic()
        scope = await context(request, "data_derive")
        key = scope.identity.command_id
        if key is None:
            raise HTTPException(status_code=403, detail="Runner action identity is missing")
        try:
            with bind_identity(scope.identity):
                if body.get("mode") == "create":
                    _fields(
                        body,
                        {"mode", "inputs", "execution", "outputs", "output_package", "parameters", "inline_tables", "ifc_source"},
                        {"mode", "execution", "outputs", "output_package"},
                    )
                    command = resolve_data_transform_create({key: value for key, value in body.items() if key != "mode"})
                    timeout = (
                        DATA_PYTHON_EXECUTION.total_seconds
                        if command.execution.engine == "python_arrow"
                        else DATA_SQL_EXECUTION.total_seconds
                    )
                    result = await data_transform.transform(
                        scope, command,
                        request_identity=key,
                        deadline=started + timeout,
                    )
                    return _json(result)
                if body.get("mode") != "repair":
                    raise HTTPException(status_code=422, detail="invalid transform mode")
                _fields(body, {"mode", "source", "edits", "outputs", "output_package"}, {"mode", "source"})
                source = body["source"]
                if not isinstance(source, dict):
                    raise HTTPException(status_code=422, detail="invalid transform repair source")
                request_payload = {
                    "mode": "repair",
                    "request": body,
                }
                resumed = await data_transform.resume_by_identity(
                    scope, key, request_payload,
                    deadline=started + DATA_PYTHON_EXECUTION.total_seconds,
                )
                if resumed is not None:
                    parent_alias = resumed.repair_parent_operation_alias
                    if parent_alias is None:
                        raise ValueError("repaired Operation is missing its parent alias")
                    return _json(resumed)
                if source.get("source_kind") == "failed_operation":
                    _fields(source, {"source_kind", "operation_alias"}, {"source_kind", "operation_alias"})
                    parent_alias = source["operation_alias"]
                    parent = await data_runtime.get_operation(scope, parent_alias)
                    if parent.operation.operation.status is not CriticalOperationStatus.FAILED:
                        raise HTTPException(status_code=409, detail={
                            "code": "TRANSFORM_REPAIR_PARENT_INVALID",
                            "message": "Repair parent must be a failed Data Transform Operation",
                            "phase": "preflight", "retryable": False,
                        })
                elif source.get("source_kind") == "succeeded_package":
                    _fields(source, {"source_kind", "package_alias"}, {"source_kind", "package_alias"})
                    code = await data_runtime.get_transformation_code(
                        scope, PackageCodeReference(
                            reference_kind="package", package_alias=source["package_alias"]
                        ),
                    )
                    parent_alias = code.operation.catalog_entry.alias
                    parent = await data_runtime.get_operation(scope, parent_alias)
                    if parent.operation.operation.status is not CriticalOperationStatus.SUCCEEDED:
                        raise HTTPException(status_code=409, detail={
                            "code": "TRANSFORM_REPAIR_PARENT_INVALID",
                            "message": "Repair parent must be a succeeded Data Transform Operation",
                            "phase": "preflight", "retryable": False,
                        })
                else:
                    raise HTTPException(status_code=422, detail="invalid transform repair source")
                if parent.operation.operation.tool_name != "data_derive":
                    raise HTTPException(status_code=409, detail={
                        "code": "TRANSFORM_REPAIR_PARENT_INVALID",
                        "message": "Repair source is not a Data Transform Operation",
                        "phase": "preflight", "retryable": False,
                    })
                detail = parent.operation.operation.detail
                deadline = started + (
                    DATA_PYTHON_EXECUTION.total_seconds
                    if getattr(detail, "engine", None) == "python_arrow"
                    else DATA_SQL_EXECUTION.total_seconds
                )
                result = await data_transform.repair_transform(
                    scope=scope,
                    parent_operation=parent.operation,
                    catalog_packages={
                        package.catalog_entry.alias: package
                        for package in parent.input_packages.values()
                    },
                    command=_parse(DataRepairTransformCommand, {
                        "parent_operation_id": parent.operation.operation.operation_id,
                        **{name: body[name] for name in ("edits", "outputs", "output_package") if name in body},
                    }),
                    request_identity=key,
                    request_payload=request_payload,
                    repair_parent_operation_alias=parent_alias,
                    deadline=deadline,
                )
            return _json(result)
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/data/code_list")
    async def code_list(body: dict[str, Any], request: Request) -> dict[str, object]:
        scope = await context(request, "code_list")
        _fields(body, {"query", "cursor"})
        try:
            with bind_identity(scope.identity):
                result = await code_discovery.list_codes(
                    scope,
                    _parse(CodeListQuery, body["query"]) if body.get("query") else None,
                    body.get("cursor"),
                )
            return _json(result)
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/data/code_read")
    async def code_read(body: dict[str, Any], request: Request) -> dict[str, object]:
        scope = await context(request, "code_read")
        _fields(body, {"code_locator", "source_path", "view", "reads"})
        try:
            with bind_identity(scope.identity):
                if body.get("reads") is None:
                    if body.get("code_locator") is None or body.get("view") is None:
                        raise HTTPException(status_code=422, detail="invalid single code read")
                    result = await code_discovery.read_code(
                        scope, body["code_locator"], body.get("source_path"),
                        _parse(TextSourceView, body["view"]),
                    )
                    return _json(result)
                if any(body.get(name) is not None for name in ("code_locator", "source_path", "view")):
                    raise HTTPException(status_code=422, detail="invalid batch code read")
                batch = await code_discovery.read_code_batch(
                    scope, _parse(tuple[CodeReadSelection, ...], body["reads"])
                )
            return _json(batch)
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/data/operation_list")
    async def operation_list(body: dict[str, Any], request: Request) -> dict[str, object]:
        scope = await context(request, "operation_list")
        if body.get("mode") == "ifc_products_recovery":
            from CAD2BIM_mcp.direct.operation.inputs import IfcProductsRecoveryListInput
            from CAD2BIM_mcp.direct.operation.application import list_ifc_products_recovery
            query = _parse(IfcProductsRecoveryListInput, body)
            try:
                with bind_identity(scope.identity):
                    result = await list_ifc_products_recovery(scope, data_runtime=data_runtime,
                        references=references, tool_name=query.tool_name,
                        cursor=query.cursor, page_size=query.page_size)
                return _json(result)
            except Exception as exc:
                raise _domain_error(exc) from None
        if body.get("mode", "catalog") != "catalog":
            raise HTTPException(status_code=422, detail="invalid Operation discovery mode")
        _fields(body, {"mode", "before_sequence", "page_size", "operation_kind", "tool_name", "outcome"})
        try:
            with bind_identity(scope.identity):
                catalog = await data_runtime.list_operation_catalog(
                    scope, before_sequence=body.get("before_sequence"),
                    page_size=body.get("page_size", 20),
                    operation_kind=(
                        _parse(CriticalOperationKind, body["operation_kind"])
                        if body.get("operation_kind") else None
                    ),
                    tool_name=body.get("tool_name"),
                    outcome=_parse(CriticalOperationStatus, body["outcome"]) if body.get("outcome") else None,
                )
            operation_refs = {item.operation.operation_id: references.operation_reference(scope.identity,
                origin_run_id=item.operation.run_id, operation_id=item.operation.operation_id) for item in catalog.operations}
            return {"catalog": _json(catalog), "operation_refs": operation_refs}
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/data/operation_get")
    async def operation_get(body: dict[str, Any], request: Request) -> dict[str, object]:
        scope = await context(request, "operation_get")
        mode = body.get("mode", "operation")
        if mode == "transform_input":
            from CAD2BIM_mcp.runner.data_contracts import OperationGetInput
            selected = _parse(OperationGetInput, body)
            if selected.mode != "transform_input":
                raise HTTPException(status_code=422, detail="invalid saved input selection")
            try:
                with bind_identity(scope.identity):
                    return _json(await data_runtime.get_transform_input(
                        scope, selected.operation_alias,
                        **{key: value for key, value in selected.model_dump().items()
                           if key not in {"mode", "operation_alias", "view"}},
                        **({"view": selected.view} if hasattr(selected, "view") else {}),
                    ))
            except Exception as exc:
                raise _domain_error(exc) from None
        if mode not in {"operation", "operation_reference", "inspection", "preview_report"}:
            raise HTTPException(status_code=422, detail="invalid Operation selection")
        preview_selection = None
        if mode == "preview_report":
            from CAD2BIM_backend.domain.preview_report_read import PreviewTextSelection
            preview_selection = _parse(PreviewTextSelection, body)
        key = {"operation": "operation_alias", "operation_reference": "operation_ref", "inspection": "inspection_ref", "preview_report":"operation_ref"}[mode]
        if preview_selection is None:
            _fields(body, {"mode", key} | ({"case_key", "after_case_key", "page_size"} if mode != "inspection" else set()), {key})
        origin_run_id = scope.run_id
        try:
            with bind_identity(scope.identity):
                case_key = body.get("case_key")
                if mode == "operation":
                    inspection = await data_runtime.get_operation(scope, body["operation_alias"])
                else:
                    if mode == "inspection":
                        loaded = await geometry_inspection.read(scope, body["inspection_ref"])
                        origin_run_id, operation_id = loaded.manifest.origin_run_id, loaded.manifest.operation_id
                        case_key = loaded.manifest.case_key
                    else:
                        origin_run_id, operation_id = references.operation_locator(scope.identity, body["operation_ref"])
                    inspection = await data_runtime.get_operation_by_locator(scope,
                        origin_run_id=origin_run_id, operation_id=operation_id)
                record = inspection.operation.operation
                if record.tool_name == "ifc_products_edit":
                    from CAD2BIM_mcp.direct.operation.application import project_ifc_products_edit_operation
                    projected = project_ifc_products_edit_operation(inspection,
                        operation_ref=references.operation_reference(scope.identity,
                            origin_run_id=record.run_id, operation_id=record.operation_id),
                        same_run=record.run_id == scope.run_id)
                    return {"edit_result": _json(projected)}
                if record.status in {CriticalOperationStatus.PREPARED, CriticalOperationStatus.INDETERMINATE}:
                    from CAD2BIM_mcp.direct.operation.application import project_ifc_apply_operation
                    if family_version_reference is None:
                        raise ValueError("Apply Family navigation is unavailable")
                    family_version_ref = await family_version_reference(
                        scope.identity, record.detail.request.family_version_id, verify_assets=False)
                    projected = project_ifc_apply_operation(inspection,
                        family_version_ref=family_version_ref,
                        operation_ref=references.operation_reference(scope.identity,
                            origin_run_id=record.run_id, operation_id=record.operation_id),
                        same_run=record.run_id == scope.run_id)
                    return {"apply_result": _json(projected)}
                inspection_result = None
                authoring_result = await authoring_operation_result(scope, inspection.operation)
                if inspection.operation.operation.operation_kind is CriticalOperationKind.GEOMETRY_INSPECTION:
                    inspection_result = await geometry_inspection_operation_result(
                        scope, inspection.operation, include_request=True,
                    )
                if mode == "inspection":
                    report = inspection_result.case_report if inspection_result else None
                    case = next((item for item in report.cases if item.case.case_key == case_key), None) if report else None
                    if case_key is not None and (case is None or case.inspection_artifact != loaded.artifact):
                        raise ValueError("Saved Inspection differs from its original case")
                    if case_key is None and (inspection_result is None or inspection_result.inspection_ref != body["inspection_ref"]):
                        raise ValueError("Saved Inspection differs from its original Operation")
                if inspection_result is not None and inspection_result.result_kind == "case_report":
                    return {"preview_result": _json(await geometry_inspection.read_preview_operation(
                        scope, inspection.operation, references=references,
                        case_key=case_key if preview_selection is None else None,
                        after_case_key=body.get("after_case_key"), page_size=body.get("page_size",1),
                        selection=preview_selection, saved=inspection_result))}
                if preview_selection is not None:
                    from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionApplicationError
                    raise GeometryInspectionApplicationError("PREVIEW_REPORT_NOT_AVAILABLE", "The original Operation has no saved Preview case report", phase="read")
                record = inspection.operation.operation
                from CAD2BIM_backend.domain.data_runtime import IfcProductsApplyOperationDetail
                family_version_ref = None
                if isinstance(record.detail, IfcProductsApplyOperationDetail):
                    if family_version_reference is None:
                        raise ValueError("Apply Family navigation is unavailable")
                    family_version_ref = await family_version_reference(
                        scope.identity, record.detail.request.family_version_id, verify_assets=False)
                if authoring_result is not None and record.operation_kind is CriticalOperationKind.AUTHORING_PUBLISH:
                    examples = []
                    for example in record.detail.preview_examples:
                        try:
                            origin = await data_runtime.get_operation_by_locator(scope,
                                origin_run_id=example.origin_run_id, operation_id=example.operation_id)
                        except DataRuntimeApplicationError as exc:
                            if exc.code != "OPERATION_NOT_FOUND":
                                raise
                            examples.append({"availability": "unavailable"})
                            continue
                        result = await geometry_inspection_operation_result(scope, origin.operation)
                        cases = [item for item in result.case_index if not example.case_keys or item["case_key"] in example.case_keys]
                        if example.case_keys and {item["case_key"] for item in cases} != set(example.case_keys):
                            raise ValueError("Publication examples differ from saved cases")
                        examples.append({"availability": "available", "operation_ref": references.operation_reference(scope.identity,
                            origin_run_id=example.origin_run_id, operation_id=example.operation_id),
                            "operation_alias": origin.operation.catalog_entry.alias if example.origin_run_id == scope.run_id else None,
                            "cases": cases})
                    authoring_result = {**authoring_result, "preview_examples": examples}
            return {
                "operation_ref": references.operation_reference(scope.identity,
                    origin_run_id=record.run_id, operation_id=record.operation_id),
                "same_run": record.run_id == scope.run_id, "case_key": case_key,
                "inspection": _operation_inspection_json(inspection),
                "authoring_result": jsonable_encoder(authoring_result),
                "family_version_ref": family_version_ref,
                "code_locator": (code_discovery.data_operation_locator(scope, inspection.operation)
                    if record.tool_name == "data_derive" else None),
                "inspection_result": jsonable_encoder(inspection_result),
            }
        except Exception as exc:
            if isinstance(exc, DataRuntimeApplicationError) and getattr(exc, "operation_id", None):
                exc.operation_ref = references.operation_reference(scope.identity,
                    origin_run_id=origin_run_id, operation_id=exc.operation_id)
            raise _domain_error(exc) from None

    return router


__all__ = ["create_internal_data_router"]
