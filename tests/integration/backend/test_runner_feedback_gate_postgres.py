"""Exact terminal-root reentry through the production fenced Graph runtime."""

from __future__ import annotations

import asyncio
import base64
from contextvars import Context
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
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
from langchain_core.runnables.config import RunnableConfig
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from sqlalchemy import text
from sqlalchemy.engine import make_url

from CAD2BIM_backend.bootstrap import open_backend_runtime
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.control_plane import CreateTaskRequest, RunStatus, StartRunRequest
from CAD2BIM_backend.domain.control_plane import feedback_message_id
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.identity import AuthenticatedSubject
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant, RunnerGraphContext
from CAD2BIM_backend.infrastructure.langgraph.runtime import AsyncPostgresGraphRuntime
from CAD2BIM_backend.infrastructure.langgraph.feedback_nodes import FeedbackNodes
from CAD2BIM_backend.infrastructure.langgraph.user_gate import user_gate_node
from CAD2BIM_backend.infrastructure.postgres.database import create_database
from CAD2BIM_backend.infrastructure.postgres.identity_directory import PostgresIdentityDirectory
from CAD2BIM_backend.infrastructure.postgres.repository import PostgresControlPlaneRepository
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    bind_tenant_execution_context,
    set_tenant_execution_context,
)
from CAD2BIM_backend.domain.role_memory import parse_role_messages
from CAD2BIM_backend.application.role_state_handoff import accept_role_output, enter_role
from CAD2BIM_backend.application.requirement_state_actions import RequirementStateActions
from CAD2BIM_backend.infrastructure.agent_skills import AgentSkills
from CAD2BIM_backend.domain.requirement_state import (
    ChangeClarifications,
    ChangeRequirementMarkdown,
    InitializeRequirements,
    MarkdownChange,
    RequirementRowDraft,
    RequirementState,
    SavedRequirementCommand,
)
from CAD2BIM_backend.infrastructure.langgraph.node_read_binding import resolve_node_read_binding
from CAD2BIM_backend.infrastructure.markdown_editor import DeepAgentsMarkdownEditor
from CAD2BIM_backend.infrastructure.mcp_state_client import RunnerStateMcpClient


pytestmark = [pytest.mark.integration, pytest.mark.network]


class _State(TypedDict):
    content: str
    role_memories: dict[str, Any]
    business_entry_id: str
    reenter_requested: bool
    edit_count: int


class _GateState(TypedDict, total=False):
    requirements: Any
    requirement_md: str
    role_memories: dict[str, Any]
    role: str
    stage_key: str
    business_entry_id: str
    role_entry_index: int
    gate_target_role: str
    input_high_watermark: int
    clarifications: list[Any] | None
    messages: list[Any]
    feedback_batch_ids: tuple[str, ...]
    feedback_task_ids: tuple[str, ...]
    feedback_check_ids: tuple[str, ...]
    feedback_subject_id: str
    role_exit_route: str
    gate_route: str
    gate_reentry_reason: str
    reenter_requested: bool
    main_count: int
    content: str
    parent_before: dict[str, Any]
    edit_marker_ids: list[str]


def _feedback_bodies(state: _GateState) -> list[str]:
    """Test decisions consume actual messages, without a second copy of their bodies."""
    batch_ids = set(state.get("feedback_batch_ids", ()))
    return [
        message.text for message in parse_role_messages(state.get("messages", ()))
        if message.additional_kwargs.get("source") == "user_feedback"
        and message.additional_kwargs.get("feedback_batch_id") in batch_ids
    ]


def _binding() -> ExecutionProtocolBinding:
    return ExecutionProtocolBinding(
        bundle_id="feedback_gate_reentry",
        module="workflow",
        stage_key="cad2bim_workflow",
    )


async def _user_gate_with_role_entry(
    state: _GateState, config: RunnableConfig, runtime: Runtime[RunnerGraphContext],
) -> dict[str, Any]:
    update = await user_gate_node(state, config, runtime)
    if update.get("gate_route") == "reenter":
        update["role_entry_index"] = state["role_entry_index"] + 1
    return update


def _isolated_urls() -> tuple[str, str]:
    raw = os.environ["CAD2BIM_BATCH3_END_DATABASE_URL"]
    url = make_url(raw)
    if not (url.database or "").startswith("cad2bim_batch3_end_"):
        raise RuntimeError("END proof requires its own batch-3 database")
    if url.host not in {None, "127.0.0.1", "localhost"}:
        raise RuntimeError("END proof requires a local isolated PostgreSQL")
    return (
        url.set(username="cad2bim_api_login").render_as_string(hide_password=False),
        url.set(username="cad2bim_runner_login").render_as_string(hide_password=False),
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _wait_tcp(port: int, *, process: subprocess.Popen[str] | None = None) -> None:
    for _ in range(100):
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"isolated MCP process exited: {process.returncode}")
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise RuntimeError(f"isolated HTTP process on port {port} did not start")


def _batch4_graph_builder(
    saver: Any, feedback_nodes: FeedbackNodes, *, fault: str, audit_path: Path
) -> Any:
    """One formal child handoff and parent Gate, compiled anew in each PID."""

    skills = AgentSkills(audit_path.parent / "skills")

    def enter(state: _GateState) -> dict[str, Any]:
        entry = enter_role(
            state,
            role="requirement",
            stage_key="cad2bim_workflow",
            business_entry_id=state["business_entry_id"],
            owned_fields=("requirement_md",),
            business_schema=RequirementState,
        )
        return {
            **entry.child_state,
            "parent_before": {
                "requirement_md": state.get("requirement_md"),
                "role_memories": state.get("role_memories", {}),
            },
        }

    def main(state: _GateState) -> dict[str, Any]:
        edit_marker_id = f"edit_{state['business_entry_id']}"
        with audit_path.open("a", encoding="utf-8") as audit:
            audit.write(f"{os.getpid()} {edit_marker_id}\n")
            audit.flush()
        bodies = _feedback_bodies(state)
        return {
            "requirement_md": (
                (state.get("requirement_md") or "")
                + f"\nEdit: {edit_marker_id}"
                + "".join(f"\nFeedback: {body}" for body in bodies)
            ),
            "main_count": state.get("main_count", 0) + 1,
            "edit_marker_ids": [*state.get("edit_marker_ids", []), edit_marker_id],
        }

    async def read_required_skill(state: _GateState, runtime: Runtime[RunnerGraphContext],
                                  config: RunnableConfig) -> dict[str, Any]:
        await skills.discover()
        call_id = f"read-skill:{state['business_entry_id']}"
        call = {"name": "read_file", "args": {"file_path": "/batch4-skill/SKILL.md"}, "id": call_id}
        result = await skills.aread(call, state=state, config=config, runtime=runtime)
        assert isinstance(result, ToolMessage)
        result = result.model_copy(update={"id": f"result:{call_id}"})
        return {"messages": [*state.get("messages", ()),
            AIMessage(id=f"call:{call_id}", content="", tool_calls=[call]), result]}

    def accept(state: _GateState) -> dict[str, Any]:
        if fault == "before_parent_accept":
            raise RuntimeError("batch4 fault after child save before parent accept")
        return accept_role_output(
            state["parent_before"],
            state,
            role="requirement",
            stage_key="cad2bim_workflow",
            owned_fields=("requirement_md",),
            business_schema=RequirementState,
        )

    role = StateGraph(_GateState, context_schema=RunnerGraphContext)
    role.add_node("read_required_skill", read_required_skill)
    role.add_node("prepare_main", feedback_nodes.prepare_main)
    role.add_node("main", main)
    role.add_node("decide_role_exit", feedback_nodes.decide_role_exit)
    role.add_edge(START, "read_required_skill")
    role.add_edge("read_required_skill", "prepare_main")
    role.add_edge("prepare_main", "main")
    role.add_edge("main", "decide_role_exit")
    role.add_conditional_edges(
        "decide_role_exit",
        lambda state: state["role_exit_route"],
        {"main": "prepare_main", "parent_gate": END},
    )

    parent = StateGraph(_GateState, context_schema=RunnerGraphContext)
    parent.add_node("enter_role", enter)
    parent.add_node("role_subgraph", role.compile())
    parent.add_node("accept_role", accept)
    parent.add_node("user_gate", _user_gate_with_role_entry)
    parent.add_node("user_gate_route", lambda _state: {})
    parent.add_edge(START, "enter_role")
    parent.add_edge("enter_role", "role_subgraph")
    parent.add_edge("role_subgraph", "accept_role")
    parent.add_edge("accept_role", "user_gate")
    parent.add_edge("user_gate", "user_gate_route")
    parent.add_conditional_edges(
        "user_gate_route",
        lambda state: "reenter" if state.get("reenter_requested") else "done",
        {"reenter": "enter_role", "done": END},
    )
    return parent.compile(checkpointer=saver)


