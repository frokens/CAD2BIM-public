from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import sys
from dataclasses import replace
from pathlib import Path
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError
from mcp.types import ListToolsResult
from tests.unit.mcp.test_provider_output_compaction import _expand_shared_input


from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.identity import ConfiguredLocalSubject
from CAD2BIM_backend.infrastructure.data_transform.python_worker_environment import (
    APPROVED_PACKAGES,
    PythonRuntimeIdentity,
    PythonWorkerEnvironmentAttestation,
    write_attestation,
)
from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationErrorPhase,
    CriticalOperationKind,
    CriticalOperationInitiator,
    CriticalOperationStatus,
    DataPackageKind,
    DataTransformOperationDetail as DomainDataTransformOperationDetail,
    JsonPayloadKind,
    PackageMemberKind,
    TransformInputBindingRecord,
    TransformOutputRecord,
)
from CAD2BIM_backend.domain.data_query import (
    CadEntityCountResult,
    CadEntityQuery,
    CadEntityRowsResult,
    CadEntitiesQuery,
    CadGetEntityResult,
    CadXdataProfileResult,
    CadXdataQuery,
    CadXdataRowsResult,
    DataDescribePackageResult,
    DataQueryRowsResult,
    DataRowsQuery,
    PageInfo,
)
from CAD2BIM_backend.domain.data_query_sql import (
    DataQuerySqlQuery,
    DataQuerySqlResult,
    QuerySqlColumn,
    QuerySqlResponseEncoding,
    QuerySqlRow,
    QuerySqlRowSchema,
    QuerySqlStatistics,
)
from CAD2BIM_backend.domain.data_json import JsonValueQuery, JsonValueResult
from CAD2BIM_mcp.direct import application as facade_application
from CAD2BIM_mcp.direct.operation import application as operation_application
from CAD2BIM_mcp.direct.query import application as query_application
from CAD2BIM_mcp.direct.transform import application as transform_application
from CAD2BIM_mcp.direct import entry as direct_entry
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.config import DirectMcpConfig
from CAD2BIM_mcp.direct.cad.contracts import (
    CadL0Summary,
    UnsupportedEntityTypeSummary,
)
from CAD2BIM_mcp.direct.operation.contracts import (
    DataOperationCard,
    FailedDataTransformOperationCard,
    FailedOperationGetResult,
    FailedSourceIngestionOperationCard,
    OperationListResult,
    SourceIngestionSourceDetail,
    SucceededDataTransformOperationCard,
    SucceededOperationGetResult,
    SucceededSourceIngestionOperationCard,
)
from CAD2BIM_mcp.direct.catalog.contracts import (
    DirectDataDescribePackageResult,
    DerivedCatalogPackageCard,
    DirectPackageListResult,
    JsonDataPayloadCard,
    PackageDescriptionResult,
    ParquetDataPayloadCard,
    SourceL0CatalogPackageCard,
)
from CAD2BIM_mcp.direct.transform.contracts import (
    DataTransformResult,
    JsonDataTransformOutputResult,
    MultiOutputDataTransformResult,
)
from CAD2BIM_mcp.direct.session.contracts import (
    DirectSessionResult,
)
from CAD2BIM_mcp.direct.transform.inputs import (
    DirectDuckDbTransformInput as DirectDataTransformInput,
    to_domain_duckdb_transform_command as to_domain_data_transform_command,
)
from CAD2BIM_mcp.direct.query.sql_inputs import (
    QuerySqlContinuationInput,
    QuerySqlFirstPageInput,
    QuerySqlQuery,
    to_domain_data_query_sql_query,
    validate_direct_data_query_sql_input,
)
from CAD2BIM_mcp.direct.server import create_direct_mcp
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.tool_support import DirectToolInputErrorMiddleware
from CAD2BIM_mcp.direct.query.cad_inputs import (
    to_domain_cad_entities_query,
    to_domain_cad_xdata_query,
    to_domain_data_rows_query,
)
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.function_tool import FunctionTool
from CAD2BIM_mcp.direct.operation.tools import register_operation_list


_DIRECT_TOOL_ORDER = (
    "runtime_open_direct_session",
    "ifc_model_initialize",
    "ifc_model_info",
    "ifc_model_configure_geometry_basis",
    "ifc_representation_context_list",
    "ifc_project_update",
    "ifc_spatial_structure_list",
    "ifc_spatial_structure_apply",
    "data_package_list",
    "operation_list",
    "operation_get",
    "code_list",
    "code_read",
    "cad_analyze_tolerance_candidates",
    "cad_compute_measurements",
    "data_derive",
    "data_query_sql",
    "data_query_python",
    "ifc_search_official_docs",
    "ifc_read_official_doc",
    "data_read_json",
    "data_get_package_description",
    "data_describe_package",
    "data_query_rows",
    "cad_query_entities",
    "cad_get_entity",
    "cad_query_xdata",
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
    "ifc_products_apply",
    "ifc_products_edit",
    "ifc_products_read",
    "ifc_authoring_publish",
    "ifc_query_python",
)


def test_direct_source_layout_uses_only_stable_capability_names() -> None:
    package_root = Path(__file__).resolve().parents[3] / "src" / "CAD2BIM_mcp"
    direct_root = package_root / "direct"

    assert {path.name for path in direct_root.iterdir() if path.is_dir()} >= {
        "session",
        "catalog",
        "operation",
        "query",
        "transform",
        "cad",
        "ifc",
    }
    assert not tuple(package_root.glob("direct_*.py"))


def _direct_environ(root: Path) -> dict[str, str]:
    worker_prefix = root / "data-python-worker"
    worker_python = worker_prefix / "bin" / "python"
    worker_python.parent.mkdir(parents=True, exist_ok=True)
    worker_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    worker_python.chmod(0o700)
    write_attestation(
        worker_python,
        PythonWorkerEnvironmentAttestation(
            approved_packages=APPROVED_PACKAGES,
            distributions={"cad2bim": importlib.metadata.version("cad2bim")},
            runtime=PythonRuntimeIdentity(
                implementation="cpython",
                version=(3, 11, 9),
                cache_tag="cpython-311",
                abi_flags="",
                executable=str(worker_python),
                prefix=str(worker_prefix),
                base_prefix=str(root / "base-python"),
            ),
        ),
    )
    return {
        "CAD2BIM_DIRECT_SUBJECT": "local-user",
        "CAD2BIM_DATABASE_URL": "postgresql+psycopg://runtime:secret@db/cad2bim",
        "CAD2BIM_STORAGE_ROOT": str(root / "storage"),
        "CAD2BIM_MAX_UPLOAD_BYTES": "104857600",
        "CAD2BIM_DATA_QUERY_CURSOR_KEY": "A" * 43,
        "CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE": str(worker_python),
    }


def test_direct_config_uses_only_explicit_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_load = Mock(side_effect=AssertionError("dotenv loading is forbidden"))
    monkeypatch.setattr(Settings, "load", settings_load)

    config = DirectMcpConfig.from_environ(_direct_environ(tmp_path))

    assert config.subject == ConfiguredLocalSubject(subject="local-user")
    assert config.storage_root == tmp_path / "storage"
    assert config.max_upload_bytes == 104857600
    assert config.host_python_max_concurrent_workers == 1
    assert config.data_query_cursor_key == "A" * 43
    assert config.python_worker_executable == tmp_path / "data-python-worker" / "bin" / "python"
    assert config.python_worker_attestation.approved_packages == APPROVED_PACKAGES
    assert config.backend_settings().database_url.startswith("postgresql+psycopg://")
    assert config.data_query_cursor_key not in repr(config)
    assert not hasattr(config.backend_settings(), "data_query_cursor_key")
    settings_load.assert_not_called()


@pytest.mark.parametrize("capacity", ["0", "129", "1.5", "invalid"])
def test_direct_config_rejects_invalid_host_python_capacity(tmp_path: Path, capacity: str) -> None:
    with pytest.raises(ValueError):
        DirectMcpConfig.from_environ({
            **_direct_environ(tmp_path),
            "CAD2BIM_HOST_PYTHON_MAX_CONCURRENT_WORKERS": capacity,
        })


def test_direct_config_accepts_explicit_host_python_capacity(tmp_path: Path) -> None:
    config = DirectMcpConfig.from_environ({
        **_direct_environ(tmp_path), "CAD2BIM_HOST_PYTHON_MAX_CONCURRENT_WORKERS": "2",
    })
    assert config.host_python_max_concurrent_workers == 2


def test_direct_config_rejects_dotenv_and_missing_database_or_cursor_key(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="does not read"):
        DirectMcpConfig.from_environ({**_direct_environ(tmp_path), "CAD2BIM_ENV_FILE": ".env"})

    values = _direct_environ(tmp_path)
    del values["CAD2BIM_DATABASE_URL"]
    with pytest.raises(ValueError, match="DATABASE_URL"):
        DirectMcpConfig.from_environ(values)

    values = _direct_environ(tmp_path)
    del values["CAD2BIM_DATA_QUERY_CURSOR_KEY"]
    with pytest.raises(ValueError, match="DATA_QUERY_CURSOR_KEY"):
        DirectMcpConfig.from_environ(values)

    values = _direct_environ(tmp_path)
    del values["CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE"]
    with pytest.raises(ValueError, match="DATA_PYTHON_WORKER_EXECUTABLE"):
        DirectMcpConfig.from_environ(values)

    values = _direct_environ(tmp_path)
    values["CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE"] = "relative/python"
    with pytest.raises(ValueError, match="absolute path"):
        DirectMcpConfig.from_environ(values)

    values = _direct_environ(tmp_path)
    values["CAD2BIM_MAX_UPLOAD_BYTES"] = "104857599"
    with pytest.raises(ValueError, match="at least 104857600"):
        DirectMcpConfig.from_environ(values)


def test_direct_config_rejects_missing_or_prefix_drifted_worker_attestation(
    tmp_path: Path,
) -> None:
    values = _direct_environ(tmp_path)
    python = Path(values["CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE"])
    python.parent.parent.joinpath(".cad2bim-data-python-worker.json").unlink()
    with pytest.raises(ValueError, match="attestation is unavailable"):
        DirectMcpConfig.from_environ(values)

    values = _direct_environ(tmp_path)
    values["CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE"] = sys.executable
    with pytest.raises(ValueError, match="attestation is unavailable"):
        DirectMcpConfig.from_environ(values)

    values = _direct_environ(tmp_path)
    python = Path(values["CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE"])
    valid = DirectMcpConfig.from_environ(values).python_worker_attestation
    write_attestation(
        python,
        replace(
            valid,
            runtime=replace(valid.runtime, prefix=str(tmp_path / "different-prefix")),
        ),
    )
    with pytest.raises(ValueError, match="prefix differs"):
        DirectMcpConfig.from_environ(values)








async def test_direct_server_registers_frozen_cad_data_plus_current_ifc_tools() -> None:
    server = create_direct_mcp(Mock())

    tools = {tool.name: tool for tool in await server.list_tools()}

    assert set(tools) == set(_DIRECT_TOOL_ORDER)
    serialized_schemas = repr({name: tool.parameters for name, tool in tools.items()}).lower()
    for forbidden in ("source_alias", "job_alias", "row_alias", "entity_alias"):
        assert f"'{forbidden}'" not in serialized_schemas


def test_provider_numeric_tuple_projection_preserves_matrix_and_vector_domains() -> None:
    from copy import deepcopy
    from jsonschema import Draft202012Validator
    from pydantic import TypeAdapter
    from CAD2BIM_mcp.direct.provider_schema import provider_input_array_schema

    original = TypeAdapter(tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]).json_schema()
    before = deepcopy(original)
    projected = provider_input_array_schema(original)
    assert original == before
    assert projected["items"]["items"]["type"] == "number"
    old, new = Draft202012Validator(original), Draft202012Validator(projected)
    identity = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    for candidate in (identity, [], identity[:2], [*identity, identity[0]], [["1", 0, 0], *identity[1:]], [[1, 0], *identity[1:]]):
        assert old.is_valid(candidate) == new.is_valid(candidate)
    assert new.is_valid(identity)
    assert not new.is_valid([["1", "0", "0"]] * 3)
    heterogeneous = TypeAdapter(tuple[str, float]).json_schema()
    with pytest.raises(ValueError, match="fixed homogeneous"):
        provider_input_array_schema(heterogeneous)


async def test_geometry_authoring_and_preview_input_arrays_keep_numeric_items() -> None:
    server = create_direct_mcp(Mock())
    for tool in await server.list_tools():
        if tool.name in {"ifc_geometry_resource_write", "ifc_geometry_preview"}:
            assert '"prefixItems"' not in json.dumps(tool.parameters), tool.name


async def test_direct_provider_registers_expected_tools_and_resource_templates() -> None:
    server = create_direct_mcp(Mock())
    tools = await server.list_tools()
    resource_templates = await server.list_resource_templates()
    tool_names = tuple(tool.name for tool in tools)

    assert tool_names == _DIRECT_TOOL_ORDER
    assert len(tool_names) == 42
    assert sum(name.startswith("ifc_") for name in tool_names) == 24
    assert len(resource_templates) == 0
    assert tool_names[-10:] == (
        "ifc_geometry_resource_write",
        "ifc_product_family_list",
        "ifc_product_family_info",
        "ifc_product_family_write",
        "ifc_product_realization_preview",
        "ifc_products_apply",
        "ifc_products_edit",
        "ifc_products_read",
        "ifc_authoring_publish",
        "ifc_query_python",
    )


async def test_direct_input_boundary_covers_every_registered_function_tool() -> None:
    server = create_direct_mcp(Mock())
    tools = await server.list_tools()

    assert len(tools) == 41
    assert server.strict_input_validation is False
    assert all(isinstance(tool, FunctionTool) for tool in tools)


async def test_direct_input_boundary_fails_closed_without_validation_target() -> None:
    middleware = DirectToolInputErrorMiddleware()
    call_next = AsyncMock()

    with pytest.raises(RuntimeError, match="validation context is unavailable"):
        await middleware.on_call_tool(
            SimpleNamespace(fastmcp_context=None),
            call_next,
        )

    get_tool = AsyncMock(return_value=None)
    context = SimpleNamespace(
        fastmcp_context=SimpleNamespace(fastmcp=SimpleNamespace(get_tool=get_tool)),
        message=SimpleNamespace(name="missing_tool", arguments={}),
    )
    with pytest.raises(RuntimeError, match="validation target is unavailable"):
        await middleware.on_call_tool(context, call_next)

    get_tool.assert_awaited_once_with("missing_tool")
    call_next.assert_not_awaited()


@pytest.mark.parametrize("name", ["data_query_sql", "data_query_python"])
@pytest.mark.parametrize("mode", ["fresh", "default_fresh", "repair", "cursor"])
async def test_data_query_pagination_error_names_only_the_current_branch_location(name, mode):
    application = Mock()
    server = create_direct_mcp(application)
    query = {"inputs": [{"input_name": "source", "package_alias": "data_001", "payload_name": "input"}],
             **({"execution": {"engine": "duckdb_sql", "code": "SELECT 1"}} if name == "data_query_sql"
                else {"code": "def query(ctx):\n    return []"})}
    arguments = {"direct_session_alias": "direct_001"}
    if mode in {"fresh", "default_fresh"}:
        arguments.update(query=query, page_size="PRIVATE_PAGE_VALUE")
        if mode == "fresh":
            arguments["mode"] = "fresh"
        correction = "mode=fresh: put page_size inside query as query.page_size"
        valid = {key: value for key, value in arguments.items() if key != "page_size"}
        valid["query"] = {**query, "page_size": 2}
    elif mode == "repair":
        arguments.update(mode="repair", parent_attempt_alias="cur.private_parent",
            edits=[{"old_text": "private_source", "new_text": "private_replacement"}],
            query={"page_size": "PRIVATE_PAGE_VALUE"})
        correction = "mode=repair: put page_size at the request root; repair does not accept query"
        valid = {key: value for key, value in arguments.items() if key != "query"}
        valid["page_size"] = 2
    else:
        arguments.update(mode="cursor", cursor="cur.private_cursor", page_size="PRIVATE_PAGE_VALUE")
        correction = "mode=cursor: use the unchanged cursor; omit page_size and query"
        valid = {key: value for key, value in arguments.items() if key != "page_size"}
    middleware = DirectToolInputErrorMiddleware()
    context = SimpleNamespace(fastmcp_context=SimpleNamespace(fastmcp=server),
        message=SimpleNamespace(name=name, arguments=arguments))
    call_next = AsyncMock(return_value="accepted")
    with pytest.raises(ToolError) as caught:
        await middleware.on_call_tool(context, call_next)
    message = str(caught.value)
    assert correction in message
    assert "INVALID_ARGUMENT" in message and "phase=preflight" in message
    assert not any(secret in message for secret in (
        "PRIVATE_PAGE_VALUE", "private_parent", "private_cursor", "private_source", "private_replacement"))
    call_next.assert_not_awaited()
    context.message.arguments = valid
    assert await middleware.on_call_tool(context, call_next) == "accepted"


