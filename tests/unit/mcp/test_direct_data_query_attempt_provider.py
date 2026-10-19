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

from CAD2BIM_backend.application.code_discovery_service import CodeKind
from CAD2BIM_mcp.direct.query.python_inputs import (
    QueryPythonContinuationInput,
    QueryPythonFirstPageInput,
    QueryPythonRepairInput,
    validate_direct_data_query_python_input,
)
from CAD2BIM_mcp.direct.query.sql_inputs import (
    QuerySqlContinuationInput,
    QuerySqlFirstPageInput,
    QuerySqlRepairInput,
    validate_direct_data_query_sql_input,
)
from CAD2BIM_mcp.direct.query.tools import register_data_query_python, register_data_query_sql


FORBIDDEN_SCHEMA_KEYS = {"allOf", "if", "then", "else", "not"}


def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


def _assert_provider_safe(schema: Mapping[str, object]) -> None:
    Draft202012Validator.check_schema(schema)
    for node in _walk(schema):
        if isinstance(node, Mapping):
            assert node != {}
            assert not (FORBIDDEN_SCHEMA_KEYS & set(node))
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False
    assert "unknown" not in json.dumps(schema, sort_keys=True).lower()


async def test_sql_and_python_query_provider_have_three_closed_attempt_branches() -> None:
    server = FastMCP(name="Data Query Attempt Provider Test", strict_input_validation=True)
    application = SimpleNamespace(
        query_sql=AsyncMock(),
        repair_query_sql=AsyncMock(),
        query_python=AsyncMock(),
        repair_query_python=AsyncMock(),
    )
    register_data_query_sql(server, application)
    register_data_query_python(server, application)
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tuple(tools) == ("data_query_sql", "data_query_python")

    for tool in tools.values():
        schema = tool.parameters
        _assert_provider_safe(schema)
        branches = schema["oneOf"]
        assert [branch["properties"]["mode"]["const"] for branch in branches] == [
            "fresh",
            "repair",
            "cursor",
        ]
        assert all(branch["additionalProperties"] is False for branch in branches)
        assert tuple(branches[1]["properties"]) == (
            "direct_session_alias",
            "mode",
            "parent_attempt_alias",
            "edits",
            "page_size",
        )
        assert branches[1]["properties"]["edits"]["maxItems"] == 16
        assert set(branches[2]["properties"]) == {
            "direct_session_alias",
            "mode",
            "cursor",
        }
        assert tool.output_schema is not None
        _assert_provider_safe(tool.output_schema)


@pytest.mark.parametrize(
    ("validator", "fresh_type", "repair_type", "cursor_type", "query"),
    [
        (
            validate_direct_data_query_sql_input,
            QuerySqlFirstPageInput,
            QuerySqlRepairInput,
            QuerySqlContinuationInput,
            {
                "inputs": [
                    {
                        "input_name": "records",
                        "package_alias": "data_001",
                        "payload_name": "records",
                    }
                ],
                "parameters": [],
                "execution": {
                    "engine": "duckdb_sql",
                    "code": "SELECT * FROM records",
                },
                "page_size": 100,
            },
        ),
        (
            validate_direct_data_query_python_input,
            QueryPythonFirstPageInput,
            QueryPythonRepairInput,
            QueryPythonContinuationInput,
            {
                "inputs": [
                    {
                        "input_name": "records",
                        "package_alias": "data_001",
                        "payload_name": "records",
                    }
                ],
                "inline_tables": [],
                "parameters": [],
                "code": "def query(ctx):\n    return ctx.input('records')\n",
                "page_size": 100,
            },
        ),
    ],
)
def test_attempt_branch_validators_reject_cross_branch_fields(
    validator, fresh_type, repair_type, cursor_type, query
) -> None:
    common = {"direct_session_alias": "direct_001"}
    assert isinstance(validator({**common, "mode": "fresh", "query": query}), fresh_type)
    assert isinstance(
        validator(
            {
                **common,
                "mode": "repair",
                "parent_attempt_alias": "cur.parent",

                "edits": [{"old_text": "bad", "new_text": "good"}],
            }
        ),
        repair_type,
    )
    assert isinstance(
        validator({**common, "mode": "cursor", "cursor": "cur.page"}), cursor_type
    )
    with pytest.raises(ValidationError):
        validator(
            {
                **common,
                "mode": "repair",
                "parent_attempt_alias": "cur.parent",
                "edits": [{"old_text": "bad", "new_text": "good"}],
                "expected_source_sha256": "a" * 64,
            }
        )
    with pytest.raises(ValidationError):
        validator({**common, "mode": "cursor", "cursor": "cur.page", "query": query})
    with pytest.raises(ValidationError):
        validator(
            {
                **common,
                "mode": "repair",
                "parent_attempt_alias": "cur.parent",

                "edits": [{"old_text": "bad", "new_text": "good"}],
                "query": query,
            }
        )


