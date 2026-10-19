from __future__ import annotations

from datetime import datetime, timezone
from typing import TypedDict

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.control_plane import ExecutionMode, RunRecord, RunStatus
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest
from CAD2BIM_backend.domain.state_read_binding import (
    ExactStateReadBinding,
    StateReadBindingError,
)
from CAD2BIM_backend.infrastructure.langgraph.node_read_binding import resolve_node_read_binding
from CAD2BIM_backend.ports.graph_runtime import GraphCheckpointDescriptor, GraphCheckpointState


def _run() -> RunRecord:
    binding = ExecutionProtocolBinding(
        bundle_id="workflow", module="workflow", stage_key="cad2bim_workflow"
    )
    now = datetime.now(timezone.utc)
    return RunRecord.model_construct(
        run_id="run-one",
        task_id="task-one",
        owner_id="tenant-one",
        tenant_id="tenant-one",
        principal_id="principal-one",
        module="workflow",
        stage_key="cad2bim_workflow",
        input_payload={},
        idempotency_key="key-one",
        thread_id="thread-one",
        execution_mode=ExecutionMode.GRAPH,
        execution_protocol=binding,
        status=RunStatus.RUNNING,
        claimed_by="runner-one",
        lease_token="lease-private",
        writer_generation=1,
        created_at=now,
        updated_at=now,
    )


class _Graph:
    def __init__(self, run: RunRecord) -> None:
        self.run = run
        self.calls: list[tuple[str, str, str]] = []

    async def get_checkpoint_state(self, *, thread_id, checkpoint_id, checkpoint_ns=""):
        self.calls.append((thread_id, checkpoint_id, checkpoint_ns))
        binding = self.run.execution_protocol
        assert binding is not None
        checkpoint_map = (("", "root-one"),)
        if checkpoint_ns:
            checkpoint_map += (("role:task123", "child-one"),)
        return GraphCheckpointState(
            values={"requirements": []},
            checkpoint=GraphCheckpointDescriptor(
                thread_id=thread_id,
                checkpoint_id=checkpoint_id,
                checkpoint_ns=checkpoint_ns,
                root_checkpoint_id="root-one",
                checkpoint_map=checkpoint_map,
                checkpoint_map_digest=checkpoint_cursor_digest(
                    checkpoint_id=checkpoint_id,
                    checkpoint_ns=checkpoint_ns,
                    checkpoint_map=checkpoint_map,
                ),
            ),
        )


@pytest.mark.asyncio
async def test_node_binding_uses_saved_parent_and_child_input_map_exactly() -> None:
    run = _run()
    graph = _Graph(run)
    grant = RunnerCheckpointGrant.from_claim(run, runner_id="runner-one")
    node_config = {
        "configurable": {
            "thread_id": run.thread_id,
            "checkpoint_id": None,
            "checkpoint_ns": "role:task123|edit:task456",
            "checkpoint_map": {"": "root-one", "role:task123": "child-one"},
        }
    }

    binding = await resolve_node_read_binding(
        run=run,
        grant=grant,
        node_config=node_config,
        registered_graph_paths=("", "role"),
        field_owner_paths={
            "requirements": "role",
            "clarifications": "role",
            "requirement_md": "role",
        },
        graph_runtime=graph,
    )

    assert binding.checkpoint_cursor.checkpoint_ns == "role:task123"
    assert binding.checkpoint_cursor.checkpoint_id == "child-one"
    assert binding.checkpoint_cursor.root_checkpoint_id == "root-one"
    assert binding.checkpoint_cursor.checkpoint_map == (
        ("", "root-one"),
        ("role:task123", "child-one"),
    )
    assert graph.calls == [
        ("thread-one", "root-one", ""),
        ("thread-one", "child-one", "role:task123"),
    ]
    assert "lease-private" not in binding.model_dump_json()


