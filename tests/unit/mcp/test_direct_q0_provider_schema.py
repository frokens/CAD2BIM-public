from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastmcp import Client
from jsonschema import Draft202012Validator

from CAD2BIM_backend.domain.data_query_python import (
    DataQueryPythonQuery,
    DataQueryPythonRequest,
)
from CAD2BIM_backend.domain.ifc_official_docs import (
    IfcOfficialDocsReadQuery,
    IfcOfficialDocsSearchQuery,
)
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.query.python_inputs import (
    QueryPythonQuery,
    to_domain_data_query_python_query,
)
from CAD2BIM_mcp.direct.server import create_direct_mcp


_Q0_TOOLS = (
    "data_query_python",
    "ifc_search_official_docs",
    "ifc_read_official_doc",
)
_FORBIDDEN_SCHEMA_KEYS = {"allOf", "if", "then", "else", "not"}
_VALVE_URL = (
    "https://ifc43-docs.standards.buildingsmart.org/"
    "IFC/RELEASE/IFC4x3/HTML/lexical/IfcValve.htm"
)


def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


def _python_query() -> QueryPythonQuery:
    return QueryPythonQuery.model_validate(
        {
            "inputs": [
                {
                    "input_name": "entities",
                    "package_alias": "data_001",
                    "payload_name": "entities",
                }
            ],
            "code": "def query(ctx):\n    return ctx.table('entities')\n",
        }
    )


async def test_data_query_python_schema_has_three_complete_closed_provider_branches() -> None:
    tools = {tool.name: tool for tool in await create_direct_mcp(Mock()).list_tools()}
    schema = tools["data_query_python"].parameters

    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object"
    assert len(schema["oneOf"]) == 3
    first_page, repair, continuation = schema["oneOf"]
    assert tuple(first_page["properties"]) == ("direct_session_alias", "query", "mode")
    assert tuple(first_page["required"]) == ("direct_session_alias", "query")
    assert tuple(repair["required"]) == (
        "direct_session_alias", "mode", "parent_attempt_alias", "edits"
    )
    assert tuple(continuation["properties"]) == ("direct_session_alias", "cursor", "mode")
    assert tuple(continuation["required"]) == ("direct_session_alias", "cursor")
    assert first_page["additionalProperties"] is False
    assert continuation["additionalProperties"] is False

    query = first_page["properties"]["query"]
    assert tuple(query["properties"]) == (
        "inputs",
        "inline_tables",
        "parameters",
        "code",
        "page_size",
    )
    assert tuple(query["required"]) == ("inputs", "code")
    assert query["additionalProperties"] is False
    assert all(field["description"] for field in query["properties"].values())

    for node in _walk(schema):
        if isinstance(node, Mapping):
            assert not (_FORBIDDEN_SCHEMA_KEYS & set(node))
            assert "title" not in node
    serialized = json.dumps(schema, sort_keys=True).lower()
    assert "unknown" not in serialized
    assert '"additionalproperties": {}' not in serialized

    first_arguments = {
        "direct_session_alias": "direct_001",
        "mode": "fresh",
        "query": _python_query().model_dump(mode="json"),
    }
    continuation_arguments = {
        "direct_session_alias": "direct_001",
        "mode": "cursor",
        "cursor": "cur.next",
    }
    validator = Draft202012Validator(schema)
    assert validator.is_valid(first_arguments)
    assert validator.is_valid(continuation_arguments)
    assert not validator.is_valid({**first_arguments, "cursor": "cur.next"})
    assert not validator.is_valid({"direct_session_alias": "direct_001"})
    assert not validator.is_valid({**continuation_arguments, "unknown": True})


async def test_ifc_official_search_and_read_schemas_are_small_closed_objects() -> None:
    tools = {tool.name: tool for tool in await create_direct_mcp(Mock()).list_tools()}
    search = tools["ifc_search_official_docs"]
    read = tools["ifc_read_official_doc"]

    assert tuple(search.parameters["properties"]) == ("query", "max_results")
    assert tuple(search.parameters["required"]) == ("query",)
    assert search.parameters["additionalProperties"] is False
    assert search.parameters["properties"]["max_results"] == {
        "default": 3,
        "description": "Official result cards requested; default 3 and maximum 5.",
        "maximum": 5,
        "minimum": 1,
        "type": "integer",
    }
    assert tuple(search.output_schema["properties"]) == ("query", "results")
    assert search.output_schema["additionalProperties"] is False
    assert search.output_schema["properties"]["results"]["maxItems"] == 5

    assert tuple(read.parameters["properties"]) == ("url", "focus")
    assert tuple(read.parameters["required"]) == ("url",)
    assert read.parameters["additionalProperties"] is False
    assert [variant["type"] for variant in read.parameters["properties"]["focus"]["anyOf"]] == [
        "string",
        "null",
    ]
    assert tuple(read.output_schema["properties"]) == (
        "title",
        "url",
        "content",
        "truncated",
    )
    assert read.output_schema["additionalProperties"] is False

    for tool in (search, read):
        Draft202012Validator.check_schema(tool.parameters)
        assert not Draft202012Validator(tool.parameters).is_valid(
            {**{next(iter(tool.parameters["required"])): "value"}, "unknown": True}
        )


