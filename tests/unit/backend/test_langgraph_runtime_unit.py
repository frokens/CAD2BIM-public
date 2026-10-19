from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import TypedDict

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Interrupt, interrupt

from CAD2BIM_backend.domain.control_plane import LeaseConflictError
from CAD2BIM_backend.infrastructure.langgraph.fenced_saver import FencedAsyncPostgresSaver
from CAD2BIM_backend.infrastructure.langgraph.observing_saver import ObservingAsyncCheckpointSaver
from CAD2BIM_backend.infrastructure.langgraph.runtime import (
    AsyncPostgresGraphRuntime,
    _OperationReceipt,
)
from CAD2BIM_backend.ports.graph_runtime import (
    GraphCheckpointDescriptor,
    GraphExecutionResult,
)


class _ExactUpdateGraph:
    def __init__(self) -> None:
        self.read_configs: list[dict] = []

    async def aupdate_state(self, config, values, as_node=None):
        configurable = dict(config["configurable"])
        checkpoint_map = configurable.pop("checkpoint_map", None)
        assert configurable == {
            "thread_id": "thread-1",
            "checkpoint_ns": "ifc:gas",
            "checkpoint_id": "checkpoint-before",
        }
        if checkpoint_map is not None:
            assert checkpoint_map == {
                "": "checkpoint-root",
                "ifc:gas": "checkpoint-before",
            }
        assert values == {"mcp_snapshot_id": "snapshot-1"}
        assert as_node is None
        return {
            "configurable": {
                "thread_id": "thread-1",
                "checkpoint_ns": "ifc:gas",
                "checkpoint_id": "checkpoint-from-update",
                "checkpoint_map": {
                    "": "checkpoint-root",
                    "ifc:gas": "checkpoint-from-update",
                },
            }
        }

    async def aget_state(self, config):
        self.read_configs.append(config)
        return SimpleNamespace(
            config=config,
            next=("reviewer",),
            created_at="2026-07-11T00:00:00Z",
        )


class _SaverContext:
    async def __aexit__(self, *_args):
        return None


class _RetryingSaverContext:
    def __init__(self) -> None:
        self.calls = 0

    async def __aexit__(self, *_args):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("close failed")
        return None


class _ExactReadSaver:
    def __init__(self, persisted=None) -> None:
        self.persisted = persisted
        self.configs: list[dict] = []

    async def aget_tuple(self, config):
        self.configs.append(config)
        return self.persisted


class _FencedSaver:
    def __init__(self) -> None:
        self.bound: list[object] = []

    @contextmanager
    def bind_execution(self, grant):
        self.bound.append(grant)
        yield


@pytest.mark.asyncio
async def test_callbacks_keep_sync_durability_and_claim_context():
    callback = AsyncCallbackHandler()
    received = []

    class Graph:
        async def ainvoke(self, value, config, **kwargs):
            received.append((value, config, kwargs))

    saver = _FencedSaver()
    runtime = AsyncPostgresGraphRuntime(
        graph=Graph(), saver=saver, fenced_saver=saver, saver_context=_SaverContext(),
        callbacks=[callback],
    )
    grant = _grant()
    config = {"configurable": {"thread_id": "thread-1"}, "metadata": {"operation": "start"}}
    await runtime._invoke(grant, {"value": 1}, config)
    value, invoked, kwargs = received[0]
    assert value == {"value": 1}
    assert invoked == {**config, "callbacks": [callback], "metadata": {
        **config["metadata"],
        "run_id": grant.scope.run_id, "task_id": grant.scope.identity.task_id,
        "thread_id": grant.thread_id,
    }}
    assert "callbacks" not in config
    assert kwargs == {"context": grant.graph_context, "durability": "sync"}
    assert saver.bound == [grant]


