from __future__ import annotations

from types import SimpleNamespace

import pytest

from CAD2BIM_backend.domain.control_plane import ExecutionMode, LeaseConflictError
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant, RunnerGraphContext
from CAD2BIM_backend.infrastructure.langgraph.feedback_nodes import (
    FeedbackNodes,
    exact_node_input_cursor,
)


def _runtime(thread_id: str, checkpoint_id: str):
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id, checkpoint_id=checkpoint_id)
    )


def _grant() -> RunnerCheckpointGrant:
    identity = TenantExecutionContext(
        tenant_id="tenant-one",
        principal_id="runner-one",
        task_id="task-one",
        actor_kind=TenantActorKind.RUNNER,
        command_id="run:run-one:generation:2",
    )
    scope = InvocationScope(
        identity=identity,
        owner_id="tenant-one",
        run_id="run-one",
        execution_mode=ExecutionMode.GRAPH,
        catalog_frontier=None,
    )
    return RunnerCheckpointGrant(
        graph_context=RunnerGraphContext(scope=scope),
        thread_id="run-thread",
        runner_id="runner-one",
        lease_token="lease-two",
        writer_generation=2,
    )


def test_exact_node_input_cursor_uses_saved_nested_namespace() -> None:
    cursor = exact_node_input_cursor(
        {
            "configurable": {
                "thread_id": "run-thread",
                "checkpoint_map": {"": "root-1", "role:call-1": "child-2"},
            }
        },
        _runtime("run-thread", "child-2"),
    )
    assert cursor.checkpoint_id == "child-2"
    assert cursor.checkpoint_ns == "role:call-1"
    assert cursor.root_checkpoint_id == "root-1"
    assert dict(cursor.checkpoint_map) == {"": "root-1", "role:call-1": "child-2"}


def test_exact_node_input_cursor_rejects_stale_or_ambiguous_map() -> None:
    config = {"configurable": {"thread_id": "run-thread", "checkpoint_map": {"": "root-1"}}}
    with pytest.raises(RuntimeError, match="one exact namespace"):
        exact_node_input_cursor(config, _runtime("run-thread", "child-2"))
    with pytest.raises(RuntimeError, match="exact thread checkpoint"):
        exact_node_input_cursor(config, _runtime("other-thread", "root-1"))


@pytest.mark.asyncio
async def test_feedback_nodes_use_saved_input_and_same_run_boundary() -> None:
    calls = []

    class _Service:
        async def activate_feedback_subject(self, grant, **kwargs):
            calls.append(("activate", grant, kwargs))
            return SimpleNamespace(
                subject_id="feedback_subject_one",
                stage_key="cad2bim_workflow",
                writer_generation=2,
                task_ids=(),
                check_ids=(),
            )

        async def freeze_for_main(self, grant, **kwargs):
            calls.append(("freeze", grant, kwargs))
            return None

        async def decide_role_exit(self, grant, subject_id, watermark, **kwargs):
            calls.append(("exit", grant, subject_id, watermark, kwargs))
            return True

    grant = _grant()
    runtime = _runtime("run-thread", "child-2")
    runtime.context = grant.graph_context
    config = {
        "configurable": {
            "thread_id": "run-thread",
            "checkpoint_map": {"": "root-1", "role:call-1": "child-2"},
        }
    }
    nodes = FeedbackNodes(_Service())
    nodes.bind_grant_provider(lambda: grant)
    update = await nodes.prepare_main(
        {"role": "requirement", "stage_key": None, "business_entry_id": "entry-one",
         "role_entry_index": 2, "feedback_batch_ids": ("previous-saved-batch",)},
        config,
        runtime,
    )
    assert update == {
        "feedback_subject_id": "feedback_subject_one",
        "feedback_batch_ids": (),
        "input_high_watermark": 0,
    }
    assert calls[0][2]["checkpoint_id"] == "child-2"
    assert calls[0][2]["business_entry_id"] == "entry-one"
    assert calls[0][2]["attempt"] == 2
    assert calls[1][2]["role_execution_id"] == "feedback_subject_one"
    assert calls[1][2]["input_checkpoint_cursor"].checkpoint_ns == "role:call-1"
    assert calls[1][2]["delivered_batch_ids"] == ("previous-saved-batch",)
    update["feedback_batch_ids"] = ("last-saved-batch",)
    assert await nodes.decide_role_exit(update, config, runtime) == {
        "role_exit_route": "parent_gate", "feedback_batch_ids": ()
    }
    assert calls[2][:4] == ("exit", grant, "feedback_subject_one", 0)
    assert calls[2][4]["delivered_batch_ids"] == ("last-saved-batch",)
    assert calls[2][4]["input_checkpoint_cursor"].checkpoint_id == "child-2"


@pytest.mark.asyncio
async def test_feedback_node_requires_the_bound_run_claim() -> None:
    nodes = FeedbackNodes(SimpleNamespace())
    grant = _grant()
    runtime = _runtime("run-thread", "root-1")
    runtime.context = grant.graph_context
    with pytest.raises(RuntimeError, match="not bound"):
        await nodes.prepare_main({}, {}, runtime)
    nodes.bind_grant_provider(lambda: grant)
    runtime.execution_info.thread_id = "other-thread"
    with pytest.raises(LeaseConflictError, match="matching Run claim"):
        await nodes.prepare_main({}, {}, runtime)
