"""Safe HTTP and HTML adapter for the maintained buildingSMART IFC 4.3 docs."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag

from CAD2BIM_backend.domain.ifc_official_docs import (
    OFFICIAL_IFC_DOCS_HOST,
    OFFICIAL_IFC_DOCS_PREFIX,
    canonical_official_ifc_doc_url,
)
from CAD2BIM_backend.ports.ifc_official_docs import (
    OfficialIfcDocsClientError,
    OfficialIfcDocsSearchCard,
    OfficialIfcDocument,
)


_BASE_URL = f"https://{OFFICIAL_IFC_DOCS_HOST}{OFFICIAL_IFC_DOCS_PREFIX}"
_PUBLISHER_ROOT = "https://raw.githubusercontent.com/buildingSMART/Standards-builds/main"
_SEARCH_URL = f"{_PUBLISHER_ROOT}{OFFICIAL_IFC_DOCS_PREFIX}assets/search/search-index.json"
_MAX_SEARCH_BYTES = 32 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_MAX_REDIRECTS = 3
_BLOCK_NAMES = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "pre"})
_HEADING_NAMES = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_WHITESPACE = re.compile(r"\s+")
_SEARCH_TERM = re.compile(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}")
_INDEX_CACHE_SECONDS = 300.0
_MAX_INDEX_RECORDS = 20_000


@dataclass(frozen=True, slots=True)
class BuildingSmartIfcDocsPolicy:
    request_timeout_seconds: float = 30.0
    search_download_bytes: int = _MAX_SEARCH_BYTES
    document_download_bytes: int = _MAX_DOCUMENT_BYTES
    max_redirects: int = _MAX_REDIRECTS

    def __post_init__(self) -> None:
        if (
            self.request_timeout_seconds <= 0
            or self.search_download_bytes < 1
            or self.document_download_bytes < 1
            or self.max_redirects < 0
        ):
            raise ValueError("IFC documentation HTTP policy values must be positive")
        if (
            self.request_timeout_seconds > 30.0
            or self.search_download_bytes > _MAX_SEARCH_BYTES
            or self.document_download_bytes > _MAX_DOCUMENT_BYTES
            or self.max_redirects > _MAX_REDIRECTS
        ):
            raise ValueError("IFC documentation HTTP policy cannot weaken the hard limits")


class BuildingSmartIfcDocsClient:
    """Read publisher-owned IFC assets; never build or persist a local search index."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        policy: BuildingSmartIfcDocsPolicy | None = None,
        *,
        proxy: str | None = None,
    ) -> None:
        if client is not None and proxy is not None:
            raise ValueError("Configure the proxy on the supplied HTTP client, or let this adapter create it")
        self._policy = policy or BuildingSmartIfcDocsPolicy()
        self._search_index: list[dict[str, str]] | None = None
        self._search_index_expires = 0.0
        self._index_lock = asyncio.Lock()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            trust_env=False,
            proxy=proxy,
            follow_redirects=False,
            timeout=self._policy.request_timeout_seconds,
            headers={"User-Agent": "CAD2BIM/0.1 official-IFC-docs-reader"},
        )

    async def aclose(self) -> None:
        self._search_index = None
        if self._owns_client:
            await self._client.aclose()

    async def search(
        self,
        query: str,
        max_results: int,
    ) -> tuple[OfficialIfcDocsSearchCard, ...]:
        async with self._index_lock:
            if self._search_index is None or time.monotonic() >= self._search_index_expires:
                # Never serve a stale index after a failed refresh.
                self._search_index = None
                payload, _ = await self._get_asset(
                    httpx.URL(_SEARCH_URL),
                    maximum=self._policy.search_download_bytes,
                    search_endpoint=True,
                )
                self._search_index = _parse_search_index(payload)
                self._search_index_expires = time.monotonic() + _INDEX_CACHE_SECONDS
            records = self._search_index
        return _search_cards(records, query, max_results)

    async def read(self, url: str) -> OfficialIfcDocument:
        canonical = canonical_official_ifc_doc_url(url)
        html, final_url = await self._get_asset(
            httpx.URL(_PUBLISHER_ROOT + urlsplit(canonical).path),
            maximum=self._policy.document_download_bytes,
            search_endpoint=False,
        )
        final_canonical = f"https://{OFFICIAL_IFC_DOCS_HOST}" + final_url.removeprefix(_PUBLISHER_ROOT)
        return _parse_document_html(html, final_canonical)

    async def _get_asset(
        self,
        url: httpx.URL,
        *,
        maximum: int,
        search_endpoint: bool,
    ) -> tuple[bytes, str]:
        current = url
        for redirect_count in range(self._policy.max_redirects + 1):
            _validate_http_url(current, search_endpoint=search_endpoint)
            try:
                async with self._client.stream(
                    "GET",
                    current,
                    headers={"Accept": "application/json, text/plain" if search_endpoint else "text/html, text/plain"},
                    timeout=self._policy.request_timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if location is None:
                            raise _invalid("The official IFC documentation returned a redirect without a location")
                        if redirect_count >= self._policy.max_redirects:
                            raise _invalid("The official IFC documentation exceeded the redirect limit")
                        current = httpx.URL(urljoin(str(current), location))
                        continue
                    if response.status_code == 429 or response.status_code >= 500:
                        raise _unavailable(
                            "The official IFC documentation is temporarily unavailable"
                        )
                    if response.status_code < 200 or response.status_code >= 300:
                        raise _invalid(
                            f"The official IFC documentation returned HTTP {response.status_code}"
                        )
                    content_type = response.headers.get("content-type", "")
                    allowed_types = {"application/json", "text/plain"} if search_endpoint else {"text/html", "text/plain"}
                    if content_type.split(";", 1)[0].strip().lower() not in allowed_types:
                        raise _invalid("The official IFC publication returned an unexpected content type")
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            if int(content_length) > maximum:
                                raise _invalid(
                                    "The official IFC documentation response exceeded its download limit"
                                )
                        except ValueError as exc:
                            raise _invalid(
                                "The official IFC documentation returned an invalid Content-Length"
                            ) from exc
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > maximum:
                            raise _invalid(
                                "The official IFC documentation response exceeded its download limit"
                            )
            except OfficialIfcDocsClientError:
                raise
            except httpx.HTTPError as exc:
                raise _unavailable(
                    "The official IFC documentation could not be reached"
                ) from exc
            return bytes(payload), str(current)
        raise _invalid("The official IFC documentation exceeded the redirect limit")


def _validate_http_url(url: httpx.URL, *, search_endpoint: bool) -> None:
    try:
        parsed = urlsplit(str(url))
        port = parsed.port
    except ValueError as exc:
        raise _invalid("The official IFC documentation returned an invalid redirect URL") from exc
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "raw.githubusercontent.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
    ):
        raise _invalid("The official IFC documentation redirected outside its HTTPS allowlist")
    if search_endpoint:
        if str(url) != _SEARCH_URL:
            raise _invalid("The official IFC search redirected outside its allowlisted path")
        return
    try:
        publisher_path = "/buildingSMART/Standards-builds/main"
        if not parsed.path.startswith(publisher_path + "/"):
            raise ValueError("unapproved publication repository")
        canonical_official_ifc_doc_url(
            f"https://{OFFICIAL_IFC_DOCS_HOST}" + parsed.path.removeprefix(publisher_path)
        )
    except ValueError as exc:
        raise _invalid("The official IFC page redirected outside its allowlisted path") from exc