@pytest.mark.asyncio
async def test_runtime_binds_presentation_hooks_only_before_execution():
    class Graph:
        async def ainvoke(self, value, config, **kwargs):
            assert len(config["callbacks"]) == 1

    observing = ObservingAsyncCheckpointSaver(MemorySaver())
    runtime = AsyncPostgresGraphRuntime(
        graph=Graph(), saver=observing, fenced_saver=_FencedSaver(), saver_context=_SaverContext(),
    )
    observer = object()
    runtime.bind_checkpoint_observer(observer)
    runtime.bind_callbacks([AsyncCallbackHandler()])
    with pytest.raises(RuntimeError, match="already bound"):
        runtime.bind_checkpoint_observer(object())
    with pytest.raises(RuntimeError, match="already bound"):
        runtime.bind_callbacks([AsyncCallbackHandler()])
    await runtime._invoke(_grant(), {}, {"configurable": {"thread_id": "thread-1"}})
    with pytest.raises(RuntimeError, match="before execution"):
        runtime.bind_checkpoint_observer(object())
    with pytest.raises(RuntimeError, match="before execution"):
        runtime.bind_callbacks([AsyncCallbackHandler()])


def _grant(run_id: str = "run-1", thread_id: str = "thread-1"):
    return SimpleNamespace(
        thread_id=thread_id,
        scope=SimpleNamespace(run_id=run_id, identity=SimpleNamespace(task_id="task-1")),
        graph_context=object(),
    )


@pytest.mark.asyncio
async def test_current_execution_grant_is_private_and_request_bound():
    saver = FencedAsyncPostgresSaver(conn=object())
    runtime = AsyncPostgresGraphRuntime(
        graph=object(), saver=saver, fenced_saver=saver, saver_context=_SaverContext()
    )
    grant = _grant()
    with pytest.raises(LeaseConflictError):
        runtime.require_current_execution_grant()
    with saver.bind_execution(grant):
        assert runtime.require_current_execution_grant() is grant
    with pytest.raises(LeaseConflictError):
        runtime.require_current_execution_grant()


class _RootResumeGraph:
    def __init__(self) -> None:
        self.read_configs: list[dict] = []

    async def aget_state(self, config):
        self.read_configs.append(config)
        assert config == {
            "configurable": {
                "thread_id": "thread-1",
                "checkpoint_ns": "",
                "checkpoint_id": "checkpoint-root",
            }
        }
        return SimpleNamespace(
            config=config,
            next=("outer",),
            created_at="2026-07-12T00:00:00Z",
        )


