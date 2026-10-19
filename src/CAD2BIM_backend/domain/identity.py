"""Current identity and tenant contracts independent from provider tokens."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import hashlib
import unicodedata
from typing import Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
DIRECT_LOCAL_IDENTITY_ISSUER = "urn:cad2bim:identity:direct-local"


class _IdentityContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_nonempty(value: str) -> str:
    canonical = value.strip()
    if not canonical:
        raise ValueError("identity value must not be empty")
    return canonical


def _canonical_https_issuer(value: str) -> str:
    parsed = urlsplit(_canonical_nonempty(value))
    loopback_http = parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    if (parsed.scheme != "https" and not loopback_http) or not parsed.hostname:
        raise ValueError("issuer must use HTTPS except for a loopback test issuer")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("issuer must not contain user information")
    if parsed.query or parsed.fragment:
        raise ValueError("issuer must not contain query or fragment")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("identity timestamps must be timezone-aware")
    return value


def stable_internal_identity_id(prefix: str, namespace: str, *parts: str) -> str:
    """Create a deterministic opaque server ID without exposing provider subjects."""

    canonical_prefix = _canonical_nonempty(prefix)
    canonical_namespace = _canonical_nonempty(namespace)
    canonical_parts = tuple(_canonical_nonempty(part) for part in parts)
    hasher = hashlib.sha256()
    for value in (canonical_namespace, *canonical_parts):
        encoded = value.encode("utf-8")
        hasher.update(len(encoded).to_bytes(8, byteorder="big"))
        hasher.update(encoded)
    digest = hasher.hexdigest()
    return f"{canonical_prefix}{digest}"


class AuthenticatedSubject(_IdentityContract):
    """Verified public-provider identity without credentials or raw claims."""

    issuer: str = Field(min_length=1, max_length=512)
    subject: str = Field(min_length=1, max_length=255)
    audience: str = Field(min_length=1, max_length=255)
    authenticated_at: datetime
    token_expires_at: datetime

    @field_validator("issuer")
    @classmethod
    def _validate_issuer(cls, value: str) -> str:
        return _canonical_https_issuer(value)

    @field_validator("subject", "audience")
    @classmethod
    def _validate_identity_value(cls, value: str) -> str:
        return _canonical_nonempty(value)

    @field_validator("authenticated_at", "token_expires_at")
    @classmethod
    def _validate_timestamp(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def _validate_expiry(self) -> Self:
        if self.token_expires_at <= self.authenticated_at:
            raise ValueError("token_expires_at must be after authenticated_at")
        return self


class ConfiguredLocalSubject(_IdentityContract):
    """Startup-configured identity for the local Direct MCP adapter.

    This is deliberately separate from :class:`AuthenticatedSubject`: no token
    was presented, and the value must never be inferred from the OS account.
    """

    subject: str = Field(min_length=1, max_length=255)

    @field_validator("subject")
    @classmethod
    def _validate_subject(cls, value: str) -> str:
        canonical = _canonical_nonempty(value)
        if any(unicodedata.category(character).startswith("C") for character in canonical):
            raise ValueError("configured local subject contains control characters")
        return canonical


class TenantActorKind(StrEnum):
    USER = "user"
    RUNNER = "runner"
    SYSTEM_CURATOR = "system_curator"


class TenantExecutionContext(_IdentityContract):
    """Identity restored at a command boundary, never from a client body."""

    tenant_id: str = Field(pattern=_OPAQUE_ID_PATTERN)
    principal_id: str = Field(pattern=_OPAQUE_ID_PATTERN)
    task_id: str | None = Field(default=None, pattern=_OPAQUE_ID_PATTERN)
    actor_kind: TenantActorKind
    command_id: str = Field(pattern=_OPAQUE_ID_PATTERN)


__all__ = [
    "AuthenticatedSubject",
    "ConfiguredLocalSubject",
    "DIRECT_LOCAL_IDENTITY_ISSUER",
    "TenantActorKind",
    "TenantExecutionContext",
    "stable_internal_identity_id",
]
