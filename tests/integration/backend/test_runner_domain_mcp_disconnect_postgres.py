"""An isolated MCP process can disappear after IFC commit while Backend stays live."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
import signal
from typing import Any, TypedDict
from uuid import uuid4

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from langgraph.graph import END, START, StateGraph
from mcp import McpError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from CAD2BIM_backend.bootstrap import BackendRuntime, open_backend_runtime
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.runner_context import RunnerGraphContext
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity, InitializeIfcModelCommand
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from CAD2BIM_backend.infrastructure.mcp_domain_client import RunnerDomainMcpClient, RunnerDomainOutcomeUnknown
from CAD2BIM_backend.infrastructure.runner_domain_tokens import RunnerDomainTokenCodec
from CAD2BIM_backend.infrastructure.state_access_tokens import StateAccessTokenCodec
from CAD2BIM_backend.ports.ifc_revision_runtime import (
    CompileIfcProductsRevisionRequest, InitializeIfcRevisionRequest, InspectExactIfcRevisionRequest, UpdateIfcProjectRevisionRequest,
)
from CAD2BIM_mcp.runner.backend_client import RunnerStateBackendClient
from CAD2BIM_mcp.runner.server import create_runner_state_mcp
from tests.integration.backend.test_runner_domain_ifc_postgres import (
    _free_port, _protocol, _wait_tcp,
)


pytestmark = [pytest.mark.integration, pytest.mark.network]
_SECRET = b"runner-domain-mcp-isolation-secret-32-bytes"
_ISSUER = "https://runner-domain-mcp-isolation.example"
_AUDIENCE = "cad2bim-runner-domain-mcp-isolation"


class _State(TypedDict, total=False):
    pending_call: dict[str, Any] | None
    last_result: dict[str, Any]


def _serve_mcp(backend_port: int, mcp_port: int, root: Path, secret: bytes = _SECRET) -> None:
    async def run() -> None:
        backend = RunnerStateBackendClient(f"http://127.0.0.1:{backend_port}")
        server = create_runner_state_mcp(
            codec=StateAccessTokenCodec(secret),
            domain_codec=RunnerDomainTokenCodec(secret),
            backend=backend,
        )
        await uvicorn.Server(uvicorn.Config(
            server.http_app(stateless_http=True), host="127.0.0.1", port=mcp_port,
            log_level="warning", access_log=False,
        )).serve()

    asyncio.run(run())


async def _stop_mcp(process: multiprocessing.Process) -> None:
    if process.is_alive():
        process.terminate()
    await asyncio.to_thread(process.join, 10)
    if process.is_alive():
        process.kill()
        await asyncio.to_thread(process.join, 5)
    assert not process.is_alive()


async def _prepare_apply_input(backend, scope, ifc_scope, fixture):
    """Publish the frozen row through real ingestion/Transform owners in this Run."""
    from CAD2BIM_backend.domain.data_runtime import ValidatedSource
    from CAD2BIM_backend.domain.data_transform import DataTransformCommand
    from CAD2BIM_backend.domain.ifc_geometry_basis import ConfigureIfcGeometryBasisCommand

    names = ("product_key", "origin_x_m", "origin_y_m", "origin_z_m",
        "parameters_json", "external_constraints_json")
    kinds = ("string", "number", "number", "number", "string", "string")
    source = Path(__file__).resolve().parents[2] / "fixtures" / "cad" / "minimal_line.dxf"
    with bind_tenant_execution_context(scope.identity):
        imported = await backend.data_resources.runtime.import_cad_source(scope, ValidatedSource(
            filename=source.name, media_type="application/dxf", content=source.read_bytes(),
            source_identity=f"public-apply-fixture:{scope.run_id}"))
        command = DataTransformCommand.model_validate({
            "inputs": [{"input_name": "source_entities", "package_alias": imported.published.package.catalog_entry.alias,
                "payload_name": "entities"}],
            "inline_tables": [{"input_name": "frozen_row",
                "columns": [{"name": name, "type": kind, "nullable": False} for name, kind in zip(names, kinds)],
                "rows": [{"values": [{"kind": kind, "value": fixture["row"][name]} for name, kind in zip(names, kinds)]}]}],
            "execution": {"engine": "duckdb_sql", "code":
                "CREATE TEMP TABLE products AS SELECT * FROM frozen_row WHERE EXISTS (SELECT 1 FROM source_entities)"},
            "outputs": [{"payload_name": "products", "format": "parquet", "logical_key_fields": ["product_key"],
                "description": "The unchanged frozen six-column product row"}],
            "output_package": {"name": "One frozen product engineering input", "description_markdown":
                "# One frozen product engineering input\n\n## Summary\nThe unchanged frozen product row.\n\n"
                "## Intended use\nPublic Apply and disconnect integration verification.\n\n"
                "## Contents\nThe products payload contains one exact six-column row.\n\n"
                "## Method\nData Transform copies the typed inline row unchanged after a real CAD ingestion.\n\n"
                "## Scope and completeness\nOne selected frozen row; no outcome filtering.\n\n"
                "## Known limitations\nThe synthetic single-line CAD source proves publication lineage only, "
                "not CAD business correctness.\n"}})
        published = await backend.data_resources.transform.transform(scope, command,
            request_identity=f"public-apply-input:{scope.run_id}")
        family_ref = await backend.authoring_resources.family_catalog.version_ref_for_visible_id(
            scope.identity, fixture["family_version_id"])
        await backend.ifc_resources.geometry_basis.configure(ifc_scope,
            ConfigureIfcGeometryBasisCommand(
                project_unit_change={"length_unit": {"prefix": "MILLI"}, "plane_angle_unit": {}},
                coordinate_reference_change={"mode": "local"}, context_operations=(
                {"action": "create_parent_context", "context_identifier": "Model", "context_type": "Model",
                    "coordinate_space_dimension": 3, "precision": 0.00001,
                    "world_coordinate_system": {"kind": "3d", "origin": [0., 0., 0.],
                        "axis": [0., 0., 1.], "ref_direction": [1., 0., 0.]}},
                {"action": "create_subcontext", "context_identifier": "Body", "context_type": "Model",
                    "parent_context_identifier": "Model", "target_view": "MODEL_VIEW"})),
            idempotency_key=f"body-fixture:{uuid4().hex}")
    return {"mode": "apply", "family_version_alias": family_ref,
        "package_alias": published.published.package.catalog_entry.alias, "payload_name": "products"}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["ifc_model_initialize", "ifc_project_update", "ifc_products_apply"])
async def test_only_mcp_process_dies_after_ifc_commit_then_same_action_replays(
    tmp_path: Path, tool_name: str,
) -> None:
    database_url = os.environ["CAD2BIM_TEST_DATABASE_URL"]
    parsed = make_url(database_url)
    if parsed.host not in {"127.0.0.1", "localhost"} or not (
        parsed.database or ""
    ).startswith("cad2bim_runner_domain_b4_mcp_"):
        raise RuntimeError("MCP isolation test requires its own local Runner database")
    api_url = parsed.set(
        username="cad2bim_api_login", password=os.environ["CAD2BIM_API_POSTGRES_PASSWORD"],
    ).render_as_string(hide_password=False)
    runner_url = parsed.set(
        username="cad2bim_runner_login", password=os.environ["CAD2BIM_RUNNER_POSTGRES_PASSWORD"],
    ).render_as_string(hide_password=False)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    backend_port, mcp_port = _free_port(), _free_port()
    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
    holders: dict[str, Any] = {
        "committed": asyncio.Event(),
        "worker_active": asyncio.Event(),
        "release_worker": asyncio.Event(),
        "release_backend": asyncio.Event(),
        "action_ids": [],
        "mutations": 0,
        "inspections": 0,
    }
    project_case = tool_name == "ifc_project_update"
    apply_case = tool_name == "ifc_products_apply"
    storage_root = Path(os.environ["CAD2BIM_STORAGE_ROOT"]) if apply_case else tmp_path / "ifc-storage"
    fixture = json.loads(Path(os.environ["CAD2BIM_TEST_PRODUCT_APPLY_FIXTURE_JSON"]).read_text()) if apply_case else None

    def graph_builder(saver, _feedback_nodes):
        def issue_call(_state: _State) -> dict[str, Any]:
            return {"pending_call": {
                "id": f"call:{uuid4().hex}",
                "arguments": (holders["apply_arguments"] if apply_case else {"patch": {"name": "MCP isolated Project"}}
                              if project_case else {"project_name": "MCP isolated Project"}),
            }}

        async def execute_call(state: _State) -> dict[str, Any]:
            call = state["pending_call"]
            assert call is not None
            backend: BackendRuntime = holders["backend"]
            grant = backend.graph_runtime.require_current_execution_grant()
            holders["grant"] = grant
            holders["action_ids"].append(call["id"])
            result = await holders["domain_client"].call_tool(
                grant=grant, action_id=call["id"],
                mcp_tool_name=tool_name,
                domain_tool_name=tool_name,
                arguments=call["arguments"], timeout_seconds=45,
            )
            if result.isError or result.structuredContent is None:
                raise RuntimeError("replayed IFC initialization returned no structured result")
            return {"pending_call": None, "last_result": result.structuredContent}

        graph = StateGraph(_State, context_schema=RunnerGraphContext)
        graph.add_node("issue_call", issue_call)
        graph.add_node("execute_call", execute_call)
        graph.add_edge(START, "issue_call")
        graph.add_edge("issue_call", "execute_call")
        graph.add_edge("execute_call", END)
        return graph.compile(checkpointer=saver)

    backend = await open_backend_runtime(
        api_settings=Settings(database_url=api_url, environment="test"),
        runner_settings=Settings(database_url=runner_url, environment="test"),
        public_key_pem=public_pem,
        issuer=_ISSUER, audience=_AUDIENCE,
        execution_protocol=_protocol(), graph_builder=graph_builder,
        runner_id="runner-domain-mcp-isolation",
        state_access_secret=_SECRET,
        ifc_storage_root=storage_root,
    )
    holders["backend"] = backend
    holders["domain_client"] = RunnerDomainMcpClient(
        mcp_url=mcp_url, codec=backend.domain_access_codec,
    )
    assert backend.ifc_resources is not None
    revision_runtime = backend.ifc_resources.revision_runtime
    original_execute = revision_runtime.execute
    original_launch = revision_runtime._supervisor.launch

    async def observe_live_worker(*args, **kwargs):
        process = await original_launch(*args, **kwargs)
        if project_case and holders.get("project_executing") and not holders["worker_active"].is_set():
            pid = process._cad2bim_worker_pid
            os.kill(pid, signal.SIGSTOP)
            holders["worker_pid"] = pid
            holders["worker_active"].set()
            try:
                await holders["release_worker"].wait()
            finally:
                os.kill(pid, signal.SIGCONT)
        return process

    revision_runtime._supervisor.launch = observe_live_worker

    async def count_initialize(request, *, source=None):
        if isinstance(request, InspectExactIfcRevisionRequest):
            holders["inspections"] += 1
        target = (CompileIfcProductsRevisionRequest if apply_case else
            UpdateIfcProjectRevisionRequest if project_case else InitializeIfcRevisionRequest)
        if isinstance(request, target):
            holders["mutations"] += 1
        holders["project_executing"] = isinstance(request, UpdateIfcProjectRevisionRequest)
        try:
            return await original_execute(request, source=source)
        finally:
            holders["project_executing"] = False

    revision_runtime.execute = count_initialize
    original_initialize = backend.ifc_resources.model.initialize
    original_call = (backend.ifc_resources.products_apply.apply if apply_case else
        backend.ifc_resources.project.update if project_case else original_initialize)

    async def commit_then_wait(*args, **kwargs):
        result = await original_call(*args, **kwargs)
        holders.setdefault("original_result", result)
        assert result == holders["original_result"]
        holders["committed"].set()
        await holders["release_backend"].wait()
        return result

    if apply_case:
        backend.ifc_resources.products_apply.apply = commit_then_wait
    elif project_case:
        backend.ifc_resources.project.update = commit_then_wait
    else:
        backend.ifc_resources.model.initialize = commit_then_wait
    backend_server = uvicorn.Server(uvicorn.Config(
        backend.api, host="127.0.0.1", port=backend_port,
        log_level="warning", lifespan="off", access_log=False,
    ))
    backend_task = asyncio.create_task(backend_server.serve())
    process_context = multiprocessing.get_context("spawn")
    mcp_process: multiprocessing.Process | None = None
    running: asyncio.Task | None = None
    admin_engine = create_async_engine(database_url)
    try:
        if not apply_case:
            async with admin_engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM runs")) == 0, (
                    "Use a fresh dedicated database: a prior pending Run owns its original storage"
                )
        await _wait_tcp(backend_port)
        mcp_process = process_context.Process(
            target=_serve_mcp, args=(backend_port, mcp_port, tmp_path),
        )
        mcp_process.start()
        await _wait_tcp(mcp_port)
        token = jwt.encode({
            "iss": _ISSUER, "aud": _AUDIENCE,
            "sub": f"person-{uuid4().hex}",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        }, key, algorithm="RS256")
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}", timeout=30,
        ) as public:
            task = await public.post(
                "/tasks", headers=headers, json={"title": "B4 MCP process isolation"},
            )
            assert task.status_code == 201, task.text
            task_id = task.json()["task_id"]
            started = await public.post(
                f"/tasks/{task_id}/runs", headers=headers,
                json={"idempotency_key": f"start-{uuid4().hex}", "input_payload": {}},
            )
            assert started.status_code == 201, started.text
            run_id = started.json()["run_id"]
            if project_case or apply_case:
                async with admin_engine.connect() as connection:
                    row = (await connection.execute(text(
                        "SELECT r.owner_id,r.tenant_id,b.principal_id FROM runs r "
                        "JOIN task_tenant_bindings b ON b.tenant_id=r.tenant_id AND b.task_id=r.task_id "
                        "WHERE r.run_id=:run_id"
                    ), {"run_id": run_id})).one()
                identity = TenantExecutionContext(
                    tenant_id=row.tenant_id, principal_id=row.principal_id, task_id=task_id,
                    actor_kind=TenantActorKind.RUNNER if apply_case else TenantActorKind.USER,
                    command_id=f"fixture-init:{uuid4().hex}",
                )
                with bind_tenant_execution_context(identity):
                    ifc_scope = IfcRunIdentity(
                        owner_id=row.owner_id, tenant_id=row.tenant_id, task_id=task_id, run_id=run_id,
                    )
                    await original_initialize(ifc_scope, InitializeIfcModelCommand(project_name="Initial Project"))
                if apply_case:
                    from CAD2BIM_backend.domain.control_plane import ExecutionMode
                    from CAD2BIM_backend.domain.invocation import InvocationScope
                    input_scope = InvocationScope(identity=identity, owner_id=row.owner_id, run_id=run_id,
                        execution_mode=ExecutionMode.GRAPH, catalog_frontier=None)
                    holders["apply_arguments"] = await _prepare_apply_input(
                        backend, input_scope, ifc_scope, fixture)
            running = asyncio.create_task(backend.runner.execute_next())
            barrier = holders["worker_active"] if project_case else holders["committed"]
            reached_barrier = asyncio.create_task(barrier.wait())
            try:
                done, _ = await asyncio.wait({running, reached_barrier}, timeout=40,
                    return_when=asyncio.FIRST_COMPLETED)
                if running in done:
                    await running  # Preserve the original ToolError/cause instead of a barrier timeout.
                    raise AssertionError("Runner settled before the expected disconnect barrier")
                assert reached_barrier in done, "IFC commit did not reach the disconnect barrier within 40 seconds"
            finally:
                reached_barrier.cancel()
                await asyncio.gather(reached_barrier, return_exceptions=True)
            assert mcp_process.pid is not None and mcp_process.pid != os.getpid()
            mcp_process.kill()
            await _stop_mcp(mcp_process)
            assert mcp_process.exitcode == -signal.SIGKILL
            mcp_process = None
            assert not backend_task.done(), "Backend must survive the MCP process loss"
            authorization = backend.domain_access_codec.issue(
                holders["grant"], action_id=holders["action_ids"][0],
                tool_name=tool_name,
            )
            backend_client = RunnerStateBackendClient(f"http://127.0.0.1:{backend_port}")
            try:
                await backend_client.authorize_domain(access_token=authorization)
            finally:
                await backend_client.aclose()
            holders["release_worker"].set()
            await asyncio.wait_for(holders["committed"].wait(), timeout=30)
            holders["release_backend"].set()
            # The SDK swallows an SSE EOF without an event ID, then reports
            # REQUEST_TIMEOUT at the original request deadline. Let the graph
            # fail through that real client boundary, without cancelling it.
            with pytest.raises(RunnerDomainOutcomeUnknown) as failure:
                await asyncio.wait_for(asyncio.shield(running), timeout=50)
            assert isinstance(failure.value.__cause__, McpError)
            assert failure.value.__cause__.error.code == httpx.codes.REQUEST_TIMEOUT
            async with admin_engine.connect() as connection:
                claim_before = (await connection.execute(text(
                    "SELECT thread_id, writer_generation, claimed_by FROM runs WHERE run_id=:run_id"
                ), {"run_id": run_id})).one()
                checkpoint_id = (await connection.execute(text(
                    "SELECT checkpoint_id FROM checkpoints "
                    "WHERE thread_id=:thread_id AND checkpoint_ns='' "
                    "ORDER BY checkpoint_id DESC LIMIT 1"
                ), {"thread_id": claim_before.thread_id})).scalar_one()
            assert claim_before.claimed_by == "runner-domain-mcp-isolation"
            pending = await backend.graph_runtime.get_checkpoint_state(
                thread_id=claim_before.thread_id, checkpoint_id=checkpoint_id,
            )
            assert pending is not None
            assert pending.checkpoint.next_nodes == ("execute_call",)
            assert pending.values["pending_call"]["id"] == holders["action_ids"][0]
            assert holders["mutations"] == 1
            mcp_process = process_context.Process(
                target=_serve_mcp, args=(backend_port, mcp_port, tmp_path),
            )
            mcp_process.start()
            await _wait_tcp(mcp_port)
            finished = None
            for _ in range(100):
                finished = await backend.runner.execute_next()
                if finished is not None:
                    break
                await asyncio.sleep(0.5)
            assert finished is not None and finished.run_id == run_id
            assert finished.status.value == "completed"
            assert holders["action_ids"] == [holders["action_ids"][0]] * 2
            assert holders["mutations"] == 1
            async with admin_engine.connect() as connection:
                generation_after = await connection.scalar(text(
                    "SELECT writer_generation FROM runs WHERE run_id=:run_id"
                ), {"run_id": run_id})
            assert generation_after == claim_before.writer_generation + 1
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
            assert saved is not None and saved.values["pending_call"] is None
            operation_result = saved.values["last_result"]
            if apply_case:
                operation_result = operation_result["operation"]
                assert operation_result["outcome"] == "succeeded"
                assert operation_result["primary_result"]["summary"]["committed_count"] == 1
            assert operation_result["operation_alias"].startswith("op_")
            async with admin_engine.connect() as connection:
                operation_count = await connection.scalar(text(
                    "SELECT count(*) FROM critical_operations "
                    "WHERE run_id = :run_id AND tool_name = :tool_name"
                ), {"run_id": run_id, "tool_name": tool_name})
            assert operation_count == 1
            async with admin_engine.connect() as connection:
                revisions = (await connection.execute(text(
                    "SELECT r.revision_id, r.revision_no FROM ifc_model_revisions r "
                    "JOIN critical_operations o ON o.operation_id=r.operation_id "
                    "WHERE o.run_id=:run_id AND o.tool_name=:tool_name"
                ), {"run_id": run_id, "tool_name": tool_name})).all()
            assert len(revisions) == 1 and revisions[0].revision_no == (3 if apply_case else 2 if project_case else 1)
            if apply_case:
                async with admin_engine.connect() as connection:
                    reports = (await connection.execute(text(
                        "SELECT o.result_package_id, count(c.catalog_entry_id) AS entries "
                        "FROM critical_operations o JOIN data_catalog_entries c ON c.package_id=o.result_package_id "
                        "AND c.run_id=o.run_id WHERE o.run_id=:run_id AND o.tool_name=:tool_name "
                        "GROUP BY o.result_package_id"), {"run_id": run_id, "tool_name": tool_name})).all()
                assert len(reports) == 1 and reports[0].entries == 1
            print({"run_id": run_id, "action_ids": holders["action_ids"],
                   "runner_pid": os.getpid(), "runner_id": claim_before.claimed_by,
                   "generation_before": claim_before.writer_generation,
                   "generation_after": generation_after,
                   "checkpoint_id": checkpoint_id, "next_nodes": pending.checkpoint.next_nodes,
                   "mutations": holders["mutations"], "inspections": holders["inspections"],
                   "worker_pid": holders.get("worker_pid"), "revisions": [tuple(r) for r in revisions],
                   "result": saved.values["last_result"]})
    finally:
        holders["release_backend"].set()
        holders["release_worker"].set()
        if running is not None and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        if mcp_process is not None:
            await _stop_mcp(mcp_process)
        backend_server.should_exit = True
        try:
            await backend_task
        finally:
            try:
                await admin_engine.dispose()
            finally:
                await backend.close()
