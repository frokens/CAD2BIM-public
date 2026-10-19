from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from CAD2BIM_backend.application.ifc_query_service import IfcQueryApplicationError
from CAD2BIM_backend.domain.ifc_query import IfcQueryInputBinding, IfcQueryPage
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.query.application import IfcQueryApplicationMixin
from CAD2BIM_mcp.direct.ifc.query.contracts import (
    DirectIfcQueryCursorInput,
    DirectIfcQueryFreshInput,
    DirectIfcQueryRepairInput,
    IfcQueryPythonResult,
)
from CAD2BIM_mcp.direct.ifc.query.tools import IFC_QUERY_TOOL_REGISTRARS
from CAD2BIM_mcp.tool_docs import ifc_query as _ifc_query_tool_docs


_TOOL_NAMES = {"ifc_query_python"}
_FORBIDDEN_SCHEMA_KEYS = {"allOf", "if", "then", "else", "not"}
_INTERNAL_NAMES = {
    "revision",
    "revision_id",
    "revision_no",
    "model_id",
    "artifact_id",
    "artifact_version_id",
    "writer_id",
    "storage_path",
    "source_path",
    "result_artifact_id",
    "result_artifact_version_id",
    "internal_package_id",
}
_SHA = "a" * 64
_ATTEMPT = "cur.ifc_query_attempt_001"


class _IfcQueryFacadeProbe(IfcQueryApplicationMixin):
    def __init__(self, service: object) -> None:
        self._ifc_query_service = service
        self.scope = SimpleNamespace(identity=SimpleNamespace(), run_id="run_001")
        self.runtime_reads: list[tuple[str, str, dict[str, object]]] = []

    async def _run_runtime_read(self, alias, action, operation, **options):
        self.runtime_reads.append((alias, action, options))
        return await operation(self.scope)


def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


def _assert_safe_schema(schema: Mapping[str, object]) -> None:
    Draft202012Validator.check_schema(schema)
    for node in _walk(schema):
        if isinstance(node, Mapping):
            assert node != {}
            assert not (_FORBIDDEN_SCHEMA_KEYS & set(node))
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False
    serialized = json.dumps(schema, sort_keys=True).lower()
    assert "unknown" not in serialized
    for name in _INTERNAL_NAMES:
        assert f'"{name}"' not in serialized


def _results() -> SimpleNamespace:
    query = IfcQueryPythonResult(
        attempt_alias=_ATTEMPT,
        parent_attempt_alias=None,
        source_sha256=_SHA,
        status="succeeded",
        summary="Returned 1 of 1 bounded IFC-query results.",
        result_json='[{"project_name":"Demo"}]',
        next_cursor=None,
    )
    return SimpleNamespace(query=query)


def _application() -> SimpleNamespace:
    values = _results()
    return SimpleNamespace(
        query_ifc_python=AsyncMock(return_value=values.query),
    )


def _server(application: object) -> FastMCP:
    server = FastMCP(
        name="IFC Query Provider Test",
        strict_input_validation=True,
        mask_error_details=True,
    )
    for registrar in IFC_QUERY_TOOL_REGISTRARS:
        registrar(server, application)
    return server