async def test_query_pagination_hint_is_shared_with_runner_but_excludes_ifc_query():
    from CAD2BIM_mcp.runner.data_tools import register_runner_data_tools

    middleware = DirectToolInputErrorMiddleware()
    runner = FastMCP("query-pagination")
    register_runner_data_tools(runner, Mock())
    context = SimpleNamespace(fastmcp_context=SimpleNamespace(fastmcp=runner),
        message=SimpleNamespace(name="data_query_sql", arguments={"mode": "fresh", "query": {}, "page_size": 2}))
    call_next = AsyncMock(return_value="accepted")
    with pytest.raises(ToolError, match="query.page_size"):
        await middleware.on_call_tool(context, call_next)
    call_next.assert_not_awaited()
    direct = create_direct_mcp(Mock())
    context.fastmcp_context.fastmcp = direct
    context.message.name = "ifc_query_python"
    context.message.arguments = {"direct_session_alias": "direct_001", "mode": "fresh",
        "code": "def query(ctx):\n    return []", "page_size": 2}
    assert await middleware.on_call_tool(context, call_next) == "accepted"


@pytest.mark.parametrize("source_form", ["single_text", "bundle", "batch"])
@pytest.mark.parametrize("line_count", [200, 225])
async def test_code_read_diagnostic_follows_actual_source_and_nested_view(
    source_form: str, line_count: int,
) -> None:
    server = create_direct_mcp(Mock())
    read = {
        "source_form": "bundle" if source_form == "batch" else source_form,
        "code_locator": "cur.private_source_token",
        "view": {"view_mode": "lines", "start_line": 1, "line_count": line_count},
    }
    if read["source_form"] == "bundle":
        read["source_path"] = "main.py"
    arguments = {"direct_session_alias": "direct_001"}
    if source_form == "batch":
        arguments.update(mode="batch", reads=[{**read, "view": {"view_mode": "full"}}, read])
    else:
        arguments.update(read)
    context = SimpleNamespace(
        fastmcp_context=SimpleNamespace(fastmcp=server),
        message=SimpleNamespace(name="code_read", arguments=arguments),
    )
    call_next = AsyncMock(return_value="accepted")
    middleware = DirectToolInputErrorMiddleware()
    if line_count == 200:
        assert await middleware.on_call_tool(context, call_next) == "accepted"
        call_next.assert_awaited_once_with(context)
    else:
        with pytest.raises(ToolError) as caught:
            await middleware.on_call_tool(context, call_next)
        path = "reads.1.view.line_count" if source_form == "batch" else "view.line_count"
        message = str(caught.value)
        assert f"invalid_path={path}; error_type=less_than_equal; constraint=maximum=200" in message
        assert "private_source_token" not in message
        assert len(message.encode()) < 2_000
        call_next.assert_not_awaited()


@pytest.mark.parametrize("query", [
    {"code_kind": "geometry_resource_program", "page_size": 51},
    {"code_kind": "data_query_sql_attempt", "page_size": 51},
])
async def test_code_list_nested_diagnostic_preserves_other_tool_contracts(query: dict) -> None:
    server = create_direct_mcp(Mock())
    context = SimpleNamespace(
        fastmcp_context=SimpleNamespace(fastmcp=server),
        message=SimpleNamespace(name="code_list", arguments={
            "direct_session_alias": "direct_001", "mode": "first", "query": query,
        }),
    )
    call_next = AsyncMock()
    with pytest.raises(ToolError, match="invalid_path=query.page_size; error_type=less_than_equal; constraint=maximum=50"):
        await DirectToolInputErrorMiddleware().on_call_tool(context, call_next)
    call_next.assert_not_awaited()


async def test_rows_input_union_reports_missing_wrapping_and_accepts_query() -> None:
    server = create_direct_mcp(Mock())
    query = {"package_alias": "data_001", "payload_name": "realization_sources", "select": ["product_key"]}
    call_next = AsyncMock(return_value="accepted")
    context = SimpleNamespace(
        fastmcp_context=SimpleNamespace(fastmcp=server),
        message=SimpleNamespace(name="data_query_rows", arguments={
            "direct_session_alias": "direct_001", **query,
        }),
    )
    with pytest.raises(ToolError) as caught:
        await DirectToolInputErrorMiddleware().on_call_tool(context, call_next)
    message = str(caught.value)
    assert "error_type=union_validation" in message
    assert "0: missing=query" in message and "1: missing=cursor" in message
    assert "payload_name" in message and "select" in message
    assert "realization_sources" not in message and "data_001" not in message
    call_next.assert_not_awaited()
    context.message.arguments = {"direct_session_alias": "direct_001", "query": query}
    assert await DirectToolInputErrorMiddleware().on_call_tool(context, call_next) == "accepted"


async def test_direct_unknown_tool_is_bounded_at_the_mcp_client_boundary() -> None:
    application = Mock()
    with pytest.raises(ToolError) as captured:
        async with Client(create_direct_mcp(application)) as client:
            await client.call_tool(
                "missing_direct_tool",
                {"code": "SOURCE_SENTINEL" * 4_096},
            )

    message = str(captured.value)
    assert message == "Direct tool input validation target is unavailable"
    assert len(message.encode("utf-8")) < 2_000
    assert "SOURCE_SENTINEL" not in message
    assert application.mock_calls == []


async def test_describe_projection_preserves_business_schema_and_domain_invariants() -> None:
    from CAD2BIM_mcp.direct.catalog.application import _describe_package_result

    schema_text = '{ "type": "object", "properties": {"sha256": {"type": "string"}, "schema_available": {"type": "boolean"}} }'
    backend = DataDescribePackageResult.model_validate({
        "package": {"package_alias": "data_001", "name": "Mixed results", "package_kind": "derived"},
        "payloads": [
            {"payload_name": "rows", "format": "parquet", "row_count": 2, "logical_key_fields": ["id"]},
            {"payload_name": "facts", "format": "json", "size_bytes": 123, "json_kind": "document"},
        ],
        "payload_schema": {"payload_name": "facts", "format": "json", "json_kind": "document", "schema_json": schema_text},
    })
    projected = _describe_package_result(backend)
    assert projected.payloads[0] == backend.payloads[0]
    assert projected.payloads[1].model_dump() == {"payload_name": "facts", "format": "json", "size_bytes": 123, "json_kind": "document"}
    assert projected.payload_schema.schema_json == schema_text
    assert projected.package == backend.package
    value = projected.model_dump(mode="json")
    server = create_direct_mcp(Mock())
    tool = next(item for item in await server.list_tools() if item.name == "data_describe_package")
    Draft202012Validator(tool.output_schema).validate(value)
    assert "schema_available" not in json.dumps(tool.output_schema)
    with pytest.raises(ValidationError, match="listed payload"):
        DirectDataDescribePackageResult.model_validate({**value, "payload_schema": {**value["payload_schema"], "payload_name": "missing"}})
    with pytest.raises(ValidationError, match="format must match"):
        DirectDataDescribePackageResult.model_validate({**value, "payload_schema": {**value["payload_schema"], "payload_name": "rows"}})


async def test_four_direct_tool_input_and_response_schemas_are_exactly_frozen() -> None:
    server = create_direct_mcp(Mock())
    tools = {tool.name: tool for tool in await server.list_tools()}
    expected_inputs = {
        "runtime_open_direct_session": (("session_name",), ("session_name",)),
        "data_package_list": (
            ("direct_session_alias", "before_sequence", "page_size"),
            ("direct_session_alias",),
        ),
        "data_get_package_description": (
            ("direct_session_alias", "package_alias"),
            ("direct_session_alias", "package_alias"),
        ),
    }
    for name, (fields, required) in expected_inputs.items():
        parameters = tools[name].parameters
        assert tuple(parameters["properties"]) == fields
        assert tuple(parameters["required"]) == required
        assert parameters.get("additionalProperties") is False

    expected_response_fields = {
        DirectSessionResult: (
            "direct_session_alias",
            "session_name",
            "resumed",
        ),
        DirectPackageListResult: ("packages", "complete", "next_before_sequence"),
        PackageDescriptionResult: ("package_alias", "package_name", "markdown"),
        ParquetDataPayloadCard: ("payload_name", "format", "row_count"),
        JsonDataPayloadCard: (
            "payload_name",
            "format",
            "size_bytes",
            "json_kind",
        ),
        DataOperationCard: (
            "operation_alias",
            "tool",
            "outcome",
            "source_filename",
            "started_at",
            "finished_at",
            "result_package_alias",
            "error_code",
        ),
        CadL0Summary: (
            "parse_complete",
            "observed_sections",
            "record_count",
            "raw_tag_count",
            "entity_count",
            "geometry_element_count",
            "xdata_tag_count",
            "relationship_count",
            "typed_geometry_entity_count",
            "raw_only_entity_count",
            "unsupported_entity_types",
        ),
        UnsupportedEntityTypeSummary: ("entity_type", "count"),
    }
    for contract, fields in expected_response_fields.items():
        assert tuple(contract.model_fields) == fields
        assert contract.model_config["extra"] == "forbid"


async def test_data_rows_filter_factoring_preserves_operator_values_and_cursor() -> None:
    from fastmcp.tools.function_tool import FunctionTool
    from fastmcp.utilities.json_schema import dereference_refs
    from CAD2BIM_mcp.direct.provider_schema import _apply_query_cursor_xor_schema
    from CAD2BIM_mcp.direct.query.cad_inputs import to_domain_data_rows_query

    tool = await create_direct_mcp(Mock()).get_tool("data_query_rows")
    original = FunctionTool.from_function(tool.fn)
    _apply_query_cursor_xor_schema(original)
    before = Draft202012Validator(dereference_refs(original.parameters))
    after = Draft202012Validator(tool.parameters)
    base = {"direct_session_alias": "direct_001"}
    query = {"package_alias": "data_001", "payload_name": "entities", "select": ["entity_key"]}
    for op in ("eq", "lt", "lte", "gt", "gte", "in", "is_null", "is_not_null"):
        filter_value = {"field": "entity_key", "op": op}
        if op == "in":
            filter_value["value"] = ["a", "b"]
        elif op not in ("is_null", "is_not_null"):
            filter_value["value"] = "a"
        selected = {**query, "filters": [filter_value]}
        payload = {**base, "query": selected}
        assert before.is_valid(payload) and after.is_valid(payload)
        assert to_domain_data_rows_query(selected).filters[0].op == op
        invalid_filter = {**filter_value, "value": {"unexpected": True}}
        invalid = {**base, "query": {**query, "filters": [invalid_filter]}}
        assert not before.is_valid(invalid) and not after.is_valid(invalid)
    for payload, valid in (
        ({**base, "cursor": "cur.saved"}, True),
        ({**base, "query": query, "cursor": "cur.saved"}, False),
        (base, False),
    ):
        assert before.is_valid(payload) == after.is_valid(payload) == valid


async def test_data_rows_complete_usage_examples_match_provider_and_conversion() -> None:
    from CAD2BIM_mcp.tool_docs.data_query import (
        DATA_QUERY_ROWS_FIRST_EXAMPLE, DATA_QUERY_ROWS_CONTINUATION_EXAMPLE,
    )
    from CAD2BIM_mcp.direct.query.cad_inputs import to_domain_data_rows_query

    tool = await create_direct_mcp(Mock()).get_tool("data_query_rows")
    wire = ListToolsResult.model_validate_json(
        ListToolsResult(tools=[tool.to_mcp_tool()]).model_dump_json(by_alias=True)
    ).tools[0]
    validator = Draft202012Validator(wire.inputSchema)
    first = json.loads(DATA_QUERY_ROWS_FIRST_EXAMPLE)
    continuation = json.loads(DATA_QUERY_ROWS_CONTINUATION_EXAMPLE)
    for example in (first, continuation):
        validator.validate(example)
        assert json.dumps(example, separators=(",", ":")) in wire.description
    converted = to_domain_data_rows_query(first["query"])
    assert converted.package_alias == first["query"]["package_alias"]
    assert converted.payload_name == first["query"]["payload_name"]
    assert list(converted.select) == first["query"]["select"]
    assert converted.filters[0].value == 0.5 and converted.page_size == 2
    # Original flat request and the focused single-field relocation remain invalid.
    for fields in (("package_alias", "payload_name", "page_size"), ("package_alias",)):
        misplaced = json.loads(DATA_QUERY_ROWS_FIRST_EXAMPLE)
        for field in fields:
            misplaced[field] = misplaced["query"].pop(field)
        assert not validator.is_valid(misplaced)
        with pytest.raises(ValidationError):
            to_domain_data_rows_query(misplaced["query"])
    assert not validator.is_valid({**first, "cursor": continuation["cursor"]})
    assert not validator.is_valid({"direct_session_alias": first["direct_session_alias"]})


async def test_cad_entities_input_factoring_preserves_modes_defaults_and_cursor_exclusivity() -> None:
    from fastmcp.tools.function_tool import FunctionTool
    from fastmcp.utilities.json_schema import dereference_refs
    from CAD2BIM_mcp.direct.provider_schema import _apply_query_cursor_xor_schema
    from CAD2BIM_mcp.direct.query.cad_inputs import to_domain_cad_entities_query

    server = create_direct_mcp(Mock())
    tool = await server.get_tool("cad_query_entities")
    original_tool = FunctionTool.from_function(tool.fn)
    _apply_query_cursor_xor_schema(original_tool)
    before = Draft202012Validator(dereference_refs(original_tool.parameters))
    after = Draft202012Validator(tool.parameters)
    base = {"direct_session_alias": "direct_001"}
    for count_by in ("entity_type", "layer_and_entity_type", "layer"):
        query = {"package_alias": "data_001", "mode": "count_by", "count_by": count_by}
        for explicit_empty in (False, True):
            candidate = dict(query)
            if explicit_empty:
                candidate["include_declared_empty_layers"] = True
            payload = {**base, "query": candidate}
            expected = not explicit_empty or count_by == "layer"
            assert before.is_valid(payload) == after.is_valid(payload) == expected
            if expected:
                assert to_domain_cad_entities_query(candidate).model_dump(mode="json")["count_by"] == count_by
    rows = {"package_alias": "data_001", "mode": "rows"}
    for payload, expected in (
        ({**base, "query": rows}, True),
        ({**base, "query": {**rows, "count_by": "layer"}}, False),
        ({**base, "cursor": "cur.next"}, True),
        ({**base, "query": None, "cursor": "cur.next"}, True),
        ({**base, "query": rows, "cursor": "cur.next"}, False),
        ({**base, "query": None}, False),
        ({**base, "cursor": None}, False),
        ({**base, "query": rows, "unexpected": 1}, False),
    ):
        assert before.is_valid(payload) == after.is_valid(payload) == expected


