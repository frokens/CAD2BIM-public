from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from types import MappingProxyType, SimpleNamespace

import httpx
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from CAD2BIM_backend.application.data_runtime_service import DataPackageCatalog, OperationSummaryCatalog, OperationInspection
from CAD2BIM_backend.application.data_query_sql_service import DataQuerySqlApplicationError
from CAD2BIM_backend.application.data_transform_service import DataTransformApplicationError
from CAD2BIM_backend.domain.runtime_execution import DATA_PYTHON_EXECUTION, DATA_SQL_EXECUTION
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef, CatalogResourceKind, CriticalOperationErrorPhase, CriticalOperationKind,
    CriticalOperationRecord, CriticalOperationStatus, DataCatalogEntry,
    AuthoringDraftWriteOperationDetail, PublishedCriticalOperation, PublishedOperationCatalogSummary, CriticalOperationCatalogSummary,
    GeometryInspectionOperationDetail,
    SourceIngestionOperationDetail,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionOperationResult
from CAD2BIM_backend.internal_data_api import create_internal_data_router
from CAD2BIM_mcp.runner import data_tools as data_tools_module
from CAD2BIM_mcp.runner.backend_client import BackendDomainError
from CAD2BIM_mcp.runner.data_tools import register_runner_data_tools


NAMES = {
    "data_package_list", "data_describe_package", "data_get_package_description",
    "data_query_rows", "data_query_sql", "data_query_python", "data_read_json",
    "data_derive", "code_list", "code_read", "operation_list", "operation_get",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["rejected", "committed"])
async def test_runner_apply_operation_reads_same_authorized_report_projection(monkeypatch, outcome):
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection
    from CAD2BIM_backend.internal_data_api import _operation_inspection_json
    from CAD2BIM_mcp.direct.operation.application import _operation_get_result
    inspection = _apply_inspection(outcome)
    class Backend:
        async def call_data_tool(self, **kwargs):
            assert kwargs["tool_name"] == "operation_get"
            return {"inspection": _operation_inspection_json(inspection), "authoring_result": None,
                "inspection_result": None, "operation_ref": "cur.original-operation", "same_run": True,
                "case_key": None, "family_version_ref": "cur.authorized-family"}
    monkeypatch.setattr(data_tools_module, "request_domain_credential", lambda: "token")
    server = FastMCP("Saved Apply projection")
    register_runner_data_tools(server, Backend())
    result = await server.call_tool("operation_get", {"request": {"operation_alias": "op_006"}})
    direct = _operation_get_result(inspection, operation_ref="cur.original-operation", family_version_ref="cur.authorized-family")
    assert result.structured_content == direct.model_dump(mode="json")
    assert "ifc_products_apply" not in {tool.name for tool in await server.list_tools()}


@pytest.mark.asyncio
async def test_internal_apply_operation_family_reference_uses_same_scope_and_failure_authorization():
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection
    from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogNotFoundError
    inspection = _apply_inspection("rejected")
    identity = TenantExecutionContext(tenant_id="tenant_local", principal_id="principal", task_id="task_local",
        actor_kind=TenantActorKind.RUNNER, command_id="saved-action")
    async def authorized(_request, *, tool_name):
        assert tool_name == "operation_get"
        return SimpleNamespace(owner_id="owner_local", run_id="run_local"), identity
    @contextmanager
    def bind_identity(_identity):
        yield
    reference = AsyncMock(return_value="cur.authorized-family")
    other = SimpleNamespace()
    app = FastAPI()
    app.include_router(create_internal_data_router(authorized=authorized, bind_identity=bind_identity,
        data_runtime=SimpleNamespace(get_operation=AsyncMock(return_value=inspection)), data_query=other,
        data_query_sql=other, data_query_python=other, data_json=other, data_transform=other, code_discovery=other,
        authoring_operation_result=AsyncMock(return_value=None), geometry_inspection_operation_result=AsyncMock(),
        geometry_inspection=other, references=SimpleNamespace(operation_reference=Mock(return_value="cur.original-operation")),
        family_version_reference=reference), prefix="/internal/domain")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/internal/domain/data/operation_get", json={"operation_alias": "op_006"})
        assert response.status_code == 200
        assert response.json()["family_version_ref"] == "cur.authorized-family"
        reference.assert_awaited_once_with(identity, "family-private", verify_assets=False)
        reference.side_effect = ProductFamilyCatalogNotFoundError("exact Family unavailable")
        hidden = await client.post("/internal/domain/data/operation_get", json={"operation_alias": "op_006"})
    assert hidden.status_code == 409 and hidden.json()["detail"]["code"] == "PRODUCT_FAMILY_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [CriticalOperationStatus.PREPARED, CriticalOperationStatus.INDETERMINATE])
