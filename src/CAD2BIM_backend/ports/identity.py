"""Identity directory boundary used after public token verification."""

from __future__ import annotations

from typing import Protocol

from CAD2BIM_backend.domain.identity import (
    AuthenticatedSubject,
    ConfiguredLocalSubject,
    TenantExecutionContext,
)


class IdentityAccessDenied(RuntimeError):
    """The verified subject is known but cannot access CAD2BIM."""


class IdentityDirectory(Protocol):
    async def resolve_authenticated_subject(
        self,
        subject: AuthenticatedSubject,
        *,
        command_id: str,
    ) -> TenantExecutionContext: ...

    async def resolve_configured_local_subject(
        self,
        subject: ConfiguredLocalSubject,
        *,
        command_id: str,
    ) -> TenantExecutionContext: ...


__all__ = ["IdentityAccessDenied", "IdentityDirectory"]
