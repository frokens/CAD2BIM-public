"""Request credentials for Runner domain calls, separate from State reads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re
from uuid import uuid4

from fastmcp.server.auth import AccessToken, TokenVerifier

from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.infrastructure.internal_token_signing import InternalTokenSigner
from CAD2BIM_backend.infrastructure.state_access_tokens import (
    FastMcpStateTokenVerifier,
    StateAccessTokenCodec,
)


_ISSUER = "urn:cad2bim:runner-domain"
_AUDIENCE = "cad2bim-runner-domain"
_PURPOSE = "domain-execute"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class RunnerDomainAccessDenied(ValueError):
    """The caller has no valid current domain request credential."""


@dataclass(frozen=True, slots=True)
class VerifiedRunnerDomainAccess:
    owner_id: str
    task_id: str
    run_id: str
    thread_id: str
    principal_id: str
    runner_id: str
    writer_generation: int
    lease_token_sha256: str
    action_id: str
    tool_name: str


class RunnerDomainTokenCodec:
    def __init__(self, secret: bytes, *, lifetime: timedelta = timedelta(minutes=5)) -> None:
        if lifetime <= timedelta(0) or lifetime > timedelta(minutes=15):
            raise ValueError("Runner domain credential lifetime must be between 0 and 15 minutes")
        self._signer = InternalTokenSigner(secret, issuer=_ISSUER, audience=_AUDIENCE)
        self._lifetime = lifetime

    def issue(
        self, grant: RunnerCheckpointGrant, *, action_id: str, tool_name: str
    ) -> str:
        if not _IDENTIFIER.fullmatch(action_id) or not _IDENTIFIER.fullmatch(tool_name):
            raise ValueError("Runner domain action and tool identities must be stable identifiers")
        identity = grant.scope.identity
        if identity.task_id is None:
            raise ValueError("Runner domain request has no Task")
        now = datetime.now(timezone.utc)
        return self._signer.encode(
            {
                "iss": _ISSUER,
                "aud": _AUDIENCE,
                "iat": int(now.timestamp()),
                "nbf": int(now.timestamp()),
                "exp": int((now + self._lifetime).timestamp()),
                "jti": uuid4().hex,
                "purpose": _PURPOSE,
                "owner_id": grant.scope.owner_id,
                "task_id": identity.task_id,
                "run_id": grant.scope.run_id,
                "thread_id": grant.thread_id,
                "principal_id": identity.principal_id,
                "runner_id": grant.runner_id,
                "writer_generation": grant.writer_generation,
                "lease_token_sha256": sha256(grant.lease_token.encode("utf-8")).hexdigest(),
                "action_id": action_id,
                "tool_name": tool_name,
            }
        )

    def verify(self, token: str) -> VerifiedRunnerDomainAccess:
        try:
            claims = self._signer.decode(token)
            if claims.get("purpose") != _PURPOSE:
                raise ValueError("Runner domain credential purpose differs")
            if claims["exp"] - claims["iat"] > self._lifetime.total_seconds() + 1:
                raise ValueError("Runner domain credential lifetime exceeds configured limit")
            names = ("owner_id", "task_id", "run_id", "thread_id", "principal_id", "runner_id")
            if any(
                not isinstance(claims.get(name), str) or not 0 < len(claims[name]) <= 255
                for name in names
            ) or any(
                not isinstance(claims.get(name), str) or not _IDENTIFIER.fullmatch(claims[name])
                for name in ("action_id", "tool_name")
            ):
                raise ValueError("Runner domain identity is malformed")
            generation = claims.get("writer_generation")
            lease_digest = claims.get("lease_token_sha256")
            if (
                type(generation) is not int or generation < 1
                or not isinstance(lease_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", lease_digest)
            ):
                raise ValueError("Runner domain claim is malformed")
            return VerifiedRunnerDomainAccess(
                **{name: claims[name] for name in (*names, "action_id", "tool_name")},
                writer_generation=generation,
                lease_token_sha256=lease_digest,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RunnerDomainAccessDenied("invalid Runner domain credential") from exc


class FastMcpRunnerTokenVerifier(TokenVerifier):
    """Accept either credential purpose; each request path enforces its own grant."""

    def __init__(self, state: StateAccessTokenCodec, domain: RunnerDomainTokenCodec) -> None:
        super().__init__()
        self._state = FastMcpStateTokenVerifier(state)
        self._domain = domain

    async def verify_token(self, token: str) -> AccessToken | None:
        state = await self._state.verify_token(token)
        if state is not None:
            return state
        try:
            access = self._domain.verify(token)
        except RunnerDomainAccessDenied:
            return None
        return AccessToken(
            token=token,
            client_id="cad2bim-domain-client",
            scopes=["domain:execute"],
            subject=access.run_id,
            claims={"tool_name": access.tool_name},
        )


__all__ = [
    "FastMcpRunnerTokenVerifier",
    "RunnerDomainAccessDenied",
    "RunnerDomainTokenCodec",
    "VerifiedRunnerDomainAccess",
]