async def test_i0b304_query_schemas_are_closed_discriminated_and_native_visible() -> None:
    server = _server(_application())
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert set(tools) == _TOOL_NAMES

    query = tools["ifc_query_python"]
    branches = query.parameters["anyOf"]
    assert [branch["properties"]["mode"]["const"] for branch in branches] == [
        "fresh",
        "repair",
        "cursor",
    ]
    assert all("mode" in branch["required"] for branch in branches)
    assert tuple(branches[0]["properties"]) == (
        "direct_session_alias",
        "mode",
        "inputs",
        "code",
        "page_size",
    )
    assert branches[0]["properties"]["inputs"]["maxItems"] == 16
    assert "minItems" not in branches[0]["properties"]["inputs"]
    code_description = branches[0]["properties"]["code"]["description"]
    for text in (
        "defining query(ctx)",
        "server-fixed CURRENT IFC",
        "exact Package Payload bindings",
        "JSON-compatible data",
        "without modifying IFC or publishing data",
        _ifc_query_tool_docs.IFC_QUERY_PROGRAM_RESOURCE_URI,
    ):
        assert text in code_description
    binding = branches[0]["properties"]["inputs"]["items"]
    assert tuple(binding["properties"]) == ("input_name", "package_alias", "payload_name")
    assert binding["additionalProperties"] is False
    assert set(binding["required"]) == {"input_name", "package_alias", "payload_name"}
    assert tuple(branches[1]["properties"]) == (
        "direct_session_alias",
        "mode",
        "parent_attempt_alias",
        "edits",
        "page_size",
    )
    assert branches[1]["properties"]["edits"]["maxItems"] == 16
    assert tuple(branches[2]["properties"]) == (
        "direct_session_alias",
        "mode",
        "cursor",
    )

    assert query.annotations.readOnlyHint is True
    assert query.annotations.destructiveHint is False
    assert query.annotations.idempotentHint is False

    for tool in tools.values():
        _assert_safe_schema(tool.parameters)
        assert all(
            "title" not in node
            for node in _walk(tool.parameters)
            if isinstance(node, Mapping)
        )
        assert tool.output_schema is not None
        _assert_safe_schema(tool.output_schema)
    assert tuple(query.output_schema["properties"]) == (
        "attempt_alias",
        "parent_attempt_alias",
        "source_sha256",
        "status",
        "summary",
        "result_json",
        "next_cursor",
    )
    assert query.output_schema["properties"]["status"]["const"] == "succeeded"
    assert "status" in query.output_schema["required"]

    descriptions = " ".join(tool.description for tool in tools.values())
    for text in (
        "server fixes CURRENT",
        "source before",
        "fresh",
        "repair",
        "cursor",
        "exact Package Payload",
        "does not rerun",
        "does not create an IFC revision",
        "does not advance CURRENT",
        "does not publish a Package",
        "private copy",
        "attempt_alias",
    ):
        assert text in descriptions

    assert _ifc_query_tool_docs.IFC_QUERY_PROGRAM_RESOURCE_URI in query.description
    for phrase in (
        "server fixes CURRENT",
        "zero to 16 explicitly named exact Package Payload bindings",
        "Only a top-level list is paged",
        "reuses the parent's exact IFC revision and exact Package Payloads",
        "does not create an IFC revision",
        "does not advance CURRENT",
        "does not publish a Package",
        "safe attempt_alias",
    ):
        assert phrase in query.description

    async with Client(server) as client:
        program_contents = await client.read_resource(
            _ifc_query_tool_docs.IFC_QUERY_PROGRAM_RESOURCE_URI
        )
        native = {tool.name: tool for tool in await client.list_tools()}
    program = json.loads(program_contents[0].text)
    assert set(program) == {
        "usage",
        "input_binding_schema",
        "return_contract",
        "examples",
    }
    binding_schema = program["input_binding_schema"]
    assert tuple(binding_schema["properties"]) == (
        "input_name",
        "package_alias",
        "payload_name",
    )
    assert set(binding_schema["required"]) == {
        "input_name",
        "package_alias",
        "payload_name",
    }
    assert binding_schema["additionalProperties"] is False
    for phrase in (
        "real process-private IfcOpenShell model",
        "by_type, by_id, by_guid, traverse, get_inverse",
        "ctx.table(name)",
        "ctx.batches(name)",
        "ctx.json(name)",
        "ctx.host_sections(product_global_id)",
        "sections[].body_section_ref",
        "collections, decimal, fractions, functools",
        "ifcopenshell, itertools, json, math, operator, pyarrow, re, and statistics",
        "Local dictionary and list index assignment or deletion is allowed",
        "low-level wrapper or transaction access",
        "private transaction mutation audit",
        "65,536-byte server result budget",
        "use Python list for arrays",
        "get_info(recursive=True)",
        "json_path, value_type, and reason",
        "HasAssociations does not enumerate associations of its assigned Type",
    ):
        assert phrase in program["usage"]
    assert "query(ctx) returns JSON-compatible data" in program["return_contract"]
    assert "IfcQueryPythonResult" in program["return_contract"]
    assert "JSON array" in program["return_contract"]
    assert "singleton array" in query.output_schema["properties"]["result_json"]["description"]
    for field in (
        "attempt_alias",
        "parent_attempt_alias",
        "source_sha256",
        "result_json",
        "next_cursor",
    ):
        assert field in program["return_contract"]
    assert "Only a top-level list is paged" in program["return_contract"]
    assert program["examples"] == {
        "ifc_only": json.loads(_ifc_query_tool_docs.IFC_QUERY_FRESH_EXAMPLE)
    }
    assert (
        program["examples"]["ifc_only"]["code"]
        == _ifc_query_tool_docs.IFC_QUERY_SOURCE_EXAMPLE
    )
    for name, tool in tools.items():
        assert native[name].inputSchema == tool.parameters
        assert native[name].outputSchema == tool.output_schema
        assert native[name].description == tool.description


