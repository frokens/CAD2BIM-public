from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator

from CAD2BIM_backend.application.code_discovery_service import (
    CodeCard,
    CodeKind,
    CodeMember,
    CodeListResult,
    CodeReadBatchResult,
    CodeReadResult,
)
from CAD2BIM_backend.domain.text_source import TextSourceFullResult
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogRejection
from CAD2BIM_mcp.direct.code.application import CodeApplicationMixin
from CAD2BIM_mcp.direct.code.contracts import (
    DirectCodeCard,
    DirectCodeListResult,
    DirectCodeReadBatchResult,
    DirectCodeReadResult,
)
from CAD2BIM_mcp.direct.code.tools import CODE_TOOL_REGISTRARS


NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
SHA = "a" * 64
LOCATOR = "cur.locator"
_FORBIDDEN = {"allOf", "if", "then", "else", "not"}


def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


def _assert_provider_schema(schema: Mapping[str, object]) -> None:
    Draft202012Validator.check_schema(schema)
    for node in _walk(schema):
        if isinstance(node, Mapping):
            assert node != {}
            assert not (_FORBIDDEN & set(node))
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False
    serialized = json.dumps(schema, sort_keys=True).lower()
    assert "unknown" not in serialized
    for internal in ("artifact_id", "artifact_version_id", "storage", "tenant_id", "task_id"):
        assert f'"{internal}"' not in serialized


def _list_result() -> DirectCodeListResult:
    return DirectCodeListResult(
        code_kind=CodeKind.CAD_MEASUREMENT_IMPLEMENTATION,
        codes=(
            DirectCodeCard(
                code_locator=LOCATOR,
                code_kind=CodeKind.CAD_MEASUREMENT_IMPLEMENTATION,
                language="python",
                source_form="single_text",
                source_sha256=SHA,
                owner_type="critical_operation",
                owner_alias="op_004",
                status="succeeded",
                ownership="server",
                repairable=False,
            ),
        ),
        returned_count=1,
        next_cursor=None,
    )


def _read_result() -> DirectCodeReadResult:
    return DirectCodeReadResult(
        code_locator=LOCATOR,
        code_kind=CodeKind.CAD_MEASUREMENT_IMPLEMENTATION,
        language="python",
        source_form="single_text",
        source_sha256=SHA,
        view_mode="full",
        total_lines=1,
        text="def measure(ctx): return []\n",
        returned_start_line=1,
        returned_line_count=1,
        match_count=0,
        truncated=False,
    )


def _server(application: object) -> FastMCP:
    server = FastMCP(
        name="Code Provider Test",
        strict_input_validation=True,
        mask_error_details=True,
    )
    for registrar in CODE_TOOL_REGISTRARS:
        registrar(server, application)
    return server


async def test_code_tools_have_closed_provider_safe_schemas() -> None:
    server = _server(
        SimpleNamespace(
            list_code=AsyncMock(return_value=_list_result()),
            read_code=AsyncMock(return_value=_read_result()),
        )
    )
    tools = await server.list_tools()

    assert [tool.name for tool in tools] == ["code_list", "code_read"]
    assert "page_size defaults to 20" in tools[0].description
    assert "integer from 1 through 50" in tools[0].description
    assert "returned_count=0 and codes=[]" in tools[0].description
    assert "owner_alias" in tools[0].description
    assert "only code_locator is accepted by code_read" in tools[0].description
    assert "owner_alias" in tools[1].description
    assert "are not code locators" in tools[1].description
    assert len(tools[0].parameters["oneOf"]) == 2
    assert len(tools[1].parameters["anyOf"]) == 3
    for tool in tools:
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.idempotentHint is True
        _assert_provider_schema(tool.parameters)
        assert tool.output_schema is not None
        _assert_provider_schema(tool.output_schema)

    list_output = json.dumps(tools[0].output_schema, sort_keys=True)
    read_output = json.dumps(tools[1].output_schema, sort_keys=True)
    assert all(field not in list_output for field in ('"created_at"', '"finished_at"', '"sequence"'))
    assert '"selected_source_sha256"' not in read_output
    assert all(field in list_output for field in ('"source_sha256"', '"members"', '"owner_alias"'))
    assert '"historical_contract_read_only"' not in list_output
    assert all(field in read_output for field in ('"source_form"', '"source_sha256"', '"member_sha256"'))
    assert "repairable and repair_entry identify the current repair route" in tools[0].description
    assert "do not invent a repair route" in tools[0].description
    assert "historical_contract_read_only" not in tools[0].description
    assert "historical_contract_read_only" not in tools[1].description
    assert "never contain or recover authoring validation scenarios" in tools[0].description
    assert "never authorizes reuse of an earlier validation outcome" in tools[1].description

    async with Client(server) as client:
        native = await client.list_tools()
    assert [item.name for item in native] == ["code_list", "code_read"]
    assert native[0].inputSchema == tools[0].parameters
    assert native[1].outputSchema == tools[1].output_schema


