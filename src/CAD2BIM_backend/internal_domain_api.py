"""One protected Backend boundary for Runner domain actions."""

from __future__ import annotations
from CAD2BIM_backend.domain.data_runtime import CriticalOperationKind

from contextlib import AbstractContextManager
from datetime import datetime, timezone
from hashlib import sha256
import hmac
from typing import Callable

from fastapi import APIRouter, HTTPException, Request

from CAD2BIM_backend.data_application_resources import DataApplicationResources
from CAD2BIM_backend.domain.control_plane import (
    ExecutionMode,
    ExecutionProtocolStatus,
    RunStatus,
)
from CAD2BIM_backend.domain.execution_protocol import (
    ExecutionProtocolBinding,
    ExecutionProtocolMismatchError,
    require_current_execution_protocol,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.ifc_application_resources import (
    AuthoringApplicationResources,
    IfcApplicationResources,
)
from CAD2BIM_backend.infrastructure.runner_domain_tokens import (
    RunnerDomainAccessDenied,
    RunnerDomainTokenCodec,
    VerifiedRunnerDomainAccess,
)
from CAD2BIM_backend.internal_authoring_api import (
    create_internal_authoring_router,
)
from CAD2BIM_backend.internal_data_api import create_internal_data_router
from CAD2BIM_backend.internal_ifc_api import create_internal_ifc_router
from CAD2BIM_backend.internal_reference_api import create_internal_reference_router
from CAD2BIM_backend.ports.task_runs import TaskRunRepository


def create_internal_domain_router(
    *,
    codec: RunnerDomainTokenCodec,
    runner_repository: TaskRunRepository,
    bind_identity: Callable[[TenantExecutionContext], AbstractContextManager[None]],
    execution_protocol: ExecutionProtocolBinding,
    data: DataApplicationResources,
    ifc: IfcApplicationResources,
    authoring: AuthoringApplicationResources,
) -> APIRouter:
    """Verify the current Run claim once per request and bind one Application graph."""

    router = APIRouter(prefix="/internal/domain", include_in_schema=False)

    async def authorized(
        request: Request, *, tool_name: str | None = None
    ) -> tuple[VerifiedRunnerDomainAccess, TenantExecutionContext]:
        bearer = request.headers.get("authorization", "")
        if not bearer.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="unauthorized")
        try:
            access = codec.verify(bearer[7:])
            if tool_name is not None and access.tool_name != tool_name:
                raise RunnerDomainAccessDenied("credential belongs to another domain tool")
            identity = TenantExecutionContext(
                tenant_id=access.owner_id,
                principal_id=access.principal_id,
                task_id=access.task_id,
                actor_kind=TenantActorKind.RUNNER,
                command_id=access.action_id,
            )
            with bind_identity(identity):
                run = await runner_repository.get_run_with_execution_identity(
                    access.owner_id, access.run_id
                )
            if (
                run is None
                or run.owner_id != access.owner_id
                or run.tenant_id != access.owner_id
                or run.task_id != access.task_id
                or run.thread_id != access.thread_id
                or run.status is not RunStatus.RUNNING
                or run.cancel_requested
                or run.execution_mode is not ExecutionMode.GRAPH
                or run.execution_protocol_status is not ExecutionProtocolStatus.PINNED
                or run.claimed_by != access.runner_id
                or run.writer_generation != access.writer_generation
                or not run.lease_token
                or not hmac.compare_digest(
                    sha256(run.lease_token.encode("utf-8")).hexdigest(),
                    access.lease_token_sha256,
                )
                or run.lease_expires_at is None
                or run.lease_expires_at <= datetime.now(timezone.utc)
                or run.execution_protocol is None
            ):
                raise RunnerDomainAccessDenied("Runner no longer owns the Run")
            require_current_execution_protocol(
                run.execution_protocol,
                expected=execution_protocol,
                module=run.module,
                stage_key=run.stage_key,
            )
            tenant_id, principal_id = run.require_execution_identity()
            if (tenant_id, principal_id) != (access.owner_id, access.principal_id):
                raise RunnerDomainAccessDenied("Runner identity differs from the Run")
            return access, identity
        except (
            RunnerDomainAccessDenied,
            ExecutionProtocolMismatchError,
            ValueError,
        ):
            raise HTTPException(status_code=403, detail="Runner domain access denied") from None

    @router.post("/authorize")
    async def authorize_only(request: Request) -> dict[str, bool]:
        await authorized(request)
        return {"authorized": True}

    async def authoring_operation_result(scope, published):
        if published.operation.operation_kind is CriticalOperationKind.AUTHORING_PUBLISH and published.operation.status.value != "succeeded":
            return None
        return await authoring.geometry_authoring.operation_result(scope, published)

    router.include_router(create_internal_data_router(
        authorized=authorized,
        bind_identity=bind_identity,
        data_runtime=data.runtime,
        data_query=data.query,
        data_query_sql=data.query_sql,
        data_query_python=data.query_python,
        data_json=data.json,
        data_transform=data.transform,
        code_discovery=data.code_discovery,
        authoring_operation_result=authoring_operation_result,
        family_version_reference=authoring.family_catalog.version_ref_for_visible_id,
        geometry_inspection_operation_result=authoring.geometry_inspection.operation_result,
        geometry_inspection=authoring.geometry_inspection,
        references=data.references,
    ))
    router.include_router(create_internal_ifc_router(
        authorized=authorized,
        bind_identity=bind_identity,
        ifc=ifc,
        data_runtime=data.runtime,
        family_catalog=authoring.family_catalog,
        references=data.references,
    ))
    router.include_router(create_internal_authoring_router(
        authorized=authorized,
        bind_identity=bind_identity,
        authoring=authoring,
        data_runtime=data.runtime,
    ))
    router.include_router(create_internal_reference_router(
        authorized=authorized,
        bind_identity=bind_identity,
        references=data.references,
    ))
    return router


__all__ = ["create_internal_domain_router"]
