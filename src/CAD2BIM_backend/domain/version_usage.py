"""Shared validation for version-owned human usage instructions."""

from __future__ import annotations


MAX_VERSION_USAGE_INSTRUCTIONS_CHARS = 8_192
MAX_VERSION_USAGE_INSTRUCTIONS_UTF8_BYTES = 32_768


def validate_version_usage_instructions(
    value: str | None,
    *,
    required: bool,
) -> str | None:
    """Validate bounded Markdown while preserving the author's exact text."""

    if value is None:
        if required:
            raise ValueError("usage_instructions is required")
        return None
    if not isinstance(value, str):
        raise ValueError("usage_instructions must be UTF-8 text")
    if "\x00" in value:
        raise ValueError("usage_instructions must not contain NUL")
    if not value.strip():
        raise ValueError("usage_instructions must not be blank")
    if len(value) > MAX_VERSION_USAGE_INSTRUCTIONS_CHARS:
        raise ValueError("usage_instructions exceeds 8192 Unicode characters")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("usage_instructions must be valid UTF-8 text") from exc
    if len(encoded) > MAX_VERSION_USAGE_INSTRUCTIONS_UTF8_BYTES:
        raise ValueError("usage_instructions exceeds 32768 UTF-8 bytes")
    return value


__all__ = [
    "MAX_VERSION_USAGE_INSTRUCTIONS_CHARS",
    "MAX_VERSION_USAGE_INSTRUCTIONS_UTF8_BYTES",
    "validate_version_usage_instructions",
]