async def test_runner_recovers_unpublished_apply_by_original_reference_without_catalog_alias(monkeypatch, status):
    from dataclasses import replace
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection
    from CAD2BIM_backend.domain.data_runtime import IfcProductsRecoveryOperation

    terminal = _apply_inspection()
    record = terminal.operation.operation.model_copy(update={
        "status": status, "finished_at": terminal.operation.operation.finished_at if status is CriticalOperationStatus.INDETERMINATE else None,
        "error_code": "IFC_COMMIT_UNCERTAIN" if status is CriticalOperationStatus.INDETERMINATE else None,
        "error_message": "Original commit requires reconciliation" if status is CriticalOperationStatus.INDETERMINATE else None,
        "error_phase": CriticalOperationErrorPhase.RECONCILE if status is CriticalOperationStatus.INDETERMINATE else None,
        "result_ifc_revision_id": None,
        "result_package_id": None, "detail": terminal.operation.operation.detail.model_copy(update={"result": None}),
    })
    recovery = IfcProductsRecoveryOperation(operation=record, created_at=record.started_at)
    inspection = replace(terminal, operation=recovery, result_package=None)
    identity = TenantExecutionContext(tenant_id="tenant_local", principal_id="principal", task_id="task_local",
        actor_kind=TenantActorKind.RUNNER, command_id="query-action")
    scopes = []
    async def authorized(_request, *, tool_name):
        assert tool_name in {"operation_list", "operation_get"}
        return SimpleNamespace(owner_id="owner_local", run_id="run_local"), identity
    @contextmanager
    def bind_identity(bound):
        assert bound == identity
        yield
    async def list_recovery(scope, **kwargs):
        scopes.append(scope)
        assert kwargs == {"tool_name": "ifc_products_apply", "before_created_at": None, "before_operation_id": None, "limit": 21}
        return (recovery,)
    runtime = SimpleNamespace(list_ifc_products_recovery_operations=AsyncMock(side_effect=list_recovery),
        get_operation_by_locator=AsyncMock(return_value=inspection), list_operation_catalog=AsyncMock())
    references = SimpleNamespace(operation_reference=Mock(return_value="cur.original-operation"),
        operation_locator=Mock(return_value=("run_local", record.operation_id)))
    other = SimpleNamespace()
    app = FastAPI()
    app.include_router(create_internal_data_router(authorized=authorized, bind_identity=bind_identity,
        data_runtime=runtime, data_query=other, data_query_sql=other, data_query_python=other,
        data_json=other, data_transform=other, code_discovery=other, geometry_inspection=other,
        authoring_operation_result=AsyncMock(), geometry_inspection_operation_result=AsyncMock(),
        references=references, family_version_reference=AsyncMock(return_value="cur.authorized-family")),
        prefix="/internal/domain")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        class Backend:
            async def call_data_tool(self, *, tool_name, body, **kwargs):
                response = await client.post(f"/internal/domain/data/{tool_name}", json=body)
                assert response.status_code == 200, response.text
                return response.json()
        monkeypatch.setattr(data_tools_module, "request_domain_credential", lambda: "token")
        server = FastMCP("Unpublished Apply recovery")
        register_runner_data_tools(server, Backend())
        listed = (await server.call_tool("operation_list", {"request": {"mode": "ifc_products_recovery", "tool_name": "ifc_products_apply"}})).structured_content
        assert listed["complete"] is True and listed["next_cursor"] is None
        card = listed["operations"][0]
        assert card["operation_ref"] == "cur.original-operation" and card["outcome"] == status.value
        assert card.get("operation_alias") is None and "sequence" not in card
        read = (await server.call_tool("operation_get", {"request": {
            "mode": "operation_reference", "operation_ref": card["operation_ref"]}})).structured_content
        assert read["operation"]["operation_ref"] == card["operation_ref"]
        assert read["operation"]["outcome"] == status.value
        assert read["detail"]["family_version_ref"] == "cur.authorized-family"
        assert read["detail"]["input_binding"]["package_alias"] == "data_001"
        assert read["operation"].get("operation_alias") is None and "sequence" not in read["operation"]
    runtime.get_operation_by_locator.assert_awaited_once_with(scopes[0],
        origin_run_id="run_local", operation_id=record.operation_id)
    runtime.list_operation_catalog.assert_not_awaited()


