from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from pydantic import BaseModel
from pydantic import TypeAdapter
from sqlalchemy.engine import make_url

from CAD2BIM_backend.domain.cad_measurement import (
    CAD_MEASUREMENT_PAYLOAD_NAMES,
    CadToleranceAnalysisResult,
)
from CAD2BIM_backend.domain.data_query import DataDescribePackageResult
from CAD2BIM_backend.domain.data_query_sql import DataQuerySqlResult
from CAD2BIM_backend.infrastructure.postgres.runtime_roles import (
    provision_runtime_login_roles,
)
from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.data_runtime_service import DataRuntimeService, DirectScopeResolver
from CAD2BIM_backend.domain.data_runtime import ValidatedSource
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from tests.integration.backend.test_geometry_query_postgres import _runtime
from CAD2BIM_mcp.direct.catalog.contracts import (
    DirectPackageListResult,
    PackageDescriptionResult,
)
from CAD2BIM_mcp.direct.session.contracts import (
    DirectSessionResult,
)
from CAD2BIM_mcp.direct.transform.contracts import (
    MultiOutputDataTransformResult,
)
from CAD2BIM_mcp.direct.operation.contracts import (
    OperationGetResult,
    OperationListResult,
)


pytestmark = [pytest.mark.integration, pytest.mark.network]
TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL")
FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "cad"
MINIMAL_SOURCE = FIXTURE_ROOT / "minimal_line.dxf"
MCP_PASSWORD = "m2b-stdio-mcp-password"
API_PASSWORD = "m2b-stdio-api-password"
RUNNER_PASSWORD = "m2b-stdio-runner-password"
CURSOR_KEY = "public-test-key-" * 4
SESSION_NAME = "M2b real stdio restart capability"
T = TypeVar("T", bound=BaseModel)

DESCRIPTION = """# Minimal stdio products and ports
## Summary
Publishes two deterministic Payloads through a real Direct stdio process.
## Intended use
Verify M2b process transport, durable replay, and process-private query snapshots.
## Contents
The products and ports Parquet Payloads.
## Method
Create one TEMP TABLE and one dependent TEMP VIEW from the exact entity Payload.
## Scope and completeness
Contains the single entity and its deterministic synthetic port key.
## Known limitations
The port is a transport test fact, not an approved engineering decision.
"""

M3_DESCRIPTION = """# Minimal stdio CAD measurements
## Summary
Publishes the fixed ten deterministic M3 geometry-measurement Payloads.
## Intended use
Verify the real Direct stdio process boundary for M3 analysis and publication.
## Contents
The ten server-defined M3 Parquet Payloads.
## Method
Measure the one exact LINE from the imported source_l0 Package in metres.
## Scope and completeness
Contains all geometric facts selected from Layer 0 in the minimal fixture.
## Known limitations
This fixture validates transport and publication, not business topology or IFC decisions.
"""


def _runtime_database_url(value: str) -> str:
    return (
        make_url(value)
        .set(
            drivername="postgresql+psycopg",
            username="cad2bim_mcp_login",
            password=MCP_PASSWORD,
        )
        .render_as_string(hide_password=False)
    )


def _structured(result: Any, contract: type[T]) -> T:
    assert result.is_error is False
    assert result.structured_content is not None
    return contract.model_validate(result.structured_content)


def _structured_union(result: Any, contract: Any) -> Any:
    assert result.is_error is False
    assert result.structured_content is not None
    return TypeAdapter(contract).validate_python(result.structured_content)


