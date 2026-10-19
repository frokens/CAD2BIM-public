"""Entry-neutral contracts for querying the official IFC documentation."""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import unquote, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


OFFICIAL_IFC_DOCS_HOST = "standards.buildingsmart.org"
OFFICIAL_IFC_DOCS_PREFIX = "/IFC/DEV/IFC4_3/HTML/"
OFFICIAL_IFC_DOCS_RELEASE_PREFIX = "/IFC/RELEASE/IFC4_3/HTML/"
OFFICIAL_IFC_DOCS_TIMEOUT_SECONDS = 45.0
_LEGACY_HOST = "ifc43-docs.standards.buildingsmart.org"
_LEGACY_PREFIX = "/IFC/RELEASE/IFC4x3/HTML/"
_MAX_QUERY_CHARS = 1_000
_MAX_QUERY_UTF8_BYTES = 4_096
_MAX_URL_CHARS = 2_048


class IfcOfficialDocsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IfcOfficialDocsErrorCode(StrEnum):
    UNAVAILABLE = "IFC_DOCS_UNAVAILABLE"
    RESPONSE_INVALID = "IFC_DOCS_RESPONSE_INVALID"


class IfcOfficialDocsSearchQuery(IfcOfficialDocsModel):
    query: str = Field(
        min_length=1,
        max_length=_MAX_QUERY_CHARS,
        description="Natural-language text or an IFC concept matched against the official published DEV search index.",
    )
    max_results: int = Field(
        default=3,
        strict=True,
        ge=1,
        le=5,
        description="Maximum official search cards to return; default 3 and maximum 5.",
    )

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must contain non-whitespace text")
        if len(value.encode("utf-8")) > _MAX_QUERY_UTF8_BYTES:
            raise ValueError("query exceeds 4,096 UTF-8 bytes")
        return value


class IfcOfficialDocsSearchCard(IfcOfficialDocsModel):
    title: str = Field(min_length=1, max_length=512)
    url: str = Field(min_length=1, max_length=_MAX_URL_CHARS)
    snippet: str = Field(max_length=4_096)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return canonical_official_ifc_doc_url(value)


class IfcOfficialDocsSearchResult(IfcOfficialDocsModel):
    query: str = Field(min_length=1, max_length=_MAX_QUERY_CHARS)
    results: tuple[IfcOfficialDocsSearchCard, ...] = Field(max_length=5)


class IfcOfficialDocsReadQuery(IfcOfficialDocsModel):
    url: str = Field(
        min_length=1,
        max_length=_MAX_URL_CHARS,
        description="Exact official page URL selected from ifc_search_official_docs results.",
    )
    focus: str | None = Field(
        default=None,
        min_length=1,
        max_length=_MAX_QUERY_CHARS,
        description="Optional concept or question used only to select relevant original page blocks.",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return canonical_official_ifc_doc_url(value)

    @field_validator("focus")
    @classmethod
    def validate_focus(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("focus must contain non-whitespace text")
        if len(value.encode("utf-8")) > _MAX_QUERY_UTF8_BYTES:
            raise ValueError("focus exceeds 4,096 UTF-8 bytes")
        return value


class IfcOfficialDocsReadResult(IfcOfficialDocsModel):
    title: str = Field(min_length=1, max_length=512)
    url: str = Field(min_length=1, max_length=_MAX_URL_CHARS)
    content: str
    truncated: bool

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return canonical_official_ifc_doc_url(value)


def canonical_official_ifc_doc_url(value: str) -> str:
    """Validate and canonicalize one readable page on the maintained IFC site."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("url is not a valid official IFC documentation URL") from exc
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in {OFFICIAL_IFC_DOCS_HOST, _LEGACY_HOST}
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("url must be an HTTPS page on the official IFC documentation host")

    path = parsed.path
    if (parsed.hostname or "").lower() == _LEGACY_HOST:
        if not path.startswith(_LEGACY_PREFIX):
            raise ValueError("url path is outside the legacy official IFC documentation pages")
        path = OFFICIAL_IFC_DOCS_PREFIX + path.removeprefix(_LEGACY_PREFIX)
        if path.endswith(".htm"):
            path += "l"
    prefix = next((prefix for prefix in (OFFICIAL_IFC_DOCS_PREFIX, OFFICIAL_IFC_DOCS_RELEASE_PREFIX)
                   if path.startswith(prefix)), None)
    if prefix is None or unquote(path) != path:
        raise ValueError("url path is outside the official IFC documentation pages")
    suffix = path.removeprefix(prefix)
    segments = suffix.rstrip("/").split("/")
    if not suffix or any(
        not segment
        or segment in {".", ".."}
        or any(not (char.isascii() and (char.isalnum() or char in "._~-")) for char in segment)
        for segment in segments
    ):
        raise ValueError("url path is outside the official IFC documentation pages")
    if segments[0] == "assets" or not suffix.endswith(".html"):
        raise ValueError("url must select an IFC documentation page, not a site asset")
    return urlunsplit(("https", OFFICIAL_IFC_DOCS_HOST, path, "", ""))


__all__ = [
    "IfcOfficialDocsErrorCode",
    "IfcOfficialDocsReadQuery",
    "IfcOfficialDocsReadResult",
    "IfcOfficialDocsSearchCard",
    "IfcOfficialDocsSearchQuery",
    "IfcOfficialDocsSearchResult",
    "OFFICIAL_IFC_DOCS_HOST",
    "OFFICIAL_IFC_DOCS_PREFIX",
    "OFFICIAL_IFC_DOCS_RELEASE_PREFIX",
    "OFFICIAL_IFC_DOCS_TIMEOUT_SECONDS",
    "canonical_official_ifc_doc_url",
]