async def test_five_phase2_tool_schemas_are_exact_and_read_only() -> None:
    server = create_direct_mcp(Mock())
    tools = {tool.name: tool for tool in await server.list_tools()}
    tools = {name: tool.model_copy(update={"parameters": _expand_shared_input(tool.parameters)})
             for name, tool in tools.items()}

    describe_parameters = tools["data_describe_package"].parameters
    assert tuple(describe_parameters["properties"]) == (
        "direct_session_alias",
        "package_alias",
        "payload_name",
    )
    assert tuple(describe_parameters["required"]) == (
        "direct_session_alias",
        "package_alias",
    )
    assert describe_parameters["additionalProperties"] is False

    for name in (
        "data_describe_package",
        "data_query_rows",
        "cad_query_entities",
        "cad_get_entity",
        "cad_query_xdata",
    ):
        tool = tools[name]
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True

    paged_tools = (
        "data_query_rows",
        "cad_query_entities",
        "cad_get_entity",
        "cad_query_xdata",
    )
    for name in paged_tools:
        parameters = tools[name].parameters
        assert parameters["type"] == "object"
        assert "properties" not in parameters
        assert len(parameters["oneOf"]) == 2
        query_branch, cursor_branch = parameters["oneOf"]
        for branch in (query_branch, cursor_branch):
            assert branch["type"] == "object"
            assert branch["additionalProperties"] is False
            assert tuple(branch["properties"]) == (
                "direct_session_alias",
                "query",
                "cursor",
            )
            assert branch["properties"]["direct_session_alias"]["pattern"] == ("^direct_[0-9]{3,}$")
            assert "direct_session_alias" in branch["required"]

        assert tuple(query_branch["required"]) == ("direct_session_alias", "query")
        assert query_branch["properties"]["cursor"]["type"] == "null"

        assert tuple(cursor_branch["required"]) == ("direct_session_alias", "cursor")
        assert cursor_branch["properties"]["query"]["type"] == "null"
        cursor_schema = cursor_branch["properties"]["cursor"]
        assert cursor_schema["type"] == "string"
        assert cursor_schema["pattern"] == "^cur\..+$"
        assert cursor_schema["maxLength"] == 16_384

    data_query_schema = tools["data_query_rows"].parameters["oneOf"][0]["properties"]["query"]
    assert data_query_schema["type"] == "object"
    assert tuple(data_query_schema["properties"]) == (
        "package_alias",
        "payload_name",
        "select",
        "filters",
        "page_size",
    )
    assert "select" in data_query_schema["required"]
    assert "Required" in data_query_schema["properties"]["select"]["description"]
    assert "logical-key fields" in data_query_schema["properties"]["select"]["description"]
    filter_variants = data_query_schema["properties"]["filters"]["items"]["anyOf"]
    assert len(filter_variants) == 3
    assert filter_variants[0]["properties"]["op"]["anyOf"] == [
        {"const": "eq", "type": "string", "description": "Equality comparison."},
        {"enum": ["lt", "lte", "gt", "gte"], "type": "string",
         "description": "Ordered comparison; boolean and binary payload fields are not orderable."},
    ]
    assert filter_variants[1]["properties"]["op"]["const"] == "in"
    assert filter_variants[2]["properties"]["op"]["enum"] == [
        "is_null",
        "is_not_null",
    ]
    assert "value" not in filter_variants[2]["properties"]

    entity_query_schema = tools["cad_query_entities"].parameters["oneOf"][0]["properties"]["query"]
    entity_variants = entity_query_schema["anyOf"]
    assert len(entity_variants) == 3
    assert [
        variant["properties"].get("count_by", {}).get("const") for variant in entity_variants
    ] == [
        None,
        "layer",
        None,
    ]
    assert entity_variants[2]["properties"]["count_by"]["anyOf"] == [
        {"const": "entity_type", "description": "Group matching entities by DXF entity type.", "type": "string"},
        {"const": "layer_and_entity_type", "description": "Group matching entities by CAD layer and DXF entity type.", "type": "string"},
    ]
    assert "count_by" not in entity_variants[0]["properties"]
    assert "include_declared_empty_layers" not in entity_variants[0]["properties"]
    assert "include_declared_empty_layers" in entity_variants[1]["properties"]
    assert "include_declared_empty_layers" not in entity_variants[2]["properties"]
    for variant in entity_variants:
        assert variant["additionalProperties"] is False
        assert set(variant["properties"]["filters"]["properties"]) == {
            "layers",
            "entity_types",
            "entity_keys",
            "handles",
            "typed_geometry_supported",
        }

    detail_query_schema = tools["cad_get_entity"].parameters["oneOf"][0]["properties"]["query"]
    assert detail_query_schema["type"] == "object"
    assert detail_query_schema["properties"]["section"]["enum"] == [
        "geometry_elements",
        "xdata_tags",
        "relationships",
        "raw_tags",
    ]
    xdata_query_schema = tools["cad_query_xdata"].parameters["oneOf"][0]["properties"]["query"]
    xdata_variants = xdata_query_schema["anyOf"]
    assert len(xdata_variants) == 4
    assert [
        variant["properties"].get("profile_dimension", {}).get("const")
        for variant in xdata_variants
    ] == [None, "appid", "group_code", "assignment_key"]
    assert "xdata_filters" in xdata_variants[0]["properties"]
    assert "profile_dimension" not in xdata_variants[0]["properties"]
    assert "xdata_filters" not in xdata_variants[1]["properties"]
    assert "include_declared_unused_appids" in xdata_variants[1]["properties"]
    assert "include_declared_unused_appids" not in xdata_variants[2]["properties"]
    assert "assignment_keys" in xdata_variants[3]["properties"]
    for variant in xdata_variants:
        assert variant["additionalProperties"] is False
        assert set(variant["properties"]["entity_filters"]["properties"]) == {
            "entity_scope",
            "layers",
            "entity_types",
            "entity_keys",
        }
    assert set(xdata_variants[0]["properties"]["xdata_filters"]["properties"]) == {
        "appids",
        "group_codes",
        "assignment_keys",
        "assignment_values",
    }
    assignment_values_schema = xdata_variants[0]["properties"]["xdata_filters"]["properties"][
        "assignment_values"
    ]
    assignment_array_variants = [
        variant for variant in assignment_values_schema["anyOf"] if variant.get("type") == "array"
    ]
    assert len(assignment_array_variants) == 5
    assert all(variant["maxItems"] == 100 for variant in assignment_array_variants)
    assert "maxLength" not in assignment_values_schema
    assert all("kind" in variant["items"]["required"] for variant in assignment_array_variants)

    def assert_provider_safe(value: object) -> None:
        if isinstance(value, dict):
            assert not ({"allOf", "if", "then", "else", "not"} & value.keys())
            for nested in value.values():
                assert_provider_safe(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_provider_safe(nested)

    for tool in tools.values():
        assert_provider_safe(tool.parameters)

    wire = ListToolsResult(tools=[tool.to_mcp_tool() for tool in await server.list_tools()])
    provider_tools = {tool.name: tool for tool in ListToolsResult.model_validate_json(
        wire.model_dump_json(by_alias=True)).tools}
    for name in paged_tools:
        assert _expand_shared_input(provider_tools[name].inputSchema) == tools[name].parameters

    assert tools["data_describe_package"].return_type is DirectDataDescribePackageResult
    column_schemas: list[dict[str, object]] = []

    def collect_column_schemas(value: object) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if (
                isinstance(properties, dict)
                and {
                    "name",
                    "type",
                    "nullable",
                }
                <= properties.keys()
            ):
                column_schemas.append(value)
            for nested in value.values():
                collect_column_schemas(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_column_schemas(nested)

    collect_column_schemas(tools["data_describe_package"].output_schema)
    assert len(column_schemas) == 1
    assert column_schemas[0]["properties"]["name"]["maxLength"] == 255
    assert column_schemas[0]["properties"]["type"]["maxLength"] == 65_536
    assert tools["data_query_rows"].return_type is DataQueryRowsResult
    assert tools["cad_get_entity"].return_type is CadGetEntityResult
    for name in ("cad_query_entities", "cad_query_xdata"):
        assert tools[name].output_schema["type"] == "object"
        assert len(tools[name].output_schema["oneOf"]) == 2
        assert "x-fastmcp-wrap-result" not in tools[name].output_schema
        assert "result" not in tools[name].output_schema.get("properties", {})
    assert tuple(DirectDataDescribePackageResult.model_fields) == (
        "package",
        "producing_operation_alias",
        "payloads",
        "payload_schema",
    )
    assert tuple(DataQueryRowsResult.model_fields) == (
        "package_alias",
        "payload_name",
        "logical_key_fields",
        "rows",
        "page",
    )
    assert tuple(CadEntityRowsResult.model_fields) == (
        "package_alias",
        "mode",
        "rows",
        "page",
    )
    assert tuple(CadEntityCountResult.model_fields) == (
        "package_alias",
        "mode",
        "count_by",
        "scope",
        "counts",
        "page",
    )
    assert tuple(CadXdataRowsResult.model_fields) == (
        "package_alias",
        "mode",
        "rows",
        "page",
    )
    assert tuple(CadXdataProfileResult.model_fields) == (
        "package_alias",
        "mode",
        "profile_dimension",
        "scope",
        "profiles",
        "page",
    )
    assert tuple(PageInfo.model_fields) == (
        "total_matched_count",
        "returned_count",
        "remaining_count",
        "complete",
        "next_cursor",
    )


async def test_operation_list_dispatches_every_advertised_kind_to_domain_enum() -> None:
    result = OperationListResult(operations=(), complete=True, next_before_sequence=None)
    application = SimpleNamespace(list_operations=AsyncMock(return_value=result))
    server = FastMCP("operation-kind-filter")
    register_operation_list(server, application)

    async with Client(server) as client:
        for operation_kind in CriticalOperationKind:
            response = await client.call_tool(
                "operation_list",
                {
                    "direct_session_alias": "direct_001",
                    "operation_kind": operation_kind.value,
                },
            )
            assert response.structured_content == result.model_dump(mode="json")

    assert application.list_operations.await_count == len(CriticalOperationKind)
    assert [call.args[3] for call in application.list_operations.await_args_list] == list(
        CriticalOperationKind
    )


async def test_operation_reads_preserve_current_family_reference_error() -> None:
    from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogReferenceError
    from CAD2BIM_mcp.direct.errors import DirectApplicationError

    error = ProductFamilyCatalogReferenceError("No visible current Family contract")
    application = SimpleNamespace(
        list_operation_catalog=AsyncMock(side_effect=error),
        get_operation=AsyncMock(side_effect=error),
    )

    class Adapter(operation_application.OperationApplicationMixin):
        _application = application

        async def _run_runtime_read(self, _alias, _operation, action):
            return await action(object())

    for read in (
        Adapter().list_operations("direct_001"),
        Adapter().get_operation("direct_001", "op_001"),
    ):
        with pytest.raises(DirectApplicationError) as caught:
            await read
        assert caught.value.code == "PRODUCT_FAMILY_REFERENCE_INVALID"
        assert caught.value.phase == "read"
        assert caught.value.retryable is False


async def test_m2c_provider_schemas_are_complete_closed_and_domain_safe() -> None:
    tools = {tool.name: tool for tool in await create_direct_mcp(Mock()).list_tools()}
    tools = {name: tool.model_copy(update={"parameters": _expand_shared_input(tool.parameters)})
             for name, tool in tools.items()}

    catalog = tools["data_package_list"].parameters
    assert tuple(catalog["properties"]) == (
        "direct_session_alias",
        "before_sequence",
        "page_size",
    )
    assert catalog["properties"]["before_sequence"]["anyOf"] == [
        {"maximum": 9_223_372_036_854_775_807, "minimum": 1, "type": "integer"},
        {"type": "null"},
    ]
    assert catalog["properties"]["page_size"]["default"] == 20
    assert catalog["properties"]["page_size"]["maximum"] == 50

    operation_list, recovery_list = tools["operation_list"].parameters["anyOf"]
    assert set(operation_list["properties"]) == {
        "tool_name",
        "outcome",
        "direct_session_alias",
        "before_sequence",
        "page_size",
        "operation_kind",
        "mode",
    }
    assert recovery_list["properties"]["mode"]["const"] == "ifc_products_recovery"
    assert set(recovery_list["properties"]) == {"mode", "tool_name", "cursor", "page_size", "direct_session_alias"}
    operation_kind_schema = operation_list["properties"]["operation_kind"]["anyOf"]
    assert operation_kind_schema[0]["enum"] == [kind.value for kind in CriticalOperationKind]
    assert operation_kind_schema[1] == {"type": "null"}
    get_branches = tools["operation_get"].parameters["anyOf"]
    assert [branch["properties"]["mode"]["const"] for branch in get_branches] == [
        "preview_report", "preview_report",
        "operation", "operation_reference", "inspection", *(["transform_input"] * 7),
    ]

    transform = tools["data_derive"].parameters
    def expanded(node):
        if isinstance(node, list):
            return [expanded(value) for value in node]
        if isinstance(node, dict):
            if "$ref" in node:
                return expanded(transform["$defs"][node["$ref"].rsplit("/", 1)[-1]])
            return {key: expanded(value) for key, value in node.items()}
        return node
    transform = expanded(transform)
    assert transform["type"] == "object"
    assert len(transform["oneOf"]) == 4
    create, python_create, ifc_create, repair = transform["oneOf"]
    assert ifc_create["properties"]["ifc_source"]["const"] == "current"
    assert "ifc_source" in ifc_create["required"]
    assert ifc_create["properties"]["inputs"].get("minItems", 0) == 0
    assert create["properties"]["mode"]["const"] == "create"
    assert repair["properties"]["mode"]["const"] == "repair"
    assert [branch["properties"]["source_kind"]["const"]
            for branch in repair["properties"]["source"]["oneOf"]] == [
        "failed_operation", "succeeded_package",
    ]
    properties = create["properties"]
    assert properties["inputs"]["minItems"] == 1
    assert properties["inputs"]["maxItems"] == 16
    assert properties["inputs"]["items"]["properties"]["payload_name"]["maxLength"] == 120
    assert properties["inline_tables"]["default"] == []
    assert properties["inline_tables"]["maxItems"] == 8
    inline_table = properties["inline_tables"]["items"]
    assert inline_table["properties"]["columns"]["maxItems"] == 32
    assert inline_table["properties"]["rows"]["maxItems"] == 500
    def resolved(node):
        while "$ref" in node:
            node = transform["$defs"][node["$ref"].rsplit("/", 1)[-1]]
        return node
    outputs = resolved(properties["outputs"]["properties"]["value"])
    assert outputs["minItems"] == 1
    assert outputs["maxItems"] == 16
    markdown_description = properties["output_package"]["properties"]["description_markdown"][
        "description"
    ]
    assert "exactly one H1" in markdown_description
    assert "Summary" in markdown_description
    assert "Known limitations" in markdown_description
    output_items = resolved(outputs["items"])
    output_variants = [resolved(variant) for variant in output_items.get("oneOf", output_items.get("anyOf"))]
    assert [variant["properties"]["format"]["const"] for variant in output_variants] == [
        "parquet",
        "json",
    ]
    assert "schema_json" not in output_variants[0]["properties"]
    assert output_variants[1]["properties"]["schema_json"]["maxLength"] == 6 * 65_536 + 128
    sql = properties["execution"]
    python = python_create["properties"]["execution"]
    assert sql["properties"]["engine"]["const"] == "duckdb_sql"
    assert python["properties"]["engine"]["const"] == "python_arrow"
    assert sql["properties"]["code"]["maxLength"] == 131_072
    assert python["properties"]["code"]["maxLength"] == 131_072
    parameter_variants = properties["parameters"]["items"]["anyOf"]
    assert [variant["properties"]["kind"]["const"] for variant in parameter_variants] == [
        "string",
        "integer",
        "number",
        "boolean",
        "string_list",
        "integer_list",
        "number_list",
        "boolean_list",
    ]

    query_sql = tools["data_query_sql"].parameters
    assert query_sql["type"] == "object"
    assert "properties" not in query_sql
    assert len(query_sql["oneOf"]) == 3
    first_page, repair, continuation = query_sql["oneOf"]
    assert tuple(first_page["properties"]) == ("direct_session_alias", "query", "mode")
    assert tuple(first_page["required"]) == ("direct_session_alias", "query")
    assert first_page["additionalProperties"] is False
    assert repair["properties"]["mode"]["const"] == "repair"
    assert tuple(continuation["properties"]) == ("direct_session_alias", "cursor", "mode")
    assert tuple(continuation["required"]) == ("direct_session_alias", "cursor")
    assert continuation["additionalProperties"] is False
    query_schema = first_page["properties"]["query"]
    assert tuple(query_schema["properties"]) == (
        "inputs",
        "parameters",
        "execution",
        "page_size",
    )
    assert tuple(query_schema["required"]) == ("inputs", "execution")
    assert query_schema["properties"]["page_size"]["default"] == 100
    assert query_schema["properties"]["page_size"]["maximum"] == 200
    query_code_description = query_schema["properties"]["execution"]["properties"]["code"][
        "description"
    ]
    assert "final statement" in query_code_description
    assert "declared output" not in query_code_description
    assert continuation["properties"]["cursor"]["maxLength"] == 16_384
    wire = ListToolsResult(tools=[tool.to_mcp_tool() for tool in
                                 await create_direct_mcp(Mock()).list_tools()])
    sdk_tools = {tool.name: tool for tool in ListToolsResult.model_validate_json(
        wire.model_dump_json(by_alias=True)).tools}
    assert _expand_shared_input(sdk_tools["data_derive"].inputSchema) == transform
    assert "exactly one H1" in sdk_tools["data_derive"].description
    assert "Summary is the first H2" in sdk_tools["data_derive"].description
    assert _expand_shared_input(sdk_tools["data_query_sql"].inputSchema) == query_sql

    read_json = tools["data_read_json"].parameters
    assert read_json["type"] == "object"
    assert "properties" not in read_json
    assert len(read_json["anyOf"]) == 4
    value_branch, children_branch, records_branch, json_continuation = read_json["anyOf"]
    for branch in (value_branch, children_branch, records_branch):
        assert tuple(branch["properties"]) == ("direct_session_alias", "query")
        assert tuple(branch["required"]) == ("direct_session_alias", "query")
        assert branch["additionalProperties"] is False
        assert branch["properties"]["query"]["additionalProperties"] is False
    assert tuple(value_branch["properties"]["query"]["properties"]) == (
        "package_alias",
        "payload_name",
        "mode",
        "pointer",
    )
    assert value_branch["properties"]["query"]["properties"]["mode"]["const"] == "value"
    assert value_branch["properties"]["query"]["properties"]["pointer"]["maxLength"] == 4_096
    children_query = children_branch["properties"]["query"]
    assert children_query["properties"]["mode"]["const"] == "children"
    assert children_query["properties"]["page_size"]["default"] == 50
    assert children_query["properties"]["page_size"]["maximum"] == 100
    records_query = records_branch["properties"]["query"]
    assert records_query["properties"]["mode"]["const"] == "records"
    assert "pointer" not in records_query["properties"]
    assert records_query["properties"]["page_size"]["default"] == 100
    assert records_query["properties"]["page_size"]["maximum"] == 200
    assert tuple(json_continuation["properties"]) == ("direct_session_alias", "cursor")
    assert tuple(json_continuation["required"]) == ("direct_session_alias", "cursor")
    assert json_continuation["additionalProperties"] is False
    assert json_continuation["properties"]["cursor"]["maxLength"] == 16_384
    assert _expand_shared_input(sdk_tools["data_read_json"].inputSchema) == read_json

    code_list = tools["code_list"].parameters
    assert len(code_list["oneOf"]) == 2
    first_page, continuation = code_list["oneOf"]
    assert first_page["properties"]["mode"]["const"] == "first"
    code_kind_branches = first_page["properties"]["query"]["oneOf"]
    assert len(code_kind_branches) == 5
    branches_by_kind = {}
    for branch in code_kind_branches:
        kind = branch["properties"]["code_kind"]
        for variant in kind.get("oneOf", [kind]):
            assert variant["const"] not in branches_by_kind
            branches_by_kind[variant["const"]] = branch
    assert set(branches_by_kind) == {
        "data_transform_attempt",
        "cad_measurement_implementation",
        "data_query_sql_attempt",
        "data_query_python_attempt",
        "geometry_authoring_attempt",
        "geometry_resource_program",
        "geometry_query_attempt",
        "ifc_query_attempt",
        "product_family_authoring_attempt",
        "product_family_realization_program",
        "geometry_draft",
        "product_family_draft",
    }
    for branch in code_kind_branches:
        if "draft_ref" in branch["properties"]:
            assert "draft_ref" in branch["required"]
            assert "page_size" not in branch["properties"]
            continue
        page_size = branch["properties"]["page_size"]
        assert page_size["default"] == 20
        assert page_size["minimum"] == 1
        assert page_size["maximum"] == 50
    for kind, branch in branches_by_kind.items():
        assert ("statuses" in branch["properties"]) == (kind not in {
            "geometry_resource_program", "product_family_realization_program",
            "geometry_draft", "product_family_draft",
        })
    assert continuation["properties"]["mode"]["const"] == "cursor"

    code_read = tools["code_read"].parameters
    assert code_read["type"] == "object"
    assert len(code_read["anyOf"]) == 3
    single_text, bundle, batch = code_read["anyOf"]
    assert single_text["properties"]["source_form"]["const"] == "single_text"
    assert "source_path" not in single_text["properties"]
    assert bundle["properties"]["source_form"]["const"] == "bundle"
    assert "source_path" in bundle["properties"]
    assert [
        branch["properties"]["view_mode"]["const"]
        for branch in single_text["properties"]["view"]["oneOf"]
    ] == ["full", "lines", "search"]
    assert tuple(batch["properties"]) == ("direct_session_alias", "mode", "reads")
    assert batch["properties"]["mode"]["const"] == "batch"
    assert batch["properties"]["reads"]["minItems"] == 1
    assert batch["properties"]["reads"]["maxItems"] == 8
    batch_read_branches = batch["properties"]["reads"]["items"]["oneOf"]
    assert [branch["properties"]["source_form"]["const"] for branch in batch_read_branches] == [
        "single_text",
        "bundle",
    ]
    assert "source_path" not in batch_read_branches[0]["properties"]
    assert "source_path" in batch_read_branches[1]["properties"]

    transform_repair = transform["oneOf"][-1]
    assert tuple(transform_repair["properties"]) == (
        "mode", "direct_session_alias", "edits", "outputs", "output_package", "source",
    )
    assert "edits" not in transform_repair["required"]
    assert "expected_source_sha256" not in transform_repair["properties"]
    edits = transform_repair["properties"]["edits"]["anyOf"][0]
    assert edits["minItems"] == 1
    assert edits["maxItems"] == 16
    edit_items = edits["items"]
    assert tuple(edit_items["properties"]) == ("old_text", "new_text")
    assert edit_items["additionalProperties"] is False
    failed_source, package_source = transform_repair["properties"]["source"]["oneOf"]
    assert tuple(failed_source["properties"]) == (
        "source_kind",
        "operation_alias",
    )
    assert (
        failed_source["properties"]["source_kind"]["const"]
        == "failed_operation"
    )
    assert tuple(package_source["properties"]) == (
        "source_kind",
        "package_alias",
    )
    assert (
        package_source["properties"]["source_kind"]["const"]
        == "succeeded_package"
    )
    assert "failed_operation_alias" not in repr(transform)
    assert _expand_shared_input(sdk_tools["code_list"].inputSchema) == code_list
    assert _expand_shared_input(sdk_tools["code_read"].inputSchema) == code_read
    assert _expand_shared_input(sdk_tools["data_derive"].inputSchema) == transform

    def assert_provider_safe_and_closed(value: object) -> None:
        if isinstance(value, dict):
            assert value != {}, "Provider-visible Schema must not contain open Any nodes"
            assert value.get("type") != "unknown"
            assert "unknown" not in value
            assert not ({"allOf", "if", "then", "else", "not"} & value.keys())
            if value.get("type") == "object" and "properties" in value:
                assert value.get("additionalProperties") is False
            for nested in value.values():
                assert_provider_safe_and_closed(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_provider_safe_and_closed(nested)
        elif isinstance(value, str):
            assert value.lower() != "unknown"

    for tool in tools.values():
        Draft202012Validator.check_schema(tool.parameters)
        assert_provider_safe_and_closed(tool.parameters)
    for name in (
        "data_package_list",
        "operation_list",
        "operation_get",
        "data_derive",
        "data_read_json",
        "code_list",
        "code_read",
    ):
        output_schema = tools[name].output_schema
        assert output_schema is not None
        Draft202012Validator.check_schema(output_schema)
        assert_provider_safe_and_closed(output_schema)

    for contract in (
        SourceL0CatalogPackageCard,
        DerivedCatalogPackageCard,
        SucceededSourceIngestionOperationCard,
        FailedSourceIngestionOperationCard,
        SucceededDataTransformOperationCard,
        FailedDataTransformOperationCard,
        SucceededOperationGetResult,
        FailedOperationGetResult,
        DataTransformResult,
        MultiOutputDataTransformResult,
        OperationListResult,
    ):
        assert contract.model_config["extra"] == "forbid"
        assert contract.model_config["allow_inf_nan"] is False

    transform_output_schema = tools["data_derive"].output_schema
    assert transform_output_schema is not None
    assert transform_output_schema["type"] == "object"
    assert len(transform_output_schema["anyOf"]) == 4
    single_response, multiple_response, repair_single, repair_multiple = transform_output_schema[
        "anyOf"
    ]
    assert "output" in single_response["properties"]
    assert "outputs" not in single_response["properties"]
    assert "outputs" in multiple_response["properties"]
    assert "output" not in multiple_response["properties"]
    assert multiple_response["properties"]["outputs"]["minItems"] == 2
    assert multiple_response["properties"]["outputs"]["maxItems"] == 16
    assert "repair_parent_operation_alias" in repair_single["properties"]
    assert "repair_parent_operation_alias" in repair_multiple["properties"]

    query_output_schema = tools["data_query_sql"].output_schema
    assert query_output_schema is not None
    Draft202012Validator.check_schema(query_output_schema)
    assert tuple(query_output_schema["properties"]) == (
        "row_schema",
        "rows",
        "page",
        "query_statistics",
    )
    values_schema = query_output_schema["properties"]["rows"]["items"]["properties"]["values"]
    scalar_variants = values_schema["additionalProperties"]["anyOf"]
    assert [variant["type"] for variant in scalar_variants] == [
        "boolean",
        "integer",
        "number",
        "string",
        "null",
    ]

    assert "source_filename" in SourceL0CatalogPackageCard.model_fields
    assert "producing_operation_alias" in SourceL0CatalogPackageCard.model_fields
    assert not SourceL0CatalogPackageCard.model_fields["producing_operation_alias"].is_required()
    assert "producing_operation_alias" in DerivedCatalogPackageCard.model_fields
    assert "source_filename" not in DerivedCatalogPackageCard.model_fields
    for contract in (
        SucceededSourceIngestionOperationCard,
        SucceededDataTransformOperationCard,
    ):
        assert "primary_result" in contract.model_fields
        assert "error_code" not in contract.model_fields
    for contract in (
        FailedSourceIngestionOperationCard,
        FailedDataTransformOperationCard,
    ):
        assert "error_code" in contract.model_fields
        assert "primary_result" not in contract.model_fields
    assert "error" not in SucceededOperationGetResult.model_fields
    assert "error" in FailedOperationGetResult.model_fields
    source_detail_schemas: list[dict[str, object]] = []
    operation_detail_properties: dict[str, dict[str, object]] = {}

    def collect_source_detail_schemas(value: object) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                kind = properties.get("detail_kind", {}).get("const")
                if kind:
                    operation_detail_properties[kind] = properties
            if (
                isinstance(properties, dict)
                and {
                    "filename",
                    "media_type",
                    "size_bytes",
                }
                <= properties.keys()
            ):
                source_detail_schemas.append(value)
            for nested in value.values():
                collect_source_detail_schemas(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_source_detail_schemas(nested)

    collect_source_detail_schemas(tools["operation_get"].output_schema)
    for kind, omitted, retained in (
        ("authoring_draft_write", {"worker_profile", "budget_profile", "diagnostic_summary", "repair_eligible", "source_bundle_sha256"}, {"result", "root_kind", "draft_ref", "edit_token", "code_available"}),
        ("product_family_write", {"runtime_profile", "budget_profile", "validation_scenario_count", "repair_eligible"}, {"mode", "program_entry", "source_bundle_sha256", "code_available"}),
    ):
        assert not omitted & operation_detail_properties[kind].keys()
        assert retained <= operation_detail_properties[kind].keys()
    assert source_detail_schemas
    for source_schema in source_detail_schemas:
        assert "size_bytes" in source_schema["required"]
        size_schema = source_schema["properties"]["size_bytes"]
        assert size_schema["anyOf"] == [
            {"maximum": 9_223_372_036_854_775_807, "minimum": 1, "type": "integer"},
            {"type": "null"},
        ]
        assert "retained failed Source Ingestion" in size_schema["description"]
        assert "no recorded original byte-size fact" in size_schema["description"]
        assert "otherwise the value is a positive integer" in size_schema["description"]

    assert (
        SourceIngestionSourceDetail(
            filename="legacy-failure.dxf",
            media_type="application/dxf",
            size_bytes=None,
        ).size_bytes
        is None
    )
    assert (
        SourceIngestionSourceDetail(
            filename="new-operation.dxf",
            media_type="application/dxf",
            size_bytes=1,
        ).size_bytes
        == 1
    )
    with pytest.raises(ValidationError):
        SourceIngestionSourceDetail(
            filename="invalid.dxf",
            media_type="application/dxf",
            size_bytes=0,
        )

    request = DirectDataTransformInput.model_validate(
        {
            "mode": "create",
            "direct_session_alias": "direct_001",
            "inputs": [
                {
                    "input_name": "entities",
                    "package_alias": "data_001",
                    "payload_name": "entities",
                }
            ],
            "parameters": [
                {"name": "s", "kind": "string", "value": "x"},
                {"name": "i", "kind": "integer", "value": 1},
                {"name": "n", "kind": "number", "value": 1.5},
                {"name": "b", "kind": "boolean", "value": True},
                {"name": "ss", "kind": "string_list", "value": ["x"]},
                {"name": "ii", "kind": "integer_list", "value": [1]},
                {"name": "nn", "kind": "number_list", "value": [1.5]},
                {"name": "bb", "kind": "boolean_list", "value": [True]},
            ],
            "execution": {"engine": "duckdb_sql", "code": "SELECT * FROM entities"},
            "outputs": {"action": "set", "value": [
                {
                    "payload_name": "selected_entities",
                    "format": "parquet",
                    "description": "Selected entity facts",
                    "logical_key_fields": ["entity_key"],
                }
            ]},
            "output_package": {
                "name": "selected_entities_v1",
                "description_markdown": {"action": "set", "value": "# Selected\n## Summary\nSelected entity facts."},
            },
        }
    )
    command = to_domain_data_transform_command(request)
    assert not hasattr(command, "direct_session_alias")
    assert [item.kind for item in command.parameters] == [
        "string",
        "integer",
        "number",
        "boolean",
        "string_list",
        "integer_list",
        "number_list",
        "boolean_list",
    ]

    multi = request.model_dump(mode="python")
    multi["outputs"]["value"] = [
        *multi["outputs"]["value"],
        {
            "payload_name": "selected_layers",
            "format": "parquet",
            "description": "Selected layer facts",
            "logical_key_fields": ["layer"],
        },
    ]
    multi_request = DirectDataTransformInput.model_validate(multi)
    multi_command = to_domain_data_transform_command(multi_request)
    assert [item.payload_name for item in multi_command.outputs] == [
        "selected_entities",
        "selected_layers",
    ]

    duplicate_output = multi_request.model_dump(mode="python")
    duplicate_output["outputs"]["value"][1]["payload_name"] = "selected_entities"
    with pytest.raises(ValidationError, match="must be unique"):
        DirectDataTransformInput.model_validate(duplicate_output)
    colliding_output = multi_request.model_dump(mode="python")
    colliding_output["outputs"]["value"][1]["payload_name"] = "entities"
    with pytest.raises(ValidationError, match="must not collide"):
        DirectDataTransformInput.model_validate(colliding_output)

    first_page = validate_direct_data_query_sql_input(
        {
            "direct_session_alias": "direct_001",
            "mode": "fresh",
            "query": {
                "inputs": [
                    {
                        "input_name": "entities",
                        "package_alias": "data_001",
                        "payload_name": "entities",
                    }
                ],
                "execution": {
                    "engine": "duckdb_sql",
                    "code": "SELECT entity_key FROM entities ORDER BY entity_key",
                },
            },
        }
    )
    assert isinstance(first_page, QuerySqlFirstPageInput)
    query_command = to_domain_data_query_sql_query(first_page.query)
    assert isinstance(query_command, DataQuerySqlQuery)
    assert query_command.page_size == 100
    continuation = validate_direct_data_query_sql_input(
        {"direct_session_alias": "direct_001", "mode": "cursor", "cursor": "cur.exact"}
    )
    assert isinstance(continuation, QuerySqlContinuationInput)
    with pytest.raises(ValidationError):
        validate_direct_data_query_sql_input(
            {
                "direct_session_alias": "direct_001",
                "mode": "fresh",
                "query": first_page.query,
                "cursor": "cur.exact",
            }
        )

    invalid = request.model_dump(mode="python")
    invalid["parameters"] = [{"name": "bad", "kind": "integer", "value": True}]
    with pytest.raises(ValidationError):
        DirectDataTransformInput.model_validate(invalid)
    invalid["parameters"] = [{"name": "bad", "kind": "number", "value": "1.0"}]
    with pytest.raises(ValidationError):
        DirectDataTransformInput.model_validate(invalid)
    invalid["parameters"] = []
    invalid["execution"] = {"engine": "duckdb_sql", "code": "中" * 43_691}
    with pytest.raises(ValidationError, match="UTF-8"):
        DirectDataTransformInput.model_validate(invalid)


def test_transform_success_projection_preserves_single_and_multi_output_shapes() -> None:
    package_id = "package-id"
    package = SimpleNamespace(
        package=SimpleNamespace(
            package=SimpleNamespace(
                package_kind=DataPackageKind.DERIVED,
                package_id=package_id,
                name="derived",
            ),
            catalog_entry=SimpleNamespace(alias="data_002"),
        ),
        operation=SimpleNamespace(
            operation=SimpleNamespace(
                status=CriticalOperationStatus.SUCCEEDED,
                operation_kind=CriticalOperationKind.DATA_TRANSFORM,
                result_package_id=package_id,
                detail=None,
            ),
            catalog_entry=SimpleNamespace(alias="op_002"),
        ),
    )

    def member(name: str, rows: int) -> SimpleNamespace:
        return SimpleNamespace(
            package_id=package_id,
            member_kind=PackageMemberKind.PAYLOAD,
            logical_payload_name=name,
            media_type="application/vnd.apache.parquet",
            row_count=rows,
            logical_key_fields=(f"{name}_key",),
        )

    def detail(*names: str, ifc_source=None) -> DomainDataTransformOperationDetail:
        return DomainDataTransformOperationDetail(
            engine="python_arrow" if ifc_source is not None else "duckdb_sql",
            ifc_source=ifc_source,
            inputs=(
                TransformInputBindingRecord(
                    ordinal=1,
                    input_name="entities",
                    package_id="source-package",
                    member_id="source-member",
                    payload_name="entities",
                ),
            ),
            outputs=tuple(
                TransformOutputRecord(
                    payload_name=name,
                    description=f"{name} facts",
                    logical_key_fields=(f"{name}_key",),
                )
                for name in names
            ),
            output_package_name="derived",
        )

    package.operation.operation.detail = detail("products")
    single = transform_application._data_transform_result(
        SimpleNamespace(published=package, output_members=(member("products", 2),))
    )
    assert isinstance(single, DataTransformResult)
    assert single.output.payload_name == "products"
    assert "outputs" not in type(single).model_fields

    package.operation.operation.detail = detail("products", "ports")
    multiple = transform_application._data_transform_result(
        SimpleNamespace(
            published=package,
            output_members=(member("products", 2), member("ports", 4)),
        )
    )
    assert isinstance(multiple, MultiOutputDataTransformResult)
    assert [item.payload_name for item in multiple.outputs] == ["products", "ports"]
    assert "output" not in type(multiple).model_fields

    from CAD2BIM_backend.domain.data_runtime import IfcReadSourceRef
    source = IfcReadSourceRef(model_id="server-model", revision_id="server-revision", revision_no=7)
    for names in [("products",), ("products", "ports")]:
        package.operation.operation.detail = detail(*names, ifc_source=source)
        projected = transform_application._data_transform_result(SimpleNamespace(
            published=package, output_members=tuple(member(name, 2) for name in names),
        ))
        assert projected.model_dump(mode="json")["ifc_source"] == {"revision_no": 7}
        assert "server-model" not in projected.model_dump_json()
        assert "server-revision" not in projected.model_dump_json()

    package.operation.operation.detail = detail("ports", "products")
    with pytest.raises(ValueError, match="output order"):
        transform_application._data_transform_result(
            SimpleNamespace(
                published=package,
                output_members=(member("products", 2), member("ports", 4)),
            )
        )


def _apply_report_catalog_fixture(outcome):
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection

    inspection = _apply_inspection(outcome)
    report = inspection.result_package
    member = inspection.input_packages["products"].members[0]
    # Reuse the actual typed Apply operation fixture; card projection needs only
    # already-persisted description and payload metadata, never report bytes.
    report = report.model_copy(update={
        "package": report.package.model_copy(update={
            "description_title": "Product Apply report", "section_titles": ("Summary",)}),
        "members": tuple(member.model_copy(update={"member_id": f"report_{name}",
            "package_id": report.package.package_id, "logical_payload_name": name,
            "member_name": f"payloads/{name}/part-00000.parquet"})
            for name in ("products", "components", "interfaces", "checks")),
    })
    return inspection, report


@pytest.mark.parametrize("outcome", ["committed", "rejected"])
async def test_catalog_lists_terminal_apply_report_with_existing_derived_card(outcome) -> None:
    inspection, report = _apply_report_catalog_fixture(outcome)
    application = SimpleNamespace(list_package_catalog=AsyncMock(return_value=SimpleNamespace(
        packages=(report,), producing_operations={report.package.package_id: inspection.operation},
        complete=True, next_before_sequence=None)))
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(identity=object())))
    facade = DirectApplicationFacade(application, Mock(), resolver, lambda _identity: nullcontext())

    result = await facade.list_data_packages("direct_001")

    assert len(result.packages) == 1
    card = result.packages[0]
    assert isinstance(card, DerivedCatalogPackageCard)
    assert card.package_alias == report.catalog_entry.alias
    assert card.producing_operation_alias == inspection.operation.catalog_entry.alias
    assert [(item.payload_name, item.row_count) for item in card.payloads] == [
        ("checks", 1), ("components", 1), ("interfaces", 1), ("products", 1)]
    assert result.complete and result.next_before_sequence is None


@pytest.mark.parametrize("corruption", ["missing_producer", "different_package", "unrelated_ifc_detail"])
def test_catalog_apply_report_still_requires_its_exact_typed_producer(corruption) -> None:
    from CAD2BIM_mcp.direct.catalog.application import _catalog_package_card
    from tests.unit.backend.test_ifc_model_operation_projection import _detail, _operation, _published_geometry_authoring

    inspection, report = _apply_report_catalog_fixture("rejected")
    producer = inspection.operation
    if corruption == "missing_producer":
        producer = None
    elif corruption == "different_package":
        # Another lawful Apply report is not this Package's producer.
        producer = producer.model_copy(update={"operation": producer.operation.model_copy(
            update={"result_package_id": "another_report"})})
    else:
        producer = _published_geometry_authoring(_operation(status=CriticalOperationStatus.SUCCEEDED,
            detail=_detail(result="model_initialized"), finished_at=datetime.now(timezone.utc),
            result_ifc_revision_id="initialized_revision"))
    with pytest.raises(ValueError, match="no producing Operation|producing Operation is inconsistent"):
        _catalog_package_card(report, producer)


def test_transform_success_projection_supports_mixed_parquet_and_json_outputs() -> None:
    package_id = "package-id"
    detail = DomainDataTransformOperationDetail(
        engine="python_arrow",
        inputs=(
            TransformInputBindingRecord(
                ordinal=1,
                input_name="entities",
                package_id="source-package",
                member_id="source-member",
                payload_name="entities",
            ),
        ),
        outputs=(
            TransformOutputRecord(
                payload_name="products",
                format="parquet",
                description="Product facts",
                logical_key_fields=("product_key",),
            ),
            TransformOutputRecord(
                payload_name="summary",
                format="json",
                description="Topology summary",
                json_kind=JsonPayloadKind.DOCUMENT,
            ),
        ),
        output_package_name="derived",
    )
    published = SimpleNamespace(
        package=SimpleNamespace(
            package=SimpleNamespace(
                package_kind=DataPackageKind.DERIVED,
                package_id=package_id,
                name="derived",
            ),
            catalog_entry=SimpleNamespace(alias="data_002"),
        ),
        operation=SimpleNamespace(
            operation=SimpleNamespace(
                status=CriticalOperationStatus.SUCCEEDED,
                operation_kind=CriticalOperationKind.DATA_TRANSFORM,
                result_package_id=package_id,
                detail=detail,
            ),
            catalog_entry=SimpleNamespace(alias="op_002"),
        ),
    )
    parquet = SimpleNamespace(
        package_id=package_id,
        member_kind=PackageMemberKind.PAYLOAD,
        logical_payload_name="products",
        media_type="application/vnd.apache.parquet",
        row_count=2,
        logical_key_fields=("product_key",),
    )
    json_output = SimpleNamespace(
        package_id=package_id,
        member_kind=PackageMemberKind.PAYLOAD,
        logical_payload_name="summary",
        media_type="application/json",
        logical_byte_count=81,
        json_kind=JsonPayloadKind.DOCUMENT,
    )

    result = transform_application._data_transform_result(
        SimpleNamespace(
            published=published,
            output_members=(parquet, json_output),
        )
    )

    assert isinstance(result, MultiOutputDataTransformResult)
    assert result.outputs[0].format == "parquet"
    assert isinstance(result.outputs[1], JsonDataTransformOutputResult)
    assert result.outputs[1].model_dump(mode="json") == {
        "payload_name": "summary",
        "format": "json",
        "json_kind": "document",
        "size_bytes": 81,
    }


@pytest.mark.parametrize("tool_name", ["data_derive", "data_transform", "data_repair_transform"])
def test_operation_projection_preserves_python_engine_and_mixed_output_kinds(tool_name: str) -> None:
    package_id = "derived-package"
    source_id = "source-package"
    detail = DomainDataTransformOperationDetail(
        engine="python_arrow",
        ifc_source={"model_id": "server-model", "revision_id": "server-revision", "revision_no": 7},
        inline_tables=({
            "input_name": "limits",
            "columns": [{"name": "sha256", "type": "string", "nullable": False}, {"name": "digest", "type": "integer", "nullable": False}],
            "rows": [{"values": [{"kind": "string", "value": "business-source" * 40}, {"kind": "integer", "value": 7}]}] * 500,
        },),
        inputs=(
            TransformInputBindingRecord(
                ordinal=1,
                input_name="entities",
                package_id=source_id,
                member_id="source-member",
                payload_name="entities",
            ),
        ),
        outputs=(
            TransformOutputRecord(
                payload_name="products",
                format="parquet",
                description="Product facts",
                logical_key_fields=("product_key",),
            ),
            TransformOutputRecord(
                payload_name="summary",
                format="json",
                description="Topology summary",
                json_kind=JsonPayloadKind.DOCUMENT,
            ),
        ),
        output_package_name="derived",
    )
    now = datetime.now(timezone.utc)
    result_package = SimpleNamespace(
        package=SimpleNamespace(package_id=package_id, name="derived"),
        catalog_entry=SimpleNamespace(alias="data_002"),
    )
    source_package = SimpleNamespace(
        package=SimpleNamespace(package_id=source_id),
        catalog_entry=SimpleNamespace(alias="data_001"),
        members=(SimpleNamespace(member_id="source-member", logical_key_fields=("entity_id",)),),
    )
    operation = SimpleNamespace(
        operation=SimpleNamespace(
            operation_kind=CriticalOperationKind.DATA_TRANSFORM,
            tool_name=tool_name,
            initiator=CriticalOperationInitiator.DIRECT_CLIENT,
            status=CriticalOperationStatus.SUCCEEDED,
            result_package_id=package_id,
            intent_summary="Derived facts",
            started_at=now,
            finished_at=now,
            detail=detail,
            error_code=None,
        ),
        catalog_entry=SimpleNamespace(alias="op_002", ordinal=2),
    )

    projected = operation_application._operation_get_result(
        SimpleNamespace(
            operation=operation,
            result_package=result_package,
            input_packages={"entities": source_package},
        ), code_locator="cur.issued-original-code" if tool_name == "data_derive" else None,
    )

    assert isinstance(projected, SucceededOperationGetResult)
    assert projected.operation.tool_name == tool_name
    assert projected.detail.code_available is (tool_name == "data_derive")
    assert projected.detail.engine == "python_arrow"
    assert projected.detail.model_dump(mode="json")["ifc_source"] == {"revision_no": 7}
    assert "server-model" not in projected.detail.model_dump_json()
    assert "server-revision" not in projected.detail.model_dump_json()
    assert projected.detail.inline_tables.items[0].row_count == 500
    assert projected.detail.inline_tables.complete
    assert "business-source" not in projected.detail.model_dump_json()
    assert len(projected.model_dump_json().encode()) <= 64 * 1024
    assert [item.format for item in projected.detail.outputs.items] == ["parquet", "json"]
    assert projected.detail.outputs.items[1].payload_name == "summary"
    assert projected.detail.outputs.items[1].json_kind == "document"
    if tool_name == "data_derive":
        operation.operation.status = CriticalOperationStatus.FAILED
        operation.operation.result_package_id = None
        operation.operation.error_code = "IFC_SOURCE_CHANGED"
        operation.operation.error_message = "CURRENT changed; no Package published. Create again against current."
        operation.operation.error_phase = CriticalOperationErrorPhase.COMMIT_FENCE
        failed = operation_application._operation_get_result(SimpleNamespace(
            operation=operation, result_package=None, input_packages={"entities": source_package},
        ))
        assert failed.operation.error_code == "IFC_SOURCE_CHANGED"
        assert failed.error.phase == "commit_fence"
        assert failed.detail.ifc_source.revision_no == 7


async def test_data_query_sql_dispatches_complete_first_page_and_continuation_branches() -> None:
    result = DataQuerySqlResult(
        row_schema=QuerySqlRowSchema(
            columns=(
                QuerySqlColumn(
                    name="entity_count",
                    logical_type="BIGINT",
                    nullable=False,
                    response_encoding=QuerySqlResponseEncoding.JSON_INTEGER,
                ),
            )
        ),
        rows=(QuerySqlRow(values={"entity_count": 1}),),
        page=PageInfo(
            total_matched_count=1,
            returned_count=1,
            remaining_count=0,
            complete=True,
        ),
        query_statistics=QuerySqlStatistics(
            bound_input_payload_count=1,
            bound_input_row_count=10,
            bound_input_stored_bytes=100,
            execution_ms=2,
        ),
    )
    application = SimpleNamespace(query_sql=AsyncMock(return_value=result))
    tools = {tool.name: tool for tool in await create_direct_mcp(application).list_tools()}
    first_page = QuerySqlQuery.model_validate(
        {
            "inputs": [
                {
                    "input_name": "entities",
                    "package_alias": "data_001",
                    "payload_name": "entities",
                }
            ],
            "execution": {
                "engine": "duckdb_sql",
                "code": "SELECT count(*) AS entity_count FROM entities",
            },
        }
    )

    assert (
        await tools["data_query_sql"].fn(
            direct_session_alias="direct_001",
            ctx=SimpleNamespace(session_id="test", request_id="1"),
            mode="fresh",
            query=first_page,
            cursor=None,
        )
        == result
    )
    domain_query = application.query_sql.await_args.args[1]
    assert isinstance(domain_query, DataQuerySqlQuery)
    assert application.query_sql.await_args.args[2] is None

    assert (
        await tools["data_query_sql"].fn(
            direct_session_alias="direct_001",
            ctx=SimpleNamespace(session_id="test", request_id="2"),
            mode="cursor",
            query=None,
            cursor="cur.next",
        )
        == result
    )
    assert application.query_sql.await_args.args == ("direct_001", None, "cur.next")

    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await tools["data_query_sql"].fn(
            direct_session_alias="direct_001",
            ctx=SimpleNamespace(session_id="test", request_id="3"),
            mode="fresh",
            query=first_page,
            cursor="cur.next",
        )


async def test_direct_json_read_resolves_scope_and_dispatches_exact_request() -> None:
    result = JsonValueResult(
        package_alias="data_002",
        payload_name="summary",
        json_kind="document",
        pointer="/metrics",
        node_type="object",
        size_bytes=11,
        value_json='{"count":1}',
    )
    json_service = SimpleNamespace(read_json=AsyncMock(return_value=result))
    scope = SimpleNamespace(identity=object())
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=scope))
    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        resolver,
        lambda _identity: nullcontext(),
        json_service=json_service,
    )
    query = JsonValueQuery(
        mode="value",
        package_alias="data_002",
        payload_name="summary",
        pointer="/metrics",
    )

    projected = await facade.read_json("direct_001", query, None)

    assert projected is result
    resolver.resolve.assert_awaited_once()
    request = json_service.read_json.await_args.args[1]
    assert request.query == query
    assert request.cursor is None


