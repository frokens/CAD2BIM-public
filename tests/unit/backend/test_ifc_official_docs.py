from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from CAD2BIM_backend.application.ifc_official_docs_service import (
    IfcOfficialDocsApplicationError,
    IfcOfficialDocsPolicy,
    IfcOfficialDocsService,
)
from CAD2BIM_backend.domain.ifc_official_docs import (
    IfcOfficialDocsReadQuery,
    IfcOfficialDocsSearchQuery,
)
from CAD2BIM_backend.infrastructure.ifc_official_docs import (
    BuildingSmartIfcDocsClient,
    BuildingSmartIfcDocsPolicy,
)


_FIXTURES = Path(__file__).parents[2] / "fixtures" / "ifc_official_docs"
_VALVE_URL = (
    "https://standards.buildingsmart.org/IFC/DEV/IFC4_3/HTML/lexical/IfcValve.html"
)
_PUBLISHER_ROOT = "https://raw.githubusercontent.com/buildingSMART/Standards-builds/main"
_INDEX_PATH = "/buildingSMART/Standards-builds/main/IFC/DEV/IFC4_3/HTML/assets/search/search-index.json"
_VALVE_SOURCE = _PUBLISHER_ROOT + httpx.URL(_VALVE_URL).path


def _fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _html_response(request: httpx.Request, fixture: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=_fixture(fixture),
        request=request,
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://ifc43-docs.standards.buildingsmart.org/IFC/RELEASE/IFC4x3/HTML/lexical/IfcValve.htm",
        "https://example.com/IFC/RELEASE/IFC4x3/HTML/lexical/IfcValve.htm",
        "https://ifc43-docs.standards.buildingsmart.org/search?query=IfcValve",
        "https://ifc43-docs.standards.buildingsmart.org/IFC/RELEASE/IFC4x3/HTML/../secret",
        "https://ifc43-docs.standards.buildingsmart.org/IFC/RELEASE/IFC4x3/HTML/assets/app.js",
        "https://ifc43-docs.standards.buildingsmart.org:443/IFC/RELEASE/IFC4x3/HTML/lexical/IfcValve.htm",
    ],
)
def test_read_query_rejects_urls_outside_exact_official_page_allowlist(url: str) -> None:
    with pytest.raises(ValidationError):
        IfcOfficialDocsReadQuery(url=url)


def test_queries_strip_text_and_reject_blank_focus() -> None:
    assert IfcOfficialDocsSearchQuery(query="  IfcValve  ").query == "IfcValve"
    assert IfcOfficialDocsReadQuery(url=_VALVE_URL, focus="  operation status ").focus == (
        "operation status"
    )
    with pytest.raises(ValidationError):
        IfcOfficialDocsReadQuery(url=_VALVE_URL, focus="   ")


@pytest.mark.asyncio
async def test_search_returns_cards_only_without_fetching_result_pages() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == _INDEX_PATH
        assert not request.url.query
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=_fixture("search_index.json"))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as http_client:
        service = IfcOfficialDocsService(BuildingSmartIfcDocsClient(http_client))
        result = await service.search_official_docs(
            IfcOfficialDocsSearchQuery(query="IfcValve", max_results=5)
        )

    assert len(requests) == 1
    assert [card.title for card in result.results] == ["IfcValve", "IfcValveType"]
    assert result.results[0].model_dump() == {
        "title": "IfcValve",
        "url": _VALVE_URL,
        "snippet": "IfcValve A valve controls or modulates fluid flow.",
    }
    assert all("example.com" not in card.url for card in result.results)


@pytest.mark.asyncio
async def test_search_no_matches_is_a_successful_empty_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=_fixture("search_index.json"))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as http_client:
        result = await IfcOfficialDocsService(
            BuildingSmartIfcDocsClient(http_client)
        ).search(IfcOfficialDocsSearchQuery(query="no_such_concept"))

    assert result.query == "no_such_concept"
    assert result.results == ()


@pytest.mark.asyncio
async def test_old_search_html_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<div id='main-content'><h1>Search</h1></div>",
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as http_client:
        service = IfcOfficialDocsService(BuildingSmartIfcDocsClient(http_client))
        with pytest.raises(IfcOfficialDocsApplicationError) as caught:
            await service.search(IfcOfficialDocsSearchQuery(query="IfcValve"))

    assert caught.value.code == "IFC_DOCS_RESPONSE_INVALID"
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_read_fetches_only_selected_url_and_focus_selects_relevant_original_blocks() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == _VALVE_SOURCE
        return _html_response(request, "ifc_valve.html")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as http_client:
        result = await IfcOfficialDocsService(
            BuildingSmartIfcDocsClient(http_client)
        ).read_official_doc(IfcOfficialDocsReadQuery(url=_VALVE_URL, focus="operation status"))

    assert len(requests) == 1
    assert result.title == "7.5.3.63 IfcValve"
    assert result.url == _VALVE_URL
    assert "7.5.3.63.5 Property sets" in result.content
    assert "Status, ValveOperation" in result.content
    assert "Semantic definition" not in result.content
    assert "Navigation text" not in result.content
    assert "this text must not enter" not in result.content
    assert result.truncated is True


