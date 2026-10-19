"""Independent MCP HTTP, backend HTTP and PostgreSQL through a real Runner subgraph."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any, TypedDict
from uuid import uuid4

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from mcp.shared.exceptions import McpError
from sqlalchemy import text
from sqlalchemy.engine import make_url

from CAD2BIM_backend.application.requirement_state_actions import RequirementStateActions
from CAD2BIM_backend.application.role_state_handoff import accept_role_output, enter_role
from CAD2BIM_backend.infrastructure.agent_skills import AgentSkills
from CAD2BIM_backend.bootstrap import BackendRuntime, open_backend_runtime
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.requirement_state import (
    ChangeRequirementRows,
    RequirementRowChange,
    RequirementRowDraft,
    RequirementState,
    SavedRequirementCommand,
)
from CAD2BIM_backend.domain.runner_context import RunnerGraphContext
from CAD2BIM_backend.infrastructure.langgraph.node_read_binding import resolve_node_read_binding
from CAD2BIM_backend.infrastructure.markdown_editor import DeepAgentsMarkdownEditor
from CAD2BIM_backend.infrastructure.mcp_state_client import RunnerStateMcpClient
from CAD2BIM_backend.infrastructure.postgres.database import create_database
from CAD2BIM_backend.infrastructure.postgres.repository import PostgresControlPlaneRepository


pytestmark = [pytest.mark.integration, pytest.mark.network]
_EDIT_OWNED = ("requirements", "clarifications", "previous_clarifications")
_EDIT_RESOURCE_OWNERS = {
    "requirements": "edit_role",
    "clarifications": "edit_role",
    "requirement_md": "",
}
_READ_RESOURCE_OWNERS = {field: "" for field in _EDIT_RESOURCE_OWNERS}
_SECRET = b"runner-state-batch-two-test-secret-32-bytes"


class _State(TypedDict, total=False):
    requirements: Any
    clarifications: Any
    previous_clarifications: Any
    requirement_md: Any
    saved_command: Any
    action_result: Any
    parent_before: Any
    role_memories: Any
    messages: Any
    conversation_summary: str
    tool_chain_summary: str
    role: str
    stage_key: str
    business_entry_id: str
    inspection: Any
    before_uris: Any
    role_kind: str


class _RoleMemoryState(TypedDict, total=False):
    role: str
    stage_key: str
    business_entry_id: str
    messages: Any
    conversation_summary: str
    tool_chain_summary: str
    role_kind: str
    inspection: Any
    before_uris: Any


class _EditRoleState(_RoleMemoryState, total=False):
    requirements: Any
    clarifications: Any
    previous_clarifications: Any
    saved_command: Any
    action_result: Any


class _ReadRoleState(_RoleMemoryState):
    pass


def _protocol() -> ExecutionProtocolBinding:
    return ExecutionProtocolBinding(
        bundle_id="runner_state_resources",
        module="workflow",
        stage_key="cad2bim_workflow",
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _wait_tcp(port: int, *, process: subprocess.Popen[str] | None = None) -> None:
    for _ in range(100):
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"independent MCP process exited: {process.returncode}")
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise RuntimeError(f"HTTP process on port {port} did not start")


async def _stop_test_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait, timeout=5)
    assert process.poll() is not None


def _readback_graph_builder(saver, _feedback_nodes):
    """The restarted read service never executes this graph; it reads saved roots."""

    graph = StateGraph(_State, context_schema=RunnerGraphContext)
    graph.add_node("readback", lambda _state: {})
    graph.add_edge(START, "readback")
    graph.add_edge("readback", END)
    return graph.compile(checkpointer=saver)


async def _serve_readback_backend() -> None:
    backend = await open_backend_runtime(
        api_settings=Settings(database_url=os.environ["CAD2BIM_READBACK_API_URL"], environment="test"),
        runner_settings=Settings(
            database_url=os.environ["CAD2BIM_READBACK_RUNNER_URL"], environment="test"
        ),
        public_key_pem=Path(os.environ["CAD2BIM_READBACK_PUBLIC_KEY_PATH"]).read_bytes(),
        issuer="https://runner-state-b2.example",
        audience="cad2bim-runner-b2",
        execution_protocol=_protocol(),
        graph_builder=_readback_graph_builder,
        runner_id="runner-state-readback",
        state_access_secret=base64.b64decode(os.environ["CAD2BIM_STATE_ACCESS_SECRET_B64"]),
    )
    try:
        server = uvicorn.Server(
            uvicorn.Config(
                backend.api,
                host="127.0.0.1",
                port=int(os.environ["CAD2BIM_READBACK_PORT"]),
                log_level="warning",
                lifespan="off",
                access_log=False,
            )
        )
        await server.serve()
    finally:
        await backend.close()


def _user_header(key: rsa.RSAPrivateKey, *, subject: str) -> dict[str, str]:
    token = jwt.encode(
        {
            "iss": "https://runner-state-b2.example",
            "aud": "cad2bim-runner-b2",
            "sub": subject,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        key,
        algorithm="RS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_runner_subgraph_resources_skill_and_completed_readback(tmp_path: Path) -> None:
    database_url = os.environ["CAD2BIM_TEST_DATABASE_URL"]
    api_url = make_url(database_url).set(
        username="cad2bim_api_login", password=os.environ["CAD2BIM_API_POSTGRES_PASSWORD"]
    )
    runner_url = make_url(database_url).set(
        username="cad2bim_runner_login", password=os.environ["CAD2BIM_RUNNER_POSTGRES_PASSWORD"]
    )
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = signing_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    skill_text = "Read listed State resources by their exact URI."
    optional_skill_text = "Use the saved source."
    skill_root = tmp_path / "skills"
    for name, body in (("mcp-tool-usage", skill_text), ("on-demand", optional_skill_text)):
        package = skill_root / name
        package.mkdir(parents=True)
        (package / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Read current tool usage.\n---\n{body}", encoding="utf-8")
    skills = AgentSkills(skill_root)
    await skills.discover()

    async def read_skill(state, name, call_key, *, config=None, runtime=None):
        call_id = f"read-skill:{state['business_entry_id']}:{call_key}"
        call = {"name": "read_file", "args": {"file_path": f"/{name}/SKILL.md"}, "id": call_id}
        result = await skills.aread(call, state=state, config=config or {}, runtime=runtime)
        assert isinstance(result, ToolMessage)
        result = result.model_copy(update={"id": f"result:{call_id}"})
        return {"messages": [*state.get("messages", ()),
            AIMessage(id=f"call:{call_id}", content="", tool_calls=[call]), result]}
    backend_port = _free_port()
    mcp_port = _free_port()
    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
    backend_holder: dict[str, BackendRuntime] = {}
    mcp_holder: dict[str, RunnerStateMcpClient] = {}
    editor = RequirementStateActions(markdown_editor=DeepAgentsMarkdownEditor())

    def graph_builder(saver, _feedback_nodes):
        async def enter(state: _State, runtime: Runtime[RunnerGraphContext]) -> dict[str, Any]:
            ownership = {"edit": _EDIT_OWNED, "read": ()}
            owned_fields = ownership[state["role_kind"]]
            entry = enter_role(
                state,
                role="requirement",
                stage_key="cad2bim_workflow",
                business_entry_id=f"entry_{runtime.context.scope.run_id}",
                owned_fields=owned_fields,
                business_schema=RequirementState,
            )
            return {
                **entry.child_state,
                "parent_before": {
                    **{
                        field: state.get(field)
                        for field in (
                            "requirements",
                            "clarifications",
                            "previous_clarifications",
                            "requirement_md",
                        )
                    },
                    "role_memories": state.get("role_memories", {}),
                },
            }

        async def _read_binding(
            runtime: Runtime[RunnerGraphContext],
            config: RunnableConfig,
            *,
            graph_path: str,
            owners: dict[str, str],
        ):
            backend = backend_holder["runtime"]
            scope = runtime.context.scope
            repository = PostgresControlPlaneRepository(backend.runner_database.session_factory)
            run = await repository.get_run(scope.owner_id, scope.run_id)
            assert run is not None
            assert run.tenant_id == scope.identity.tenant_id
            assert run.task_id == scope.identity.task_id
            grant = backend.graph_runtime.require_current_execution_grant()
            binding = await resolve_node_read_binding(
                run=run,
                grant=grant,
                node_config=config,
                registered_graph_paths=("", graph_path),
                field_owner_paths=owners,
                graph_runtime=backend.graph_runtime,
            )
            return backend, run, grant, binding

        async def inspect_before(
            state: _EditRoleState,
            runtime: Runtime[RunnerGraphContext],
            config: RunnableConfig,
        ) -> dict[str, Any]:
            assert "requirement_md" not in state
            backend, run, grant, binding = await _read_binding(
                runtime, config, graph_path="edit_role", owners=_EDIT_RESOURCE_OWNERS
            )
            async with mcp_holder["client"].open_read_batch(grant=grant, binding=binding) as batch:
                uris = {str(item.uri) for item in await batch.list_resources()}
                old = json.loads(await batch.read_text("state://requirements"))
                markdown = await batch.read_text("state://requirement-md")
                clarifications = json.loads(await batch.read_text("state://clarifications"))
                # Inject only claim state in the isolated DB; both real HTTP boundaries
                # must reject the already issued credentials, without a new MCP session.
                admin = create_database(Settings(database_url=database_url, environment="test"))
                pair = backend.state_access_codec.issue_running(grant, binding)
                headers = {
                    "Authorization": f"Bearer {pair.access_token}",
                    "X-CAD2BIM-State-Binding": pair.read_binding,
                }
                update_claim = text(
                    "UPDATE runs SET writer_generation = :generation, lease_expires_at = :expiry "
                    "WHERE run_id = :run_id"
                )
                try:
                    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{backend_port}") as probe:
                        for generation, expiry in (
                            (run.writer_generation + 1, run.lease_expires_at),
                            (run.writer_generation, datetime.now(timezone.utc) - timedelta(seconds=1)),
                        ):
                            try:
                                async with admin.session_factory() as session, session.begin():
                                    await session.execute(update_claim, {
                                        "generation": generation, "expiry": expiry, "run_id": run.run_id,
                                    })
                                with pytest.raises(McpError):
                                    await batch.list_resources()
                                with pytest.raises(McpError):
                                    await batch.read_text("state://requirement-md")
                                for path, body in (
                                    ("list", {}), ("read", {"uri": "state://requirement-md"}),
                                ):
                                    rejected = await probe.post(
                                        f"/internal/state/resources/{path}", headers=headers, json=body,
                                    )
                                    assert rejected.status_code == 403
                            finally:
                                async with admin.session_factory() as session, session.begin():
                                    await session.execute(update_claim, {
                                        "generation": run.writer_generation,
                                        "expiry": run.lease_expires_at, "run_id": run.run_id,
                                    })
                    assert await batch.read_text("state://requirement-md") == markdown
                finally:
                    await admin.dispose()
            assert uris == {"state://requirements", "state://requirement-md", "state://clarifications"}
            assert old["requirements"][0]["purpose"] == "A parent purpose"
            assert markdown == "A parent MD"
            assert clarifications == state["clarifications"]
            assert clarifications[0]["id"] == "clar_parent"
            return {"before_uris": sorted(uris)}

        async def decide_rows(
            state: _EditRoleState, runtime: Runtime[RunnerGraphContext]
        ) -> dict[str, Any]:
            command = SavedRequirementCommand(
                command_id=f"cmd_{uuid4().hex}",
                role_execution_id=runtime.context.scope.identity.command_id,
                action=ChangeRequirementRows(
                    changes=[
                        RequirementRowChange(
                            operation="replace",
                            row_id="req_old",
                            row=RequirementRowDraft(
                                purpose="A child purpose",
                                ifc_structure="pipe",
                                modeling_method="axis",
                                tools=["MCP"],
                            ),
                        )
                    ]
                ),
            )
            return {"saved_command": command.model_dump(mode="python")}

        def apply_saved(
            state: _EditRoleState, runtime: Runtime[RunnerGraphContext]
        ) -> dict[str, Any]:
            command = SavedRequirementCommand.model_validate(state["saved_command"])
            result = editor.apply(
                RequirementState.from_checkpoint_values(state),
                command,
                role_execution_id=runtime.context.scope.identity.command_id,
                owned_fields=_EDIT_OWNED,
            )
            return {
                **result.updates,
                "action_result": {
                    "command_id": command.command_id,
                    "changed": result.changed,
                    "created_item_ids": result.created_item_ids,
                },
            }

        async def inspect_edit(
            state: _EditRoleState,
            runtime: Runtime[RunnerGraphContext],
            config: RunnableConfig,
        ) -> dict[str, Any]:
            backend, run, grant, binding = await _read_binding(
                runtime, config, graph_path="edit_role", owners=_EDIT_RESOURCE_OWNERS
            )
            root_id = binding.checkpoint_cursor.root_checkpoint_id
            parent_input = await backend.graph_runtime.get_checkpoint_state(
                thread_id=run.thread_id, checkpoint_id=root_id, checkpoint_ns=""
            )
            assert parent_input is not None
            parent_requirements = RequirementState.from_checkpoint_values(parent_input.values)
            assert parent_requirements.requirements is not None
            assert parent_requirements.requirements.requirements[0].purpose == "A parent purpose"
            async with mcp_holder["client"].open_read_batch(grant=grant, binding=binding) as batch:
                uris = {str(item.uri) for item in await batch.list_resources()}
                requirements = json.loads(await batch.read_text("state://requirements"))
                markdown = await batch.read_text("state://requirement-md")
                clarifications = json.loads(await batch.read_text("state://clarifications"))
            consumed = await read_skill(state, "on-demand", "original", config=config, runtime=runtime)
            assert uris == {"state://requirements", "state://requirement-md", "state://clarifications"}
            assert requirements["requirements"][0]["id"] == "req_old"
            assert requirements["requirements"][0]["purpose"] == "A child purpose"
            assert markdown == "A parent MD"
            assert clarifications == state["clarifications"]
            assert clarifications[0]["id"] == "clar_parent"
            assert optional_skill_text in consumed["messages"][-1].content
            return {
                **consumed,
                "inspection": {"child_uris": sorted(uris), "parent_old": True},
            }

        async def drift_and_read_current(
            state: _EditRoleState,
            runtime: Runtime[RunnerGraphContext],
            config: RunnableConfig,
        ) -> dict[str, Any]:
            (skill_root / "on-demand" / "SKILL.md").write_text(
                "---\nname: on-demand\ndescription: Current tool usage.\n---\nChanged on-demand instructions.", encoding="utf-8")
            consumed = await read_skill(state, "on-demand", "current", config=config, runtime=runtime)
            assert optional_skill_text in consumed["messages"][-3].content
            assert "Changed on-demand instructions." in consumed["messages"][-1].content
            return {**consumed, "inspection": {**state["inspection"], "read_current": True}}

        async def inspect_readonly(
            state: _ReadRoleState,
            runtime: Runtime[RunnerGraphContext],
            config: RunnableConfig,
        ) -> dict[str, Any]:
            assert not set(_EDIT_OWNED + ("requirement_md",)) & set(state)
            _, _, grant, binding = await _read_binding(
                runtime, config, graph_path="read_role", owners=_READ_RESOURCE_OWNERS
            )
            async with mcp_holder["client"].open_read_batch(grant=grant, binding=binding) as batch:
                uris = {str(item.uri) for item in await batch.list_resources()}
                requirements = json.loads(await batch.read_text("state://requirements"))
                markdown = await batch.read_text("state://requirement-md")
                clarifications = json.loads(await batch.read_text("state://clarifications"))
            assert uris == {
                "state://requirements",
                "state://clarifications",
                "state://requirement-md",
            }
            assert requirements["requirements"][0]["purpose"] == "B parent purpose"
            assert markdown == "B parent MD" and clarifications == []
            return {"inspection": {"readonly_uris": sorted(uris), "no_body_copy": True}}

        def accept(state: _State) -> dict[str, Any]:
            owned_fields = {"edit": _EDIT_OWNED, "read": ()}[state["role_kind"]]
            return accept_role_output(
                state["parent_before"],
                state,
                role="requirement",
                stage_key="cad2bim_workflow",
                owned_fields=owned_fields,
                business_schema=RequirementState,
            )

        async def read_required_skill(state: _RoleMemoryState, runtime: Runtime[RunnerGraphContext],
                                      config: RunnableConfig) -> dict[str, Any]:
            return await read_skill(state, "mcp-tool-usage", "required", config=config, runtime=runtime)

        edit_role = StateGraph(_EditRoleState, context_schema=RunnerGraphContext)
        edit_role.add_node("read_required_skill", read_required_skill)
        for name, action in (
            ("inspect_before", inspect_before),
            ("decide_rows", decide_rows),
            ("apply_rows", apply_saved),
            ("inspect_edit", inspect_edit),
            ("drift_and_read_current", drift_and_read_current),
        ):
            edit_role.add_node(name, action)
        edit_role.add_edge(START, "read_required_skill")
        edit_role.add_edge("read_required_skill", "inspect_before")
        for before, after in (
            ("decide_rows", "apply_rows"),
            ("inspect_before", "decide_rows"),
            ("apply_rows", "inspect_edit"),
            ("inspect_edit", "drift_and_read_current"),
        ):
            edit_role.add_edge(before, after)
        edit_role.add_edge("drift_and_read_current", END)
        read_role = StateGraph(_ReadRoleState, context_schema=RunnerGraphContext)
        read_role.add_node("read_required_skill", read_required_skill)
        read_role.add_node("inspect_readonly", inspect_readonly)
        read_role.add_edge(START, "read_required_skill")
        read_role.add_edge("read_required_skill", "inspect_readonly")
        read_role.add_edge("inspect_readonly", END)
        graph = StateGraph(_State, context_schema=RunnerGraphContext)
        graph.add_node("enter", enter)
        graph.add_node("edit_role", edit_role.compile())
        graph.add_node("read_role", read_role.compile())
        graph.add_node("accept", accept)
        graph.add_edge(START, "enter")
        graph.add_conditional_edges(
            "enter", lambda state: {"edit": "edit_role", "read": "read_role"}[state["role_kind"]]
        )
        graph.add_edge("edit_role", "accept")
        graph.add_edge("read_role", "accept")
        graph.add_edge("accept", END)
        return graph.compile(checkpointer=saver)

    backend = await open_backend_runtime(
        api_settings=Settings(
            database_url=api_url.render_as_string(hide_password=False), environment="test"
        ),
        runner_settings=Settings(
            database_url=runner_url.render_as_string(hide_password=False), environment="test"
        ),
        public_key_pem=public_pem,
        issuer="https://runner-state-b2.example",
        audience="cad2bim-runner-b2",
        execution_protocol=_protocol(),
        graph_builder=graph_builder,
        runner_id="runner-state-b2",
        state_access_secret=_SECRET,
    )
    backend_holder["runtime"] = backend
    mcp_holder["client"] = RunnerStateMcpClient(mcp_url=mcp_url, codec=backend.state_access_codec)
    api_server = uvicorn.Server(
        uvicorn.Config(
            backend.api,
            host="127.0.0.1",
            port=backend_port,
            log_level="warning",
            lifespan="off",
            access_log=False,
        )
    )
    api_task = asyncio.create_task(api_server.serve())
    original_backend_closed = False
    mcp_process: subprocess.Popen[str] | None = None
    readback_process: subprocess.Popen[str] | None = None
    mcp_log = tmp_path / "runner-mcp.log"
    created_run_ids: list[str] = []
    try:
        for database, role in (
            (backend.api_database, "cad2bim_api_login"),
            (backend.runner_database, "cad2bim_runner_login"),
        ):
            async with database.engine.connect() as connection:
                actual = (await connection.execute(text(
                    "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
                ))).one()
                assert tuple(actual) == (role, False, False)
        await _wait_tcp(backend_port)
        env = dict(os.environ)
        env.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "CAD2BIM_STATE_ACCESS_SECRET_B64": base64.b64encode(_SECRET).decode("ascii"),
                "CAD2BIM_RUNNER_STATE_BACKEND_URL": f"http://127.0.0.1:{backend_port}",
                "CAD2BIM_RUNNER_MCP_PORT": str(mcp_port),
            }
        )
        with mcp_log.open("w", encoding="utf-8") as output:
            mcp_process = subprocess.Popen(
                [sys.executable, "-m", "CAD2BIM_mcp.runner.server"],
                cwd=Path(__file__).resolve().parents[3],
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
            )
            await _wait_tcp(mcp_port, process=mcp_process)
            user_a = _user_header(signing_key, subject=f"person-{uuid4().hex}")
            user_b = _user_header(signing_key, subject=f"person-{uuid4().hex}")
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{backend_port}", timeout=20
            ) as public:
                task_a = await public.post("/tasks", headers=user_a, json={"title": "B2 A"})
                task_b = await public.post("/tasks", headers=user_b, json={"title": "B2 B"})
                task_a_other = await public.post(
                    "/tasks", headers=user_a, json={"title": "B2 A other"}
                )
                assert task_a.status_code == task_b.status_code == task_a_other.status_code == 201
                task_a_id = task_a.json()["task_id"]
                task_b_id = task_b.json()["task_id"]
                task_a_other_id = task_a_other.json()["task_id"]
                initial_a = {
                    "role_kind": "edit",
                    "requirements": {
                        "sources": [],
                        "requirements": [
                            {
                                "id": "req_old",
                                "purpose": "A parent purpose",
                                "ifc_structure": "pipe",
                                "modeling_method": "axis",
                                "tools": [],
                            }
                        ],
                    },
                    "clarifications": [
                        {
                            "id": "clar_parent",
                            "topic": "existing",
                            "question": "Should this remain?",
                            "defaultAnswer": "yes",
                            "notes": "",
                        }
                    ],
                    "previous_clarifications": None,
                    "requirement_md": "A parent MD",
                }
                initial_b = {
                    "role_kind": "read",
                    "requirements": {
                        "sources": [],
                        "requirements": [
                            {
                                "id": "req_old",
                                "purpose": "B parent purpose",
                                "ifc_structure": "pipe",
                                "modeling_method": "axis",
                                "tools": [],
                            }
                        ],
                    },
                    "clarifications": [],
                    "previous_clarifications": None,
                    "requirement_md": "B parent MD",
                }
                started_a = await public.post(
                    f"/tasks/{task_a_id}/runs",
                    headers=user_a,
                    json={"idempotency_key": f"start-{uuid4().hex}", "input_payload": initial_a},
                )
                started_b = await public.post(
                    f"/tasks/{task_b_id}/runs",
                    headers=user_b,
                    json={"idempotency_key": f"start-{uuid4().hex}", "input_payload": initial_b},
                )
                assert started_a.status_code == started_b.status_code == 201
                run_a_id = started_a.json()["run_id"]
                run_b_id = started_b.json()["run_id"]
                created_run_ids.extend((run_a_id, run_b_id))
                assert (
                    await public.post(
                        f"/tasks/{task_a_id}/runs/{run_a_id}/state-read-access",
                        headers=user_b,
                    )
                ).status_code == 404
                finished_a = await backend.runner.execute_next()
                assert finished_a is not None and finished_a.run_id == run_a_id
                assert finished_a.status.value == "completed"
                finished_b = await backend.runner.execute_next()
                assert finished_b is not None and finished_b.run_id == run_b_id
                assert finished_b.status.value == "completed"
                issued_a = await public.post(
                    f"/tasks/{task_a_id}/runs/{run_a_id}/state-read-access",
                    headers=user_a,
                )
                issued_b = await public.post(
                    f"/tasks/{task_b_id}/runs/{run_b_id}/state-read-access",
                    headers=user_b,
                )
                assert issued_a.status_code == issued_b.status_code == 200
                pair_a, pair_b = issued_a.json(), issued_b.json()
                for pair, finished in ((pair_a, finished_a), (pair_b, finished_b)):
                    cursor = backend.state_access_codec.verify_pair(
                        pair["access_token"], pair["read_binding"]
                    ).binding.checkpoint_cursor
                    assert cursor.thread_id == finished.thread_id
                    assert cursor.checkpoint_ns == ""
                    assert cursor.checkpoint_id == cursor.root_checkpoint_id
                assert (
                    await public.post(
                        f"/tasks/{task_b_id}/runs/{run_b_id}/state-read-access",
                        headers=user_a,
                    )
                ).status_code == 404
                assert (
                    await public.post(
                        f"/tasks/{task_a_other_id}/runs/{run_a_id}/state-read-access",
                        headers=user_a,
                    )
                ).status_code == 404

            def transport(access: dict[str, str], binding: dict[str, str] | None = None):
                return StreamableHttpTransport(
                    mcp_url,
                    headers={
                        "Authorization": f"Bearer {access['access_token']}",
                        "X-CAD2BIM-State-Binding": (binding or access)["read_binding"],
                    },
                )

            async with Client(transport(pair_a)) as client_a, Client(transport(pair_b)) as client_b:
                concurrent = await asyncio.gather(
                    client_a.read_resource("state://requirement-md"),
                    client_b.read_resource("state://requirement-md"),
                )
                assert [items[0].text for items in concurrent] == ["A parent MD", "B parent MD"]
                a_first = json.loads((await client_a.read_resource("state://requirements"))[0].text)
                b_first = json.loads((await client_b.read_resource("state://requirements"))[0].text)
                a_markdown = (await client_a.read_resource("state://requirement-md"))[0].text
                b_markdown = (await client_b.read_resource("state://requirement-md"))[0].text
                a_again = json.loads((await client_a.read_resource("state://requirements"))[0].text)
                b_again = json.loads((await client_b.read_resource("state://requirements"))[0].text)
                assert a_first == a_again and b_first == b_again
                assert a_first["requirements"][0]["purpose"] == "A child purpose"
                assert b_first["requirements"][0]["purpose"] == "B parent purpose"
                assert a_markdown == "A parent MD" and b_markdown == "B parent MD"
                assert {str(item.uri) for item in await client_a.list_resources()} == {
                    "state://requirements",
                    "state://requirement-md",
                    "state://clarifications",
                }
                assert json.loads((await client_a.read_resource("state://clarifications"))[0].text) == initial_a["clarifications"]
                assert {str(item.uri) for item in await client_b.list_resources()} == {
                    "state://requirements",
                    "state://clarifications",
                    "state://requirement-md",
                }

            terminal_a = await backend.graph_runtime.get_checkpoint_state(
                thread_id=finished_a.thread_id,
                checkpoint_id=backend.state_access_codec.verify_pair(
                    pair_a["access_token"], pair_a["read_binding"]
                ).binding.checkpoint_cursor.checkpoint_id,
            )
            terminal_b = await backend.graph_runtime.get_checkpoint_state(
                thread_id=finished_b.thread_id,
                checkpoint_id=backend.state_access_codec.verify_pair(
                    pair_b["access_token"], pair_b["read_binding"]
                ).binding.checkpoint_cursor.checkpoint_id,
            )
            assert terminal_a is not None and terminal_b is not None
            for terminal, pair in ((terminal_a, pair_a), (terminal_b, pair_b)):
                saved_text = json.dumps(terminal.values, default=str)
                assert pair["access_token"] not in saved_text
                assert pair["read_binding"] not in saved_text
            assert terminal_a.values["before_uris"] == [
                "state://clarifications",
                "state://requirement-md",
                "state://requirements",
            ]
            assert terminal_a.values["inspection"] == {
                "child_uris": ["state://clarifications", "state://requirement-md", "state://requirements"],
                "parent_old": True,
                "read_current": True,
            }
            assert terminal_b.values["inspection"]["no_body_copy"] is True
            assert terminal_b.values["requirement_md"] == "B parent MD"
            memory_a = terminal_a.values["role_memories"]["requirement:cad2bim_workflow"]
            memory_b = terminal_b.values["role_memories"]["requirement:cad2bim_workflow"]
            assert skill_text in memory_a["messages"][1]["content"]
            assert optional_skill_text in memory_a["messages"][3]["content"]
            assert "Changed on-demand instructions." in memory_a["messages"][5]["content"]
            assert skill_text in memory_b["messages"][1]["content"]
            assert len(memory_a["messages"]) == 6 and len(memory_b["messages"]) == 2
            (skill_root / "mcp-tool-usage" / "SKILL.md").write_text(
                "---\nname: mcp-tool-usage\ndescription: Current tool usage.\n---\nChanged required instructions.", encoding="utf-8")
            restored = enter_role(
                terminal_a.values,
                role="requirement",
                stage_key="cad2bim_workflow",
                business_entry_id="entry_restored",
                owned_fields=_EDIT_OWNED,
                business_schema=RequirementState,
            )
            assert skill_text in restored.child_state["messages"][1].content
            assert optional_skill_text in restored.child_state["messages"][3].content
            assert len(restored.child_state["messages"]) == 6
            other_stage = enter_role(
                terminal_a.values,
                role="requirement",
                stage_key="another_stage",
                business_entry_id="entry_other_stage",
                owned_fields=_EDIT_OWNED,
                business_schema=RequirementState,
            )
            assert other_stage.child_state["messages"] == []
            consumed_other_stage = await read_skill(other_stage.child_state, "mcp-tool-usage", "required")
            assert "Changed required instructions." in consumed_other_stage["messages"][-1].content
            assert other_stage.memory_slot != restored.memory_slot

            wrong_binding = pair_a["read_binding"][:-1] + (
                "A" if pair_a["read_binding"][-1] != "A" else "B"
            )
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{backend_port}") as probe:
                rejected = await probe.post(
                    "/internal/state/authorize",
                    headers={
                        "Authorization": f"Bearer {pair_a['access_token']}",
                        "X-CAD2BIM-State-Binding": wrong_binding,
                    },
                    json={},
                )
                exchanged = await probe.post(
                    "/internal/state/authorize",
                    headers={
                        "Authorization": f"Bearer {pair_a['access_token']}",
                        "X-CAD2BIM-State-Binding": pair_b["read_binding"],
                    },
                    json={},
                )
            assert rejected.status_code == 403
            assert exchanged.status_code == 403
            wrong_transport = StreamableHttpTransport(
                mcp_url,
                headers={
                    "Authorization": f"Bearer {pair_a['access_token']}",
                    "X-CAD2BIM-State-Binding": wrong_binding,
                },
            )
            async with Client(wrong_transport) as wrong_client:
                with pytest.raises(McpError):
                    await wrong_client.list_resources()
            for access, binding in ((pair_a, pair_b), (pair_b, pair_a)):
                async with Client(transport(access, binding)) as wrong_client:
                    with pytest.raises(McpError):
                        await wrong_client.list_resources()

            # The completed root and credentials above survive neither old
            # HTTP service's memory nor the original MCP process.
            first_mcp_pid = mcp_process.pid
            await _stop_test_process(mcp_process)
            print(f"batch4 original MCP stopped: pid={first_mcp_pid} exit={mcp_process.returncode}")
            mcp_process = None
            api_server.should_exit = True
            await api_task
            await backend.close()
            original_backend_closed = True

            readback_backend_port = _free_port()
            readback_mcp_port = _free_port()
            public_key_path = tmp_path / "readback-public-key.pem"
            public_key_path.write_bytes(public_pem)
            readback_env = dict(env)
            readback_env.update(
                {
                    "CAD2BIM_READBACK_API_URL": api_url.render_as_string(hide_password=False),
                    "CAD2BIM_READBACK_RUNNER_URL": runner_url.render_as_string(hide_password=False),
                    "CAD2BIM_READBACK_PUBLIC_KEY_PATH": str(public_key_path),
                    "CAD2BIM_READBACK_PORT": str(readback_backend_port),
                }
            )
            with (tmp_path / "readback-backend.log").open("w", encoding="utf-8") as output:
                readback_process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--readback-backend"],
                    cwd=Path(__file__).resolve().parents[3],
                    env=readback_env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            await _wait_tcp(readback_backend_port, process=readback_process)
            assert readback_process.pid != os.getpid()
            mcp_url = f"http://127.0.0.1:{readback_mcp_port}/mcp"
            readback_env.update(
                {
                    "CAD2BIM_RUNNER_STATE_BACKEND_URL":
                        f"http://127.0.0.1:{readback_backend_port}",
                    "CAD2BIM_RUNNER_MCP_PORT": str(readback_mcp_port),
                }
            )
            with (tmp_path / "runner-mcp-restarted.log").open("w", encoding="utf-8") as output:
                mcp_process = subprocess.Popen(
                    [sys.executable, "-m", "CAD2BIM_mcp.runner.server"],
                    cwd=Path(__file__).resolve().parents[3],
                    env=readback_env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            await _wait_tcp(readback_mcp_port, process=mcp_process)
            assert mcp_process.pid != first_mcp_pid
            assert mcp_process.pid != readback_process.pid
            print(
                "batch4 readback processes: "
                f"old_backend_pid={os.getpid()} new_backend_pid={readback_process.pid} "
                f"old_mcp_pid={first_mcp_pid} new_mcp_pid={mcp_process.pid} "
                f"task_a={task_a_id} run_a={run_a_id} "
                f"task_b={task_b_id} run_b={run_b_id}"
            )

            # One fresh MCP instance alternates old completed credentials;
            # the restarted Backend checks the current Run on every request.
            async with Client(transport(pair_a)) as restarted_a, Client(
                transport(pair_b)
            ) as restarted_b:
                reads = await asyncio.gather(
                    restarted_a.read_resource("state://requirement-md"),
                    restarted_b.read_resource("state://requirement-md"),
                )
                assert [items[0].text for items in reads] == ["A parent MD", "B parent MD"]
                a_values = json.loads(
                    (await restarted_a.read_resource("state://requirements"))[0].text
                )
                b_values = json.loads(
                    (await restarted_b.read_resource("state://requirements"))[0].text
                )
                assert a_values["requirements"][0]["purpose"] == "A child purpose"
                assert b_values["requirements"][0]["purpose"] == "B parent purpose"
                assert (await restarted_a.read_resource("state://requirement-md"))[0].text == (
                    "A parent MD"
                )
                assert (await restarted_b.read_resource("state://requirement-md"))[0].text == (
                    "B parent MD"
                )
            assert not restarted_a.is_connected() and not restarted_b.is_connected()

            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{readback_backend_port}", timeout=20
            ) as readback_http:
                assert (
                    await readback_http.post(
                        f"/tasks/{task_b_id}/runs/{run_b_id}/state-read-access",
                        headers=user_a,
                    )
                ).status_code == 404
                assert (
                    await readback_http.post(
                        f"/tasks/{task_a_other_id}/runs/{run_a_id}/state-read-access",
                        headers=user_a,
                    )
                ).status_code == 404
                assert (
                    await readback_http.post(
                        "/internal/state/authorize",
                        headers={
                            "Authorization": f"Bearer {pair_a['access_token']}",
                            "X-CAD2BIM-State-Binding": pair_b["read_binding"],
                        },
                        json={},
                    )
                ).status_code == 403

            async with Client(transport(pair_a, pair_b)) as crossed:
                with pytest.raises(McpError):
                    await crossed.list_resources()
            assert not crossed.is_connected()
            async with Client(transport(pair_b)) as still_b:
                assert (await still_b.read_resource("state://requirement-md"))[0].text == (
                    "B parent MD"
                )
            assert not still_b.is_connected()

            # A valid old signature stops authorizing immediately when the
            # current Run ceases to be completed; restoring it re-enables read.
            admin = create_database(Settings(database_url=database_url, environment="test"))
            try:
                async with admin.session_factory() as session, session.begin():
                    await session.execute(
                        text("UPDATE runs SET status = 'cancelled' WHERE run_id = :run_id"),
                        {"run_id": run_a_id},
                    )
                async with Client(transport(pair_a)) as revoked:
                    with pytest.raises(McpError):
                        await revoked.list_resources()
                assert not revoked.is_connected()
            finally:
                async with admin.session_factory() as session, session.begin():
                    await session.execute(
                        text("UPDATE runs SET status = 'completed' WHERE run_id = :run_id"),
                        {"run_id": run_a_id},
                    )
                await admin.dispose()
            async with Client(transport(pair_a)) as restored_a:
                assert (await restored_a.read_resource("state://requirement-md"))[0].text == (
                    "A parent MD"
                )
            assert not restored_a.is_connected()
            print("batch4 readback assertions: exact_root, isolation, dynamic_authorization, client_closed")
    finally:
        if mcp_process is not None:
            await _stop_test_process(mcp_process)
            print(f"batch4 restarted MCP stopped: pid={mcp_process.pid} exit={mcp_process.returncode}")
        if readback_process is not None:
            await _stop_test_process(readback_process)
            print(
                "batch4 restarted Backend stopped: "
                f"pid={readback_process.pid} exit={readback_process.returncode}"
            )
        if not original_backend_closed:
            api_server.should_exit = True
            await api_task
            await backend.close()
        if created_run_ids:
            admin = create_database(Settings(database_url=database_url, environment="test"))
            try:
                async with admin.session_factory() as session, session.begin():
                    await session.execute(
                        text(
                            "UPDATE runs SET status = 'cancelled', claimed_by = NULL, "
                            "lease_token = NULL, lease_expires_at = NULL "
                            "WHERE run_id = ANY(:ids) AND status IN ('queued', 'running')"
                        ),
                        {"ids": created_run_ids},
                    )
            finally:
                await admin.dispose()


if __name__ == "__main__":
    if sys.argv[1:] != ["--readback-backend"]:
        raise SystemExit("readback Backend fixture requires --readback-backend")
    asyncio.run(_serve_readback_backend())