async def test_direct_json_read_fastmcp_first_page_reenters_closed_union_as_plain_data() -> None:
    result = JsonValueResult(
        package_alias="data_002",
        payload_name="summary",
        json_kind="document",
        pointer="/metrics",
        node_type="object",
        size_bytes=11,
        value_json='{"count":1}',
    )
    application = SimpleNamespace(read_json=AsyncMock(return_value=result))

    async with Client(create_direct_mcp(application)) as client:
        await client.call_tool(
            "data_read_json",
            {
                "direct_session_alias": "direct_001",
                "query": {
                    "package_alias": "data_002",
                    "payload_name": "summary",
                    "mode": "value",
                    "pointer": "/metrics",
                },
            },
        )

    application.read_json.assert_awaited_once()
    direct_session_alias, query, cursor = application.read_json.await_args.args
    assert direct_session_alias == "direct_001"
    assert isinstance(query, JsonValueQuery)
    assert query.pointer == "/metrics"
    assert cursor is None


async def test_direct_facade_binds_scope_and_passes_entry_neutral_sql_request() -> None:
    from CAD2BIM_backend.domain.data_query_sql import DataQuerySqlRequest

    query = DataQuerySqlQuery.model_validate(
        {
            "inputs": [
                {
                    "input_name": "entities",
                    "package_alias": "data_001",
                    "payload_name": "entities",
                }
            ],
            "execution": {
                "engine": "duckdb_sql",
                "code": "SELECT entity_key FROM entities",
            },
        }
    )
    expected = Mock(spec=DataQuerySqlResult)
    service = SimpleNamespace(query=AsyncMock(return_value=expected))
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
        query_sql_service=service,
    )

    assert await facade.query_sql("direct_001", query, None) is expected
    assert resolver.resolve.await_args.args == ("direct_001",)
    assert resolver.resolve.await_args.kwargs["command_id"].startswith(
        "command_direct_data_query_sql_"
    )
    request = service.query.await_args.args[1]
    assert isinstance(request, DataQuerySqlRequest)
    assert request.query == query
    assert request.cursor is None
    assert service.query.await_args.kwargs["deadline"] > 0
    assert bound == [identity]