@pytest.mark.asyncio
async def test_node_binding_selects_current_child_and_keeps_saved_sibling_lineage() -> None:
    run = _run()
    graph = _Graph(run)
    grant = RunnerCheckpointGrant.from_claim(run, runner_id="runner-one")
    checkpoint_map = {
        "": "root-one",
        "role:old-task": "old-child",
        "role:task123": "child-one",
    }
    binding = await resolve_node_read_binding(
        run=run,
        grant=grant,
        node_config={"configurable": {
            "thread_id": run.thread_id,
            "checkpoint_ns": "role:task123|inspect:new-task",
            "checkpoint_map": checkpoint_map,
        }},
        registered_graph_paths=("", "role"),
        field_owner_paths={
            "requirements": "role", "clarifications": "role", "requirement_md": "role"
        },
        graph_runtime=graph,
    )
    assert binding.checkpoint_cursor.checkpoint_ns == "role:task123"
    assert binding.checkpoint_cursor.checkpoint_map == tuple(sorted(checkpoint_map.items()))
    assert {source.checkpoint_ns for source in binding.field_sources} == {"role:task123"}
    assert graph.calls == [
        ("thread-one", "root-one", ""),
        ("thread-one", "child-one", "role:task123"),
    ]

    graph.calls.clear()
    with pytest.raises(StateReadBindingError, match="one registered Graph"):
        await resolve_node_read_binding(
            run=run,
            grant=grant,
            node_config={"configurable": {
                "thread_id": run.thread_id,
                "checkpoint_ns": "role:missing|inspect:new-task",
                "checkpoint_map": checkpoint_map,
            }},
            registered_graph_paths=("", "role"),
            field_owner_paths={
                "requirements": "role", "clarifications": "role", "requirement_md": "role"
            },
            graph_runtime=graph,
        )
    assert graph.calls == []


@pytest.mark.asyncio
async def test_partial_field_owner_uses_child_for_owned_and_root_for_unowned() -> None:
    run = _run()
    graph = _Graph(run)
    binding = await resolve_node_read_binding(
        run=run,
        grant=RunnerCheckpointGrant.from_claim(run, runner_id="runner-one"),
        node_config={
            "configurable": {
                "thread_id": run.thread_id,
                "checkpoint_ns": "role:task123|edit:task456",
                "checkpoint_map": {"": "root-one", "role:task123": "child-one"},
            }
        },
        registered_graph_paths=("", "role"),
        field_owner_paths={"requirements": "role", "clarifications": "", "requirement_md": ""},
        graph_runtime=graph,
    )

    assert binding.source_for("requirements").checkpoint_ns == "role:task123"
    assert binding.source_for("requirements").checkpoint_id == "child-one"
    assert binding.source_for("requirement_md").checkpoint_ns == ""
    assert binding.source_for("requirement_md").checkpoint_id == "root-one"
    assert ExactStateReadBinding.model_validate_json(binding.model_dump_json()) == binding

    forged = binding.model_dump(mode="python")
    forged["field_sources"][0]["checkpoint_id"] = "different-child"
    with pytest.raises(ValidationError, match="outside the exact checkpoint map"):
        ExactStateReadBinding.model_validate(forged)


@pytest.mark.asyncio
async def test_read_only_role_keeps_every_field_at_root() -> None:
    run = _run()
    graph = _Graph(run)
    binding = await resolve_node_read_binding(
        run=run,
        grant=RunnerCheckpointGrant.from_claim(run, runner_id="runner-one"),
        node_config={
            "configurable": {
                "thread_id": run.thread_id,
                "checkpoint_ns": "role:task123|read:task456",
                "checkpoint_map": {"": "root-one", "role:task123": "child-one"},
            }
        },
        registered_graph_paths=("", "role"),
        field_owner_paths={"requirements": "", "clarifications": "", "requirement_md": ""},
        graph_runtime=graph,
    )
    assert {source.checkpoint_ns for source in binding.field_sources} == {""}
    assert {source.checkpoint_id for source in binding.field_sources} == {"root-one"}