def _operation_checkpoint(
    checkpoint_id: str,
    *,
    checkpoint_ns: str,
    timestamp: str,
    step: int,
):
    return SimpleNamespace(
        config={
            "configurable": {
                "thread_id": "thread-1",
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        },
        checkpoint={"ts": timestamp},
        metadata={"step": step},
    )


@pytest.mark.asyncio
async def test_unclaimed_legacy_graph_write_entries_fail_closed():
    runtime = AsyncPostgresGraphRuntime(
        graph=object(), saver=object(), saver_context=_SaverContext()
    )
    with pytest.raises(RuntimeError, match="volatile Graph writes"):
        await runtime.start(thread_id="thread-1", input_value={})
    with pytest.raises(RuntimeError, match="volatile Graph writes"):
        await runtime.resume(thread_id="thread-1", resume_value="continue")
    with pytest.raises(RuntimeError, match="volatile Graph writes"):
        await runtime.checkpoint_state(thread_id="thread-1", values={})
    with pytest.raises(RuntimeError, match="fork is unavailable"):
        await runtime.fork_exact_checkpoint(
            source=object(),
            target_thread_id="thread-2",
            operation_id="fork-1",
            target_run_id="run-2",
        )


@pytest.mark.asyncio
async def test_get_checkpoint_returns_none_when_exact_cursor_does_not_exist():
    saver = _ExactReadSaver()

    class EchoGraph:
        def __init__(self) -> None:
            self.read_configs: list[dict] = []

        async def aget_state(self, config):
            self.read_configs.append(config)
            return SimpleNamespace(config=None, created_at=None)

    graph = EchoGraph()
    runtime = AsyncPostgresGraphRuntime(
        graph=graph,
        saver=saver,
        saver_context=_SaverContext(),
    )

    result = await runtime.get_checkpoint(
        thread_id="thread-missing",
        checkpoint_id="checkpoint-missing",
        checkpoint_ns="stage:child",
    )

    assert result is None
    assert saver.configs == []
    assert graph.read_configs == [
        {
            "configurable": {
                "thread_id": "thread-missing",
                "checkpoint_ns": "stage:child",
                "checkpoint_id": "checkpoint-missing",
            }
        }
    ]


@pytest.mark.asyncio
async def test_exact_checkpoint_uses_one_graph_read_and_snapshot_metadata():
    saver = _ExactReadSaver()

    class Graph:
        def __init__(self) -> None:
            self.calls = []

        async def aget_state(self, config):
            self.calls.append(config)
            return SimpleNamespace(
                config={
                    "configurable": {
                        **config["configurable"],
                        "checkpoint_map": {"": "root-one", "role:main": "child-one"},
                    }
                },
                created_at="2026-09-24T00:00:00Z",
                next=(),
                interrupts=(),
                values={"requirement_md": "saved"},
                metadata={
                    "cad2bim_resume_operation_id": "operation-one",
                    "cad2bim_target_run_id": "run-one",
                },
            )

    graph = Graph()
    runtime = AsyncPostgresGraphRuntime(
        graph=graph, saver=saver, saver_context=_SaverContext()
    )
    state = await runtime.get_checkpoint_state(
        thread_id="thread-one", checkpoint_id="child-one", checkpoint_ns="role:main"
    )
    assert state is not None
    assert state.checkpoint.root_checkpoint_id == "root-one"
    assert state.graph_operation_id == "operation-one"
    assert state.target_run_id == "run-one"
    assert len(graph.calls) == 1
    assert saver.configs == []


@pytest.mark.asyncio
async def test_exact_display_read_preserves_state_without_replaying_observer():
    class State(TypedDict):
        text: str

    class Observer:
        def __init__(self):
            self.commits = []

        async def checkpoint_committed(self, commit):
            self.commits.append(commit)

    observer = Observer()
    saver = ObservingAsyncCheckpointSaver(MemorySaver(), observer)
    builder = StateGraph(State)
    builder.add_node("saved", lambda state: {"text": state["text"]})
    builder.add_edge(START, "saved")
    builder.add_edge("saved", END)
    graph = builder.compile(checkpointer=saver)
    config = {"configurable": {"thread_id": "display-thread"}, "metadata": {
        "cad2bim_target_run_id": "display-run", "cad2bim_resume_operation_id": "original-operation",
    }}
    await graph.ainvoke({"text": "Original saved text"}, config, durability="sync")
    saved = await saver._delegate.aget_tuple(config)
    runtime = AsyncPostgresGraphRuntime(graph=graph, saver=saver, saver_context=_SaverContext())
    cursor = {"thread_id": "display-thread", "checkpoint_id": saved.config["configurable"]["checkpoint_id"]}
    before = len(observer.commits)
    display = await runtime.get_checkpoint_state(**cursor, observe=False)
    assert len(observer.commits) == before
    ordinary = await runtime.get_checkpoint_state(**cursor)
    assert len(observer.commits) == before + 1
    assert display == ordinary
    assert display.values == {"text": "Original saved text"}
    assert display.target_run_id == "display-run" and display.graph_operation_id == "original-operation"


@pytest.mark.asyncio
async def test_nested_resume_command_routes_from_the_exact_branch_root():
    graph = _RootResumeGraph()
    runtime = AsyncPostgresGraphRuntime(
        graph=graph,
        saver=object(),
        saver_context=_SaverContext(),
    )
    source_snapshot = SimpleNamespace(
        config={
            "configurable": {
                "thread_id": "thread-1",
                "checkpoint_ns": "outer:1|inner:2",
                "checkpoint_id": "checkpoint-leaf",
                "checkpoint_map": {
                    "": "checkpoint-root",
                    "outer:1": "checkpoint-parent",
                    "outer:1|inner:2": "checkpoint-leaf",
                },
            }
        },
        next=("modeler",),
        created_at="2026-07-12T00:00:00Z",
    )
    metadata = {"cad2bim_resume_operation_id": "resume-1"}

    config, update = await runtime._prepare_resume_command(
        source_snapshot,
        grant=_grant(),
        state_update=None,
        metadata=metadata,
    )

    assert config == {
        "configurable": {
            "thread_id": "thread-1",
            "checkpoint_ns": "",
            "checkpoint_id": "checkpoint-root",
        },
        "metadata": metadata,
    }
    assert update is None
    assert len(graph.read_configs) == 1


@pytest.mark.asyncio
async def test_nested_resume_rebind_forks_the_exact_checkpoint_address():
    graph = _ExactUpdateGraph()
    saver = _FencedSaver()
    runtime = AsyncPostgresGraphRuntime(
        graph=graph,
        saver=object(),
        saver_context=_SaverContext(),
        fenced_saver=saver,
    )
    source_snapshot = SimpleNamespace(
        config={
            "configurable": {
                "thread_id": "thread-1",
                "checkpoint_ns": "ifc:gas",
                "checkpoint_id": "checkpoint-before",
                "checkpoint_map": {
                    "": "checkpoint-root",
                    "ifc:gas": "checkpoint-before",
                },
            }
        }
    )
    metadata = {"cad2bim_resume_operation_id": "resume-rebind-1"}

    grant = _grant()
    config, update = await runtime._prepare_resume_command(
        source_snapshot,
        grant=grant,
        state_update={"mcp_snapshot_id": "snapshot-1"},
        metadata=metadata,
    )

    assert config == {
        "configurable": {
            "thread_id": "thread-1",
            "checkpoint_ns": "ifc:gas",
            "checkpoint_id": "checkpoint-from-update",
            "checkpoint_map": {
                "": "checkpoint-root",
                "ifc:gas": "checkpoint-from-update",
            },
        },
        "metadata": metadata,
    }
    assert update is None
    assert saver.bound == [grant]
    assert graph.read_configs == [
        {
            "configurable": {
                "thread_id": "thread-1",
                "checkpoint_ns": "ifc:gas",
                "checkpoint_id": "checkpoint-from-update",
                "checkpoint_map": {
                    "": "checkpoint-root",
                    "ifc:gas": "checkpoint-from-update",
                },
            }
        }
    ]


@pytest.mark.asyncio
async def test_graph_runtime_close_can_retry_after_transport_failure():
    context = _RetryingSaverContext()
    runtime = AsyncPostgresGraphRuntime(
        graph=object(),
        saver=object(),
        saver_context=context,
    )

    with pytest.raises(RuntimeError, match="close failed"):
        await runtime.close()
    await runtime.close()
    await runtime.close()

    assert context.calls == 2


@pytest.mark.asyncio
async def test_durable_checkpoint_state_replays_the_original_operation_receipt(
    monkeypatch,
) -> None:
    class DurableUpdateGraph:
        def __init__(self) -> None:
            self.update_calls = 0

        async def aupdate_state(self, config, values, as_node=None):
            self.update_calls += 1
            assert config["configurable"]["checkpoint_id"] == "checkpoint-source"
            assert config["metadata"]["cad2bim_resume_operation_id"] == ("safe-point-operation")
            assert values == {"durable_safe_point": {"snapshot_id": "snapshot-1"}}
            assert as_node == "boundary"
            return receipt.snapshot.config

        async def aget_state(self, config):
            if config["configurable"]["checkpoint_id"] == "checkpoint-source":
                return SimpleNamespace(
                    config={"configurable": {**config["configurable"], "checkpoint_map": {
                        "": "checkpoint-root", "stage:child": "checkpoint-source",
                    }}},
                    next=("boundary",), created_at="2026-07-12T00:00:00Z",
                )
            assert config == receipt.snapshot.config
            return receipt.snapshot

    snapshot = SimpleNamespace(
        config={
            "configurable": {
                "thread_id": "thread-1",
                "checkpoint_ns": "stage:child",
                "checkpoint_id": "checkpoint-created-once",
                "checkpoint_map": {
                    "": "checkpoint-root",
                    "stage:child": "checkpoint-created-once",
                },
            }
        },
        next=("modeler",),
        created_at="2026-07-12T00:00:00Z",
    )
    receipt = _OperationReceipt(
        checkpoint=SimpleNamespace(metadata={}),
        snapshot=snapshot,
        root_checkpoint_id="checkpoint-root",
    )
    graph = DurableUpdateGraph()
    saver = _FencedSaver()
    runtime = AsyncPostgresGraphRuntime(
        graph=graph,
        saver=object(),
        saver_context=_SaverContext(),
        fenced_saver=saver,
        database_url="postgresql+psycopg://runner@db/cad2bim",
    )
    receipt_reads = 0

    async def operation_receipt(**_kwargs):
        nonlocal receipt_reads
        receipt_reads += 1
        return None if receipt_reads == 1 else receipt

    monkeypatch.setattr(runtime, "_operation_receipt", operation_receipt)

    async def run_once():
        return await runtime.checkpoint_state_durable(
            grant=_grant(),
            thread_id="thread-1",
            source_checkpoint_id="checkpoint-source",
            operation_id="safe-point-operation",
            target_run_id="run-1",
            checkpoint_ns="stage:child",
            as_node="boundary",
            values={"durable_safe_point": {"snapshot_id": "snapshot-1"}},
        )

    first = await run_once()
    second = await run_once()

    assert first == second
    assert first.checkpoint_id == "checkpoint-created-once"
    assert first.root_checkpoint_id == "checkpoint-root"
    assert graph.update_calls == 1
    assert len(saver.bound) == 1


def test_operation_receipt_uses_timestamp_across_namespaces_not_input_order():
    newest = _operation_checkpoint(
        "checkpoint-newest",
        checkpoint_ns="stage_b:2",
        timestamp="2026-07-11T00:00:02+00:00",
        step=0,
    )
    selected = AsyncPostgresGraphRuntime._latest_operation_checkpoint(
        [
            newest,
            _operation_checkpoint(
                "checkpoint-root",
                checkpoint_ns="",
                timestamp="2026-07-11T00:00:01+00:00",
                step=12,
            ),
            _operation_checkpoint(
                "checkpoint-child",
                checkpoint_ns="stage_a:1",
                timestamp="2026-07-11T00:00:00+00:00",
                step=99,
            ),
        ]
    )

    assert selected is newest


def test_operation_receipt_rejects_equal_timestamp_and_step_across_cursors():
    timestamp = "2026-07-11T00:00:02+00:00"
    with pytest.raises(RuntimeError, match="ambiguous"):
        AsyncPostgresGraphRuntime._latest_operation_checkpoint(
            [
                _operation_checkpoint(
                    "checkpoint-a",
                    checkpoint_ns="stage_a:1",
                    timestamp=timestamp,
                    step=4,
                ),
                _operation_checkpoint(
                    "checkpoint-b",
                    checkpoint_ns="stage_b:2",
                    timestamp=timestamp,
                    step=4,
                ),
            ]
        )


@pytest.mark.asyncio
async def test_reclaimed_start_receipt_continues_with_current_graph_context(monkeypatch):
    class ContinueGraph:
        def __init__(self) -> None:
            self.calls = []

        async def ainvoke(self, value, config, *, context, durability):
            self.calls.append((value, config, context, durability))

    def receipt(checkpoint_id: str):
        return _OperationReceipt(
            checkpoint=None,
            snapshot=SimpleNamespace(
                config={
                    "configurable": {
                        "thread_id": "thread-1",
                        "checkpoint_ns": "",
                        "checkpoint_id": checkpoint_id,
                    }
                }
            ),
            root_checkpoint_id=checkpoint_id,
        )

    first = receipt("checkpoint-pending")
    second = receipt("checkpoint-done")
    graph = ContinueGraph()
    saver = _FencedSaver()
    runtime = AsyncPostgresGraphRuntime(
        graph=graph,
        saver=object(),
        saver_context=_SaverContext(),
        fenced_saver=saver,
    )
    reads = 0

    async def operation_receipt(**_kwargs):
        nonlocal reads
        reads += 1
        return first if reads == 1 else second

    async def operation_result(item, *, thread_id):
        assert thread_id == "thread-1"
        checkpoint_id = item.snapshot.config["configurable"]["checkpoint_id"]
        return GraphExecutionResult(
            values={},
            checkpoint=GraphCheckpointDescriptor(
                thread_id="thread-1",
                checkpoint_id=checkpoint_id,
                next_nodes=("next",) if item is first else (),
            ),
        )

    monkeypatch.setattr(runtime, "_operation_receipt", operation_receipt)
    monkeypatch.setattr(runtime, "_operation_result", operation_result)
    grant = _grant()
    result = await runtime.start_durable(
        grant=grant,
        thread_id="thread-1",
        operation_id="run:run-1:start",
        target_run_id="run-1",
        input_value={"original": True},
    )

    assert result.checkpoint.checkpoint_id == "checkpoint-done"
    assert len(graph.calls) == 1
    value, config, context, durability = graph.calls[0]
    assert value is None
    assert config["configurable"]["checkpoint_id"] == "checkpoint-pending"
    assert context is grant.graph_context
    assert durability == "sync"
    assert saver.bound == [grant]


@pytest.mark.asyncio
async def test_reclaimed_nested_receipt_continues_root_pending_task_without_rewriting_leaf(
    monkeypatch,
):
    class ContinueGraph:
        def __init__(self) -> None:
            self.reads = []
            self.calls = []
            self.root_id = "pending-root"
            self.task_ns = "role:old-task"

        async def aget_state(self, config):
            self.reads.append(config)
            return SimpleNamespace(
                config={"configurable": {
                    "thread_id": "thread-1", "checkpoint_ns": "",
                    "checkpoint_id": self.root_id,
                }},
                created_at="2026-07-11T00:00:00Z",
                next=("role",), interrupts=(),
                tasks=(SimpleNamespace(
                    name="role",
                    state={"configurable": {"checkpoint_ns": self.task_ns}},
                ),),
            )

        async def ainvoke(self, value, config, *, context, durability):
            self.calls.append((value, config, context, durability))

    leaf_config = {
        "thread_id": "thread-1",
        "checkpoint_ns": "role:old-task",
        "checkpoint_id": "saved-result",
        "checkpoint_map": {"": "pending-root", "role:old-task": "saved-result"},
    }
    first = _OperationReceipt(
        checkpoint=None,
        snapshot=SimpleNamespace(config={"configurable": leaf_config}),
        root_checkpoint_id="pending-root",
    )
    second = _OperationReceipt(
        checkpoint=None,
        snapshot=SimpleNamespace(config={
            "configurable": {
                "thread_id": "thread-1", "checkpoint_ns": "", "checkpoint_id": "done-root"
            }
        }),
        root_checkpoint_id="done-root",
    )
    graph = ContinueGraph()

    class PendingSaver(_FencedSaver):
        child_id = "saved-result"

        async def aget_tuple(self, config):
            assert config == {"configurable": {
                "thread_id": "thread-1", "checkpoint_ns": "role:old-task"
            }}
            return SimpleNamespace(
                config={"configurable": {**leaf_config, "checkpoint_id": self.child_id}},
                metadata={
                    "cad2bim_resume_operation_id": "run:run-1:start",
                    "cad2bim_target_run_id": "run-1",
                    "cad2bim_source_checkpoint_id": "__start__",
                },
            )

    saver = PendingSaver()
    runtime = AsyncPostgresGraphRuntime(
        graph=graph, saver=saver, saver_context=_SaverContext(), fenced_saver=saver,
    )
    receipts = iter((first, second))

    async def operation_receipt(**_kwargs):
        return next(receipts)

    async def operation_result(item, *, thread_id):
        assert thread_id == "thread-1"
        checkpoint_id = item.snapshot.config["configurable"]["checkpoint_id"]
        return GraphExecutionResult(
            values={},
            checkpoint=GraphCheckpointDescriptor(
                thread_id="thread-1", checkpoint_id=checkpoint_id,
                next_nodes=("continue",) if item is first else (),
            ),
        )

    monkeypatch.setattr(runtime, "_operation_receipt", operation_receipt)
    monkeypatch.setattr(runtime, "_operation_result", operation_result)
    grant = _grant()
    result = await runtime.start_durable(
        grant=grant, thread_id="thread-1", operation_id="run:run-1:start",
        target_run_id="run-1", input_value={"should_not_restart": True},
    )

    assert result.checkpoint.checkpoint_id == "done-root"
    assert graph.reads == [{"configurable": {
        "thread_id": "thread-1", "checkpoint_ns": ""
    }}]
    assert len(graph.calls) == 1
    value, config, context, durability = graph.calls[0]
    assert value is None
    assert config["configurable"] == {
        "thread_id": "thread-1", "checkpoint_ns": ""
    }
    assert config["metadata"]["cad2bim_resume_operation_id"] == "run:run-1:start"
    assert context is grant.graph_context and durability == "sync"
    assert first.snapshot.config["configurable"] == leaf_config

    metadata = runtime._operation_metadata(
        operation_id="run:run-1:start", target_run_id="run-1",
        source_checkpoint_id="__start__",
    )
    graph.root_id = "different-root"
    with pytest.raises(RuntimeError, match="pending child root changed"):
        await runtime._live_nested_continuation_config(
            first, thread_id="thread-1", metadata=metadata
        )
    graph.root_id = "pending-root"
    graph.task_ns = "role:another-task"
    with pytest.raises(RuntimeError, match="no longer owns the exact child task"):
        await runtime._live_nested_continuation_config(
            first, thread_id="thread-1", metadata=metadata
        )
    graph.task_ns = "role:old-task"
    saver.child_id = "different-child"
    with pytest.raises(RuntimeError, match="pending child changed"):
        await runtime._live_nested_continuation_config(
            first, thread_id="thread-1", metadata=metadata
        )
    assert len(graph.calls) == 1


async def _resumed_child_with_later_failure():
    """Keep the real parent's stale pause after its child durably advances."""

    class State(TypedDict):
        value: int

    calls = {"pause": 0, "after_pause": 0, "fail": True}

    def pause(state):
        calls["pause"] += 1
        interrupt({"kind": "run_pause"})
        return {"value": state["value"] + 1}

    def after_pause(state):
        calls["after_pause"] += 1
        if calls["fail"]:
            raise RuntimeError("later child node failed")
        return {"value": state["value"] + 1}

    child_builder = StateGraph(State)
    child_builder.add_node("pause", pause)
    child_builder.add_node("after_pause", after_pause)
    child_builder.add_edge(START, "pause")
    child_builder.add_edge("pause", "after_pause")
    child_builder.add_edge("after_pause", END)
    child = child_builder.compile()

    async def role(state, config):
        return await child.ainvoke(state, config=config)

    saver = MemorySaver()
    parent = StateGraph(State)
    parent.add_node("role", role)
    parent.add_edge(START, "role")
    parent.add_edge("role", END)
    graph = parent.compile(checkpointer=saver)
    runtime = AsyncPostgresGraphRuntime(
        graph=graph, saver=saver, saver_context=_SaverContext(),
    )
    config = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}}
    await graph.ainvoke({"value": 0}, config)
    paused_parent = await graph.aget_state(config)
    paused_child = await graph.aget_state(paused_parent.tasks[0].state)
    metadata = runtime._operation_metadata(
        operation_id="continue-original", target_run_id="run-1",
        source_checkpoint_id=paused_child.config["configurable"]["checkpoint_id"],
    )
    with pytest.raises(RuntimeError, match="later child node failed"):
        await graph.ainvoke(Command(resume={paused_child.interrupts[0].id: "continue"}),
            {**config, "metadata": metadata})
    receipt = await runtime._operation_receipt(thread_id="thread-1", metadata=metadata)
    return runtime, receipt, metadata, calls


