from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator
from mcp.shared.exceptions import McpError
from sse_starlette.sse import AppStatus

from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.identity import AuthenticatedSubject
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest
from CAD2BIM_backend.domain.state_read_binding import ExactStateReadBinding, root_field_sources
from CAD2BIM_backend.infrastructure.state_access_tokens import (
    StateAccessPair,
    StateAccessTokenCodec,
)
from CAD2BIM_mcp.runner.server import create_runner_state_mcp
from CAD2BIM_mcp.runner import backend_client as backend_client_module
from CAD2BIM_mcp.runner.backend_client import RunnerStateBackendClient
from CAD2BIM_backend.infrastructure.state_access_tokens import StateAccessDenied
from CAD2BIM_backend.infrastructure.runner_domain_tokens import RunnerDomainTokenCodec
from CAD2BIM_mcp.runner.resources import request_domain_credential
from CAD2BIM_mcp.direct.tool_support import register_provider_document
from CAD2BIM_mcp.tool_docs.common import common_usage_document


def _pair(codec: StateAccessTokenCodec, run_id: str) -> StateAccessPair:
    checkpoint_map = (("", "root-one"),)
    binding = ExactStateReadBinding(
        owner_id=f"tenant-{run_id}",
        task_id=f"task-{run_id}",
        run_id=run_id,
        thread_id=f"thread-{run_id}",
        checkpoint_cursor=CheckpointCursor(
            thread_id=f"thread-{run_id}",
            checkpoint_id="root-one",
            checkpoint_ns="",
            root_checkpoint_id="root-one",
            checkpoint_map=checkpoint_map,
            checkpoint_digest=checkpoint_cursor_digest(
                checkpoint_id="root-one",
                checkpoint_ns="",
                checkpoint_map=checkpoint_map,
            ),
        ),
        field_sources=root_field_sources("root-one"),
    )
    now = datetime.now(timezone.utc)
    subject = AuthenticatedSubject(
        issuer="https://identity.example.test",
        subject=run_id,
        audience="cad2bim-api",
        authenticated_at=now,
        token_expires_at=now + timedelta(minutes=2),
    )
    return codec.issue_completed(subject, binding)


class _Backend:
    def __init__(self, codec: StateAccessTokenCodec) -> None:
        self.codec = codec
        self.authorized_runs: list[str] = []
        self.closed = False
        self.revoked_runs: set[str] = set()

    async def aclose(self) -> None:
        self.closed = True

    async def authorize(self, *, access_token: str, read_binding: str) -> None:
        verified = self.codec.verify_pair(access_token, read_binding)
        self.authorized_runs.append(verified.binding.run_id)
        if verified.binding.run_id in self.revoked_runs:
            raise StateAccessDenied("Run access revoked")

    async def list_resources(self, *, access_token: str, read_binding: str):
        run_id = self.codec.verify_pair(access_token, read_binding).binding.run_id
        return (
            {
                "uri": "state://requirement-md",
                "name": "requirement-md",
                "description": f"saved State of {run_id}",
                "mime_type": "text/markdown",
            },
        )

    async def read_resource(self, *, access_token: str, read_binding: str, uri: str):
        run_id = self.codec.verify_pair(access_token, read_binding).binding.run_id
        assert uri == "state://requirement-md"
        return (
            {
                "uri": uri,
                "name": "requirement-md",
                "description": f"saved State of {run_id}",
                "mime_type": "text/markdown",
            },
            f"{run_id} saved markdown",
        )


def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


def _transport(app, pair: StateAccessPair, *, read_binding: str | None = None):
    headers = {"x-cad2bim-state-binding": read_binding or pair.read_binding}

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), **kwargs)

    return StreamableHttpTransport(
        "http://testserver/mcp",
        headers=headers,
        auth=pair.access_token,
        httpx_client_factory=factory,
    )