async def _batch4_run_one_process() -> int:
    mode = os.environ["CAD2BIM_BATCH4_WORKER_MODE"]
    result_path = Path(os.environ["CAD2BIM_BATCH4_WORKER_RESULT"])
    audit_path = Path(os.environ["CAD2BIM_BATCH4_WORKER_AUDIT"])
    api_url, runner_url = _isolated_urls()
    backend = await open_backend_runtime(
        api_settings=Settings(database_url=api_url, environment="test"),
        runner_settings=Settings(database_url=runner_url, environment="test"),
        public_key_pem=base64.b64decode(os.environ["CAD2BIM_BATCH4_PUBLIC_PEM_B64"]),
        issuer="https://batch4-restart.example",
        audience="cad2bim-batch4",
        execution_protocol=_binding(),
        graph_builder=lambda saver, nodes: _batch4_graph_builder(
            saver, nodes, fault=mode, audit_path=audit_path
        ),
        runner_id=f"batch4-runner-{os.getpid()}",
        state_access_secret=b"batch4-restart-test-state-access-secret-32-bytes",
    )
    backend.runner._lease_seconds = 1
    backend.runner._heartbeat_interval_seconds = 0.2
    observed: dict[str, Any] = {"pid": os.getpid(), "phase": mode}
    feedback_service = backend.runner._feedback_service
    original_load = feedback_service.load_gate_resume

    async def observe_gate_resume(grant: RunnerCheckpointGrant) -> Any:
        plan = await original_load(grant)
        if plan is not None:
            observed["gate_source_checkpoint_id"] = plan.source_checkpoint_cursor.checkpoint_id
            observed["gate_operation_id"] = plan.operation_id
            observed["gate_batch_id"] = (
                plan.frozen_batch.batch.batch_id if plan.frozen_batch is not None else None
            )
        return plan

    feedback_service.load_gate_resume = observe_gate_resume
    if mode == "before_gate_resume":
        async def stop_before_resume(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("batch4 fault after saved Gate selection before Graph resume")

        backend.graph_runtime.resume_durable = stop_before_resume
    try:
        run = await backend.runner.execute_next()
        assert run is not None
        observed.update(
            run_id=run.run_id,
            thread_id=run.thread_id,
            status=run.status.value,
            writer_generation=run.writer_generation,
        )
    except RuntimeError as exc:
        observed["fault"] = str(exc)
        result_path.write_text(json.dumps(observed), encoding="utf-8")
        os._exit(73)
    else:
        await backend.close()
        result_path.write_text(json.dumps(observed), encoding="utf-8")
        return 0


def _batch4_worker_entry() -> None:
    raise SystemExit(asyncio.run(_batch4_run_one_process()))


class _FormalState(_GateState, total=False):
    previous_clarifications: list[Any] | None
    saved_command: dict[str, Any] | None
    action_result: dict[str, Any]
    action_history: list[dict[str, Any]]
    applied_feedback_batches: list[str]
    inspection_history: list[dict[str, Any]]


def _formal_graph_builder(
    saver: Any,
    feedback_nodes: FeedbackNodes,
    *,
    backend_holder: dict[str, Any],
    mcp_holder: dict[str, RunnerStateMcpClient],
    structure_started: asyncio.Event,
    release_structure: asyncio.Event,
    fault_after_structure: dict[str, bool],
) -> Any:
    """One normal Run: saved decisions, formal actions, exact MCP reads, and Gate."""

    owned = ("requirements", "clarifications", "previous_clarifications", "requirement_md")
    actions = RequirementStateActions(markdown_editor=DeepAgentsMarkdownEditor())

    def enter(state: _FormalState) -> dict[str, Any]:
        entry = enter_role(
            state,
            role="requirement",
            stage_key="cad2bim_workflow",
            business_entry_id=state["business_entry_id"],
            owned_fields=owned,
            business_schema=RequirementState,
        )
        return {
            **entry.child_state,
            "saved_command": None,
            "parent_before": {
                **{field: state.get(field) for field in owned},
                "role_memories": state.get("role_memories", {}),
            },
        }

    def decide_core(state: _FormalState) -> dict[str, Any]:
        # The predecessor apply result is already in a fenced child checkpoint.
        if state.get("requirements") is not None and fault_after_structure["armed"]:
            fault_after_structure["armed"] = False
            raise RuntimeError("formal fault after saved structure command and result")
        if state.get("requirements") is None:
            action = InitializeRequirements(
                sources=["formal user input"],
                rows=[RequirementRowDraft(
                    purpose="Keep a formal row",
                    ifc_structure="pipe",
                    modeling_method="axis",
                    tools=["MCP"],
                )],
            )
        elif state.get("clarifications") is None:
            action = ChangeClarifications(assessed=True, changes=[])
        elif state.get("requirement_md") is None:
            action = ChangeRequirementMarkdown(changes=[MarkdownChange(
                operation="initialize",
                initial_content="Formal requirements\n<!-- feedback-end -->",
            )])
        else:
            return {"saved_command": None}
        subject_id = state["feedback_subject_id"]
        command = SavedRequirementCommand(
            command_id=f"cmd_{uuid4().hex}",
            role_execution_id=subject_id,
            action=action,
        )
        return {"saved_command": command.model_dump(mode="json")}

    async def apply_saved(state: _FormalState) -> dict[str, Any]:
        command = SavedRequirementCommand.model_validate(state["saved_command"])
        assert command.role_execution_id == state["feedback_subject_id"]
        if isinstance(command.action, InitializeRequirements) and not structure_started.is_set():
            structure_started.set()
            await asyncio.wait_for(release_structure.wait(), timeout=15)
        result = actions.apply(
            RequirementState.from_checkpoint_values(state),
            command,
            role_execution_id=state["feedback_subject_id"],
            owned_fields=owned,
        )
        record = {
            "command_id": command.command_id,
            "role_execution_id": command.role_execution_id,
            "kind": command.action.kind,
            "created_item_ids": list(result.created_item_ids),
            "replacements": result.replacements,
        }
        return {
            **result.updates,
            "action_result": record,
            "action_history": [*state.get("action_history", []), record],
        }

    async def safe_prepare(
        state: _FormalState, config: RunnableConfig, runtime: Runtime[RunnerGraphContext]
    ) -> dict[str, Any]:
        if state.get("feedback_batch_ids"):
            return {}
        return await feedback_nodes.prepare_main(state, config, runtime)

    def decide_feedback(state: _FormalState) -> dict[str, Any]:
        batch_ids = tuple(state.get("feedback_batch_ids", ()))
        if not batch_ids or set(batch_ids) <= set(state.get("applied_feedback_batches", [])):
            return {"saved_command": None}
        bodies = _feedback_bodies(state)
        assert bodies
        content = "".join(f"Feedback: {body}\n" for body in bodies)
        command = SavedRequirementCommand(
            command_id=f"cmd_{uuid4().hex}",
            role_execution_id=state["feedback_subject_id"],
            action=ChangeRequirementMarkdown(changes=[MarkdownChange(
                operation="replace_fragment",
                old_string="<!-- feedback-end -->",
                new_string=content + "<!-- feedback-end -->",
            )]),
        )
        return {"saved_command": command.model_dump(mode="json")}

    async def apply_feedback(state: _FormalState) -> dict[str, Any]:
        result = await apply_saved(state)
        batch_ids = state["feedback_batch_ids"]
        return {
            **result,
            "applied_feedback_batches": [
                *state.get("applied_feedback_batches", []), *batch_ids
            ],
        }

    async def inspect_saved(
        state: _FormalState, runtime: Runtime[RunnerGraphContext], config: RunnableConfig
    ) -> dict[str, Any]:
        backend = backend_holder["runtime"]
        node_config = config.get("configurable", {})
        print(
            "formal inspect namespace=",
            node_config.get("checkpoint_ns"),
            "map_namespaces=",
            sorted(node_config.get("checkpoint_map", {})),
        )
        scope = runtime.context.scope
        repository = PostgresControlPlaneRepository(backend.runner_database.session_factory)
        run = await repository.get_run(scope.owner_id, scope.run_id)
        assert run is not None
        grant = backend.graph_runtime.require_current_execution_grant()
        binding = await resolve_node_read_binding(
            run=run,
            grant=grant,
            node_config=config,
            registered_graph_paths=("", "role_subgraph"),
            field_owner_paths={
                "requirements": "role_subgraph",
                "clarifications": "role_subgraph",
                "requirement_md": "role_subgraph",
            },
            graph_runtime=backend.graph_runtime,
        )
        async with mcp_holder["client"].open_read_batch(grant=grant, binding=binding) as batch:
            uris = {str(item.uri) for item in await batch.list_resources()}
            rows = json.loads(await batch.read_text("state://requirements"))
            clarifications = json.loads(await batch.read_text("state://clarifications"))
            markdown = await batch.read_text("state://requirement-md")
        assert uris == {
            "state://requirements", "state://clarifications", "state://requirement-md"
        }
        assert rows == state["requirements"]
        assert clarifications == state["clarifications"] == []
        assert markdown == state["requirement_md"]
        record = {
            "business_entry_id": state["business_entry_id"],
            "checkpoint_id": binding.checkpoint_cursor.checkpoint_id,
            "row_ids": [item["id"] for item in rows["requirements"]],
            "clarifications": clarifications,
            "markdown": markdown,
        }
        return {"inspection_history": [*state.get("inspection_history", []), record]}

    def accept(state: _FormalState) -> dict[str, Any]:
        return accept_role_output(
            state["parent_before"], state, role="requirement",
            stage_key="cad2bim_workflow", owned_fields=owned,
            business_schema=RequirementState,
        )

    role = StateGraph(_FormalState, context_schema=RunnerGraphContext)
    for name, node in (
        ("prepare_main", feedback_nodes.prepare_main),
        ("decide_core", decide_core),
        ("apply_core", apply_saved),
        ("safe_prepare", safe_prepare),
        ("decide_feedback", decide_feedback),
        ("apply_feedback", apply_feedback),
        ("inspect_saved", inspect_saved),
        ("decide_role_exit", feedback_nodes.decide_role_exit),
    ):
        role.add_node(name, node)
    role.add_edge(START, "prepare_main")
    role.add_edge("prepare_main", "decide_core")
    role.add_conditional_edges(
        "decide_core", lambda state: "apply" if state.get("saved_command") else "safe",
        {"apply": "apply_core", "safe": "safe_prepare"},
    )
    role.add_edge("apply_core", "decide_core")
    role.add_edge("safe_prepare", "decide_feedback")
    role.add_conditional_edges(
        "decide_feedback", lambda state: "apply" if state.get("saved_command") else "inspect",
        {"apply": "apply_feedback", "inspect": "inspect_saved"},
    )
    role.add_edge("apply_feedback", "inspect_saved")
    role.add_edge("inspect_saved", "decide_role_exit")
    role.add_conditional_edges(
        "decide_role_exit", lambda state: state["role_exit_route"],
        {"main": "prepare_main", "parent_gate": END},
    )

    parent = StateGraph(_FormalState, context_schema=RunnerGraphContext)
    for name, node in (
        ("enter_role", enter),
        ("role_subgraph", role.compile()),
        ("accept_role", accept),
        ("user_gate", _user_gate_with_role_entry),
        ("user_gate_route", lambda _state: {}),
    ):
        parent.add_node(name, node)
    parent.add_edge(START, "enter_role")
    parent.add_edge("enter_role", "role_subgraph")
    parent.add_edge("role_subgraph", "accept_role")
    parent.add_edge("accept_role", "user_gate")
    parent.add_edge("user_gate", "user_gate_route")
    parent.add_conditional_edges(
        "user_gate_route",
        lambda state: "reenter" if state.get("reenter_requested") else "done",
        {"reenter": "enter_role", "done": END},
    )
    return parent.compile(checkpointer=saver)


@pytest.mark.asyncio
async def test_terminal_root_exact_reentry_survives_update_before_continuation() -> None:
    api_url, runner_url = _isolated_urls()
    api_database = create_database(Settings(database_url=api_url, environment="test"))
    runner_database = create_database(Settings(database_url=runner_url, environment="test"))
    api_repository = PostgresControlPlaneRepository(api_database.session_factory)
    runner_repository = PostgresControlPlaneRepository(runner_database.session_factory)
    identity_directory = PostgresIdentityDirectory(api_database.session_factory)
    run_id: str | None = None
    runtime: AsyncPostgresGraphRuntime | None = None
    main_calls: list[str] = []

    def graph_builder(saver: Any) -> Any:
        async def main(state: _State, runtime: Runtime[RunnerGraphContext]) -> dict[str, Any]:
            assert runtime.context.scope.run_id == run_id
            main_calls.append(state["business_entry_id"])
            return {
                "content": state["content"] + "|main",
                "edit_count": state["edit_count"] + 1,
                "reenter_requested": False,
            }

        def route(_state: _State) -> dict[str, Any]:
            return {}

        graph = StateGraph(_State, context_schema=RunnerGraphContext)
        graph.add_node("main", main)
        graph.add_node("route", route)
        graph.add_edge(START, "main")
        graph.add_edge("main", "route")
        graph.add_conditional_edges(
            "route", lambda state: "main" if state["reenter_requested"] else END
        )
        return graph.compile(checkpointer=saver)

    try:
        now = datetime.now(timezone.utc)
        identity = await identity_directory.resolve_authenticated_subject(
            AuthenticatedSubject(
                issuer="https://batch3-end-test.example",
                subject=f"person-{uuid4().hex}",
                audience="cad2bim-batch3",
                authenticated_at=now,
                token_expires_at=now + timedelta(hours=1),
            ),
            command_id=f"batch3_{uuid4().hex}",
        )
        with bind_tenant_execution_context(identity):
            task = await api_repository.create_task(
                identity.tenant_id, CreateTaskRequest(title="Terminal exact reentry")
            )
            run = await api_repository.create_run(
                identity.tenant_id,
                task.task_id,
                StartRunRequest(idempotency_key=f"run-{uuid4().hex}"),
                execution_protocol_factory=_binding,
            )
        run_id = run.run_id
        claimed = await runner_repository.claim_next_run("batch3-end-runner")
        assert claimed is not None and claimed.run_id == run_id
        grant = RunnerCheckpointGrant.from_claim(claimed, runner_id="batch3-end-runner")
        runtime = await AsyncPostgresGraphRuntime.open(
            database_url=runner_url,
            graph_builder=graph_builder,
            execution_protocol=_binding(),
        )
        initial = await runtime.start_durable(
            grant=grant,
            thread_id=claimed.thread_id,
            operation_id=f"run:{run_id}:start",
            target_run_id=run_id,
            input_value={
                "content": "accepted-result",
                "role_memories": {"role:stage": {"messages": ["kept"]}},
                "business_entry_id": "entry-one",
                "reenter_requested": False,
                "edit_count": 0,
            },
        )
        assert initial.checkpoint.checkpoint_ns == ""
        assert initial.checkpoint.next_nodes == () and initial.interrupts == ()
        assert initial.values["content"] == "accepted-result|main"
        source_id = initial.checkpoint.checkpoint_id
        stable_operation = f"run:{run_id}:gate:gate-one:batch:batch-one:source:{source_id}"

        # Simulate a crash after the route update commits and before Main continues.
        routed = await runtime.checkpoint_state_durable(
            grant=grant,
            thread_id=claimed.thread_id,
            source_checkpoint_id=source_id,
            operation_id=stable_operation,
            target_run_id=run_id,
            values={"business_entry_id": "entry-two", "reenter_requested": True},
            as_node="route",
        )
        assert routed.checkpoint_id != source_id
        assert routed.next_nodes == ("main",)
        await runtime.close()
        runtime = await AsyncPostgresGraphRuntime.open(
            database_url=runner_url,
            graph_builder=graph_builder,
            execution_protocol=_binding(),
        )
        resumed = await runtime.reconcile_durable_resume(
            grant=grant,
            thread_id=claimed.thread_id,
            source_checkpoint_id=source_id,
            operation_id=stable_operation,
            target_run_id=run_id,
            continue_pending=True,
        )
        assert resumed is not None
        assert resumed.checkpoint.checkpoint_ns == ""
        assert resumed.checkpoint.next_nodes == () and resumed.interrupts == ()
        assert resumed.values["content"] == "accepted-result|main|main"
        assert resumed.values["edit_count"] == 2
        assert resumed.values["role_memories"] == {"role:stage": {"messages": ["kept"]}}
        assert resumed.values["business_entry_id"] == "entry-two"
        assert main_calls == ["entry-one", "entry-two"]

        repeated = await runtime.reconcile_durable_resume(
            grant=grant,
            thread_id=claimed.thread_id,
            source_checkpoint_id=source_id,
            operation_id=stable_operation,
            target_run_id=run_id,
            continue_pending=True,
        )
        assert repeated is not None
        assert repeated.checkpoint.checkpoint_id == resumed.checkpoint.checkpoint_id
        assert repeated.values == resumed.values
        assert main_calls == ["entry-one", "entry-two"]
        assert claimed.lease_token is not None
        with bind_tenant_execution_context(grant.scope.identity):
            completed = await runner_repository.complete(
                run_id, "batch3-end-runner", claimed.lease_token
            )
        with bind_tenant_execution_context(identity):
            stored_run = await api_repository.get_run(identity.tenant_id, run_id)
        assert stored_run is not None
        assert completed.status is RunStatus.COMPLETED
        assert stored_run.status is RunStatus.COMPLETED
        assert stored_run.run_id == run_id
        assert stored_run.thread_id == claimed.thread_id
        assert stored_run.task_id == task.task_id
    finally:
        if runtime is not None:
            await runtime.close()
        await runner_database.dispose()
        await api_database.dispose()


@pytest.mark.asyncio
async def test_authenticated_feedback_reenters_same_run_and_gate_then_reads_final_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_url, runner_url = _isolated_urls()
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = signing_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    main_entries: list[str] = []

    def graph_builder(saver: Any, feedback_nodes: FeedbackNodes) -> Any:
        async def main(state: _GateState) -> dict[str, Any]:
            main_entries.append(state["business_entry_id"])
            bodies = _feedback_bodies(state)
            number = state.get("main_count", 0) + 1
            return {
                "main_count": number,
                "requirement_md": (
                    state.get("requirement_md", "")
                    + f"\nMain {number}"
                    + "".join(f"\nFeedback: {body}" for body in bodies)
                ),
            }

        role = StateGraph(_GateState, context_schema=RunnerGraphContext)
        role.add_node("prepare_main", feedback_nodes.prepare_main)
        role.add_node("main", main)
        role.add_node("decide_role_exit", feedback_nodes.decide_role_exit)
        role.add_edge(START, "prepare_main")
        role.add_edge("prepare_main", "main")
        role.add_edge("main", "decide_role_exit")
        role.add_conditional_edges(
            "decide_role_exit",
            lambda state: state["role_exit_route"],
            {"main": "prepare_main", "parent_gate": END},
        )

        parent = StateGraph(_GateState, context_schema=RunnerGraphContext)
        parent.add_node("role_subgraph", role.compile())
        parent.add_node("user_gate", _user_gate_with_role_entry)
        parent.add_node("user_gate_route", lambda _state: {})
        parent.add_edge(START, "role_subgraph")
        parent.add_edge("role_subgraph", "user_gate")
        parent.add_edge("user_gate", "user_gate_route")
        parent.add_conditional_edges(
            "user_gate_route",
            lambda state: "reenter" if state.get("reenter_requested") else "done",
            {"reenter": "role_subgraph", "done": END},
        )
        return parent.compile(checkpointer=saver)

    backend = await open_backend_runtime(
        api_settings=Settings(database_url=api_url, environment="test"),
        runner_settings=Settings(database_url=runner_url, environment="test"),
        public_key_pem=public_pem,
        issuer="https://batch3-gate-test.example",
        audience="cad2bim-batch3",
        execution_protocol=_binding(),
        graph_builder=graph_builder,
        runner_id=f"batch3-gate-{uuid4().hex[:8]}",
        state_access_secret=b"batch3-gate-test-state-access-secret-32-bytes",
    )
    token = jwt.encode(
        {
            "iss": "https://batch3-gate-test.example",
            "aud": "cad2bim-batch3",
            "sub": f"person-{uuid4().hex}",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        signing_key,
        algorithm="RS256",
    )
    headers = {"Authorization": f"Bearer {token}"}
    api_server: uvicorn.Server | None = None
    api_task: asyncio.Task[None] | None = None
    mcp_process: subprocess.Popen[str] | None = None
    successor = None
    try:
        for database, expected in (
            (backend.api_database, "cad2bim_api_login"),
            (backend.runner_database, "cad2bim_runner_login"),
        ):
            async with database.engine.connect() as connection:
                row = (await connection.exec_driver_sql(
                    "SELECT current_user, rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname=current_user"
                )).one()
                assert tuple(row) == (expected, False, False)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend.api),
            base_url="http://batch3-test.local",
        ) as client:
            task_response = await client.post(
                "/tasks", headers=headers, json={"title": "Feedback and Gate chain"}
            )
            assert task_response.status_code == 201, task_response.text
            task_id = task_response.json()["task_id"]
            run_response = await client.post(
                f"/tasks/{task_id}/runs",
                headers=headers,
                json={
                    "idempotency_key": f"run-{uuid4().hex}",
                    "input_payload": {
                        "role": "requirement",
                        "stage_key": "cad2bim_workflow",
                        "business_entry_id": "first-entry",
                        "role_entry_index": 1,
                        "gate_target_role": "requirement",
                        "input_high_watermark": 0,
                        "clarifications": [],
                        "requirements": {"sources": [], "requirements": []},
                        "requirement_md": "Accepted result",
                        "messages": [],
                        "main_count": 0,
                    },
                },
            )
            assert run_response.status_code == 201, run_response.text
            run_id = run_response.json()["run_id"]
            path = f"/tasks/{task_id}/runs/{run_id}"

            async def current_gate() -> str:
                response = await client.get(f"{path}/gates/current", headers=headers)
                assert response.status_code == 200, response.text
                return response.json()["interrupt_id"]

            await backend.runner.execute_next()
            waiting = await client.get(path, headers=headers)
            assert waiting.status_code == 200
            assert waiting.json()["status"] == "waiting_user"
            first_gate = await current_gate()

            first_feedback = await client.post(
                f"{path}/feedback",
                headers=headers,
                json={
                    "client_request_id": "feedback-one",
                    "delivery_mode": "steer",
                    "content": [{"type": "text", "text": "Keep the first complete feedback body."}],
                },
            )
            assert first_feedback.status_code == 201, first_feedback.text
            repeated = await client.post(
                f"{path}/feedback",
                headers=headers,
                json={
                    "client_request_id": "feedback-one",
                    "delivery_mode": "steer",
                    "content": [{"type": "text", "text": "Keep the first complete feedback body."}],
                },
            )
            assert repeated.status_code == 201
            assert repeated.json() == first_feedback.json()
            await backend.runner.execute_next()
            second_gate = await current_gate()
            assert second_gate != first_gate
            old_response = await client.post(
                f"{path}/gates/{first_gate}/respond",
                headers=headers,
                json={"action": "approve", "idempotency_key": "old-gate"},
            )
            assert old_response.status_code == 409

            response_with_text = await client.post(
                f"{path}/gates/{second_gate}/respond",
                headers=headers,
                json={
                    "action": "approve",
                    "content": [{"type": "text", "text": "Keep the second complete feedback body."}],
                    "idempotency_key": "response-two",
                },
            )
            assert response_with_text.status_code == 200, response_with_text.text
            await backend.runner.execute_next()
            third_gate = await current_gate()
            assert third_gate not in {first_gate, second_gate}

            approve = await client.post(
                f"{path}/gates/{third_gate}/respond",
                headers=headers,
                json={"action": "approve", "idempotency_key": "response-three"},
            )
            assert approve.status_code == 200, approve.text

            backend.runner._lease_seconds = 1
            backend.runner._heartbeat_interval_seconds = 0.2
            original_complete = backend.runner._repository.complete
            original_reconcile = backend.graph_runtime.reconcile_durable_resume
            raced_once = False
            route_fault_armed = True
            saved_terminal_route: tuple[str, str] | None = None

            async def complete_after_feedback(*args: Any, **kwargs: Any) -> Any:
                nonlocal raced_once
                if not raced_once:
                    raced_once = True
                    accepted = await asyncio.create_task(
                        client.post(
                            f"{path}/feedback",
                            headers=headers,
                            json={
                                "client_request_id": "feedback-before-complete",
                                "delivery_mode": "steer",
                                "content": [{"type": "text", "text": "Keep feedback accepted before completion."}],
                            },
                        ),
                        context=Context(),
                    )
                    assert accepted.status_code == 201, accepted.text
                return await original_complete(*args, **kwargs)

            monkeypatch.setattr(backend.runner._repository, "complete", complete_after_feedback)

            async def stop_after_terminal_route(*args: Any, **kwargs: Any) -> Any:
                nonlocal route_fault_armed, saved_terminal_route
                if route_fault_armed and kwargs.get("operation_id", "").startswith(
                    "gate_reentry_"
                ):
                    route_fault_armed = False
                    saved_terminal_route = (
                        kwargs["source_checkpoint_id"],
                        kwargs["operation_id"],
                    )
                    raise RuntimeError("fault after terminal route checkpoint save")
                return await original_reconcile(*args, **kwargs)

            monkeypatch.setattr(
                backend.graph_runtime,
                "reconcile_durable_resume",
                stop_after_terminal_route,
            )
            with pytest.raises(RuntimeError, match="fault after terminal route"):
                await backend.runner.execute_next()
            assert raced_once
            assert not route_fault_armed
            assert saved_terminal_route is not None
            later_feedback = await client.post(
                f"{path}/feedback",
                headers=headers,
                json={
                    "client_request_id": "feedback-after-route-save",
                    "delivery_mode": "steer",
                    "content": [{"type": "text", "text": "Keep feedback accepted after the route save."}],
                },
            )
            assert later_feedback.status_code == 201, later_feedback.text
            await asyncio.sleep(1.2)
            successor = await open_backend_runtime(
                api_settings=Settings(database_url=api_url, environment="test"),
                runner_settings=Settings(database_url=runner_url, environment="test"),
                public_key_pem=public_pem,
                issuer="https://batch3-gate-test.example",
                audience="cad2bim-batch3",
                execution_protocol=_binding(),
                graph_builder=graph_builder,
                runner_id=f"batch3-gate-successor-{uuid4().hex[:8]}",
                state_access_secret=b"batch3-gate-test-state-access-secret-32-bytes",
            )
            successor_reconcile = successor.graph_runtime.reconcile_durable_resume
            recovered_operations: list[tuple[str, str]] = []

            async def observe_recovery(*args: Any, **kwargs: Any) -> Any:
                recovered_operations.append(
                    (kwargs["source_checkpoint_id"], kwargs["operation_id"])
                )
                return await successor_reconcile(*args, **kwargs)

            monkeypatch.setattr(
                successor.graph_runtime,
                "reconcile_durable_resume",
                observe_recovery,
            )
            await successor.runner.execute_next()
            assert saved_terminal_route in recovered_operations
            fourth_gate = await current_gate()
            assert fourth_gate not in {first_gate, second_gate, third_gate}
            final_approve = await client.post(
                f"{path}/gates/{fourth_gate}/respond",
                headers=headers,
                json={"action": "approve", "idempotency_key": "response-four"},
            )
            assert final_approve.status_code == 200, final_approve.text
            await successor.runner.execute_next()
            completed = await client.get(path, headers=headers)
            assert completed.json()["status"] == "completed"
            assert len(main_entries) >= 4
            assert len(set(main_entries)) == 4

            access = await client.post(f"{path}/state-read-access", headers=headers)
            assert access.status_code == 200, access.text
            credential = access.json()
            markdown = await client.post(
                "/internal/state/resources/read",
                headers={
                    "Authorization": f"Bearer {credential['access_token']}",
                    "x-cad2bim-state-binding": credential["read_binding"],
                },
                json={"uri": "state://requirement-md"},
            )
            assert markdown.status_code == 200, markdown.text
            text = markdown.json()["text"]
            assert "Keep the first complete feedback body." in text
            assert "Keep the second complete feedback body." in text
            assert "Keep feedback accepted before completion." in text
            assert "Keep feedback accepted after the route save." in text
            assert text.count("Feedback:") == 4

            backend_port, mcp_port = _free_port(), _free_port()
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
            await _wait_tcp(backend_port)
            mcp_env = dict(os.environ)
            mcp_env.update(
                {
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "CAD2BIM_STATE_ACCESS_SECRET_B64": base64.b64encode(
                        b"batch3-gate-test-state-access-secret-32-bytes"
                    ).decode("ascii"),
                    "CAD2BIM_RUNNER_STATE_BACKEND_URL": f"http://127.0.0.1:{backend_port}",
                    "CAD2BIM_RUNNER_MCP_PORT": str(mcp_port),
                }
            )
            with (tmp_path / "runner-mcp.log").open("w", encoding="utf-8") as output:
                mcp_process = subprocess.Popen(
                    [sys.executable, "-m", "CAD2BIM_mcp.runner.server"],
                    cwd=Path(__file__).resolve().parents[3],
                    env=mcp_env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            await _wait_tcp(mcp_port, process=mcp_process)
            transport = StreamableHttpTransport(
                f"http://127.0.0.1:{mcp_port}/mcp",
                headers={
                    "Authorization": f"Bearer {credential['access_token']}",
                    "X-CAD2BIM-State-Binding": credential["read_binding"],
                },
            )
            async with Client(transport) as mcp:
                final_md = (await mcp.read_resource("state://requirement-md"))[0].text
                assert final_md == text

            cancel_task = await client.post(
                "/tasks", headers=headers, json={"title": "Gate cancellation"}
            )
            assert cancel_task.status_code == 201, cancel_task.text
            cancel_task_id = cancel_task.json()["task_id"]
            cancel_run = await client.post(
                f"/tasks/{cancel_task_id}/runs",
                headers=headers,
                json={
                    "idempotency_key": f"run-{uuid4().hex}",
                    "input_payload": {
                        "role": "requirement",
                        "stage_key": "cad2bim_workflow",
                        "business_entry_id": "cancel-entry",
                        "role_entry_index": 1,
                        "gate_target_role": "requirement",
                        "input_high_watermark": 0,
                        "clarifications": [],
                        "requirements": {"sources": [], "requirements": []},
                        "requirement_md": "Result awaiting cancellation",
                        "messages": [],
                        "main_count": 0,
                    },
                },
            )
            assert cancel_run.status_code == 201, cancel_run.text
            cancel_path = f"/tasks/{cancel_task_id}/runs/{cancel_run.json()['run_id']}"
            await backend.runner.execute_next()
            cancel_gate = await client.get(f"{cancel_path}/gates/current", headers=headers)
            assert cancel_gate.status_code == 200, cancel_gate.text
            cancel_gate_id = cancel_gate.json()["interrupt_id"]
            cancel_reply = await client.post(
                f"{cancel_path}/gates/{cancel_gate_id}/respond",
                headers=headers,
                json={"action": "cancel", "idempotency_key": "cancel-once"},
            )
            assert cancel_reply.status_code == 200, cancel_reply.text
            cancel_retry = await client.post(
                f"{cancel_path}/gates/{cancel_gate_id}/respond",
                headers=headers,
                json={"action": "cancel", "idempotency_key": "cancel-once"},
            )
            assert cancel_retry.json() == cancel_reply.json()
            changed_retry = await client.post(
                f"{cancel_path}/gates/{cancel_gate_id}/respond",
                headers=headers,
                json={"action": "approve", "idempotency_key": "new-key"},
            )
            assert changed_retry.status_code == 409
            await backend.runner.execute_next()
            cancelled = await client.get(cancel_path, headers=headers)
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
    finally:
        if mcp_process is not None:
            mcp_process.terminate()
            try:
                await asyncio.to_thread(mcp_process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                mcp_process.kill()
                await asyncio.to_thread(mcp_process.wait, timeout=5)
        if api_server is not None and api_task is not None:
            api_server.should_exit = True
            await api_task
        if successor is not None:
            await successor.close()
        await backend.close()


@pytest.mark.asyncio
async def test_fresh_runner_pids_recover_saved_child_and_gate_response(
    tmp_path: Path,
) -> None:
    """Only PostgreSQL joins the child, Gate, and five fresh Runner processes."""

    database_name = make_url(os.environ["CAD2BIM_BATCH3_END_DATABASE_URL"]).database or ""
    assert database_name.startswith("cad2bim_batch3_end_batch4runner_")
    api_url, runner_url = _isolated_urls()
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = signing_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    audit_path = tmp_path / "business-edit-audit.log"
    skill_path = tmp_path / "skills" / "batch4-skill" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_header = "---\nname: batch4-skill\ndescription: Use saved input identities.\n---\n"
    original_skill = "Use saved input identities."
    skill_path.write_text(skill_header + original_skill, encoding="utf-8")
    backend = await open_backend_runtime(
        api_settings=Settings(database_url=api_url, environment="test"),
        runner_settings=Settings(database_url=runner_url, environment="test"),
        public_key_pem=public_pem,
        issuer="https://batch4-restart.example",
        audience="cad2bim-batch4",
        execution_protocol=_binding(),
        graph_builder=lambda saver, nodes: _batch4_graph_builder(
            saver, nodes, fault="none", audit_path=audit_path
        ),
        runner_id=f"batch4-api-only-{uuid4().hex[:8]}",
        state_access_secret=b"batch4-restart-test-state-access-secret-32-bytes",
    )
    subject_name = f"person-{uuid4().hex}"
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": "https://batch4-restart.example",
            "aud": "cad2bim-batch4",
            "sub": subject_name,
            "exp": now + timedelta(hours=1),
        },
        signing_key,
        algorithm="RS256",
    )
    headers = {"Authorization": f"Bearer {token}"}

    async def run_worker(phase: str, *, expected_exit: int) -> dict[str, Any]:
        marker_path = tmp_path / f"{phase}-{uuid4().hex}.json"
        log_path = tmp_path / f"{phase}-{uuid4().hex}.log"
        worker_env = dict(os.environ)
        worker_env.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "CAD2BIM_BATCH4_WORKER_MODE": phase,
                "CAD2BIM_BATCH4_WORKER_RESULT": str(marker_path),
                "CAD2BIM_BATCH4_WORKER_AUDIT": str(audit_path),
                "CAD2BIM_BATCH4_PUBLIC_PEM_B64": base64.b64encode(public_pem).decode("ascii"),
            }
        )
        with log_path.open("w", encoding="utf-8") as output:
            worker = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--batch4-runner-worker"],
                cwd=Path(__file__).resolve().parents[3],
                env=worker_env,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
            )
        try:
            exit_code = await asyncio.to_thread(worker.wait, timeout=30)
        except subprocess.TimeoutExpired:
            worker.kill()
            await asyncio.to_thread(worker.wait, timeout=5)
            raise AssertionError(f"batch4 Runner PID {worker.pid} timed out; see {log_path}")
        assert exit_code == expected_exit, (
            f"Runner PID {worker.pid} phase {phase} exited {exit_code}; see {log_path}"
        )
        assert marker_path.exists(), f"Runner PID {worker.pid} lost marker; see {log_path}"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert marker["pid"] == worker.pid and marker["phase"] == phase
        print(f"batch4 Runner PID={worker.pid} phase={phase} exit={exit_code}")
        return marker

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend.api),
            base_url="http://batch4-runner.local",
        ) as client:
            created_task = await client.post(
                "/tasks", headers=headers, json={"title": "Cross-PID child and Gate recovery"}
            )
            assert created_task.status_code == 201, created_task.text
            task_id = created_task.json()["task_id"]
            created_run = await client.post(
                f"/tasks/{task_id}/runs", headers=headers,
                json={
                    "idempotency_key": f"batch4-run-{uuid4().hex}",
                    "input_payload": {
                        "role": "requirement",
                        "stage_key": "cad2bim_workflow",
                        "business_entry_id": "entry-first",
                        "role_entry_index": 1,
                        "gate_target_role": "requirement",
                        "input_high_watermark": 0,
                        "requirements": {"sources": [], "requirements": []},
                        "clarifications": [],
                        "requirement_md": "Original parent MD",
                        "role_memories": {},
                        "messages": [],
                        "main_count": 0,
                        "edit_marker_ids": [],
                    },
                },
            )
            assert created_run.status_code == 201, created_run.text
            run_id = created_run.json()["run_id"]
            path = f"/tasks/{task_id}/runs/{run_id}"
            first_body = "Full first feedback saved in the child."
            first_feedback = await client.post(
                f"{path}/feedback", headers=headers,
                json={
                    "client_request_id": "batch4-first-feedback",
                    "delivery_mode": "steer", "content": [{"type": "text", "text": first_body}],
                },
            )
            assert first_feedback.status_code == 201, first_feedback.text
            first_feedback_id = first_feedback.json()["feedback_id"]
            identity = await PostgresIdentityDirectory(
                backend.api_database.session_factory
            ).resolve_authenticated_subject(
                AuthenticatedSubject(
                    issuer="https://batch4-restart.example",
                    subject=subject_name,
                    audience="cad2bim-batch4",
                    authenticated_at=now,
                    token_expires_at=now + timedelta(hours=1),
                ),
                command_id=f"batch4_{uuid4().hex}",
            )
            with bind_tenant_execution_context(identity):
                run = await PostgresControlPlaneRepository(
                    backend.api_database.session_factory
                ).get_run(identity.tenant_id, run_id)
            assert run is not None
            thread_id = run.thread_id

            first_pid = await run_worker("before_parent_accept", expected_exit=73)
            assert "after child save before parent accept" in first_pid["fault"]
            parent_before = await backend.graph_runtime._graph.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
            assert parent_before.next == ("accept_role",)
            assert parent_before.values["parent_before"]["requirement_md"] == "Original parent MD"
            assert parent_before.values.get("role_memories", {}) == {}
            assert parent_before.values["requirement_md"].count(first_body) == 1
            # Diagnostic probe for this single registered child graph. The
            # completed child is absent from the parent snapshot's checkpoint_map;
            # production State reads use exact saved bindings, never this query.
            async with backend.runner_database.engine.connect() as connection:
                child_row = (await connection.execute(
                    text(
                        "SELECT checkpoint_ns, checkpoint_id FROM checkpoints "
                        "WHERE thread_id=:thread_id AND checkpoint_ns<>'' "
                        "ORDER BY checkpoint_id DESC LIMIT 1"
                    ),
                    {"thread_id": thread_id},
                )).one()
            child_saved = await backend.graph_runtime.get_checkpoint_state(
                thread_id=thread_id,
                checkpoint_id=child_row.checkpoint_id,
                checkpoint_ns=child_row.checkpoint_ns,
            )
            assert child_saved is not None
            assert child_saved.checkpoint.next_nodes == ()
            assert child_saved.values["requirement_md"].count(first_body) == 1
            assert child_saved.values["edit_marker_ids"] == ["edit_entry-first"]
            assert child_saved.graph_operation_id == f"run:{run_id}:start"
            async with backend.api_database.session_factory() as session, session.begin():
                await set_tenant_execution_context(session, identity)
                first_member = (await session.execute(
                    text(
                        "SELECT m.batch_id, m.feedback_id FROM feedback_delivery_batch_members m "
                        "WHERE m.run_id=:run_id AND m.feedback_id=:feedback_id"
                    ),
                    {"run_id": run_id, "feedback_id": first_feedback_id},
                )).one()
            first_message_id = feedback_message_id(
                first_member.batch_id, first_member.feedback_id
            )
            assert [item.id for item in child_saved.values["messages"]].count(
                first_message_id
            ) == 1

            await asyncio.sleep(1.2)
            second_pid = await run_worker("normal", expected_exit=0)
            assert second_pid["run_id"] == run_id
            assert second_pid["thread_id"] == thread_id
            assert second_pid["status"] == "waiting_user"
            parent_after = await backend.graph_runtime._graph.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
            assert parent_after.values["requirement_md"].count(first_body) == 1
            memory = parent_after.values["role_memories"]["requirement:cad2bim_workflow"]
            assert [item["id"] for item in memory["messages"]].count(first_message_id) == 1
            original_result = next(message for message in parse_role_messages(memory["messages"])
                if isinstance(message, ToolMessage) and original_skill in message.content)
            skill_message_id = original_result.id
            assert [item["id"] for item in memory["messages"]].count(skill_message_id) == 1
            assert parent_after.values["main_count"] == 1
            first_gate = await client.get(f"{path}/gates/current", headers=headers)
            assert first_gate.status_code == 200, first_gate.text
            first_gate_id = first_gate.json()["interrupt_id"]

            skill_path.write_text(skill_header + "Read current instructions on explicit tool calls.", encoding="utf-8")

            second_body = "Full approved feedback after the process exited."
            approved = await client.post(
                f"{path}/gates/{first_gate_id}/respond", headers=headers,
                json={
                    "action": "approve", "content": [{"type": "text", "text": second_body}],
                    "idempotency_key": "batch4-approve-with-text",
                },
            )
            assert approved.status_code == 200, approved.text
            assert (await client.get(path, headers=headers)).json()["status"] == "queued"
            third_pid = await run_worker("before_gate_resume", expected_exit=73)
            assert "saved Gate selection" in third_pid["fault"]
            assert third_pid["gate_operation_id"].startswith("gate_operation_")
            assert third_pid["gate_batch_id"]
            repeated = await client.post(
                f"{path}/gates/{first_gate_id}/respond", headers=headers,
                json={
                    "action": "approve", "content": [{"type": "text", "text": second_body}],
                    "idempotency_key": "batch4-approve-with-text",
                },
            )
            assert repeated.status_code == 200 and repeated.json() == approved.json()
            rejected = await client.post(
                f"{path}/gates/{first_gate_id}/respond", headers=headers,
                json={"action": "approve", "idempotency_key": "new-approval-key"},
            )
            assert rejected.status_code == 409

            await asyncio.sleep(1.2)
            fourth_pid = await run_worker("normal", expected_exit=0)
            assert fourth_pid["run_id"] == run_id
            assert fourth_pid["thread_id"] == thread_id
            assert fourth_pid["status"] == "waiting_user"
            assert (
                fourth_pid["gate_source_checkpoint_id"],
                fourth_pid["gate_operation_id"],
                fourth_pid["gate_batch_id"],
            ) == (
                third_pid["gate_source_checkpoint_id"],
                third_pid["gate_operation_id"],
                third_pid["gate_batch_id"],
            )
            parent_reentered = await backend.graph_runtime._graph.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
            markdown = parent_reentered.values["requirement_md"]
            assert markdown.count(first_body) == markdown.count(second_body) == 1
            assert parent_reentered.values["main_count"] == 2
            reentered_memory = parent_reentered.values["role_memories"]["requirement:cad2bim_workflow"]
            restored_result = next(message for message in parse_role_messages(reentered_memory["messages"])
                if message.id == skill_message_id)
            assert restored_result == original_result
            assert any(isinstance(message, ToolMessage)
                and "Read current instructions on explicit tool calls." in message.content
                for message in parse_role_messages(reentered_memory["messages"]))
            assert [item["id"] for item in reentered_memory["messages"]].count(
                skill_message_id
            ) == 1
            edit_marker_ids = parent_reentered.values["edit_marker_ids"]
            assert len(edit_marker_ids) == len(set(edit_marker_ids)) == 2
            async with backend.api_database.session_factory() as session, session.begin():
                await set_tenant_execution_context(session, identity)
                memberships = (await session.execute(
                    text(
                        "SELECT m.batch_id, m.feedback_id FROM feedback_delivery_batch_members m "
                        "WHERE m.run_id=:run_id ORDER BY m.accepted_sequence"
                    ),
                    {"run_id": run_id},
                )).all()
            assert len(memberships) == 2
            assert memberships[0].feedback_id == first_feedback_id
            assert memberships[1].batch_id == third_pid["gate_batch_id"]
            message_ids = [item.id for item in parent_reentered.values["messages"]]
            assert message_ids.count(feedback_message_id(*memberships[0])) == 1
            assert message_ids.count(feedback_message_id(*memberships[1])) == 1

            second_gate = await client.get(f"{path}/gates/current", headers=headers)
            assert second_gate.status_code == 200, second_gate.text
            second_gate_id = second_gate.json()["interrupt_id"]
            assert second_gate_id != first_gate_id
            final_approval = await client.post(
                f"{path}/gates/{second_gate_id}/respond", headers=headers,
                json={"action": "approve", "idempotency_key": "batch4-final-approve"},
            )
            assert final_approval.status_code == 200, final_approval.text
            final_pid = await run_worker("normal", expected_exit=0)
            assert final_pid["run_id"] == run_id
            assert final_pid["thread_id"] == thread_id
            assert final_pid["status"] == "completed"
            assert (await client.get(path, headers=headers)).json()["status"] == "completed"
            audit = audit_path.read_text(encoding="utf-8").splitlines()
            assert len(audit) == 2
            assert {line.split()[0] for line in audit} == {
                str(first_pid["pid"]), str(fourth_pid["pid"]),
            }
            assert {line.split()[1] for line in audit} == set(edit_marker_ids)
            assert len({
                first_pid["pid"], second_pid["pid"], third_pid["pid"],
                fourth_pid["pid"], final_pid["pid"],
            }) == 5
            print(f"batch4 Run={run_id} thread={thread_id} Gate responses=1+1 edits=2")
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_formal_requirement_actions_feedback_gate_and_restarted_mcp_read(
    tmp_path: Path,
) -> None:
    """One Run joins saved formal actions, safe feedback, Gate, and exact MCP reads."""

    database_name = make_url(os.environ["CAD2BIM_BATCH3_END_DATABASE_URL"]).database or ""
    assert database_name.startswith("cad2bim_batch3_end_batch4formal_")
    api_url, runner_url = _isolated_urls()
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = signing_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    secret = b"batch4-formal-test-state-access-secret-32-bytes"
    issuer, audience = "https://batch4-formal.example", "cad2bim-batch4-formal"
    structure_started, release_structure = asyncio.Event(), asyncio.Event()
    fault_after_structure = {"armed": True}
    backend_holder: dict[str, Any] = {}
    mcp_holder: dict[str, RunnerStateMcpClient] = {}

    def graph_builder(saver: Any, nodes: FeedbackNodes) -> Any:
        return _formal_graph_builder(
            saver, nodes, backend_holder=backend_holder, mcp_holder=mcp_holder,
            structure_started=structure_started, release_structure=release_structure,
            fault_after_structure=fault_after_structure,
        )

    async def open_runtime(label: str) -> Any:
        return await open_backend_runtime(
            api_settings=Settings(database_url=api_url, environment="test"),
            runner_settings=Settings(database_url=runner_url, environment="test"),
            public_key_pem=public_pem,
            issuer=issuer,
            audience=audience,
            execution_protocol=_binding(),
            graph_builder=graph_builder,
            runner_id=f"batch4-formal-{label}-{uuid4().hex[:8]}",
            state_access_secret=secret,
        )

    backend = await open_runtime("first")
    backend_holder["runtime"] = backend
    successor = None
    readback = None
    api_server: uvicorn.Server | None = None
    api_task: asyncio.Task[None] | None = None
    mcp_process: subprocess.Popen[str] | None = None
    mcp_port = _free_port()
    backend_port = _free_port()

    async def stop_services() -> None:
        nonlocal mcp_process, api_server, api_task
        if mcp_process is not None:
            mcp_process.terminate()
            try:
                await asyncio.to_thread(mcp_process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                mcp_process.kill()
                await asyncio.to_thread(mcp_process.wait, timeout=5)
            print(f"formal MCP PID={mcp_process.pid} exit={mcp_process.returncode}")
            mcp_process = None
        if api_server is not None and api_task is not None:
            api_server.should_exit = True
            await api_task
            api_server, api_task = None, None

    async def start_services(runtime: Any, *, label: str) -> str:
        nonlocal mcp_process, api_server, api_task, backend_port, mcp_port
        backend_port, mcp_port = _free_port(), _free_port()
        api_server = uvicorn.Server(uvicorn.Config(
            runtime.api, host="127.0.0.1", port=backend_port,
            log_level="warning", lifespan="off", access_log=False,
        ))
        api_task = asyncio.create_task(api_server.serve())
        await _wait_tcp(backend_port)
        mcp_env = dict(os.environ)
        mcp_env.update({
            "PYTHONDONTWRITEBYTECODE": "1",
            "CAD2BIM_STATE_ACCESS_SECRET_B64": base64.b64encode(secret).decode("ascii"),
            "CAD2BIM_RUNNER_STATE_BACKEND_URL": f"http://127.0.0.1:{backend_port}",
            "CAD2BIM_RUNNER_MCP_PORT": str(mcp_port),
        })
        with (tmp_path / f"{label}-mcp.log").open("w", encoding="utf-8") as output:
            mcp_process = subprocess.Popen(
                [sys.executable, "-m", "CAD2BIM_mcp.runner.server"],
                cwd=Path(__file__).resolve().parents[3], env=mcp_env,
                stdout=output, stderr=subprocess.STDOUT, text=True,
            )
        await _wait_tcp(mcp_port, process=mcp_process)
        print(f"formal MCP PID={mcp_process.pid} phase={label}")
        return f"http://127.0.0.1:{mcp_port}/mcp"

    now = datetime.now(timezone.utc)
    subject_name = f"person-{uuid4().hex}"
    token = jwt.encode({
        "iss": issuer, "aud": audience, "sub": subject_name,
        "exp": now + timedelta(hours=1),
    }, signing_key, algorithm="RS256")
    headers = {"Authorization": f"Bearer {token}"}
    backend.runner._lease_seconds = 1
    backend.runner._heartbeat_interval_seconds = 0.2
    try:
        mcp_url = await start_services(backend, label="running")
        mcp_holder["client"] = RunnerStateMcpClient(
            mcp_url=mcp_url, codec=backend.state_access_codec
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend.api),
            base_url="http://batch4-formal.local",
        ) as client:
            task_response = await client.post(
                "/tasks", headers=headers, json={"title": "Formal action and Gate Run"}
            )
            assert task_response.status_code == 201, task_response.text
            task_id = task_response.json()["task_id"]
            run_response = await client.post(
                f"/tasks/{task_id}/runs", headers=headers,
                json={
                    "idempotency_key": f"formal-{uuid4().hex}",
                    "input_payload": {
                        "role": "requirement", "stage_key": "cad2bim_workflow",
                        "business_entry_id": "formal-first-entry",
                        "role_entry_index": 1,
                        "gate_target_role": "requirement", "input_high_watermark": 0,
                        "requirements": None, "clarifications": None,
                        "previous_clarifications": None, "requirement_md": None,
                        "role_memories": {}, "messages": [],
                        "action_history": [], "applied_feedback_batches": [],
                        "inspection_history": [],
                    },
                },
            )
            assert run_response.status_code == 201, run_response.text
            run_id = run_response.json()["run_id"]
            path = f"/tasks/{task_id}/runs/{run_id}"
            identity = await PostgresIdentityDirectory(
                backend.api_database.session_factory
            ).resolve_authenticated_subject(
                AuthenticatedSubject(
                    issuer=issuer, subject=subject_name, audience=audience,
                    authenticated_at=now, token_expires_at=now + timedelta(hours=1),
                ),
                command_id=f"formal_{uuid4().hex}",
            )
            with bind_tenant_execution_context(identity):
                run = await PostgresControlPlaneRepository(
                    backend.api_database.session_factory
                ).get_run(identity.tenant_id, run_id)
            assert run is not None
            thread_id = run.thread_id
            first_execution = asyncio.create_task(backend.runner.execute_next())
            await asyncio.wait_for(structure_started.wait(), timeout=15)
            running_feedback_body = "Apply this feedback after the selected structure action."
            running_feedback = await client.post(
                f"{path}/feedback", headers=headers,
                json={
                    "client_request_id": "formal-running-feedback",
                    "delivery_mode": "steer", "content": [{"type": "text", "text": running_feedback_body}],
                },
            )
            assert running_feedback.status_code == 201, running_feedback.text
            release_structure.set()
            with pytest.raises(RuntimeError, match="saved structure command and result"):
                await first_execution
            parent_fault = await backend.graph_runtime._graph.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
            assert parent_fault.next == ("role_subgraph",)
            pending_children = [
                task for task in parent_fault.tasks if task.name == "role_subgraph"
            ]
            assert len(pending_children) == 1
            pending_child_ns = pending_children[0].state["configurable"]["checkpoint_ns"]
            # Test-only probe of the saved child result before any successor
            # starts. Production reads use the signed exact node binding.
            async with backend.runner_database.engine.connect() as connection:
                child_row = (await connection.execute(
                    text(
                        "SELECT checkpoint_ns, checkpoint_id FROM checkpoints "
                        "WHERE thread_id=:thread_id AND checkpoint_ns=:checkpoint_ns "
                        "ORDER BY checkpoint_id DESC LIMIT 1"
                    ),
                    {"thread_id": thread_id, "checkpoint_ns": pending_child_ns},
                )).one()
            assert child_row.checkpoint_ns == pending_child_ns
            child_fault = await backend.graph_runtime.get_checkpoint_state(
                thread_id=thread_id,
                checkpoint_ns=child_row.checkpoint_ns,
                checkpoint_id=child_row.checkpoint_id,
            )
            assert child_fault is not None
            assert child_fault.values["action_result"]["kind"] == "initialize_requirements"
            saved_command = SavedRequirementCommand.model_validate(
                child_fault.values["saved_command"]
            )
            assert saved_command.role_execution_id == child_fault.values["feedback_subject_id"]
            created_row_id = child_fault.values["action_result"]["created_item_ids"][0]
            assert child_fault.values["requirements"]["requirements"][0]["id"] == created_row_id
            print(
                f"formal Run={run_id} child_ns={child_row.checkpoint_ns} "
                f"saved command={saved_command.command_id} row={created_row_id} after fault"
            )

            await asyncio.sleep(1.2)
            successor = await open_runtime("successor")
            successor.runner._lease_seconds = 10
            successor.runner._heartbeat_interval_seconds = 1
            backend_holder["runtime"] = successor
            await successor.runner.execute_next()
            first_wait = await client.get(path, headers=headers)
            assert first_wait.status_code == 200 and first_wait.json()["status"] == "waiting_user"
            first_state = await successor.graph_runtime._graph.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
            first_values = first_state.values
            print(
                "formal recovered command/row=",
                first_values["action_history"][0]["command_id"],
                first_values["requirements"]["requirements"][0]["id"],
            )
            assert first_values["clarifications"] == []
            assert first_values["previous_clarifications"] is None
            assert first_values["requirements"]["requirements"][0]["id"] == created_row_id
            assert first_values["requirement_md"].count(running_feedback_body) == 1
            first_actions = first_values["action_history"]
            assert [record["kind"] for record in first_actions] == [
                "initialize_requirements", "change_clarifications",
                "change_requirement_md", "change_requirement_md",
            ]
            assert first_actions[0]["command_id"] == saved_command.command_id
            assert first_actions[0]["role_execution_id"] == saved_command.role_execution_id
            assert len(first_values["inspection_history"]) >= 1
            first_entry = first_values["inspection_history"][-1]["business_entry_id"]
            assert first_entry == "formal-first-entry"
            gate_response = await client.get(f"{path}/gates/current", headers=headers)
            assert gate_response.status_code == 200, gate_response.text
            first_gate = gate_response.json()["interrupt_id"]
            gate_body = "Include the approved Gate text in the next business entry."
            replied = await client.post(
                f"{path}/gates/{first_gate}/respond", headers=headers,
                json={
                    "action": "approve", "content": [{"type": "text", "text": gate_body}],
                    "idempotency_key": "formal-gate-text",
                },
            )
            assert replied.status_code == 200, replied.text
            await successor.runner.execute_next()
            second_wait = await client.get(path, headers=headers)
            assert second_wait.status_code == 200 and second_wait.json()["status"] == "waiting_user"
            second_state = await successor.graph_runtime._graph.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
            second_values = second_state.values
            assert second_values["business_entry_id"] != first_entry
            assert second_values["previous_clarifications"] is None
            assert second_values["clarifications"] == []
            assert second_values["requirement_md"].count(running_feedback_body) == 1
            assert second_values["requirement_md"].count(gate_body) == 1
            assert second_values["requirements"]["requirements"][0]["id"] == created_row_id
            assert len(second_values["inspection_history"]) >= 2
            assert second_values["inspection_history"][-1]["business_entry_id"] == second_values[
                "business_entry_id"
            ]
            assert [item["kind"] for item in second_values["action_history"]].count(
                "initialize_requirements"
            ) == 1
            second_gate_response = await client.get(f"{path}/gates/current", headers=headers)
            assert second_gate_response.status_code == 200, second_gate_response.text
            second_gate = second_gate_response.json()["interrupt_id"]
            assert second_gate != first_gate
            final_approval = await client.post(
                f"{path}/gates/{second_gate}/respond", headers=headers,
                json={"action": "approve", "idempotency_key": "formal-final-approval"},
            )
            assert final_approval.status_code == 200, final_approval.text
            await successor.runner.execute_next()
            assert (await client.get(path, headers=headers)).json()["status"] == "completed"
            access = await client.post(f"{path}/state-read-access", headers=headers)
            assert access.status_code == 200, access.text
            pair = access.json()

        await stop_services()
        await backend.close()
        await successor.close()
        readback = await open_runtime("readback")
        readback_url = await start_services(readback, label="readback")
        transport = StreamableHttpTransport(readback_url, headers={
            "Authorization": f"Bearer {pair['access_token']}",
            "X-CAD2BIM-State-Binding": pair["read_binding"],
        })
        async with Client(transport) as mcp:
            resources = {str(item.uri) for item in await mcp.list_resources()}
            requirements = json.loads((await mcp.read_resource("state://requirements"))[0].text)
            clarifications = json.loads((await mcp.read_resource("state://clarifications"))[0].text)
            markdown = (await mcp.read_resource("state://requirement-md"))[0].text
        assert resources == {
            "state://requirements", "state://clarifications", "state://requirement-md"
        }
        assert requirements["requirements"][0]["id"] == created_row_id
        assert clarifications == []
        assert markdown.count(running_feedback_body) == markdown.count(gate_body) == 1
        print(f"formal Run={run_id} completed, restarted MCP read exact root")
    finally:
        release_structure.set()
        await stop_services()
        if readback is not None:
            await readback.close()
        if successor is not None:
            await successor.close()
        await backend.close()


if __name__ == "__main__" and sys.argv[1:] == ["--batch4-runner-worker"]:
    _batch4_worker_entry()
