"""Small shared signing primitive for distinct internal request credentials."""

from __future__ import annotations

from typing import Any

import jwt


class InternalTokenSigner:
    def __init__(self, secret: bytes, *, issuer: str, audience: str) -> None:
        if len(secret) < 32:
            raise ValueError("internal credential secret must contain at least 32 bytes")
        self._secret = secret
        self._issuer = issuer
        self._audience = audience

    def encode(self, claims: dict[str, Any]) -> str:
        return jwt.encode(claims, self._secret, algorithm="HS256")

    def decode(self, token: str) -> dict[str, Any]:
        try:
            return jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["iss", "aud", "iat", "nbf", "exp", "jti", "purpose"]},
            )
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid internal credential") from exc


__all__ = ["InternalTokenSigner"]
