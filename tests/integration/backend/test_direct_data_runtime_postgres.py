from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
from uuid import uuid4

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from CAD2BIM_mcp.direct.bootstrap import create_direct_runtime_resources
from CAD2BIM_backend.domain.data_query import (
    CadEntityCountResult,
    CadEntityRowsResult,
    CadGetEntityResult,
    CadQueryEntitiesResult,
    CadQueryXdataResult,
    CadXdataProfileResult,
    DataQueryRowsResult,
)
from CAD2BIM_backend.infrastructure.postgres.runtime_roles import (
    provision_runtime_login_roles,
)
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.config import DirectMcpConfig
from CAD2BIM_backend.domain.data_runtime import ValidatedSource
from CAD2BIM_mcp.direct.transform.contracts import DataRepairTransformResult, DataTransformResult
from CAD2BIM_mcp.direct.code.contracts import DirectCodeListResult, DirectCodeReadResult
from CAD2BIM_mcp.direct.catalog.contracts import (
    DirectDataDescribePackageResult,
    DirectPackageListResult,
    PackageDescriptionResult,
)
from CAD2BIM_mcp.direct.session.contracts import (
    DirectSessionResult,
)
from CAD2BIM_mcp.direct.operation.contracts import (
    OperationGetResult,
    OperationListResult,
)
from CAD2BIM_mcp.direct.server import create_direct_mcp
from CAD2BIM_mcp.direct.transform import tools as transform_tools


pytestmark = [pytest.mark.integration, pytest.mark.network]
TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL")
FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "cad"
GAS_SOURCE = FIXTURE_ROOT / "sample-network.dxf"
MINIMAL_SOURCE = FIXTURE_ROOT / "minimal_line.dxf"
API_PASSWORD = "phase1-api-password"
RUNNER_PASSWORD = "phase1-runner-password"
MCP_PASSWORD = "phase1-mcp-password"
CURSOR_KEY = "public-test-key-" * 4
def _python_worker_executable() -> str:
    value = os.environ.get("CAD2BIM_TEST_DATA_PYTHON_WORKER_EXECUTABLE", "").strip()
    if not value:
        pytest.skip("CAD2BIM_TEST_DATA_PYTHON_WORKER_EXECUTABLE is not configured")
    return value


M2A_DESCRIPTION = """# PostgreSQL M2a selection
## Summary
Publishes selected entity facts through the durable PostgreSQL Data Runtime.
## Intended use
Verify M2a publication and restart recovery.
## Contents
One selected_entities Parquet Payload.
## Method
Run one parameterized DuckDB SELECT over an exact L0 Payload.
## Scope and completeness
Contains all rows matching the declared layer parameter.
## Known limitations
The rows are CAD facts, not approved IFC decisions.
"""

REPAIR_DESCRIPTION = """# PostgreSQL exact repair projection
## Summary
Publishes the exact minimal entities Payload after one localized Python source repair.
## Intended use
Verify durable failed-source inspection, repair ancestry and restart recovery.
## Contents
One selected_entities Parquet Payload.
## Method
Use the controlled Python and Arrow worker over one exact source_l0 input.
## Scope and completeness
Contains every row from the selected exact entities Payload.
## Known limitations
This is a persistence fixture, not an IFC modeling decision.
"""


def _runtime_database_url(value: str, *, username: str, password: str) -> str:
    return (
        make_url(value)
        .set(
            drivername="postgresql+psycopg",
            username=username,
            password=password,
        )
        .render_as_string(hide_password=False)
    )


def _structured(result, contract):
    assert result.is_error is False
    assert result.structured_content is not None
    return contract.model_validate(result.structured_content)


def _structured_union(result, contract):
    assert result.is_error is False
    assert result.structured_content is not None
    payload = result.structured_content
    assert "result" not in payload
    return TypeAdapter(contract).validate_python(payload)


async def _expect_tool_error(client: Client, name: str, arguments: dict, code: str) -> None:
    with pytest.raises(ToolError, match=code):
        await client.call_tool(name, arguments)


async def _published_state(configured_subject: str) -> tuple[int, int, int, int, int, int]:
    """Return the query-visible publication state for one Direct identity."""

    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT count(DISTINCT dp.package_id) AS package_count, "
                            "count(DISTINCT co.operation_id) AS operation_count, "
                            "count(DISTINCT dce.catalog_entry_id) AS catalog_entry_count, "
                            "coalesce(max(dce.catalog_revision), 0) AS catalog_revision, "
                            "count(DISTINCT a.artifact_id) AS artifact_count, "
                            "count(DISTINCT av.version_id) AS artifact_version_count "
                            "FROM direct_session_bindings AS dsb "
                            "LEFT JOIN data_packages AS dp ON dp.task_id = dsb.task_id "
                            "LEFT JOIN critical_operations AS co ON co.task_id = dsb.task_id "
                            "LEFT JOIN data_catalog_entries AS dce ON dce.run_id = dsb.run_id "
                            "LEFT JOIN artifacts AS a ON a.task_id = dsb.task_id "
                            "LEFT JOIN artifact_versions AS av ON av.task_id = dsb.task_id "
                            "AND av.artifact_id = a.artifact_id "
                            "WHERE dsb.configured_subject = :configured_subject"
                        ),
                        {"configured_subject": configured_subject},
                    )
                )
                .mappings()
                .one()
            )
    finally:
        await engine.dispose()
    return (
        row["package_count"],
        row["operation_count"],
        row["catalog_entry_count"],
        row["catalog_revision"],
        row["artifact_count"],
        row["artifact_version_count"],
    )