async def test_operation_get_projects_failed_source_with_unrecorded_size() -> None:
    from datetime import datetime, timezone

    from CAD2BIM_backend.application.data_runtime_service import OperationInspection
    from CAD2BIM_backend.domain.data_runtime import (
        CatalogResourceKind,
        CriticalOperationErrorPhase,
        CriticalOperationKind,
        CriticalOperationRecord,
        CriticalOperationStatus,
        DataCatalogEntry,
        PublishedCriticalOperation,
        SourceIngestionOperationDetail,
    )

    now = datetime.now(timezone.utc)
    operation_id = "operation-failed-source-unrecorded-size"
    operation = PublishedCriticalOperation(
        operation=CriticalOperationRecord(
            operation_id=operation_id,
            owner_id="owner",
            tenant_id="tenant",
            task_id="task",
            run_id="run",
            operation_kind=CriticalOperationKind.SOURCE_INGESTION,
            tool_name="cad_import_source",
            source_filename="failed-source.dxf",
            idempotency_key="failed-source-key",
            request_fingerprint="a" * 64,
            status=CriticalOperationStatus.FAILED,
            error_code="SOURCE_EXTRACTION_FAILED",
            error_message="Source extraction failed; original byte size is not recorded.",
            error_phase=CriticalOperationErrorPhase.SOURCE_EXTRACTION,
            detail=SourceIngestionOperationDetail(
                filename="failed-source.dxf",
                size_bytes=None,
            ),
            started_at=now,
            finished_at=now,
        ),
        catalog_entry=DataCatalogEntry(
            catalog_entry_id="catalog-operation-1",
            owner_id="owner",
            tenant_id="tenant",
            task_id="task",
            run_id="run",
            resource_kind=CatalogResourceKind.OPERATION,
            alias="op_001",
            ordinal=1,
            catalog_revision=1,
            operation_id=operation_id,
            created_at=now,
        ),
    )
    inspection = OperationInspection(
        operation=operation,
        result_package=None,
        input_packages={},
    )
    application = SimpleNamespace(get_operation=AsyncMock(return_value=inspection))
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(identity=object(), run_id="run")))
    facade = DirectApplicationFacade(
        application,
        Mock(),
        resolver,
        lambda _identity: nullcontext(),
    )

    result = await facade.get_operation("direct_001", "op_001")

    assert isinstance(result, FailedOperationGetResult)
    assert result.detail.source.size_bytes is None


