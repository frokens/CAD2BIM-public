"""Fixed RS256 bearer verification for the user-facing backend entry."""

from __future__ import annotations

from datetime import datetime, timezone

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jwt.exceptions import InvalidTokenError

from CAD2BIM_backend.domain.identity import AuthenticatedSubject


class AuthenticationError(ValueError):
    """A bearer credential failed verification; never contains token material."""


class JwtRs256Authenticator:
    """Verify a startup-pinned public key, issuer, audience, expiry and subject."""

    def __init__(self, *, public_key_pem: bytes, issuer: str, audience: str) -> None:
        public_key = serialization.load_pem_public_key(public_key_pem)
        if not isinstance(public_key, RSAPublicKey):
            raise ValueError("authentication key must be an RSA public key")
        if not issuer or not audience:
            raise ValueError("authentication issuer and audience are required")
        self._public_key = public_key
        self._issuer = issuer
        self._audience = audience

    def authenticate(self, token: str) -> AuthenticatedSubject:
        if not token:
            raise AuthenticationError("invalid bearer credential")
        try:
            claims = jwt.decode(
                token,
                self._public_key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["iss", "aud", "exp", "sub"]},
            )
            subject = claims["sub"]
            expires_at = claims["exp"]
            if not isinstance(subject, str) or not subject.strip():
                raise AuthenticationError("invalid bearer credential")
            if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
                raise AuthenticationError("invalid bearer credential")
            now = datetime.now(timezone.utc)
            return AuthenticatedSubject(
                issuer=self._issuer,
                subject=subject,
                audience=self._audience,
                authenticated_at=now,
                token_expires_at=datetime.fromtimestamp(expires_at, timezone.utc),
            )
        except (InvalidTokenError, ValueError, OverflowError, OSError):
            raise AuthenticationError("invalid bearer credential") from None


__all__ = ["AuthenticationError", "JwtRs256Authenticator"]