def _parse_search_index(payload: bytes) -> list[dict[str, str]]:
    try:
        records = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _invalid("The official IFC search index is not valid JSON") from exc
    if not isinstance(records, list) or not 1 <= len(records) <= _MAX_INDEX_RECORDS:
        raise _invalid("The official IFC search index has an invalid record list")
    for record in records:
        if not isinstance(record, dict) or any(
            not isinstance(record.get(key), str)
            for key in ("path", "title", "headings", "summary", "text")
        ) or not 1 <= len(record["title"]) <= 512:
            raise _invalid("The official IFC search index has an invalid record")
    return records


def _search_cards(
    records: list[dict[str, str]], query: str, max_results: int,
) -> tuple[OfficialIfcDocsSearchCard, ...]:
    terms = tuple(dict.fromkeys(_SEARCH_TERM.findall(query.casefold())))
    if not terms:
        return ()
    ranked = []
    for index, record in enumerate(records):
        title = record["title"].casefold()
        title_terms = set(_SEARCH_TERM.findall(title))
        headings = record["headings"].casefold()
        body = record["text"].casefold()
        score = (
            int(query.casefold() in title),
            sum(term in title_terms for term in terms),
            sum(term in title for term in terms),
            sum(term in headings for term in terms),
            sum(term in body for term in terms),
        )
        if any(score):
            ranked.append((score, index, record))
    ranked.sort(key=lambda item: (tuple(-part for part in item[0]), item[1]))
    cards = []
    seen = set()
    for _, _, record in ranked:
        path = record["path"]
        if not path.startswith("/") or path.startswith("//"):
            continue
        if path == "/":
            path = "/index.html"
        try:
            url = canonical_official_ifc_doc_url(_BASE_URL + path[1:])
        except ValueError:
            continue
        if url in seen:
            continue
        seen.add(url)
        # An unmodified excerpt from the official publisher's summary.
        cards.append(OfficialIfcDocsSearchCard(title=record["title"], url=url, snippet=record["summary"]))
        if len(cards) >= max_results:
            break
    return tuple(cards)


def _parse_document_html(html: bytes, final_url: str) -> OfficialIfcDocument:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#main-content")
    if main is None:
        raise _invalid("The official IFC page is missing #main-content")
    for noise in main.select("#translations-aside, script, style, nav, button"):
        noise.decompose()
    heading = main.find("h1")
    title = _visible_text(heading) if isinstance(heading, Tag) else ""
    if not title:
        raise _invalid("The official IFC page is missing its title")

    blocks: list[str] = []
    heading_indexes: list[int] = []
    for element in main.find_all(_BLOCK_NAMES):
        if any(
            isinstance(parent, Tag) and parent.name in _BLOCK_NAMES
            for parent in element.parents
            if parent is not main
        ):
            continue
        text = _visible_text(element)
        if not text:
            continue
        if element.name in _HEADING_NAMES:
            heading_indexes.append(len(blocks))
        blocks.append(text)
    if not blocks:
        raise _invalid("The official IFC page contains no readable document text")
    return OfficialIfcDocument(
        title=title,
        url=canonical_official_ifc_doc_url(final_url),
        blocks=tuple(blocks),
        heading_indexes=tuple(heading_indexes),
    )


def _visible_text(element: Tag) -> str:
    return _WHITESPACE.sub(" ", element.get_text(" ", strip=True)).strip()


def _unavailable(message: str) -> OfficialIfcDocsClientError:
    return OfficialIfcDocsClientError("unavailable", message)


def _invalid(message: str) -> OfficialIfcDocsClientError:
    return OfficialIfcDocsClientError("invalid", message)


__all__ = ["BuildingSmartIfcDocsClient", "BuildingSmartIfcDocsPolicy"]
