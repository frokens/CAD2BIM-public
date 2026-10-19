from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, TypeAdapter
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from CAD2BIM_mcp.direct.bootstrap import (
    DirectRuntimeResources,
    create_direct_runtime_resources,
)
from CAD2BIM_backend.domain.data_json import JsonReadResult, JsonValueResult
from CAD2BIM_backend.domain.data_query import DataDescribePackageResult, JsonPayloadSchema
from CAD2BIM_backend.infrastructure.postgres.runtime_roles import provision_runtime_login_roles
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.config import DirectMcpConfig
from CAD2BIM_backend.domain.data_runtime import ValidatedSource
from CAD2BIM_mcp.direct.transform.contracts import (
    DataTransformResult,
    MultiOutputDataTransformResult,
)
from CAD2BIM_mcp.direct.session.contracts import (
    DirectSessionResult,
)
from CAD2BIM_mcp.direct.server import create_direct_mcp


pytestmark = [pytest.mark.integration, pytest.mark.network]
TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL")
PYTHON_WORKER = os.getenv("CAD2BIM_TEST_DATA_PYTHON_WORKER_EXECUTABLE")
FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "cad"
SOURCE = FIXTURE_ROOT / "minimal_line.dxf"
API_PASSWORD = "m2c-json-api-password"
RUNNER_PASSWORD = "m2c-json-runner-password"
MCP_PASSWORD = "m2c-json-mcp-password"
CURSOR_KEY = "public-test-key-" * 4
T = TypeVar("T", bound=BaseModel)

DOCUMENT_SCHEMA = json.dumps(
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "entity_count": {"type": "integer"},
            "ready": {"type": "boolean"},
        },
        "required": ["entity_count", "ready"],
        "additionalProperties": False,
    },
    separators=(",", ":"),
)
RECORDS_SCHEMA = json.dumps(
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "record_key": {"type": "string"},
                "ordinal": {"type": "integer"},
            },
            "required": ["record_key", "ordinal"],
            "additionalProperties": False,
        },
    },
    separators=(",", ":"),
)
DESCRIPTION = """# Durable M2c JSON fixture
## Summary
Publishes Parquet, document JSON, and records JSON from one exact L0 input.
## Intended use
Verify PostgreSQL persistence, restart recovery, and RLS isolation.
## Contents
Three queryable payloads with two exact JSON schemas.
## Method
Run one controlled Python/Arrow transform over minimal_line.dxf.
## Scope and completeness
Contains only deterministic integration-fixture values.
## Known limitations
This package is not an IFC modeling decision.
"""
CONSUMER_DESCRIPTION = """# Durable JSON consumer
## Summary
Consumes exact document and records JSON after a runtime restart.
## Intended use
Verify JSON Package members remain valid transformation inputs.
## Contents
One Parquet summary payload.
## Method
Use ctx.json for both exact immutable JSON inputs.
## Scope and completeness
Contains only the two declared JSON inputs.
## Known limitations
This package is not an IFC modeling decision.
"""


def _runtime_database_url(value: str, *, username: str, password: str) -> str:
    return (
        make_url(value)
        .set(drivername="postgresql+psycopg", username=username, password=password)
        .render_as_string(hide_password=False)
    )


def _config(subject: str, storage_root: Path) -> DirectMcpConfig:
    assert TEST_DATABASE_URL is not None
    assert PYTHON_WORKER is not None
    return DirectMcpConfig.from_environ(
        {
            "CAD2BIM_DIRECT_SUBJECT": subject,
            "CAD2BIM_DATABASE_URL": _runtime_database_url(
                TEST_DATABASE_URL,
                username="cad2bim_mcp_login",
                password=MCP_PASSWORD,
            ),
            "CAD2BIM_STORAGE_ROOT": str(storage_root),
            "CAD2BIM_MAX_UPLOAD_BYTES": str(100 * 1024 * 1024),
            "CAD2BIM_DATA_QUERY_CURSOR_KEY": CURSOR_KEY,
            "CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE": PYTHON_WORKER,
        }
    )


def _server(resources: DirectRuntimeResources, config: DirectMcpConfig):
    return create_direct_mcp(
        DirectApplicationFacade(
            resources.application,
            resources.sessions,
            resources.scope_resolver,
            resources.scope_binder,
            query_service=resources.query_service,
            query_sql_service=resources.query_sql_service,
            json_service=resources.json_service,
            transform_service=resources.transform_service,
        ),
    )