@pytest.mark.asyncio
async def test_http_provider_keeps_state_and_contract_resources_request_bound() -> None:
    codec = StateAccessTokenCodec(b"state-provider-test-secret-material-32-bytes")
    backend = _Backend(codec)
    server = create_runner_state_mcp(
        codec=codec,
        backend=backend,
    )
    contract_uri = "cad2bim://contracts/common/usage"
    register_provider_document(server, tool_name="common", document_name="usage",
        description="Shared current tool usage", document=common_usage_document())
    app = server.http_app(stateless_http=True)
    first = _pair(codec, "run-one")
    second = _pair(codec, "run-two")

    async with app.lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as anonymous:
            response = await anonymous.post(
                "/mcp",
                headers={"x-cad2bim-state-binding": first.read_binding},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert response.status_code == 401

        async with Client(_transport(app, first)) as client:
            tools = await client.list_tools()
            assert tools == []
            authorizations = len(backend.authorized_runs)
            assert {str(item.uri) for item in await client.list_resources()} == {
                "state://requirement-md", contract_uri,
            }
            assert (await client.read_resource("state://requirement-md"))[0].text == (
                "run-one saved markdown"
            )
            assert len(backend.authorized_runs) == authorizations
            assert json.loads((await client.read_resource(contract_uri))[0].text) == (
                common_usage_document()
            )
            assert len(backend.authorized_runs) == authorizations + 1
            with pytest.raises(McpError):
                await client.read_resource("state://unknown")
            backend.revoked_runs.add("run-one")
            with pytest.raises(McpError, match="revoked"):
                await client.read_resource("state://unknown")
            with pytest.raises(McpError, match="revoked"):
                await client.read_resource(contract_uri)
            with pytest.raises(McpError, match="revoked"):
                await client.list_tools()
            backend.revoked_runs.clear()

        async with Client(_transport(app, second)) as client:
            assert (await client.read_resource("state://requirement-md"))[0].text == (
                "run-two saved markdown"
            )
            assert await client.list_tools() == []
        assert "run-one" in backend.authorized_runs and "run-two" in backend.authorized_runs

        async with Client(_transport(app, first, read_binding=second.read_binding)) as client:
            with pytest.raises(McpError, match="binding"):
                await client.list_tools()
            with pytest.raises(McpError, match="binding"):
                await client.list_resources()
            with pytest.raises(McpError, match="binding"):
                await client.read_resource("state://requirement-md")
            with pytest.raises(McpError, match="binding"):
                await client.read_resource("state://unknown")

        async def read_as(pair: StateAccessPair) -> str:
            async with Client(_transport(app, pair)) as client:
                await client.list_resources()
                return (await client.read_resource("state://requirement-md"))[0].text

        assert await asyncio.gather(read_as(first), read_as(second)) == [
            "run-one saved markdown",
            "run-two saved markdown",
        ]
    assert backend.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_inside", [False, True])
async def test_backend_client_reuses_transport_without_identity_and_closes(
    tmp_path, monkeypatch, fail_inside: bool,
) -> None:
    actual_client = httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []
    received: list[tuple[str, str]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        received.append((request.headers["authorization"], request.headers["x-cad2bim-state-binding"]))
        await asyncio.sleep(0)
        return httpx.Response(200, json={"resources": []})

    def create_client(**kwargs):
        client = actual_client(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(backend_client_module.httpx, "AsyncClient", create_client)
    backend = RunnerStateBackendClient("http://backend")
    server = create_runner_state_mcp(
        codec=StateAccessTokenCodec(b"state-provider-test-secret-material-32-bytes"),
        backend=backend,
    )
    app = server.http_app(stateless_http=True)
    try:
        async with app.lifespan(app):
            await asyncio.gather(
                backend.list_resources(access_token="alice", read_binding="binding-a"),
                backend.list_resources(access_token="bob", read_binding="binding-b"),
            )
            assert len(clients) == 1
            assert "authorization" not in clients[0].headers
            assert "x-cad2bim-state-binding" not in clients[0].headers
            assert not clients[0].is_closed
            if fail_inside:
                raise RuntimeError("lifespan failure")
    except* RuntimeError as group:
        assert fail_inside and len(group.exceptions) == 1
        assert str(group.exceptions[0]) == "lifespan failure"
    assert sorted(received) == [("Bearer alice", "binding-a"), ("Bearer bob", "binding-b")]
    assert clients[0].is_closed


@pytest.fixture
def fresh_sse_exit_event():
    # SSE's module-level exit event belongs to the prior async test loop.
    AppStatus.should_exit_event = None
    yield
    AppStatus.should_exit_event = None


@pytest.mark.asyncio
async def test_domain_tool_uses_its_own_credential_without_state_binding(
    tmp_path, fresh_sse_exit_event
) -> None:
    secret = b"runner-domain-provider-test-secret-32-bytes"
    state = StateAccessTokenCodec(secret)
    domain = RunnerDomainTokenCodec(secret)

    class Backend(_Backend):
        def __init__(self):
            super().__init__(state)
            self.domain_calls: list[str] = []

        async def authorize_domain(self, *, access_token: str) -> None:
            self.domain_calls.append(domain.verify(access_token).action_id)

        async def update_ifc_project(self, *, access_token: str, command):
            verified = domain.verify(access_token)
            assert verified.tool_name == "ifc_project_update"
            return {"action_id": verified.action_id, "description": command["patch"]["description"]}

    backend = Backend()
    server = create_runner_state_mcp(
        codec=state,
        domain_codec=domain,
        backend=backend,
    )

    @server.tool(name="test_update_ifc_project")
    async def test_update_ifc_project(name: str, description: str) -> dict[str, str]:
        token = request_domain_credential()
        return await backend.update_ifc_project(
            access_token=token,
            command={"patch": {"name": name, "description": description}},
        )

    grant = SimpleNamespace(
        scope=SimpleNamespace(
            identity=SimpleNamespace(task_id="task-one", principal_id="principal-one"),
            owner_id="tenant-one", run_id="run-one",
        ),
        thread_id="thread-one", runner_id="runner-one",
        lease_token="private-lease", writer_generation=1,
    )
    domain_token = domain.issue(grant, action_id="call-one", tool_name="ifc_project_update")
    app = server.http_app(stateless_http=True)

    def transport(token: str, *, binding: str | None = None):
        def factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), **kwargs)

        return StreamableHttpTransport(
            "http://testserver/mcp",
            headers={} if binding is None else {"x-cad2bim-state-binding": binding},
            auth=token,
            httpx_client_factory=factory,
        )

    async with app.lifespan(app):
        async with Client(transport(domain_token)) as client:
            tools = await client.list_tools()
            project_tool = next(item for item in tools if item.name == "test_update_ifc_project")
            Draft202012Validator.check_schema(project_tool.inputSchema)
            assert set(project_tool.inputSchema["properties"]) == {"name", "description"}
            assert not any(
                {"allOf", "if", "then", "else", "not"} & set(node)
                for node in _walk(project_tool.inputSchema)
                if isinstance(node, Mapping)
            )
            assert "unknown" not in json.dumps(project_tool.inputSchema).lower()
            result = await client.call_tool(
                "test_update_ifc_project", {"name": "new name", "description": "saved MD"}
            )
            assert result.structured_content == {
                "action_id": "call-one", "description": "saved MD"
            }
            with pytest.raises(McpError):
                await client.list_resources()

        read_only = _pair(state, "run-one")
        async with Client(transport(read_only.access_token, binding=read_only.read_binding)) as client:
            with pytest.raises(ToolError):
                await client.call_tool(
                    "test_update_ifc_project", {"name": "new name", "description": "saved MD"}
                )
    assert backend.domain_calls == ["call-one"]
    assert backend.closed


@pytest.mark.asyncio
async def test_production_runner_domain_provider_exposes_current_safe_tools(
    tmp_path, fresh_sse_exit_event
) -> None:
    secret = b"runner-domain-schema-test-secret-32-bytes"
    state = StateAccessTokenCodec(secret)
    domain = RunnerDomainTokenCodec(secret)

    class Backend(_Backend):
        async def authorize_domain(self, *, access_token: str) -> None:
            domain.verify(access_token)

    server = create_runner_state_mcp(
        codec=state,
        domain_codec=domain,
        backend=Backend(state),
    )
    grant = SimpleNamespace(
        scope=SimpleNamespace(
            identity=SimpleNamespace(task_id="task-one", principal_id="principal-one"),
            owner_id="tenant-one", run_id="run-one",
        ),
        thread_id="thread-one", runner_id="runner-one",
        lease_token="private-lease", writer_generation=1,
    )
    token = domain.issue(grant, action_id="call-one", tool_name="ifc_project_update")
    app = server.http_app(stateless_http=True)

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), **kwargs)

    transport = StreamableHttpTransport(
        "http://testserver/mcp", headers={}, auth=token, httpx_client_factory=factory
    )
    expected = set((
        "data_package_list data_describe_package data_get_package_description "
        "data_query_rows data_query_sql data_query_python data_read_json data_derive "
        "code_list code_read operation_list operation_get "
        "ifc_model_initialize ifc_model_info ifc_model_configure_geometry_basis "
        "ifc_representation_context_list ifc_project_update ifc_spatial_structure_list "
        "ifc_spatial_structure_apply ifc_query_python ifc_products_apply ifc_products_read "
        "ifc_geometry_resource_list ifc_geometry_resource_info "
        "ifc_geometry_resource_source_read ifc_geometry_resource_write "
        "ifc_geometry_preview ifc_geometry_query_python "
        "ifc_product_family_list ifc_product_family_info ifc_product_family_write "
        "ifc_product_realization_preview ifc_authoring_publish"
    ).split())
    async with app.lifespan(app):
        async with Client(transport) as client:
            assert client.initialize_result is not None
            guide = client.initialize_result.instructions
            assert guide is not None and "operation_status=failed" in guide
            assert "retryable=false alone does not prove FAILED" in guide
            tools = {tool.name: tool for tool in await client.list_tools()}
    assert set(tools) == expected
    for name in expected:
        for schema in (tools[name].inputSchema, tools[name].outputSchema):
            if schema is None:
                continue
            Draft202012Validator.check_schema(schema)
            assert not any(
                {"allOf", "if", "then", "else", "not"} & set(node)
                for node in _walk(schema)
                if isinstance(node, Mapping)
            ), name
            assert not any(
                node.get("type") == "unknown"
                for node in _walk(schema) if isinstance(node, Mapping)
            ), name
