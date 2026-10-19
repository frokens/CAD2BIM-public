from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from CAD2BIM_backend.domain.identity import (
    AuthenticatedSubject,
    TenantActorKind,
)
from CAD2BIM_backend.infrastructure.memory.identity_directory import (
    InMemoryIdentityDirectory,
)


def _subject(*, issuer: str, subject: str) -> AuthenticatedSubject:
    authenticated_at = datetime(2026, 7, 19, tzinfo=timezone.utc)
    return AuthenticatedSubject(
        issuer=issuer,
        subject=subject,
        audience="cad2bim-test-app",
        authenticated_at=authenticated_at,
        token_expires_at=authenticated_at + timedelta(hours=1),
    )


def test_memory_identity_directory_is_stable_and_issuer_scoped() -> None:
    async def scenario():
        directory = InMemoryIdentityDirectory()
        first_subject = _subject(
            issuer="https://identity-a.example/oidc",
            subject="same-public-subject",
        )
        second_issuer = _subject(
            issuer="https://identity-b.example/oidc",
            subject="same-public-subject",
        )
        first, repeated, distinct = await asyncio.gather(
            directory.resolve_authenticated_subject(first_subject, command_id="command-1"),
            directory.resolve_authenticated_subject(first_subject, command_id="command-2"),
            directory.resolve_authenticated_subject(second_issuer, command_id="command-3"),
        )
        return first, repeated, distinct

    first, repeated, distinct = asyncio.run(scenario())

    assert first.tenant_id == repeated.tenant_id
    assert first.principal_id == repeated.principal_id
    assert first.command_id != repeated.command_id
    assert first.tenant_id != distinct.tenant_id
    assert first.principal_id != distinct.principal_id
    assert first.actor_kind is TenantActorKind.USER
    assert "same-public-subject" not in first.tenant_id