def _structured(result: Any, contract: type[T]) -> T:
    assert result.is_error is False
    assert result.structured_content is not None
    return contract.model_validate(result.structured_content)


def _structured_union(result: Any, contract: Any) -> Any:
    assert result.is_error is False
    assert result.structured_content is not None
    return TypeAdapter(contract).validate_python(result.structured_content)


def _producer_request(session: str, source_package: str) -> dict[str, Any]:
    code = """def transform(ctx):
    import pyarrow as pa
    count = ctx.table("entities").num_rows
    ctx.write_table("entity_summary", pa.table({"summary_key": ["all"], "entity_count": [count]}))
    ctx.write_json("durable_document", {"entity_count": count, "ready": True})
    ctx.write_json(
        "durable_records",
        [
            {"record_key": "record_0", "ordinal": 0},
            {"record_key": "record_1", "ordinal": 1},
        ],
    )
"""
    return {
        "mode": "create",
        "direct_session_alias": session,
        "inputs": [
            {
                "input_name": "entities",
                "package_alias": source_package,
                "payload_name": "entities",
            }
        ],
        "inline_tables": [],
        "parameters": [],
        "execution": {"engine": "python_arrow", "code": code},
        "outputs": [
            {
                "payload_name": "entity_summary",
                "format": "parquet",
                "description": "One durable Parquet summary row.",
                "logical_key_fields": ["summary_key"],
            },
            {
                "payload_name": "durable_document",
                "format": "json",
                "description": "One durable JSON document.",
                "json_kind": "document",
                "schema_json": DOCUMENT_SCHEMA,
            },
            {
                "payload_name": "durable_records",
                "format": "json",
                "description": "Two durable JSON records.",
                "json_kind": "records",
                "schema_json": RECORDS_SCHEMA,
            },
        ],
        "output_package": {
            "name": "postgres_m2c_json_fixture_v1",
            "description_markdown": DESCRIPTION,
        },
    }


def _consumer_request(session: str, package_alias: str) -> dict[str, Any]:
    code = """def transform(ctx):
    import pyarrow as pa
    document = ctx.json("document_input")
    records = ctx.json("records_input")
    ctx.write_table(
        "json_summary",
        pa.table(
            {
                "summary_key": ["durable_json"],
                "entity_count": [document["entity_count"]],
                "record_count": [len(records)],
            }
        ),
    )
"""
    return {
        "mode": "create",
        "direct_session_alias": session,
        "inputs": [
            {
                "input_name": "document_input",
                "package_alias": package_alias,
                "payload_name": "durable_document",
            },
            {
                "input_name": "records_input",
                "package_alias": package_alias,
                "payload_name": "durable_records",
            },
        ],
        "inline_tables": [],
        "parameters": [],
        "execution": {"engine": "python_arrow", "code": code},
        "outputs": [
            {
                "payload_name": "json_summary",
                "format": "parquet",
                "description": "Summary read from two exact durable JSON inputs.",
                "logical_key_fields": ["summary_key"],
            }
        ],
        "output_package": {
            "name": "postgres_m2c_json_consumer_v1",
            "description_markdown": CONSUMER_DESCRIPTION,
        },
    }


