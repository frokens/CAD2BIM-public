"""Short-lived, request-bound credentials for the independent State MCP."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from fastmcp.server.auth import AccessToken, TokenVerifier
from pydantic import ValidationError

from CAD2BIM_backend.domain.identity import AuthenticatedSubject
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.domain.state_read_binding import ExactStateReadBinding
from CAD2BIM_backend.infrastructure.internal_token_signing import InternalTokenSigner


_ISSUER = "urn:cad2bim:runner-state"
_AUDIENCE = "cad2bim-runner-state"


class StateAccessDenied(ValueError):
    """A service credential or its exact read binding is invalid."""


@dataclass(frozen=True, slots=True)
class StateAccessPair:
    access_token: str = field(repr=False)
    read_binding: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class VerifiedStateAccess:
    mode: Literal["running", "completed"]
    binding: ExactStateReadBinding
    subject: AuthenticatedSubject | None = None
    runner_id: str | None = None
    writer_generation: int | None = None
    principal_id: str | None = None


class StateAccessTokenCodec:
    """Sign a service credential and a separate, immutable exact binding.

    The access credential pins the hash of the binding. Both are required for
    every MCP and backend request; a URI or client-provided cursor grants nothing.
    """

    def __init__(self, secret: bytes, *, lifetime: timedelta = timedelta(minutes=5)) -> None:
        if len(secret) < 32:
            raise ValueError("State access secret must contain at least 32 bytes")
        if lifetime <= timedelta(0) or lifetime > timedelta(minutes=15):
            raise ValueError("State access lifetime must be between 0 and 15 minutes")
        self._signer = InternalTokenSigner(secret, issuer=_ISSUER, audience=_AUDIENCE)
        self._lifetime = lifetime

    def issue_running(
        self, grant: RunnerCheckpointGrant, binding: ExactStateReadBinding
    ) -> StateAccessPair:
        identity = grant.scope.identity
        if (
            binding.owner_id != grant.scope.owner_id
            or binding.task_id != identity.task_id
            or binding.run_id != grant.scope.run_id
            or binding.thread_id != grant.thread_id
        ):
            raise StateAccessDenied("running binding does not match the current Run claim")
        return self._issue(
            mode="running",
            binding=binding,
            extras={
                "runner_id": grant.runner_id,
                "writer_generation": grant.writer_generation,
                "principal_id": identity.principal_id,
            },
        )

    def issue_completed(
        self, subject: AuthenticatedSubject, binding: ExactStateReadBinding
    ) -> StateAccessPair:
        return self._issue(
            mode="completed",
            binding=binding,
            extras={"subject": subject.model_dump(mode="json")},
            expires_before=subject.token_expires_at,
        )

    def _issue(
        self,
        *,
        mode: Literal["running", "completed"],
        binding: ExactStateReadBinding,
        extras: dict[str, Any],
        expires_before: datetime | None = None,
    ) -> StateAccessPair:
        now = datetime.now(timezone.utc)
        expiry = now + self._lifetime
        if expires_before is not None:
            expiry = min(expiry, expires_before)
        if expiry <= now:
            raise StateAccessDenied("authenticated subject has expired")
        common: dict[str, Any] = {
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(expiry.timestamp()),
            "owner_id": binding.owner_id,
            "task_id": binding.task_id,
            "run_id": binding.run_id,
            "mode": mode,
        }
        read_binding = self._signer.encode(
            {
                **common,
                "jti": uuid4().hex,
                "purpose": "state-read-binding",
                "binding": binding.model_dump(mode="json"),
            },
        )
        access_token = self._signer.encode(
            {
                **common,
                "jti": uuid4().hex,
                "purpose": "state-service-access",
                "binding_sha256": sha256(read_binding.encode("ascii")).hexdigest(),
                **extras,
            },
        )
        return StateAccessPair(access_token=access_token, read_binding=read_binding)

    def _decode(self, token: str, purpose: str) -> dict[str, Any]:
        try:
            claims = self._signer.decode(token)
        except ValueError as exc:
            raise StateAccessDenied("invalid State access credential") from exc
        if claims.get("purpose") != purpose:
            raise StateAccessDenied("invalid State access credential")
        if claims["exp"] - claims["iat"] > self._lifetime.total_seconds() + 1:
            raise StateAccessDenied("invalid State access credential")
        return claims

    def verify_access(self, access_token: str) -> dict[str, Any]:
        return self._decode(access_token, "state-service-access")

    def verify_pair(self, access_token: str, read_binding: str) -> VerifiedStateAccess:
        access = self.verify_access(access_token)
        bound = self._decode(read_binding, "state-read-binding")
        if sha256(read_binding.encode("ascii")).hexdigest() != access.get("binding_sha256"):
            raise StateAccessDenied("access credential does not match its read binding")
        if any(
            access.get(key) != bound.get(key) for key in ("owner_id", "task_id", "run_id", "mode")
        ):
            raise StateAccessDenied("State access scope differs from read binding")
        try:
            binding = ExactStateReadBinding.model_validate(bound["binding"])
        except (KeyError, ValidationError, ValueError, TypeError) as exc:
            raise StateAccessDenied("invalid exact State read binding") from exc
        if (
            binding.owner_id != access.get("owner_id")
            or binding.task_id != access.get("task_id")
            or binding.run_id != access.get("run_id")
        ):
            raise StateAccessDenied("exact State read binding has another scope")
        mode = access.get("mode")
        if mode == "running":
            runner_id = access.get("runner_id")
            generation = access.get("writer_generation")
            principal_id = access.get("principal_id")
            if (
                not isinstance(runner_id, str)
                or not runner_id
                or not isinstance(generation, int)
                or generation < 1
                or not isinstance(principal_id, str)
                or not principal_id
            ):
                raise StateAccessDenied("running State access has no current claim identity")
            return VerifiedStateAccess(
                mode="running",
                binding=binding,
                runner_id=runner_id,
                writer_generation=generation,
                principal_id=principal_id,
            )
        if mode == "completed":
            try:
                subject = AuthenticatedSubject.model_validate(access["subject"])
            except (KeyError, ValidationError, ValueError, TypeError) as exc:
                raise StateAccessDenied("completed State access has no user identity") from exc
            if subject.token_expires_at <= datetime.now(timezone.utc):
                raise StateAccessDenied("completed State user identity has expired")
            return VerifiedStateAccess(mode="completed", binding=binding, subject=subject)
        raise StateAccessDenied("unknown State access mode")


class FastMcpStateTokenVerifier(TokenVerifier):
    """FastMCP HTTP authentication for the backend-issued access credential."""

    def __init__(self, codec: StateAccessTokenCodec) -> None:
        super().__init__()
        self._codec = codec

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = self._codec.verify_access(token)
        except StateAccessDenied:
            return None
        return AccessToken(
            token=token,
            client_id="cad2bim-state-client",
            scopes=["state:read"],
            expires_at=claims["exp"],
            subject=str(claims.get("run_id")),
            claims={"mode": claims.get("mode")},
        )


__all__ = [
    "FastMcpStateTokenVerifier",
    "StateAccessDenied",
    "StateAccessPair",
    "StateAccessTokenCodec",
    "VerifiedStateAccess",
]