async def test_q0_provider_dispatches_python_search_and_read_as_domain_inputs() -> None:
    application = SimpleNamespace(
        query_python=AsyncMock(return_value=object()),
        search_ifc_official_docs=AsyncMock(return_value=object()),
        read_ifc_official_doc=AsyncMock(return_value=object()),
    )
    tools = {
        tool.name: tool for tool in await create_direct_mcp(application).list_tools()
    }

    provider_query = _python_query()
    await tools["data_query_python"].fn(
        direct_session_alias="direct_001",
        ctx=SimpleNamespace(session_id="test", request_id="1"),
        mode="fresh",
        query=provider_query,
        cursor=None,
    )
    session_alias, domain_query, cursor = application.query_python.await_args.args
    assert session_alias == "direct_001"
    assert isinstance(domain_query, DataQueryPythonQuery)
    assert domain_query == to_domain_data_query_python_query(provider_query)
    assert cursor is None

    await tools["data_query_python"].fn(
        direct_session_alias="direct_001",
        ctx=SimpleNamespace(session_id="test", request_id="2"),
        mode="cursor",
        query=None,
        cursor="cur.next",
    )
    assert application.query_python.await_args.args == ("direct_001", None, "cur.next")

    await tools["ifc_search_official_docs"].fn(query="IfcValve", max_results=4)
    (search_query,) = application.search_ifc_official_docs.await_args.args
    assert search_query == IfcOfficialDocsSearchQuery(query="IfcValve", max_results=4)

    await tools["ifc_read_official_doc"].fn(url=_VALVE_URL, focus="operation status")
    (read_query,) = application.read_ifc_official_doc.await_args.args
    assert read_query == IfcOfficialDocsReadQuery(
        url=_VALVE_URL,
        focus="operation status",
    )


async def test_q0_facade_dispatches_to_entry_neutral_services() -> None:
    expected_python = object()
    expected_search = object()
    expected_read = object()
    python_service = SimpleNamespace(query=AsyncMock(return_value=expected_python))
    docs_service = SimpleNamespace(
        search_official_docs=AsyncMock(return_value=expected_search),
        read_official_doc=AsyncMock(return_value=expected_read),
    )
    identity = object()
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(identity=identity)))
    bound: list[object] = []

    class Binder:
        def __enter__(self):
            bound.append(identity)

        def __exit__(self, *_args):
            return None

    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        resolver,
        lambda actual: Binder() if actual is identity else nullcontext(),
        query_python_service=python_service,
        ifc_official_docs_service=docs_service,
    )
    domain_query = to_domain_data_query_python_query(_python_query())
    search_query = IfcOfficialDocsSearchQuery(query="IfcValve", max_results=4)
    read_query = IfcOfficialDocsReadQuery(url=_VALVE_URL, focus="operation status")

    assert await facade.query_python("direct_001", domain_query, None) is expected_python
    assert await facade.search_ifc_official_docs(search_query) is expected_search
    assert await facade.read_ifc_official_doc(read_query) is expected_read

    request = python_service.query.await_args.args[1]
    assert isinstance(request, DataQueryPythonRequest)
    assert request.query == domain_query
    assert request.cursor is None
    assert python_service.query.await_args.kwargs["deadline"] > 0
    assert resolver.resolve.await_args.args == ("direct_001",)
    assert resolver.resolve.await_args.kwargs["command_id"].startswith(
        "command_direct_data_query_python_"
    )
    assert bound == [identity]
    docs_service.search_official_docs.assert_awaited_once_with(
        search_query,
        deadline=docs_service.search_official_docs.await_args.kwargs["deadline"],
    )
    docs_service.read_official_doc.assert_awaited_once_with(
        read_query,
        deadline=docs_service.read_official_doc.await_args.kwargs["deadline"],
    )


