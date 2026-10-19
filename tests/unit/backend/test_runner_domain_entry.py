"""The Runner domain request is independent from exact State read access."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TypedDict
from uuid import uuid4
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from fastapi import FastAPI
from fastmcp import Client, FastMCP
from mcp.types import CallToolResult, TextContent
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, END, StateGraph

from CAD2BIM_backend.domain.control_plane import (
    ExecutionMode, ExecutionProtocolStatus, RunStatus,
)
from CAD2BIM_backend.application.geometry_query_service import GeometryQueryApplicationError
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.ifc_model import IfcSettledOperationError
from CAD2BIM_backend.domain.ifc_project import IfcProjectNoChangeError
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant, RunnerGraphContext
from CAD2BIM_backend.domain.runtime_execution import (
    DATA_PYTHON_EXECUTION, GEOMETRY_AUTHORING_EXECUTION, PRODUCT_AUTHORING_EXECUTION,
    IFC_QUERY_EXECUTION, IFC_REVISION_EXECUTION,
    IFC_REVISION_INVOCATION_EXECUTION, RUNTIME_RESPONSE_MARGIN_SECONDS,
)
from CAD2BIM_backend.infrastructure.runner_domain_tokens import RunnerDomainTokenCodec
from CAD2BIM_backend.infrastructure import mcp_domain_client
from CAD2BIM_backend.infrastructure.mcp_domain_client import RunnerDomainMcpClient, RunnerDomainOutcomeUnknown
from CAD2BIM_backend.internal_authoring_api import _domain_error as _authoring_error
from CAD2BIM_backend.internal_authoring_api import create_internal_authoring_router
from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftError
from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService
from CAD2BIM_backend.domain.authoring_draft import AuthoringDraftChanged
from CAD2BIM_backend.ports.geometry_authoring import GeometryAuthoringSourceError, GeometryAuthoringSourceFailure
from CAD2BIM_backend.internal_data_api import _domain_error
from CAD2BIM_backend.internal_domain_api import create_internal_domain_router
from CAD2BIM_backend.internal_ifc_api import create_internal_ifc_router
from CAD2BIM_backend.application.ifc_write_support import bind_failed_operation
from CAD2BIM_mcp.direct.ifc.application_support import ifc_direct_error
from CAD2BIM_mcp.runner.backend_client import BackendDomainError, RunnerStateBackendClient
from CAD2BIM_mcp.runner.domain_errors import backend_domain_tool_error
from tests.unit.backend.test_opaque_reference_service import MemoryReferenceIndex, reference_codec


_SECRET = b"runner-domain-entry-test-secret-32-bytes"


class _PendingDomainState(TypedDict, total=False):
    pending_call: str | None
    failure: str


@pytest.mark.asyncio
@pytest.mark.parametrize("effect,diagnostic,unknown", [
    ("READ", "BACKEND_DOMAIN_FAILURE; phase=read; retryable=false; operation_status=unknown; message=Read rejected", False),
    ("WRITE", "BACKEND_DOMAIN_FAILURE; phase=read; retryable=false; operation_status=unknown; message=Write unconfirmed", True),
    ("WRITE", "IFC_NO_CHANGES; phase=ifc_validation; retryable=false; operation_alias=op_003; operation_status=failed; message=Persisted failure", False),
    ("WRITE", "IFC_NO_CHANGES; phase=ifc_validation; retryable=false; operation_ref=cur.original; operation_status=failed; message=Persisted failure", False),
    ("WRITE", "IFC_COMMITTED_READBACK_FAILED; phase=storage_verification; retryable=false; operation_ref=cur.original; operation_status=succeeded; message=Readback unconfirmed", True),
    ("WRITE", "INVALID_ARGUMENT; phase=preflight; retryable=false; message=Missing field request", False),
    ("WRITE", "AUTHORING_SOURCE_INVALID; phase=preflight; retryable=false; operation_status=not_started; message=Fixed input refused", False),
    ("WRITE", "AUTHORING_SOURCE_INVALID; phase=preflight; retryable=false; message=Unproven refusal", True),
    ("WRITE", "DRAFT_CHANGED; phase=preflight; retryable=false; operation_status=unknown; message=Original admission unconfirmed", True),
    ("WRITE", "DRAFT_CHANGED; phase=preflight; retryable=false; operation_alias=op_003; operation_status=not_started; message=Contradictory receipt", True),
    ("WRITE", "DRAFT_CHANGED; phase=preflight; retryable=false; operation_ref=cur.original; operation_status=not_started; message=Contradictory receipt", True),
    ("WRITE", "DRAFT_CHANGED; phase=preflight; retryable=true; operation_status=not_started; message=Unproven retry", True),
    ("WRITE", "private-code; phase=private-phase; operation_status=private-status; message=private-message", True),
])
async def test_native_client_consumes_read_and_confirmed_write_only(monkeypatch, caplog, effect, diagnostic, unknown):
    native = CallToolResult(isError=True, content=[TextContent(type="text", text=diagnostic)], structuredContent={"exact_ref": "native-result"})
    requests = []
    actions = []

    class NativeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def call_tool_mcp(self, name, arguments, **kwargs):
            requests.append((name, arguments))
            return native

    def issue(_grant, *, action_id, tool_name):
        actions.append((action_id, tool_name))
        return "private-test-token"

    monkeypatch.setattr(mcp_domain_client, "Client", NativeClient)
    adapter = RunnerDomainMcpClient(mcp_url="http://unused/mcp", codec=SimpleNamespace(issue=issue))
    arguments = {"request": {"ref": "exact-source"}}
    kwargs = {"grant": SimpleNamespace(scope=SimpleNamespace(run_id="saved-run")), "action_id": "saved-original-action", "mcp_tool_name": "saved_tool", "domain_tool_name": "saved_tool", "arguments": arguments, "effect": effect}
    if unknown:
        with pytest.raises(RunnerDomainOutcomeUnknown):
            await adapter.call_tool(**kwargs)
    else:
        assert await adapter.call_tool(**kwargs) is native
    assert requests == [("saved_tool", arguments)]
    assert actions == [("saved-original-action", "saved_tool")]
    assert arguments == {"request": {"ref": "exact-source"}}
    records = [record for record in caplog.records if record.name == mcp_domain_client.__name__]
    assert len(records) == int(unknown)
    if records:
        logged = records[0].getMessage()
        assert "run=saved-run action=saved-original-action tool=saved_tool" in logged
        assert not records[0].exc_info
        assert all(value not in logged for value in ("exact-source", "private-test-token", diagnostic.split("; message=")[-1]))
        if diagnostic.startswith("private-code"):
            assert "code=None phase=None operation_status=None" in logged
        else:
            assert f"code={diagnostic.split('; ')[0]}" in logged


@pytest.mark.asyncio
async def test_unknown_transport_log_preserves_cause_and_hides_error_and_credentials(monkeypatch, caplog):
    original = httpx.ReadTimeout("private transport URL and credential")

    class FailedClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            raise original

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr(mcp_domain_client, "Client", FailedClient)
    adapter = RunnerDomainMcpClient(mcp_url="http://unused/mcp",
        codec=SimpleNamespace(issue=lambda *a, **k: "private-credential"))
    with pytest.raises(RunnerDomainOutcomeUnknown) as raised:
        await adapter.call_tool(grant=SimpleNamespace(scope=SimpleNamespace(run_id="run")),
            action_id="original-action", mcp_tool_name="write", domain_tool_name="write",
            arguments={"private": "request-content"})
    assert raised.value.__cause__ is original
    records = [record for record in caplog.records if record.name == mcp_domain_client.__name__]
    assert len(records) == 1 and not records[0].exc_info
    assert records[0].getMessage() == (
        "Runner domain outcome unconfirmed run=run action=original-action tool=write exception_type=ReadTimeout"
    )


@pytest.mark.asyncio
async def test_preview_exception_log_keeps_safe_identity_and_type_without_message_or_request(caplog):
    identity = TenantExecutionContext(tenant_id="tenant", principal_id="person", task_id="task",
        actor_kind=TenantActorKind.RUNNER, command_id="original-action")

    async def authorized(_request, *, tool_name):
        assert tool_name == "ifc_geometry_preview"
        return SimpleNamespace(owner_id="owner", run_id="run", action_id="original-action"), identity

    async def preview(scope, *_args):
        assert scope.identity.command_id == "original-action"
        raise RuntimeError("private renderer error and credential")

    app = FastAPI()
    app.include_router(create_internal_authoring_router(authorized=authorized,
        bind_identity=lambda _: nullcontext(), data_runtime=object(),
        authoring=SimpleNamespace(geometry_preview=SimpleNamespace(preview_family_standard_model=preview))))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        response = await client.post("/authoring/ifc_geometry_preview", json={
            "mode": "family_standard_model", "family_version_alias": "cur.private-family-reference",
            "observation": {"observation_kind": "model"},
        })
    assert response.status_code == 500
    assert response.json()["detail"] == {
        "code": "BACKEND_DOMAIN_FAILURE", "message": "Domain request failed", "phase": "read", "retryable": False,
    }
    records = [record for record in caplog.records if record.name == "CAD2BIM_backend.internal_authoring_api"]
    assert len(records) == 1 and not records[0].exc_info
    assert records[0].getMessage() == (
        "Domain Preview failed run=run action=original-action tool=ifc_geometry_preview "
        "code=BACKEND_DOMAIN_FAILURE phase=read operation_status=None exception_type=RuntimeError"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["persisted", "bound", "unbound"])
async def test_ifc_entry_only_attests_existing_failed_operation(kind) -> None:
    if kind == "persisted":
        error = IfcSettledOperationError(code="IFC_NO_CHANGES", message="No changes",
                                        phase="ifc_validation", operation_alias="op_003")
    else:
        error = IfcProjectNoChangeError("No changes")
        if kind == "bound":
            bind_failed_operation(error, operation_alias="op_003")
    identity = TenantExecutionContext(tenant_id="tenant", principal_id="person", task_id="task",
                                      actor_kind=TenantActorKind.RUNNER, command_id="action")

    async def authorized(_request, **_kwargs):
        return SimpleNamespace(owner_id="owner", task_id="task", run_id="run", action_id="action"), identity

    class Project:
        async def update(self, *_args, **_kwargs):
            raise error

    app = FastAPI()
    app.include_router(create_internal_ifc_router(authorized=authorized, bind_identity=lambda _: nullcontext(),
                                                 ifc=SimpleNamespace(project=Project())))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        response = await client.post("/ifc/project/update", json={"command": {"patch": {"name": "new"}}})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail.get("operation_status") == (None if kind == "unbound" else "failed")
    assert detail.get("operation_alias") == (None if kind == "unbound" else "op_003")


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved", ["failed", "not_started"])
@pytest.mark.parametrize("detail", [
    None,
    {"code": "BACKEND_DOMAIN_FAILURE", "message": "Unavailable", "phase": "read", "retryable": False},
    {"code": "IFC_CURRENT_CONFLICT", "message": "Unconfirmed", "phase": "preflight", "retryable": False,
     "operation_alias": "op_003"},
])
async def test_uncertain_native_mcp_error_keeps_saved_graph_action(monkeypatch, detail, resolved) -> None:
    server = FastMCP("Outcome propagation")
    calls = []

    @server.tool()
    async def write() -> dict:
        if len(calls) == 1:
            raise backend_domain_tool_error(BackendDomainError(409, detail))
        raise backend_domain_tool_error(BackendDomainError(409, {
            "code": "IFC_NO_CHANGES" if resolved == "failed" else "AUTHORING_SOURCE_INVALID",
            "message": "Confirmed rejection", "phase": "ifc_validation" if resolved == "failed" else "preflight",
            "retryable": False, "operation_status": resolved,
            **({"operation_alias": "op_003"} if resolved == "failed" else {}),
        }))

    # Keep the actual FastMCP client/session/protocol; only its transport is in-process.
    monkeypatch.setattr(mcp_domain_client, "Client", lambda _transport, **kwargs: Client(server, **kwargs))
    adapter = RunnerDomainMcpClient(mcp_url="http://unused/mcp", codec=SimpleNamespace(issue=lambda *a, **k: "token"))
    graph = StateGraph(_PendingDomainState)
    graph.add_node("issue", lambda _state: {"pending_call": "saved-original-K"})

    async def execute(state):
        calls.append(state["pending_call"])
        response = await adapter.call_tool(grant=None, action_id=state["pending_call"],
                                           mcp_tool_name="write", domain_tool_name="write", arguments={})
        if response.isError:
            return {"pending_call": None, "failure": "\n".join(content.text for content in response.content if isinstance(content, TextContent))}
        raise AssertionError("fixture must return a failure")

    graph.add_node("execute", execute)
    graph.add_edge(START, "issue")
    graph.add_edge("issue", "execute")
    graph.add_edge("execute", END)
    compiled = graph.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "outcome-test"}}
    with pytest.raises(RunnerDomainOutcomeUnknown):
        await compiled.ainvoke({}, config)
    pending = await compiled.aget_state(config)
    assert pending.next == ("execute",)
    assert pending.values["pending_call"] == "saved-original-K"
    result = await compiled.ainvoke(None, config)
    assert calls == ["saved-original-K", "saved-original-K"]
    assert result["pending_call"] is None
    assert f"operation_status={resolved}" in result["failure"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(500, "private SQL error"), (200, "[]")])
async def test_backend_noncontract_response_is_unconfirmed_and_bounded(status, body) -> None:
    client = RunnerStateBackendClient("http://backend")
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="http://backend", transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text=body),
    ))
    try:
        with pytest.raises(BackendDomainError) as failure:
            await client.call_ifc_domain(access_token="token", endpoint="/internal/domain/ifc/project/update", body={})
        rendered = str(backend_domain_tool_error(failure.value))
        assert "operation_status=unknown" in rendered
        assert "private SQL" not in rendered
    finally:
        await client.aclose()


@pytest.mark.parametrize("tool_name,endpoint,policy", [
    ("ifc_products_apply", "/internal/domain/ifc/products/apply", IFC_REVISION_INVOCATION_EXECUTION),
    ("ifc_products_edit", "/internal/domain/ifc/products/edit", IFC_REVISION_INVOCATION_EXECUTION),
    ("ifc_products_read", "/internal/domain/ifc/products/read", IFC_REVISION_EXECUTION),
    ("ifc_model_info", "/internal/domain/ifc/model/info", IFC_REVISION_EXECUTION),
    ("ifc_representation_context_list", "/internal/domain/ifc/model/representation-contexts/list", IFC_REVISION_EXECUTION),
    ("ifc_spatial_structure_list", "/internal/domain/ifc/spatial-structure/list", IFC_REVISION_EXECUTION),
    ("ifc_query_python", "/internal/domain/ifc/query/python", IFC_QUERY_EXECUTION),
    ("data_derive", None, DATA_PYTHON_EXECUTION),
    ("operation_list", None, None),
])
@pytest.mark.asyncio
async def test_runner_domain_transport_timeouts_follow_selected_existing_policies(monkeypatch, tool_name, endpoint, policy) -> None:
    seen = []
    backend = RunnerStateBackendClient("http://backend")
    original_timeout = backend._client.timeout
    assert original_timeout.read == 10.0
    await backend._client.aclose()

    def respond(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"accepted": True})

    backend._client = httpx.AsyncClient(base_url="http://backend", timeout=original_timeout,
                                      transport=httpx.MockTransport(respond))
    try:
        for explicit in (None, 7.0):
            if endpoint is None:
                await backend.call_data_tool(access_token="token", tool_name=tool_name, body={}, timeout=explicit)
            else:
                await backend.call_ifc_domain(access_token="token", endpoint=endpoint, body={}, timeout=explicit)
    finally:
        await backend.aclose()
    inner = policy.total_seconds + RUNTIME_RESPONSE_MARGIN_SECONDS if policy is not None else 10.0
    assert [row["read"] for row in seen] == [inner, 7.0]

    calls = []

    class Client:
        def __init__(self, _transport, *, timeout):
            calls.append(("session", timeout))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def call_tool_mcp(self, _name, _arguments, *, timeout):
            calls.append(("request", timeout))
            return CallToolResult(content=[])

    monkeypatch.setattr(mcp_domain_client, "Client", Client)
    client = RunnerDomainMcpClient(mcp_url="http://unused/mcp", codec=SimpleNamespace(issue=lambda *a, **k: "token"))
    for explicit in (None, 7.0):
        await client.call_tool(grant=None, action_id="original", mcp_tool_name=tool_name,
                               domain_tool_name=tool_name, arguments={}, timeout_seconds=explicit)
    outer = policy.total_seconds + 2 * RUNTIME_RESPONSE_MARGIN_SECONDS if policy is not None else None
    assert calls == [("session", outer), ("request", outer), ("session", 7.0), ("request", 7.0)]


@pytest.mark.parametrize("status", ["prepared", "indeterminate", "succeeded", "failed"])
def test_runner_apply_error_keeps_original_reference_status_and_structured_diagnostic(status):
    rendered = str(backend_domain_tool_error(BackendDomainError(409, {
        "code": "IFC_COMMITTED_READBACK_FAILED", "message": "Original result requires inspection",
        "phase": "reconcile", "retryable": False,
        "operation_ref": "cur.original", "operation_status": status,
        "diagnostic": {"source_path": "geometry.py", "line": 7, "binding": {"key": "body"}},
    })))
    assert "operation_ref=cur.original" in rendered
    assert f"operation_status={status}" in rendered
    assert '"source_path":"geometry.py"' in rendered and '"binding":{"key":"body"}' in rendered
    assert "operation_alias=" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["apply", "recover"])
async def test_runner_apply_route_resolves_exact_inputs_and_recover_never_replaces_original_identity(mode):
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection
    inspection = _apply_inspection()
    record = inspection.operation.operation
    identity = TenantExecutionContext(tenant_id="tenant", principal_id="person", task_id="task",
                                      actor_kind=TenantActorKind.RUNNER, command_id="new-transport-action")

    async def authorized(_request, **kwargs):
        assert kwargs["tool_name"] == "ifc_products_apply"
        return SimpleNamespace(owner_id="owner", task_id="task", run_id="run", action_id=identity.command_id), identity

    core_result = SimpleNamespace(operation_id=record.operation_id,
        operation_alias="op_006", ifc_revision_id="original-revision")
    core = SimpleNamespace(apply=AsyncMock(return_value=core_result), recover=AsyncMock(return_value=core_result))
    data = SimpleNamespace(
        resolve_package=AsyncMock(return_value=SimpleNamespace(package=SimpleNamespace(package_id="package-exact"))),
        get_operation=AsyncMock(return_value=inspection),
        get_ifc_products_recovery_operation=AsyncMock(return_value=inspection),
    )
    family = SimpleNamespace(
        resolve_exact_version=AsyncMock(return_value=SimpleNamespace(version=SimpleNamespace(family_version_id="family-exact"))),
        version_ref_for_visible_id=AsyncMock(return_value="cur.family"),
    )
    references = SimpleNamespace(
        operation_locator=lambda context, ref: ("run", "operation-original"),
        operation_reference=lambda context, **kwargs: "cur.original",
    )
    app = FastAPI()
    app.include_router(create_internal_ifc_router(authorized=authorized, bind_identity=lambda _: nullcontext(),
        ifc=SimpleNamespace(products_apply=core), data_runtime=data, family_catalog=family, references=references))
    body = (dict(mode="apply", family_version_alias="cur.family", package_alias="data_001", payload_name="products")
            if mode == "apply" else dict(mode="recover", operation_ref="cur.original"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        response = await client.post("/ifc/products/apply", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["operation"]["operation_ref"] == "cur.original"
    if mode == "apply":
        args, kwargs = core.apply.await_args
        assert args[0].run_id == "run" and args[0].identity is identity
        assert args[1].family_version_id == "family-exact" and args[1].package_id == "package-exact"
        assert kwargs == {"idempotency_key": "new-transport-action"}
        core.recover.assert_not_awaited()
    else:
        assert core.recover.await_args.kwargs == {"origin_run_id": "run", "operation_id": "operation-original"}
        core.apply.assert_not_awaited()
        data.resolve_package.assert_not_awaited()
        family.resolve_exact_version.assert_not_awaited()


@pytest.mark.parametrize("mode,outcome,with_report", [
    ("apply", "succeeded", True),
    ("recover", "failed", True),
    ("recover", "indeterminate", False),
])
async def test_runner_edit_route_resolves_exact_inputs_and_original_three_branch_projection(mode, outcome, with_report):
    from tests.unit.mcp.test_direct_ifc_products_provider import _edit_inspection
    from CAD2BIM_mcp.direct.operation.application import project_ifc_products_edit_operation

    inspection = _edit_inspection(outcome, with_report=with_report)
    record = inspection.operation.operation
    identity = TenantExecutionContext(tenant_id=record.tenant_id, principal_id="person", task_id=record.task_id,
        actor_kind=TenantActorKind.RUNNER, command_id="server-transport-action")
    references = OpaqueReferenceService(MemoryReferenceIndex(), reference_codec())
    original_ref = references.operation_reference(identity,
        origin_run_id=record.run_id, operation_id=record.operation_id)
    basis = _edit_inspection().committed_ifc_revision.model_copy(update={
        "revision_id": record.base_ifc_revision_id, "revision_no": inspection.base_revision_no})
    base_ref = references.ifc_revision_reference(identity, basis)
    authorizations, bindings = [], []

    async def authorized(_request, *, tool_name):
        authorizations.append(tool_name)
        return SimpleNamespace(owner_id=record.owner_id, task_id=record.task_id,
            run_id=record.run_id, action_id=identity.command_id), identity

    @contextmanager
    def bind_identity(value):
        bindings.append(value)
        yield

    core_result = SimpleNamespace(operation_id=record.operation_id,
        operation_alias=inspection.operation.catalog_entry.alias if inspection.operation.catalog_entry else None,
        outcome=outcome)
    core = SimpleNamespace(edit=AsyncMock(return_value=core_result), recover=AsyncMock(return_value=core_result))
    data = SimpleNamespace(resolve_package=AsyncMock(return_value=inspection.input_packages["products"]),
        get_ifc_products_recovery_operation=AsyncMock(return_value=inspection))
    app = FastAPI()
    app.include_router(create_internal_ifc_router(authorized=authorized, bind_identity=bind_identity,
        ifc=SimpleNamespace(products_edit=core), data_runtime=data, references=references))
    body = (dict(mode="apply", package_alias="data_001", payload_name="products", base_revision_ref=base_ref)
        if mode == "apply" else dict(mode="recover", operation_ref=original_ref))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        response = await client.post("/ifc/products/edit", json=body)
    assert response.status_code == 200, response.text
    assert response.json() == project_ifc_products_edit_operation(inspection,
        operation_ref=original_ref, same_run=True).model_dump(mode="json")
    assert authorizations == ["ifc_products_edit"] and bindings == [identity]
    invoked = core.edit if mode == "apply" else core.recover
    args, kwargs = invoked.await_args
    assert args[0].identity is identity and args[0].run_id == record.run_id
    assert args[0].owner_id == record.owner_id
    if mode == "apply":
        assert args[1].model_dump() == {"package_id": record.detail.input_binding.package_id,
            "payload_name": "products", "base_revision_id": record.base_ifc_revision_id}
        assert kwargs == {"idempotency_key": "server-transport-action"}
        data.resolve_package.assert_awaited_once_with(args[0], "data_001")
        core.recover.assert_not_awaited()
    else:
        assert kwargs == {"origin_run_id": record.run_id, "operation_id": record.operation_id}
        core.edit.assert_not_awaited()
        data.resolve_package.assert_not_awaited()
    data.get_ifc_products_recovery_operation.assert_awaited_once_with(args[0],
        origin_run_id=record.run_id, operation_id=record.operation_id)


@pytest.mark.parametrize("by_reference", [False, True])
async def test_runner_edit_operation_report_read_failure_preserves_original_commit(by_reference):
    from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError
    from CAD2BIM_backend.internal_data_api import create_internal_data_router
    from tests.unit.mcp.test_direct_ifc_products_provider import _edit_inspection

    record = _edit_inspection().operation.operation
    identity = TenantExecutionContext(tenant_id=record.tenant_id, principal_id="person", task_id=record.task_id,
        actor_kind=TenantActorKind.RUNNER, command_id="read-action")
    references = OpaqueReferenceService(MemoryReferenceIndex(), reference_codec())
    original_ref = references.operation_reference(identity,
        origin_run_id=record.run_id, operation_id=record.operation_id)
    error = DataRuntimeApplicationError("OPERATION_INTEGRITY_ERROR", "The saved edit report is unavailable",
        operation_alias="op_006")
    error.operation_id, error.operation_status = record.operation_id, record.status.value
    data = SimpleNamespace(get_operation=AsyncMock(side_effect=error),
        get_operation_by_locator=AsyncMock(side_effect=error))

    async def authorized(_request, *, tool_name):
        assert tool_name == "operation_get"
        return SimpleNamespace(owner_id=record.owner_id, task_id=record.task_id, run_id=record.run_id), identity

    app = FastAPI()
    app.include_router(create_internal_data_router(authorized=authorized, bind_identity=lambda _: nullcontext(),
        data_runtime=data, data_query=None, data_query_sql=None, data_query_python=None, data_json=None,
        data_transform=None, code_discovery=None, authoring_operation_result=None,
        geometry_inspection_operation_result=None, geometry_inspection=None, references=references))
    body = (dict(mode="operation_reference", operation_ref=original_ref) if by_reference
        else dict(mode="operation", operation_alias="op_006"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        response = await client.post("/data/operation_get", json=body)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["operation_ref"] == original_ref and detail["operation_status"] == "succeeded"
    assert detail["operation_alias"] == "op_006" and detail["code"] == "OPERATION_INTEGRITY_ERROR"
    rendered = str(backend_domain_tool_error(BackendDomainError(409, detail)))
    assert "operation_status=succeeded" in rendered and f"operation_ref={original_ref}" in rendered
    (data.get_operation if by_reference else data.get_operation_by_locator).assert_not_awaited()


@pytest.mark.parametrize("entry", ["first", "recover", "operation_get"])
async def test_runner_edit_actual_query_failure_retains_public_original_read_error(entry):
    from CAD2BIM_backend.internal_data_api import create_internal_data_router
    from tests.unit.mcp.test_direct_ifc_products_provider import _edit_diagnostics_query_runtime
    inspection, scope, failure, queries, inspect = _edit_diagnostics_query_runtime()
    record = inspection.operation.operation
    refs = OpaqueReferenceService(MemoryReferenceIndex(), reference_codec())
    reference = refs.operation_reference(scope.identity, origin_run_id=scope.run_id, operation_id=record.operation_id)

    async def authorized(_request, *, tool_name):
        assert tool_name == ("operation_get" if entry == "operation_get" else "ifc_products_edit")
        return SimpleNamespace(owner_id=scope.owner_id, task_id=scope.identity.task_id,
            run_id=scope.run_id, action_id=scope.identity.command_id), scope.identity
    data = SimpleNamespace(get_operation_by_locator=AsyncMock(side_effect=inspect),
        get_ifc_products_recovery_operation=AsyncMock(side_effect=inspect),
        resolve_package=AsyncMock(return_value=inspection.input_packages["products"]))
    core = SimpleNamespace(edit=AsyncMock(return_value=SimpleNamespace(operation_id=record.operation_id,
        operation_alias=inspection.operation.catalog_entry.alias, outcome="succeeded")), recover=AsyncMock(side_effect=inspect))
    app = FastAPI()
    app.include_router(create_internal_ifc_router(authorized=authorized, bind_identity=lambda _: nullcontext(),
        ifc=SimpleNamespace(products_edit=core), data_runtime=data, references=refs))
    app.include_router(create_internal_data_router(authorized=authorized, bind_identity=lambda _: nullcontext(),
        data_runtime=data, data_query=None, data_query_sql=None, data_query_python=None, data_json=None,
        data_transform=None, code_discovery=None, authoring_operation_result=None,
        geometry_inspection_operation_result=None, geometry_inspection=None, references=refs))
    body = ({"mode": "operation_reference", "operation_ref": reference} if entry == "operation_get"
        else {"mode": "recover", "operation_ref": reference} if entry == "recover"
        else {"mode": "apply", "package_alias": "data_001", "payload_name": "products"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        response = await client.post("/data/operation_get" if entry == "operation_get" else "/ifc/products/edit", json=body)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert (detail["code"], detail["phase"], detail["operation_status"]) == (failure.code, "read", "succeeded")
    assert detail["diagnostic"] == failure.diagnostic and detail["operation_ref"] == reference
    assert detail["operation_alias"] == inspection.operation.catalog_entry.alias
    rendered = str(backend_domain_tool_error(BackendDomainError(409, detail)))
    assert '"payload_name":"checks"' in rendered and "operation_status=succeeded" in rendered
    queries.query_rows.assert_awaited_once()


async def test_runner_product_read_route_uses_calling_scope_and_no_write_identity():
    from CAD2BIM_backend.domain.ifc_products_read import IfcProductsReadFacts, IfcProductsReadItem, IfcApplyProductMatch
    identity = TenantExecutionContext(tenant_id="tenant", principal_id="person", task_id="task",
        actor_kind=TenantActorKind.RUNNER, command_id="read-action")

    async def authorized(_request, **kwargs):
        assert kwargs["tool_name"] == "ifc_products_read"
        return SimpleNamespace(owner_id="owner", task_id="task", run_id="calling-run", action_id="read-action"), identity

    guid = "0000000000000000000000"
    core = SimpleNamespace(read=AsyncMock(return_value=SimpleNamespace(
        facts=IfcProductsReadFacts(view="current", items=(IfcProductsReadItem(global_id=guid,
            apply_match=IfcApplyProductMatch(status="not_checked")),)),
        operation=None, input_alias=None, report_alias=None)))
    app = FastAPI()
    app.include_router(create_internal_ifc_router(authorized=authorized,
        bind_identity=lambda _: nullcontext(), ifc=SimpleNamespace(products_read=core)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        response = await client.post("/ifc/products/read", json={"mode": "from_ifc", "global_ids": [guid]})
        rejected = await client.post("/ifc/products/read", json={"mode": "from_apply", "operation_ref": "cur.original", "product_keys": ["a"], "groups": {}})
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["apply_match"]["status"] == "not_checked"
    assert rejected.status_code == 422
    assert core.read.await_count == 1
    assert core.read.await_args.args[0].run_id == "calling-run"
    assert core.read.await_args.args[0].identity is identity
    assert core.read.await_args.kwargs == {"operation_locator": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["preflight", "committed_readback"])
async def test_runner_apply_route_preserves_input_code_and_committed_readback_fact(kind):
    from CAD2BIM_backend.application.product_apply_service import ProductApplyCallError
    from CAD2BIM_backend.domain.ifc_model import IfcCommittedVerificationError
    identity = TenantExecutionContext(tenant_id="tenant", principal_id="person", task_id="task",
                                      actor_kind=TenantActorKind.RUNNER, command_id="recover-transport")

    async def authorized(_request, **_kwargs):
        return SimpleNamespace(owner_id="owner", task_id="task", run_id="run", action_id=identity.command_id), identity

    error = (ProductApplyCallError("PRODUCT_INPUT_FORMAT_INVALID", "Expected one Parquet payload")
        if kind == "preflight" else IfcCommittedVerificationError("Original bytes failed readback",
            operation_alias="op_003", revision_id="original-revision"))
    app = FastAPI()
    app.include_router(create_internal_ifc_router(authorized=authorized, bind_identity=lambda _: nullcontext(),
        ifc=SimpleNamespace(products_apply=SimpleNamespace(recover=AsyncMock(side_effect=error))),
        references=SimpleNamespace(operation_locator=lambda *_: ("run", "original"))))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        response = await client.post("/ifc/products/apply", json={"mode": "recover", "operation_ref": "cur.original"})
    detail = response.json()["detail"]
    assert response.status_code == 409 and detail["code"] == error.code
    assert detail["operation_ref"] == "cur.original"
    assert detail["phase"] == ("preflight" if kind == "preflight" else "storage_verification")
    assert detail["operation_status"] == ("unknown" if kind == "preflight" else "succeeded")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"values": ["cur.first", "cur.second"]},
    {"values": ["cur.first"]}, {"values": ["cur.first", 1]},
    {"values": "cur.first"}, {},
])
async def test_reference_client_posts_ordered_batch_and_rejects_malformed_values(payload):
    client = RunnerStateBackendClient("http://backend")
    await client._client.aclose()
    requests = []

    def respond(request):
        import json
        requests.append((request.url.path, request.headers.get("authorization"),
            request.headers.get("x-cad2bim-state-binding"), json.loads(request.content)))
        return httpx.Response(200, json=payload)

    client._client = httpx.AsyncClient(base_url="http://backend", transport=httpx.MockTransport(respond))
    items = [{"value": "gv_first", "kinds": ["gv"]}, {"value": "gd_second", "kinds": ["gd"]}]
    try:
        if payload == {"values": ["cur.first", "cur.second"]}:
            assert await client.translate_references(access_token="current-tool-token", direction="expand", items=items) == (
                "cur.first", "cur.second")
        else:
            with pytest.raises(BackendDomainError) as rejected:
                await client.translate_references(access_token="current-tool-token", direction="expand", items=items)
            assert rejected.value.status_code == 502 and rejected.value.detail is None
    finally:
        await client.aclose()
    assert requests == [("/internal/domain/references/translate", "Bearer current-tool-token", None,
        {"direction": "expand", "items": items})]


def test_settled_ifc_error_keeps_original_phase_and_alias_in_both_entries() -> None:
    settled = IfcSettledOperationError(
        code="IFC_SPATIAL_NOT_EMPTY", message="original blocker",
        phase="ifc_validation", operation_alias="op_004",
    )
    direct = ifc_direct_error(settled)
    assert (direct.code, str(direct), direct.phase, direct.retryable,
            direct.operation_alias) == (
        "IFC_SPATIAL_NOT_EMPTY", "original blocker", "ifc_validation", False, "op_004",
    )
    runner = backend_domain_tool_error(BackendDomainError(409, {
        "code": settled.code, "message": str(settled), "phase": settled.phase,
        "retryable": settled.retryable, "operation_alias": settled.operation_alias,
        "operation_status": "failed",
    }))
    assert "IFC_SPATIAL_NOT_EMPTY; phase=ifc_validation; retryable=false" in str(runner)
    assert "operation_alias=op_004; operation_status=failed; message=original blocker" in str(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,body", [
    ("ifc_geometry_resource_write", {"mode": "create", "display_name": "Geometry",
        "usage_instructions": {"action": "set", "value": "Use local metre geometry."},
        "source": {"source_kind": "mapped_model", "source_files": [{"path": "main.py", "source": "def build(ctx): return None"}]}}),
    ("ifc_product_family_write", {"mode": "create", "source": {"source_kind": "published", "family_version_alias": "cur.fixture"}}),
    ("ifc_authoring_publish", {"root_kind": "geometry", "draft_ref": "cur.fixture", "expected_edit_token": 1}),
])
@pytest.mark.parametrize("proof", ["not_started", "unknown", "query_failed", "failed"])
async def test_authoring_entry_projects_only_its_owner_non_admission_proof(tool, body, proof):
    identity = TenantExecutionContext(tenant_id="tenant", principal_id="person", task_id="task",
        actor_kind=TenantActorKind.RUNNER, command_id="original-action")
    error = AuthoringDraftError("AUTHORING_SOURCE_INVALID", "Invalid fixed Python input",
        operation_status="failed" if proof == "failed" else None,
        operation_alias="op_003" if proof == "failed" else None)
    checks = []

    async def authorized(_request, *, tool_name):
        assert tool_name == tool
        return SimpleNamespace(owner_id="owner", run_id="run", action_id="original-action"), identity

    async def refuse(scope, **_kwargs):
        assert scope.identity.command_id == "original-action"
        raise error

    async def prove(scope, *, request_identity, error):
        checks.append((scope.identity.command_id, request_identity, error))
        if proof == "query_failed":
            raise RuntimeError("Operation query unavailable")
        return proof

    service = SimpleNamespace(save_draft_from_aliases=refuse, publish_draft=refuse,
        request_rejection_status=prove, _publication=SimpleNamespace(request_rejection_status=prove))
    app = FastAPI()
    app.include_router(create_internal_authoring_router(authorized=authorized,
        bind_identity=lambda _: nullcontext(), data_runtime=object(),
        authoring=SimpleNamespace(geometry_authoring=service, family_authoring=service)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        response = await client.post(f"/authoring/{tool}", json=body)
    assert response.status_code == 409
    detail = response.json()["detail"]
    expected = proof if proof in {"not_started", "failed"} else "unknown"
    assert detail["operation_status"] == expected
    assert checks == ([] if proof == "failed" else [("original-action", "original-action", error)])
    assert f"operation_status={expected}" in str(backend_domain_tool_error(BackendDomainError(409, detail)))


def test_source_and_cas_errors_keep_actionable_diagnostics_without_asserting_admission():
    source = GeometryAuthoringSourceError(GeometryAuthoringSourceFailure.MATCH_NOT_FOUND,
        "old_text did not match the current source")
    response = _authoring_error(source)
    assert response.detail["code"] == "GEOMETRY_SOURCE_MATCH_NOT_FOUND"
    assert response.detail["diagnostic"] == {"failure": "match_not_found"}
    assert "operation_status=unknown" in str(backend_domain_tool_error(BackendDomainError(409, response.detail)))
    assert _authoring_error(AuthoringDraftChanged("stale")).detail["code"] == "DRAFT_CHANGED"


@pytest.mark.parametrize("status,extra", [
    (500, {}), (409, {"operation_alias": "op_003"}),
    (409, {"attempt_alias": "cur.attempt"}), (409, {"retryable": True}),
])
def test_non_admission_projection_rejects_contradictory_or_noncontract_proof(status, extra):
    rendered = backend_domain_tool_error(BackendDomainError(status, {
        "code": "DRAFT_CHANGED", "message": "Rejected", "phase": "preflight",
        "retryable": False, "operation_status": "not_started", **extra,
    }))
    assert "operation_status=unknown" in str(rendered)


@pytest.mark.asyncio
@pytest.mark.parametrize("root_kind,policy", [
    ("geometry", GEOMETRY_AUTHORING_EXECUTION), ("family", PRODUCT_AUTHORING_EXECUTION),
])
async def test_runner_publish_http_deadline_uses_current_authoring_execution_budget(root_kind, policy):
    client = RunnerStateBackendClient("http://backend")
    calls = []

    async def post(path, *, access_token, body, timeout):
        calls.append((path, access_token, body, timeout))
        return {"operation_alias": "op_003"}

    client._post_domain = post
    body = {"root_kind": root_kind, "draft_ref": "cur.exact", "expected_edit_token": 2}
    try:
        assert await client.call_authoring(access_token="token", tool_name="ifc_authoring_publish", body=body) == {"operation_alias": "op_003"}
    finally:
        await client.aclose()
    assert calls == [("/internal/domain/authoring/ifc_authoring_publish", "token", body,
        policy.total_seconds + RUNTIME_RESPONSE_MARGIN_SECONDS)]


def test_internal_data_error_hides_infrastructure_code_and_sql_details() -> None:
    class SqlFailure(RuntimeError):
        code = "f405"

    response = _domain_error(SqlFailure("permission denied; SELECT private_value"))
    assert response.status_code == 500
    assert response.detail == {
        "code": "BACKEND_DOMAIN_FAILURE",
        "message": "Domain request failed",
        "phase": "read",
        "retryable": False,
    }

    authoring_response = _authoring_error(SqlFailure("permission denied; SELECT private_value"))
    assert authoring_response.status_code == 500
    assert authoring_response.detail == response.detail


def _protocol() -> ExecutionProtocolBinding:
    return ExecutionProtocolBinding(
        bundle_id="domain_entry_test",
        module="workflow",
        stage_key="cad2bim_workflow",
    )


@pytest.mark.asyncio
async def test_domain_write_uses_saved_action_without_state_binding_and_rejects_stale_claim() -> None:
    protocol = _protocol()
    identity = TenantExecutionContext(
        tenant_id="tenant-one",
        principal_id="principal-one",
        task_id="task-one",
        actor_kind=TenantActorKind.RUNNER,
        command_id="claim-generation-one",
    )
    grant = RunnerCheckpointGrant(
        graph_context=RunnerGraphContext(scope=InvocationScope(
            identity=identity,
            owner_id="tenant-one",
            run_id="run-one",
            execution_mode=ExecutionMode.GRAPH,
            catalog_frontier=None,
        )),
        thread_id="thread-one",
        runner_id="runner-one",
        lease_token="private-lease-one",
        writer_generation=1,
    )
    codec = RunnerDomainTokenCodec(_SECRET)
    action_id = f"call:{uuid4().hex}"
    token = codec.issue(grant, action_id=action_id, tool_name="ifc_project_update")
    run = SimpleNamespace(
        owner_id="tenant-one", tenant_id="tenant-one", task_id="task-one",
        thread_id="thread-one", status=RunStatus.RUNNING,
        execution_mode=ExecutionMode.GRAPH,
        execution_protocol_status=ExecutionProtocolStatus.PINNED,
        claimed_by="runner-one", writer_generation=1,
        cancel_requested=False,
        lease_token="private-lease-one",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        execution_protocol=protocol,
        module="workflow", stage_key="cad2bim_workflow",
        require_execution_identity=lambda: ("tenant-one", "principal-one"),
    )
    calls: list[tuple[object, object, str]] = []
    geometry_calls = []

    class GeometryQuery:
        async def resolve_attempt_alias(self, scope, attempt_alias):
            geometry_calls.append(("resolve", scope, attempt_alias))
            return "parent-exact-id"

        async def repair(
            self, scope, parent_attempt_id, edits, request_identity, page_size=100,
            *, deadline=None,
        ):
            geometry_calls.append(("repair", scope, parent_attempt_id, edits, request_identity, page_size))
            raise GeometryQueryApplicationError(
                "GEOMETRY_QUERY_REPAIR_INVALID", "literal unmatched", phase="preflight",
                attempt_alias="cur.parent",
            )

    class Repository:
        async def get_run_with_execution_identity(self, owner_id, run_id):
            assert (owner_id, run_id) == ("tenant-one", "run-one")
            return run

    class Project:
        async def update(self, scope, command, *, idempotency_key):
            calls.append((scope, command, idempotency_key))
            return SimpleNamespace(model_dump=lambda **_: {
                "operation_alias": "op_001",
                "project": {"global_id": "0" * 22, "name": "new name", "description": "saved MD"},
            })

    @contextmanager
    def bind_identity(value):
        assert value.command_id == action_id
        yield

    app = FastAPI()
    references = OpaqueReferenceService(MemoryReferenceIndex(), reference_codec())
    data = SimpleNamespace(
        runtime=object(), query=object(), query_sql=object(), query_python=object(),
        json=object(), transform=object(), code_discovery=object(), references=references,
    )
    app.include_router(create_internal_domain_router(
        codec=codec,
        runner_repository=Repository(),
        bind_identity=bind_identity,
        execution_protocol=protocol,
        data=data,
        ifc=SimpleNamespace(project=Project()),
        authoring=SimpleNamespace(
            geometry_inspection=SimpleNamespace(operation_result=None),
            geometry_query=GeometryQuery(),
            family_catalog=SimpleNamespace(version_ref_for_visible_id=None),
        ),
    ))
    request = {"command": {"patch": {"name": "new name", "description": "saved MD"}}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.post("/internal/domain/ifc/project/update", headers=headers, json=request)
        assert response.status_code == 200
        assert calls[0][2] == action_id
        assert calls[0][0].run_id == "run-one"
        assert calls[0][1].patch.description == "saved MD"

        query_token = codec.issue(grant, action_id=action_id, tool_name="ifc_geometry_query_python")
        repaired = await client.post(
            "/internal/domain/authoring/ifc_geometry_query_python",
            headers={"Authorization": f"Bearer {query_token}"},
            json={"mode": "repair", "parent_attempt_alias": "cur.parent",
                  "edits": [{"old_text": "bad", "new_text": "good"}], "page_size": 17},
        )
        assert repaired.status_code == 409
        assert repaired.json()["detail"]["code"] == "GEOMETRY_QUERY_REPAIR_INVALID"
        assert repaired.json()["detail"]["phase"] == "preflight"
        assert geometry_calls[0][0] == "resolve"
        assert geometry_calls[0][2] == "cur.parent"
        _, repair_scope, parent_id, edits, request_identity, page_size = geometry_calls[1]
        assert repair_scope is geometry_calls[0][1]
        assert (repair_scope.owner_id, repair_scope.run_id) == ("tenant-one", "run-one")
        assert (parent_id, request_identity, page_size) == ("parent-exact-id", action_id, 17)
        assert (edits[0].old_text, edits[0].new_text) == ("bad", "good")


        version = references._codec.encode({"domain": "geometry_catalog_reference", "reference_kind": "resource_version"})
        draft = references._codec.encode({"domain": "authoring_draft", "root_kind": "geometry"})
        translate_path = "/internal/domain/references/translate"
        body = {"direction": "shorten", "items": [
            {"value": draft, "kinds": ["gd"]}, {"value": version, "kinds": ["gv"]},
            {"value": draft, "kinds": ["gd"]}]}
        converted = await client.post(translate_path, headers=headers, json=body)
        assert converted.status_code == 200
        handles = converted.json()["values"]
        assert handles[0].startswith("gd_") and handles[1].startswith("gv_") and handles[0] == handles[2]
        expanded = await client.post(translate_path, headers=headers, json={"direction": "expand", "items": [
            {"value": handles[0], "kinds": ["gd"]}, {"value": handles[1], "kinds": ["gv"]}]})
        assert expanded.json() == {"values": [draft, version]}
        wrong_kind = await client.post(translate_path, headers=headers, json={"direction": "expand", "items": [
            {"value": handles[0], "kinds": ["fv"]}]})
        assert wrong_kind.status_code == 409 and wrong_kind.json()["detail"]["code"] == "REFERENCE_INVALID"
        assert "operation_status" not in wrong_kind.json()["detail"]
        assert "operation_alias" not in wrong_kind.json()["detail"]
        for invalid in [
            {**body, "tenant_id": "another-tenant"}, {**body, "direct_session_alias": "session_001"},
            {"direction": "expand", "items": [{"value": handles[0], "kinds": ["unsupported"]}]},
            {"direction": "expand", "items": [{"value": handles[0], "kinds": ["gd"], "principal_id": "other"}]},
        ]:
            assert (await client.post(translate_path, headers=headers, json=invalid)).status_code == 422
        assert (await client.post(translate_path, headers=headers,
            json={"direction": "shorten", "items": []})).json() == {"values": []}

        # An actual State-purpose token signed with the same deployment secret is not write access.
        now = int(datetime.now(timezone.utc).timestamp())
        state_only = jwt.encode({
            "iss": "urn:cad2bim:runner-state", "aud": "cad2bim-runner-state",
            "iat": now, "nbf": now, "exp": now + 60, "jti": "state-only",
            "purpose": "state-service-access", "run_id": "run-one",
        }, _SECRET, algorithm="HS256")
        response = await client.post(
            "/internal/domain/ifc/project/update",
            headers={"Authorization": f"Bearer {state_only}"}, json=request,
        )
        assert response.status_code == 403
        assert (await client.post(translate_path,
            headers={"Authorization": f"Bearer {state_only}"}, json=body)).status_code == 403

        run.writer_generation = 2
        assert (await client.post(
            "/internal/domain/ifc/project/update", headers=headers, json=request,
        )).status_code == 403
        assert (await client.post(translate_path, headers=headers, json=body)).status_code == 403
        run.writer_generation = 1
        run.task_id = "another-task"
        assert (await client.post(
            "/internal/domain/ifc/project/update", headers=headers, json=request,
        )).status_code == 403
        run.task_id = "task-one"
        run.claimed_by = "another-runner"
        assert (await client.post(
            "/internal/domain/ifc/project/update", headers=headers, json=request,
        )).status_code == 403
        run.claimed_by = "runner-one"
        run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert (await client.post(
            "/internal/domain/ifc/project/update", headers=headers, json=request,
        )).status_code == 403
        assert len(calls) == 1
