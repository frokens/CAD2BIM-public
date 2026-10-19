"""PostgreSQL-backed verified-subject to personal-tenant resolution."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from CAD2BIM_backend.domain.identity import (
    AuthenticatedSubject,
    ConfiguredLocalSubject,
    DIRECT_LOCAL_IDENTITY_ISSUER,
    TenantActorKind,
    TenantExecutionContext,
    stable_internal_identity_id,
)
from CAD2BIM_backend.ports.identity import IdentityAccessDenied


class PostgresIdentityDirectory:
    """Provision one stable personal tenant for one verified issuer/subject."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def resolve_authenticated_subject(
        self,
        subject: AuthenticatedSubject,
        *,
        command_id: str,
    ) -> TenantExecutionContext:
        principal_id = stable_internal_identity_id(
            "principal_",
            "authenticated-principal",
            subject.issuer,
            subject.subject,
        )
        tenant_id = stable_internal_identity_id(
            "tenant_",
            "personal-tenant",
            subject.issuer,
            subject.subject,
        )
        try:
            async with self._sessions() as session, session.begin():
                result = await session.execute(
                    text(
                        "SELECT principal_id, tenant_id "
                        "FROM cad2bim_resolve_identity("
                        ":issuer, :subject, :principal_id, :tenant_id)"
                    ),
                    {
                        "issuer": subject.issuer,
                        "subject": subject.subject,
                        "principal_id": principal_id,
                        "tenant_id": tenant_id,
                    },
                )
                resolved = result.mappings().one_or_none()
        except DBAPIError as exc:
            if "identity_access_denied" in str(exc.orig):
                raise IdentityAccessDenied("verified identity is not allowed") from None
            raise
        if resolved is None:
            raise IdentityAccessDenied("verified identity could not be resolved")
        if (
            resolved["principal_id"] != principal_id
            or resolved["tenant_id"] != tenant_id
        ):
            raise IdentityAccessDenied("verified identity mapping is inconsistent")
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
        principal_id = stable_internal_identity_id(
            "principal_",
            "configured-local-principal",
            DIRECT_LOCAL_IDENTITY_ISSUER,
            subject.subject,
        )
        tenant_id = stable_internal_identity_id(
            "tenant_",
            "configured-local-tenant",
            DIRECT_LOCAL_IDENTITY_ISSUER,
            subject.subject,
        )
        try:
            async with self._sessions() as session, session.begin():
                result = await session.execute(
                    text(
                        "SELECT principal_id, tenant_id "
                        "FROM cad2bim_resolve_identity("
                        ":issuer, :subject, :principal_id, :tenant_id)"
                    ),
                    {
                        "issuer": DIRECT_LOCAL_IDENTITY_ISSUER,
                        "subject": subject.subject,
                        "principal_id": principal_id,
                        "tenant_id": tenant_id,
                    },
                )
                resolved = result.mappings().one_or_none()
        except DBAPIError as exc:
            if "identity_access_denied" in str(exc.orig):
                raise IdentityAccessDenied("configured local identity is not allowed") from None
            raise
        if resolved is None:
            raise IdentityAccessDenied("configured local identity could not be resolved")
        if (
            resolved["principal_id"] != principal_id
            or resolved["tenant_id"] != tenant_id
        ):
            raise IdentityAccessDenied("configured local identity mapping is inconsistent")
        return TenantExecutionContext(
            tenant_id=tenant_id,
            principal_id=principal_id,
            actor_kind=TenantActorKind.USER,
            command_id=command_id,
        )


__all__ = ["PostgresIdentityDirectory"]
