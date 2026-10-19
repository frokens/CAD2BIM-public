from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys
from typing import Any, TypeVar

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, TypeAdapter

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.data_json_service import DataJsonService
from CAD2BIM_backend.application.data_query_service import DataQueryService
from CAD2BIM_backend.application.data_runtime_service import (
    DataRuntimeService,
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.application.data_transform_service import DataTransformService
from CAD2BIM_backend.application.exact_payload_resolver import ExactPayloadResolver
from CAD2BIM_backend.domain.data_json import (
    JsonChildrenResult,
    JsonReadResult,
    JsonRecordsResult,
    JsonValueResult,
)
from CAD2BIM_backend.domain.data_query import (
    DataQueryRowsResult,
    JsonPayloadSchema,
)
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.data_transform import (
    SubprocessDuckDBDataTransformWorker,
    SubprocessPythonArrowDataTransformWorker,
)
from CAD2BIM_backend.infrastructure.memory.control_plane_repository import (
    InMemoryControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.memory.identity_directory import InMemoryIdentityDirectory
from CAD2BIM_backend.infrastructure.memory.state import MemoryState
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.catalog.contracts import DirectDataDescribePackageResult
from CAD2BIM_backend.domain.data_runtime import ValidatedSource
from CAD2BIM_mcp.direct.session.contracts import (
    DirectSessionResult,
)
from CAD2BIM_mcp.direct.transform.contracts import (
    MultiOutputDataTransformResult,
)
from CAD2BIM_mcp.direct.server import create_direct_mcp


pytestmark = pytest.mark.integration
FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "cad"
SOURCE = FIXTURE_ROOT / "minimal_line.dxf"
T = TypeVar("T", bound=BaseModel)

DOCUMENT_SCHEMA = json.dumps(
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "metrics": {
                "type": "object",
                "properties": {"entity_count": {"type": "integer"}},
                "required": ["entity_count"],
                "additionalProperties": False,
            },
            "layers": {"type": "array", "items": {"type": "string"}},
            "ready": {"type": "boolean"},
        },
        "required": ["metrics", "layers", "ready"],
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
ROUND_TRIP_SCHEMA = json.dumps(
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "entity_count": {"type": "integer"},
            "record_count": {"type": "integer"},
        },
        "required": ["entity_count", "record_count"],
        "additionalProperties": False,
    },
    separators=(",", ":"),
)

FIRST_DESCRIPTION = """# Minimal JSON capability
## Summary
Publishes one Parquet payload and two exact JSON payloads from minimal_line.dxf.
## Intended use
Exercise the Direct M2c JSON publication and bounded read loop.
## Contents
One Parquet summary, one document JSON, and one records JSON payload.
## Method
Use the controlled Python/Arrow worker and predeclared exact schemas.
## Scope and completeness
Contains facts computed from the exact imported L0 entities payload.
## Known limitations
This is a focused integration fixture, not an IFC modeling decision.
"""

SECOND_DESCRIPTION = """# JSON input round trip
## Summary
Consumes two exact published JSON payloads and emits Parquet and JSON.
## Intended use
Verify ctx.json exact input binding across immutable packages.
## Contents
One row summary and one document JSON payload.
## Method
Read document and records values with ctx.json in the Python/Arrow worker.
## Scope and completeness
Contains only values derived from the two declared JSON inputs.
## Known limitations
This is a focused integration fixture, not an IFC modeling decision.
"""


def _cursor_key() -> str:
    return base64.urlsafe_b64encode(b"m2c-direct-json-e2e-key-00000001").decode().rstrip("=")


def _structured(result: Any, contract: type[T]) -> T:
    assert result.is_error is False
    assert result.structured_content is not None
    return contract.model_validate(result.structured_content)


def _structured_union(result: Any, contract: Any) -> Any:
    assert result.is_error is False
    assert result.structured_content is not None
    return TypeAdapter(contract).validate_python(result.structured_content)


def _output(payload_name: str, *, json_kind: str | None = None) -> dict[str, Any]:
    if json_kind is None:
        return {
            "payload_name": payload_name,
            "format": "parquet",
            "description": "One bounded summary row derived from exact input data.",
            "logical_key_fields": ["summary_key"],
        }
    return {
        "payload_name": payload_name,
        "format": "json",
        "description": f"Exact {json_kind} JSON fixture for bounded reads.",
        "json_kind": json_kind,
        "schema_json": DOCUMENT_SCHEMA if json_kind == "document" else RECORDS_SCHEMA,
    }