async def test_i0b304_tools_dispatch_complete_provider_branches() -> None:
    application = _application()
    server = _server(application)
    code = "def query(ctx):\n    return [{'project_name': p.Name} for p in ctx.model().by_type('IfcProject')]\n"

    async with Client(server) as client:
        query = await client.call_tool(
            "ifc_query_python",
            {
                "direct_session_alias": "direct_001",
                "mode": "fresh",
                "inputs": [
                    {
                        "input_name": "facts",
                        "package_alias": "data_001",
                        "payload_name": "facts.parquet",
                    }
                ],
                "code": code,
            },
        )
    assert query.structured_content == _results().query.model_dump(mode="json")

    request = application.query_ifc_python.await_args.args[0]
    assert isinstance(request, DirectIfcQueryFreshInput)
    assert request.code == code
    assert request.inputs[0].input_name == "facts"
    request_identity = application.query_ifc_python.await_args.kwargs["request_identity"]
    assert isinstance(request_identity, str) and request_identity


async def test_i0b304_tools_dispatch_repair_and_cursor_branches() -> None:
    application = _application()
    server = _server(application)

    async with Client(server) as client:
        await client.call_tool(
            "ifc_query_python",
            {
                "direct_session_alias": "direct_001",
                "mode": "repair",
                "parent_attempt_alias": _ATTEMPT,

                "edits": [{"old_text": "return []", "new_text": "return [1]"}],
                "page_size": 25,
            },
        )
        await client.call_tool(
            "ifc_query_python",
            {
                "direct_session_alias": "direct_001",
                "mode": "cursor",
                "cursor": "cur.ifc_query_page_002",
            },
        )

    query_requests = [call.args[0] for call in application.query_ifc_python.await_args_list]
    assert isinstance(query_requests[0], DirectIfcQueryRepairInput)
    assert query_requests[0].edits[0].old_text == "return []"
    assert query_requests[0].page_size == 25
    assert isinstance(query_requests[1], DirectIfcQueryCursorInput)
    assert query_requests[1].cursor == "cur.ifc_query_page_002"


async def test_i0b304_direct_adapter_uses_shared_scope_and_projects_core_results() -> None:
    page = IfcQueryPage(
        attempt_alias=_ATTEMPT,
        parent_attempt_alias=None,
        source_sha256=_SHA,
        result_bytes=25,
        total_count=1,
        returned_count=1,
        results=({"project_name": "Demo"},),
        next_cursor=None,
    )
    service = SimpleNamespace(
        fresh=AsyncMock(return_value=page),
        repair=AsyncMock(return_value=page),
        continue_page=AsyncMock(return_value=page),
        resolve_attempt_alias=AsyncMock(return_value="ifcqa_internal"),
    )
    facade = _IfcQueryFacadeProbe(service)
    fresh = DirectIfcQueryFreshInput(
        direct_session_alias="direct_001",
        mode="fresh",
        inputs=(
            {
                "input_name": "facts",
                "package_alias": "data_001",
                "payload_name": "facts",
            },
        ),
        code="def query(ctx):\n    return []\n",
        page_size=50,
    )

    result = await facade.query_ifc_python(fresh, request_identity="request_001")
    continued = await facade.query_ifc_python(
        DirectIfcQueryCursorInput(
            direct_session_alias="direct_001",
            mode="cursor",
            cursor="cur.ifc_query_page_002",
        ),
        request_identity="request_cursor_is_not_executed",
    )
    assert result.status == "succeeded"
    assert result.result_json == '[{"project_name":"Demo"}]'
    assert continued == result
    fresh_call = service.fresh.await_args
    assert fresh_call.args[0] is facade.scope
    assert fresh_call.args[1] == fresh.code
    assert fresh_call.args[2] == "request_001"
    assert isinstance(fresh_call.args[3][0], IfcQueryInputBinding)
    assert fresh_call.args[3][0].package_alias == "data_001"
    assert fresh_call.args[4] == 50
    assert facade.runtime_reads[0] == (
        "direct_001",
        "ifc_query_python",
        {
            "timeout_seconds": 860.0,
            "timeout_code": "IFC_QUERY_TIMEOUT",
            "timeout_message": "The Direct IFC-query total deadline expired",
        },
    )
    service.continue_page.assert_awaited_once_with(
        facade.scope,
        "cur.ifc_query_page_002",
    )
    assert facade.runtime_reads[1][0:2] == (
        "direct_001",
        "ifc_query_python",
    )