async def test_code_tools_route_complete_first_and_single_text_branches() -> None:
    application = SimpleNamespace(
        list_code=AsyncMock(return_value=_list_result()),
        read_code=AsyncMock(return_value=_read_result()),
    )
    server = _server(application)

    async with Client(server) as client:
        listed = await client.call_tool(
            "code_list",
            {
                "direct_session_alias": "direct_001",
                "mode": "first",
                "query": {
                    "code_kind": "cad_measurement_implementation",
                    "page_size": 10,
                },
            },
        )
        assert listed.structured_content is not None
        code_card = listed.structured_content["codes"][0]
        assert code_card["owner_alias"] == "op_004"
        assert code_card["code_locator"] == LOCATOR
        assert code_card["owner_alias"] != code_card["code_locator"]
        read = await client.call_tool(
            "code_read",
            {
                "direct_session_alias": "direct_001",
                "source_form": "single_text",
                "code_locator": code_card["code_locator"],
                "view": {"view_mode": "full"},
            },
        )

    assert listed.structured_content == _list_result().model_dump(mode="json")
    assert read.structured_content == _read_result().model_dump(mode="json")
    list_request = application.list_code.await_args.args[0]
    assert list_request.query.code_kind == "cad_measurement_implementation"
    read_request = application.read_code.await_args.args[0]
    assert read_request.code_locator == LOCATOR
    assert read_request.source_form == "single_text"
    assert read_request.view.view_mode == "full"


async def test_code_list_application_projects_public_card_and_preserves_bundle_revision_chain() -> None:
    backend = CodeListResult(
        code_kind=CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM,
        codes=(
            CodeCard(
                code_locator=LOCATOR,
                code_kind=CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM,
                language="python",
                source_form="bundle",
                source_sha256="b" * 64,
                members=(
                    CodeMember(
                        source_path="main.py",
                        size_bytes=18,
                        sha256="c" * 64,
                        text_available=True,
                    ),
                ),
                owner_type="product_family_version",
                owner_alias="cur.family-version",
                status="published",
                ownership="agent",
                repairable=True,
                repair_entry="ifc_product_family_write",
                created_at=NOW,
                sequence=2,
            ),
        ),
        returned_count=1,
        rejections=(ProductFamilyCatalogRejection(
            family_version_ref="cur.rejected-version", version_no=1,
            code="PRODUCT_FAMILY_CONTRACT_INVALID",
            field_path="interfaces.0.position_rule.mode", reason="Input should be 'fixed'",
        ),),
        next_cursor=None,
    )
    service = SimpleNamespace(list_codes=AsyncMock(return_value=backend))
    scope = object()

    class Application(CodeApplicationMixin):
        _code_discovery_service = service

        async def _run_runtime_read(self, alias, operation, action):
            assert (alias, operation) == ("direct_001", "code_list")
            return await action(scope)

    async with Client(_server(Application())) as client:
        response = await client.call_tool(
            "code_list",
            {
                "direct_session_alias": "direct_001",
                "mode": "first",
                "query": {
                    "code_kind": "product_family_realization_program",
                    "page_size": 20,
                },
            },
        )

    card = response.structured_content["codes"][0]
    assert card["source_form"] == "bundle"
    assert card["source_sha256"] == "b" * 64
    assert card["members"] == [
        {
            "source_path": "main.py",
            "size_bytes": 18,
            "sha256": "c" * 64,
            "text_available": True,
        }
    ]
    assert card["owner_alias"] == "cur.family-version"
    assert card["code_locator"] == LOCATOR
    assert card["repair_entry"] == "ifc_product_family_write"
    assert response.structured_content["rejections"] == [{
        "family_version_alias": "cur.rejected-version", "version_no": 1,
        "code": "PRODUCT_FAMILY_CONTRACT_INVALID",
        "field_path": "interfaces.0.position_rule.mode", "reason": "Input should be 'fixed'",
    }]
    assert response.structured_content["returned_count"] == 1
    assert all(field not in card for field in ("created_at", "finished_at", "sequence"))
    called_scope, query, cursor = service.list_codes.await_args.args
    assert called_scope is scope and query.code_kind == "product_family_realization_program"
    assert cursor is None


