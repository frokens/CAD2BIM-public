"""Deterministic development identity directory; production uses PostgreSQL."""

from __future__ import annotations

import asyncio

from CAD2BIM_backend.domain.identity import (
    AuthenticatedSubject,
    ConfiguredLocalSubject,
    DIRECT_LOCAL_IDENTITY_ISSUER,
    TenantActorKind,
    TenantExecutionContext,
    stable_internal_identity_id,
)


class InMemoryIdentityDirectory:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._identities: dict[tuple[str, str], tuple[str, str]] = {}

    async def resolve_authenticated_subject(
        self,
        subject: AuthenticatedSubject,
        *,
        command_id: str,
    ) -> TenantExecutionContext:
        key = (subject.issuer, subject.subject)
        async with self._lock:
            principal_id, tenant_id = self._identities.setdefault(
                key,
                (
                    stable_internal_identity_id(
                        "principal_", "authenticated-principal", *key
                    ),
                    stable_internal_identity_id("tenant_", "personal-tenant", *key),
                ),
            )
        return TenantExecutionContext(
            tenant_id=tenant_id,
            principal_id=principal_id,
            actor_kind=TenantActorKind.USER,
            command_id=command_id,
        )

    async def resolve_configured_local_subject(
        self,
        subject: ConfiguredLocalSubject,
        *,
        command_id: str,
    ) -> TenantExecutionContext:
        key = (DIRECT_LOCAL_IDENTITY_ISSUER, subject.subject)
        async with self._lock:
            principal_id, tenant_id = self._identities.setdefault(
                key,
                (
                    stable_internal_identity_id(
                        "principal_", "configured-local-principal", *key
                    ),
                    stable_internal_identity_id(
                        "tenant_", "configured-local-tenant", *key
                    ),
                ),
            )
        return TenantExecutionContext(
            tenant_id=tenant_id,
            principal_id=principal_id,
            actor_kind=TenantActorKind.USER,
            command_id=command_id,
        )


__all__ = ["InMemoryIdentityDirectory"]
