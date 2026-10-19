"""A saved Runner domain action survives a process exit and both HTTP restarts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from langgraph.graph import END, START, StateGraph
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from CAD2BIM_backend.bootstrap import BackendRuntime, open_backend_runtime
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity, InitializeIfcModelCommand
from CAD2BIM_backend.domain.runner_context import RunnerGraphContext
from CAD2BIM_backend.infrastructure.mcp_domain_client import RunnerDomainMcpClient
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from CAD2BIM_backend.ports.ifc_revision_runtime import UpdateIfcProjectRevisionRequest
from CAD2BIM_mcp.runner.backend_client import RunnerStateBackendClient
from CAD2BIM_mcp.runner.server import create_runner_state_mcp
from tests.integration.backend.test_runner_domain_ifc_postgres import (
    _QUERY_SOURCE,
    _free_port,
    _protocol,
    _wait_tcp,
)


pytestmark = [pytest.mark.integration, pytest.mark.network]
_ISSUER = "https://runner-domain-b2.example"
_AUDIENCE = "cad2bim-runner-domain-b2"
_SECRET = b"runner-domain-b2-shared-internal-secret-32-bytes"
_RUNNER_ID = "runner-domain-b2"
_RECOVERY_RUNNER_ID = "runner-domain-b2-recovery"
_FINAL_RUNNER_ID = "runner-domain-b2-final"


class _State(TypedDict, total=False):
    pending_call: dict[str, Any] | None
    project_result: dict[str, Any]
    read_result: dict[str, Any]


def _graph_builder(
    holder: dict[str, BackendRuntime], mcp_url: str, *, crash: bool,
    audit_path: Path | None = None,
):
    def build(saver, _feedback_nodes):
        def issue_call(state: _State) -> dict[str, Any]:
            if "project_result" in state:
                name = "ifc_query_python"
                arguments = {"mode": "fresh", "code": _QUERY_SOURCE, "page_size": 10}
            else:
                name = "ifc_project_update"
                arguments = {"patch": {
                    "name": "Recovered Project", "description": "one external commit",
                }}
            return {"pending_call": {
                "id": f"call:{uuid4().hex}", "name": name,
                "arguments": arguments,
            }}

        async def execute_call(state: _State) -> dict[str, Any]:
            call = state["pending_call"]
            assert call is not None
            backend = holder["backend"]
            grant = backend.graph_runtime.require_current_execution_grant()
            if audit_path is not None:
                with audit_path.open("a") as stream:
                    stream.write(f"{os.getpid()} {call['name']} {call['id']}\n")
            result = await RunnerDomainMcpClient(
                mcp_url=mcp_url, codec=backend.domain_access_codec
            ).call_tool(
                grant=grant,
                action_id=call["id"],
                mcp_tool_name=call["name"],
                domain_tool_name=call["name"],
                arguments=call["arguments"],
            )
            if result.isError or result.structuredContent is None:
                raise RuntimeError(f"external {call['name']} did not succeed")
            if crash and call["name"] == "ifc_project_update":
                # Backend committed and MCP answered; execute_call's resulting
                # checkpoint has not yet been written by LangGraph.
                os._exit(73)
            field = "project_result" if call["name"] == "ifc_project_update" else "read_result"
            return {"pending_call": None, field: result.structuredContent}

        graph = StateGraph(_State, context_schema=RunnerGraphContext)
        graph.add_node("issue_call", issue_call)
        graph.add_node("execute_call", execute_call)
        graph.add_edge(START, "issue_call")
        graph.add_edge("issue_call", "execute_call")
        graph.add_conditional_edges(
            "execute_call", lambda state: END if "read_result" in state else "issue_call",
        )
        return graph.compile(checkpointer=saver)

    return build


def _runner_process(
    api_url: str,
    runner_url: str,
    public_pem: bytes,
    mcp_url: str,
    crash: bool,
    runner_id: str,
    result_path: Path,
) -> None:
    async def run() -> None:
        holder: dict[str, BackendRuntime] = {}
        backend = await open_backend_runtime(
            api_settings=Settings(database_url=api_url, environment="test"),
            runner_settings=Settings(database_url=runner_url, environment="test"),
            public_key_pem=public_pem,
            issuer=_ISSUER,
            audience=_AUDIENCE,
            execution_protocol=_protocol(),
            graph_builder=_graph_builder(
                holder, mcp_url, crash=crash,
                audit_path=result_path.parent / "tool-requests.log",
            ),
            runner_id=runner_id,
            state_access_secret=_SECRET,
        )
        holder["backend"] = backend
        try:
            for _ in range(85):
                completed = await backend.runner.execute_next()
                if completed is not None:
                    result_path.write_text(
                        json.dumps({"run_id": completed.run_id, "status": completed.status.value})
                    )
                    return
                await asyncio.sleep(0.5)
            raise AssertionError("original Runner claim did not expire for recovery")
        finally:
            await backend.close()

    asyncio.run(run())


@dataclass
class _Host:
    backend: BackendRuntime
    backend_client: RunnerStateBackendClient
    backend_server: uvicorn.Server
    mcp_server: uvicorn.Server
    backend_task: asyncio.Task[Any]
    mcp_task: asyncio.Task[Any]

    async def close(self) -> None:
        self.backend_server.should_exit = True
        self.mcp_server.should_exit = True
        try:
            await asyncio.wait_for(
                asyncio.gather(self.backend_task, self.mcp_task), timeout=15
            )
        finally:
            try:
                await self.backend_client.aclose()
            finally:
                await self.backend.close()


async def _open_host(
    *,
    api_url: str,
    runner_url: str,
    public_pem: bytes,
    backend_port: int,
    mcp_port: int,
    root: Path,
    phase_path: Path | None = None,
) -> _Host:
    holder: dict[str, BackendRuntime] = {}
    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
    backend = await open_backend_runtime(
        api_settings=Settings(database_url=api_url, environment="test"),
        runner_settings=Settings(database_url=runner_url, environment="test"),
        public_key_pem=public_pem,
        issuer=_ISSUER,
        audience=_AUDIENCE,
        execution_protocol=_protocol(),
        graph_builder=_graph_builder(holder, mcp_url, crash=False),
        runner_id=_RUNNER_ID,
        state_access_secret=_SECRET,
        ifc_storage_root=root / "ifc-storage",
    )
    if phase_path is not None:
        phase_path.write_text("backend-ready")
    assert backend.ifc_resources is not None
    revision_runtime = backend.ifc_resources.revision_runtime
    original_execute = revision_runtime.execute

    async def count_mutations(request, *, source=None):
        if isinstance(request, UpdateIfcProjectRevisionRequest):
            with (root / "project-mutation-requests.log").open("a") as stream:
                stream.write(f"{os.getpid()}\n")
        return await original_execute(request, source=source)

    revision_runtime.execute = count_mutations
    holder["backend"] = backend
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
    host = _Host(
        backend, backend_client, backend_server, mcp_server,
        asyncio.create_task(backend_server.serve()),
        asyncio.create_task(mcp_server.serve()),
    )
    if phase_path is not None:
        phase_path.write_text("http-started")
    try:
        await _wait_tcp(backend_port)
        await _wait_tcp(mcp_port)
        if phase_path is not None:
            phase_path.write_text("http-ready")
        return host
    except BaseException:
        await host.close()
        raise


def _host_process(
    api_url: str,
    runner_url: str,
    public_pem: bytes,
    backend_port: int,
    mcp_port: int,
    root: Path,
    ready_path: Path,
) -> None:
    async def serve() -> None:
        phase_path = ready_path.with_suffix(".phase")
        phase_path.write_text("opening-backend")
        host = await _open_host(
            api_url=api_url,
            runner_url=runner_url,
            public_pem=public_pem,
            backend_port=backend_port,
            mcp_port=mcp_port,
            root=root,
            phase_path=phase_path,
        )
        ready_path.write_text(json.dumps({"pid": os.getpid()}))
        try:
            await asyncio.Event().wait()
        finally:
            await host.close()

    asyncio.run(serve())


async def _wait_host_process(process: multiprocessing.Process, ready_path: Path) -> int:
    for _ in range(300):
        if ready_path.exists() and process.is_alive():
            pid = json.loads(ready_path.read_text())["pid"]
            assert pid == process.pid
            return pid
        if process.exitcode is not None:
            raise AssertionError(f"Backend/MCP host exited before ready: {process.exitcode}")
        await asyncio.sleep(0.1)
    phase_path = ready_path.with_suffix(".phase")
    phase = phase_path.read_text() if phase_path.exists() else "no phase marker"
    raise AssertionError(f"Backend/MCP host did not become ready: {phase}")


async def _stop_host_process(process: multiprocessing.Process) -> None:
    if process.is_alive():
        process.kill()
    await asyncio.to_thread(process.join, 10)
    assert not process.is_alive()


async def _root_checkpoint(backend: BackendRuntime, thread_id: str):
    async with backend.runner_database.engine.connect() as connection:
        checkpoint_id = (await connection.execute(text(
            "SELECT checkpoint_id FROM checkpoints "
            "WHERE thread_id=:thread_id AND checkpoint_ns='' "
            "ORDER BY checkpoint_id DESC LIMIT 1"
        ), {"thread_id": thread_id})).scalar_one()
    saved = await backend.graph_runtime.get_checkpoint_state(
        thread_id=thread_id, checkpoint_id=checkpoint_id
    )
    assert saved is not None
    return saved


async def _project_writes(admin_engine: Any, run_id: str):
    async with admin_engine.connect() as connection:
        return (await connection.execute(text(
            "SELECT o.operation_id, o.idempotency_key, r.revision_id "
            "FROM critical_operations AS o JOIN ifc_model_revisions AS r "
            "ON r.operation_id=o.operation_id "
            "WHERE o.run_id=:run_id AND o.tool_name='ifc_project_update'"
        ), {"run_id": run_id})).all()


async def _run_claim(admin_engine: Any, run_id: str) -> tuple[int, str | None]:
    async with admin_engine.connect() as connection:
        row = (await connection.execute(text(
            "SELECT writer_generation, claimed_by FROM runs WHERE run_id=:run_id"
        ), {"run_id": run_id})).one()
    return row.writer_generation, row.claimed_by


@pytest.mark.asyncio
async def test_runner_restarts_after_external_commit_before_checkpoint(tmp_path: Path) -> None:
    database_url = os.environ["CAD2BIM_TEST_DATABASE_URL"]
    parsed = make_url(database_url)
    if parsed.host not in {"127.0.0.1", "localhost"} or not (
        parsed.database or ""
    ).startswith("cad2bim_runner_domain_b1_"):
        raise RuntimeError("cross-PID Runner test requires its dedicated local database")
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
    assert backend_port != mcp_port
    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
    admin_engine = create_async_engine(database_url)
    holder: dict[str, BackendRuntime] = {}
    reader_backend: BackendRuntime | None = None
    host_process: multiprocessing.Process | None = None
    runner_processes: list[multiprocessing.Process] = []
    ctx = multiprocessing.get_context("spawn")
    try:
        reader_backend = await open_backend_runtime(
            api_settings=Settings(database_url=api_url, environment="test"),
            runner_settings=Settings(database_url=runner_url, environment="test"),
            public_key_pem=public_pem,
            issuer=_ISSUER,
            audience=_AUDIENCE,
            execution_protocol=_protocol(),
            graph_builder=_graph_builder(holder, mcp_url, crash=False),
            runner_id=_RUNNER_ID,
            state_access_secret=_SECRET,
            ifc_storage_root=tmp_path / "ifc-storage",
        )
        holder["backend"] = reader_backend
        host_process = ctx.Process(
            target=_host_process,
            args=(
                api_url, runner_url, public_pem, backend_port, mcp_port,
                tmp_path, tmp_path / "host-first.json",
            ),
        )
        host_process.start()
        first_host_pid = await _wait_host_process(host_process, tmp_path / "host-first.json")
        token = jwt.encode({
            "iss": _ISSUER, "aud": _AUDIENCE,
            "sub": f"person-{uuid4().hex}",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        }, key, algorithm="RS256")
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}", timeout=30
        ) as public:
            task = await public.post("/tasks", headers=headers, json={"title": "B2 crash recovery"})
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
                "SELECT r.owner_id, r.tenant_id, r.thread_id, b.principal_id "
                "FROM runs AS r JOIN task_tenant_bindings AS b "
                "ON b.tenant_id=r.tenant_id AND b.task_id=r.task_id "
                "WHERE r.run_id=:run_id"
            ), {"run_id": run_id})).one()
        identity = TenantExecutionContext(
            tenant_id=row.tenant_id, principal_id=row.principal_id,
            task_id=task_id, actor_kind=TenantActorKind.USER,
            command_id=f"fixture-init:{uuid4().hex}",
        )
        scope = IfcRunIdentity(
            owner_id=row.owner_id, tenant_id=row.tenant_id,
            task_id=task_id, run_id=run_id,
        )
        assert reader_backend.ifc_resources is not None
        with bind_tenant_execution_context(identity):
            await reader_backend.ifc_resources.model.initialize(
                scope, InitializeIfcModelCommand(project_name="Before external write")
            )

        crashed = ctx.Process(
            target=_runner_process,
            args=(
                api_url, runner_url, public_pem, mcp_url, True,
                _RUNNER_ID, tmp_path / "crashed.json",
            ),
        )
        runner_processes.append(crashed)
        crashed.start()
        await asyncio.to_thread(crashed.join, 45)
        if crashed.is_alive():
            crashed.terminate()
            await asyncio.to_thread(crashed.join, 5)
        assert crashed.exitcode == 73
        first_generation, first_runner_id = await _run_claim(admin_engine, run_id)
        assert first_runner_id == _RUNNER_ID
        writes_before = await _project_writes(admin_engine, run_id)
        assert len(writes_before) == 1
        pending = await _root_checkpoint(reader_backend, row.thread_id)
        assert pending.values["pending_call"] is not None
        assert pending.checkpoint.next_nodes == ("execute_call",)
        assert pending.values["pending_call"]["id"] == writes_before[0].idempotency_key
        assert pending.values["pending_call"]["name"] == "ifc_project_update"

        await _stop_host_process(host_process)
        host_process = None
        for port in (backend_port, mcp_port):
            with pytest.raises(OSError):
                _reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
        host_process = ctx.Process(
            target=_host_process,
            args=(
                api_url, runner_url, public_pem, backend_port, mcp_port,
                tmp_path, tmp_path / "host-second.json",
            ),
        )
        host_process.start()
        second_host_pid = await _wait_host_process(host_process, tmp_path / "host-second.json")
        assert second_host_pid != first_host_pid
        interrupted = ctx.Process(
            target=_runner_process,
            args=(
                api_url, runner_url, public_pem, mcp_url, True,
                _RECOVERY_RUNNER_ID, tmp_path / "interrupted.json",
            ),
        )
        runner_processes.append(interrupted)
        interrupted.start()
        await asyncio.to_thread(interrupted.join, 65)
        if interrupted.is_alive():
            interrupted.terminate()
            await asyncio.to_thread(interrupted.join, 5)
        assert interrupted.exitcode == 73
        assert interrupted.pid != crashed.pid
        second_generation, second_runner_id = await _run_claim(admin_engine, run_id)
        assert (second_generation, second_runner_id) == (
            first_generation + 1, _RECOVERY_RUNNER_ID,
        )
        assert await _project_writes(admin_engine, run_id) == writes_before
        interrupted_pending = await _root_checkpoint(reader_backend, row.thread_id)
        assert interrupted_pending.values["pending_call"] == pending.values["pending_call"]
        assert interrupted_pending.checkpoint.next_nodes == ("execute_call",)
        recovered = ctx.Process(
            target=_runner_process,
            args=(
                api_url, runner_url, public_pem, mcp_url, False,
                _FINAL_RUNNER_ID, tmp_path / "recovered.json",
            ),
        )
        runner_processes.append(recovered)
        recovered.start()
        await asyncio.to_thread(recovered.join, 65)
        if recovered.is_alive():
            recovered.terminate()
            await asyncio.to_thread(recovered.join, 5)
        assert recovered.exitcode == 0
        assert recovered.pid not in {crashed.pid, interrupted.pid}
        final_generation, _final_runner_id = await _run_claim(admin_engine, run_id)
        assert final_generation == second_generation + 1
        result = json.loads((tmp_path / "recovered.json").read_text())
        assert result == {"run_id": run_id, "status": "completed"}
        writes_after = await _project_writes(admin_engine, run_id)
        assert writes_after == writes_before
        final = await _root_checkpoint(reader_backend, row.thread_id)
        assert final.values["pending_call"] is None
        assert final.values["project_result"]["project"]["name"] == "Recovered Project"
        assert json.loads(final.values["read_result"]["result_json"]) == [{
            "name": "Recovered Project", "description": "one external commit",
        }]
        assert final.checkpoint.next_nodes == ()
        async with admin_engine.connect() as connection:
            query_revision = (await connection.execute(text(
                "SELECT ifc_revision_id FROM ifc_query_attempts WHERE run_id=:run_id"
            ), {"run_id": run_id})).scalar_one()
        assert query_revision == writes_before[0].revision_id
        assert len((tmp_path / "project-mutation-requests.log").read_text().splitlines()) == 1
        requests = [line.split() for line in (tmp_path / "tool-requests.log").read_text().splitlines()]
        assert [name for _pid, name, _key in requests] == [
            "ifc_project_update", "ifc_project_update", "ifc_project_update",
            "ifc_query_python",
        ]
        assert len({key for _pid, _name, key in requests[:3]}) == 1
        assert len({pid for pid, _name, _key in requests[:3]}) == 3
    finally:
        for process in runner_processes:
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
        if host_process is not None:
            await _stop_host_process(host_process)
        if reader_backend is not None:
            await reader_backend.close()
        await admin_engine.dispose()
