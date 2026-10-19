"""Server-derived invocation authority shared across capability runtimes."""

from __future__ import annotations

from pydantic import Field, model_validator

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.data_runtime import DataRuntimeModel
from CAD2BIM_backend.domain.identity import TenantExecutionContext


class InvocationScope(DataRuntimeModel):
    """Server-derived authority for one Data Runtime invocation."""

    identity: TenantExecutionContext
    owner_id: str = Field(min_length=1, max_length=255)
    run_id: str = Field(min_length=1, max_length=80)
    execution_mode: ExecutionMode
    catalog_frontier: int | None = Field(ge=0)

    @model_validator(mode="after")
    def require_task(self) -> "InvocationScope":
        if self.identity.task_id is None:
            raise ValueError("InvocationScope identity requires task_id")
        return self


def invocation_scope_digest(scope: InvocationScope) -> str:
    """Digest the complete server-derived authority for process-private bindings."""

    return sha256_digest(
        {
            "tenant_id": scope.identity.tenant_id,
            "principal_id": scope.identity.principal_id,
            "task_id": scope.identity.task_id,
            "actor_kind": scope.identity.actor_kind.value,
            "owner_id": scope.owner_id,
            "run_id": scope.run_id,
            "execution_mode": scope.execution_mode.value,
            "catalog_frontier": scope.catalog_frontier,
        }
    )


__all__ = ["InvocationScope", "invocation_scope_digest"]
