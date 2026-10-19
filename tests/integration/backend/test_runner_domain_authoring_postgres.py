"""Saved Runner actions publish and read Geometry and Family through production MCP."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
import signal
import socket
from time import perf_counter
from typing import Any, TypedDict
from uuid import uuid4

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from CAD2BIM_backend.bootstrap import open_backend_runtime
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.runner_context import RunnerGraphContext
from CAD2BIM_backend.domain.runtime_execution import (
    GEOMETRY_AUTHORING_EXECUTION,
    PRODUCT_AUTHORING_EXECUTION,
    RUNTIME_RESPONSE_MARGIN_SECONDS,
)
from CAD2BIM_backend.infrastructure.mcp_domain_client import RunnerDomainMcpClient, RunnerDomainOutcomeUnknown
from CAD2BIM_mcp.runner.backend_client import RunnerStateBackendClient
from CAD2BIM_mcp.runner.server import create_runner_state_mcp
from tests.integration.backend.test_runner_domain_mcp_disconnect_postgres import _serve_mcp, _stop_mcp


pytestmark = [pytest.mark.integration, pytest.mark.network]
_SECRET = b"runner-domain-b3-shared-internal-secret-32-bytes"
_PUBLICATION_TOOL = "ifc_authoring_publish"

_RING_SOURCE = """import cadquery as cq

def build(ctx):
    ctx.define_interface(
        key='ring_outer', origin_m=(0.0, 0.0, 0.0), axis=(0.0, 0.0, -1.0),
        reference_direction=(1.0, 0.0, 0.0),
        profile={'kind': 'circular', 'diameter_m': 0.10},
    )
    ctx.define_interface(
        key='ring_join', origin_m=(0.0, 0.0, 0.02), axis=(0.0, 0.0, 1.0),
        reference_direction=(1.0, 0.0, 0.0),
        profile={'kind': 'circular', 'diameter_m': 0.10},
    )
    return cq.Workplane('XY').circle(0.05).circle(0.04).extrude(0.02)
"""
_FAMILY_SOURCE = """def _solve(ctx):
    span = float(ctx.param('span'))
    return {
        'status': 'SOLVED',
        'components': [{'component_key': 'body'}],
        'fragments': [{
            'fragment_key': 'body_shape', 'binding_key': 'body_geometry',
            'local_transform': {'translation_m': (span, 0.0, 0.0)},
        }],
    }

def realize(ctx):
    return _solve(ctx)

def standard_model(ctx):
    return _solve(ctx)