async def test_i0b304_direct_adapter_repair_and_safe_failure_projection() -> None:
    failure = IfcQueryApplicationError(
        "IFC_QUERY_RUNTIME_ERROR",
        "query(ctx) failed",
        phase="execution",
        attempt_alias=_ATTEMPT,
        diagnostic={"line": 2, "source_line": "    return missing_name\n"},
    )
    service = SimpleNamespace(
        repair_from_alias=AsyncMock(side_effect=failure),
    )
    facade = _IfcQueryFacadeProbe(service)
    request = DirectIfcQueryRepairInput(
        direct_session_alias="direct_001",
        mode="repair",
        parent_attempt_alias=_ATTEMPT,

        edits=({"old_text": "return []", "new_text": "return [1]"},),
        page_size=25,
    )

    with pytest.raises(DirectApplicationError) as caught:
        await facade.query_ifc_python(request, request_identity="request_002")

    assert caught.value.code == "IFC_QUERY_RUNTIME_ERROR"
    assert caught.value.phase == "execution"
    assert caught.value.diagnostic == {
        "line": 2,
        "source_line": "    return missing_name\n",
        "attempt_alias": _ATTEMPT,
    }
    repair_call = service.repair_from_alias.await_args
    assert repair_call.args[0] is facade.scope
    assert repair_call.args[1] == _ATTEMPT
    assert repair_call.args[2][0].old_text == "return []"
    assert repair_call.args[3:] == ("request_002", 25)


async def test_direct_repair_passes_raw_alias_and_k_to_core_for_replay_and_conflict() -> None:
    page = IfcQueryPage(
        attempt_alias="cur.ifc_query_repaired_001",
        parent_attempt_alias=_ATTEMPT,
        source_sha256=_SHA,
        result_bytes=2,
        total_count=0,
        returned_count=0,
        results=(),
        next_cursor=None,
    )
    service = SimpleNamespace(
        repair_from_alias=AsyncMock(side_effect=(
            page,
            page,
            IfcQueryApplicationError(
                "IFC_QUERY_IDEMPOTENCY_CONFLICT", "changed repair", phase="preflight",
            ),
            IfcQueryApplicationError(
                "IFC_QUERY_ATTEMPT_NOT_FOUND", "old alias unavailable", phase="preflight",
            ),
        )),
        resolve_attempt_alias=AsyncMock(side_effect=AssertionError("adapter resolved alias")),
    )
    facade = _IfcQueryFacadeProbe(service)
    request = DirectIfcQueryRepairInput(
        direct_session_alias="direct_001", mode="repair",
        parent_attempt_alias=_ATTEMPT, edits=({"old_text": "broken", "new_text": "fixed"},),
    )
    first = await facade.query_ifc_python(request, request_identity="same-k")
    assert await facade.query_ifc_python(request, request_identity="same-k") == first
    changed = DirectIfcQueryRepairInput.model_validate({
        **request.model_dump(mode="json"),
        "edits": [{"old_text": "broken", "new_text": "different"}],
    })
    with pytest.raises(DirectApplicationError) as conflict:
        await facade.query_ifc_python(changed, request_identity="same-k")
    assert conflict.value.code == "IFC_QUERY_IDEMPOTENCY_CONFLICT"
    with pytest.raises(DirectApplicationError) as missing:
        await facade.query_ifc_python(request, request_identity="new-k")
    assert missing.value.code == "IFC_QUERY_ATTEMPT_NOT_FOUND"
    assert [call.args[3] for call in service.repair_from_alias.await_args_list] == [
        "same-k", "same-k", "same-k", "new-k",
    ]
    service.resolve_attempt_alias.assert_not_awaited()