@pytest.mark.asyncio
async def test_nested_resume_continues_after_consumed_parent_pause_and_later_child_failure():
    runtime, receipt, metadata, calls = await _resumed_child_with_later_failure()
    root = await runtime._graph.aget_state({"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}})
    assert root.interrupts and root.tasks[0].interrupts == root.interrupts
    assert receipt.snapshot.interrupts == ()
    assert receipt.snapshot.next == ("after_pause",)
    assert receipt.root_checkpoint_id == root.config["configurable"]["checkpoint_id"]

    config = await runtime._live_nested_continuation_config(
        receipt, thread_id="thread-1", metadata=metadata,
    )
    assert config["configurable"] == {"thread_id": "thread-1", "checkpoint_ns": ""}
    calls["fail"] = False
    result = await runtime._graph.ainvoke(None, config)
    assert result == {"value": 2}
    assert calls["pause"] == 2  # interrupt once and consume its original resume once
    assert calls["after_pause"] == 2  # failed once, then continued from that node


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["new_interrupt", "other_parent_task", "live_child", "late_child_interrupt", "missing_resume", "wrong_interrupt_id"])
async def test_nested_resume_rejects_parent_interrupt_without_exact_consumption(monkeypatch, mismatch):
    runtime, receipt, metadata, _calls = await _resumed_child_with_later_failure()
    graph, saver = runtime._graph, runtime._saver
    root = await graph.aget_state({"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}})
    extra = Interrupt(value={"kind": "run_pause"}, id="another-interrupt")
    if mismatch == "new_interrupt":
        changed_task = root.tasks[0]._replace(interrupts=(*root.tasks[0].interrupts, extra))
        root = root._replace(interrupts=(*root.interrupts, extra), tasks=(changed_task,))
    elif mismatch == "other_parent_task":
        other = root.tasks[0]._replace(id="other-task", name="other", state=None)
        original = root.tasks[0]._replace(interrupts=())
        root = root._replace(tasks=(original, other))
    elif mismatch == "live_child":
        receipt = _OperationReceipt(receipt.checkpoint,
            receipt.snapshot._replace(interrupts=(extra,)), receipt.root_checkpoint_id)
    elif mismatch == "late_child_interrupt":
        original_get = saver.aget_tuple

        async def interrupted_latest(config):
            saved = await original_get(config)
            if not config["configurable"].get("checkpoint_id"):
                saved = saved._replace(pending_writes=[*saved.pending_writes,
                    ("late-task", "__interrupt__", [extra])])
            return saved

        monkeypatch.setattr(saver, "aget_tuple", interrupted_latest)
    else:
        original_get = saver.aget_tuple

        async def changed_source(config):
            saved = await original_get(config)
            if config["configurable"].get("checkpoint_id") == metadata["cad2bim_source_checkpoint_id"]:
                writes = [item for item in saved.pending_writes if item[1] != "__resume__"] if mismatch == "missing_resume" else [
                    (task_id, channel, [extra] if channel == "__interrupt__" else value)
                    for task_id, channel, value in saved.pending_writes
                ]
                saved = saved._replace(pending_writes=writes)
            return saved

        monkeypatch.setattr(saver, "aget_tuple", changed_source)

    async def current_parent(_config):
        return root

    monkeypatch.setattr(graph, "aget_state", current_parent)
    with pytest.raises(RuntimeError, match="pending parent interrupt"):
        await runtime._live_nested_continuation_config(receipt, thread_id="thread-1", metadata=metadata)


@pytest.mark.asyncio
async def test_resume_receipt_probe_does_not_continue_pending_graph_work(monkeypatch):
    reads: list[tuple[dict, bool]] = []

    class ReceiptGraph:
        async def aget_state(self, config, *, subgraphs=False):
            reads.append((config, subgraphs))
            return snapshot

    runtime = AsyncPostgresGraphRuntime(
        graph=ReceiptGraph(),
        saver=object(),
        saver_context=_SaverContext(),
        fenced_saver=_FencedSaver(),
        database_url="postgresql+psycopg://runner@db/cad2bim",
    )
    snapshot = SimpleNamespace(
        config={
            "configurable": {
                "thread_id": "thread-1",
                "checkpoint_ns": "",
                "checkpoint_id": "checkpoint-receipt",
            }
        },
        next=("modeler",),
        interrupts=(),
        values={"mcp_session_id": "restored-session"},
        created_at="2026-07-11T00:00:00Z",
        tasks=(),
    )

    async def operation_receipt(**_kwargs):
        return _OperationReceipt(
            checkpoint=None,
            snapshot=snapshot,
            root_checkpoint_id="checkpoint-receipt",
        )

    async def forbidden_continue(*_args, **_kwargs):
        raise AssertionError("read-only receipt probe must not continue Graph nodes")

    monkeypatch.setattr(runtime, "_operation_receipt", operation_receipt)
    monkeypatch.setattr(runtime, "_reconcile_operation_receipt", forbidden_continue)

    result = await runtime.reconcile_durable_resume(
        grant=_grant(run_id="run-target"),
        thread_id="thread-1",
        source_checkpoint_id="checkpoint-source",
        operation_id="resume-operation-1",
        target_run_id="run-target",
    )

    assert result is not None
    assert result.checkpoint.checkpoint_id == "checkpoint-receipt"
    assert result.checkpoint.next_nodes == ("modeler",)
    assert reads == [
        (
            {
                "configurable": {
                    "thread_id": "thread-1",
                    "checkpoint_ns": "",
                    "checkpoint_id": "checkpoint-receipt",
                }
            },
            False,
        )
    ]
