"""Authenticated library access over the existing Geometry, Family and Code owners."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from CAD2BIM_backend.application.code_discovery_service import (
    CodeCard, CodeDiscoveryApplicationError, CodeKind, CodeReadResult, CodeReadSelection,
)
from CAD2BIM_backend.application.errors import ConflictError, NotFoundError
from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionApplicationError
from CAD2BIM_backend.application.identity_binding import resolve_user_task_identity
from CAD2BIM_backend.application.product_family_asset_service import ProductFamilyAssetIntegrityError
from CAD2BIM_backend.application.reference_projection import project_reference_fields
from CAD2BIM_backend.domain.geometry_catalog import (
    GeometryCatalogError, GeometryCatalogIntegrityError, GeometryCatalogListQuery,
    GeometryCatalogListResult, GeometryCatalogRootInfo, GeometryCatalogVersionInfo,
)
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.resource_scope import ResourceDeletionError
from CAD2BIM_backend.domain.product_family_catalog import (
    ProductFamilyCatalogError, ProductFamilyCatalogIntegrityError, ProductFamilyCatalogListQuery,
    ProductFamilyCatalogListResult, ProductFamilyCatalogRootInfo, ProductFamilyCatalogVersionInfo,
)
from CAD2BIM_backend.infrastructure.authentication import AuthenticationError
from CAD2BIM_backend.ports.identity import IdentityAccessDenied
from CAD2BIM_backend.ports.reference_index import ReferenceIndexError
from CAD2BIM_backend.saved_geometry_display_api import authorized_component_read_scope


_PUBLISHED_CODE_KINDS = frozenset({CodeKind.GEOMETRY_RESOURCE_PROGRAM, CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM})
# Paths belong to the returned DTOs. Manifest JSON and source text are not visited.
_REFERENCE_PATHS = {
    GeometryCatalogListResult: {
        ("resources", "*", "resource_root_ref"): ("gr",),
        ("resources", "*", "latest_version", "resource_version_ref"): ("gv",),
    },
    GeometryCatalogRootInfo: {
        ("resource", "resource_root_ref"): ("gr",),
        ("resource", "latest_version", "resource_version_ref"): ("gv",),
        ("recent_versions", "*", "resource_version_ref"): ("gv",),
    },
    GeometryCatalogVersionInfo: {
        ("resource_root_ref",): ("gr",), ("version", "resource_version_ref"): ("gv",),
    },
    ProductFamilyCatalogListResult: {
        ("families", "*", "family_root_ref"): ("fr",),
        ("families", "*", "latest_version", "family_version_ref"): ("fv",),
    },
    ProductFamilyCatalogRootInfo: {
        ("family", "family_root_ref"): ("fr",),
        ("family", "latest_version", "family_version_ref"): ("fv",),
        ("versions", "*", "family_version_ref"): ("fv",),
    },
    ProductFamilyCatalogVersionInfo: {
        ("family_root_ref",): ("fr",), ("family_version_ref",): ("fv",),
        ("parent_family_version_ref",): ("fv",),
        ("geometry_bindings", "*", "geometry_resource_version_ref"): ("gv",),
    },
    CodeCard: {("code_locator",): ("sc",), ("owner_alias",): ("gv", "fv")},
    CodeReadResult: {("code_locator",): ("sc",)},
}


def _error(exc):
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, IdentityAccessDenied):
        return HTTPException(status_code=403, detail="access denied")
    if isinstance(exc, ResourceDeletionError):
        return HTTPException(status_code=403 if exc.code == "LIBRARY_SYSTEM_RESOURCE" else 409,
            detail={"code": exc.code, "message": str(exc)})
    if isinstance(exc, GeometryInspectionApplicationError):
        return HTTPException(status_code=409 if "INTEGRITY" in exc.code else 404,
            detail={"code": exc.code, "message": str(exc)})
    if isinstance(exc, (ConflictError, GeometryCatalogIntegrityError,
                        ProductFamilyCatalogIntegrityError, ProductFamilyAssetIntegrityError)):
        return HTTPException(status_code=409, detail="library source integrity conflict")
    if isinstance(exc, (NotFoundError, GeometryCatalogError, ProductFamilyCatalogError, ReferenceIndexError)):
        return HTTPException(status_code=404, detail="library source not available")
    if isinstance(exc, CodeDiscoveryApplicationError):
        status = 404 if exc.code in {"CODE_LOCATOR_INVALID", "CODE_OWNER_NOT_FOUND",
            "GEOMETRY_RESOURCE_REFERENCE_INVALID", "GEOMETRY_RESOURCE_NOT_FOUND",
            "PRODUCT_FAMILY_REFERENCE_INVALID", "PRODUCT_FAMILY_NOT_FOUND"} else (
            409 if "INTEGRITY" in exc.code else 422)
        return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    raise exc


def create_library_router(*, authenticator, task_runs, identity_directory, geometry_catalog,
                          family_catalog, code_discovery, references, bind_identity, saved_display):
    router = APIRouter()
    bearer = HTTPBearer(auto_error=False)

    def subject(credential: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credential is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        try:
            return authenticator.authenticate(credential.credentials)
        except AuthenticationError:
            raise HTTPException(status_code=401, detail="unauthorized") from None

    async def context(task_id: str | None = Query(default=None, min_length=1, max_length=80),
                      run_id: str | None = Query(default=None, min_length=1, max_length=80),
                      authenticated=Depends(subject)):
        try:
            if run_id is not None:
                if task_id is None:
                    raise ValueError("run_id requires its task_id")
                scope = await authorized_component_read_scope(task_runs, authenticated, task_id, run_id)
                return scope.identity, scope
            if task_id is not None:
                identity, _ = await task_runs.authorized_task_context(authenticated, task_id)
            else:
                identity = await resolve_user_task_identity(identity_directory, authenticated)
            return identity, None
        except Exception as exc:
            raise _error(exc) from None

    async def project(result, identity):
        document = result.model_dump(mode="json")
        if identity.task_id is None:
            return document
        return await project_reference_fields(document, _REFERENCE_PATHS[type(result)],
            lambda value, kinds: references.shorten(identity, value, kinds))

    @router.get("/library/{kind}")
    async def list_catalog(kind: Literal["geometry", "families"],
                           page_size: int = Query(default=20, ge=1, le=100),
                           cursor: str | None = Query(default=None, min_length=1, max_length=16_384),
                           access=Depends(context)):
        identity, _ = access
        try:
            with bind_identity(identity):
                catalog = geometry_catalog if kind == "geometry" else family_catalog
                query = GeometryCatalogListQuery(page_size=page_size) if kind == "geometry" else ProductFamilyCatalogListQuery(page_size=page_size)
                result = await catalog.list(identity, None if cursor else query, cursor)
                return await project(result, identity)
        except Exception as exc:
            raise _error(exc) from None

    @router.get("/library/{kind}/info")
    async def info(kind: Literal["geometry", "families"],
                   reference: str = Query(min_length=1, max_length=16_384),
                   before_version_no: int | None = Query(default=None, ge=1),
                   page_size: int = Query(default=20, ge=1, le=100),
                   access=Depends(context)):
        identity, scope = access
        try:
            with bind_identity(identity):
                kinds = ("gr", "gv") if kind == "geometry" else ("fr", "fv")
                exact_ref = await references.expand(identity, reference, kinds)
                code = display_source = display_message = None
                if kind == "geometry":
                    if before_version_no is not None or page_size != 20:
                        raise ValueError("Geometry Info uses its existing recent-version window")
                    result = await geometry_catalog.info(identity, exact_ref)
                    if isinstance(result, GeometryCatalogVersionInfo) and scope is not None:
                        version_ref = result.version.resource_version_ref
                        if result.version.source_available:
                            code = await code_discovery.published_program_card(scope, CodeKind.GEOMETRY_RESOURCE_PROGRAM, version_ref)
                        if result.resource_kind is GeometryResourceKind.MAPPED_MODEL:
                            display_source = {"source_kind": "geometry_resource_version",
                                "source_ref": await references.shorten(identity, version_ref, ("gv",))}
                        else:
                            display_source = await saved_display.definition_source(scope, version_ref)
                            if display_source is not None:
                                display_source = {**display_source, "source_ref": await references.shorten(
                                    identity, display_source["source_ref"], ("gi",))}
                            else:
                                display_message = "参数化几何，尚无该参数的保存模型。"
                else:
                    result = await family_catalog.info(identity, exact_ref,
                        before_version_no=before_version_no, page_size=page_size)
                    if isinstance(result, ProductFamilyCatalogVersionInfo) and scope is not None:
                        version_ref = result.family_version_ref
                        code = await code_discovery.published_program_card(scope, CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM, version_ref)
                        if result.family_contract.standard_model is not None:
                            display_source = {"source_kind": "family_standard_model",
                                "source_ref": await references.shorten(identity, version_ref, ("fv",))}
                return {"catalog": await project(result, identity),
                    "code": await project(code, identity) if code is not None else None,
                    "display_source": display_source, "display_message": display_message}
        except Exception as exc:
            raise _error(exc) from None

    @router.delete("/library/{kind}", status_code=204)
    async def delete_catalog(kind: Literal["geometry", "families"],
                             reference: str = Query(min_length=1, max_length=16_384),
                             access=Depends(context)):
        identity, _ = access
        try:
            with bind_identity(identity):
                kinds = ("gr",) if kind == "geometry" else ("fr",)
                exact_ref = await references.expand(identity, reference, kinds)
                catalog = geometry_catalog if kind == "geometry" else family_catalog
                await catalog.delete(identity, exact_ref)
                return Response(status_code=204)
        except Exception as exc:
            raise _error(exc) from None

    @router.post("/library/code")
    async def code(body: CodeReadSelection, access=Depends(context)):
        identity, scope = access
        if scope is None:
            raise HTTPException(status_code=409, detail="Select an existing Component Task and Run to view code")
        try:
            with bind_identity(identity):
                exact_ref = await references.expand(identity, body.code_locator, ("sc",))
                result = await code_discovery.read_code(scope, exact_ref, body.source_path, body.view,
                    allowed_code_kinds=_PUBLISHED_CODE_KINDS)
                return await project(result, identity)
        except Exception as exc:
            raise _error(exc) from None

    return router