def test_code_catalog_exposes_separate_sql_and_python_attempt_kinds() -> None:
    assert CodeKind.DATA_QUERY_SQL_ATTEMPT.value == "data_query_sql_attempt"
    assert CodeKind.DATA_QUERY_PYTHON_ATTEMPT.value == "data_query_python_attempt"


@pytest.mark.parametrize("engine", ["sql", "python"])
@pytest.mark.parametrize("branch", ["fresh", "cursor", "repair", "invalid"])
async def test_query_mode_defaults_reach_application_without_weakening_branches(
    engine: str, branch: str,
) -> None:
    server = FastMCP(name="Query Mode Dispatch Test", strict_input_validation=True)
    query_call = AsyncMock(side_effect=RuntimeError("application boundary reached"))
    repair_call = AsyncMock(side_effect=RuntimeError("application boundary reached"))
    application = SimpleNamespace(**{
        f"query_{engine}": query_call,
        f"repair_query_{engine}": repair_call,
    })
    register = register_data_query_sql if engine == "sql" else register_data_query_python
    register(server, application)
    query = {
        "inputs": [{"input_name": "records", "package_alias": "data_001", "payload_name": "records"}],
    }
    if engine == "sql":
        query["execution"] = {"engine": "duckdb_sql", "code": "SELECT * FROM records"}
    else:
        query["code"] = "def query(ctx):\n    return ctx.table('records')\n"
    repair = {
        "parent_attempt_alias": "cur.parent",

        "edits": [{"old_text": "bad", "new_text": "good"}],
    }
    common = {"direct_session_alias": "direct_001"}
    async with Client(server) as client:
        if branch == "invalid":
            for extra in (
                {"query": query, "mode": None},
                {"query": query, "cursor": "cur.page"},
                {"query": query, "mode": "repair", **repair},
                repair,
            ):
                with pytest.raises(ToolError):
                    await client.call_tool(f"data_query_{engine}", {**common, **extra})
            query_call.assert_not_awaited()
            repair_call.assert_not_awaited()
            return
        extra = ({"query": query} if branch == "fresh" else
                 {"cursor": "cur.page"} if branch == "cursor" else
                 {"mode": "repair", **repair})
        # A deliberate Application exception stops before output serialization:
        # this test covers real FastMCP argument dispatch, without a Worker or DB.
        with pytest.raises(ToolError, match="application boundary reached"):
            await client.call_tool(f"data_query_{engine}", {**common, **extra})
    if branch == "repair":
        repair_call.assert_awaited_once()
        query_call.assert_not_awaited()
        assert repair_call.await_args.args[:2] == ("direct_001", "cur.parent")
        assert repair_call.await_args.args[2][0].old_text == "bad"
        assert repair_call.await_args.args[2][0].new_text == "good"
    else:
        query_call.assert_awaited_once()
        repair_call.assert_not_awaited()
        alias, domain_query, cursor = query_call.await_args.args
        assert alias == "direct_001"
        if branch == "fresh":
            if engine == "sql":
                assert domain_query.execution.engine == "duckdb_sql"
            else:
                assert domain_query.code == query["code"]
            assert cursor is None
        else:
            assert domain_query is None
            assert cursor == "cur.page"
