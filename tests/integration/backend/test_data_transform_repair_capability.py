from __future__ import annotations

import base64
import os
from pathlib import Path
import re
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
    SubprocessPythonArrowDataTransformWorker,
)
from CAD2BIM_backend.infrastructure.memory.control_plane_repository import (
    InMemoryControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.memory.identity_directory import InMemoryIdentityDirectory
from CAD2BIM_backend.infrastructure.source_text.source_patch_adapter import TextSourcePatchAdapter
from CAD2BIM_backend.infrastructure.memory.state import MemoryState
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_backend.domain.data_runtime import ValidatedSource
from CAD2BIM_mcp.direct.transform.contracts import DataRepairTransformResult
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
    FailedOperationGetResult,
    OperationGetResult,
    OperationListResult,
    SucceededOperationGetResult,
)
from CAD2BIM_mcp.direct.server import create_direct_mcp


pytestmark = pytest.mark.integration
FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "cad"
SOURCE = FIXTURE_ROOT / "minimal_line.dxf"
T = TypeVar("T", bound=BaseModel)

DESCRIPTION = """# Repaired minimal entity projection
## Summary
Publishes exact entity keys and layers after repairing one localized Python syntax error.
## Intended use
Exercise the failed Data Transform inspection and exact repair chain.
## Contents
One selected_entities Parquet Payload.
## Method
Read the exact L0 entities input with the controlled Python and Arrow worker.
## Scope and completeness
Contains every row from the exact minimal source entities Payload.
## Known limitations
This focused fixture is not an IFC modeling decision.
"""


def _cursor_key() -> str:
    return base64.urlsafe_b64encode(b"repair-capability-cursor-key-0001").decode().rstrip("=")


def _structured(result: Any, contract: type[T]) -> T:
    assert result.is_error is False
    assert result.structured_content is not None
    return contract.model_validate(result.structured_content)


def _structured_union(result: Any, contract: Any) -> Any:
    assert result.is_error is False
    assert result.structured_content is not None
    return TypeAdapter(contract).validate_python(result.structured_content)


def _operation_alias(error: ToolError) -> str:
    matched = re.search(r"operation_alias=(op_[0-9]+)", str(error))
    assert matched is not None, str(error)
    return matched.group(1)


def _syntax_error_code() -> tuple[str, str, str]:
    filler = "\n".join(f"    # bounded filler line {index:03d}" for index in range(1, 121))
    old_text = '        "layer": entities["layer"],\n    ]'
    new_text = '        "layer": entities["layer"],\n    }'
    code = (
        "def transform(ctx):\n"
        "    import pyarrow as pa\n"
        '    entities = ctx.table("entities")\n'
        "    # repeated repair marker\n"
        "    # repeated repair marker\n"
        f"{filler}\n"
        "    selected = pa.table(\n"
        "    {\n"
        '        "entity_key": entities["entity_key"],\n'
        '        "layer": entities["layer"],\n'
        "    ]\n"
        "    )\n"
        '    ctx.write_table("selected_entities", selected)\n'
    )
    return code, old_text, new_text


def _transform_request(session: str, package: str, code: str) -> dict[str, Any]:
    return {
        "mode": "create",
        "direct_session_alias": session,
        "inputs": [
            {
                "input_name": "entities",
                "package_alias": package,
                "payload_name": "entities",
            }
        ],
        "inline_tables": [],
        "parameters": [],
        "execution": {"engine": "python_arrow", "code": code},
        "outputs": {"action": "set", "value": [
            {
                "payload_name": "selected_entities",
                "format": "parquet",
                "description": "Exact entity keys and layers copied from the L0 input.",
                "logical_key_fields": ["entity_key"],
            }
        ]},
        "output_package": {
            "name": "repaired_minimal_entity_projection_v1",
            "description_markdown": {"action": "set", "value": DESCRIPTION},
        },
    }


async def _operations(client: Client[Any], session: str) -> OperationListResult:
    return _structured(
        await client.call_tool(
            "operation_list",
            {"direct_session_alias": session, "page_size": 50},
        ),
        OperationListResult,
    )