def _first_transform(session: str, source_package: str) -> dict[str, Any]:
    code = """def transform(ctx):
    import pyarrow as pa
    entities = ctx.table("entities")
    count = entities.num_rows
    ctx.write_table(
        "entity_summary",
        pa.table({"summary_key": ["all"], "entity_count": [count]}),
    )
    ctx.write_json(
        "analysis_document",
        {"metrics": {"entity_count": count}, "layers": ["0", "fixture"], "ready": True},
    )
    ctx.write_json(
        "analysis_records",
        [
            {"record_key": "record_0", "ordinal": 0},
            {"record_key": "record_1", "ordinal": 1},
            {"record_key": "record_2", "ordinal": 2},
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
            _output("entity_summary"),
            _output("analysis_document", json_kind="document"),
            _output("analysis_records", json_kind="records"),
        ],
        "output_package": {
            "name": "minimal_json_capability_v1",
            "description_markdown": FIRST_DESCRIPTION,
        },
    }


def _second_transform(session: str, first_package: str) -> dict[str, Any]:
    code = """def transform(ctx):
    import pyarrow as pa
    document = ctx.json("document_input")
    records = ctx.json("records_input")
    entity_count = document["metrics"]["entity_count"]
    record_count = len(records)
    ctx.write_table(
        "round_trip_rows",
        pa.table(
            {
                "summary_key": ["json_round_trip"],
                "entity_count": [entity_count],
                "record_count": [record_count],
            }
        ),
    )
    ctx.write_json(
        "round_trip_document",
        {"entity_count": entity_count, "record_count": record_count},
    )