async def test_code_tools_reject_open_or_cross_branch_fields() -> None:
    application = SimpleNamespace(list_code=AsyncMock(), read_code=AsyncMock())
    server = _server(application)

    async with Client(server) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "code_list",
                {
                    "direct_session_alias": "direct_001",
                    "mode": "first",
                    "query": {
                        "code_kind": "geometry_query_attempt",
                        "page_size": 51,
                    },
                },
            )
        with pytest.raises(ToolError):
            await client.call_tool(
                "code_list",
                {
                    "direct_session_alias": "direct_001",
                    "mode": "first",
                    "query": {
                        "code_kind": "geometry_resource_program",
                        "statuses": ["failed"],
                    },
                },
            )
        with pytest.raises(ToolError):
            await client.call_tool(
                "code_read",
                {
                    "direct_session_alias": "direct_001",
                    "source_form": "single_text",
                    "code_locator": LOCATOR,
                    "source_path": "main.py",
                    "view": {"view_mode": "search", "text": ".*", "regex": True},
                },
            )

    application.list_code.assert_not_awaited()
    application.read_code.assert_not_awaited()


async def test_code_batch_preserves_member_selection_order_and_closed_branches() -> None:
    member = _read_result().model_copy(
        update={
            "source_form": "bundle",
            "source_path": "layout.py",
            "member_sha256": SHA,
        }
    )
    expected = DirectCodeReadBatchResult(reads=(_read_result(), member))
    application = SimpleNamespace(list_code=AsyncMock(), read_code=AsyncMock(return_value=expected))
    payload = {
        "direct_session_alias": "direct_001",
        "mode": "batch",
        "reads": [
            {"source_form": "single_text", "code_locator": LOCATOR, "view": {"view_mode": "full"}},
            {
                "source_form": "bundle",
                "code_locator": LOCATOR,
                "source_path": "layout.py",
                "view": {"view_mode": "lines", "start_line": 1, "line_count": 10},
            },
        ],
    }
    async with Client(_server(application)) as client:
        response = await client.call_tool("code_read", payload)
        assert response.structured_content == expected.model_dump(mode="json")
        request = application.read_code.await_args.args[0]
        assert request.reads[1].source_path == "layout.py"
        for invalid in (
            {**payload, "source_form": "bundle"},
            {**payload, "reads": []},
            {**payload, "reads": payload["reads"] * 5},
            {**payload, "reads": [{**payload["reads"][0], "source_path": "secret.py"}]},
        ):
            with pytest.raises(ToolError):
                await client.call_tool("code_read", invalid)
    assert application.read_code.await_count == 1