def _child_environment(
    *,
    configured_subject: str,
    database_url: str,
    storage_root: Path,
) -> dict[str, str]:
    worker_executable = os.environ.get(
        "CAD2BIM_TEST_DATA_PYTHON_WORKER_EXECUTABLE",
        "",
    ).strip()
    if not worker_executable:
        pytest.skip("CAD2BIM_TEST_DATA_PYTHON_WORKER_EXECUTABLE is not configured")
    environment = dict(os.environ)
    environment.update(
        {
            "CAD2BIM_ENV_FILE": "",
            "CAD2BIM_DIRECT_SUBJECT": configured_subject,
            "CAD2BIM_DIRECT_APPROVED_ROOTS": str(FIXTURE_ROOT),
            "CAD2BIM_DATABASE_URL": database_url,
            "CAD2BIM_STORAGE_ROOT": str(storage_root),
            "CAD2BIM_MAX_UPLOAD_BYTES": str(100 * 1024 * 1024),
            "CAD2BIM_DATA_QUERY_CURSOR_KEY": CURSOR_KEY,
            "CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE": worker_executable,
        }
    )
    return environment


def _transport(environment: dict[str, str], log_file: Path) -> StdioTransport:
    return StdioTransport(
        command=sys.executable,
        args=["-m", "CAD2BIM_mcp.direct.entry"],
        env=environment,
        cwd=str(Path(__file__).resolve().parents[3]),
        keep_alive=False,
        log_file=log_file,
    )


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
        "parameters": [],
        "execution": {
            "engine": "duckdb_sql",
            "code": (
                "CREATE TEMP TABLE products AS "
                "SELECT entity_key AS product_key FROM entities; "
                "CREATE TEMP VIEW ports AS "
                "SELECT product_key || '_p' AS port_key, product_key FROM products;"
            ),
        },
        "outputs": [
            {
                "payload_name": "products",
                "format": "parquet",
                "description": "The exact minimal CAD entity as one product fact.",
                "logical_key_fields": ["product_key"],
            },
            {
                "payload_name": "ports",
                "format": "parquet",
                "description": "One deterministic synthetic port key for the product.",
                "logical_key_fields": ["port_key"],
            },
        ],
        "output_package": {
            "name": "minimal_stdio_products_ports_v1",
            "description_markdown": DESCRIPTION,
        },
    }