async def _transform_code_locator(
    client: Client[Any],
    session: str,
    *,
    operation_alias: str | None = None,
    package_alias: str | None = None,
) -> str:
    listed = _structured(
        await client.call_tool(
            "code_list",
            {
                "direct_session_alias": session,
                "mode": "first",
                "query": {
                    "code_kind": "data_transform_attempt",
                    "page_size": 50,
                },
            },
        ),
        DirectCodeListResult,
    )
    for card in listed.codes:
        if operation_alias is not None and card.owner_alias == operation_alias:
            return card.code_locator
        if package_alias is not None and card.result_alias == package_alias:
            assert card.result_code_locator is not None
            return card.result_code_locator
    raise AssertionError("expected Data Transform code owner was not discovered")


async def _read_transform_code(
    client: Client[Any],
    session: str,
    locator: str,
    view: dict[str, Any],
) -> DirectCodeReadResult:
    return _structured(
        await client.call_tool(
            "code_read",
            {
                "direct_session_alias": session,
                "source_form": "single_text",
                "code_locator": locator,
                "view": view,
            },
        ),
        DirectCodeReadResult,
    )


async def _selected_rows(
    client: Client[Any],
    session: str,
    package: str,
) -> DataQueryRowsResult:
    return _structured(
        await client.call_tool(
            "data_query_rows",
            {
                "direct_session_alias": session,
                "query": {
                    "package_alias": package,
                    "payload_name": "selected_entities",
                    "select": ["entity_key", "layer"],
                },
            },
        ),
        DataQueryRowsResult,
    )


