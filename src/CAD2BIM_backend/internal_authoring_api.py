"""Protected Runner Geometry and ProductFamily routes over the shared Core."""

from __future__ import annotations

from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftError
from CAD2BIM_backend.application.product_family_asset_service import ProductFamilyAssetIntegrityError
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogError
from CAD2BIM_backend.domain.authoring_draft import AuthoringDraftChanged
from CAD2BIM_backend.ports.geometry_authoring import GeometryAuthoringSourceError

import base64
import json
import logging
import re
from contextlib import AbstractContextManager
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from CAD2BIM_backend.application.data_runtime_service import DataRuntimeService
from CAD2BIM_backend.application.geometry_authoring_service import GeometryAuthoringApplicationError
from CAD2BIM_backend.application.geometry_query_service import GeometryQueryApplicationError
from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionApplicationError
from CAD2BIM_backend.application.product_family_authoring_service import (
    ProductFamilyAuthoringApplicationError,
)
from CAD2BIM_backend.domain.geometry_runtime import GeometryPreviewRequestError
from CAD2BIM_backend.domain.product_realization_preview import ProductRealizationPreviewError
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.control_plane import ExecutionMode, ConflictError
from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationKind,
)
from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogRootInfo, GeometryCatalogError
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole, geometry_resource_inputs,
)
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.runner_domain_tokens import VerifiedRunnerDomainAccess
from CAD2BIM_backend.ifc_application_resources import AuthoringApplicationResources
from CAD2BIM_backend.internal_authoring_contracts import (
    AuthoringPublishInput,
    FamilyInfoInput,
    FamilyListInput,
    FamilyWriteInput,
    GeometryInfoInput,
    GeometryListInput,
    GeometryPreviewInput,
    GeometryCasesPreviewInput, GeometryInspectionPreviewInput, FamilyStandardModelPreviewInput,
    FamilyCasesPreviewInput,
    GeometryInspectionQueryTarget,
    FamilyStandardModelQueryTarget,
    GeometryQueryCursorInput,
    GeometryQueryFreshInput,
    GeometryQueryInput,
    GeometryQueryRepairInput,
    GeometrySourceReadInput,
    GeometryWriteInput,
    ProductRealizationPreviewInput,
)


_LOGGER = logging.getLogger(__name__)


def _scope(access: VerifiedRunnerDomainAccess, identity: TenantExecutionContext) -> InvocationScope:
    return InvocationScope(
        identity=identity, owner_id=access.owner_id, run_id=access.run_id,
        execution_mode=ExecutionMode.GRAPH, catalog_frontier=None,
    )


def _json(result: BaseModel) -> dict[str, Any]:
    return result.model_dump(mode="json")


def _domain_error(exc: Exception, *, authoring_publish: bool = False) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, AuthoringDraftChanged):
        return HTTPException(status_code=409, detail={"code": exc.code,
            "message": "Draft changed; read current Info before submitting a corrected request.",
            "phase": "preflight", "retryable": False})
    if isinstance(exc, GeometryAuthoringSourceError):
        return HTTPException(status_code=409, detail={"code": f"GEOMETRY_SOURCE_{exc.failure.value.upper()}",
            "message": str(exc), "phase": "preflight", "retryable": False,
            "diagnostic": {"failure": exc.failure.value}})
    if authoring_publish and isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail={"code": "AUTHORING_CONFLICT",
            "message": "Publication conflicted with another edit or publication. Read the original Operation and current draft before submitting a corrected new request.",
            "phase": "preflight", "retryable": False})
    if isinstance(exc, GeometryPreviewRequestError):
        return HTTPException(status_code=422, detail={"code": "GEOMETRY_PREVIEW_REQUEST_INVALID",
            "message": str(exc), "phase": "input_validation", "retryable": False})
    if isinstance(exc, ProductFamilyAssetIntegrityError):
        return HTTPException(status_code=409, detail={"code": "PRODUCT_FAMILY_INTEGRITY_ERROR",
            "message": str(exc), "phase": "read", "retryable": False})
    if not isinstance(exc, (
        GeometryAuthoringApplicationError,
        GeometryQueryApplicationError,
        GeometryInspectionApplicationError,
        GeometryCatalogError, ProductFamilyCatalogError,
        ProductFamilyAuthoringApplicationError, ProductRealizationPreviewError, AuthoringDraftError,
    )):
        return HTTPException(status_code=500, detail={
            "code": "BACKEND_DOMAIN_FAILURE", "message": "Domain request failed",
            "phase": "read", "retryable": False,
        })
    return HTTPException(status_code=409, detail={
        "code": exc.code, "message": str(exc),
        "phase": getattr(exc, "phase", "read"),
        "retryable": bool(getattr(exc, "retryable", False)),
        "operation_alias": getattr(exc, "operation_alias", None),
        "attempt_alias": getattr(exc, "attempt_alias", None),
        **({"operation_status": "failed"} if isinstance(exc, AuthoringDraftError)
           and exc.operation_status == "failed" and exc.operation_alias is not None else {}),
        **({"diagnostic": exc.diagnostic} if getattr(exc, "diagnostic", None) is not None else {}),
    })