@pytest.mark.asyncio
async def test_read_without_focus_preserves_document_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _html_response(request, "ifc_valve.html")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as http_client:
        result = await IfcOfficialDocsService(
            BuildingSmartIfcDocsClient(http_client)
        ).read(IfcOfficialDocsReadQuery(url=_VALVE_URL))

    assert result.content.index("Semantic definition") < result.content.index("Attributes")
    assert result.content.index("Attributes") < result.content.index("Property sets")
    assert result.truncated is False


@pytest.mark.asyncio
async def test_read_marks_block_boundary_budget_truncation_and_stays_within_response_budget() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _html_response(request, "ifc_valve.html")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as http_client:
        result = await IfcOfficialDocsService(
            BuildingSmartIfcDocsClient(http_client),
            IfcOfficialDocsPolicy(response_bytes=420),
        ).read(IfcOfficialDocsReadQuery(url=_VALVE_URL))

    assert result.truncated is True
    assert len(result.model_dump_json().encode("utf-8")) <= 420
    assert result.content.endswith("IfcFlowController")
    assert "7.5.3.63.3 Attributes" not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_temporary_upstream_status_is_retryable_unavailable(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"content-type": "text/html"},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as http_client:
        service = IfcOfficialDocsService(BuildingSmartIfcDocsClient(http_client))
        with pytest.raises(IfcOfficialDocsApplicationError) as caught:
            await service.search(IfcOfficialDocsSearchQuery(query="IfcValve"))

    assert caught.value.code == "IFC_DOCS_UNAVAILABLE"
    assert caught.value.retryable is True
    assert "No official IFC documentation was obtained" in str(caught.value)


@pytest.mark.asyncio
async def test_network_timeout_is_retryable_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as http_client:
        service = IfcOfficialDocsService(BuildingSmartIfcDocsClient(http_client))
        with pytest.raises(IfcOfficialDocsApplicationError) as caught:
            await service.search(IfcOfficialDocsSearchQuery(query="IfcValve"))

    assert caught.value.code == "IFC_DOCS_UNAVAILABLE"
    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_non_official_redirect_is_non_retryable_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://example.com/copied-ifc-docs"},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as http_client:
        service = IfcOfficialDocsService(BuildingSmartIfcDocsClient(http_client))
        with pytest.raises(IfcOfficialDocsApplicationError) as caught:
            await service.read(IfcOfficialDocsReadQuery(url=_VALVE_URL))

    assert caught.value.code == "IFC_DOCS_RESPONSE_INVALID"
    assert caught.value.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"content-type": "application/json"}, b"{}"),
        ({"content-type": "text/html"}, b"<html><body>changed</body></html>"),
    ],
)
async def test_invalid_content_type_or_html_shape_fails_closed(
    headers: dict[str, str],
    body: bytes,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, content=body, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as http_client:
        service = IfcOfficialDocsService(BuildingSmartIfcDocsClient(http_client))
        with pytest.raises(IfcOfficialDocsApplicationError) as caught:
            await service.read(IfcOfficialDocsReadQuery(url=_VALVE_URL))

    assert caught.value.code == "IFC_DOCS_RESPONSE_INVALID"
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_download_budget_is_enforced_on_decoded_html() -> None:
    body = b"<div id='main-content'><h1>IfcValve</h1><p>" + b"x" * 500 + b"</p></div>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=body,
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as http_client:
        client = BuildingSmartIfcDocsClient(
            http_client,
            BuildingSmartIfcDocsPolicy(document_download_bytes=128),
        )
        with pytest.raises(IfcOfficialDocsApplicationError) as caught:
            await IfcOfficialDocsService(client).read(IfcOfficialDocsReadQuery(url=_VALVE_URL))

    assert caught.value.code == "IFC_DOCS_RESPONSE_INVALID"


def test_legacy_urls_follow_dev_migration_and_explicit_release_is_preserved() -> None:
    legacy = "https://ifc43-docs.standards.buildingsmart.org/IFC/RELEASE/IFC4x3/HTML/lexical/IfcValve.htm"
    assert IfcOfficialDocsReadQuery(url=legacy).url == _VALVE_URL
    release = _VALVE_URL.replace("/DEV/", "/RELEASE/")
    assert IfcOfficialDocsReadQuery(url=release).url == release


@pytest.mark.parametrize("url", [
    _VALVE_SOURCE,
    _VALVE_URL.replace("/DEV/", "/other/"),
    _VALVE_URL.replace("IfcValve.html", "%2e%2e/IfcValve.html"),
    _VALVE_URL + "?redirect=https://example.com",
    _VALVE_URL.replace("lexical/IfcValve.html", "assets/search/search-index.json"),
])
def test_new_page_allowlist_rejects_arbitrary_sources_and_encoded_paths(url: str) -> None:
    with pytest.raises(ValidationError):
        IfcOfficialDocsReadQuery(url=url)


@pytest.mark.asyncio
async def test_concurrent_searches_share_bounded_cache_and_expiry_refreshes() -> None:
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        await asyncio.sleep(0)
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=_fixture("search_index.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BuildingSmartIfcDocsClient(http)
        first, second = await asyncio.gather(client.search("IfcValve", 3), client.search("fluid flow", 3))
        assert first[0].url == second[0].url == _VALVE_URL
        assert len(calls) == 1
        client._search_index_expires = 0
        await client.search("IfcValve", 3)
        assert len(calls) == 2
        await client.aclose()
        assert client._search_index is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"{}", b"[]", b"not-json", b'[{}]', b'[{"title":null}]'])
