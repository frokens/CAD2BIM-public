"""A saved Runner action publishes a Data Package and reads its exact output."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
import signal
import socket
from typing import Any, TypedDict
from uuid import uuid4

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from mcp import McpError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from CAD2BIM_backend.bootstrap import BackendRuntime, open_backend_runtime
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.data_runtime import ValidatedSource
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.runner_context import RunnerGraphContext
from CAD2BIM_backend.infrastructure.mcp_domain_client import RunnerDomainMcpClient, RunnerDomainOutcomeUnknown
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from CAD2BIM_backend.infrastructure.worker_runtime.supervisor import WorkerProcessSupervisor
from tests.integration.backend.test_runner_domain_mcp_disconnect_postgres import _serve_mcp, _stop_mcp


pytestmark = [pytest.mark.integration, pytest.mark.network]
_SOURCE = Path(__file__).resolve().parents[2] / "fixtures" / "cad" / "minimal_line.dxf"
_SECRET = b"runner-domain-data-shared-internal-secret-32-bytes"


class _State(TypedDict, total=False):
    pending_call: dict[str, Any] | None
    transform_result: dict[str, Any]
    read_result: dict[str, Any]


def _protocol() -> ExecutionProtocolBinding:
    return ExecutionProtocolBinding(
        bundle_id="runner_domain_data", module="workflow",
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


def _build_data_graph(holders, saver, _feedback_nodes):
    def issue_call(state: _State) -> dict[str, Any]:
        if "transform_result" not in state:
            name = "data_derive"
            arguments = {
                "request": {
                    "mode": "create",
                    "inputs": [{
                        "input_name": "entities",
                        "package_alias": holders["source_alias"],
                        "payload_name": "entities",
                    }],
                    "execution": {
                        "engine": "duckdb_sql",
                        "code": "CREATE TEMP TABLE selected_entities AS SELECT entity_key, layer FROM entities WHERE layer = $layer",
                    },
                    "parameters": [{"name": "layer", "kind": "string", "value": "0"}],
                    "outputs": {"action": "set", "value": [{
                        "payload_name": "selected_entities", "format": "parquet",
                        "description": "Selected exact CAD entities.",
                        "logical_key_fields": ["entity_key"],
                    }]},
                    "output_package": {
                        "name": "runner_selected_entities",
                        "description_markdown": {"action": "set", "value": (
                            "# Selected CAD entities\n## Summary\nExact entity rows.\n"
                            "## Intended use\nVerify Runner Data publication.\n"
                            "## Contents\nEntity keys.\n## Method\nParameterized SQL.\n"
                            "## Scope and completeness\nSmall test source.\n"
                            "## Known limitations\nNo IFC decisions.\n"
                        )},
                    },
                },
            }
        else:
            name = "data_query_rows"
            arguments = {
                "request": {
                    "query": {
                        "package_alias": state["transform_result"]["package_alias"],
                        "payload_name": "selected_entities",
                        "select": ["entity_key", "layer"],
                        "page_size": 10,
                    },
                },
            }
        return {"pending_call": {
            "id": f"call:{uuid4().hex}", "name": name, "arguments": arguments,
        }}

    async def execute_call(
        state: _State, runtime: Runtime[RunnerGraphContext],
    ) -> dict[str, Any]:
        del runtime
        call = state["pending_call"]
        assert call is not None
        backend: BackendRuntime = holders["backend"]
        grant = backend.graph_runtime.require_current_execution_grant()
        with (holders["root"] / "data-calls.jsonl").open("a") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "runner": grant.runner_id,
                                     "key": call["id"], "name": call["name"]}) + "\n")
        response = await holders["domain_client"].call_tool(
            grant=grant,
            action_id=call["id"],
            mcp_tool_name=call["name"],
            domain_tool_name=call["name"],
            arguments=call["arguments"],
            timeout_seconds=45,
        )
        if response.isError or response.structuredContent is None:
            raise RuntimeError(f"Runner {call['name']} returned no structured result: {response}")
        field = "transform_result" if call["name"] == "data_derive" else "read_result"
        if field == "transform_result":
            if holders.get("crash"):
                os._exit(73)
            if holders.pop("interrupt_result", False):
                raise RuntimeError("Data committed before result checkpoint")
        return {"pending_call": None, field: response.structuredContent}

    graph = StateGraph(_State, context_schema=RunnerGraphContext)
    graph.add_node("issue_call", issue_call)
    graph.add_node("execute_call", execute_call)
    graph.add_edge(START, "issue_call")
    graph.add_edge("issue_call", "execute_call")
    graph.add_conditional_edges(
        "execute_call", lambda state: END if "read_result" in state else "issue_call",
    )
    return graph.compile(checkpointer=saver)


def _resume_data_runner(api_url, runner_url, public_pem, mcp_url, root, runner_id, crash):
    async def run():
        holders = {"root": root, "crash": crash}
        backend = await open_backend_runtime(
            api_settings=Settings(database_url=api_url, environment="test"),
            runner_settings=Settings(database_url=runner_url, environment="test"),
            public_key_pem=public_pem, issuer="https://runner-domain-data.example",
            audience="cad2bim-runner-domain-data", execution_protocol=_protocol(),
            graph_builder=lambda saver, nodes: _build_data_graph(holders, saver, nodes),
            runner_id=runner_id, state_access_secret=_SECRET,
        )
        holders["backend"] = backend
        assert backend.data_resources is None and backend.authoring_resources is None and backend.ifc_resources is None
        with (root / "data-runner-roles.jsonl").open("a") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "runner": runner_id, "domain_workers": False}) + "\n")
        holders["domain_client"] = RunnerDomainMcpClient(mcp_url=mcp_url, codec=backend.domain_access_codec)
        try:
            for _ in range(90):
                result = await backend.runner.execute_next()
                if result is not None:
                    assert result.status.value == "completed"
                    return
                await asyncio.sleep(0.5)
            raise AssertionError("Data Runner claim did not expire")
        finally:
            await backend.close()
    asyncio.run(run())


@pytest.mark.asyncio
async def test_saved_data_transform_publishes_package_and_next_call_reads_exact_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ["CAD2BIM_TEST_DATABASE_URL"]
    parsed = make_url(database_url)
    if parsed.host not in {"127.0.0.1", "localhost"} or not (
        parsed.database or ""
    ).startswith("cad2bim_runner_domain_b4_data_"):
        raise RuntimeError("Runner Data test requires its isolated local database")
    api_url = parsed.set(
        username="cad2bim_api_login", password=os.environ["CAD2BIM_API_POSTGRES_PASSWORD"]
    ).render_as_string(hide_password=False)
    runner_url = parsed.set(
        username="cad2bim_runner_login", password=os.environ["CAD2BIM_RUNNER_POSTGRES_PASSWORD"]
    ).render_as_string(hide_password=False)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    backend_port, mcp_port = _free_port(), _free_port()
    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
    holders: dict[str, Any] = {"root": tmp_path, "worker_active": asyncio.Event(),
                               "release_worker": asyncio.Event(), "committed": asyncio.Event(),
                               "builds": 0, "interrupt_result": True}

    def graph_builder(saver, feedback_nodes):
        return _build_data_graph(holders, saver, feedback_nodes)
    backend = await open_backend_runtime(
        api_settings=Settings(database_url=api_url, environment="test"),
        runner_settings=Settings(database_url=runner_url, environment="test"),
        public_key_pem=public_pem,
        issuer="https://runner-domain-data.example",
        audience="cad2bim-runner-domain-data",
        execution_protocol=_protocol(), graph_builder=graph_builder,
        runner_id="runner-domain-data",
        state_access_secret=_SECRET,
        ifc_storage_root=tmp_path / "ifc-storage",
    )
    holders["backend"] = backend
    holders["domain_client"] = RunnerDomainMcpClient(
        mcp_url=mcp_url, codec=backend.domain_access_codec,
    )
    backend_server = uvicorn.Server(uvicorn.Config(
        backend.api, host="127.0.0.1", port=backend_port,
        log_level="warning", lifespan="off", access_log=False,
    ))
    backend_task = asyncio.create_task(backend_server.serve())
    context = multiprocessing.get_context("spawn")
    mcp_process = context.Process(target=_serve_mcp, args=(backend_port, mcp_port, tmp_path, _SECRET))
    mcp_process.start()
    children = []
    running = None
    assert backend.data_resources is not None
    original_execute = backend.data_resources.transform_worker.execute
    original_transform = backend.data_resources.transform.transform
    original_launch = WorkerProcessSupervisor.launch

    async def observe_launch(*args, **kwargs):
        process = await original_launch(*args, **kwargs)
        if holders.get("transforming") and not holders["worker_active"].is_set():
            pid = process._cad2bim_worker_pid
            os.kill(pid, signal.SIGSTOP)
            holders["worker_pid"] = pid
            holders["worker_active"].set()
            try:
                await holders["release_worker"].wait()
            finally:
                os.kill(pid, signal.SIGCONT)
        return process

    async def count_transform(*args, **kwargs):
        holders["builds"] += 1
        with (tmp_path / "data-worker-entry.jsonl").open("a") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "entry": "transform_worker.execute"}) + "\n")
        holders["transforming"] = True
        try:
            return await original_execute(*args, **kwargs)
        finally:
            holders["transforming"] = False

    async def observe_commit(*args, **kwargs):
        result = await original_transform(*args, **kwargs)
        holders["committed"].set()
        return result

    monkeypatch.setattr(WorkerProcessSupervisor, "launch", staticmethod(observe_launch))
    monkeypatch.setattr(backend.data_resources.transform_worker, "execute", count_transform)
    monkeypatch.setattr(backend.data_resources.transform, "transform", observe_commit)
    admin_engine = create_async_engine(database_url)
    try:
        async with admin_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM runs")) == 0
        await _wait_tcp(backend_port)
        await _wait_tcp(mcp_port)
        token = jwt.encode({
            "iss": "https://runner-domain-data.example",
            "aud": "cad2bim-runner-domain-data",
            "sub": f"person-{uuid4().hex}",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        }, key, algorithm="RS256")
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}", timeout=30,
        ) as public:
            task = await public.post("/tasks", headers=headers, json={"title": "B4 Data"})
            assert task.status_code == 201, task.text
            task_id = task.json()["task_id"]
            started = await public.post(
                f"/tasks/{task_id}/runs", headers=headers,
                json={"idempotency_key": f"start-{uuid4().hex}", "input_payload": {}},
            )
            assert started.status_code == 201, started.text
            run_id = started.json()["run_id"]
            async with admin_engine.connect() as connection:
                row = (await connection.execute(text(
                    "SELECT r.owner_id, r.tenant_id, b.principal_id FROM runs AS r "
                    "JOIN task_tenant_bindings AS b "
                    "ON b.tenant_id = r.tenant_id AND b.task_id = r.task_id "
                    "WHERE r.run_id = :run_id"
                ), {"run_id": run_id})).one()
            identity = TenantExecutionContext(
                tenant_id=row.tenant_id, principal_id=row.principal_id,
                task_id=task_id, actor_kind=TenantActorKind.USER,
                command_id=f"fixture-import:{uuid4().hex}",
            )
            scope = InvocationScope(
                identity=identity, owner_id=row.owner_id, run_id=run_id,
                execution_mode=ExecutionMode.GRAPH, catalog_frontier=None,
            )
            assert backend.data_resources is not None
            with bind_tenant_execution_context(identity):
                imported = await backend.data_resources.runtime.import_cad_source(
                    scope, ValidatedSource(
                        filename=_SOURCE.name, media_type="application/dxf",
                        content=_SOURCE.read_bytes(),
                        source_identity=f"runner-data:{run_id}",
                    ),
                )
            holders["source_alias"] = imported.published.package.catalog_entry.alias
            running = asyncio.create_task(backend.runner.execute_next())
            active = asyncio.create_task(holders["worker_active"].wait())
            done, _ = await asyncio.wait((active, running), timeout=40, return_when=asyncio.FIRST_COMPLETED)
            if running in done:
                active.cancel()
                await running
                raise AssertionError("Runner finished before the transform Worker became active")
            assert active in done, "Transform Worker did not become active within the existing test budget"
            mcp_process.kill()
            await _stop_mcp(mcp_process)
            assert mcp_process.exitcode == -signal.SIGKILL and not backend_task.done()
            holders["release_worker"].set()
            await asyncio.wait_for(holders["committed"].wait(), 30)
            with pytest.raises(RunnerDomainOutcomeUnknown) as failure:
                await asyncio.wait_for(asyncio.shield(running), 50)
            assert isinstance(failure.value.__cause__, McpError)
            assert failure.value.__cause__.error.code == 408

            async def snapshot():
                async with admin_engine.connect() as connection:
                    claim = (await connection.execute(text(
                        "SELECT thread_id,writer_generation,claimed_by FROM runs WHERE run_id=:id"
                    ), {"id": run_id})).one()
                    cursor = await connection.scalar(text(
                        "SELECT checkpoint_id FROM checkpoints WHERE thread_id=:id AND checkpoint_ns='' "
                        "ORDER BY checkpoint_id DESC LIMIT 1"
                    ), {"id": claim.thread_id})
                    operation = (await connection.execute(text(
                        "SELECT operation_id,result_package_id,transform_resolved_json,transform_deadline_at "
                        "FROM critical_operations WHERE run_id=:id AND operation_kind='data_transform'"
                    ), {"id": run_id})).one()
                state = await backend.graph_runtime.get_checkpoint_state(thread_id=claim.thread_id, checkpoint_id=cursor)
                return claim, state, operation

            first_claim, pending, operation = await snapshot()
            assert pending.checkpoint.next_nodes == ("execute_call",)
            key_before = pending.values["pending_call"]["id"]
            mcp_process = context.Process(target=_serve_mcp, args=(backend_port, mcp_port, tmp_path, _SECRET))
            mcp_process.start()
            await _wait_tcp(mcp_port)
            with pytest.raises(RuntimeError, match="Data committed before result checkpoint"):
                for _ in range(90):
                    await backend.runner.execute_next()
                    await asyncio.sleep(0.5)
            claim, pending, current = await snapshot()
            assert claim.claimed_by == first_claim.claimed_by
            assert claim.writer_generation == first_claim.writer_generation + 1
            assert pending.values["pending_call"]["id"] == key_before and current == operation
            for runner_id, crash in (("runner-data-recovery", True), ("runner-data-final", False)):
                process = context.Process(target=_resume_data_runner,
                    args=(api_url, runner_url, public_pem, mcp_url, tmp_path, runner_id, crash))
                children.append(process)
                process.start()
                await asyncio.to_thread(process.join, 70)
                assert process.exitcode == (73 if crash else 0)
                next_claim, pending, current = await snapshot()
                assert next_claim.claimed_by == (runner_id if crash else None)
                assert next_claim.writer_generation == claim.writer_generation + 1
                assert current == operation
                claim = next_claim
                if crash:
                    assert pending.checkpoint.next_nodes == ("execute_call",)
                    assert pending.values["pending_call"]["id"] == key_before
            assert len({os.getpid(), *(child.pid for child in children)}) == 3
            assert holders["builds"] == 1
            executions = [json.loads(line) for line in (tmp_path / "data-worker-entry.jsonl").read_text().splitlines()]
            roles = [json.loads(line) for line in (tmp_path / "data-runner-roles.jsonl").read_text().splitlines()]
            assert executions == [{"pid": os.getpid(), "entry": "transform_worker.execute"}]
            assert {role["pid"] for role in roles} == {child.pid for child in children}
            assert all(role["domain_workers"] is False for role in roles)
            issued = await public.post(
                f"/tasks/{task_id}/runs/{run_id}/state-read-access", headers=headers,
            )
            assert issued.status_code == 200, issued.text
            pair = issued.json()
            binding = backend.state_access_codec.verify_pair(
                pair["access_token"], pair["read_binding"],
            ).binding
            saved = await backend.graph_runtime.get_checkpoint_state(
                thread_id=binding.thread_id,
                checkpoint_id=binding.checkpoint_cursor.checkpoint_id,
                checkpoint_ns=binding.checkpoint_cursor.checkpoint_ns,
            )
            assert saved is not None
            transform = saved.values["transform_result"]
            read = saved.values["read_result"]
            assert transform["operation_alias"].startswith("op_")
            assert transform["package_alias"].startswith("data_")
            assert transform["package_alias"] != holders["source_alias"]
            assert read["package_alias"] == transform["package_alias"]
            assert read["payload_name"] == "selected_entities"
            assert read["rows"] and all(row["values"]["layer"] == "0" for row in read["rows"])
            assert saved.values["pending_call"] is None
            calls = [json.loads(line) for line in (tmp_path / "data-calls.jsonl").read_text().splitlines()]
            assert [call["name"] for call in calls] == ["data_derive"] * 4 + ["data_query_rows"]
            assert {call["key"] for call in calls[:4]} == {key_before}
            print({"run_id": run_id, "operation": tuple(operation), "calls": calls,
                   "worker_pid": holders["worker_pid"], "builds": holders["builds"],
                   "worker_entries": executions, "runner_roles": roles,
                   "result": transform, "next_read": read})
    finally:
        holders["release_worker"].set()
        if running is not None and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await _stop_mcp(mcp_process)
        for child in children:
            await _stop_mcp(child)
        backend_server.should_exit = True
        try:
            await backend_task
        finally:
            try:
                await admin_engine.dispose()
            finally:
                await backend.close()