async def test_real_gas_dxf_supports_current_direct_tools_without_query_side_effects(
    tmp_path: Path,
) -> None:
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
    configured_subject = f"phase1-gas-{uuid4().hex}"
    config = DirectMcpConfig.from_environ(
        {
            "CAD2BIM_DIRECT_SUBJECT": configured_subject,
            "CAD2BIM_DIRECT_APPROVED_ROOTS": str(FIXTURE_ROOT),
            "CAD2BIM_DATABASE_URL": _runtime_database_url(
                TEST_DATABASE_URL,
                username="cad2bim_mcp_login",
                password=MCP_PASSWORD,
            ),
            "CAD2BIM_STORAGE_ROOT": str(tmp_path / "storage"),
            "CAD2BIM_MAX_UPLOAD_BYTES": str(100 * 1024 * 1024),
            "CAD2BIM_DATA_QUERY_CURSOR_KEY": CURSOR_KEY,
            "CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE": _python_worker_executable(),
        }
    )
    resources = create_direct_runtime_resources(config)
    try:
        await resources.verify_ready()
        server = create_direct_mcp(
            DirectApplicationFacade(
                resources.application,
                resources.sessions,
                resources.scope_resolver,
                resources.scope_binder,
                query_service=resources.query_service,
                query_sql_service=resources.query_sql_service,
                transform_service=resources.transform_service,
                cad_measurement_service=resources.cad_measurement_service,
            ),
        )
        async with Client(server) as client:
            tools = {tool.name for tool in await client.list_tools()}
            assert tools == {
                "runtime_open_direct_session",
                "ifc_model_initialize",
                "ifc_model_info",
                "ifc_authoring_publish",
                "data_package_list",
                "data_get_package_description",
                "data_describe_package",
                "data_query_rows",
                "cad_query_entities",
                "cad_get_entity",
                "cad_query_xdata",
                "operation_list",
                "operation_get",
                "data_transform",
                "data_query_sql",
                "data_query_python",
                "code_list",
                "code_read",
                "data_read_json",
                "ifc_search_official_docs",
                "ifc_read_official_doc",
                "cad_analyze_tolerance_candidates",
                "cad_compute_measurements",
                "ifc_model_configure_geometry_basis",
                "ifc_representation_context_list",
                "ifc_project_update",
                "ifc_spatial_structure_list",
                "ifc_spatial_structure_apply",
                "ifc_geometry_resource_list",
                "ifc_geometry_resource_info",
                "ifc_geometry_resource_source_read",
                "ifc_geometry_preview",
                "ifc_geometry_query_python",
                "ifc_geometry_resource_write",
                "ifc_product_family_list",
                "ifc_product_family_info",
                "ifc_product_family_write",
                "ifc_product_realization_preview",
                "ifc_query_python",
            }
            assert len(tools) == 39
            opened = _structured(
                await client.call_tool(
                    "runtime_open_direct_session",
                    {"session_name": "真实燃气全图第一阶段"},
                ),
                DirectSessionResult,
            )
            resumed = _structured(
                await client.call_tool(
                    "runtime_open_direct_session",
                    {"session_name": "真实燃气全图第一阶段"},
                ),
                DirectSessionResult,
            )
            parallel_a, parallel_b = await asyncio.gather(
                resources.sessions.open_direct_session("并发Direct会话A"),
                resources.sessions.open_direct_session("并发Direct会话B"),
            )
            # Backend preparation of a user upload; the Agent only consumes its Package.
            upload_scope = await resources.scope_resolver.resolve(opened.direct_session_alias, command_id="upload-fixture")
            with resources.scope_binder(upload_scope.identity):
                imported = await resources.application.import_cad_source(upload_scope, ValidatedSource(
                    filename=GAS_SOURCE.name, media_type="application/dxf", content=GAS_SOURCE.read_bytes(),
                    source_identity=str(GAS_SOURCE), adapter_metadata={}))

            catalog = _structured(
                await client.call_tool(
                    "data_package_list",
                    {"direct_session_alias": opened.direct_session_alias},
                ),
                DirectPackageListResult,
            )
            operation_catalog = _structured(
                await client.call_tool(
                    "operation_list",
                    {"direct_session_alias": opened.direct_session_alias},
                ),
                OperationListResult,
            )
            description = _structured(
                await client.call_tool(
                    "data_get_package_description",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "package_alias": imported.published.package.catalog_entry.alias,
                    },
                ),
                PackageDescriptionResult,
            )

            publication_state_before_queries = await _published_state(configured_subject)

            described = _structured(
                await client.call_tool(
                    "data_describe_package",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "package_alias": imported.published.package.catalog_entry.alias,
                    },
                ),
                DirectDataDescribePackageResult,
            )
            payload_descriptions: dict[str, DirectDataDescribePackageResult] = {}
            for payload_name in (
                "records",
                "raw_tags",
                "entities",
                "geometry_elements",
                "xdata_tags",
                "relationships",
            ):
                payload_descriptions[payload_name] = _structured(
                    await client.call_tool(
                        "data_describe_package",
                        {
                            "direct_session_alias": opened.direct_session_alias,
                            "package_alias": imported.published.package.catalog_entry.alias,
                            "payload_name": payload_name,
                        },
                    ),
                    DirectDataDescribePackageResult,
                )

            layer_counts = _structured_union(
                await client.call_tool(
                    "cad_query_entities",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "query": {
                            "package_alias": imported.published.package.catalog_entry.alias,
                            "mode": "count_by",
                            "count_by": "layer",
                            "page_size": 100,
                        },
                    },
                    timeout=30,
                ),
                CadQueryEntitiesResult,
            )
            assert isinstance(layer_counts, CadEntityCountResult)

            entity_rows = _structured_union(
                await client.call_tool(
                    "cad_query_entities",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "query": {
                            "package_alias": imported.published.package.catalog_entry.alias,
                            "mode": "rows",
                            "filters": {"layers": ["阀门点"]},
                            "page_size": 10,
                        },
                    },
                    timeout=30,
                ),
                CadQueryEntitiesResult,
            )
            assert isinstance(entity_rows, CadEntityRowsResult)
            selected_entity_key = entity_rows.rows[0].entity_key

            entity_sections: dict[str, CadGetEntityResult] = {}
            for section in (
                "geometry_elements",
                "xdata_tags",
                "relationships",
                "raw_tags",
            ):
                entity_sections[section] = _structured(
                    await client.call_tool(
                        "cad_get_entity",
                        {
                            "direct_session_alias": opened.direct_session_alias,
                            "query": {
                                "package_alias": imported.published.package.catalog_entry.alias,
                                "entity_key": selected_entity_key,
                                "section": section,
                                "page_size": 200,
                            },
                        },
                        timeout=30,
                    ),
                    CadGetEntityResult,
                )

            appid_profile = _structured_union(
                await client.call_tool(
                    "cad_query_xdata",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "query": {
                            "package_alias": imported.published.package.catalog_entry.alias,
                            "mode": "profile",
                            "profile_dimension": "appid",
                            "page_size": 100,
                        },
                    },
                    timeout=30,
                ),
                CadQueryXdataResult,
            )
            group_code_profile = _structured_union(
                await client.call_tool(
                    "cad_query_xdata",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "query": {
                            "package_alias": imported.published.package.catalog_entry.alias,
                            "mode": "profile",
                            "profile_dimension": "group_code",
                            "page_size": 100,
                        },
                    },
                    timeout=30,
                ),
                CadQueryXdataResult,
            )
            assignment_profile = _structured_union(
                await client.call_tool(
                    "cad_query_xdata",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "query": {
                            "package_alias": imported.published.package.catalog_entry.alias,
                            "mode": "profile",
                            "profile_dimension": "assignment_key",
                            "page_size": 100,
                        },
                    },
                    timeout=30,
                ),
                CadQueryXdataResult,
            )

            first_page = _structured(
                await client.call_tool(
                    "data_query_rows",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "query": {
                            "package_alias": imported.published.package.catalog_entry.alias,
                            "payload_name": "entities",
                            "select": ["layer", "entity_type"],
                            "filters": [{"field": "layer", "op": "eq", "value": "阀门点"}],
                            "page_size": 10,
                        },
                    },
                    timeout=30,
                ),
                DataQueryRowsResult,
            )
            first_cursor = first_page.page.next_cursor
            assert first_cursor is not None
            all_entity_keys = [row.key_values["entity_key"] for row in first_page.rows]
            cursor = first_cursor
            second_page_keys: list[str] | None = None
            while cursor is not None:
                page = _structured(
                    await client.call_tool(
                        "data_query_rows",
                        {
                            "direct_session_alias": opened.direct_session_alias,
                            "cursor": cursor,
                        },
                        timeout=30,
                    ),
                    DataQueryRowsResult,
                )
                page_keys = [row.key_values["entity_key"] for row in page.rows]
                if second_page_keys is None:
                    second_page_keys = page_keys
                all_entity_keys.extend(page_keys)
                cursor = page.page.next_cursor

            await _expect_tool_error(
                client,
                "data_query_rows",
                {
                    "direct_session_alias": opened.direct_session_alias,
                    "query": {
                        "package_alias": imported.published.package.catalog_entry.alias,
                        "payload_name": "entities",
                        "select": ["not_a_real_field"],
                    },
                },
                "FIELD_NOT_FOUND",
            )
            # The shared Provider middleware rejects both incomplete XOR forms
            # before dispatch and preserves the closed schema diagnostic.
            with pytest.raises(ToolError, match="INVALID_ARGUMENT.*error_type=union_validation"):
                await client.call_tool(
                    "data_query_rows",
                    {"direct_session_alias": opened.direct_session_alias},
                )
            with pytest.raises(ToolError, match="INVALID_ARGUMENT.*error_type=union_validation"):
                await client.call_tool(
                    "data_query_rows",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "query": {
                            "package_alias": imported.published.package.catalog_entry.alias,
                            "payload_name": "entities",
                            "select": [],
                        },
                        "cursor": first_cursor,
                    },
                )
            tampered_cursor = first_cursor[:-1] + ("A" if first_cursor[-1] != "A" else "B")
            await _expect_tool_error(
                client,
                "data_query_rows",
                {
                    "direct_session_alias": opened.direct_session_alias,
                    "cursor": tampered_cursor,
                },
                "CURSOR_INVALID",
            )
            await _expect_tool_error(
                client,
                "cad_query_entities",
                {
                    "direct_session_alias": opened.direct_session_alias,
                    "cursor": first_cursor,
                },
                "CURSOR_TOOL_MISMATCH",
            )
            await _expect_tool_error(
                client,
                "data_describe_package",
                {
                    "direct_session_alias": parallel_a.binding.direct_session_alias,
                    "package_alias": imported.published.package.catalog_entry.alias,
                },
                "PACKAGE_NOT_FOUND",
            )
            await _expect_tool_error(
                client,
                "data_query_rows",
                {
                    "direct_session_alias": parallel_a.binding.direct_session_alias,
                    "cursor": first_cursor,
                },
                "CURSOR_SCOPE_MISMATCH",
            )

            publication_state_after_queries = await _published_state(configured_subject)

        assert tuple(
            (item.payload_name, item.logical_key_fields) for item in described.payloads
        ) == (
            ("records", ("record_key",)),
            ("raw_tags", ("raw_tag_key",)),
            ("entities", ("entity_key",)),
            ("geometry_elements", ("geometry_element_key",)),
            ("xdata_tags", ("xdata_tag_key",)),
            ("relationships", ("relationship_key",)),
        )
        assert {
            name: result.payload_schema.logical_key_fields
            for name, result in payload_descriptions.items()
            if result.payload_schema is not None
        } == {
            "records": ("record_key",),
            "raw_tags": ("raw_tag_key",),
            "entities": ("entity_key",),
            "geometry_elements": ("geometry_element_key",),
            "xdata_tags": ("xdata_tag_key",),
            "relationships": ("relationship_key",),
        }
        assert layer_counts.scope.matched_entity_count == 18_848
        assert layer_counts.scope.declared_layer_count == 19
        assert layer_counts.scope.used_layer_count == 18
        assert len(layer_counts.counts) == 19
        assert all(
            result.entity.entity_key == selected_entity_key for result in entity_sections.values()
        )
        assert all(result.section.value == section for section, result in entity_sections.items())
        assert isinstance(appid_profile, CadXdataProfileResult)
        assert len(appid_profile.profiles) == 17
        assert (
            sum(
                item.declared_in_appid_table and item.payload_tag_count > 0
                for item in appid_profile.profiles
            )
            == 12
        )
        assert sum(item.marker_count for item in appid_profile.profiles) == 144_184
        assert sum(item.payload_tag_count for item in appid_profile.profiles) == 398_940
        assert appid_profile.scope.raw_non_marker_tag_count == 398_940
        assert isinstance(group_code_profile, CadXdataProfileResult)
        assert sum(item.tag_count for item in group_code_profile.profiles) == 543_124
        marker_bucket = next(
            item for item in group_code_profile.profiles if item.group_code == 1001
        )
        assert marker_bucket.tag_count == 144_184
        assert isinstance(assignment_profile, CadXdataProfileResult)
        assert len(assignment_profile.profiles) == 43
        assert first_page.page.total_matched_count == 62
        assert len(all_entity_keys) == 62
        assert len(set(all_entity_keys)) == 62
        assert second_page_keys is not None
        assert (
            publication_state_before_queries
            == publication_state_after_queries
            == (
                1,
                1,
                2,
                1,
                9,
                9,
            )
        )
    finally:
        await resources.close()

    restarted_resources = create_direct_runtime_resources(config)
    try:
        await restarted_resources.verify_ready()
        restarted_server = create_direct_mcp(
            DirectApplicationFacade(
                restarted_resources.application,
                restarted_resources.sessions,
                restarted_resources.scope_resolver,
                restarted_resources.scope_binder,
                query_service=restarted_resources.query_service,
                transform_service=restarted_resources.transform_service,
                code_discovery_service=restarted_resources.code_discovery_service,
            ),
        )
        async with Client(restarted_server) as client:
            resumed_page = _structured(
                await client.call_tool(
                    "data_query_rows",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "cursor": first_cursor,
                    },
                    timeout=30,
                ),
                DataQueryRowsResult,
            )
            assert [row.key_values["entity_key"] for row in resumed_page.rows] == second_page_keys

            isolated_subject = f"{configured_subject}-isolated"
            isolated_config = DirectMcpConfig.from_environ(
                {
                    "CAD2BIM_DIRECT_SUBJECT": isolated_subject,
                    "CAD2BIM_DIRECT_APPROVED_ROOTS": str(FIXTURE_ROOT),
                    "CAD2BIM_DATABASE_URL": config.database_url,
                    "CAD2BIM_STORAGE_ROOT": str(tmp_path / "isolated-storage"),
                    "CAD2BIM_MAX_UPLOAD_BYTES": str(100 * 1024 * 1024),
                    "CAD2BIM_DATA_QUERY_CURSOR_KEY": CURSOR_KEY,
                    "CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE": _python_worker_executable(),
                }
            )
            isolated_resources = create_direct_runtime_resources(isolated_config)
            try:
                await isolated_resources.verify_ready()
                isolated_server = create_direct_mcp(
                    DirectApplicationFacade(
                        isolated_resources.application,
                        isolated_resources.sessions,
                        isolated_resources.scope_resolver,
                        isolated_resources.scope_binder,
                        query_service=isolated_resources.query_service,
                        transform_service=isolated_resources.transform_service,
                    ),
                )
                async with Client(isolated_server) as isolated_client:
                    isolated_session = _structured(
                        await isolated_client.call_tool(
                            "runtime_open_direct_session",
                            {"session_name": "隔离身份会话"},
                        ),
                        DirectSessionResult,
                    )
                    await _expect_tool_error(
                        isolated_client,
                        "data_describe_package",
                        {
                            "direct_session_alias": isolated_session.direct_session_alias,
                            "package_alias": imported.published.package.catalog_entry.alias,
                        },
                        "PACKAGE_NOT_FOUND",
                    )
                    await _expect_tool_error(
                        isolated_client,
                        "data_query_rows",
                        {
                            "direct_session_alias": isolated_session.direct_session_alias,
                            "cursor": first_cursor,
                        },
                        "CURSOR_SCOPE_MISMATCH",
                    )
            finally:
                await isolated_resources.close()
    finally:
        await restarted_resources.close()

    inspection_engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with inspection_engine.connect() as connection:
            package_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT dpm.member_name, dpm.member_kind, dpm.media_type, "
                            "dpm.row_count, dpm.artifact_id, dpm.artifact_version_id "
                            "FROM direct_session_bindings AS dsb "
                            "JOIN data_packages AS dp ON dp.task_id = dsb.task_id "
                            "JOIN data_package_members AS dpm ON dpm.package_id = dp.package_id "
                            "WHERE dsb.configured_subject = :configured_subject"
                        ),
                        {"configured_subject": configured_subject},
                    )
                )
                .mappings()
                .all()
            )
            durable_counts = (
                (
                    await connection.execute(
                        text(
                            "SELECT count(DISTINCT a.artifact_id) AS artifact_count, "
                            "count(DISTINCT av.version_id) AS version_count, "
                            "count(DISTINCT av.version_id) FILTER "
                            "(WHERE a.kind = 'cad_source') AS source_version_count "
                            "FROM direct_session_bindings AS dsb "
                            "JOIN artifacts AS a ON a.task_id = dsb.task_id "
                            "JOIN artifact_versions AS av ON av.artifact_id = a.artifact_id "
                            "WHERE dsb.configured_subject = :configured_subject"
                        ),
                        {"configured_subject": configured_subject},
                    )
                )
                .mappings()
                .one()
            )
    finally:
        await inspection_engine.dispose()

    assert opened.direct_session_alias == resumed.direct_session_alias == "direct_001"
    assert opened.resumed is False
    assert resumed.resumed is True
    assert {parallel_a.binding.direct_session_alias, parallel_b.binding.direct_session_alias} == {
        "direct_002",
        "direct_003",
    }
    assert imported.published.operation.catalog_entry.alias == "op_001"
    assert imported.published.package.catalog_entry.alias == "data_001"
    assert len(package_rows) == 9
    source_member = next(row for row in package_rows if row["member_kind"] == "source")
    assert source_member["member_name"] == "source/original.dxf"
    assert source_member["media_type"] == "application/dxf"
    assert source_member["row_count"] is None
    assert source_member["artifact_id"]
    assert source_member["artifact_version_id"]
    assert dict(durable_counts) == {
        "artifact_count": 9,
        "version_count": 9,
        "source_version_count": 1,
    }
    assert len(catalog.packages) == 1
    assert len(operation_catalog.operations) == 1
    assert operation_catalog.operations[0].primary_result.package_alias == "data_001"
    assert "543,124 rows" in description.markdown
    assert "No business meaning, topology" in description.markdown