async def test_data_rows_provider_output_preserves_closed_compound_values() -> None:
    tool = await create_direct_mcp(Mock()).get_tool("data_query_rows")
    wire = ListToolsResult.model_validate_json(
        ListToolsResult(tools=[tool.to_mcp_tool()]).model_dump_json(by_alias=True)
    ).tools[0]
    schema = wire.outputSchema
    assert schema is not None
    Draft202012Validator.check_schema(schema)
    selected_schema = schema["$defs"]["GenericSelectedCellValue"]
    assert selected_schema["anyOf"]
    assert any(branch.get("type") == "array" for branch in selected_schema["anyOf"])
    assert any(branch.get("type") == "object" for branch in selected_schema["anyOf"])
    sample = {
        "package_alias": "data_001", "payload_name": "items", "logical_key_fields": ["id"],
        "rows": [{"key_values": {"id": "a"}, "values": {
            "event_keys": [], "source_event_keys": ["event_a"],
            "details": {"entries": [[True, 3, 1.25, None], {"kind": "binary_base64", "base64": "Yg=="}]},
        }}],
        "page": {"total_matched_count": 1, "returned_count": 1, "remaining_count": 0,
                 "complete": True, "next_cursor": None},
    }
    result = DataQueryRowsResult.model_validate(sample)
    output_validator = Draft202012Validator(schema)
    output_validator.validate(result.model_dump(mode="json"))
    sample["rows"][0]["key_values"]["id"] = ["a"]
    assert not output_validator.is_valid(sample)
    with pytest.raises(ValidationError):
        DataQueryRowsResult.model_validate(sample)
    input_validator = Draft202012Validator(wire.inputSchema)
    assert not input_validator.is_valid({
        "direct_session_alias": "direct_001", "query": {
            "package_alias": "data_001", "payload_name": "items", "select": ["event_keys"],
            "filters": [{"field": "event_keys", "op": "eq", "value": ["event_a"]}],
        },
    })


async def test_phase2_provider_inputs_convert_to_domain_contracts() -> None:
    server = create_direct_mcp(Mock())
    tools = {tool.name: tool for tool in await server.list_tools()}
    cases: list[tuple[str, type[BaseModel], dict[str, object], bool]] = [
        (
            "data_query_rows",
            DataRowsQuery,
            {
                "package_alias": "data_001",
                "payload_name": "entities",
                "select": ["entity_key"],
            },
            True,
        ),
        (
            "data_query_rows",
            DataRowsQuery,
            {
                "package_alias": "data_001",
                "payload_name": "entities",
                "select": ["payload"],
                "filters": [
                    {
                        "field": "payload",
                        "op": "eq",
                        "value": {"base64": "Zg=="},
                    }
                ],
            },
            False,
        ),
        (
            "data_query_rows",
            DataRowsQuery,
            {
                "package_alias": "data_001",
                "payload_name": "entities",
                "select": [""],
            },
            False,
        ),
        (
            "data_query_rows",
            DataRowsQuery,
            {
                "package_alias": "data_001",
                "payload_name": "entities",
                "select": ["payload"],
                "filters": [
                    {
                        "field": "payload",
                        "op": "eq",
                        "value": {"kind": "binary_base64", "base64": "Zg=="},
                    }
                ],
            },
            True,
        ),
        (
            "data_query_rows",
            DataRowsQuery,
            {
                "package_alias": "data_001",
                "payload_name": "entities",
                "select": ["payload"],
                "filters": [
                    {
                        "field": "payload",
                        "op": "eq",
                        "value": {"kind": "binary_base64", "base64": "AB=="},
                    }
                ],
            },
            False,
        ),
        (
            "cad_query_xdata",
            CadXdataQuery,
            {
                "package_alias": "data_001",
                "mode": "rows",
                "xdata_filters": {
                    "assignment_values": [
                        {"kind": "string", "value": "a"},
                        {"kind": "string", "value": "b"},
                    ]
                },
            },
            True,
        ),
        (
            "cad_query_xdata",
            CadXdataQuery,
            {
                "package_alias": "data_001",
                "mode": "rows",
                "xdata_filters": {"assignment_values": [{"value": "a"}]},
            },
            False,
        ),
        (
            "cad_query_xdata",
            CadXdataQuery,
            {
                "package_alias": "data_001",
                "mode": "rows",
                "xdata_filters": {"assignment_values": [{}]},
            },
            False,
        ),
        (
            "cad_query_xdata",
            CadXdataQuery,
            {
                "package_alias": "data_001",
                "mode": "rows",
                "xdata_filters": {
                    "assignment_values": [
                        {"kind": "string", "value": "a"},
                        {"kind": "integer", "value": 1},
                    ]
                },
            },
            False,
        ),
        (
            "cad_query_xdata",
            CadXdataQuery,
            {
                "package_alias": "data_001",
                "mode": "rows",
                "xdata_filters": {
                    "assignment_values": [
                        {"kind": "binary", "base64": "Zg=="},
                        {"kind": "binary", "base64": "Zg=="},
                    ]
                },
            },
            False,
        ),
        (
            "cad_query_xdata",
            CadXdataQuery,
            {
                "package_alias": "data_001",
                "mode": "rows",
                "xdata_filters": {"assignment_values": [{"kind": "binary", "base64": "AAB="}]},
            },
            False,
        ),
    ]
    for field in ("layers", "entity_types", "entity_keys", "handles"):
        cases.append(
            (
                "cad_query_entities",
                CadEntitiesQuery,
                {
                    "package_alias": "data_001",
                    "mode": "rows",
                    "filters": {field: [""]},
                },
                False,
            )
        )
    for field in ("layers", "entity_types", "entity_keys"):
        cases.append(
            (
                "cad_query_xdata",
                CadXdataQuery,
                {
                    "package_alias": "data_001",
                    "mode": "rows",
                    "entity_filters": {field: [""]},
                },
                False,
            )
        )
    for field in ("appids", "assignment_keys"):
        cases.append(
            (
                "cad_query_xdata",
                CadXdataQuery,
                {
                    "package_alias": "data_001",
                    "mode": "rows",
                    "xdata_filters": {field: [""]},
                },
                False,
            )
        )
    cases.append(
        (
            "cad_query_xdata",
            CadXdataQuery,
            {
                "package_alias": "data_001",
                "mode": "profile",
                "profile_dimension": "assignment_key",
                "assignment_keys": [""],
            },
            False,
        )
    )
    for values in (
        ["a", "b"],
        [1, 2],
        [1.5, 2.5],
        [True, False],
        [1, 1],
        [1, 1.0],
        [1.0, 2],
        [0.0, 1],
        [1.0, 2.0],
    ):
        cases.append(
            (
                "data_query_rows",
                DataRowsQuery,
                {
                    "package_alias": "data_001",
                    "payload_name": "entities",
                    "select": ["entity_key"],
                    "filters": [{"field": "entity_key", "op": "in", "value": values}],
                },
                True,
            )
        )
    for values in ([1, "a"], [True, 1], [1, 1.5]):
        cases.append(
            (
                "data_query_rows",
                DataRowsQuery,
                {
                    "package_alias": "data_001",
                    "payload_name": "entities",
                    "select": ["entity_key"],
                    "filters": [{"field": "entity_key", "op": "in", "value": values}],
                },
                False,
            )
        )

    converters = {
        "data_query_rows": to_domain_data_rows_query,
        "cad_query_entities": to_domain_cad_entities_query,
        "cad_query_xdata": to_domain_cad_xdata_query,
    }
    for tool in tools.values():
        Draft202012Validator.check_schema(tool.parameters)
    for tool_name, model, query, expected in cases:
        arguments = {"direct_session_alias": "direct_001", "query": query}
        schema_accepts = Draft202012Validator(tools[tool_name].parameters).is_valid(arguments)
        try:
            converted = converters[tool_name](query)
        except ValidationError:
            converted_accepts = False
        else:
            converted_accepts = True
            assert isinstance(converted, model)
        assert converted_accepts is expected, (tool_name, query)
        if expected:
            assert schema_accepts, (tool_name, query)


