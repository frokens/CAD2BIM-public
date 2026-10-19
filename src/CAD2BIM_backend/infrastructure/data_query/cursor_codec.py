"""Authenticated, deterministic cursors for opaque queries and references.

The token deliberately contains no readable query state.  A cursor is an
encrypted continuation capability; the query layer remains responsible for
checking tool, scope, resource, and query bindings after decoding it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.ports.opaque_cursor import OpaqueCursorCodecError


CURSOR_PREFIX = "cur"
CURSOR_INVALID = "CURSOR_INVALID"
MIN_SERVER_KEY_BYTES = 32

_MAX_SERVER_KEY_BYTES = 4096
# Bound the actual continuation JSON and its encrypted token when issued.
# The token boundary is identical to the public DTO/tool boundary.
_MAX_PLAINTEXT_BYTES = 12 * 1024
_MAX_TOKEN_CHARS = 16_384
_MAX_NESTING_DEPTH = 16
_MAX_CONTAINER_ITEMS = 4096
_MAX_STRING_CHARS = 32 * 1024
_MAX_KEY_CHARS = 256
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_AAD_DOMAIN = b"CAD2BIM opaque cursor"
_HKDF_INFO = b"CAD2BIM opaque cursor AESSIV key"
_KID_DOMAIN = b"CAD2BIM opaque cursor key id\x00"


class CursorCodecError(OpaqueCursorCodecError):
    """A cursor is malformed, unauthentic, or outside the bounded JSON shape."""

    code = CURSOR_INVALID

    def __init__(self, message: str = "invalid cursor") -> None:
        super().__init__(message)
        self.code = CURSOR_INVALID


class CursorKeyError(ValueError):
    """The server's cursor-key configuration is invalid."""


def parse_cursor_server_key(value: str) -> bytes:
    """Parse an unpadded base64url server secret used across all replicas."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise CursorKeyError("cursor server key must be non-empty unpadded base64url")
    if "=" in value or _BASE64URL.fullmatch(value) is None:
        raise CursorKeyError("cursor server key must be unpadded base64url")
    try:
        encoded = value.encode("ascii")
        raw = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise CursorKeyError("cursor server key is malformed base64url") from exc
    if _base64url_encode(raw) != value:
        raise CursorKeyError("cursor server key is not canonical base64url")
    if not MIN_SERVER_KEY_BYTES <= len(raw) <= _MAX_SERVER_KEY_BYTES:
        raise CursorKeyError(
            f"cursor server key must decode to at least {MIN_SERVER_KEY_BYTES} bytes"
        )
    return raw


def derive_cursor_key_id(server_key: bytes) -> str:
    """Return a stable, non-secret identifier for a configured server key."""

    if not isinstance(server_key, bytes) or len(server_key) < MIN_SERVER_KEY_BYTES:
        raise CursorKeyError(
            f"cursor server key must contain at least {MIN_SERVER_KEY_BYTES} bytes"
        )
    digest = hashlib.sha256(_KID_DOMAIN + server_key).digest()[:9]
    return _base64url_encode(digest)


class CursorCodec:
    """Encode and decode opaque self-contained cursor payloads."""

    def __init__(self, server_key_base64url: str) -> None:
        server_key = parse_cursor_server_key(server_key_base64url)
        derived_key = HKDF(
            algorithm=hashes.SHA256(),
            length=64,
            salt=None,
            info=_HKDF_INFO,
        ).derive(server_key)
        self._cipher = AESSIV(derived_key)
        self._key_id = derive_cursor_key_id(server_key)
        self._associated_data = [_AAD_DOMAIN, CURSOR_PREFIX.encode(), self._key_id.encode()]

    @property
    def key_id(self) -> str:
        return self._key_id

    def encode(self, payload: Mapping[str, Any]) -> str:
        """Encrypt a bounded JSON mapping into a deterministic opaque token."""

        if not isinstance(payload, Mapping):
            raise CursorCodecError("cursor payload must be a JSON mapping")
        _validate_json_shape(payload)
        try:
            plaintext = canonical_json(payload).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CursorCodecError("cursor payload is outside canonical JSON") from exc
        if len(plaintext) > _MAX_PLAINTEXT_BYTES:
            raise CursorCodecError("cursor payload is too large")

        ciphertext = self._cipher.encrypt(plaintext, self._associated_data)
        token = f"{CURSOR_PREFIX}.{self._key_id}.{_base64url_encode(ciphertext)}"
        if len(token) > _MAX_TOKEN_CHARS:
            raise CursorCodecError("cursor payload is too large")
        return token

    def decode(self, token: str) -> dict[str, Any]:
        """Authenticate and decrypt a cursor into its JSON mapping."""

        if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_CHARS:
            raise CursorCodecError()
        try:
            prefix, key_id, ciphertext_text = token.split(".")
        except ValueError as exc:
            raise CursorCodecError() from exc
        if prefix != CURSOR_PREFIX or key_id != self._key_id or not ciphertext_text:
            raise CursorCodecError()

        try:
            ciphertext = _base64url_decode(ciphertext_text)
            plaintext = self._cipher.decrypt(ciphertext, self._associated_data)
        except (InvalidTag, ValueError) as exc:
            raise CursorCodecError() from exc
        if len(plaintext) > _MAX_PLAINTEXT_BYTES:
            raise CursorCodecError()

        try:
            value = json.loads(
                plaintext.decode("utf-8"),
                object_pairs_hook=_mapping_without_duplicate_keys,
            )
            if not isinstance(value, dict):
                raise ValueError("cursor root is not a mapping")
            _validate_json_shape(value)
            if canonical_json(value).encode("utf-8") != plaintext:
                raise ValueError("cursor JSON is not canonical")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CursorCodecError() from exc
        return value


def _validate_json_shape(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [_MAX_CONTAINER_ITEMS]
    if depth > _MAX_NESTING_DEPTH:
        raise CursorCodecError("cursor payload is nested too deeply")

    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise CursorCodecError("cursor payload contains a non-finite number")
        return
    if isinstance(value, str):
        if len(value) > _MAX_STRING_CHARS:
            raise CursorCodecError("cursor payload string is too large")
        return
    if isinstance(value, Mapping):
        budget[0] -= len(value)
        if budget[0] < 0:
            raise CursorCodecError("cursor payload contains too many items")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > _MAX_KEY_CHARS:
                raise CursorCodecError("cursor payload contains an invalid mapping key")
            _validate_json_shape(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, (list, tuple)):
        budget[0] -= len(value)
        if budget[0] < 0:
            raise CursorCodecError("cursor payload contains too many items")
        for item in value:
            _validate_json_shape(item, depth=depth + 1, budget=budget)
        return
    raise CursorCodecError("cursor payload is outside the JSON domain")


def _mapping_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate cursor JSON key")
        result[key] = value
    return result


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not value or "=" in value or _BASE64URL.fullmatch(value) is None:
        raise ValueError("invalid base64url")
    try:
        raw = value.encode("ascii")
        decoded = base64.b64decode(
            raw + b"=" * (-len(raw) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("invalid base64url") from exc
    if _base64url_encode(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded


__all__ = [
    "CURSOR_INVALID",
    "CURSOR_PREFIX",
    "MIN_SERVER_KEY_BYTES",
    "CursorCodec",
    "CursorCodecError",
    "CursorKeyError",
    "derive_cursor_key_id",
    "parse_cursor_server_key",
]