async def test_m2a_transform_package_operation_and_code_survive_postgres_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    configured_subject = f"m2a-postgres-{uuid4().hex}"
    # A transport retry carries the same server-side request identity across
    # a Backend restart. A separate invocation receives a different identity.
    monkeypatch.setattr(transform_tools, "server_direct_invocation_key", lambda _ctx: "mcp:pg-restart-original")
    config = DirectMcpConfig.from_environ(
        {
            "CAD2BIM_DIRECT_SUBJECT": configured_subject,
            "CAD2BIM_DIRECT_APPROVED_ROOTS": str(FIXTURE_ROOT),
            "CAD2BIM_DATABASE_URL": _runtime_database_url(
                TEST_DATABASE_URL,
                username="cad2bim_mcp_login",
                password=MCP_PASSWORD,
            ),
            "CAD2BIM_STORAGE_ROOT": str(tmp_path / "storage"),
            "CAD2BIM_MAX_UPLOAD_BYTES": str(100 * 1024 * 1024),
            "CAD2BIM_DATA_QUERY_CURSOR_KEY": CURSOR_KEY,
            "CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE": _python_worker_executable(),
        }
    )
    request: dict[str, object]
    first_resources = create_direct_runtime_resources(config)
    try:
        await first_resources.verify_ready()
        server = create_direct_mcp(
            DirectApplicationFacade(
                first_resources.application,
                first_resources.sessions,
                first_resources.scope_resolver,
                first_resources.scope_binder,
                query_service=first_resources.query_service,
                transform_service=first_resources.transform_service,
                code_discovery_service=first_resources.code_discovery_service,
            ),
        )
        async with Client(server) as client:
            opened = _structured(
                await client.call_tool(
                    "runtime_open_direct_session",
                    {"session_name": "M2a PostgreSQL restart"},
                ),
                DirectSessionResult,
            )
            # Backend preparation of a user upload; the Agent only consumes its Package.
            upload_scope = await first_resources.scope_resolver.resolve(opened.direct_session_alias, command_id="upload-fixture")
            with first_resources.scope_binder(upload_scope.identity):
                imported = await first_resources.application.import_cad_source(upload_scope, ValidatedSource(
                    filename=MINIMAL_SOURCE.name, media_type="application/dxf", content=MINIMAL_SOURCE.read_bytes(),
                    source_identity=str(MINIMAL_SOURCE), adapter_metadata={}))
            request = {
                "mode": "create",
                "direct_session_alias": opened.direct_session_alias,
                "inputs": [
                    {
                        "input_name": "entities",
                        "package_alias": imported.published.package.catalog_entry.alias,
                        "payload_name": "entities",
                    }
                ],
                "parameters": [{"name": "target_layer", "kind": "string", "value": "0"}],
                "execution": {
                    "engine": "duckdb_sql",
                    "code": (
                        "SELECT entity_key, layer, entity_type FROM entities "
                        "WHERE layer = $target_layer"
                    ),
                },
                "outputs": [
                    {
                        "payload_name": "selected_entities",
                        "format": "parquet",
                        "description": "Selected entity facts.",
                        "logical_key_fields": ["entity_key"],
                    }
                ],
                "output_package": {
                    "name": "postgres_m2a_selection_v1",
                    "description_markdown": M2A_DESCRIPTION,
                },
            }
            transformed = _structured(
                await client.call_tool("data_transform", request, timeout=65),
                DataTransformResult,
            )
    finally:
        await first_resources.close()

    restarted_resources = create_direct_runtime_resources(config)
    try:
        await restarted_resources.verify_ready()
        restarted_server = create_direct_mcp(
            DirectApplicationFacade(
                restarted_resources.application,
                restarted_resources.sessions,
                restarted_resources.scope_resolver,
                restarted_resources.scope_binder,
                query_service=restarted_resources.query_service,
                transform_service=restarted_resources.transform_service,
                code_discovery_service=restarted_resources.code_discovery_service,
            ),
        )
        async with Client(restarted_server) as client:
            replayed = _structured(
                await client.call_tool("data_transform", request, timeout=65),
                DataTransformResult,
            )
            assert replayed == transformed
            catalog = _structured(
                await client.call_tool(
                    "data_package_list",
                    {"direct_session_alias": opened.direct_session_alias},
                ),
                DirectPackageListResult,
            )
            operations = _structured(
                await client.call_tool(
                    "operation_list",
                    {"direct_session_alias": opened.direct_session_alias},
                ),
                OperationListResult,
            )
            transform_operations = _structured(
                await client.call_tool(
                    "operation_list",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "operation_kind": "data_transform",
                        "page_size": 1,
                    },
                ),
                OperationListResult,
            )
            source_operations = _structured(
                await client.call_tool(
                    "operation_list",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "operation_kind": "source_ingestion",
                        "page_size": 1,
                    },
                ),
                OperationListResult,
            )
            inspected = _structured_union(
                await client.call_tool(
                    "operation_get",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "operation_alias": transformed.operation_alias,
                    },
                ),
                OperationGetResult,
            )
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
                item for item in code_page.codes if item.result_alias == transformed.package_alias
            )
            assert code_card.result_code_locator is not None
            code = _structured(
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
            assert [item.package_kind for item in catalog.packages] == [
                "derived",
                "source_l0",
            ]
            assert [item.operation_kind for item in operations.operations] == [
                "data_transform",
                "source_ingestion",
            ]
            assert [item.operation_kind for item in transform_operations.operations] == [
                "data_transform"
            ]
            assert transform_operations.complete is True
            assert [item.operation_kind for item in source_operations.operations] == [
                "source_ingestion"
            ]
            assert source_operations.complete is True
            assert inspected.operation.outcome == "succeeded"
            assert code.text == request["execution"]["code"]
            assert rows.page.total_matched_count == 1
    finally:
        await restarted_resources.close()

    inspection_engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with inspection_engine.connect() as connection:
            persisted = (
                (
                    await connection.execute(
                        text(
                            "SELECT count(DISTINCT dp.package_id) AS packages, "
                            "count(DISTINCT co.operation_id) AS operations, "
                            "count(DISTINCT dce.catalog_entry_id) AS entries "
                            "FROM direct_session_bindings AS dsb "
                            "LEFT JOIN data_packages AS dp ON dp.task_id = dsb.task_id "
                            "LEFT JOIN critical_operations AS co ON co.task_id = dsb.task_id "
                            "LEFT JOIN data_catalog_entries AS dce ON dce.run_id = dsb.run_id "
                            "WHERE dsb.configured_subject = :configured_subject"
                        ),
                        {"configured_subject": configured_subject},
                    )
                )
                .mappings()
                .one()
            )
            shared_code = (
                (
                    await connection.execute(
                        text(
                            "SELECT count(*) AS matching_refs FROM direct_session_bindings dsb "
                            "JOIN critical_operations co ON co.task_id = dsb.task_id "
                            "JOIN data_package_members dpm "
                            "ON dpm.package_id = co.result_package_id "
                            "AND dpm.member_kind = 'code' "
                            "WHERE dsb.configured_subject = :configured_subject "
                            "AND co.code_artifact_id = dpm.artifact_id "
                            "AND co.code_artifact_version_id = dpm.artifact_version_id "
                            "AND co.code_artifact_sha256 = dpm.artifact_sha256"
                        ),
                        {"configured_subject": configured_subject},
                    )
                )
                .mappings()
                .one()
            )
            payload_binding = (
                (
                    await connection.execute(
                        text(
                            "SELECT dsb.run_id, pce.alias AS package_alias, "
                            "dp.package_kind, dpm.logical_payload_name, "
                            "av.version_id, av.version_no, av.sha256, av.size_bytes, "
                            "oce.alias AS operation_alias "
                            "FROM direct_session_bindings AS dsb "
                            "JOIN data_catalog_entries AS pce "
                            "ON pce.task_id = dsb.task_id AND pce.run_id = dsb.run_id "
                            "AND pce.resource_kind = 'package' "
                            "JOIN data_packages AS dp ON dp.package_id = pce.package_id "
                            "AND dp.task_id = dsb.task_id "
                            "JOIN data_package_members AS dpm "
                            "ON dpm.package_id = dp.package_id AND dpm.member_kind = 'payload' "
                            "JOIN artifact_versions AS av ON av.artifact_id = dpm.artifact_id "
                            "AND av.version_id = dpm.artifact_version_id "
                            "JOIN critical_operations AS co ON co.task_id = dsb.task_id "
                            "AND co.result_package_id = dp.package_id "
                            "JOIN data_catalog_entries AS oce "
                            "ON oce.task_id = dsb.task_id AND oce.run_id = dsb.run_id "
                            "AND oce.operation_id = co.operation_id "
                            "AND oce.resource_kind = 'operation' "
                            "WHERE dsb.configured_subject = :configured_subject "
                            "AND pce.alias = :package_alias"
                        ),
                        {
                            "configured_subject": configured_subject,
                            "package_alias": transformed.package_alias,
                        },
                    )
                )
                .mappings()
                .one()
            )
    finally:
        await inspection_engine.dispose()

    assert dict(persisted) == {"packages": 2, "operations": 2, "entries": 4}
    assert shared_code["matching_refs"] == 1
    assert payload_binding["run_id"]
    assert payload_binding["package_alias"] == transformed.package_alias
    assert payload_binding["package_kind"] == "derived"
    assert payload_binding["logical_payload_name"] == "selected_entities"
    assert payload_binding["version_id"]
    assert payload_binding["version_no"] == 1
    assert len(payload_binding["sha256"]) == 64
    assert payload_binding["size_bytes"] > 0
    assert payload_binding["operation_alias"] == transformed.operation_alias


async def test_failed_python_repair_chain_survives_postgres_restart_without_duplication(
    tmp_path: Path,
) -> None:
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
    configured_subject = f"repair-postgres-{uuid4().hex}"
    config = DirectMcpConfig.from_environ(
        {
            "CAD2BIM_DIRECT_SUBJECT": configured_subject,
            "CAD2BIM_DIRECT_APPROVED_ROOTS": str(FIXTURE_ROOT),
            "CAD2BIM_DATABASE_URL": _runtime_database_url(
                TEST_DATABASE_URL,
                username="cad2bim_mcp_login",
                password=MCP_PASSWORD,
            ),
            "CAD2BIM_STORAGE_ROOT": str(tmp_path / "storage"),
            "CAD2BIM_MAX_UPLOAD_BYTES": str(100 * 1024 * 1024),
            "CAD2BIM_DATA_QUERY_CURSOR_KEY": CURSOR_KEY,
            "CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE": _python_worker_executable(),
        }
    )
    session_name = "PostgreSQL failed transform exact repair"
    bad_line = '    ctx.write_table("selected_entities", ctx.table("entities")\n'
    good_line = '    ctx.write_table("selected_entities", ctx.table("entities"))\n'
    request = {
        "mode": "create",
        "inputs": [
            {
                "input_name": "entities",
                "package_alias": "",
                "payload_name": "entities",
            }
        ],
        "inline_tables": [],
        "parameters": [],
        "execution": {
            "engine": "python_arrow",
            "code": f"def transform(ctx):\n{bad_line}",
        },
        "outputs": [
            {
                "payload_name": "selected_entities",
                "format": "parquet",
                "description": "Every exact source entity row after localized repair.",
                "logical_key_fields": ["entity_key"],
            }
        ],
        "output_package": {
            "name": "postgres_repaired_entities_v1",
            "description_markdown": REPAIR_DESCRIPTION,
        },
    }

    first_resources = create_direct_runtime_resources(config)
    try:
        await first_resources.verify_ready()
        server = create_direct_mcp(
            DirectApplicationFacade(
                first_resources.application,
                first_resources.sessions,
                first_resources.scope_resolver,
                first_resources.scope_binder,
                query_service=first_resources.query_service,
                transform_service=first_resources.transform_service,
                code_discovery_service=first_resources.code_discovery_service,
            ),
        )
        async with Client(server) as client:
            opened = _structured(
                await client.call_tool(
                    "runtime_open_direct_session",
                    {"session_name": session_name},
                ),
                DirectSessionResult,
            )
            # Backend preparation of a user upload; the Agent only consumes its Package.
            upload_scope = await first_resources.scope_resolver.resolve(opened.direct_session_alias, command_id="upload-fixture")
            with first_resources.scope_binder(upload_scope.identity):
                imported = await first_resources.application.import_cad_source(upload_scope, ValidatedSource(
                    filename=MINIMAL_SOURCE.name, media_type="application/dxf", content=MINIMAL_SOURCE.read_bytes(),
                    source_identity=str(MINIMAL_SOURCE), adapter_metadata={}))
            request["direct_session_alias"] = opened.direct_session_alias
            request["inputs"][0]["package_alias"] = imported.published.package.catalog_entry.alias

            with pytest.raises(ToolError) as failed_call:
                await client.call_tool("data_transform", request, timeout=125)
            failure_text = str(failed_call.value)
            assert "TRANSFORM_PYTHON_INVALID" in failure_text
            assert "phase=code_validation" in failure_text
            alias_match = re.search(r"operation_alias=(op_[0-9]+)", failure_text)
            assert alias_match is not None
            failed_alias = alias_match.group(1)

            code_page = _structured(
                await client.call_tool(
                    "code_list",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "mode": "first",
                        "query": {
                            "code_kind": "data_transform_attempt",
                            "statuses": ["failed"],
                            "page_size": 50,
                        },
                    },
                ),
                DirectCodeListResult,
            )
            code_card = next(item for item in code_page.codes if item.owner_alias == failed_alias)
            source = _structured(
                await client.call_tool(
                    "code_read",
                    {
                        "direct_session_alias": opened.direct_session_alias,
                        "source_form": "single_text",
                        "code_locator": code_card.code_locator,
                        "view": {
                            "view_mode": "search",
                            "text": bad_line,
                            "context_lines": 1,
                            "max_matches": 5,
                        },
                    },
                ),
                DirectCodeReadResult,
            )
            repair_request = {
                "mode": "repair",
                "direct_session_alias": opened.direct_session_alias,
                "source": {
                    "source_kind": "failed_operation",
                    "operation_alias": failed_alias,
                },
                "edits": [{"old_text": bad_line, "new_text": good_line}],
            }
            repaired = _structured(
                await client.call_tool(
                    "data_transform",
                    repair_request,
                    timeout=125,
                ),
                DataRepairTransformResult,
            )
            assert repaired.repair_parent_operation_alias == failed_alias
            publication_state = await _published_state(configured_subject)

            other_session = _structured(
                await client.call_tool(
                    "runtime_open_direct_session",
                    {"session_name": f"{session_name} isolated"},
                ),
                DirectSessionResult,
            )
            await _expect_tool_error(
                client,
                "operation_get",
                {
                    "direct_session_alias": other_session.direct_session_alias,
                    "operation_alias": failed_alias,
                },
                "OPERATION_NOT_FOUND",
            )
    finally:
        await first_resources.close()

    legacy_engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with legacy_engine.begin() as connection:
            updated = await connection.execute(
                text(
                    "UPDATE critical_operations AS co "
                    "SET tool_name = 'data_repair_transform', "
                    "idempotency_key = 'data-repair-transform:' || co.request_fingerprint "
                    "FROM direct_session_bindings AS dsb, data_catalog_entries AS dce "
                    "WHERE dsb.configured_subject = :configured_subject "
                    "AND dce.task_id = dsb.task_id AND dce.run_id = dsb.run_id "
                    "AND dce.operation_id = co.operation_id AND dce.alias = :operation_alias"
                ),
                {
                    "configured_subject": configured_subject,
                    "operation_alias": repaired.operation_alias,
                },
            )
            assert updated.rowcount == 1
    finally:
        await legacy_engine.dispose()

    legacy_publication_state = await _published_state(configured_subject)
    assert legacy_publication_state == publication_state

    restarted_resources = create_direct_runtime_resources(config)
    try:
        await restarted_resources.verify_ready()
        restarted_server = create_direct_mcp(
            DirectApplicationFacade(
                restarted_resources.application,
                restarted_resources.sessions,
                restarted_resources.scope_resolver,
                restarted_resources.scope_binder,
                query_service=restarted_resources.query_service,
                transform_service=restarted_resources.transform_service,
            ),
        )
        async with Client(restarted_server) as client:
            resumed = _structured(
                await client.call_tool(
                    "runtime_open_direct_session",
                    {"session_name": session_name},
                ),
                DirectSessionResult,
            )
            assert resumed.resumed is True
            assert resumed.direct_session_alias == opened.direct_session_alias
            failed = _structured_union(
                await client.call_tool(
                    "operation_get",
                    {
                        "direct_session_alias": resumed.direct_session_alias,
                        "operation_alias": failed_alias,
                    },
                ),
                OperationGetResult,
            )
            succeeded = _structured_union(
                await client.call_tool(
                    "operation_get",
                    {
                        "direct_session_alias": resumed.direct_session_alias,
                        "operation_alias": repaired.operation_alias,
                    },
                ),
                OperationGetResult,
            )
            assert failed.operation.outcome == "failed"
            assert succeeded.operation.outcome == "succeeded"
            assert succeeded.detail.repair_parent_operation_alias == failed_alias

            replayed = _structured(
                await client.call_tool(
                    "data_transform",
                    repair_request,
                    timeout=125,
                ),
                DataRepairTransformResult,
            )
            assert replayed == repaired
            assert await _published_state(configured_subject) == legacy_publication_state

            description = _structured(
                await client.call_tool(
                    "data_get_package_description",
                    {
                        "direct_session_alias": resumed.direct_session_alias,
                        "package_alias": repaired.package_alias,
                    },
                ),
                PackageDescriptionResult,
            )
            assert description.markdown == REPAIR_DESCRIPTION
    finally:
        await restarted_resources.close()

    inspection_engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with inspection_engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT co.operation_id, co.tool_name, co.idempotency_key, co.status, "
                            "co.result_package_id, co.code_artifact_id, "
                            "co.code_artifact_version_id, co.code_artifact_sha256, "
                            "co.description_artifact_id, "
                            "co.description_artifact_version_id, "
                            "co.description_artifact_sha256, "
                            "co.repair_parent_operation_id "
                            "FROM direct_session_bindings dsb "
                            "JOIN critical_operations co ON co.task_id = dsb.task_id "
                            "WHERE dsb.configured_subject = :configured_subject "
                            "AND co.tool_name IN ('data_transform', 'data_repair_transform') "
                            "ORDER BY co.started_at"
                        ),
                        {"configured_subject": configured_subject},
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await inspection_engine.dispose()

    assert len(rows) == 2
    original, repaired_row = rows
    assert original["tool_name"] == "data_transform"
    assert original["status"] == "failed"
    assert original["result_package_id"] is None
    assert original["repair_parent_operation_id"] is None
    assert repaired_row["tool_name"] == "data_repair_transform"
    assert repaired_row["idempotency_key"].startswith("data-repair-transform:")
    assert repaired_row["status"] == "succeeded"
    assert repaired_row["result_package_id"] is not None
    assert repaired_row["repair_parent_operation_id"] == original["operation_id"]
    assert all(
        row[field]
        for row in rows
        for field in (
            "code_artifact_id",
            "code_artifact_version_id",
            "code_artifact_sha256",
            "description_artifact_id",
            "description_artifact_version_id",
            "description_artifact_sha256",
        )
    )
    assert (
        original["description_artifact_id"],
        original["description_artifact_version_id"],
        original["description_artifact_sha256"],
    ) == (
        repaired_row["description_artifact_id"],
        repaired_row["description_artifact_version_id"],
        repaired_row["description_artifact_sha256"],
    )