def _preview_payload(preview: BaseModel) -> dict[str, Any]:
    value = preview.model_dump(mode="json", exclude={"content"})
    return {"preview": value, "image_base64": base64.b64encode(preview.content).decode("ascii")}


def _case_batch_payload(outcome) -> dict[str, Any]:
    return {"case_report": outcome.report.model_dump(mode="json"), "operation_alias": outcome.operation_alias,
            "images": [{"case_key": key, "preview": preview.model_dump(mode="json", exclude={"payload"}),
                        "image_base64": base64.b64encode(preview.payload).decode("ascii")}
                       for key, preview in outcome.images]}


def _saved_geometry_observation_payload(outcome) -> dict[str, Any]:
    def image(value):
        return {"metadata": value.model_dump(mode="json", exclude={"payload"}),
                "image_base64": base64.b64encode(value.payload).decode("ascii")}
    section = None
    if outcome.section is not None:
        section = outcome.section.model_dump(mode="json", exclude={"images"})
        section["images"] = [
            {**item.model_dump(mode="json", exclude={"preview"}), "preview": image(item.preview)}
            for item in outcome.section.images
        ]
    return {"diagnostics": [item.model_dump(mode="json") for item in outcome.diagnostics],
        "preview": None if outcome.preview is None else image(outcome.preview), "section": section}


def _inspection_preview_payload(outcome) -> dict[str, Any]:
    return {"inspection_preview": {**_saved_geometry_observation_payload(outcome),
        "inspection_ref": outcome.inspection_ref, "source_kind": outcome.source_kind,
        "source_version_ref": outcome.source_version_ref,
        "result_coordinate_space": outcome.result_coordinate_space,
        "selection": outcome.selection.model_dump(mode="json"),
        "coordinates": outcome.coordinates.model_dump(mode="json"),
        "operation_alias": outcome.operation_alias,
        "operation_ref": outcome.operation_ref, "case_key": outcome.case_key,
    }}


def _family_standard_preview_payload(outcome) -> dict[str, Any]:
    return {"family_standard_preview": {**_saved_geometry_observation_payload(outcome),
        "family_version_ref": outcome.family_version_ref, "public_parameters_json": outcome.public_parameters_json}}


