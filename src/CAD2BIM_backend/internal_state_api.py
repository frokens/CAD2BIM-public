"""Protected HTTP boundary used only by the independent Runner State MCP."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from CAD2BIM_backend.application.state_resource_service import (
    StateResourceBindingError,
    StateResourceNotFound,
    StateResourceService,
)
from CAD2BIM_backend.application.task_run_service import TaskRunService
from CAD2BIM_backend.domain.control_plane import (
    ExecutionMode,
    ExecutionProtocolStatus,
    ResourceNotFoundError,
    RunStatus,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.state_read_binding import StateReadBindingError
from CAD2BIM_backend.infrastructure.state_access_tokens import (
    StateAccessDenied,
    StateAccessTokenCodec,
    VerifiedStateAccess,
)
from CAD2BIM_backend.ports.identity import IdentityAccessDenied
from CAD2BIM_backend.ports.task_runs import TaskRunRepository

_BINDING_HEADER = "x-cad2bim-state-binding"


class _ReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uri: str


def create_internal_state_router(
    *,
    codec: StateAccessTokenCodec,
    resources: StateResourceService,
    task_runs: TaskRunService,
    runner_repository: TaskRunRepository,
    bind_identity: Callable[[TenantExecutionContext], AbstractContextManager[None]],
) -> APIRouter:
    """Each request rechecks the signed pair and current Run authorization."""

    router = APIRouter(prefix="/internal/state", include_in_schema=False)

    async def authorized(request: Request) -> VerifiedStateAccess:
        authorization = request.headers.get("authorization", "")
        read_binding = request.headers.get(_BINDING_HEADER, "")
        if not authorization.startswith("Bearer ") or not read_binding:
            raise HTTPException(status_code=401, detail="unauthorized")
        try:
            access = codec.verify_pair(authorization[7:], read_binding)
            binding = access.binding
            if access.mode == "completed":
                assert access.subject is not None
                current = await task_runs.completed_state_read_binding(
                    access.subject, binding.task_id, binding.run_id
                )
                if current != binding:
                    raise StateAccessDenied("completed Run read binding has changed")
            else:
                assert access.runner_id is not None
                assert access.writer_generation is not None
                assert access.principal_id is not None
                identity = TenantExecutionContext(
                    tenant_id=binding.owner_id,
                    principal_id=access.principal_id,
                    task_id=binding.task_id,
                    actor_kind=TenantActorKind.RUNNER,
                    command_id=f"state_read_{uuid4().hex}",
                )
                with bind_identity(identity):
                    run = await runner_repository.get_run(binding.owner_id, binding.run_id)
                if (
                    run is None
                    or run.owner_id != binding.owner_id
                    or run.tenant_id != binding.owner_id
                    or run.task_id != binding.task_id
                    or run.thread_id != binding.thread_id
                    or run.status is not RunStatus.RUNNING
                    or run.execution_mode is not ExecutionMode.GRAPH
                    or run.execution_protocol_status is not ExecutionProtocolStatus.PINNED
                    or run.claimed_by != access.runner_id
                    or not run.lease_token
                    or run.writer_generation < 1
                    or run.writer_generation != access.writer_generation
                    or run.lease_expires_at is None
                    or run.lease_expires_at <= datetime.now(timezone.utc)
                ):
                    raise StateAccessDenied("running Run no longer grants State read access")
            return access
        except (
            StateAccessDenied,
            StateReadBindingError,
            IdentityAccessDenied,
            ResourceNotFoundError,
            ValueError,
        ):
            raise HTTPException(status_code=403, detail="State read access denied") from None

    @router.post("/resources/list")
    async def list_resources(request: Request) -> dict[str, object]:
        access = await authorized(request)
        try:
            entries = await resources.list_resources(access.binding)
        except StateResourceBindingError:
            raise HTTPException(status_code=409, detail="exact State binding unavailable") from None
        return {"resources": [asdict(entry) for entry in entries]}

    @router.post("/authorize")
    async def authorize_only(request: Request) -> dict[str, bool]:
        await authorized(request)
        return {"authorized": True}

    @router.post("/resources/read")
    async def read_resource(body: _ReadRequest, request: Request) -> dict[str, object]:
        access = await authorized(request)
        try:
            content = await resources.read_resource(access.binding, body.uri)
        except StateResourceNotFound:
            raise HTTPException(status_code=404, detail="State Resource not found") from None
        except StateResourceBindingError:
            raise HTTPException(status_code=409, detail="exact State binding unavailable") from None
        return {"resource": asdict(content.entry), "text": content.text}

    return router


__all__ = ["create_internal_state_router"]