async def test_real_stdio_process_recovers_m2_and_executes_m3_measurements(
    tmp_path: Path,
) -> None:
    """Exercise the one current Direct stdio process chain through M2 and M3."""

    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_runtime_login_roles(
        TEST_DATABASE_URL,
        environ={
            "CAD2BIM_API_POSTGRES_PASSWORD": API_PASSWORD,
            "CAD2BIM_RUNNER_POSTGRES_PASSWORD": RUNNER_PASSWORD,
            "CAD2BIM_MCP_POSTGRES_PASSWORD": MCP_PASSWORD,
        },
    )
    configured_subject = f"m2b-stdio-{uuid4().hex}"
    environment = _child_environment(
        configured_subject=configured_subject,
        database_url=_runtime_database_url(TEST_DATABASE_URL),
        storage_root=tmp_path / "storage",
    )
    transform_request: dict[str, Any]
    first_transform: MultiOutputDataTransformResult
    old_cursor: str

    async with Client(_transport(environment, tmp_path / "stdio-first.log")) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert "cad_import_source" not in tools
        assert {"ifc_product_family_list", "ifc_product_family_info"} <= set(tools)
        transform_schema = tools["data_transform"].inputSchema
        assert len(transform_schema["oneOf"]) == 4
        failed_repair, succeeded_repair = transform_schema["oneOf"][2:]
        assert (
            failed_repair["properties"]["source"]["properties"]["source_kind"]["const"]
            == "failed_operation"
        )
        assert (
            succeeded_repair["properties"]["source"]["properties"]["source_kind"]["const"]
            == "succeeded_package"
        )
        assert failed_repair["properties"]["edits"]["maxItems"] == 16
        opened = _structured(
            await client.call_tool(
                "runtime_open_direct_session",
                {"session_name": SESSION_NAME},
            ),
            DirectSessionResult,
        )
        assert opened.resumed is False
        # Simulate the backend processing a user upload; no Agent import tool.
        preparation = _runtime(_runtime_database_url(TEST_DATABASE_URL))
        try:
            resolver = DirectScopeResolver(preparation.repository, preparation.identity, configured_subject)
            upload_scope = await resolver.resolve(opened.direct_session_alias, command_id="upload-fixture")
            runtime = DataRuntimeService(preparation.repository, ArtifactService(
                preparation.repository, LocalStorage(tmp_path / "storage")))
            with bind_tenant_execution_context(upload_scope.identity):
                imported = await runtime.import_cad_source(upload_scope, ValidatedSource(
                    filename=MINIMAL_SOURCE.name, media_type="application/dxf", content=MINIMAL_SOURCE.read_bytes(),
                    source_identity=str(MINIMAL_SOURCE), adapter_metadata={}))
        finally:
            await preparation.close()
        first_page = _structured(
            await client.call_tool(
                "data_query_sql",
                {
                    "mode": "fresh",
                    "direct_session_alias": opened.direct_session_alias,
                    "query": {
                        "inputs": [
                            {
                                "input_name": "entities",
                                "package_alias": imported.published.package.catalog_entry.alias,
                                "payload_name": "entities",
                            }
                        ],
                        "parameters": [],
                        "execution": {
                            "engine": "duckdb_sql",
                            "code": (
                                "SELECT entity_key, copy_ordinal "
                                "FROM entities CROSS JOIN range(0, 5) AS copies(copy_ordinal) "
                                "ORDER BY copy_ordinal"
                            ),
                        },
                        "page_size": 2,
                    },
                },
                timeout=65,
            ),
            DataQuerySqlResult,
        )
        assert first_page.page.total_matched_count == 5
        assert first_page.page.returned_count == 2
        assert first_page.page.next_cursor is not None
        old_cursor = first_page.page.next_cursor

        transform_request = _transform_request(
            opened.direct_session_alias,
            imported.published.package.catalog_entry.alias,
        )
        first_transform = _structured(
            await client.call_tool("data_transform", transform_request, timeout=65),
            MultiOutputDataTransformResult,
        )
        assert [item.payload_name for item in first_transform.outputs] == [
            "products",
            "ports",
        ]
        assert [item.row_count for item in first_transform.outputs] == [1, 1]

    async with Client(_transport(environment, tmp_path / "stdio-second.log")) as client:
        resumed = _structured(
            await client.call_tool(
                "runtime_open_direct_session",
                {"session_name": SESSION_NAME},
            ),
            DirectSessionResult,
        )
        assert resumed.resumed is True
        assert resumed.direct_session_alias == opened.direct_session_alias
        catalog = _structured(
            await client.call_tool(
                "data_package_list",
                {"direct_session_alias": resumed.direct_session_alias},
            ),
            DirectPackageListResult,
        )
        assert [item.package_kind for item in catalog.packages] == [
            "derived",
            "source_l0",
        ]
        replayed = _structured(
            await client.call_tool("data_transform", transform_request, timeout=65),
            MultiOutputDataTransformResult,
        )
        assert replayed == first_transform
        with pytest.raises(ToolError, match="QUERY_CURSOR_EXPIRED"):
            await client.call_tool(
                "data_query_sql",
                {
                    "mode": "cursor",
                    "direct_session_alias": resumed.direct_session_alias,
                    "cursor": old_cursor,
                },
            )

        operations_before_analysis = _structured(
            await client.call_tool(
                "operation_list",
                {"direct_session_alias": resumed.direct_session_alias},
            ),
            OperationListResult,
        )
        analysis = _structured(
            await client.call_tool(
                "cad_analyze_tolerance_candidates",
                {
                    "direct_session_alias": resumed.direct_session_alias,
                    "package_alias": imported.published.package.catalog_entry.alias,
                    "source_unit_to_m": 0.001,
                    "path_layers": ["0"],
                    "point_groups": [],
                    "trials": [],
                },
                timeout=45,
            ),
            CadToleranceAnalysisResult,
        )
        assert analysis.package_alias == imported.published.package.catalog_entry.alias
        assert analysis.coverage.atomic_edge_count == 1
        assert analysis.coverage.endpoint_count == 2
        assert (
            _structured(
                await client.call_tool(
                    "data_package_list",
                    {"direct_session_alias": resumed.direct_session_alias},
                ),
                DirectPackageListResult,
            )
            == catalog
        )
        assert (
            _structured(
                await client.call_tool(
                    "operation_list",
                    {"direct_session_alias": resumed.direct_session_alias},
                ),
                OperationListResult,
            )
            == operations_before_analysis
        )

        computed = _structured(
            await client.call_tool(
                "cad_compute_measurements",
                {
                    "direct_session_alias": resumed.direct_session_alias,
                    "package_alias": imported.published.package.catalog_entry.alias,
                    "source_unit_to_m": 0.001,
                    "path_layers": ["0"],
                    "point_scopes": [],
                    "tolerance_spec": {
                        "endpoint_quantization_m": 0.0001,
                        "endpoint_snap_candidate_m": 0.01,
                        "endpoint_on_path_candidate_m": 0.01,
                        "host_endpoint_guard_m": 0.01,
                        "point_target_uniqueness_margin_m": 0.005,
                        "straight_turn_tolerance_deg": 0.5,
                    },
                    "output_package": {
                        "name": "minimal_stdio_cad_measurements_v1",
                        "description_markdown": M3_DESCRIPTION,
                    },
                },
                timeout=45,
            ),
            MultiOutputDataTransformResult,
        )
        assert tuple(item.payload_name for item in computed.outputs) == (
            CAD_MEASUREMENT_PAYLOAD_NAMES
        )

        catalog_after_compute = _structured(
            await client.call_tool(
                "data_package_list",
                {"direct_session_alias": resumed.direct_session_alias},
            ),
            DirectPackageListResult,
        )
        assert [item.package_kind for item in catalog_after_compute.packages] == [
            "derived",
            "derived",
            "source_l0",
        ]
        measurement_card = catalog_after_compute.packages[0]
        assert measurement_card.package_alias == computed.package_alias
        assert measurement_card.producing_operation_alias == computed.operation_alias
        assert len(measurement_card.payloads) == 10
        assert {item.payload_name for item in measurement_card.payloads} == set(
            CAD_MEASUREMENT_PAYLOAD_NAMES
        )

        described = _structured(
            await client.call_tool(
                "data_describe_package",
                {
                    "direct_session_alias": resumed.direct_session_alias,
                    "package_alias": computed.package_alias,
                },
            ),
            DataDescribePackageResult,
        )
        assert described.package.package_kind == "derived"
        assert len(described.payloads) == 10
        assert {item.payload_name for item in described.payloads} == set(
            CAD_MEASUREMENT_PAYLOAD_NAMES
        )
        description = _structured(
            await client.call_tool(
                "data_get_package_description",
                {
                    "direct_session_alias": resumed.direct_session_alias,
                    "package_alias": computed.package_alias,
                },
            ),
            PackageDescriptionResult,
        )
        assert description.markdown == M3_DESCRIPTION

        operations_after_compute = _structured(
            await client.call_tool(
                "operation_list",
                {"direct_session_alias": resumed.direct_session_alias},
            ),
            OperationListResult,
        )
        assert [item.tool_name for item in operations_after_compute.operations] == [
            "cad_compute_measurements",
            "data_transform",
            "cad_import_source",
        ]
        operation = _structured_union(
            await client.call_tool(
                "operation_get",
                {
                    "direct_session_alias": resumed.direct_session_alias,
                    "operation_alias": computed.operation_alias,
                },
            ),
            OperationGetResult,
        )
        assert operation.operation.outcome == "succeeded"
        assert operation.operation.primary_result.package_alias == computed.package_alias
        assert operation.detail.detail_kind == "cad_measurement"
        assert operation.detail.code_available is True
        assert tuple(item.payload_name for item in operation.detail.outputs) == (
            CAD_MEASUREMENT_PAYLOAD_NAMES
        )
