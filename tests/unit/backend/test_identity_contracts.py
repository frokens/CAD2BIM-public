from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.identity import (
    AuthenticatedSubject,
    TenantActorKind,
    TenantExecutionContext,
    stable_internal_identity_id,
)


def _subject() -> AuthenticatedSubject:
    authenticated_at = datetime(2026, 7, 19, tzinfo=timezone.utc)
    return AuthenticatedSubject(
        issuer="https://cad2bim-dev-example-user.authing.cn/oidc/",
        subject=" cad2bim-test-subject ",
        audience="6a591ca69e4b743fd3376ef8",
        authenticated_at=authenticated_at,
        token_expires_at=authenticated_at + timedelta(hours=1),
    )


def test_authenticated_subject_is_canonical_and_contains_no_credentials() -> None:
    subject = _subject()

    assert subject.issuer == "https://cad2bim-dev-example-user.authing.cn/oidc"
    assert subject.subject == "cad2bim-test-subject"
    assert set(subject.model_dump()) == {
        "issuer",
        "subject",
        "audience",
        "authenticated_at",
        "token_expires_at",
    }
    assert not {
        "access_token",
        "refresh_token",
        "authorization_code",
        "claims",
        "cookie",
    } & set(subject.model_dump())


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("issuer", "http://auth.example.test/oidc"),
        ("issuer", "https://auth.example.test/oidc?tenant=forged"),
        ("subject", "   "),
        ("audience", "   "),
    ),
)
def test_authenticated_subject_rejects_noncanonical_identity(
    field: str,
    value: str,
) -> None:
    payload = _subject().model_dump()
    payload[field] = value

    with pytest.raises(ValidationError):
        AuthenticatedSubject.model_validate(payload)


def test_authenticated_subject_rejects_expiry_before_authentication() -> None:
    payload = _subject().model_dump()
    payload["token_expires_at"] = payload["authenticated_at"] - timedelta(seconds=1)

    with pytest.raises(ValidationError, match="token_expires_at"):
        AuthenticatedSubject.model_validate(payload)


@pytest.mark.parametrize(
    "issuer",
    (
        "http://127.0.0.1:8123/oidc",
        "http://localhost:8123/oidc",
        "http://[::1]:8123/oidc",
    ),
)
def test_authenticated_subject_accepts_loopback_http_for_local_test_issuer(
    issuer: str,
) -> None:
    payload = _subject().model_dump()
    payload["issuer"] = issuer

    assert AuthenticatedSubject.model_validate(payload).issuer == issuer


def test_principal_tenant_access_and_runner_context_keep_distinct_identities() -> None:
    subject = _subject()
    principal_id = stable_internal_identity_id(
        "principal_", "authenticated-principal", subject.issuer, subject.subject
    )
    tenant_id = stable_internal_identity_id(
        "tenant_", "personal-tenant", subject.issuer, subject.subject
    )
    context = TenantExecutionContext(
        tenant_id=tenant_id,
        task_id="task-1",
        principal_id=principal_id,
        actor_kind=TenantActorKind.RUNNER,
        command_id="claim-1",
    )

    assert context.tenant_id != context.principal_id
    assert context.actor_kind is TenantActorKind.RUNNER
    assert "token" not in repr(context).lower()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("tenant_id", "forged-tenant"),
        ("schema_version", "authenticated-subject/v1"),
    ),
)
def test_identity_contracts_forbid_client_supplied_extra_fields(
    field: str,
    value: str,
) -> None:
    payload = _subject().model_dump()
    payload[field] = value

    with pytest.raises(ValidationError):
        AuthenticatedSubject.model_validate(payload)


def test_internal_identity_ids_are_stable_opaque_and_boundary_safe() -> None:
    first = stable_internal_identity_id(
        "principal_",
        "authenticated-principal",
        "https://identity.example/oidc",
        "subject:part",
    )
    repeated = stable_internal_identity_id(
        "principal_",
        "authenticated-principal",
        "https://identity.example/oidc",
        "subject:part",
    )
    ambiguous_without_boundaries = stable_internal_identity_id(
        "principal_",
        "authenticated-principal",
        "https://identity.example/oidc:subject",
        "part",
    )

    assert first == repeated
    assert first != ambiguous_without_boundaries
    assert first.startswith("principal_")
    assert "identity.example" not in first
    assert "subject" not in first