"""
    return {
        "mode": "create",
        "direct_session_alias": session,
        "inputs": [
            {
                "input_name": "document_input",
                "package_alias": first_package,
                "payload_name": "analysis_document",
            },
            {
                "input_name": "records_input",
                "package_alias": first_package,
                "payload_name": "analysis_records",
            },
        ],
        "inline_tables": [],
        "parameters": [],
        "execution": {"engine": "python_arrow", "code": code},
        "outputs": [
            _output("round_trip_rows"),
            {
                "payload_name": "round_trip_document",
                "format": "json",
                "description": "Summary proving two exact JSON inputs were consumed.",
                "json_kind": "document",
                "schema_json": ROUND_TRIP_SCHEMA,
            },
        ],
        "output_package": {
            "name": "minimal_json_round_trip_v1",
            "description_markdown": SECOND_DESCRIPTION,
        },
    }


async def test_direct_m2c_json_publication_read_and_exact_input_round_trip(
    tmp_path: Path,
) -> None:
    repository = InMemoryControlPlaneRepository(MemoryState())
    identities = InMemoryIdentityDirectory()
    artifacts = ArtifactService(repository, LocalStorage(tmp_path / "storage"))
    runtime = DataRuntimeService(repository, artifacts)
    sessions = DirectSessionService(repository, identities, "m2c-json-e2e-user")
    resolver = DirectScopeResolver(repository, identities, "m2c-json-e2e-user")
    payloads = ExactPayloadResolver(repository, artifacts)
    cursor_codec = CursorCodec(_cursor_key())
    queries = DataQueryService(repository, artifacts, cursor_codec)
    json_reads = DataJsonService(payloads, artifacts, cursor_codec)
    duckdb_worker = SubprocessDuckDBDataTransformWorker()
    python_worker = SubprocessPythonArrowDataTransformWorker(
        python_executable=os.environ.get(
            "CAD2BIM_TEST_DATA_PYTHON_WORKER_EXECUTABLE",
            sys.executable,
        )
    )
    transform = DataTransformService(
        repository,
        artifacts,
        payloads,
        duckdb_worker,
        python_worker=python_worker,
    )
    server = create_direct_mcp(
        DirectApplicationFacade(
            runtime,
            sessions,
            resolver,
            query_service=queries,
            json_service=json_reads,
            transform_service=transform,
        ),
    )

    try:
        async with Client(server) as client:
            opened = _structured(
                await client.call_tool(
                    "runtime_open_direct_session",
                    {"session_name": "M2c Direct JSON capability"},
                ),
                DirectSessionResult,
            )
            session = opened.direct_session_alias
            # Backend preparation of a user upload; the Agent only consumes its Package.
            upload_scope = await resolver.resolve(session, command_id="upload-fixture-imported")
            imported = await runtime.import_cad_source(upload_scope, ValidatedSource(
                filename=SOURCE.name, media_type="application/dxf", content=SOURCE.read_bytes(),
                source_identity=str(SOURCE), adapter_metadata={}))

            first = _structured(
                await client.call_tool(
                    "data_transform",
                    _first_transform(session, imported.published.package.catalog_entry.alias),
                    timeout=125,
                ),
                MultiOutputDataTransformResult,
            )
            assert [(item.payload_name, item.format) for item in first.outputs] == [
                ("entity_summary", "parquet"),
                ("analysis_document", "json"),
                ("analysis_records", "json"),
            ]

            described = _structured(
                await client.call_tool(
                    "data_describe_package",
                    {
                        "direct_session_alias": session,
                        "package_alias": first.package_alias,
                        "payload_name": "analysis_document",
                    },
                ),
                DirectDataDescribePackageResult,
            )
            assert isinstance(described.payload_schema, JsonPayloadSchema)
            assert described.payload_schema.json_kind == "document"
            assert described.payload_schema.schema_json == DOCUMENT_SCHEMA

            value = _structured_union(
                await client.call_tool(
                    "data_read_json",
                    {
                        "direct_session_alias": session,
                        "query": {
                            "mode": "value",
                            "package_alias": first.package_alias,
                            "payload_name": "analysis_document",
                            "pointer": "/metrics",
                        },
                    },
                ),
                JsonReadResult,
            )
            assert isinstance(value, JsonValueResult)
            assert json.loads(value.value_json)["entity_count"] >= 1

            children_first = _structured_union(
                await client.call_tool(
                    "data_read_json",
                    {
                        "direct_session_alias": session,
                        "query": {
                            "mode": "children",
                            "package_alias": first.package_alias,
                            "payload_name": "analysis_document",
                            "pointer": "",
                            "page_size": 2,
                        },
                    },
                ),
                JsonReadResult,
            )
            assert isinstance(children_first, JsonChildrenResult)
            assert children_first.page.complete is False
            assert children_first.page.next_cursor is not None
            children_second = _structured_union(
                await client.call_tool(
                    "data_read_json",
                    {
                        "direct_session_alias": session,
                        "cursor": children_first.page.next_cursor,
                    },
                ),
                JsonReadResult,
            )
            assert isinstance(children_second, JsonChildrenResult)
            assert children_second.page.complete is True
            assert {
                item.selector for item in (*children_first.children, *children_second.children)
            } == {
                "layers",
                "metrics",
                "ready",
            }

            records_first = _structured_union(
                await client.call_tool(
                    "data_read_json",
                    {
                        "direct_session_alias": session,
                        "query": {
                            "mode": "records",
                            "package_alias": first.package_alias,
                            "payload_name": "analysis_records",
                            "page_size": 2,
                        },
                    },
                ),
                JsonReadResult,
            )
            assert isinstance(records_first, JsonRecordsResult)
            assert records_first.page.complete is False
            assert records_first.page.next_cursor is not None
            records_second = _structured_union(
                await client.call_tool(
                    "data_read_json",
                    {
                        "direct_session_alias": session,
                        "cursor": records_first.page.next_cursor,
                    },
                ),
                JsonReadResult,
            )
            assert isinstance(records_second, JsonRecordsResult)
            assert records_second.page.complete is True
            assert [
                item.record_index for item in (*records_first.records, *records_second.records)
            ] == [
                0,
                1,
                2,
            ]

            with pytest.raises(ToolError, match="PAYLOAD_FORMAT_UNSUPPORTED"):
                await client.call_tool(
                    "data_query_rows",
                    {
                        "direct_session_alias": session,
                        "query": {
                            "package_alias": first.package_alias,
                            "payload_name": "analysis_document",
                            "select": ["metrics"],
                        },
                    },
                )

            second = _structured(
                await client.call_tool(
                    "data_transform",
                    _second_transform(session, first.package_alias),
                    timeout=125,
                ),
                MultiOutputDataTransformResult,
            )
            rows = _structured(
                await client.call_tool(
                    "data_query_rows",
                    {
                        "direct_session_alias": session,
                        "query": {
                            "package_alias": second.package_alias,
                            "payload_name": "round_trip_rows",
                            "select": ["entity_count", "record_count"],
                        },
                    },
                ),
                DataQueryRowsResult,
            )
            assert rows.page.complete is True
            assert rows.rows[0].values["entity_count"] >= 1
            assert rows.rows[0].values["record_count"] == 3

            round_trip = _structured_union(
                await client.call_tool(
                    "data_read_json",
                    {
                        "direct_session_alias": session,
                        "query": {
                            "mode": "value",
                            "package_alias": second.package_alias,
                            "payload_name": "round_trip_document",
                            "pointer": "",
                        },
                    },
                ),
                JsonReadResult,
            )
            assert isinstance(round_trip, JsonValueResult)
            assert json.loads(round_trip.value_json) == {
                "entity_count": rows.rows[0].values["entity_count"],
                "record_count": 3,
            }
    finally:
        await python_worker.close()
        await duckdb_worker.close()