async def test_phase2_tool_descriptions_expose_usage_and_capability_boundaries() -> None:
    tools = {tool.name: tool for tool in await create_direct_mcp(Mock()).list_tools()}
    tools = {name: tool.model_copy(update={"parameters": _expand_shared_input(tool.parameters)})
             for name, tool in tools.items()}
    required_fragments = {
        "data_package_list": (
            "source_l0 and derived Data Packages",
            "Operations are discovered separately with operation_list",
            "next_before_sequence unchanged",
            "Default page_size is 20, maximum 50",
            "128 KiB",
            "10 seconds",
            "not business approval",
        ),
        "operation_list": (
            "terminal source_ingestion, data_transform, and ifc_model_write Operations",
            "next_before_sequence unchanged",
            "Default page_size is 20, maximum 50",
            "operation_get",
            "128 KiB",
            "10 seconds",
            "not a Job queue",
        ),
        "operation_get": (
            "operation_alias from operation_list",
            "source_ingestion, data_transform, IFC model-write",
            "code_list/code_read",
            "64 KiB",
            "10 seconds",
            "not approval",
        ),
        "data_derive": (
            "data_package_list",
            "data_describe_package",
            "execution.engine=duckdb_sql",
            "execution.engine=python_arrow",
            "1 MiB canonical JSON total",
            "same-named final TEMP relation",
            "def transform(ctx)",
            "selected engine document",
            "complete engine API, limits, output publication contract",
            "cad2bim://contracts/data_derive/duckdb_sql",
            "cad2bim://contracts/data_derive/python_arrow",
            "cad2bim://contracts/data_derive/repair",
            "Publication is atomic",
            "mode=repair",
            "failed_operation or",
            "succeeded_package source",
            "bounded literal edits",
            "Omitted fields inherit",
            "outputs uses set/edit by payload_name",
            "operation_get transform_input",
            "re-executes and creates a new Operation",
            "There is no Job or polling",
            "not approved business or IFC decisions",
        ),
        "data_read_json": (
            "data_package_list",
            "data_describe_package",
            "RFC 6901",
            "value returns one complete",
            "children pages only the direct children",
            "records pages the top-level array",
            "Continuation supplies only the returned opaque cursor unchanged",
            "Children default to 50 and allow at most 100",
            "records default to 100 and allow at most 200",
            "at most 1 MiB",
            "at most 512 KiB",
            "JSONPath, wildcard, filter, regex, custom sort, OFFSET",
            "hard timeout is 15 seconds",
            "not an approved business or IFC decision",
        ),
        "data_query_sql": (
            "data_package_list",
            "data_describe_package",
            'AS "name"',
            "bounded DuckDB line/token or candidate-column detail",
            "references inside scalar subqueries count as use",
            "truly unused binding is rejected",
            "First call",
            "Continuation",
            "SQL is not rerun",
            "Default page_size is 100 and maximum 200",
            "900-second TTL",
            "128 MiB",
            "1 MiB",
            "256 KiB",
            "512 KiB",
            "200s total",
            "QUERY_RESULT_SNAPSHOT_TOO_LARGE",
            "QUERY_CURSOR_EXPIRED",
            "QUERY_RESULT_VALUE_TOO_LARGE",
            "use data_derive",
            "without publishing a Package or Operation",
            "not approved business or IFC decisions",
        ),
        "code_list": (
            "authoritative owner",
            "opaque cursor",
            "page_size defaults to 20",
            "integer from 1 through 50",
            "returned_count=0 and codes=[]",
            "owner_alias",
            "only code_locator is accepted by code_read",
            "Geometry QueryAttempts",
            "IFC QueryAttempts",
            "does not scan naked Artifacts",
        ),
        "code_read": (
            "code_list",
            "owner_alias",
            "are not code locators",
            "re-resolves and reauthorizes",
            "single_text",
            "bundle",
            "case-sensitive literal search",
        ),
        "data_get_package_description": (
            "source_l0 or derived",
            "data_package_list",
            "data_describe_package",
            "data_query_rows",
            "10 seconds",
            "not an approved business or IFC decision",
        ),
        "data_describe_package": (
            "source_l0 or derived",
            "data_package_list",
            "payload_name",
            "not paginated",
            "does not return samples, rows, code, expanded lineage, transformations",
            "10 seconds",
        ),
        "data_query_rows": (
            "source_l0 or derived",
            "data_describe_package",
            "First call",
            "query.select is required",
            "logical-key fields",
            "cursor",
            "AND-only",
            "eq, in, lt, lte, gt, gte, is_null and is_not_null",
            "Default page_size is 100, maximum 200",
            "No OR tree, regex, SQL, arbitrary expression, custom sort or OFFSET",
            "15 seconds",
            "2,000,000-row/256-MiB",
            "page_size only limits returned rows",
            "logical-key fields must be scanned even when omitted from select",
            "data_query_sql with the same filters",
            "QUERY_TIMEOUT or QUERY_BUDGET_EXCEEDED",
            "Query and cursor are mutually exclusive",
            "not approved business or IFC decisions",
        ),
        "cad_query_entities": (
            "source_l0 Package",
            "PACKAGE_KIND_MISMATCH",
            "data_describe_package and data_query_rows",
            "rows mode",
            "count_by",
            "query.filters.layers/entity_types",
            "Different filter fields are AND",
            "default scope is top_level_modelspace",
            "Default page_size is 100, maximum 200",
            "no OR tree, regex, SQL, arbitrary expression, custom sort or OFFSET",
            "not product classification, deduplication, or IFC decisions",
            "QUERY_TIMEOUT or QUERY_BUDGET_EXCEEDED",
            "Query and cursor are mutually exclusive",
        ),
        "cad_query_xdata": (
            "source_l0 Package",
            "PACKAGE_KIND_MISMATCH",
            "rows mode",
            "only in this tool response",
            "not stored in the immutable xdata_tags",
            "entity_key, appid",
            "key=value",
            "profile mode",
            "query.entity_filters",
            "query.xdata_filters",
            "cad_query_entities",
            "AppID/group-code/assignment-key",
            "Default page_size is 100, maximum 200",
            "no OR tree, regex, SQL, arbitrary expression, custom sort or OFFSET",
            "do not assign business meaning or IFC classes",
            "QUERY_TIMEOUT or QUERY_BUDGET_EXCEEDED",
            "Query and cursor are mutually exclusive",
        ),
        "cad_get_entity": (
            "source_l0 Package",
            "PACKAGE_KIND_MISMATCH",
            "entity_key from cad_query_entities",
            "exactly one detail section",
            "First call uses query only",
            "continuation uses the returned cursor only and unchanged",
            "Default page_size is 100, maximum 200",
            "no page number, OFFSET, custom sort, or cross-section cursor reuse",
            "15 seconds",
            "2,000,000-row/256-MiB",
            "does not make product or IFC decisions",
        ),
    }
    for tool_name, fragments in required_fragments.items():
        description = tools[tool_name].description
        for fragment in fragments:
            assert fragment in description, (tool_name, fragment)

    entity_query = tools["cad_query_entities"].parameters["oneOf"][0]["properties"]["query"]
    assert all(variant["properties"]["filters"]["description"] for variant in entity_query["anyOf"])
    xdata_query = tools["cad_query_xdata"].parameters["oneOf"][0]["properties"]["query"]
    assert all(
        variant["properties"]["entity_filters"]["description"] for variant in xdata_query["anyOf"]
    )


@pytest.mark.parametrize(
    "tool_name", ("data_query_rows", "cad_query_entities", "cad_get_entity", "cad_query_xdata")
)
@pytest.mark.parametrize("both", (False, True))
async def test_paged_phase2_tools_reject_query_cursor_xor_errors_as_tool_errors(
    tool_name: str,
    both: bool,
) -> None:
    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        Mock(),
        query_service=Mock(),
    )
    tools = {tool.name: tool for tool in await create_direct_mcp(facade).list_tools()}
    queries = {
        "data_query_rows": DataRowsQuery(
            package_alias="data_001",
            payload_name="entities",
            select=(),
        ),
        "cad_query_entities": CadEntitiesQuery(package_alias="data_001", mode="rows"),
        "cad_get_entity": CadEntityQuery(
            package_alias="data_001",
            entity_key="entity-key",
            section="geometry_elements",
        ),
        "cad_query_xdata": CadXdataQuery(package_alias="data_001", mode="rows"),
    }
    arguments = {"direct_session_alias": "direct_001"}
    if both:
        arguments.update(query=queries[tool_name], cursor="cur.token")
    schema_arguments = {
        name: value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        for name, value in arguments.items()
    }
    assert not Draft202012Validator(tools[tool_name].parameters).is_valid(schema_arguments)

    with pytest.raises(ToolError, match="INVALID_ARGUMENT") as captured:
        await tools[tool_name].fn(**arguments)

    assert "Exactly one of query or cursor" in str(captured.value)
    assert "Traceback" not in str(captured.value)


@pytest.mark.parametrize("code", ["PACKAGE_NOT_FOUND", "QUERY_BUDGET_EXCEEDED"])
async def test_phase2_query_error_exposes_bounded_code_not_internal_details(code: str) -> None:
    import pyarrow as pa
    from CAD2BIM_backend.application.data_query_service import DataQueryApplicationError, DataQueryPolicy, _Budget

    internal_marker = "/private/storage/tenant-secret.parquet"
    error = DataQueryApplicationError("PACKAGE_NOT_FOUND", "The package is not visible in this scope")
    if code == "QUERY_BUDGET_EXCEEDED":
        budget = _Budget(DataQueryPolicy(scan_row_budget=1, decoded_projection_byte_budget=1), 10.0, lambda: 0.0)
        with pytest.raises(DataQueryApplicationError) as caught:
            budget.charge(pa.record_batch({"key": ["a", "b"]}))
        error = caught.value
    service = SimpleNamespace(
        query_rows=AsyncMock(
            side_effect=error
        )
    )
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(identity=object())))
    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        resolver,
        lambda _identity: nullcontext(),
        query_service=service,
    )
    tools = {tool.name: tool for tool in await create_direct_mcp(facade).list_tools()}

    with pytest.raises(ToolError, match=code) as captured:
        await tools["data_query_rows"].fn(
            direct_session_alias="direct_001",
            query=DataRowsQuery(
                package_alias="data_001",
                payload_name="entities",
                select=("entity_key",),
            ),
        )

    assert internal_marker not in str(captured.value)
    assert "Traceback" not in str(captured.value)
    assert str(error) in str(captured.value)


async def test_direct_query_rejects_result_returned_after_final_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(query_rows=AsyncMock(return_value=Mock()))
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(identity=object())))
    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        resolver,
        lambda _identity: nullcontext(),
        query_service=service,
    )
    ticks = iter((0.0, 16.0))
    monkeypatch.setattr(query_application, "monotonic", lambda: next(ticks))

    with pytest.raises(DirectApplicationError) as timeout:
        await facade.query_rows(
            "direct_001",
            DataRowsQuery(
                package_alias="data_001",
                payload_name="entities",
                select=("entity_key",),
            ),
            None,
        )

    assert timeout.value.code == "QUERY_TIMEOUT"


async def _never_finishes(*_args, **_kwargs):
    await asyncio.Event().wait()


async def test_direct_read_timeout_is_hard_and_projects_stable_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(facade_application, "_READ_TIMEOUT_SECONDS", 0.01)
    application = SimpleNamespace(list_package_catalog=AsyncMock(side_effect=_never_finishes))
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(identity=object())))
    facade = DirectApplicationFacade(
        application,
        Mock(),
        resolver,
        lambda _identity: nullcontext(),
    )
    server = create_direct_mcp(facade)

    with pytest.raises(ToolError) as captured:
        async with Client(server) as client:
            await client.call_tool(
                "data_package_list",
                {"direct_session_alias": "direct_001"},
            )

    message = str(captured.value)
    assert "QUERY_TIMEOUT" in message
    assert "phase=read" in message
    assert "retryable=false" in message
    application.list_package_catalog.assert_awaited_once()


async def test_direct_transform_timeout_requires_exact_request_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transform_application, "_TRANSFORM_TIMEOUT_SECONDS", 0.01)
    transform_service = SimpleNamespace(transform=AsyncMock(side_effect=_never_finishes))
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(identity=object())))
    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        resolver,
        lambda _identity: nullcontext(),
        transform_service=transform_service,
    )
    server = create_direct_mcp(facade)

    with pytest.raises(ToolError) as captured:
        async with Client(server) as client:
            await client.call_tool(
                "data_derive",
                {
                    "mode": "create",
                    "direct_session_alias": "direct_001",
                    "inputs": [
                        {
                            "input_name": "entities",
                            "package_alias": "data_001",
                            "payload_name": "entities",
                        }
                    ],
                    "parameters": [],
                    "execution": {
                        "engine": "duckdb_sql",
                        "code": "SELECT entity_key FROM entities",
                    },
                    "outputs": {"action": "set", "value": [
                        {
                            "payload_name": "selected_entities",
                            "format": "parquet",
                            "description": "Selected entity facts.",
                            "logical_key_fields": ["entity_key"],
                        }
                    ]},
                    "output_package": {
                        "name": "selected_entities_v1",
                        "description_markdown": {
                            "action": "set",
                            "value": "# Selected entities\n## Summary\nSelected exact entity facts.",
                        },
                    },
                },
            )

    message = str(captured.value)
    assert "TRANSFORM_PUBLICATION_UNCERTAIN" in message
    assert "phase=publication" in message
    assert "retryable=true" in message
    transform_service.transform.assert_awaited_once()


async def test_direct_transform_scope_timeout_is_preflight_without_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transform_application, "_TRANSFORM_TIMEOUT_SECONDS", 0.01)
    transform_service = SimpleNamespace(transform=AsyncMock())
    resolver = SimpleNamespace(resolve=AsyncMock(side_effect=_never_finishes))
    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        resolver,
        lambda _identity: nullcontext(),
        transform_service=transform_service,
    )

    with pytest.raises(DirectApplicationError) as captured:
        await facade.transform_data(
            "direct_001", Mock(), request_identity="test-scope-timeout-request"
        )

    assert captured.value.code == "TRANSFORM_TIMEOUT"
    assert captured.value.phase == "preflight"
    assert captured.value.retryable is False
    assert captured.value.operation_alias is None
    transform_service.transform.assert_not_awaited()


async def test_direct_transform_long_multibyte_error_preserves_terminal_metadata() -> None:
    application = SimpleNamespace(
        transform_data=AsyncMock(
            side_effect=DirectApplicationError(
                "TRANSFORM_EXECUTION_FAILED",
                "错" * 1_000,
                phase="execution",
                retryable=False,
                operation_alias="op_123",
            )
        )
    )
    server = create_direct_mcp(application)

    with pytest.raises(ToolError) as captured:
        async with Client(server) as client:
            await client.call_tool(
                "data_derive",
                {
                    "mode": "create",
                    "direct_session_alias": "direct_001",
                    "inputs": [
                        {
                            "input_name": "entities",
                            "package_alias": "data_001",
                            "payload_name": "entities",
                        }
                    ],
                    "parameters": [],
                    "execution": {
                        "engine": "duckdb_sql",
                        "code": "SELECT entity_key FROM entities",
                    },
                    "outputs": {"action": "set", "value": [
                        {
                            "payload_name": "selected_entities",
                            "format": "parquet",
                            "description": "Selected entity facts.",
                            "logical_key_fields": ["entity_key"],
                        }
                    ]},
                    "output_package": {
                        "name": "selected_entities_v1",
                        "description_markdown": (
                            "# Selected entities\n## Summary\nSelected exact entity facts."
                        ),
                    },
                },
            )

    message = str(captured.value)
    assert len(message.encode("utf-8")) <= 2_000
    assert "TRANSFORM_EXECUTION_FAILED" in message
    assert "phase=execution" in message
    assert "retryable=false" in message
    assert "operation_alias=op_123" in message
    assert "\ufffd" not in message
    application.transform_data.assert_awaited_once()


@pytest.mark.parametrize(
    "aggregate_error",
    ("duplicate_input_name", "duplicate_parameter_name", "duplicate_exact_binding"),
)
async def test_direct_transform_aggregate_validation_is_preflight(
    aggregate_error: str,
) -> None:
    application = SimpleNamespace(transform_data=AsyncMock())
    arguments = {
        "mode": "create",
        "direct_session_alias": "direct_001",
        "inputs": [
            {
                "input_name": "entities",
                "package_alias": "data_001",
                "payload_name": "entities",
            }
        ],
        "parameters": [],
        "execution": {
            "engine": "duckdb_sql",
            "code": "SELECT entity_key FROM entities",
        },
        "outputs": {"action": "set", "value": [
            {
                "payload_name": "selected_entities",
                "format": "parquet",
                "description": "Selected entity facts.",
                "logical_key_fields": ["entity_key"],
            }
        ]},
        "output_package": {
            "name": "selected_entities_v1",
            "description_markdown": {"action": "set", "value": "# Selected entities\n## Summary\nSelected facts."},
        },
    }
    if aggregate_error == "duplicate_input_name":
        arguments["inputs"].append(
            {
                "input_name": "entities",
                "package_alias": "data_002",
                "payload_name": "other_entities",
            }
        )
    elif aggregate_error == "duplicate_parameter_name":
        arguments["parameters"].extend(
            (
                {"name": "layer", "kind": "string", "value": "A"},
                {"name": "layer", "kind": "string", "value": "B"},
            )
        )
    else:
        arguments["inputs"].append(
            {
                "input_name": "entities_again",
                "package_alias": "data_001",
                "payload_name": "entities",
            }
        )

    with pytest.raises(ToolError) as captured:
        async with Client(create_direct_mcp(application)) as client:
            await client.call_tool("data_derive", arguments)

    message = str(captured.value)
    assert "INVALID_ARGUMENT" in message
    assert "phase=preflight" in message
    assert "retryable=false" in message
    application.transform_data.assert_not_awaited()


async def test_direct_transform_local_field_validation_is_bounded_and_actionable() -> None:
    application = SimpleNamespace(transform_data=AsyncMock())

    with pytest.raises(ToolError) as captured:
        async with Client(create_direct_mcp(application)) as client:
            await client.call_tool(
                "data_derive",
                {
                    "mode": "create",
                    "direct_session_alias": "direct_001",
                    "inputs": [
                        {
                            "input_name": "entities",
                            "package_alias": "data_001",
                            "payload_name": "entities",
                        }
                    ],
                    "parameters": [{"name": "limit", "kind": "integer", "value": True}],
                    "execution": {
                        "engine": "duckdb_sql",
                        "code": "SELECT entity_key FROM entities",
                    },
                    "outputs": {"action": "set", "value": [
                        {
                            "payload_name": "selected_entities",
                            "format": "parquet",
                            "description": "Selected entity facts.",
                            "logical_key_fields": ["entity_key"],
                        }
                    ]},
                    "output_package": {
                        "name": "selected_entities_v1",
                        "description_markdown": {"action": "set", "value": "# Selected\n## Summary\nSelected facts."},
                    },
                },
            )

    message = str(captured.value)
    assert "INVALID_ARGUMENT" in message
    assert "phase=preflight" in message
    assert "retryable=false" in message
    # The supplied create/duckdb_sql tags identify the branch. Report its actual
    # parameter type failure rather than another branch's engine literal.
    assert "invalid_path=parameters.0.value" in message
    assert "error_type=int_type" in message
    assert "input_value" not in message
    assert "errors.pydantic.dev" not in message
    application.transform_data.assert_not_awaited()