def create_internal_authoring_router(
    *,
    authorized: Callable,
    bind_identity: Callable[[TenantExecutionContext], AbstractContextManager[None]],
    authoring: AuthoringApplicationResources,
    data_runtime: DataRuntimeService,
) -> APIRouter:
    """Add ten relative routes; the parent authenticates each tool and current claim."""

    router = APIRouter(include_in_schema=False)

    async def context(request: Request, tool_name: str) -> tuple[VerifiedRunnerDomainAccess, InvocationScope]:
        access, identity = await authorized(request, tool_name=tool_name)
        return access, _scope(access, identity)

    async def write_error(scope, access, service, exc, *, authoring_publish=False):
        error = _domain_error(exc, authoring_publish=authoring_publish)
        if isinstance(error.detail, dict) and error.detail.get("operation_status") == "failed":
            return error
        try:
            with bind_identity(scope.identity):
                status = await service.request_rejection_status(
                    scope, request_identity=access.action_id, error=exc,
                )
        except Exception:
            status = "unknown"
        if isinstance(error.detail, dict):
            error.detail = {**error.detail,
                "operation_status": "not_started" if status == "not_started" else "unknown"}
        return error

    async def parent_operation(scope: InvocationScope, alias: str, kind: CriticalOperationKind):
        inspection = await data_runtime.get_operation(scope, alias)
        operation = inspection.operation.operation
        if operation.operation_kind is not kind:
            raise HTTPException(status_code=409, detail={
                "code": "AUTHORING_PARENT_INVALID", "message": "Operation kind does not match this write",
                "phase": "preflight", "retryable": False,
            })
        return operation

    @router.post("/authoring/ifc_geometry_resource_list")
    async def geometry_list(body: GeometryListInput, request: Request) -> dict[str, Any]:
        _, scope = await context(request, "ifc_geometry_resource_list")
        try:
            with bind_identity(scope.identity):
                result = await authoring.geometry_catalog.list(
                    scope.identity, None if body.cursor else body.query, body.cursor,
                )
            return _json(result)
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/authoring/ifc_geometry_resource_info")
    async def geometry_info(body: GeometryInfoInput, request: Request) -> dict[str, Any]:
        _, scope = await context(request, "ifc_geometry_resource_info")
        try:
            with bind_identity(scope.identity):
                if body.target_kind == "draft":
                    return _json(await authoring.geometry_authoring.draft_info(scope, draft_ref=body.draft_ref))
                catalog = await authoring.geometry_catalog.info(scope.identity, body.resource_alias)
                if isinstance(catalog, GeometryCatalogRootInfo):
                    bundles = [
                        await authoring.geometry_catalog.resolve_exact_version(
                            scope.identity, version.resource_version_ref,
                        )
                        for version in catalog.recent_versions
                    ]
                    return {
                        "catalog": _json(catalog),
                        "version_bundles": [_json(bundle) for bundle in bundles],
                    }
                bundle = await authoring.geometry_catalog.resolve_exact_version(
                    scope.identity, catalog.version.resource_version_ref,
                )
                source = None
                if catalog.version.source_available:
                    source = await authoring.geometry_source.read_source(
                        scope.identity, catalog.version.resource_version_ref, None, None,
                    )
                parameter_schema_json = None
                parameter_asset = next((
                    item for item in bundle.assets
                    if item.asset_role is GeometryResourceAssetRole.PARAMETER_SCHEMA
                ), None)
                if parameter_asset is not None:
                    exact = await authoring.geometry_resources.read_exact_asset(
                        scope.identity,
                        resource_id=bundle.resource.resource_id,
                        resource_version_id=bundle.version.resource_version_id,
                        asset_role=GeometryResourceAssetRole.PARAMETER_SCHEMA,
                    )
                    if (exact is None or exact[0].sha256 != parameter_asset.sha256
                            or len(exact[1]) != parameter_asset.size_bytes):
                        raise ValueError("Geometry parameter schema exact asset is unavailable")
                    parameter_schema_json = canonical_json(json.loads(exact[1].decode("utf-8")))
                    if len(parameter_schema_json.encode("utf-8")) > 65_536:
                        raise ValueError("Geometry parameter schema exceeds the Provider budget")
                input_aliases = []
                for item in geometry_resource_inputs(bundle.version.manifest):
                    target = await authoring.geometry_resources.get_exact_version(
                        scope.identity,
                        resource_id=item.resource_id,
                        resource_version_id=item.resource_version_id,
                    )
                    if target is None or target.version.content_digest != item.content_digest:
                        raise ValueError("Geometry input exact version is unavailable")
                    input_aliases.append({
                        "input_key": item.input_key,
                        "resource_version_alias": authoring.geometry_catalog.version_ref(scope.identity, target),
                    })
                return {
                    "catalog": _json(catalog), "bundle": _json(bundle),
                    "publication": await authoring.geometry_authoring.publication_navigation(scope,
                        root_kind="geometry", version_id=bundle.version.resource_version_id),
                    "source": None if source is None else _json(source),
                    "parameter_schema_json": parameter_schema_json,
                    "geometry_inputs": input_aliases,
                }
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/authoring/ifc_geometry_resource_source_read")
    async def geometry_source(body: GeometrySourceReadInput, request: Request) -> dict[str, Any]:
        _, scope = await context(request, "ifc_geometry_resource_source_read")
        try:
            with bind_identity(scope.identity):
                result = await authoring.geometry_source.read_source(
                    scope.identity, body.resource_version_alias, body.path, body.view,
                )
            return _json(result)
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/authoring/ifc_geometry_resource_write")
    async def geometry_write(body: GeometryWriteInput, request: Request) -> dict[str, Any]:
        access, scope = await context(request, "ifc_geometry_resource_write")
        service = authoring.geometry_authoring
        try:
            with bind_identity(scope.identity):
                committed = await service.save_draft_from_aliases(
                    scope, declaration=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
                    request_identity=access.action_id,
                )
                return {
                    "draft_ref": service.draft_reference(scope, committed.draft),
                    "edit_token": committed.draft.generation,
                    "operation_alias": committed.operation.catalog_entry.alias,
                    "saved": True, "runtime_checked": False,
                }
        except Exception as exc:
            raise await write_error(scope, access, service, exc) from None

    @router.post("/authoring/ifc_geometry_preview")
    async def geometry_preview(body: GeometryPreviewInput, request: Request) -> dict[str, Any]:
        access, scope = await context(request, "ifc_geometry_preview")
        try:
            with bind_identity(scope.identity):
                if isinstance(body, FamilyStandardModelPreviewInput):
                    outcome = await authoring.geometry_preview.preview_family_standard_model(
                        scope, body.family_version_alias, body.observation)
                    return _family_standard_preview_payload(outcome)
                if isinstance(body, GeometryInspectionPreviewInput):
                    outcome = await authoring.geometry_preview.preview_inspection(
                        scope, body.inspection_ref, body.selection,
                        body.coordinate_space, body.observation,
                    )
                    return _inspection_preview_payload(outcome)
                service = authoring.geometry_preview
                if isinstance(body, GeometryCasesPreviewInput):
                    cases = await authoring.geometry_inspection.resolve_cases(scope, body.cases, case_kind="geometry")
                    parameters = tuple(item.parameters for item in cases)
                else:
                    cases = None
                    parameters = (body.parameters,)
                if body.source.source_kind == "draft":
                    prepared = await authoring.geometry_authoring.prepare_draft_preview(
                        scope, draft_ref=body.source.draft_ref, parameters=parameters,
                    )
                else:
                    prepared = await service.prepare_published_cases(scope, body.source.resource_version_alias)
                common = dict(**prepared, datum_transform=body.datum_transform,
                              reference_geometries=body.reference_geometries,
                              retain_for_inspection=body.retain, request_identity=access.action_id)
                outcome = (await service.preview_loaded(scope, parameters=parameters[0], render_profile=body.render_profile, **common)
                           if cases is None else await service.preview_cases(scope, cases=cases, render=body.render, **common))
                return _case_batch_payload(outcome)
        except Exception as exc:
            error = _domain_error(exc)
            detail = error.detail if isinstance(error.detail, dict) else {}
            code, phase, status = (detail.get(field) for field in ("code", "phase", "operation_status"))
            _LOGGER.error(
                "Domain Preview failed run=%s action=%s tool=ifc_geometry_preview "
                "code=%s phase=%s operation_status=%s exception_type=%s",
                scope.run_id, access.action_id,
                code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", code) else None,
                phase if isinstance(phase, str) and phase in {"read", "preflight", "input_validation", "code_validation", "execution",
                    "output_validation", "publication", "ifc_validation", "storage_verification",
                    "commit_fence", "reconcile"} else None,
                status if isinstance(status, str) and status in {"unknown", "not_started", "failed"} else None,
                type(exc).__name__,
            )
            raise error from None

    @router.post("/authoring/ifc_geometry_query_python")
    async def geometry_query(body: GeometryQueryInput, request: Request) -> dict[str, Any]:
        access, scope = await context(request, "ifc_geometry_query_python")
        try:
            with bind_identity(scope.identity):
                if isinstance(body, GeometryQueryFreshInput):
                    target = body.target
                    if isinstance(target, GeometryInspectionQueryTarget):
                        page = await authoring.geometry_query.fresh_inspection(
                            scope, inspection_ref=target.inspection_ref,
                            selection=target.selection, coordinate_space=target.coordinate_space,
                            source_code=body.code, request_identity=access.action_id,
                            page_size=body.page_size,
                        )
                    elif isinstance(target, FamilyStandardModelQueryTarget):
                        page = await authoring.geometry_query.fresh_family_standard_model(
                            scope, family_version_alias=target.family_version_alias,
                            source_code=body.code, request_identity=access.action_id, page_size=body.page_size,
                        )
                    else:
                        bundle = await authoring.geometry_catalog.resolve_exact_version(
                            scope.identity, target.resource_version_alias,
                        )
                        page = await authoring.geometry_query.fresh(
                            scope, bundle.resource.resource_id, bundle.version.resource_version_id,
                            body.code, access.action_id, body.page_size,
                        )
                elif isinstance(body, GeometryQueryRepairInput):
                    parent_id = await authoring.geometry_query.resolve_attempt_alias(
                        scope, body.parent_attempt_alias,
                    )
                    page = await authoring.geometry_query.repair(
                        scope, parent_id, body.edits, access.action_id, body.page_size,
                    )
                elif isinstance(body, GeometryQueryCursorInput):
                    page = await authoring.geometry_query.continue_page(scope, body.cursor)
                else:
                    raise ValueError("unsupported Geometry query mode")
                if page.inspection_ref is not None:
                    target_result = GeometryInspectionQueryTarget(
                        target_kind="geometry_inspection", inspection_ref=page.inspection_ref,
                        selection=page.selection, coordinate_space=page.coordinate_space,
                    ).model_dump(mode="json")
                elif page.family_version_id is not None:
                    target_result = {"target_kind": "family_standard_model", "family_version_alias":
                        await authoring.geometry_query.family_version_alias(scope, page.family_version_id)}
                else:
                    if page.resource_version_id is None:
                        raise ValueError("Geometry query result lost its exact version")
                    target = await authoring.geometry_resources.get_version_by_id(
                        scope.identity, resource_version_id=page.resource_version_id,
                    )
                    if target is None:
                        raise ValueError("Geometry query result version is no longer visible")
                    target_alias = authoring.geometry_catalog.version_ref(scope.identity, target)
                    target_result = {"target_kind": "geometry_resource_version", "resource_version_alias": target_alias}
            result_json = canonical_json(list(page.results))
            if len(result_json.encode("utf-8")) > 65_536:
                raise HTTPException(status_code=409, detail={
                    "code": "GEOMETRY_QUERY_PAGE_TOO_LARGE",
                    "message": "Query page exceeds the Provider result budget",
                    "phase": "output_validation", "retryable": False,
                })
            return {
                "attempt_alias": page.attempt_alias,
                "parent_attempt_alias": (
                    body.parent_attempt_alias if isinstance(body, GeometryQueryRepairInput) else None
                ),
                "target": target_result,
                "source_sha256": page.source_sha256, "status": "succeeded",
                "summary": f"Returned {page.returned_count} of {page.total_count} bounded geometry-query results.",
                "result_json": result_json, "next_cursor": page.next_cursor,
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/authoring/ifc_product_family_list")
    async def family_list(body: FamilyListInput, request: Request) -> dict[str, Any]:
        _, scope = await context(request, "ifc_product_family_list")
        try:
            with bind_identity(scope.identity):
                return _json(await authoring.family_catalog.list(
                    scope.identity, None if body.cursor else body.query, body.cursor,
                ))
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/authoring/ifc_product_family_info")
    async def family_info(body: FamilyInfoInput, request: Request) -> dict[str, Any]:
        _, scope = await context(request, "ifc_product_family_info")
        try:
            with bind_identity(scope.identity):
                if body.target_kind == "draft":
                    return _json(await authoring.family_authoring.draft_info(scope, draft_ref=body.draft_ref))
                result = await authoring.family_catalog.info(
                    scope.identity, body.family_alias,
                    before_version_no=body.before_version_no, page_size=body.page_size,
                )
                response = _json(result)
                if result.target_kind == "family_version":
                    bundle = await authoring.family_catalog.resolve_exact_version(scope.identity, body.family_alias)
                    response["publication"] = await authoring.geometry_authoring.publication_navigation(scope,
                        root_kind="family", version_id=bundle.version.family_version_id)
                return response
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/authoring/ifc_product_family_write")
    async def family_write(body: FamilyWriteInput, request: Request) -> dict[str, Any]:
        access, scope = await context(request, "ifc_product_family_write")
        service = authoring.family_authoring
        try:
            with bind_identity(scope.identity):
                committed = await service.save_draft_from_aliases(
                    scope, declaration=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
                    request_identity=access.action_id,
                )
                return {
                    "draft_ref": service.draft_reference(scope, committed.draft),
                    "edit_token": committed.draft.generation,
                    "operation_alias": committed.operation.catalog_entry.alias,
                    "saved": True, "runtime_checked": False,
                }
        except Exception as exc:
            raise await write_error(scope, access, service, exc) from None

    @router.post("/authoring/ifc_product_realization_preview")
    async def family_preview(body: ProductRealizationPreviewInput, request: Request) -> dict[str, Any]:
        access, scope = await context(request, "ifc_product_realization_preview")
        try:
            with bind_identity(scope.identity):
                service = authoring.realization_preview
                if isinstance(body, FamilyCasesPreviewInput):
                    cases = await authoring.geometry_inspection.resolve_cases(scope, body.cases, case_kind="family")
                    if body.source.source_kind == "draft":
                        prepared = await authoring.family_authoring.prepare_draft_preview(
                            scope, draft_ref=body.source.draft_ref, invocations=tuple(item.invocation for item in cases),
                        )
                    else:
                        prepared = await service.prepare_published_cases(scope, body.source.family_version_alias, cases)
                    outcome = await service.preview_cases(scope, **prepared, cases=cases,
                        nominal_scenario_key=body.nominal_scenario_key, render=body.render,
                        retain_for_inspection=body.retain, request_identity=access.action_id,
                        reference_geometries=body.reference_geometries)
                    return _case_batch_payload(outcome)
                common = dict(render_profile=body.render_profile, reference_geometries=body.reference_geometries,
                              request_identity=access.action_id, retain_for_inspection=body.retain)
                if body.source.source_kind == "draft":
                    prepared = await authoring.family_authoring.prepare_draft_preview(
                        scope, draft_ref=body.source.draft_ref, invocations=(body.invocation,),
                    )
                    requests = prepared.pop("requests")
                    outcome = await service.preview_loaded(scope, **prepared, request=requests[0] if requests is not None else None, **common)
                else:
                    family = await authoring.family_catalog.resolve_exact_version(scope.identity, body.source.family_version_alias)
                    outcome = await service.preview(scope, family_version=family, invocation=body.invocation, **common)
                return {
                    "realization": _json(outcome.realization), "operation_alias": outcome.operation_alias,
                    "inspection_ref": outcome.inspection_ref,
                    "diagnostics": [item.model_dump(mode="json") for item in outcome.diagnostics],
                    **(_preview_payload(outcome.preview) if outcome.preview is not None else {"preview": None}),
                }
        except Exception as exc:
            raise _domain_error(exc) from None

    @router.post("/authoring/ifc_authoring_publish")
    async def authoring_publish(body: AuthoringPublishInput, request: Request) -> dict[str, Any]:
        access, scope = await context(request, "ifc_authoring_publish")
        service = authoring.geometry_authoring if body.root_kind == "geometry" else authoring.family_authoring
        try:
            with bind_identity(scope.identity):
                committed = await service.publish_draft(
                    scope, draft_ref=body.draft_ref, expected_edit_token=body.expected_edit_token,
                    preview_examples=body.preview_examples, request_identity=access.action_id,
                )
                return {
                    "operation_alias": committed.operation.catalog_entry.alias,
                    "geometry_versions": [{"resource_version_alias": authoring.geometry_catalog.version_ref(scope.identity, item)}
                                          for item in committed.geometry_versions],
                    "family_version_alias": (authoring.family_catalog.version_ref(scope.identity, committed.family_version)
                                             if committed.family_version is not None else None),
                }
        except Exception as exc:
            raise await write_error(scope, access, service._publication, exc, authoring_publish=True) from None

    return router


__all__ = ["create_internal_authoring_router"]