async def test_postgres_m2c_json_survives_restart_and_is_rls_isolated(
    tmp_path: Path,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    if not PYTHON_WORKER or not Path(PYTHON_WORKER).is_file():
        pytest.skip("CAD2BIM_TEST_DATA_PYTHON_WORKER_EXECUTABLE is not configured")

    provision_runtime_login_roles(
        TEST_DATABASE_URL,
        environ={
            "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
            "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
            "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
        },
    )
    subject = f"m2c-json-{uuid4().hex}"
    config = _config(subject, tmp_path / "storage")
    session_name = "M2c PostgreSQL JSON recovery"

    first_resources = create_direct_runtime_resources(config)
    try:
        await first_resources.verify_ready()
        async with Client(_server(first_resources, config)) as client:
            opened = _structured(
                await client.call_tool(
                    "runtime_open_direct_session",
                    {"session_name": session_name},
                ),
                DirectSessionResult,
            )
            # User upload is prepared by the backend before Agent discovery.
            upload_scope = await first_resources.scope_resolver.resolve(opened.direct_session_alias, command_id="upload-imported")
            with first_resources.scope_binder(upload_scope.identity):
                imported = await first_resources.application.import_cad_source(upload_scope, ValidatedSource(
                    filename=SOURCE.name, media_type="application/dxf",
                    content=SOURCE.read_bytes(), source_identity=str(SOURCE), adapter_metadata={},
                ))
            produced = _structured(
                await client.call_tool(
                    "data_transform",
                    _producer_request(
                        opened.direct_session_alias,
                        imported.published.package.catalog_entry.alias,
                    ),
                    timeout=125,
                ),
                MultiOutputDataTransformResult,
            )
    finally:
        await first_resources.close()

    restarted_resources = create_direct_runtime_resources(config)
    try:
        await restarted_resources.verify_ready()
        async with Client(_server(restarted_resources, config)) as client:
            resumed = _structured(
                await client.call_tool(
                    "runtime_open_direct_session",
                    {"session_name": session_name},
                ),
                DirectSessionResult,
            )
            assert resumed.direct_session_alias == opened.direct_session_alias
            assert resumed.resumed is True

            described = _structured(
                await client.call_tool(
                    "data_describe_package",
                    {
                        "direct_session_alias": resumed.direct_session_alias,
                        "package_alias": produced.package_alias,
                        "payload_name": "durable_document",
                    },
                ),
                DataDescribePackageResult,
            )
            assert isinstance(described.payload_schema, JsonPayloadSchema)
            assert described.payload_schema.schema_json == DOCUMENT_SCHEMA

            read = _structured_union(
                await client.call_tool(
                    "data_read_json",
                    {
                        "direct_session_alias": resumed.direct_session_alias,
                        "query": {
                            "mode": "value",
                            "package_alias": produced.package_alias,
                            "payload_name": "durable_document",
                            "pointer": "",
                        },
                    },
                ),
                JsonReadResult,
            )
            assert isinstance(read, JsonValueResult)
            assert json.loads(read.value_json)["ready"] is True

            consumed = _structured(
                await client.call_tool(
                    "data_transform",
                    _consumer_request(resumed.direct_session_alias, produced.package_alias),
                    timeout=125,
                ),
                DataTransformResult,
            )
            assert consumed.output.payload_name == "json_summary"
            assert consumed.output.row_count == 1
    finally:
        await restarted_resources.close()

    isolated_config = _config(f"{subject}-isolated", tmp_path / "isolated-storage")
    isolated_resources = create_direct_runtime_resources(isolated_config)
    try:
        await isolated_resources.verify_ready()
        async with Client(_server(isolated_resources, isolated_config)) as client:
            isolated = _structured(
                await client.call_tool(
                    "runtime_open_direct_session",
                    {"session_name": "M2c isolated identity"},
                ),
                DirectSessionResult,
            )
            with pytest.raises(ToolError, match="PACKAGE_NOT_FOUND"):
                await client.call_tool(
                    "data_describe_package",
                    {
                        "direct_session_alias": isolated.direct_session_alias,
                        "package_alias": produced.package_alias,
                    },
                )
    finally:
        await isolated_resources.close()

    inspection = create_async_engine(TEST_DATABASE_URL)
    try:
        async with inspection.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT dpm.member_name, dpm.member_kind, dpm.media_type, "
                            "dpm.json_kind FROM direct_session_bindings AS dsb "
                            "JOIN data_packages AS dp ON dp.task_id = dsb.task_id "
                            "JOIN data_package_members AS dpm ON dpm.package_id = dp.package_id "
                            "WHERE dsb.configured_subject = :subject "
                            "AND dp.name = 'postgres_m2c_json_fixture_v1' "
                            "ORDER BY dpm.member_name"
                        ),
                        {"subject": subject},
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await inspection.dispose()

    assert {(row["member_name"], row["member_kind"], row["json_kind"]) for row in rows} == {
        ("code/transformation.py", "code", None),
        ("description.md", "description", None),
        ("manifest.json", "manifest", None),
        ("payloads/durable_document.json", "payload", "document"),
        ("payloads/durable_records.json", "payload", "records"),
        ("payloads/entity_summary/part-00000.parquet", "payload", None),
        ("schemas/durable_document.schema.json", "schema", None),
        ("schemas/durable_records.schema.json", "schema", None),
    }