async def test_code_batch_application_projects_member_sha_and_uses_one_scope() -> None:
    source = CodeReadResult(
        code_locator=LOCATOR,
        code_kind=CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM,
        language="python",
        source_form="bundle",
        source_sha256="b" * 64,
        selected_path="layout.py",
        member_sha256="c" * 64,
        view=TextSourceFullResult(sha256="c" * 64, size_bytes=4, total_lines=1, text="pass"),
    )
    service = SimpleNamespace(
        read_code_batch=AsyncMock(return_value=CodeReadBatchResult(reads=(source,)))
    )
    scope = object()

    class Application(CodeApplicationMixin):
        _code_discovery_service = service

        async def _run_runtime_read(self, alias, operation, action):
            assert (alias, operation) == ("direct_001", "code_read")
            return await action(scope)

    async with Client(_server(Application())) as client:
        response = await client.call_tool(
            "code_read",
            {
                "direct_session_alias": "direct_001",
                "mode": "batch",
                "reads": [
                    {
                        "source_form": "bundle",
                        "code_locator": LOCATOR,
                        "source_path": "layout.py",
                        "view": {"view_mode": "full"},
                    }
                ],
            },
        )
    record = response.structured_content["reads"][0]
    assert record["source_sha256"] == "b" * 64
    assert record["member_sha256"] == "c" * 64
    assert "selected_source_sha256" not in record
    assert record["source_path"] == "layout.py" and record["text"] == "pass"
    called_scope, selections = service.read_code_batch.await_args.args
    assert called_scope is scope
    assert selections[0].source_path == "layout.py"
    assert selections[0].view.view_mode == "full"
    assert "direct_session_alias" not in type(selections[0]).model_fields


async def test_code_list_provider_fallback_reports_the_page_size_path() -> None:
    application = SimpleNamespace(list_code=AsyncMock(), read_code=AsyncMock())
    server = _server(application)
    tool = await server.get_tool("code_list")
    assert tool is not None

    with pytest.raises(ToolError) as exc_info:
        await tool.run(
            {
                "direct_session_alias": "direct_001",
                "mode": "first",
                "query": {
                    "code_kind": "geometry_query_attempt",
                    "page_size": 51,
                },
            }
        )

    message = str(exc_info.value)
    assert "INVALID_ARGUMENT; phase=preflight; retryable=false" in message
    assert "invalid_path=first.query.geometry_query_attempt.page_size" in message
    assert "error_type=less_than_equal" in message
    application.list_code.assert_not_awaited()


async def test_provider_phase_observations_keep_errors_and_isolate_call_context(caplog) -> None:
    application = SimpleNamespace(
        list_code=AsyncMock(return_value=_list_result()), read_code=AsyncMock()
    )
    tool = await _server(application).get_tool("code_list")
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    with pytest.raises(ToolError):
        await tool.run(
            {
                "direct_session_alias": "direct_001",
                "mode": "first",
                "query": {"code_kind": "geometry_query_attempt", "page_size": 51},
                "secret_marker": "do-not-record-this-value",
            }
        )
    await tool.run(
        {
            "direct_session_alias": "direct_001",
            "mode": "first",
            "query": {"code_kind": "geometry_query_attempt", "page_size": 10},
        }
    )
    records = [
        record.cad2bim_execution_phase
        for record in caplog.records
        if hasattr(record, "cad2bim_execution_phase")
    ]
    ends = [record for record in records if record["transition"] == "end"]
    assert [(r["phase"], r["status"]) for r in ends] == [
        ("parse", "failed"),
        ("parse", "succeeded"),
        ("dispatch", "succeeded"),
    ]
    assert ends[0]["correlation_id"] != ends[1]["correlation_id"]
    assert ends[1]["correlation_id"] == ends[2]["correlation_id"]
    assert all(r["operation"] == "code_list" and len(r["input_sha256"]) == 64 for r in records)
    assert "do-not-record-this-value" not in json.dumps(records)
    application.list_code.assert_awaited_once()


async def test_product_family_code_queries_are_closed_provider_branches() -> None:
    application = SimpleNamespace(
        list_code=AsyncMock(return_value=_list_result()),
        read_code=AsyncMock(return_value=_read_result()),
    )
    server = _server(application)

    async with Client(server) as client:
        await client.call_tool(
            "code_list",
            {
                "direct_session_alias": "direct_001",
                "mode": "first",
                "query": {
                    "code_kind": "product_family_authoring_attempt",
                    "statuses": ["failed"],
                },
            },
        )
        await client.call_tool(
            "code_list",
            {
                "direct_session_alias": "direct_001",
                "mode": "first",
                "query": {
                    "code_kind": "product_family_realization_program",
                },
            },
        )
        with pytest.raises(ToolError):
            await client.call_tool(
                "code_list",
                {
                    "direct_session_alias": "direct_001",
                    "mode": "first",
                    "query": {
                        "code_kind": "product_family_realization_program",
                        "statuses": ["failed"],
                    },
                },
            )

    assert application.list_code.await_count == 2
