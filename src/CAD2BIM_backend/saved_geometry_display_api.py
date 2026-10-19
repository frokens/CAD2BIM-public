"""Authorized display projection of saved entities for the existing Component UI."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from CAD2BIM_backend.application.errors import ConflictError, NotFoundError
from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionApplicationError
from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceIntegrityError
from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService
from CAD2BIM_backend.application.product_family_asset_service import ProductFamilyAssetIntegrityError
from CAD2BIM_backend.application.saved_geometry_display_service import DisplaySourceKind
from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogError
from CAD2BIM_backend.domain.geometry_runtime import GeometryRuntimeError
from CAD2BIM_backend.domain.identity import TenantActorKind
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogError
from CAD2BIM_backend.infrastructure.authentication import AuthenticationError
from CAD2BIM_backend.ports.identity import IdentityAccessDenied
from CAD2BIM_backend.ports.reference_index import ReferenceIndexError


class SavedGeometryDisplayBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_kind: DisplaySourceKind
    source_ref: str = Field(min_length=1, max_length=16_384)


async def authorized_component_read_scope(task_runs, authenticated, task_id, run_id) -> InvocationScope:
    """Reuse the saved Run's execution identity after authorizing its browser reader."""
    context, run = await task_runs.authorized_run_context(authenticated, task_id, run_id)
    if run.module != "component":
        raise NotFoundError("Component Run not found")
    # Resolve before binding USER: the observer would otherwise retain that actor.
    execution_identity, saved_run = await task_runs.checkpoint_observation_context(run.run_id, run.thread_id)
    if (context.task_id != task_id or run.run_id != run_id or run.task_id != task_id
            or run.owner_id != context.tenant_id
            or saved_run.run_id != run_id or saved_run.thread_id != run.thread_id
            or saved_run.task_id != task_id or saved_run.owner_id != context.tenant_id
            or saved_run.module != run.module or saved_run.execution_mode != run.execution_mode
            or saved_run.tenant_id != context.tenant_id
            or execution_identity.actor_kind is not TenantActorKind.RUNNER
            or execution_identity.tenant_id != saved_run.tenant_id
            or execution_identity.principal_id != saved_run.principal_id
            or execution_identity.task_id != saved_run.task_id):
        raise NotFoundError("saved Component execution identity not available")
    return InvocationScope(identity=execution_identity, owner_id=context.tenant_id,
        run_id=saved_run.run_id, execution_mode=saved_run.execution_mode, catalog_frontier=None)


def create_saved_geometry_display_router(*, authenticator, task_runs, display,
                                        references: OpaqueReferenceService, bind_identity):
    router = APIRouter()
    bearer = HTTPBearer(auto_error=False)

    def subject(credential: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credential is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        try:
            return authenticator.authenticate(credential.credentials)
        except AuthenticationError:
            raise HTTPException(status_code=401, detail="unauthorized") from None

    async def invoke(task_id, run_id, source_kind, source_ref, authenticated, *, create):
        try:
            scope = await authorized_component_read_scope(task_runs, authenticated, task_id, run_id)
            execution_identity = scope.identity
            with bind_identity(execution_identity):
                allowed_kinds = {"geometry_resource_version": ("gv",),
                    "family_standard_model": ("fv",), "geometry_inspection": ("gi",)}[source_kind]
                exact_ref = await references.expand(execution_identity, source_ref, allowed_kinds)
                result = await (display.project if create else display.read)(scope, source_kind, exact_ref)
                if result is not None:
                    public_ref = await references.shorten(execution_identity, exact_ref, allowed_kinds)
                    result = {**result, "source": {**result["source"], "source_ref": public_ref}}
            if result is None:
                raise NotFoundError("saved display projection not found")
            return result
        except IdentityAccessDenied:
            raise HTTPException(status_code=403, detail="access denied") from None
        except (NotFoundError, GeometryCatalogError, ProductFamilyCatalogError, ReferenceIndexError):
            raise HTTPException(status_code=404, detail="saved source or display not available") from None
        except GeometryInspectionApplicationError as exc:
            raise HTTPException(status_code=404 if exc.code == "INSPECTION_NOT_FOUND" else 409,
                detail={"code": exc.code, "message": exc.message}) from None
        except ConflictError:
            raise HTTPException(status_code=409, detail="saved display integrity conflict") from None
        except (GeometryResourceIntegrityError, ProductFamilyAssetIntegrityError):
            raise HTTPException(status_code=409, detail="saved source integrity conflict") from None
        except GeometryRuntimeError as exc:
            raise HTTPException(status_code=422, detail={"code": "SAVED_DISPLAY_FAILED", "message": str(exc)}) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get("/tasks/{task_id}/runs/{run_id}/display")
    async def read_display(task_id: str, run_id: str, source_kind: DisplaySourceKind,
                           source_ref: str = Query(min_length=1, max_length=16_384),
                           authenticated=Depends(subject)):
        return await invoke(task_id, run_id, source_kind, source_ref, authenticated, create=False)

    @router.post("/tasks/{task_id}/runs/{run_id}/display")
    async def project_display(task_id: str, run_id: str, body: SavedGeometryDisplayBody,
                              authenticated=Depends(subject)):
        return await invoke(task_id, run_id, body.source_kind, body.source_ref, authenticated, create=True)


    return router