def _unsupported_schema(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            key in {"allOf", "if", "then", "else", "not"} or _unsupported_schema(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_unsupported_schema(item) for item in value)
    return False


def test_runner_data_tools_register_exact_closed_provider_inputs() -> None:
    server = FastMCP("Data contracts")
    register_runner_data_tools(server, SimpleNamespace())
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == NAMES
    for tool in tools:
        assert not _unsupported_schema(tool.parameters), tool.name
        assert tool.output_schema is not None, tool.name
        assert not _unsupported_schema(tool.output_schema), tool.name
        assert "direct_session_alias" not in str(tool.parameters)


async def test_runner_code_complete_branches_project_to_existing_http_body(monkeypatch) -> None:
    from CAD2BIM_backend.application.code_discovery_service import (
        CodeListResult, CodeReadBatchResult, CodeReadResult,
    )
    from CAD2BIM_backend.application.text_source_view import TextSourceViewService
    from CAD2BIM_backend.domain.text_source import TextSourceView

    seen = []
    view = TextSourceViewService().view("def build(): return 1\n", TextSourceView(view_mode="full"))

    class Backend:
        async def call_data_tool(self, **kwargs):
            seen.append(kwargs)
            body = kwargs["body"]
            if kwargs["tool_name"] == "code_list":
                return CodeListResult(code_kind=body.get("query", {}).get("code_kind", "geometry_resource_program"),
                                      codes=(), returned_count=0).model_dump(mode="json")

            def result(selection):
                member = selection.get("source_path")
                return CodeReadResult(
                    code_locator=selection["code_locator"], code_kind="geometry_resource_program",
                    language="python", source_form="bundle" if member else "single_text",
                    source_sha256="a" * 64 if member else view.sha256, selected_path=member,
                    member_sha256=view.sha256 if member else None, view=view,
                )

            if "reads" in body:
                return CodeReadBatchResult(reads=tuple(result(item) for item in body["reads"])).model_dump(mode="json")
            return result(body).model_dump(mode="json")

    monkeypatch.setattr(data_tools_module, "request_domain_credential", lambda: "original-runner-credential")
    server = FastMCP("Code public requests")
    register_runner_data_tools(server, Backend())
    await server.call_tool("code_list", {"request": {"mode": "first", "query": {
        "code_kind": "geometry_draft", "draft_ref": "cur.draft",
    }}})
    await server.call_tool("code_list", {"request": {"mode": "cursor", "cursor": "cur.page"}})
    single = {"source_form": "single_text", "code_locator": "cur.code", "view": {"view_mode": "full"}}
    bundle = {"source_form": "bundle", "code_locator": "cur.bundle", "source_path": "main.py",
              "view": {"view_mode": "lines", "start_line": 2, "line_count": 3}}
    await server.call_tool("code_read", {"request": single})
    member = await server.call_tool("code_read", {"request": bundle})
    batch = await server.call_tool("code_read", {"request": {"mode": "batch", "reads": [single, bundle]}})
    assert seen[0]["body"]["query"]["draft_ref"] == "cur.draft"
    assert seen[1]["body"] == {"cursor": "cur.page"}
    assert seen[2]["body"]["source_path"] is None
    assert seen[3]["body"]["source_path"] == "main.py"
    assert seen[3]["body"]["view"]["start_line"] == 2
    assert seen[4]["body"]["reads"] == [seen[2]["body"], seen[3]["body"]]
    assert member.structured_content["source_path"] == "main.py"
    assert batch.structured_content["reads"][1]["source_path"] == "main.py"
    assert all(item["access_token"] == "original-runner-credential" for item in seen)
    assert all("direct_session_alias" not in item["body"] and "mode" not in item["body"]
               and "source_form" not in item["body"] for item in seen)
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert "source_path" in str(tools["code_read"].parameters)
    assert '"request"' in tools["code_read"].description and "direct_session_alias" not in tools["code_read"].description


async def test_runner_code_shared_closed_input_rejects_incomplete_or_mixed_branches_before_http() -> None:
    from fastmcp.exceptions import ValidationError

    backend = SimpleNamespace(call_data_tool=AsyncMock())
    server = FastMCP("Code rejected requests")
    register_runner_data_tools(server, backend)
    read = {"source_form": "single_text", "code_locator": "cur.code", "view": {"view_mode": "full"}}
    for name, request in (
        ("code_list", {"query": {"code_kind": "geometry_resource_program"}}),
        ("code_list", {"mode": "first", "query": {"code_kind": "geometry_resource_program"}, "cursor": "cur.page"}),
        ("code_list", {"mode": "first", "query": {"code_kind": "geometry_resource_program", "page_size": True}}),
        ("code_read", {**read, "source_path": "main.py"}),
        ("code_read", {**read, "source_form": "bundle"}),
        ("code_read", {**read, "view": {"view_mode": "full", "line_count": 2}}),
        ("code_read", {"mode": "batch", "reads": [read] * 9}),
        ("operation_list", {"page_size": True}),
    ):
        with pytest.raises((ValidationError, ToolError)):
            await server.call_tool(name, {"request": request})
    backend.call_data_tool.assert_not_awaited()


async def test_runner_transform_resources_keep_shared_contracts_with_typed_examples() -> None:
    from fastmcp import Client
    from pydantic import TypeAdapter
    from CAD2BIM_mcp.runner.data_contracts import TransformInput
    from CAD2BIM_mcp.tool_docs import data_transform as docs

    shared = {
        "duckdb_sql": docs.DATA_TRANSFORM_DUCKDB_DOCUMENT,
        "python_arrow": docs.DATA_TRANSFORM_PYTHON_DOCUMENT,
        "repair": docs.DATA_TRANSFORM_REPAIR_DOCUMENT,
    }
    originals = deepcopy(shared)
    server = FastMCP("Runner transform resources")
    register_runner_data_tools(server, SimpleNamespace())
    async with Client(server) as client:
        uris = {str(resource.uri) for resource in await client.list_resources()}
        for name, source in shared.items():
            uri = f"cad2bim://contracts/data_derive/{name}"
            assert uri in uris
            document = json.loads((await client.read_resource(uri))[0].text)
            if name == "repair":
                example = document["complete_repair_request"]
                expected = deepcopy(source)
                request = expected["complete_repair_request"]
                request.pop("direct_session_alias")
                expected["complete_repair_request"] = {"request": request}
            else:
                example = document["examples"]["complete_create_request"]
                expected = deepcopy(source)
                request = expected["examples"]["complete_create_request"]
                request.pop("direct_session_alias")
                expected["examples"]["complete_create_request"] = {"request": request}
                if name == "python_arrow":
                    ifc_request = expected["examples"]["complete_ifc_create_request"]
                    ifc_request.pop("direct_session_alias")
                    expected["examples"]["complete_ifc_create_request"] = {"request": ifc_request}
                    TypeAdapter(TransformInput).validate_python(
                        document["examples"]["complete_ifc_create_request"]["request"]
                    )
            assert document == expected
            assert set(example) == {"request"}
            TypeAdapter(TransformInput).validate_python(example["request"])
            assert "direct_session_alias" not in json.dumps(document)
    assert shared == originals


@pytest.mark.parametrize("outcome,operation_alias", [
    ("not_started", None), ("failed", "op_009"), ("unknown", None),
])
def test_runner_diagnostic_keeps_nested_details_and_original_outcome(outcome, operation_alias) -> None:
    from CAD2BIM_mcp.runner.domain_errors import backend_domain_tool_error

    diagnostic = {"errors": [{"field": "parameter_schema.radius", "reason": "invalid default"}],
        "item_index": 0, "reason": "missing input", "correction": "provide the declared input"}
    detail = {"code": "GEOMETRY_DRAFT_INPUT_INVALID", "message": "The complete draft is invalid",
        "phase": "preflight", "retryable": False, "diagnostic": diagnostic,
        "operation_status": outcome, "operation_alias": operation_alias}
    rendered = str(backend_domain_tool_error(BackendDomainError(409, detail)))
    assert f"operation_status={outcome}" in rendered
    assert json.loads(rendered.split("; diagnostic=", 1)[1]) == diagnostic
    assert len(rendered.encode("utf-8")) <= 2_000


def test_runner_diagnostic_bounds_unicode_without_dropping_coordinates_or_mutating_input() -> None:
    from CAD2BIM_mcp.runner.domain_errors import backend_domain_tool_error

    diagnostic = {"field": "source", "line": 2, "column": 3,
        "errors": [{"field": "source", "reason": "syntax error"}], "source_line": "长" * 4_000}
    original = deepcopy(diagnostic)
    detail = {"code": "AUTHORING_SOURCE_INVALID", "message": "Invalid source " + "长" * 4_000,
        "phase": "preflight", "retryable": False, "operation_status": "not_started",
        "diagnostic": diagnostic, "internal_trace": "PRIVATE_TRACE"}
    rendered = str(backend_domain_tool_error(BackendDomainError(422, detail)))
    result = json.loads(rendered.split("; diagnostic=", 1)[1])
    assert result["line"] == 2 and result["column"] == 3
    assert result["errors"] == diagnostic["errors"]
    assert result["source_line_truncated"] is True
    assert "PRIVATE_TRACE" not in rendered
    assert "operation_status=not_started" in rendered
    assert len(rendered.encode("utf-8")) <= 2_000
    assert diagnostic == original


async def test_runner_data_package_list_projects_core_catalog(monkeypatch) -> None:
    seen = []

    class Backend:
        async def call_data_tool(self, **kwargs):
            seen.append(kwargs)
            return {
                "packages": [], "producing_operations": {},
                "complete": True, "next_before_sequence": None,
            }

    monkeypatch.setattr(data_tools_module, "request_domain_credential", lambda: "token")
    server = FastMCP("Data projection")
    register_runner_data_tools(server, Backend())
    result = await server.call_tool("data_package_list", {"request": {}})
    assert result.structured_content == {
        "packages": [], "complete": True, "next_before_sequence": None,
    }
    assert len(seen) == 1
    assert seen[0]["access_token"] == "token"
    assert seen[0]["tool_name"] == "data_package_list"
    assert seen[0]["body"] == {"before_sequence": None, "page_size": 20}


async def test_runner_data_tool_exposes_only_bounded_backend_domain_diagnostic(monkeypatch) -> None:
    class Backend:
        async def call_data_tool(self, **_kwargs):
            raise BackendDomainError(409, {
                "code": "TRANSFORM_TIMEOUT",
                "message": "Worker deadline elapsed " + "m" * 4_000,
                "phase": "execution", "retryable": False,
                "operation_alias": "op_009", "attempt_alias": "cur.attempt_1",
                "internal_trace": "SECRET_STACK_AND_SQL",
            })

    monkeypatch.setattr(data_tools_module, "request_domain_credential", lambda: "token")
    server = FastMCP("Data error projection")
    register_runner_data_tools(server, Backend())
    with pytest.raises(ToolError) as caught:
        await server.call_tool("data_package_list", {"request": {}})
    rendered = str(caught.value)
    assert "TRANSFORM_TIMEOUT; phase=execution; retryable=false" in rendered
    assert "operation_alias=op_009" in rendered
    assert "attempt_alias=cur.attempt_1" in rendered
    assert "Worker deadline elapsed" in rendered
    assert "SECRET_STACK_AND_SQL" not in rendered
    assert len(rendered.encode("utf-8")) <= 2_000


async def test_runner_data_tool_hides_unstructured_backend_failure(monkeypatch) -> None:
    class Backend:
        async def call_data_tool(self, **_kwargs):
            raise BackendDomainError(500, {"detail": "SECRET_STACK_AND_SQL"})

    monkeypatch.setattr(data_tools_module, "request_domain_credential", lambda: "token")
    server = FastMCP("Data malformed error")
    register_runner_data_tools(server, Backend())
    with pytest.raises(ToolError) as caught:
        await server.call_tool("data_package_list", {"request": {}})
    assert str(caught.value) == (
        "BACKEND_DOMAIN_FAILURE; phase=read; retryable=false; operation_status=unknown; message=Domain request failed"
    )


async def test_runner_operation_get_rehydrates_exact_non_geometry_operation(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    operation = PublishedCriticalOperation(
        operation=CriticalOperationRecord(
            operation_id="operation-1", owner_id="owner", tenant_id="tenant",
            task_id="task", run_id="run",
            operation_kind=CriticalOperationKind.SOURCE_INGESTION,
            tool_name="cad_import_source", source_filename="failed.dxf",
            idempotency_key="key", request_fingerprint="a" * 64,
            status=CriticalOperationStatus.FAILED,
            error_code="SOURCE_EXTRACTION_FAILED",
            error_message="Extraction failed",
            error_phase=CriticalOperationErrorPhase.SOURCE_EXTRACTION,
            detail=SourceIngestionOperationDetail(filename="failed.dxf", size_bytes=None),
            started_at=now, finished_at=now,
        ),
        catalog_entry=DataCatalogEntry(
            catalog_entry_id="entry-1", owner_id="owner", tenant_id="tenant",
            task_id="task", run_id="run", resource_kind=CatalogResourceKind.OPERATION,
            alias="op_001", ordinal=1, catalog_revision=1,
            operation_id="operation-1", created_at=now,
        ),
    )
    inspection = OperationInspection(operation=operation, result_package=None, input_packages={})

    class Backend:
        async def call_data_tool(self, **_kwargs):
            return {"inspection": jsonable_encoder(inspection), "authoring_result": None, "inspection_result": None,
                "operation_ref": "cur.operation", "same_run": True, "case_key": None}

    monkeypatch.setattr(data_tools_module, "request_domain_credential", lambda: "token")
    server = FastMCP("Operation projection")
    register_runner_data_tools(server, Backend())
    result = await server.call_tool(
        "operation_get", {"request": {"operation_alias": "op_001"}}
    )
    assert result.structured_content is not None
    assert result.structured_content["operation"]["operation_alias"] == "op_001"


async def test_runner_operation_get_recovers_exact_saved_draft(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    record = CriticalOperationRecord(
        operation_id="geometry-operation-1", owner_id="owner", tenant_id="tenant",
        task_id="task", run_id="run",
        operation_kind=CriticalOperationKind.GEOMETRY_RESOURCE_WRITE,
        tool_name="ifc_geometry_resource_write", idempotency_key="key",
        intent_summary="Create mapped valve geometry",
        target_geometry_resource_id="geometry-resource-1",
        code_artifact=ArtifactVersionRef(
            artifact_id="artifact-1", version_id="artifact-version-1", sha256="e" * 64,
        ),
        request_fingerprint="b" * 64, status=CriticalOperationStatus.SUCCEEDED,
        detail=AuthoringDraftWriteOperationDetail(
            root_kind="geometry", root_id="geometry-resource-1", expected_generation=None, result_generation=1,
        ),
        started_at=now, finished_at=now,
    )
    published = PublishedCriticalOperation(
        operation=record,
        catalog_entry=DataCatalogEntry(
            catalog_entry_id="geometry-entry-1", owner_id="owner", tenant_id="tenant",
            task_id="task", run_id="run", resource_kind=CatalogResourceKind.OPERATION,
            alias="op_006", ordinal=6, catalog_revision=6,
            operation_id=record.operation_id, created_at=now,
        ),
    )
    inspection = OperationInspection(operation=published, result_package=None, input_packages={})
    facts = {"root_kind": "geometry", "draft_ref": "gd_opaque", "edit_token": 1}

    class Backend:
        async def call_data_tool(self, **_kwargs):
            return {"inspection": jsonable_encoder(inspection), "authoring_result": facts, "inspection_result": None,
                "operation_ref": "cur.operation", "same_run": True, "case_key": None}

    monkeypatch.setattr(data_tools_module, "request_domain_credential", lambda: "token")
    server = FastMCP("Geometry Operation projection")
    register_runner_data_tools(server, Backend())
    result = await server.call_tool("operation_get", {"request": {"operation_alias": "op_006"}})
    assert result.structured_content is not None
    assert result.structured_content["operation"]["operation_alias"] == "op_006"
    assert result.structured_content["detail"]["draft_ref"] == "gd_opaque"
    assert result.structured_content["detail"]["edit_token"] == 1


def _retained_operation(status, source_kind):
    now = datetime.now(timezone.utc)
    succeeded = status is CriticalOperationStatus.SUCCEEDED
    detail = GeometryInspectionOperationDetail(
        source_kind=source_kind, source_id="source", source_version_id="source-version",
        source_content_digest="a" * 64, source_version_ref="cur.exact_source_version",
        request_artifact=ArtifactVersionRef(artifact_id="request", version_id="request-version", sha256="b" * 64),
    )
    record = CriticalOperationRecord(
        operation_id="retained-operation", owner_id="owner", tenant_id="tenant", task_id="task", run_id="run",
        operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION,
        tool_name="ifc_geometry_preview" if source_kind == "geometry_sample" else "ifc_product_realization_preview",
        intent_summary="Retain the actual source result for local inspection",
        idempotency_key="saved-entry-identity", request_fingerprint="c" * 64,
        status=status, detail=detail, started_at=now, finished_at=now,
        result_inspection_artifact=(ArtifactVersionRef(artifact_id="result", version_id="result-version",
            sha256="d" * 64) if succeeded else None),
        error_code=None if succeeded else "INSPECTION_PUBLICATION_FAILED",
        error_message=None if succeeded else "Publication failed before a complete result was visible",
        error_phase=None if succeeded else CriticalOperationErrorPhase.STORAGE_VERIFICATION,
    )
    published = PublishedCriticalOperation(operation=record, catalog_entry=DataCatalogEntry(
        catalog_entry_id="entry", owner_id="owner", tenant_id="tenant", task_id="task", run_id="run",
        resource_kind=CatalogResourceKind.OPERATION, alias="op_031", ordinal=31,
        catalog_revision=31, operation_id=record.operation_id, created_at=now,
    ))
    result = GeometryInspectionOperationResult(
        inspection_ref="cur.exact_inspection" if succeeded else None,
        source_kind=source_kind, source_version_ref=detail.source_version_ref,
        summary=record.intent_summary, request_json='{"retain_for_inspection":true,"exact_parameters":{"diameter_m":0.12}}',
    )
    return published, result


@pytest.mark.parametrize("status", [CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED])
@pytest.mark.parametrize("source_kind", ["geometry_sample", "family_realization"])
async def test_runner_retained_preview_operation_list_get_reuse_exact_result_and_original_request(monkeypatch, status, source_kind):
    published, retained = _retained_operation(status, source_kind)
    summary = CriticalOperationCatalogSummary(**{key: getattr(published.operation, key)
        for key in CriticalOperationCatalogSummary.model_fields})
    catalog = OperationSummaryCatalog((PublishedOperationCatalogSummary(operation=summary,
        catalog_entry=published.catalog_entry),), True, None)
    inspection = OperationInspection(operation=published, result_package=None, input_packages={})

    class Backend:
        async def call_data_tool(self, *, tool_name, **_kwargs):
            if tool_name == "operation_list":
                return {"catalog": jsonable_encoder(catalog), "authoring_results": {},
                    "operation_refs": {published.operation.operation_id: "cur.operation"}, "inspection_results": {
                    published.operation.operation_id: jsonable_encoder(retained),
                }}
            return {"inspection": jsonable_encoder(inspection), "authoring_result": None,
                "inspection_result": jsonable_encoder(retained), "operation_ref": "cur.operation",
                "same_run": True, "case_key": None}

    monkeypatch.setattr(data_tools_module, "request_domain_credential", lambda: "token")
    server = FastMCP("retained-Operation-projection")
    register_runner_data_tools(server, Backend())
    listed = (await server.call_tool("operation_list", {"request": {"operation_kind": "geometry_inspection"}})).structured_content
    got = (await server.call_tool("operation_get", {"request": {"operation_alias": "op_031"}})).structured_content
    assert got["operation"]["operation_alias"] == listed["operations"][0]["operation_alias"]
    assert "primary_result" not in listed["operations"][0]
    assert got["detail"]["request_json"] == retained.request_json
    assert got["detail"]["source_version_ref"] == retained.source_version_ref
    assert got["detail"]["source_kind"] == source_kind
    if status is CriticalOperationStatus.SUCCEEDED:
        assert got["detail"]["inspection_ref"] == retained.inspection_ref
    else:
        assert got["detail"]["inspection_ref"] is None
        assert got["operation"]["primary_result"] is None
        assert got["error"]["phase"] == "storage_verification"
    assert "artifact_id" not in str(got) and "source_id" not in str(got)


async def test_internal_preview_http_bounds_original_report_before_runner_transport():
    from contextlib import nullcontext
    from tests.unit.mcp.test_authoring_draft_preview_contracts import _large_preview_read_fixture
    scope, service, published, saved, references = await _large_preview_read_fixture()
    async def authorized(_request, *, tool_name):
        assert tool_name == "operation_get"
        return SimpleNamespace(owner_id=scope.owner_id, run_id=scope.run_id), scope.identity
    runtime = SimpleNamespace(get_operation=AsyncMock(return_value=SimpleNamespace(operation=published, result_package=None)),
        get_operation_by_locator=AsyncMock(return_value=SimpleNamespace(operation=published,result_package=None)))
    other=SimpleNamespace()
    app=FastAPI()
    app.include_router(create_internal_data_router(authorized=authorized,bind_identity=lambda _:nullcontext(),
        data_runtime=runtime,data_query=other,data_query_sql=other,data_query_python=other,data_json=other,
        data_transform=other,code_discovery=other,authoring_operation_result=AsyncMock(return_value=None),
        geometry_inspection_operation_result=AsyncMock(return_value=saved),geometry_inspection=service,
        references=references),prefix="/internal/domain")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://testserver") as client:
        response=await client.post('/internal/domain/data/operation_get',json={'operation_alias':published.catalog_entry.alias,'page_size':4})
        assert response.status_code==200,response.text
        value=response.json();assert set(value)=={'preview_result'}
        assert len(response.content)<=65536
        assert len(value['preview_result']['detail']['cases'])==4
        assert 'case_report' not in value and 'inspection_result' not in value
        text=await client.post('/internal/domain/data/operation_get',json={'mode':'preview_report','operation_ref':'cur.original-operation',
            'content_kind':'case_outcome','case_key':'case_0','character_count':16384})
        assert text.status_code==200,text.text
        assert len(text.content)<=65536
        assert text.json()['preview_result']['detail']['next_character'] is not None
        invalid=await client.post('/internal/domain/data/operation_get',json={'mode':'preview_report','operation_ref':'cur.original-operation',
            'content_kind':'coverage_gaps','case_key':'case_0'})
        assert invalid.status_code==422


async def test_internal_operation_recovery_loads_request_only_for_get_and_preserves_scope():
    published, result = _retained_operation(CriticalOperationStatus.SUCCEEDED, "geometry_sample")
    identity = TenantExecutionContext(tenant_id="tenant", principal_id="principal", task_id="task",
        actor_kind=TenantActorKind.RUNNER, command_id="saved-action")
    observed = []

    async def authorized(_request, *, tool_name):
        return SimpleNamespace(owner_id="owner", run_id="run"), identity

    @contextmanager
    def bind_identity(_identity):
        yield

    class Runtime:
        async def list_operation_catalog(self, scope, **_kwargs):
            summary = CriticalOperationCatalogSummary(**{key: getattr(published.operation, key)
                for key in CriticalOperationCatalogSummary.model_fields})
            return OperationSummaryCatalog((PublishedOperationCatalogSummary(operation=summary,
                catalog_entry=published.catalog_entry),), True, None)

        async def get_operation(self, scope, alias):
            assert alias == "op_031"
            return OperationInspection(operation=published, result_package=None, input_packages={})

    async def operation_result(scope, operation, *, include_request=False):
        observed.append((scope, operation, include_request))
        return GeometryInspectionOperationResult(result.inspection_ref, result.source_kind,
            result.source_version_ref, result.summary, result.request_json if include_request else None)

    other = SimpleNamespace()
    app = FastAPI()
    app.include_router(create_internal_data_router(authorized=authorized, bind_identity=bind_identity,
        data_runtime=Runtime(), data_query=other, data_query_sql=other, data_query_python=other,
        data_json=other, data_transform=other, code_discovery=other, authoring_operation_result=AsyncMock(return_value=None),
        geometry_inspection_operation_result=operation_result, geometry_inspection=other,
        references=SimpleNamespace(operation_reference=Mock(return_value="cur.operation"))), prefix="/internal/domain")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        listing = await client.post("/internal/domain/data/operation_list", json={})
        detail = await client.post("/internal/domain/data/operation_get", json={"operation_alias": "op_031"})
    assert listing.status_code == detail.status_code == 200
    assert [item[2] for item in observed] == [True]
    assert all(item[0].identity == identity and item[0].owner_id == "owner" for item in observed)
    assert "inspection_results" not in listing.json()
    assert detail.json()["inspection_result"]["request_json"] == result.request_json


async def test_internal_data_list_uses_one_authorized_runner_scope() -> None:
    identity = TenantExecutionContext(
        tenant_id="tenant-1", principal_id="principal-1", task_id="task-1",
        actor_kind=TenantActorKind.RUNNER, command_id="action-1",
    )
    names: list[str] = []
    bound: list[TenantExecutionContext] = []
    scopes = []

    async def authorized(_request, *, tool_name: str):
        names.append(tool_name)
        return SimpleNamespace(owner_id="tenant-1", run_id="run-1"), identity

    @contextmanager
    def bind_identity(value):
        bound.append(value)
        yield

    class Runtime:
        async def list_package_catalog(self, scope, *, before_sequence, page_size):
            scopes.append(scope)
            assert before_sequence is None and page_size == 20
            return DataPackageCatalog((), {}, True, None)

    other = SimpleNamespace()
    app = FastAPI()
    app.include_router(
        create_internal_data_router(
            authorized=authorized, bind_identity=bind_identity,
            data_runtime=Runtime(), data_query=other, data_query_sql=other,
            data_query_python=other, data_json=other, data_transform=other,
            code_discovery=other,
            authoring_operation_result=AsyncMock(return_value=None),
            geometry_inspection_operation_result=other, geometry_inspection=other, references=other,
        ),
        prefix="/internal/domain",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/internal/domain/data/data_package_list", json={})
    assert response.status_code == 200, response.text
    assert names == ["data_package_list"]
    assert bound == [identity]
    assert len(scopes) == 1
    assert scopes[0].owner_id == "tenant-1"
    assert scopes[0].run_id == "run-1"
    assert scopes[0].catalog_frontier is None
    assert response.json()["packages"] == []


async def test_internal_sql_fresh_uses_saved_action_identity() -> None:
    identity = TenantExecutionContext(
        tenant_id="tenant-1", principal_id="principal-1", task_id="task-1",
        actor_kind=TenantActorKind.RUNNER, command_id="saved-action-1",
    )
    seen = []

    async def authorized(_request, *, tool_name: str):
        assert tool_name == "data_query_sql"
        return SimpleNamespace(owner_id="tenant-1", run_id="run-1"), identity

    @contextmanager
    def bind_identity(_value):
        yield

    class Sql:
        async def query(self, scope, request, *, request_identity):
            seen.append((scope, request, request_identity))
            raise DataQuerySqlApplicationError("EXPECTED_FAILURE", "Saved action reached Core")

    other = SimpleNamespace()
    app = FastAPI()
    app.include_router(create_internal_data_router(
        authorized=authorized, bind_identity=bind_identity,
        data_runtime=other, data_query=other, data_query_sql=Sql(),
        data_query_python=other, data_json=other, data_transform=other,
        code_discovery=other, authoring_operation_result=AsyncMock(return_value=None),
        geometry_inspection_operation_result=other, geometry_inspection=other, references=other,
    ), prefix="/internal/domain")
    body = {
        "mode": "fresh",
        "query": {
            "inputs": [{
                "input_name": "source", "package_alias": "data_001",
                "payload_name": "records",
            }],
            "execution": {"engine": "duckdb_sql", "code": "SELECT * FROM source"},
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/internal/domain/data/data_query_sql", json=body)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "EXPECTED_FAILURE"
    assert len(seen) == 1
    assert seen[0][2] == "saved-action-1"
    assert seen[0][1].query.execution.code == "SELECT * FROM source"


@pytest.mark.parametrize("mode", ["create", "ifc_create", "repair"])
async def test_internal_transform_uses_saved_action_and_original_worker_budget(mode) -> None:
    identity = TenantExecutionContext(
        tenant_id="tenant-1", principal_id="principal-1", task_id="task-1",
        actor_kind=TenantActorKind.RUNNER, command_id="saved-transform-1",
    )
    seen = []

    async def authorized(_request, *, tool_name: str):
        assert tool_name == "data_derive"
        return SimpleNamespace(owner_id="tenant-1", run_id="run-1"), identity

    @contextmanager
    def bind_identity(_value):
        yield

    class Transform:
        async def transform(self, scope, command, *, request_identity, deadline):
            seen.append((scope, command, request_identity, deadline))
            raise DataTransformApplicationError(
                "EXPECTED_FAILURE", "Saved action reached Core", phase="preflight"
            )

        async def resume_by_identity(self, scope, request_identity, request_payload, *, deadline):
            seen.append((scope, request_payload, request_identity, deadline))
            raise DataTransformApplicationError(
                "EXPECTED_FAILURE", "Saved repair action reached Core", phase="preflight"
            )

    other = SimpleNamespace()
    app = FastAPI()
    app.include_router(create_internal_data_router(
        authorized=authorized, bind_identity=bind_identity,
        data_runtime=other, data_query=other, data_query_sql=other,
        data_query_python=other, data_json=other, data_transform=Transform(),
        code_discovery=other, authoring_operation_result=AsyncMock(return_value=None),
        geometry_inspection_operation_result=other, geometry_inspection=other, references=other,
    ), prefix="/internal/domain")
    body = {
        "mode": "create",
        "inputs": [{
            "input_name": "source", "package_alias": "data_001",
            "payload_name": "records",
        }],
        "execution": {"engine": "duckdb_sql", "code": "CREATE TEMP TABLE output AS SELECT * FROM source"},
        "outputs": {"action": "set", "value": [{
            "payload_name": "output", "format": "parquet",
            "description": "Derived records", "logical_key_fields": ["id"],
        }]},
        "output_package": {"name": "Derived", "description_markdown": {"action": "set", "value": "# Derived"}},
    }
    if mode == "ifc_create":
        body["inputs"] = []
        body["ifc_source"] = "current"
        body["execution"] = {"engine": "python_arrow", "code": "def transform(ctx): pass"}
    elif mode == "repair":
        body = {
            "mode": "repair",
            "source": {"source_kind": "failed_operation", "operation_alias": "op_012"},
            "edits": [{"old_text": "bad", "new_text": "good"}],
        }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/internal/domain/data/data_derive", json=body)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "EXPECTED_FAILURE"
    assert len(seen) == 1
    assert seen[0][2] == "saved-transform-1"
    if mode == "create":
        assert seen[0][1].execution.engine == "duckdb_sql"
    elif mode == "ifc_create":
        assert seen[0][1].execution.engine == "python_arrow"
        assert seen[0][1].ifc_source == "current"
        assert seen[0][1].inputs == ()
    else:
        assert seen[0][1] == {"mode": "repair", "request": body}
    policy = DATA_SQL_EXECUTION if mode == "create" else DATA_PYTHON_EXECUTION
    assert 0 < seen[0][3] - asyncio.get_running_loop().time() <= policy.total_seconds


async def test_runner_transform_input_read_forwards_typed_selection(monkeypatch) -> None:
    from CAD2BIM_backend.domain.data_transform import DataTransformTextInputRead
    from CAD2BIM_backend.application.text_source_view import TextSourceViewService
    from CAD2BIM_backend.domain.text_source import TextSourceView
    source = TextSourceViewService().view('{"type":"object"}', TextSourceView(view_mode="full"))
    saved = DataTransformTextInputRead(operation_alias="op_012", input_kind="output_schema", payload_name="summary", source=source)
    seen = []
    class Backend:
        async def call_data_tool(self, **kwargs):
            seen.append(kwargs)
            return saved.model_dump(mode="json")
    monkeypatch.setattr(data_tools_module, "request_domain_credential", lambda: "token")
    server = FastMCP("transform-read")
    register_runner_data_tools(server, Backend())
    result = await server.call_tool("operation_get", {"request": {
        "mode": "transform_input", "operation_alias": "op_012", "input_kind": "output_schema",
        "payload_name": "summary", "view": {"view_mode": "full"},
    }})
    assert result.structured_content["source"]["text"] == '{"type":"object"}'
    assert seen[0]["tool_name"] == "operation_get"
    assert seen[0]["body"]["payload_name"] == "summary"
    assert "direct_session_alias" not in seen[0]["body"]


@pytest.mark.parametrize("selection", [
    {"input_kind": "metadata", "section": "parameters"},
    {"input_kind": "inline_schema", "input_name": "original", "column_count": 1},
    {"input_kind": "inline_rows", "input_name": "original", "start_row": 2, "row_count": 1, "select": ["key"]},
    {"input_kind": "parameter", "parameter_name": "limit"},
    {"input_kind": "inline_text", "input_name": "original", "row_index": 1, "column_name": "text", "start_character": 2, "character_count": 3},
])
async def test_runner_inline_input_branch_uses_the_same_runtime_rows_and_no_direct_fields(monkeypatch, selection):
    from fastmcp import Client
    from CAD2BIM_mcp.direct.tool_support import DirectToolInputErrorMiddleware
    from tests.unit.backend.test_data_runtime_application import _saved_transform_reader
    reader, _published = _saved_transform_reader()
    scope = object()
    class Backend:
        async def call_data_tool(self, **kwargs):
            assert kwargs["tool_name"] == "operation_get" and "direct_session_alias" not in kwargs["body"]
            body = kwargs["body"]
            return (await reader.get_transform_input(scope, body["operation_alias"],
                **{k:v for k,v in body.items() if k not in {"mode", "operation_alias"}})).model_dump(mode="json")
    monkeypatch.setattr(data_tools_module, "request_domain_credential", lambda: "token")
    server = FastMCP("saved-input-runner")
    register_runner_data_tools(server, Backend())
    server.add_middleware(DirectToolInputErrorMiddleware())
    async with Client(server) as client:
        result = await client.call_tool("operation_get", {"request": {"mode": "transform_input",
            "operation_alias": "op_004", **selection}})
        assert result.structured_content["input_kind"] == selection["input_kind"]
        assert result.structured_content["operation_alias"] == "op_004"
    if selection["input_kind"] == "inline_rows":
        assert result.structured_content["rows"][0]["values"][0]["value"] == 2
        assert result.structured_content["next_row"] == 3


async def test_internal_transform_input_read_uses_shared_runtime_owner() -> None:
    from CAD2BIM_backend.domain.data_transform import DataTransformTextInputRead
    from CAD2BIM_backend.application.text_source_view import TextSourceViewService
    from CAD2BIM_backend.domain.text_source import TextSourceView
    identity = TenantExecutionContext(tenant_id="tenant-1", principal_id="principal-1", task_id="task-1", actor_kind=TenantActorKind.RUNNER, command_id="saved-read")
    scope = SimpleNamespace(owner_id="tenant-1", run_id="run-1")
    async def authorized(_request, *, tool_name):
        assert tool_name == "operation_get"
        return scope, identity
    @contextmanager
    def bind_identity(_identity):
        yield
    projected = DataTransformTextInputRead(operation_alias="op_012", input_kind="package_description", source=TextSourceViewService().view("# Original", TextSourceView(view_mode="full")))
    runtime = SimpleNamespace(get_transform_input=AsyncMock(return_value=projected))
    other = SimpleNamespace()
    app = FastAPI()
    app.include_router(create_internal_data_router(authorized=authorized, bind_identity=bind_identity,
        data_runtime=runtime, data_query=other, data_query_sql=other, data_query_python=other,
        data_json=other, data_transform=other, code_discovery=other, authoring_operation_result=other,
        geometry_inspection_operation_result=other, geometry_inspection=other, references=other), prefix="/internal/domain")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/internal/domain/data/operation_get", json={"mode": "transform_input", "operation_alias": "op_012", "input_kind": "package_description", "view": {"view_mode": "full"}})
    assert response.status_code == 200
    assert response.json()["source"]["text"] == "# Original"
    invoked_scope, alias = runtime.get_transform_input.await_args.args
    assert (invoked_scope.owner_id, invoked_scope.run_id, alias) == (scope.owner_id, scope.run_id, "op_012")
    assert runtime.get_transform_input.await_args.kwargs["view"] == TextSourceView(view_mode="full")