async def test_direct_mcp_repairs_failed_transform_and_successful_package_sibling(
    tmp_path: Path,
) -> None:
    python_executable = os.environ.get("CAD2BIM_TEST_DATA_PYTHON_WORKER_EXECUTABLE", "").strip()
    if not python_executable:
        pytest.skip(
            "CAD2BIM_TEST_DATA_PYTHON_WORKER_EXECUTABLE is required for the real supervised Worker"
        )
    repository = InMemoryControlPlaneRepository(MemoryState())
    identities = InMemoryIdentityDirectory()
    artifacts = ArtifactService(repository, LocalStorage(tmp_path / "storage"))
    runtime = DataRuntimeService(repository, artifacts)
    sessions = DirectSessionService(repository, identities, "repair-capability-user")
    resolver = DirectScopeResolver(repository, identities, "repair-capability-user")
    payloads = ExactPayloadResolver(repository, artifacts)
    queries = DataQueryService(repository, artifacts, CursorCodec(_cursor_key()))
    duckdb_worker = SubprocessDuckDBDataTransformWorker()
    python_worker = SubprocessPythonArrowDataTransformWorker(python_executable=python_executable)
    transform = DataTransformService(
        repository,
        artifacts,
        payloads,
        duckdb_worker,
        python_worker=python_worker,
        source_patch=TextSourcePatchAdapter(),
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
                    {"session_name": "Data Transform exact repair capability"},
                ),
                DirectSessionResult,
            )
            session = opened.direct_session_alias
            # User upload is prepared by the backend before Agent discovery.
            upload_scope = await resolver.resolve(session, command_id="upload-imported")
            imported = await runtime.import_cad_source(upload_scope, ValidatedSource(
                filename=SOURCE.name, media_type="application/dxf",
                content=SOURCE.read_bytes(), source_identity=str(SOURCE), adapter_metadata={},
            ))
            bad_code, old_text, new_text = _syntax_error_code()

            with pytest.raises(ToolError) as initial_failure:
                await client.call_tool(
                    "data_transform",
                    _transform_request(session, imported.published.package.catalog_entry.alias, bad_code),
                    timeout=125,
                )
            failure_text = str(initial_failure.value)
            failed_alias = _operation_alias(initial_failure.value)
            assert "TRANSFORM_PYTHON_INVALID" in failure_text
            assert "phase=code_validation" in failure_text
            assert '"exception_type":"SyntaxError"' in failure_text
            assert '"line":' in failure_text
            assert '"source_line":' in failure_text
            assert '"caret":' in failure_text
            assert '"code_sha256":' in failure_text

            failed_operation = _structured_union(
                await client.call_tool(
                    "operation_get",
                    {
                        "direct_session_alias": session,
                        "operation_alias": failed_alias,
                    },
                ),
                OperationGetResult,
            )
            assert isinstance(failed_operation, FailedOperationGetResult)
            assert failed_operation.operation.tool_name == "data_transform"
            assert failed_operation.error.phase == "code_validation"
            assert "primary_result" not in failed_operation.operation.model_dump()

            failed_locator = await _transform_code_locator(
                client, session, operation_alias=failed_alias
            )
            searched = await _read_transform_code(
                client,
                session,
                failed_locator,
                {
                    "view_mode": "search",
                    "text": old_text,
                    "context_lines": 2,
                    "max_matches": 5,
                },
            )
            assert searched.match_count == 1
            assert searched.truncated is False
            assert old_text in searched.text

            assert searched.returned_start_line is not None
            line_start = max(1, searched.returned_start_line)
            lines = await _read_transform_code(
                client,
                session,
                failed_locator,
                {"view_mode": "lines", "start_line": line_start, "line_count": 8},
            )
            assert lines.source_sha256 == searched.source_sha256
            assert lines.total_lines > 120
            assert old_text in lines.text

            operations_before_patch_errors = await _operations(client, session)
            with pytest.raises(ToolError, match="TRANSFORM_REPAIR_MATCH_NOT_FOUND"):
                await client.call_tool(
                    "data_transform",
                    {
                        "mode": "repair",
                        "direct_session_alias": session,
                        "source": {
                            "source_kind": "failed_operation",
                            "operation_alias": failed_alias,
                        },
                        "edits": [{"old_text": "no such original literal", "new_text": new_text}],
                    },
                    timeout=125,
                )
            with pytest.raises(ToolError, match="TRANSFORM_REPAIR_MATCH_NOT_UNIQUE"):
                await client.call_tool(
                    "data_transform",
                    {
                        "mode": "repair",
                        "direct_session_alias": session,
                        "source": {
                            "source_kind": "failed_operation",
                            "operation_alias": failed_alias,
                        },
                        "edits": [
                            {
                                "old_text": "# repeated repair marker",
                                "new_text": "# still repeated repair marker",
                            }
                        ],
                    },
                    timeout=125,
                )
            assert await _operations(client, session) == operations_before_patch_errors

            repaired = _structured(
                await client.call_tool(
                    "data_transform",
                    {
                        "mode": "repair",
                        "direct_session_alias": session,
                        "source": {
                            "source_kind": "failed_operation",
                            "operation_alias": failed_alias,
                        },
                        "edits": [{"old_text": old_text, "new_text": new_text}],
                    },
                    timeout=125,
                ),
                DataRepairTransformResult,
            )
            assert repaired.repair_parent_operation_alias == failed_alias
            assert repaired.code_sha256 != searched.source_sha256
            assert repaired.output.payload_name == "selected_entities"
            assert repaired.output.row_count == 1

            repaired_operation = _structured_union(
                await client.call_tool(
                    "operation_get",
                    {
                        "direct_session_alias": session,
                        "operation_alias": repaired.operation_alias,
                    },
                ),
                OperationGetResult,
            )
            assert isinstance(repaired_operation, SucceededOperationGetResult)
            assert repaired_operation.operation.tool_name == "data_transform"
            assert repaired_operation.detail.repair_parent_operation_alias == failed_alias

            described = _structured(
                await client.call_tool(
                    "data_describe_package",
                    {
                        "direct_session_alias": session,
                        "package_alias": repaired.package_alias,
                    },
                ),
                DirectDataDescribePackageResult,
            )
            assert tuple(item.payload_name for item in described.payloads) == ("selected_entities",)
            description = _structured(
                await client.call_tool(
                    "data_get_package_description",
                    {
                        "direct_session_alias": session,
                        "package_alias": repaired.package_alias,
                    },
                ),
                PackageDescriptionResult,
            )
            assert description.markdown == DESCRIPTION
            rows = await _selected_rows(client, session, repaired.package_alias)
            assert rows.page.complete is True
            assert rows.page.total_matched_count == 1
            assert rows.rows[0].values["layer"] == "0"

            successful_locator = await _transform_code_locator(
                client, session, package_alias=repaired.package_alias
            )
            successful_code = await _read_transform_code(
                client, session, successful_locator, {"view_mode": "full"}
            )
            assert successful_code.source_sha256 == repaired.code_sha256
            assert new_text in successful_code.text

            business_old_text = '        "layer": entities["layer"],'
            business_new_text = '        "layer": pa.array(["patched"] * entities.num_rows),'
            successful_repair_request = {
                "mode": "repair",
                "direct_session_alias": session,
                "source": {
                    "source_kind": "succeeded_package",
                    "package_alias": repaired.package_alias,
                },
                "edits": [
                    {
                        "old_text": business_old_text,
                        "new_text": business_new_text,
                    }
                ],
            }
            corrected = _structured(
                await client.call_tool(
                    "data_transform",
                    successful_repair_request,
                    timeout=125,
                ),
                DataRepairTransformResult,
            )
            assert corrected.repair_parent_operation_alias == repaired.operation_alias
            assert corrected.operation_alias != repaired.operation_alias
            assert corrected.package_alias != repaired.package_alias
            assert corrected.package_name == repaired.package_name
            assert corrected.code_sha256 != repaired.code_sha256
            corrected_description = _structured(
                await client.call_tool(
                    "data_get_package_description",
                    {
                        "direct_session_alias": session,
                        "package_alias": corrected.package_alias,
                    },
                ),
                PackageDescriptionResult,
            )
            assert corrected_description.markdown == DESCRIPTION

            corrected_operation = _structured_union(
                await client.call_tool(
                    "operation_get",
                    {
                        "direct_session_alias": session,
                        "operation_alias": corrected.operation_alias,
                    },
                ),
                OperationGetResult,
            )
            assert isinstance(corrected_operation, SucceededOperationGetResult)
            assert corrected_operation.operation.tool_name == "data_transform"
            assert (
                corrected_operation.detail.repair_parent_operation_alias == repaired.operation_alias
            )
            assert len(corrected_operation.detail.inputs) == 1
            inherited_input = corrected_operation.detail.inputs[0]
            assert inherited_input.input_name == "entities"
            assert inherited_input.package_alias == imported.published.package.catalog_entry.alias
            assert inherited_input.package_alias != repaired.package_alias

            corrected_rows = await _selected_rows(client, session, corrected.package_alias)
            assert corrected_rows.page.complete is True
            assert corrected_rows.page.total_matched_count == 1
            assert corrected_rows.rows[0].values["layer"] == "patched"

            old_rows_after_sibling = await _selected_rows(client, session, repaired.package_alias)
            assert old_rows_after_sibling == rows
            old_code_after_sibling = await _read_transform_code(
                client, session, successful_locator, {"view_mode": "full"}
            )
            assert old_code_after_sibling == successful_code

            catalog = _structured(
                await client.call_tool(
                    "data_package_list",
                    {"direct_session_alias": session, "page_size": 50},
                ),
                DirectPackageListResult,
            )
            derived_by_alias = {
                package.package_alias: package
                for package in catalog.packages
                if package.package_kind == "derived"
            }
            assert derived_by_alias[repaired.package_alias].producing_operation_alias == (
                repaired.operation_alias
            )
            assert derived_by_alias[corrected.package_alias].producing_operation_alias == (
                corrected.operation_alias
            )

            operations_before_exact_retry = await _operations(client, session)
            exact_retry = _structured(
                await client.call_tool(
                    "data_transform",
                    successful_repair_request,
                    timeout=125,
                ),
                DataRepairTransformResult,
            )
            assert exact_retry == corrected
            assert await _operations(client, session) == operations_before_exact_retry

            operations = await _operations(client, session)
            assert [item.tool_name for item in operations.operations] == [
                "data_transform",
                "data_transform",
                "data_transform",
                "cad_import_source",
            ]
    finally:
        await python_worker.close()
        await duckdb_worker.close()