async def test_direct_input_boundary_hides_oversized_geometry_query_source() -> None:
    sensitive_marker = "/private/source.py?token=secret&next=https://example.invalid/private"
    oversized_source = sensitive_marker + "x" * 16_385
    application = Mock()
    application.query_ifc_geometry_python = AsyncMock()

    with pytest.raises(ToolError) as captured:
        async with Client(create_direct_mcp(application)) as client:
            await client.call_tool(
                "ifc_geometry_query_python",
                {
                    "direct_session_alias": "direct_001",
                    "mode": "repair",
                    "parent_attempt_alias": "cur.attempt_ref_001",

                    "edits": [{"old_text": oversized_source, "new_text": "return 1"}],
                },
            )

    message = str(captured.value)
    assert len(message.encode("utf-8")) <= 2_000
    assert "INVALID_ARGUMENT" in message
    assert "phase=preflight" in message
    assert "retryable=false" in message
    assert "invalid_path=edits.0.old_text" in message
    assert "error_type=string_too_long" in message
    assert "constraint=max_length_characters=16384" in message
    assert sensitive_marker not in message
    assert "/private/source.py" not in message
    assert "input_value" not in message
    assert "errors.pydantic.dev" not in message
    application.query_ifc_geometry_python.assert_not_awaited()


async def test_direct_input_boundary_hides_oversized_ordinary_tool_string() -> None:
    sensitive_marker = "/private/session?token=secret"
    application = Mock()
    application.open_direct_session = AsyncMock()

    with pytest.raises(ToolError) as captured:
        async with Client(create_direct_mcp(application)) as client:
            await client.call_tool(
                "runtime_open_direct_session",
                {"session_name": sensitive_marker + "x" * 100},
            )

    message = str(captured.value)
    assert len(message.encode("utf-8")) <= 2_000
    assert "INVALID_ARGUMENT" in message
    assert "phase=preflight" in message
    assert "retryable=false" in message
    assert "invalid_path=session_name" in message
    assert "error_type=string_too_long" in message
    assert "constraint=max_length_characters=100" in message
    assert sensitive_marker not in message
    assert "/private/session" not in message
    assert "input_value" not in message
    assert "errors.pydantic.dev" not in message
    application.open_direct_session.assert_not_awaited()


async def test_direct_input_boundary_keeps_utf8_byte_failure_bounded() -> None:
    sensitive_marker = "/private/source.py?token=secret"
    oversized_utf8_source = sensitive_marker + "界" * 6_000
    assert len(oversized_utf8_source) < 16_384
    assert len(oversized_utf8_source.encode("utf-8")) > 16_384
    application = Mock()
    application.query_ifc_geometry_python = AsyncMock()

    with pytest.raises(ToolError) as captured:
        async with Client(create_direct_mcp(application)) as client:
            await client.call_tool(
                "ifc_geometry_query_python",
                {
                    "direct_session_alias": "direct_001",
                    "mode": "repair",
                    "parent_attempt_alias": "cur.attempt_ref_001",

                    "edits": [{"old_text": oversized_utf8_source, "new_text": "return 1"}],
                },
            )

    message = str(captured.value)
    assert len(message.encode("utf-8")) <= 2_000
    assert "INVALID_ARGUMENT" in message
    assert "phase=preflight" in message
    assert "retryable=false" in message
    # JSON Schema's maxLength is measured in characters and therefore passes.
    # The Provider union reports its first typed branch failure; the middleware
    # must not infer a more specific branch by inspecting supplied values.
    assert "error_type=literal_error" in message
    assert "max_length_characters" not in message
    assert sensitive_marker not in message
    assert "/private/source.py" not in message
    assert "input_value" not in message
    assert "errors.pydantic.dev" not in message
    application.query_ifc_geometry_python.assert_not_awaited()


async def test_direct_input_boundary_preserves_closed_and_strict_argument_semantics() -> None:
    application = Mock()
    application.open_direct_session = AsyncMock()
    application.list_data_packages = AsyncMock()
    application.analyze_tolerance_candidates = AsyncMock()
    application.apply_ifc_spatial_structure = AsyncMock()
    application.query_ifc_geometry_python = AsyncMock()
    repair_input = {
        "direct_session_alias": "direct_001",
        "mode": "repair",
        "parent_attempt_alias": "cur.attempt_ref_001",

        "edits": [{"old_text": "return 1", "new_text": "return 2"}],
    }

    async with Client(create_direct_mcp(application)) as client:
        cases = (
            (
                "runtime_open_direct_session",
                {"session_name": "input-boundary", "unexpected_secret": "/private/value"},
                "invalid_path=unexpected_secret; error_type=extra_forbidden",
            ),
            (
                "runtime_open_direct_session",
                {"session_name": 123},
                "invalid_path=session_name; error_type=string_type",
            ),
            (
                "data_package_list",
                {"direct_session_alias": "direct_001", "page_size": "20"},
                "invalid_path=page_size; error_type=int_type",
            ),
            (
                "cad_analyze_tolerance_candidates",
                {
                    "direct_session_alias": "direct_001",
                    "package_alias": "data_001",
                    "source_unit_to_m": "0.001",
                    "path_layers": ["PIPE"],
                },
                "invalid_path=source_unit_to_m; error_type=float_type",
            ),
            (
                "ifc_geometry_query_python",
                {**repair_input, "unexpected_secret": "/private/value"},
                "invalid_path=unexpected_secret; error_type=extra_forbidden",
            ),
            (
                "ifc_geometry_query_python",
                {**repair_input, "page_size": True},
                "invalid_path=page_size; error_type=int_type",
            ),
            (
                "ifc_spatial_structure_apply",
                {
                    "direct_session_alias": "direct_001",
                    "operations": [
                        {
                            "action": "set_project_world_placement",
                            "target_global_id": "0" * 22,
                            "project_world_placement": {
                                "origin": [0, 0, 0],
                                "axis": [0, 0, 1],
                                "ref_direction": [1, 0, 0],
                            },
                            "preserve_descendant_project_world": 1,
                        }
                    ],
                },
                "invalid_path=operations.0; error_type=union_validation",
            ),
        )
        for tool_name, arguments, expected_diagnostic in cases:
            with pytest.raises(ToolError) as captured:
                await client.call_tool(tool_name, arguments)
            message = str(captured.value)
            assert "INVALID_ARGUMENT" in message
            assert "phase=preflight" in message
            assert "retryable=false" in message
            assert expected_diagnostic in message
            assert "/private/value" not in message

    application.open_direct_session.assert_not_awaited()
    application.list_data_packages.assert_not_awaited()
    application.analyze_tolerance_candidates.assert_not_awaited()
    application.apply_ifc_spatial_structure.assert_not_awaited()
    application.query_ifc_geometry_python.assert_not_awaited()


async def test_phase2_unexpected_error_is_masked_at_mcp_boundary() -> None:
    internal_marker = "/private/storage/tenant-secret.parquet"
    application = SimpleNamespace(query_rows=AsyncMock(side_effect=RuntimeError(internal_marker)))
    server = create_direct_mcp(application)

    with pytest.raises(ToolError) as captured:
        async with Client(server) as client:
            await client.call_tool(
                "data_query_rows",
                {
                    "direct_session_alias": "direct_001",
                    "cursor": "cur.token",
                },
            )

    assert internal_marker not in str(captured.value)
    assert "Traceback" not in str(captured.value)


async def test_entry_verifies_serves_quiet_stdio_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    application = object()
    query_service = object()
    query_python_service = object()
    query_sql_service = object()
    json_service = object()
    transform_service = object()
    cad_measurement_service = object()
    ifc_model_service = object()
    ifc_geometry_basis_service = object()
    ifc_project_service = object()
    ifc_spatial_structure_service = object()
    ifc_query_service = object()
    geometry_resource_service = object()
    geometry_catalog_service = object()
    geometry_source_service = object()
    geometry_preview_service = object()
    geometry_query_service = object()
    geometry_authoring_service = object()
    product_family_catalog_service = object()
    product_family_authoring_service = object()
    product_realization_preview_service = object()
    code_discovery_service = object()
    sessions = object()
    resolver = object()
    binder = object()
    facade = object()
    ifc_docs_service = object()
    ifc_docs_client = SimpleNamespace(
        aclose=AsyncMock(side_effect=lambda: events.append("ifc_docs_close"))
    )
    server = SimpleNamespace(run_async=AsyncMock())

    class Resources:
        def __init__(self) -> None:
            self.application = application
            self.query_service = query_service
            self.query_python_service = query_python_service
            self.query_sql_service = query_sql_service
            self.json_service = json_service
            self.transform_service = transform_service
            self.cad_measurement_service = cad_measurement_service
            self.ifc_model_service = ifc_model_service
            self.products_apply_service = None
            self.products_edit_service = None
            self.products_read_service = None
            self.ifc_geometry_basis_service = ifc_geometry_basis_service
            self.ifc_project_service = ifc_project_service
            self.ifc_spatial_structure_service = ifc_spatial_structure_service
            self.ifc_query_service = ifc_query_service
            self.geometry_resource_service = geometry_resource_service
            self.geometry_catalog_service = geometry_catalog_service
            self.geometry_source_service = geometry_source_service
            self.geometry_preview_service = geometry_preview_service
            self.geometry_query_service = geometry_query_service
            self.geometry_inspection_service = None
            self.geometry_authoring_service = geometry_authoring_service
            self.product_family_catalog_service = product_family_catalog_service
            self.product_family_authoring_service = product_family_authoring_service
            self.product_realization_preview_service = product_realization_preview_service
            self.code_discovery_service = code_discovery_service
            self.reference_service = None
            self.sessions = sessions
            self.scope_resolver = resolver
            self.scope_binder = binder

        async def verify_ready(self) -> None:
            events.append("verify")

        async def close(self) -> None:
            events.append("close")

    resources = Resources()
    server.run_async.side_effect = lambda **_kwargs: events.append("serve")
    resource_factory = Mock(return_value=resources)
    facade_factory = Mock(return_value=facade)
    server_factory = Mock(return_value=server)
    config = DirectMcpConfig.from_environ({**_direct_environ(tmp_path), "CAD2BIM_IFC_DOCS_PROXY": "http://127.0.0.1:3128"})
    monkeypatch.setattr(direct_entry, "create_direct_runtime_resources", resource_factory)
    monkeypatch.setattr(direct_entry, "DirectApplicationFacade", facade_factory)
    monkeypatch.setattr(direct_entry, "create_direct_mcp", server_factory)
    monkeypatch.setattr(
        direct_entry,
        "BuildingSmartIfcDocsClient",
        Mock(return_value=ifc_docs_client),
    )
    monkeypatch.setattr(
        direct_entry,
        "IfcOfficialDocsService",
        Mock(return_value=ifc_docs_service),
    )

    await direct_entry.run_direct_stdio(config)

    assert events == ["verify", "serve", "ifc_docs_close", "close"]
    direct_entry.BuildingSmartIfcDocsClient.assert_called_once_with(proxy="http://127.0.0.1:3128")
    resource_factory.assert_called_once_with(config)
    facade_factory.assert_called_once_with(
        application,
        sessions,
        resolver,
        binder,
        query_service=query_service,
        query_python_service=query_python_service,
        query_sql_service=query_sql_service,
        json_service=json_service,
        transform_service=transform_service,
        cad_measurement_service=cad_measurement_service,
        ifc_official_docs_service=ifc_docs_service,
        ifc_model_service=ifc_model_service,
        products_apply_service=None,
        products_edit_service=None,
        products_read_service=None,
        ifc_geometry_basis_service=ifc_geometry_basis_service,
        ifc_project_service=ifc_project_service,
        ifc_spatial_structure_service=ifc_spatial_structure_service,
        ifc_query_service=ifc_query_service,
        geometry_resource_service=geometry_resource_service,
        geometry_catalog_service=geometry_catalog_service,
        geometry_source_service=geometry_source_service,
        geometry_preview_service=geometry_preview_service,
        geometry_query_service=geometry_query_service,
        geometry_inspection_service=None,
        geometry_authoring_service=geometry_authoring_service,
        product_family_catalog_service=product_family_catalog_service,
        product_family_authoring_service=product_family_authoring_service,
        product_realization_preview_service=product_realization_preview_service,
        code_discovery_service=code_discovery_service,
        reference_service=None,
    )
    server_factory.assert_called_once_with(facade)
    server.run_async.assert_awaited_once_with(transport="stdio", show_banner=False)


def test_direct_facade_retains_ifc_query_service_injection() -> None:
    ifc_query_service = object()

    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        Mock(),
        ifc_query_service=ifc_query_service,
    )

    assert facade._ifc_query_service is ifc_query_service


def test_official_docs_proxy_is_explicit_and_credentials_are_not_in_config_repr(tmp_path: Path) -> None:
    values = _direct_environ(tmp_path)
    values["HTTPS_PROXY"] = "http://ambient.invalid:3128"
    assert DirectMcpConfig.from_environ(values).ifc_docs_proxy is None
    values["CAD2BIM_IFC_DOCS_PROXY"] = "http://reader:private-token@127.0.0.1:3128"
    config = DirectMcpConfig.from_environ(values)
    assert config.ifc_docs_proxy == values["CAD2BIM_IFC_DOCS_PROXY"]
    assert "private-token" not in repr(config)
    values["CAD2BIM_IFC_DOCS_PROXY"] = "file:///private-token"
    with pytest.raises(ValueError, match="valid HTTP\\(S\\) proxy URL") as caught:
        DirectMcpConfig.from_environ(values)
    assert "private-token" not in str(caught.value)


async def test_entry_closes_when_readiness_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    resources = SimpleNamespace(
        application=object(),
        verify_ready=AsyncMock(side_effect=RuntimeError("database unavailable")),
        close=AsyncMock(),
    )
    server_factory = Mock()
    ifc_docs_client = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(
        direct_entry,
        "create_direct_runtime_resources",
        Mock(return_value=resources),
    )
    monkeypatch.setattr(direct_entry, "create_direct_mcp", server_factory)
    monkeypatch.setattr(
        direct_entry,
        "BuildingSmartIfcDocsClient",
        Mock(return_value=ifc_docs_client),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await direct_entry.run_direct_stdio(DirectMcpConfig.from_environ(_direct_environ(tmp_path)))

    resources.close.assert_awaited_once_with()
    ifc_docs_client.aclose.assert_awaited_once_with()
    server_factory.assert_not_called()


@pytest.mark.asyncio
async def test_plain_function_tools_keep_one_observation_scope_per_concurrent_call(caplog):
    from fastmcp import Client, FastMCP
    from CAD2BIM_backend.infrastructure.execution_observability import observe_execution_phase
    from CAD2BIM_mcp.direct.tool_support import DirectToolInputErrorMiddleware

    server = FastMCP("plain-call-observations")
    server.add_middleware(DirectToolInputErrorMiddleware())
    both_started = asyncio.Event()
    count = 0

    @server.tool()
    async def plain_query(request_number: int) -> int:
        nonlocal count
        with observe_execution_phase("worker_capacity", "queue"):
            count += 1
            if count == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 5)
        with observe_execution_phase("worker_runtime", "process"):
            await asyncio.sleep(0)
        return request_number

    with caplog.at_level("INFO", logger="CAD2BIM.execution"):
        async with Client(server) as client:
            results = await asyncio.gather(
                *(client.call_tool("plain_query", {"request_number": i}) for i in range(2))
            )
    assert all(not result.is_error for result in results)
    phases = [
        record.cad2bim_execution_phase
        for record in caplog.records
        if hasattr(record, "cad2bim_execution_phase")
    ]
    assert len({phase["correlation_id"] for phase in phases}) == 2
    for i in range(2):
        digest = hashlib.sha256(
            json.dumps({"request_number": i}, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        selected = [phase for phase in phases if phase["input_sha256"] == digest]
        assert len(selected) == 6
        assert len({phase["correlation_id"] for phase in selected}) == 1
        assert {phase["operation"] for phase in selected} == {"plain_query"}
        assert {phase["phase"] for phase in selected} == {"dispatch", "queue", "process"}