@pytest.mark.asyncio
async def test_unregistered_field_owner_fails_before_checkpoint_read() -> None:
    run = _run()
    graph = _Graph(run)
    with pytest.raises(StateReadBindingError, match="field owners"):
        await resolve_node_read_binding(
            run=run,
            grant=RunnerCheckpointGrant.from_claim(run, runner_id="runner-one"),
            node_config={
                "configurable": {
                    "thread_id": run.thread_id,
                    "checkpoint_ns": "role:task123|edit:task456",
                    "checkpoint_map": {"": "root-one", "role:task123": "child-one"},
                }
            },
            registered_graph_paths=("", "role"),
            field_owner_paths={"requirements": "other", "clarifications": "", "requirement_md": ""},
            graph_runtime=graph,
        )
    assert graph.calls == []


@pytest.mark.asyncio
async def test_node_binding_rejects_missing_child_input_without_reading_latest() -> None:
    run = _run()
    graph = _Graph(run)
    with pytest.raises(StateReadBindingError, match="one registered Graph"):
        await resolve_node_read_binding(
            run=run,
            grant=RunnerCheckpointGrant.from_claim(run, runner_id="runner-one"),
            node_config={
                "configurable": {
                    "thread_id": run.thread_id,
                    "checkpoint_ns": "role:task123|edit:task456",
                    "checkpoint_map": {"": "root-one"},
                }
            },
            registered_graph_paths=("", "role"),
            field_owner_paths={"requirements": "role", "clarifications": "", "requirement_md": ""},
            graph_runtime=graph,
        )
    assert graph.calls == []


@pytest.mark.asyncio
async def test_node_binding_rejects_unregistered_saved_namespace() -> None:
    run = _run()
    graph = _Graph(run)
    with pytest.raises(StateReadBindingError, match="unregistered Graph"):
        await resolve_node_read_binding(
            run=run,
            grant=RunnerCheckpointGrant.from_claim(run, runner_id="runner-one"),
            node_config={
                "configurable": {
                    "thread_id": run.thread_id,
                    "checkpoint_ns": "role:task123|edit:task456",
                    "checkpoint_map": {
                        "": "root-one",
                        "role:task123": "child-one",
                        "other:task": "other-one",
                    },
                }
            },
            registered_graph_paths=("", "role"),
            field_owner_paths={"requirements": "role", "clarifications": "", "requirement_md": ""},
            graph_runtime=graph,
        )
    assert graph.calls == []


def test_locked_langgraph_recast_contract() -> None:
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph._internal._config import recast_checkpoint_ns
    from langchain_core.runnables import RunnableConfig

    class State(TypedDict):
        value: int

    seen: list[dict] = []

    def leaf(state: State, config: RunnableConfig) -> dict:
        seen.append(config["configurable"])
        return {"value": state["value"] + 1}

    child = StateGraph(State)
    child.add_node("leaf", leaf)
    child.add_edge(START, "leaf")
    child.add_edge("leaf", END)
    root = StateGraph(State)
    root.add_node("role", child.compile())
    root.add_edge(START, "role")
    root.add_edge("role", END)
    compiled = root.compile(checkpointer=MemorySaver())
    compiled.invoke({"value": 1}, {"configurable": {"thread_id": "thread-one"}})

    assert len(seen) == 1
    node = seen[0]
    assert node["checkpoint_id"] is None
    assert recast_checkpoint_ns(node["checkpoint_ns"]) == "role|leaf"
    assert len(node["checkpoint_map"]) == 2
    assert "" in node["checkpoint_map"]
    assert sum(recast_checkpoint_ns(ns) == "role" for ns in node["checkpoint_map"]) == 1
    for namespace, checkpoint_id in node["checkpoint_map"].items():
        saved = compiled.get_state(
            {
                "configurable": {
                    "thread_id": "thread-one",
                    "checkpoint_ns": namespace,
                    "checkpoint_id": checkpoint_id,
                }
            }
        )
        assert saved.config["configurable"]["checkpoint_id"] == checkpoint_id