def test_i0b304_fresh_accepts_zero_to_sixteen_unique_exact_inputs() -> None:
    base = {
        "direct_session_alias": "direct_001",
        "mode": "fresh",
        "code": "def query(ctx):\n    return []\n",
    }
    assert DirectIfcQueryFreshInput(**base).inputs == ()
    sixteen = tuple(
        {
            "input_name": f"input_{index}",
            "package_alias": f"data_{index + 100:03d}",
            "payload_name": f"payload_{index}",
        }
        for index in range(16)
    )
    assert len(DirectIfcQueryFreshInput(**base, inputs=sixteen).inputs) == 16

    with pytest.raises(ValidationError, match="unique"):
        DirectIfcQueryFreshInput(**base, inputs=(sixteen[0], sixteen[0]))
    with pytest.raises(ValidationError, match="cannot be bound twice"):
        DirectIfcQueryFreshInput(
            **base,
            inputs=(
                sixteen[0],
                {**sixteen[0], "input_name": "other_name"},
            ),
        )
    with pytest.raises(ValidationError):
        DirectIfcQueryFreshInput(**base, inputs=(*sixteen, sixteen[0]))
    with pytest.raises(ValidationError, match="trimmed"):
        DirectIfcQueryFreshInput(
            **base,
            inputs=({**sixteen[0], "payload_name": " payload_0"},),
        )


def test_i0b304_source_and_page_budgets_use_utf8_bytes_and_closed_limits() -> None:
    base = {
        "direct_session_alias": "direct_001",
        "mode": "fresh",
    }
    assert (
        DirectIfcQueryFreshInput(
            **base,
            code="a" * 65_536,
            page_size=1,
        ).page_size
        == 1
    )
    with pytest.raises(ValidationError, match="65536 UTF-8 bytes"):
        DirectIfcQueryFreshInput(**base, code="界" * 21_846)
    with pytest.raises(ValidationError):
        DirectIfcQueryFreshInput(**base, code="def query(ctx): return []", page_size=201)

    repair = {
        "direct_session_alias": "direct_001",
        "mode": "repair",
        "parent_attempt_alias": _ATTEMPT,

        "page_size": 200,
    }
    edits = tuple({"old_text": str(index), "new_text": str(index + 1)} for index in range(16))
    assert len(DirectIfcQueryRepairInput(**repair, edits=edits).edits) == 16
    with pytest.raises(ValidationError):
        DirectIfcQueryRepairInput(
            **repair,
            edits=(*edits, {"old_text": "overflow", "new_text": "no"}),
        )


async def test_i0b304_invalid_provider_branch_never_reaches_application() -> None:
    application = _application()
    server = _server(application)

    async with Client(server) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "ifc_query_python",
                {
                    "direct_session_alias": "direct_001",
                    "mode": "fresh",
                    "code": "def query(ctx): return []",
                    "cursor": "cur.invalid_mixed_branch",
                },
            )
    application.query_ifc_python.assert_not_awaited()


async def test_i0b304_persisted_failure_exposes_safe_attempt_alias_in_tool_error() -> None:
    application = _application()
    application.query_ifc_python = AsyncMock(
        side_effect=DirectApplicationError(
            "IFC_QUERY_RUNTIME_ERROR",
            "query(ctx) failed",
            phase="execution",
            diagnostic={"attempt_alias": _ATTEMPT},
        )
    )
    server = _server(application)

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool(
                "ifc_query_python",
                {
                    "direct_session_alias": "direct_001",
                    "mode": "fresh",
                    "code": "def query(ctx):\n    raise ValueError('bad')\n",
                },
            )

    detail = str(caught.value)
    assert "IFC_QUERY_RUNTIME_ERROR" in detail
    assert '"attempt_alias":"cur.ifc_query_attempt_001"' in detail
    assert "artifact" not in detail.lower()
    assert "path" not in detail.lower()


async def test_result_validation_diagnostic_survives_worker_application_and_actual_provider():
    from CAD2BIM_backend.infrastructure.ifc_query.adapter import _child_error
    from CAD2BIM_backend.application.ifc_query_service import _worker_error

    diagnostic = {"json_path": '$["units"][0]["Units"]', "value_type": "tuple", "reason": "unsupported_type"}
    failure = _worker_error(_child_error({
        "code": "IFC_QUERY_RESULT_INVALID", "message": "query(ctx) must return a canonical JSON value",
        "phase": "result_validation", "execution_started": True, "diagnostic": diagnostic,
    }))
    failure.attempt_alias = _ATTEMPT
    service = SimpleNamespace(fresh=AsyncMock(side_effect=failure))
    server = _server(_IfcQueryFacadeProbe(service))
    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("ifc_query_python", {
                "direct_session_alias": "direct_001", "mode": "fresh", "code": "def query(ctx):\n    return {}\n",
            })
    detail = str(caught.value)
    assert "IFC_QUERY_RESULT_INVALID; phase=output_validation; retryable=false" in detail
    projected = json.loads(detail.split("diagnostic=", 1)[1])
    assert projected == {**diagnostic, "attempt_alias": _ATTEMPT}
