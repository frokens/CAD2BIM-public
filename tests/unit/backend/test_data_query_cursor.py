from __future__ import annotations

import base64

import pytest

from CAD2BIM_backend.infrastructure.data_query.cursor_codec import (
    CursorCodec,
    CursorCodecError,
    CursorKeyError,
)


def _key(byte: bytes = b"k") -> str:
    return base64.urlsafe_b64encode(byte * 32).rstrip(b"=").decode("ascii")


def _claims() -> dict[str, object]:
    return {
        "schema": 1,
        "tool": "data_query_rows",
        "scope": "scope-secret-digest",
        "query": {"package_alias": "data_001", "page_size": 2},
        "last_key": ["entrec_00000001"],
        "cumulative": 2,
        "total": 5,
    }


def test_cursor_is_deterministic_opaque_and_self_contained() -> None:
    codec = CursorCodec(_key())

    first = codec.encode(_claims())
    second = codec.encode(_claims())

    assert first == second
    assert first.startswith("cur.")
    assert "data_query_rows" not in first
    assert "data_001" not in first
    assert "entrec_" not in first
    assert codec.decode(first) == _claims()


@pytest.mark.parametrize("position", (0, -1))
def test_cursor_rejects_tampering(position: int) -> None:
    codec = CursorCodec(_key())
    token = codec.encode(_claims())
    index = position % len(token)
    replacement = "A" if token[index] != "A" else "B"
    forged = token[:index] + replacement + token[index + 1 :]

    with pytest.raises(CursorCodecError) as raised:
        codec.decode(forged)

    assert raised.value.code == "CURSOR_INVALID"


def test_cursor_key_is_stable_across_instances_but_not_interchangeable() -> None:
    first_process = CursorCodec(_key(b"a"))
    restarted_process = CursorCodec(_key(b"a"))
    other_key = CursorCodec(_key(b"b"))
    token = first_process.encode(_claims())

    assert restarted_process.decode(token) == _claims()
    with pytest.raises(CursorCodecError, match="invalid cursor"):
        other_key.decode(token)


def test_cursor_encode_respects_public_token_capacity() -> None:
    codec = CursorCodec(_key())
    claims = {"value": "x" * 12_247}
    token = codec.encode(claims)

    assert len(token) == 16_384
    assert codec.decode(token) == claims
    # One more plaintext byte still fits the JSON bound, but its encrypted token
    # would exceed the public boundary and could never be submitted for decoding.
    with pytest.raises(CursorCodecError, match="cursor payload is too large"):
        codec.encode({"value": "x" * 12_248})


@pytest.mark.parametrize("value", ("", "not base64!", "YQ"))
def test_cursor_rejects_missing_or_weak_server_keys(value: str) -> None:
    with pytest.raises(CursorKeyError):
        CursorCodec(value)
