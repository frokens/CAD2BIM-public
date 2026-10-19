"""Bind a verified user identity to one Task for application commands."""

from __future__ import annotations

from uuid import uuid4

from pydantic import ValidationError

from CAD2BIM_backend.domain.identity import (
    AuthenticatedSubject,
    TenantActorKind,
    TenantExecutionContext,
)
from CAD2BIM_backend.ports.identity import IdentityAccessDenied, IdentityDirectory


async def resolve_user_task_identity(
    directory: IdentityDirectory,
    subject: AuthenticatedSubject,
    *,
    task_id: str | None = None,
) -> TenantExecutionContext:
    context = await directory.resolve_authenticated_subject(
        subject, command_id=f"api_{uuid4().hex}"
    )
    if context.actor_kind is not TenantActorKind.USER or context.task_id is not None:
        raise IdentityAccessDenied("verified subject did not resolve to a user")
    if task_id is None:
        return context
    try:
        return TenantExecutionContext(
            tenant_id=context.tenant_id,
            principal_id=context.principal_id,
            task_id=task_id,
            actor_kind=context.actor_kind,
            command_id=context.command_id,
        )
    except ValidationError:
        raise ValueError("invalid task ID") from None


__all__ = ["resolve_user_task_identity"]