"""


class _State(TypedDict, total=False):
    pending_call: dict[str, Any] | None
    geometry_draft: dict[str, Any]
    geometry_result: dict[str, Any]
    family_draft: dict[str, Any]
    family_result: dict[str, Any]
    geometry_info: dict[str, Any]
    family_info: dict[str, Any]


def _geometry_command(display_name: str) -> dict[str, Any]:
    return {
        "mode": "create",
        "display_name": display_name,
        "usage_instructions": {"action": "set", "value": "Use as the exact body Geometry of the Runner Family fixture."},
        "source": {"source_kind": "mapped_model", "source_files": [{"path": "main.py", "source": _RING_SOURCE}]},
    }


def _family_definition(display_name: str, geometry_alias: str) -> dict[str, Any]:
    definition = {
        "family_contract": {
            "product_structure_kind": "single_product",
            "standard_model": {"public_parameters": {"span": 0.43}},
            "public_parameters": [{
                "parameter_kind": "number", "parameter_key": "span", "unit": "metre",
                "minimum": 0.001, "maximum": 2.0,
            }],
            "components": [{"component_key": "body", "ifc_class": "IfcValve"}],
            "geometry_fragments": [{
                "fragment_key": "body_shape", "owner_component_key": "body",
                "binding_options": [{"binding_key": "body_geometry", "parameter_sources": {"overrides": {}}}],
                "local_transform_mode": "program",
            }],
        },
        "geometry_bindings": [{
            "binding_key": "body_geometry",
            "source": {"source_kind": "published", "resource_version_alias": geometry_alias},
            "expected_resource_kind": "mapped_model",
            "geometry_purpose": "product_component",
        }],
    }
    return {
        "mode": "create", "display_name": display_name,
        "purpose": "Publish the exact hollow ring Geometry with numeric placement.",
        "usage_instructions": {"action": "set", "value": "Use the exact published Geometry binding; span is measured in metres and places its body fragment."},
        "source": {"source_kind": "inline", "definition_json": json.dumps({"action": "set", "value": definition}),
                   "source_files": [{"path": "main.py", "source": _FAMILY_SOURCE}]},
    }


def _protocol() -> ExecutionProtocolBinding:
    return ExecutionProtocolBinding(
        bundle_id="runner_domain_authoring", module="workflow",
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


async def _retire_failed_test_run(engine, *, run_id: str, task_id: str) -> None:
    # This is isolated fixture cleanup after all owned processes stop. Retain
    # the exact checkpoint and domain Operation for the original failure audit.
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE runs SET status='cancelled', cancel_requested=true, claimed_by=NULL, "
            "lease_token=NULL, lease_expires_at=NULL, writer_generation=writer_generation+1, "
            "ended_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
            "WHERE run_id=:run_id AND task_id=:task_id "
            "AND status IN ('queued','running','paused','waiting_user')"
        ), {"run_id": run_id, "task_id": task_id})
        await connection.execute(text(
            "UPDATE tasks SET status='cancelled', updated_at=CURRENT_TIMESTAMP "
            "WHERE task_id=:task_id AND EXISTS (SELECT 1 FROM runs "
            "WHERE run_id=:run_id AND task_id=:task_id AND status='cancelled')"
        ), {"run_id": run_id, "task_id": task_id})


def _build_authoring_graph(holders, saver, _feedback_nodes):
    def issue_call(state: _State) -> dict[str, Any]:
        if "geometry_draft" not in state:
            field = "geometry_draft"
            kind = "ifc_geometry_resource_write"
            arguments = {"request": _geometry_command("Runner hollow ring")}
        elif "geometry_result" not in state:
            field = "geometry_result"
            kind = _PUBLICATION_TOOL
            arguments = {"request": {"root_kind": "geometry", "draft_ref": state["geometry_draft"]["draft_ref"],
                                      "expected_edit_token": state["geometry_draft"]["edit_token"]}}
        elif "family_draft" not in state:
            field = "family_draft"
            kind = "ifc_product_family_write"
            arguments = {"request": _family_definition(
                "Runner hollow ring Family",
                state["geometry_result"]["geometry_versions"][0]["resource_version_alias"],
            )}
        elif "family_result" not in state:
            field = "family_result"
            kind = _PUBLICATION_TOOL
            arguments = {"request": {"root_kind": "family", "draft_ref": state["family_draft"]["draft_ref"],
                                      "expected_edit_token": state["family_draft"]["edit_token"]}}
        elif "geometry_info" not in state:
            field = "geometry_info"
            kind = "ifc_geometry_resource_info"
            arguments = {"request": {"target": {"target_kind": "resource_version", "resource_version_alias": (
                state["geometry_result"]["geometry_versions"][0]["resource_version_alias"]
            )}}}
        else:
            field = "family_info"
            kind = "ifc_product_family_info"
            arguments = {"request": {"target": {"target_kind": "family_version", "family_version_alias": state["family_result"]["family_version_alias"]}}}
        return {"pending_call": {
            "id": f"authoring-{kind}-{uuid4().hex}", "kind": kind, "field": field,
            "arguments": arguments,
        }}

    async def execute_call(
        state: _State, runtime: Runtime[RunnerGraphContext], config: RunnableConfig
    ) -> dict[str, Any]:
        del runtime, config
        call = state["pending_call"]
        assert call is not None
        grant = holders["backend"].graph_runtime.require_current_execution_grant()
        if holders.get("recovery"):
            with (holders["root"] / "authoring-calls.jsonl").open("a") as stream:
                stream.write(json.dumps({"pid": os.getpid(), "runner": grant.runner_id,
                                         "key": call["id"], "kind": call["kind"],
                                         "field": call["field"], "arguments": call["arguments"]}) + "\n")
        policy = {
            "ifc_geometry_resource_write": GEOMETRY_AUTHORING_EXECUTION,
            "ifc_product_family_write": PRODUCT_AUTHORING_EXECUTION,
            _PUBLICATION_TOOL: PRODUCT_AUTHORING_EXECUTION,
        }.get(call["kind"])
        start = perf_counter()
        response = await holders["domain_client"].call_tool(
            grant=grant, action_id=call["id"], mcp_tool_name=call["kind"],
            domain_tool_name=call["kind"], arguments=call["arguments"],
            effect="READ" if call["field"].endswith("info") else "WRITE",
            timeout_seconds=(
                15 if holders.get("recovery") and call["field"] == "geometry_result" else None if policy is None
                else policy.total_seconds + RUNTIME_RESPONSE_MARGIN_SECONDS
            ),
        )
        holders["timing"][call["field"]] = round(perf_counter() - start, 3)
        if response.isError or response.structuredContent is None:
            raise RuntimeError(f"Runner {call['kind']} returned no successful structured result")
        return {"pending_call": None, call["field"]: response.structuredContent}

    graph = StateGraph(_State, context_schema=RunnerGraphContext)
    graph.add_node("issue_call", issue_call)
    graph.add_node("execute_call", execute_call)
    graph.add_edge(START, "issue_call")
    graph.add_edge("issue_call", "execute_call")
    graph.add_conditional_edges(
        "execute_call", lambda state: END if "family_info" in state else "issue_call",
    )
    return graph.compile(checkpointer=saver)


def _resume_authoring_runner(api_url, runner_url, public_pem, mcp_url, root, runner_id):
    async def run():
        holders = {"root": root, "recovery": True, "timing": {}}
        backend = await open_backend_runtime(
            api_settings=Settings(database_url=api_url, environment="test"),
            runner_settings=Settings(database_url=runner_url, environment="test"),
            public_key_pem=public_pem, issuer="https://runner-domain-b3.example",
            audience="cad2bim-runner-domain-b3", execution_protocol=_protocol(),
            graph_builder=lambda saver, nodes: _build_authoring_graph(holders, saver, nodes),
            runner_id=runner_id, state_access_secret=_SECRET,
        )
        holders["backend"] = backend
        holders["domain_client"] = RunnerDomainMcpClient(mcp_url=mcp_url, codec=backend.domain_access_codec)
        try:
            for _ in range(100):
                result = await backend.runner.execute_next()
                if result is not None:
                    assert result.status.value == "completed"
                    return
                await asyncio.sleep(0.5)
            raise AssertionError("Authoring Runner claim did not expire")
        finally:
            await backend.close()
    asyncio.run(run())


@pytest.mark.asyncio
async def test_saved_runner_actions_write_publish_and_read_real_geometry_family(
    tmp_path: Path,
) -> None:
    await _exercise_authoring(tmp_path, recovery=False)


@pytest.mark.asyncio
async def test_saved_publication_action_recovers_after_mcp_disconnect(tmp_path: Path) -> None:
    await _exercise_authoring(tmp_path, recovery=True)


async def _exercise_authoring(tmp_path: Path, *, recovery: bool) -> None:
    database_url = os.environ["CAD2BIM_TEST_DATABASE_URL"]
    parsed = make_url(database_url)
    if parsed.host not in {"127.0.0.1", "localhost"} or not (
        parsed.database or ""
    ).startswith("cad2bim_b3_runner_authoring_"):
        raise RuntimeError("Runner authoring test requires its own local B3 database")
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
    holders: dict[str, Any] = {"timing": {}, "root": tmp_path, "recovery": recovery}

    def graph_builder(saver, feedback_nodes):
        return _build_authoring_graph(holders, saver, feedback_nodes)
    backend = await open_backend_runtime(
        api_settings=Settings(database_url=api_url, environment="test"),
        runner_settings=Settings(database_url=runner_url, environment="test"),
        public_key_pem=public_pem,
        issuer="https://runner-domain-b3.example",
        audience="cad2bim-runner-domain-b3",
        execution_protocol=_protocol(), graph_builder=graph_builder,
        runner_id="runner-domain-b3",
        state_access_secret=_SECRET,
        ifc_storage_root=tmp_path / "ifc-storage",
    )
    holders["backend"] = backend
    holders["domain_client"] = RunnerDomainMcpClient(
        mcp_url=mcp_url, codec=backend.domain_access_codec,
    )
    backend_client = RunnerStateBackendClient(f"http://127.0.0.1:{backend_port}")
    server = create_runner_state_mcp(
        codec=backend.state_access_codec, domain_codec=backend.domain_access_codec,
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
    mcp_task = None if recovery else asyncio.create_task(mcp_server.serve())
    context = multiprocessing.get_context("spawn")
    mcp_process = None
    children = []
    running = None
    if recovery:
        mcp_process = context.Process(target=_serve_mcp, args=(backend_port, mcp_port, tmp_path, _SECRET))
        mcp_process.start()
        assert backend.authoring_resources is not None
        resources = backend.authoring_resources
        holders["counts"] = {"geometry_build": 0, "geometry_inspect": 0, "family_batches": 0}

        def instrument_runtime(runtime, method_name, count_name):
            original = getattr(runtime, method_name)
            async def counted(*args, **kwargs):
                holders["counts"][count_name] += 1
                return await original(*args, **kwargs)
            setattr(runtime, method_name, counted)

        instrument_runtime(resources.geometry_runtime, "build_fixed", "geometry_build")
        instrument_runtime(resources.geometry_runtime, "reopen", "geometry_inspect")
        original_batch = resources.realization_runtime.realize_batch
        def count_batch(*args, **kwargs):
            holders["counts"]["family_batches"] += 1
            return original_batch(*args, **kwargs)
        resources.realization_runtime.realize_batch = count_batch

        committed = holders["committed"] = asyncio.Event()
        release = holders["release_response"] = asyncio.Event()
        original_publish = resources.geometry_authoring.publish_draft

        async def publish_then_wait(*args, **kwargs):
            result = await original_publish(*args, **kwargs)
            if not committed.is_set():
                holders["committed_result"] = result
                committed.set()
                await release.wait()
            else:
                assert result == holders["committed_result"]
            return result

        resources.geometry_authoring.publish_draft = publish_then_wait
    admin_engine = create_async_engine(database_url)
    run_id = task_id = None
    completed = False
    try:
        async with admin_engine.connect() as connection:
            assert await connection.scalar(text(
                "SELECT count(*) FROM runs WHERE status IN ('queued','running')"
            )) == 0, "Retire this isolated test's known failed Runs before another case"
        await _wait_tcp(backend_port)
        await _wait_tcp(mcp_port)
        token = jwt.encode({
            "iss": "https://runner-domain-b3.example", "aud": "cad2bim-runner-domain-b3",
            "sub": f"person-{uuid4().hex}",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        }, key, algorithm="RS256")
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}", timeout=30,
        ) as public:
            task = await public.post("/tasks", headers=headers, json={"title": "B3 Family"})
            assert task.status_code == 201, task.text
            task_id = task.json()["task_id"]
            started = await public.post(
                f"/tasks/{task_id}/runs", headers=headers,
                json={
                    "idempotency_key": f"start-{uuid4().hex}",
                    "input_payload": {},
                },
            )
            assert started.status_code == 201, started.text
            run_id = started.json()["run_id"]
            register_started = perf_counter()
            if recovery:
                async def snapshot():
                    async with admin_engine.connect() as connection:
                        claim = (await connection.execute(text(
                            "SELECT thread_id,writer_generation,claimed_by FROM runs WHERE run_id=:id"
                        ), {"id": run_id})).one()
                        cursor = await connection.scalar(text(
                            "SELECT checkpoint_id FROM checkpoints WHERE thread_id=:id AND checkpoint_ns='' "
                            "ORDER BY checkpoint_id DESC LIMIT 1"
                        ), {"id": claim.thread_id})
                        operations = (await connection.execute(text(
                            "SELECT operation_id,status,request_fingerprint,operation_detail_json "
                            "FROM critical_operations WHERE run_id=:id AND tool_name=:kind "
                            "ORDER BY created_at"
                        ), {"id": run_id, "kind": _PUBLICATION_TOOL})).all()
                    saved = await backend.graph_runtime.get_checkpoint_state(
                        thread_id=claim.thread_id, checkpoint_id=cursor,
                    )
                    return claim, saved, operations

                running = asyncio.create_task(backend.runner.execute_next())
                await asyncio.wait_for(holders["committed"].wait(), 90)
                first_claim, pending, operations = await snapshot()
                original_call = pending.values["pending_call"]
                assert pending.checkpoint.next_nodes == ("execute_call",)
                assert original_call["kind"] == _PUBLICATION_TOOL
                assert original_call["field"] == "geometry_result"
                assert len(operations) == 1 and operations[0].status == "succeeded"
                original_operation = operations[0]
                counts_before = dict(holders["counts"])
                assert not running.done(), "The transport must be live until the forced disconnect"
                mcp_process.kill()
                await _stop_mcp(mcp_process)
                assert mcp_process.exitcode == -signal.SIGKILL and not backend_task.done()
                holders["release_response"].set()
                with pytest.raises(RunnerDomainOutcomeUnknown):
                    await asyncio.wait_for(asyncio.shield(running), 20)
                failed_claim, pending, operations = await snapshot()
                assert pending.values["pending_call"] == original_call
                assert pending.checkpoint.next_nodes == ("execute_call",)
                assert operations == [original_operation]
                mcp_process = context.Process(target=_serve_mcp, args=(backend_port, mcp_port, tmp_path, _SECRET))
                mcp_process.start()
                await _wait_tcp(mcp_port)
                resumed = context.Process(target=_resume_authoring_runner,
                    args=(api_url, runner_url, public_pem, mcp_url, tmp_path, "runner-authoring-recovered"))
                children.append(resumed)
                resumed.start()
                await asyncio.to_thread(resumed.join, 85)
                assert resumed.exitcode == 0
                final_claim, final, operations = await snapshot()
                assert final_claim.writer_generation == failed_claim.writer_generation + 1
                assert final_claim.claimed_by is None
                assert final.values["pending_call"] is None
                assert operations[0] == original_operation and len(operations) == 2
                assert holders["counts"]["geometry_build"] == counts_before["geometry_build"]
                calls = [json.loads(line) for line in (tmp_path / "authoring-calls.jsonl").read_text().splitlines()]
                published_calls = [call for call in calls if call["field"] == "geometry_result"]
                assert len(published_calls) == 2
                assert {call["pid"] for call in published_calls} == {os.getpid(), resumed.pid}
                assert {call["key"] for call in published_calls} == {original_call["id"]}
                assert all(call["arguments"] == original_call["arguments"] for call in published_calls)
                print("RUNNER_PUBLICATION_RECOVERY=" + json.dumps({
                    "run_id": run_id, "action_id": original_call["id"],
                    "operation_id": original_operation.operation_id,
                    "checkpoint_preserved": True, "same_arguments": True,
                    "geometry_builds": counts_before["geometry_build"],
                    "resumed_generation": final_claim.writer_generation,
                }, sort_keys=True))
            else:
                finished = await backend.runner.execute_next()
                assert finished is not None and finished.run_id == run_id
                assert finished.status.value == "completed"
            holders["timing"]["registration_total"] = round(
                perf_counter() - register_started, 3,
            )
            issued = await public.post(
                f"/tasks/{task_id}/runs/{run_id}/state-read-access", headers=headers,
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
            assert saved.values["pending_call"] is None
            geometry_alias = saved.values["geometry_result"]["geometry_versions"][0]["resource_version_alias"]
            family_alias = saved.values["family_result"]["family_version_alias"]
            assert geometry_alias.startswith("gv_")
            assert family_alias.startswith("fv_")
            assert saved.values["geometry_draft"]["saved"] is True
            assert saved.values["geometry_draft"]["runtime_checked"] is False
            assert saved.values["family_draft"]["saved"] is True
            assert saved.values["family_draft"]["runtime_checked"] is False
            assert saved.values["geometry_result"]["operation_alias"].startswith("op_")
            assert saved.values["family_result"]["operation_alias"].startswith("op_")
            assert saved.values["geometry_info"]["info"]["resource_version_alias"] == geometry_alias
            assert saved.values["family_info"]["info"]["family_version_alias"] == family_alias
            completed = True

        print("RUNNER_AUTHORING_TIMING=" + json.dumps(holders["timing"], sort_keys=True))
    finally:
        if recovery:
            holders.get("release_response", asyncio.Event()).set()
            if running is not None and not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            if mcp_process is not None:
                await _stop_mcp(mcp_process)
            for child in children:
                await _stop_mcp(child)
        if not completed and run_id is not None:
            await _retire_failed_test_run(admin_engine, run_id=run_id, task_id=task_id)
        backend_server.should_exit = True
        mcp_server.should_exit = True
        try:
            await asyncio.gather(backend_task, *(() if mcp_task is None else (mcp_task,)))
        finally:
            try:
                await backend_client.aclose()
            finally:
                try:
                    await admin_engine.dispose()
                finally:
                    await backend.close()
