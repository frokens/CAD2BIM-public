from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape

import httpx
import pytest

from CAD2BIM_backend.application.ifc_official_docs_service import IfcOfficialDocsService
from CAD2BIM_backend.domain.ifc_official_docs import (
    IfcOfficialDocsReadQuery,
    IfcOfficialDocsSearchQuery,
)
from CAD2BIM_backend.infrastructure.ifc_official_docs import BuildingSmartIfcDocsClient


pytestmark = pytest.mark.integration

_DOC_ROOT = "https://standards.buildingsmart.org/IFC/DEV/IFC4_3/HTML"
_PUBLISHER_PATH = "/buildingSMART/Standards-builds/main"
_INDEX_PATH = _PUBLISHER_PATH + "/IFC/DEV/IFC4_3/HTML/assets/search/search-index.json"
_BODY_SENTINEL = "OFFICIAL_BODY_ONLY_AFTER_READ"


@dataclass(frozen=True, slots=True)
class _ConceptRound:
    query: str
    page: str
    title: str
    focus: str
    expected_text: str

    @property
    def url(self) -> str:
        return f"{_DOC_ROOT}/lexical/{self.page}.html"


_ROUNDS = (
    _ConceptRound(
        query="IfcValve gas flow controller",
        page="IfcValve",
        title="7.5.3.63 IfcValve",
        focus="gas distribution flow",
        expected_text="controls or modulates gas distribution flow",
    ),
    _ConceptRound(
        query="Pset_ValveTypeCommon Status lifecycle",
        page="Pset_ValveTypeCommon",
        title="Pset_ValveTypeCommon",
        focus="Status lifecycle",
        expected_text="Status describes the valve lifecycle condition",
    ),
    _ConceptRound(
        query="Pset_ValveTypeCommon IsNormallyOpen",
        page="Pset_ValveTypeCommon",
        title="Pset_ValveTypeCommon",
        focus="IsNormallyOpen",
        expected_text="IsNormallyOpen states the valve's normal operating position",
    ),
    _ConceptRound(
        query="IfcRelNests valve ports",
        page="IfcRelNests",
        title="5.1.3.26 IfcRelNests",
        focus="nested ports",
        expected_text="IfcRelNests assigns the distribution ports as nested parts of the valve",
    ),
    _ConceptRound(
        query="IfcRelConnectsPorts gas valve connection",
        page="IfcRelConnectsPorts",
        title="5.1.3.14 IfcRelConnectsPorts",
        focus="connection between ports",
        expected_text="IfcRelConnectsPorts represents the connection between two ports",
    ),
    _ConceptRound(
        query="IfcDistributionPort FlowDirection gas",
        page="IfcDistributionPort",
        title="5.4.3.14 IfcDistributionPort",
        focus="FlowDirection gas",
        expected_text="IfcDistributionPort carries FlowDirection for the gas connection point",
    ),
)


def _search_index() -> bytes:
    return json.dumps([
        {"path": f"/lexical/{round_.page}.html", "title": round_.page,
         "headings": round_.focus, "summary": f"Official card for {round_.page}.",
         "text": round_.query}
        for round_ in _ROUNDS
    ]).encode()


def _document_html(rounds: tuple[_ConceptRound, ...]) -> bytes:
    sections = "\n".join(
        f"<h2>{escape(round_.focus)}</h2><p>{_BODY_SENTINEL}: {escape(round_.expected_text)}.</p>"
        for round_ in rounds
    )
    return f"""<!doctype html>
<html><body>
  <nav>This navigation is not document content.</nav>
  <div id="main-content">
    <h1>{escape(rounds[0].title)}</h1>
    {sections}
  </div>
</body></html>""".encode()


@pytest.mark.asyncio
async def test_gas_valve_concepts_require_search_card_selection_then_page_read() -> None:
    """Exercise the deterministic tool chain; this is not an LLM knowledge test."""

    rounds_by_path = {
        path: tuple(round_ for round_ in _ROUNDS if httpx.URL(round_.url).path == path)
        for path in {httpx.URL(round_.url).path for round_ in _ROUNDS}
    }
    requests: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query")
        requests.append((request.url.path, query))
        if request.url.path == _INDEX_PATH:
            body = _search_index()
        else:
            body = _document_html(rounds_by_path[request.url.path.removeprefix(_PUBLISHER_PATH)])
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            content=body,
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as http_client:
        service = IfcOfficialDocsService(BuildingSmartIfcDocsClient(http_client))

        for round_ in _ROUNDS:
            calls_before_search = len(requests)
            search_result = await service.search(
                IfcOfficialDocsSearchQuery(query=round_.query, max_results=5)
            )

            assert requests[calls_before_search:] == ([( _INDEX_PATH, None)] if calls_before_search == 0 else [])
            assert _BODY_SENTINEL not in search_result.model_dump_json()

            selected = next(card for card in search_result.results if card.url == round_.url)
            read_result = await service.read(
                IfcOfficialDocsReadQuery(url=selected.url, focus=round_.focus)
            )

            assert requests[-1] == (_PUBLISHER_PATH + httpx.URL(selected.url).path, None)
            assert read_result.url == selected.url
            assert read_result.title == round_.title
            assert _BODY_SENTINEL in read_result.content
            assert round_.expected_text in read_result.content

    assert len(requests) == 1 + len(_ROUNDS)
    assert requests[0] == (_INDEX_PATH, None)
    assert [path for path, _ in requests[1:]] == [
        _PUBLISHER_PATH + httpx.URL(round_.url).path for round_ in _ROUNDS
    ]