async def test_invalid_index_structure_is_nonretryable(payload: bytes) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, content=payload)
    )) as http:
        with pytest.raises(IfcOfficialDocsApplicationError) as caught:
            await IfcOfficialDocsService(BuildingSmartIfcDocsClient(http)).search(IfcOfficialDocsSearchQuery(query="IfcValve"))
    assert caught.value.code == "IFC_DOCS_RESPONSE_INVALID"
    assert caught.value.phase == "search"
    assert not caught.value.retryable


@pytest.mark.asyncio
async def test_index_budget_and_failed_refresh_do_not_serve_stale_cards() -> None:
    responses = [200, 503]
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(responses.pop(0), headers={"content-type": "text/plain"}, content=_fixture("search_index.json"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BuildingSmartIfcDocsClient(http)
        await client.search("IfcValve", 3)
        client._search_index_expires = 0
        with pytest.raises(IfcOfficialDocsApplicationError) as caught:
            await IfcOfficialDocsService(client).search(IfcOfficialDocsSearchQuery(query="IfcValve"))
        assert caught.value.retryable
        assert client._search_index is None
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, content=_fixture("search_index.json"))
    )) as http:
        client = BuildingSmartIfcDocsClient(http, BuildingSmartIfcDocsPolicy(search_download_bytes=128))
        with pytest.raises(IfcOfficialDocsApplicationError) as caught:
            await IfcOfficialDocsService(client).search(IfcOfficialDocsSearchQuery(query="IfcValve"))
        assert caught.value.code == "IFC_DOCS_RESPONSE_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize("location", [
    "https://raw.githubusercontent.com/other/Standards-builds/main/IFC/DEV/IFC4_3/HTML/lexical/IfcValve.html",
    _VALVE_SOURCE.replace("/main/", "/unreviewed/"),
    _VALVE_SOURCE + "?token=ignored",
    "http://raw.githubusercontent.com/buildingSMART/Standards-builds/main/IFC/DEV/IFC4_3/HTML/lexical/IfcValve.html",
])
async def test_publisher_redirect_cannot_escape_repository_or_transport(location: str) -> None:
    requests = []
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": location})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(IfcOfficialDocsApplicationError) as caught:
            await IfcOfficialDocsService(BuildingSmartIfcDocsClient(http)).read(IfcOfficialDocsReadQuery(url=_VALVE_URL))
    assert len(requests) == 1
    assert caught.value.code == "IFC_DOCS_RESPONSE_INVALID"


@pytest.mark.asyncio
async def test_application_total_deadline_bounds_slow_index_request() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=[])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(IfcOfficialDocsApplicationError) as caught:
            await IfcOfficialDocsService(BuildingSmartIfcDocsClient(http), IfcOfficialDocsPolicy(timeout_seconds=0.001)).search(IfcOfficialDocsSearchQuery(query="IfcValve"))
    assert caught.value.code == "IFC_DOCS_UNAVAILABLE"
    assert caught.value.retryable


@pytest.mark.asyncio
async def test_release_source_and_new_site_translation_controls_preserve_page_provenance() -> None:
    release = _VALVE_URL.replace("/DEV/", "/RELEASE/")
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == _PUBLISHER_ROOT + httpx.URL(release).path
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"""
            <div id="main-content"><h1>IfcValve</h1>
            <aside id="translations-aside"><p>No translation yet</p></aside>
            <script>secretScript()</script><p>A valve controls flow.</p></div>
        """)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await IfcOfficialDocsService(BuildingSmartIfcDocsClient(http)).read(IfcOfficialDocsReadQuery(url=release))
    assert result.url == release
    assert result.content == "IfcValve\n\nA valve controls flow."