async def test_q0_descriptions_state_usage_limits_and_decision_boundaries() -> None:
    tools = {tool.name: tool for tool in await create_direct_mcp(Mock()).list_tools()}
    required_fragments = {
        "data_query_python": (
            "data_package_list",
            "data_describe_package",
            "mode=fresh",
            "Minimal complete request",
            'def query(ctx):\\n    return ctx.table(\\"records\\")',
            "mode=cursor",
            "Python is not rerun",
            "read-only context",
            "has no write methods",
            "pyarrow.Table, RecordBatch, or RecordBatchReader",
            "paged JSON result, not that Arrow object",
            "builtins are allowlisted",
            "getattr",
            "hasattr",
            "vectorized",
            "Do not reuse imported aliases pa, np, pd, pc, pl, or nx",
            "Shapely predicates and indexes are 2D",
            "4,000,000 rows",
            "65,536 UTF-8 bytes",
            "1..512 columns",
            "128 MiB",
            "180 CPU seconds",
            "4 GiB RSS/scratch",
            "900 seconds",
            "cursor reads have 15 seconds",
            "QUERY_PYTHON_INVALID",
            "QUERY_RESULT_VALUE_TOO_LARGE",
            "QUERY_CURSOR_EXPIRED",
            "pip install, network or URL access",
            "data_query_python_attempt",
            "cad2bim://contracts/data_query_python/program_api",
            "MCP result navigation",
            "Use data_derive",
            "computed facts, not approved CAD classifications or IFC decisions",
        ),
        "ifc_search_official_docs": (
            "buildingSMART IFC 4.3.2",
            "natural-language question or exact Ifc concept",
            "default 3 and maximum 5",
            "does not fetch full result pages, use RAG, summarize with a model",
            "no cursor, pagination, Direct session, Package, or Operation",
            "hard timeout is 45 seconds",
            "response by 32 KiB",
            "IFC_DOCS_UNAVAILABLE",
            "IFC_DOCS_RESPONSE_INVALID",
            "call ifc_read_official_doc",
        ),
        "ifc_read_official_doc": (
            "selected from ifc_search_official_docs",
            "not an arbitrary web reader",
            "preserving their document order",
            "never an LLM summary",
            "no cursor or pagination",
            "hard timeout is 45 seconds",
            "response by 32 KiB",
            "IFC_DOCS_UNAVAILABLE",
            "IFC_DOCS_RESPONSE_INVALID",
            "not a project-specific IFC modeling decision",
        ),
    }
    for tool_name, fragments in required_fragments.items():
        for fragment in fragments:
            assert fragment in tools[tool_name].description, (tool_name, fragment)


async def test_data_query_python_program_resource_navigates_registered_output_without_schema_copy() -> None:
    server = create_direct_mcp(Mock())
    tool = await server.get_tool("data_query_python")

    async with Client(server) as client:
        resources = {str(item.uri): item for item in await client.list_resources()}
        uri = "cad2bim://contracts/data_query_python/program_api"
        assert "mcp_result_navigation" in resources[uri].description
        contents = await client.read_resource(uri)

    assert len(contents) == 1
    document = json.loads(contents[0].text)
    assert tuple(document) == (
        "usage",
        "context_api",
        "program_return",
        "mcp_result_navigation",
        "budgets_and_failures",
        "repair",
        "examples",
    )
    assert "tool_result_schema" not in document
    navigation = document["mcp_result_navigation"]
    assert navigation["registered_tool"] == "data_query_python"
    assert navigation["top_level_fields"] == list(tool.output_schema["properties"])
    assert navigation["top_level_fields"] == [
        "row_schema",
        "rows",
        "page",
        "query_statistics",
    ]
    assert "list_tools" in navigation["discovery"]
    assert "page.next_cursor" in navigation["discovery"]
    assert "not the in-Worker return ABI" in navigation["boundary"]
    assert "not the MCP JSON result Schema" in document["program_return"]["boundary"]


async def test_fastmcp_client_exposes_the_exact_q0_schemas_and_descriptions() -> None:
    server = create_direct_mcp(Mock())
    registered = {tool.name: tool for tool in await server.list_tools()}

    async with Client(server) as client:
        provider = {tool.name: tool for tool in await client.list_tools()}

    for name in _Q0_TOOLS:
        assert provider[name].inputSchema == registered[name].parameters
        assert provider[name].outputSchema == registered[name].output_schema
        assert provider[name].description == registered[name].description
