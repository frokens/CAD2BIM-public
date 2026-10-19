"""Saved State Resource drives a real Runner IFC Project write and exact query."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
from typing import Any, TypedDict
from uuid import uuid4

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.types import TextContent
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from CAD2BIM_backend.bootstrap import BackendRuntime, open_backend_runtime
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity, InitializeIfcModelCommand
from CAD2BIM_backend.domain.runner_context import RunnerGraphContext
from CAD2BIM_backend.infrastructure.langgraph.node_read_binding import resolve_node_read_binding
from CAD2BIM_backend.infrastructure.mcp_domain_client import RunnerDomainMcpClient
from CAD2BIM_backend.infrastructure.mcp_state_client import RunnerStateMcpClient
from CAD2BIM_backend.infrastructure.postgres.repository import PostgresControlPlaneRepository
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from CAD2BIM_mcp.runner.backend_client import RunnerStateBackendClient
from CAD2BIM_mcp.runner.server import create_runner_state_mcp


pytestmark = [pytest.mark.integration, pytest.mark.network]
_QUERY_SOURCE = (
    "def query(ctx):\n"
    "    project = ctx.model().by_type('IfcProject')[0]\n"
    "    return {'name': project.Name, 'description': project.Description}\n"
)


class _State(TypedDict, total=False):
    requirement_md: str
    pending_call: dict[str, Any] | None
    last_result: dict[str, Any]
    query_result: dict[str, Any]
    query_page: dict[str, Any]


def _protocol() -> ExecutionProtocolBinding:
    return ExecutionProtocolBinding(
        bundle_id="runner_domain_ifc",
        module="workflow",
        stage_key="cad2bim_workflow",
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _wait_tcp(port: int) -> None:
    for _ in range(100):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise RuntimeError(f"dedicated HTTP service on port {port} did not start")


@pytest.mark.asyncio
async def test_saved_resource_changes_exact_ifc_project_and_result_checkpoint(
    tmp_path: Path,
) -> None:
    database_url = os.environ["CAD2BIM_TEST_DATABASE_URL"]
    parsed = make_url(database_url)
    if parsed.host not in {"127.0.0.1", "localhost"} or not (
        parsed.database or ""
    ).startswith("cad2bim_runner_domain_b4_ifc_"):
        raise RuntimeError("Runner IFC test requires its own local B4 database")
    api_url = parsed.set(
        username="cad2bim_api_login", password=os.environ["CAD2BIM_API_POSTGRES_PASSWORD"]
    ).render_as_string(hide_password=False)
    runner_url = parsed.set(
        username="cad2bim_runner_login", password=os.environ["CAD2BIM_RUNNER_POSTGRES_PASSWORD"]
    ).render_as_string(hide_password=False)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    backend_port, mcp_port = _free_port(), _free_port()
    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
    holders: dict[str, Any] = {"query_interruptions": 2, "query_calls": [], "query_executions": 0}

    def graph_builder(saver, _feedback_nodes):
        def issue_call(state: _State) -> dict[str, Any]:
            if holders.pop("fault_before_call_save", False):
                raise RuntimeError("fault before domain call save")
            previous = state.get("last_result", {}).get("kind")
            if previous is None:
                kind, arguments = "state_resource", {}
            elif previous == "state_resource":
                kind = "ifc_project_update"
                arguments = {
                    "patch": {
                        "name": "Runner Project",
                        "description": state["last_result"]["description"],
                    },
                }
            elif previous == "ifc_project_update":
                kind = "ifc_query_python"
                arguments = {"mode": "fresh", "code": (
                    "def query(ctx):\n"
                    "    project = ctx.model().by_type('IfcProject')[0]\n"
                    "    return [{'name': project.Name, 'description': project.Description} for _ in range(2)]\n"
                ), "page_size": 1}
            elif previous == "ifc_query_python":
                kind = "ifc_query_python"
                arguments = {"mode": "cursor", "cursor": state["query_result"]["next_cursor"]}
            elif previous == "ifc_query_page":
                kind = "ifc_project_update"
                arguments = {
                    "patch": {
                        "name": "Runner Project",
                        "description": state["requirement_md"],
                    },
                }
            else:
                raise RuntimeError("the test fixture has no further domain call")
            return {"pending_call": {
                "id": f"call:{uuid4().hex}", "kind": kind, "arguments": arguments,
            }}

        async def execute_call(
            state: _State, runtime: Runtime[RunnerGraphContext], config: RunnableConfig
        ) -> dict[str, Any]:
            call = state["pending_call"]
            assert call is not None
            backend: BackendRuntime = holders["backend"]
            grant = backend.graph_runtime.require_current_execution_grant()
            if call["kind"] == "state_resource":
                scope = runtime.context.scope
                repository = PostgresControlPlaneRepository(backend.runner_database.session_factory)
                run = await repository.get_run(scope.owner_id, scope.run_id)
                assert run is not None
                binding = await resolve_node_read_binding(
                    run=run,
                    grant=grant,
                    node_config=config,
                    registered_graph_paths=("",),
                    field_owner_paths={
                        "requirements": "", "clarifications": "", "requirement_md": "",
                    },
                    graph_runtime=backend.graph_runtime,
                )
                async with holders["state_client"].open_read_batch(
                    grant=grant, binding=binding
                ) as batch:
                    description = await batch.read_text("state://requirement-md")
                result = {"kind": "state_resource", "description": description}
            else:
                response = await holders["domain_client"].call_tool(
                    grant=grant,
                    action_id=call["id"],
                    mcp_tool_name=call["kind"],
                    domain_tool_name=call["kind"],
                    arguments=call["arguments"],
                )
                if response.isError:
                    error_text = "\n".join(content.text for content in response.content if isinstance(content, TextContent))
                    if call["kind"] != "ifc_project_update" or not error_text.startswith(
                        "IFC_NO_CHANGES; phase=preflight; retryable=false; operation_alias=op_"
                    ):
                        raise RuntimeError("Runner domain tool returned an unexpected confirmed failure")
                    result = {"kind": "ifc_project_error", "error_text": error_text}
                else:
                    if response.structuredContent is None:
                        raise RuntimeError("Runner domain tool returned no successful structured result")
                    result = {"kind": call["kind"], "result": response.structuredContent}
                    if call["kind"] == "ifc_query_python":
                        if call["arguments"]["mode"] == "fresh":
                            holders["query_calls"].append(call["id"])
                            original = holders.setdefault("original_query", response.structuredContent)
                            # Cursor expiry is refreshed on re-read; immutable
                            # attempt identity and result content must stay exact.
                            assert {k: v for k, v in response.structuredContent.items() if k != "next_cursor"} == {
                                k: v for k, v in original.items() if k != "next_cursor"
                            }
                            if holders["query_interruptions"]:
                                holders["query_interruptions"] -= 1
                                raise RuntimeError("fault after query result before checkpoint")
                        else:
                            result["kind"] = "ifc_query_page"
            update = {"pending_call": None, "last_result": result}
            if result["kind"] == "ifc_query_python":
                update["query_result"] = result["result"]
            if result["kind"] == "ifc_query_page":
                update["query_page"] = result["result"]
            return update

        graph = StateGraph(_State, context_schema=RunnerGraphContext)
        graph.add_node("issue_call", issue_call)
        graph.add_node("execute_call", execute_call)
        graph.add_edge(START, "issue_call")
        graph.add_edge("issue_call", "execute_call")
        graph.add_conditional_edges(
            "execute_call",
            lambda state: END if state["last_result"]["kind"] == "ifc_project_error" else "issue_call",
        )
        return graph.compile(checkpointer=saver)

    backend = await open_backend_runtime(
        api_settings=Settings(database_url=api_url, environment="test"),
        runner_settings=Settings(database_url=runner_url, environment="test"),
        public_key_pem=public_pem,
        issuer="https://runner-domain-b1.example",
        audience="cad2bim-runner-domain-b1",
        execution_protocol=_protocol(),
        graph_builder=graph_builder,
        runner_id="runner-domain-b1",
        state_access_secret=b"runner-domain-b1-shared-internal-secret-32-bytes",
        ifc_storage_root=tmp_path / "ifc-storage",
    )
    holders["backend"] = backend
    holders["state_client"] = RunnerStateMcpClient(
        mcp_url=mcp_url, codec=backend.state_access_codec
    )
    holders["domain_client"] = RunnerDomainMcpClient(
        mcp_url=mcp_url, codec=backend.domain_access_codec
    )
    backend_client = RunnerStateBackendClient(f"http://127.0.0.1:{backend_port}")
    server = create_runner_state_mcp(
        codec=backend.state_access_codec,
        domain_codec=backend.domain_access_codec,
        backend=backend_client,
    )
    backend_server = uvicorn.Server(uvicorn.Config(
        backend.api, host="127.0.0.1", port=backend_port,
        log_level="warning", lifespan="off", access_log=False,
    ))
    mcp_server = uvicorn.Server(uvicorn.Config(
        server.http_app(stateless_http=True), host="127.0.0.1", port=mcp_port,
        log_level="warning", access_log=False,
    ))
    backend_task = asyncio.create_task(backend_server.serve())
    mcp_task = asyncio.create_task(mcp_server.serve())
    admin_engine = create_async_engine(database_url)
    assert backend.ifc_resources is not None
    query_worker = backend.ifc_resources.query._worker
    original_query = query_worker.execute_query

    async def count_query(*args, **kwargs):
        holders["query_executions"] += 1
        return await original_query(*args, **kwargs)

    query_worker.execute_query = count_query
    try:
        async with admin_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM runs")) == 0
        await _wait_tcp(backend_port)
        await _wait_tcp(mcp_port)
        token = jwt.encode({
            "iss": "https://runner-domain-b1.example",
            "aud": "cad2bim-runner-domain-b1",
            "sub": f"person-{uuid4().hex}",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        }, key, algorithm="RS256")
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}", timeout=30
        ) as public:
            task = await public.post("/tasks", headers=headers, json={"title": "B1 IFC Project"})
            assert task.status_code == 201, task.text
            task_id = task.json()["task_id"]
            started = await public.post(
                f"/tasks/{task_id}/runs", headers=headers,
                json={
                    "idempotency_key": f"start-{uuid4().hex}",
                    "input_payload": {
                        "requirement_md": "Resource value from checkpoint",
                    },
                },
            )
            assert started.status_code == 201, started.text
            run_id = started.json()["run_id"]
            async with admin_engine.connect() as connection:
                row = (await connection.execute(text(
                    "SELECT r.owner_id, r.tenant_id, r.thread_id, b.principal_id FROM runs AS r "
                    "JOIN task_tenant_bindings AS b "
                    "ON b.tenant_id = r.tenant_id AND b.task_id = r.task_id "
                    "WHERE r.run_id = :run_id"
                ), {"run_id": run_id})).one()
            identity = TenantExecutionContext(
                tenant_id=row.tenant_id,
                principal_id=row.principal_id,
                task_id=task_id,
                actor_kind=TenantActorKind.USER,
                command_id=f"fixture-init:{uuid4().hex}",
            )
            scope = IfcRunIdentity(
                owner_id=row.owner_id, tenant_id=row.tenant_id,
                task_id=task_id, run_id=run_id,
            )
            assert backend.ifc_resources is not None
            with bind_tenant_execution_context(identity):
                await backend.ifc_resources.model.initialize(
                    scope, InitializeIfcModelCommand(project_name="Initial Project")
                )
            holders["fault_before_call_save"] = True
            with pytest.raises(RuntimeError, match="fault before domain call save"):
                await backend.runner.execute_next()
            async with admin_engine.connect() as connection:
                assert await connection.scalar(text(
                    "SELECT count(*) FROM critical_operations "
                    "WHERE run_id=:run_id AND tool_name='ifc_project_update'"
                ), {"run_id": run_id}) == 0
                pre_call_checkpoint_id = await connection.scalar(text(
                    "SELECT checkpoint_id FROM checkpoints "
                    "WHERE thread_id=:thread_id AND checkpoint_ns='' "
                    "ORDER BY checkpoint_id DESC LIMIT 1"
                ), {"thread_id": row.thread_id})
            assert pre_call_checkpoint_id is not None
            pre_call = await backend.graph_runtime.get_checkpoint_state(
                thread_id=row.thread_id, checkpoint_id=pre_call_checkpoint_id,
            )
            assert pre_call is not None
            assert pre_call.values.get("pending_call") is None
            await asyncio.sleep(31)
            query_attempt = None
            for _ in range(2):
                with pytest.raises(RuntimeError, match="fault after query result before checkpoint"):
                    await backend.runner.execute_next()
                async with admin_engine.connect() as connection:
                    cursor = await connection.scalar(text(
                        "SELECT checkpoint_id FROM checkpoints WHERE thread_id=:thread_id "
                        "AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1"
                    ), {"thread_id": row.thread_id})
                    current_attempt = (await connection.execute(text(
                        "SELECT attempt_id, ifc_revision_id, status FROM ifc_query_attempts "
                        "WHERE run_id=:run_id"
                    ), {"run_id": run_id})).one()
                if query_attempt is None:
                    query_attempt = current_attempt
                assert current_attempt == query_attempt
                pending = await backend.graph_runtime.get_checkpoint_state(
                    thread_id=row.thread_id, checkpoint_id=cursor,
                )
                assert pending is not None
                assert pending.checkpoint.next_nodes == ("execute_call",)
                assert pending.values["pending_call"]["id"] == holders["query_calls"][0]
                assert "query_result" not in pending.values
                await asyncio.sleep(31)
            finished = await backend.runner.execute_next()
            assert finished is not None and finished.run_id == run_id
            assert finished.status.value == "completed"
            issued = await public.post(
                f"/tasks/{task_id}/runs/{run_id}/state-read-access", headers=headers
            )
            assert issued.status_code == 200, issued.text
            pair = issued.json()
            binding = backend.state_access_codec.verify_pair(
                pair["access_token"], pair["read_binding"]
            ).binding
            saved = await backend.graph_runtime.get_checkpoint_state(
                thread_id=binding.thread_id,
                checkpoint_id=binding.checkpoint_cursor.checkpoint_id,
                checkpoint_ns=binding.checkpoint_cursor.checkpoint_ns,
            )
            assert saved is not None
            result = saved.values["last_result"]
            assert json.loads(saved.values["query_result"]["result_json"]) == [{
                "name": "Runner Project", "description": "Resource value from checkpoint",
            }]
            assert saved.values["query_page"]["result_json"] == saved.values["query_result"]["result_json"]
            assert saved.values["query_page"]["next_cursor"] is None
            assert saved.values["query_page"]["attempt_alias"] == saved.values["query_result"]["attempt_alias"]
            assert holders["query_calls"] == [holders["query_calls"][0]] * 3
            assert holders["query_executions"] == 1
            print({"run_id": run_id, "query_attempt": tuple(query_attempt),
                   "query_calls": holders["query_calls"], "query_executions": holders["query_executions"],
                   "recovered_result": saved.values["query_result"], "page": saved.values["query_page"]})
            assert result["kind"] == "ifc_project_error"
            assert result["error_text"].startswith(
                "IFC_NO_CHANGES; phase=preflight; retryable=false; operation_alias=op_"
            )
            assert saved.values["pending_call"] is None
    finally:
        backend_server.should_exit = True
        mcp_server.should_exit = True
        try:
            await asyncio.gather(backend_task, mcp_task)
        finally:
            try:
                await backend_client.aclose()
            finally:
                try:
                    await admin_engine.dispose()
                finally:
                    await backend.close()
