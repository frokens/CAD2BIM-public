"""Verified identity setup for PostgreSQL component tests.

These helpers exercise the production identity directory.  They do not bypass
JWT verification at the public API boundary and never create administrator
fallback identities.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from CAD2BIM_backend.domain.identity import (
    AuthenticatedSubject,
    TenantExecutionContext,
)
from CAD2BIM_backend.infrastructure.postgres.identity_directory import (
    PostgresIdentityDirectory,
)


async def prepare_test_tenant_context(
    sessions: async_sessionmaker[AsyncSession],
    *,
    subject: str,
) -> TenantExecutionContext:
    now = datetime.now(UTC)
    return await PostgresIdentityDirectory(sessions).resolve_authenticated_subject(
        AuthenticatedSubject(
            issuer="https://cad2bim.local/integration-test",
            subject=subject,
            audience="cad2bim-integration-test",
            authenticated_at=now,
            token_expires_at=now + timedelta(hours=1),
        ),
        command_id=f"prepare:{subject}",
    )


__all__ = ["prepare_test_tenant_context"]
