from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, TypeVar

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, TypeAdapter

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.code_discovery_service import CodeDiscoveryService
from CAD2BIM_backend.application.data_query_service import DataQueryService
from CAD2BIM_backend.application.data_runtime_service import (
    DataRuntimeService,
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.application.data_transform_service import DataTransformService
from CAD2BIM_backend.application.exact_payload_resolver import ExactPayloadResolver
from CAD2BIM_backend.domain.data_query import DataQueryRowsResult
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.data_transform import (
    SubprocessDuckDBDataTransformWorker,
)
from CAD2BIM_backend.infrastructure.memory.control_plane_repository import (
    InMemoryControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.memory.identity_directory import InMemoryIdentityDirectory
from CAD2BIM_backend.infrastructure.memory.state import MemoryState
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_backend.domain.data_runtime import ValidatedSource
from CAD2BIM_mcp.direct.transform.contracts import DataTransformResult
from CAD2BIM_mcp.direct.code.contracts import DirectCodeListResult, DirectCodeReadResult
from CAD2BIM_mcp.direct.catalog.contracts import (
    DirectDataDescribePackageResult,
    DirectPackageListResult,
)
from CAD2BIM_mcp.direct.session.contracts import (
    DirectSessionResult,
)
from CAD2BIM_mcp.direct.operation.contracts import (
    OperationGetResult,
    OperationListResult,
)
from CAD2BIM_mcp.direct.server import create_direct_mcp


pytestmark = pytest.mark.integration
FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "cad"
SOURCE = FIXTURE_ROOT / "minimal_line.dxf"
T = TypeVar("T", bound=BaseModel)

DESCRIPTION = """# Minimal line selection
## Summary
Publishes the layer-zero entity rows selected from one exact L0 Package.
## Intended use
Exercise the M2a immutable Data Transform path.
## Contents
One selected_entities Parquet Payload.
## Method
Run one parameterized DuckDB SELECT over the declared entities input.
## Scope and completeness
Contains every matching row from this exact source Payload.
## Known limitations
The output contains CAD facts and is not an approved IFC decision.
"""


def _cursor_key() -> str:
    return base64.urlsafe_b64encode(b"m2a-direct-transform-test-key-0001").decode().rstrip("=")


def _structured(result: Any, contract: type[T]) -> T:
    assert result.is_error is False
    assert result.structured_content is not None
    return contract.model_validate(result.structured_content)


def _structured_union(result: Any, contract: Any) -> Any:
    assert result.is_error is False
    assert result.structured_content is not None
    return TypeAdapter(contract).validate_python(result.structured_content)


def _transform_request(session_alias: str, package_alias: str) -> dict[str, Any]:
    return {
        "mode": "create",
        "direct_session_alias": session_alias,
        "inputs": [
            {
                "input_name": "entities",
                "package_alias": package_alias,
                "payload_name": "entities",
            }
        ],
        "parameters": [{"name": "target_layer", "kind": "string", "value": "0"}],
        "execution": {
            "engine": "duckdb_sql",
            "code": (
                "SELECT entity_key, layer, entity_type FROM entities WHERE layer = $target_layer"
            ),
        },
        "outputs": {"action": "set", "value": [
            {
                "payload_name": "selected_entities",
                "format": "parquet",
                "description": "Layer-zero entity facts selected from the exact L0 input.",
                "logical_key_fields": ["entity_key"],
            }
        ]},
        "output_package": {
            "name": "minimal_line_selection_v1",
            "description_markdown": {"action": "set", "value": DESCRIPTION},
        },
    }


async def test_m2a_real_worker_closes_the_direct_mcp_transform_loop(tmp_path: Path) -> None:
    repository = InMemoryControlPlaneRepository(MemoryState())
    identities = InMemoryIdentityDirectory()
    artifacts = ArtifactService(repository, LocalStorage(tmp_path / "storage"))
    runtime = DataRuntimeService(repository, artifacts)
    sessions = DirectSessionService(repository, identities, "m2a-direct-test-user")
    resolver = DirectScopeResolver(repository, identities, "m2a-direct-test-user")
    queries = DataQueryService(repository, artifacts, CursorCodec(_cursor_key()))
    worker = SubprocessDuckDBDataTransformWorker()
    transform = DataTransformService(
        repository,
        artifacts,
        ExactPayloadResolver(repository, artifacts),
        worker,
    )
    code_discovery = CodeDiscoveryService(
        runtime,
        None,
        None,
        None,
        None,
        None,
        None,
        CursorCodec(_cursor_key()),
    )
    server = create_direct_mcp(
        DirectApplicationFacade(
            runtime,
            sessions,
            resolver,
            query_service=queries,
            transform_service=transform,
            code_discovery_service=code_discovery,
        ),
    )

    try:
        async with Client(server) as client:
            opened = _structured(
                await client.call_tool(
                    "runtime_open_direct_session",
                    {"session_name": "M2a Direct transform capability"},
                ),
                DirectSessionResult,
            )
            # Backend preparation of a user upload; the Agent only consumes its Package.
            upload_scope = await resolver.resolve(opened.direct_session_alias, command_id="upload-fixture-imported")
            imported = await runtime.import_cad_source(upload_scope, ValidatedSource(
                filename=SOURCE.name, media_type="application/dxf", content=SOURCE.read_bytes(),
                source_identity=str(SOURCE), adapter_metadata={}))
            request = _transform_request(
                opened.direct_session_alias,
                imported.published.package.catalog_entry.alias,
            )

            transformed = _structured(
                await client.call_tool("data_transform", request, timeout=65),
                DataTransformResult,
            )
            replayed = _structured(
                await client.call_tool("data_transform", request, timeout=65),
                DataTransformResult,
            )
            assert replayed == transformed
            assert transformed.output.payload_name == "selected_entities"
            assert transformed.output.row_count == 1
            assert transformed.output.logical_key_fields == ("entity_key",)

            catalog = _structured(
                await client.call_tool(
                    "data_package_list",
                    {"direct_session_alias": opened.direct_session_alias},
                ),
                DirectPackageListResult,
            )
            assert [item.package_kind for item in catalog.packages] == [
                "derived",
                "source_l0",
            ]
            assert catalog.packages[0].package_alias == transformed.package_alias
            assert catalog.packages[0].producing_operation_alias == transformed.operation_alias

            operations = _structured(
                await client.call_tool(
                    "operation_list",
                    {"direct_session_alias": opened.direct_session_alias},
                ),
                OperationListResult,
            )
            assert [item.operation_kind for item in operations.operations] == [
                "data_transform",
                "source_ingestion",
            ]
            assert operations.operations[0].operation_alias == transformed.operation_alias

            operation = _structured_union(
                await client.call_tool(
                    "operation_get",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "operation_alias": transformed.operation_alias,
                    },
                ),
                OperationGetResult,
            )
            assert operation.operation.outcome == "succeeded"
            assert operation.detail.inputs[0].package_alias == imported.published.package.catalog_entry.alias

            code_page = _structured(
                await client.call_tool(
                    "code_list",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "mode": "first",
                        "query": {
                            "code_kind": "data_transform_attempt",
                            "page_size": 50,
                        },
                    },
                ),
                DirectCodeListResult,
            )
            code_card = next(
                item
                for item in code_page.codes
                if item.owner_alias == transformed.operation_alias
            )
            assert code_card.result_alias == transformed.package_alias
            assert code_card.result_code_locator is not None
            by_package = _structured(
                await client.call_tool(
                    "code_read",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "source_form": "single_text",
                        "code_locator": code_card.result_code_locator,
                        "view": {"view_mode": "full"},
                    },
                ),
                DirectCodeReadResult,
            )
            by_operation = _structured(
                await client.call_tool(
                    "code_read",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "source_form": "single_text",
                        "code_locator": code_card.code_locator,
                        "view": {"view_mode": "full"},
                    },
                ),
                DirectCodeReadResult,
            )
            assert by_package.text == by_operation.text == request["execution"]["code"]

            described = _structured(
                await client.call_tool(
                    "data_describe_package",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "package_alias": transformed.package_alias,
                    },
                ),
                DirectDataDescribePackageResult,
            )
            assert [item.payload_name for item in described.payloads] == ["selected_entities"]
            assert described.payloads[0].logical_key_fields == ("entity_key",)

            rows = _structured(
                await client.call_tool(
                    "data_query_rows",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "query": {
                            "package_alias": transformed.package_alias,
                            "payload_name": "selected_entities",
                            "select": ["layer", "entity_type"],
                        },
                    },
                ),
                DataQueryRowsResult,
            )
            assert rows.page.complete is True
            assert rows.rows[0].values == {"layer": "0", "entity_type": "LINE"}

            with pytest.raises(ToolError, match="PACKAGE_KIND_MISMATCH"):
                await client.call_tool(
                    "cad_query_entities",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "query": {
                            "package_alias": transformed.package_alias,
                            "mode": "rows",
                        },
                    },
                )

            invalid = _transform_request(
                opened.direct_session_alias,
                imported.published.package.catalog_entry.alias,
            )
            invalid["execution"] = {
                "engine": "duckdb_sql",
                "code": "SELECT * FROM duckdb_tables()",
            }
            invalid["parameters"] = []
            with pytest.raises(ToolError, match="TRANSFORM_SQL_NOT_ALLOWED"):
                await client.call_tool("data_transform", invalid, timeout=65)
            after_invalid = _structured(
                await client.call_tool(
                    "operation_list",
                    {"direct_session_alias": opened.direct_session_alias},
                ),
                OperationListResult,
            )
            assert len(after_invalid.operations) == 3
            assert after_invalid.operations[0].outcome == "failed"
            assert after_invalid.operations[0].error_code == "TRANSFORM_SQL_NOT_ALLOWED"
    finally:
        await worker.close()
