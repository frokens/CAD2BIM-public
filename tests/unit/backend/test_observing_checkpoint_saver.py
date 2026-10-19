from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any
from typing import TypedDict
import asyncio
import json

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from CAD2BIM_backend.infrastructure.langgraph.observing_saver import (
    ObservingAsyncCheckpointSaver,
)
from CAD2BIM_backend.ports.checkpoint_observers import DurableCheckpointCommit


class RecordingObserver:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.commits: list[DurableCheckpointCommit] = []

    async def checkpoint_committed(self, commit: DurableCheckpointCommit) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("projection observer unavailable")
        self.commits.append(commit)


class FakeAsyncSaver(BaseCheckpointSaver[str]):
    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str, str], CheckpointTuple] = {}
        self.writes: list[tuple[str, Sequence[tuple[str, Any]]]] = []
        self.deleted_threads: list[str] = []

    def get_next_version(self, current: str | None, channel: None) -> str:
        del channel
        return str(int(current or "0") + 1)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        del new_versions
        configurable = dict(config.get("configurable") or {})
        configurable["checkpoint_id"] = checkpoint["id"]
        persisted = {"configurable": configurable}
        configured_metadata = dict(config.get("metadata") or {})
        stored_metadata = {**configured_metadata, **dict(metadata)}
        item = CheckpointTuple(
            config=persisted,
            checkpoint=checkpoint,
            metadata=stored_metadata,
            parent_config=config if config.get("configurable", {}).get("checkpoint_id") else None,
        )
        key = (
            str(configurable["thread_id"]),
            str(configurable.get("checkpoint_ns") or ""),
            str(configurable["checkpoint_id"]),
        )
        self.items[key] = item
        return persisted

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        del config, task_path
        self.writes.append((task_id, writes))

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        configurable = config.get("configurable") or {}
        return self.items.get(
            (
                str(configurable.get("thread_id") or ""),
                str(configurable.get("checkpoint_ns") or ""),
                str(configurable.get("checkpoint_id") or ""),
            )
        )

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        del before
        thread_id = str((config or {}).get("configurable", {}).get("thread_id") or "")
        matched = [
            item
            for (item_thread, _, _), item in self.items.items()
            if (not thread_id or item_thread == thread_id)
            and all(item.metadata.get(key) == value for key, value in (filter or {}).items())
        ]
        for item in matched[:limit]:
            yield item

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted_threads.append(thread_id)
        self.items = {
            key: item for key, item in self.items.items() if key[0] != thread_id
        }


def _config(*, target_run_id: str = "run-1") -> RunnableConfig:
    return {
        "configurable": {"thread_id": "thread-1", "checkpoint_ns": ""},
        "metadata": {
            "cad2bim_target_run_id": target_run_id,
            "cad2bim_resume_operation_id": "operation-1",
        },
    }


def _checkpoint(checkpoint_id: str = "checkpoint-1") -> Checkpoint:
    return {
        "v": 4,
        "id": checkpoint_id,
        "ts": "2026-07-12T12:00:00+00:00",
        "channel_values": {"active_module": "requirement", "plan": {"version": 1}},
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": None,
    }


@pytest.mark.asyncio
async def test_saver_without_observer_preserves_storage_and_can_bind_once() -> None:
    delegate = FakeAsyncSaver()
    saver = ObservingAsyncCheckpointSaver(delegate)
    persisted = await saver.aput(_config(), _checkpoint(), {"source": "loop", "step": 1}, {})
    assert await saver.aget_tuple(persisted) is not None
    observer = RecordingObserver()
    saver.bind_observer(observer)
    with pytest.raises(RuntimeError, match="already bound"):
        saver.bind_observer(RecordingObserver())
    assert await saver.aget_tuple(persisted) is not None
    assert [commit.checkpoint_id for commit in observer.commits] == ["checkpoint-1"]


@pytest.mark.asyncio
async def test_actual_nested_checkpoint_distinguishes_summary_updates_from_copies(tmp_path) -> None:
    class State(TypedDict):
        conversation_summary: str
        tool_chain_summary: str
        tick: int

    child = StateGraph(State)
    child.add_node("compress_memory", lambda state: {
        "conversation_summary": "Keep the user's geometry constraints.",
        "tool_chain_summary": "A saved preview was inspected.",
    })
    child.add_node("after_compression", lambda state: {"tick": state["tick"] + 1})
    child.add_edge(START, "compress_memory")
    child.add_edge("compress_memory", "after_compression")
    child.add_edge("after_compression", END)
    parent = StateGraph(State)
    parent.add_node("component_role", child.compile())
    parent.add_edge(START, "component_role")
    parent.add_edge("component_role", END)
    delegate = MemorySaver()
    observer = RecordingObserver()
    saver = ObservingAsyncCheckpointSaver(delegate, observer)
    app = parent.compile(checkpointer=saver)
    await app.ainvoke({
        "conversation_summary": "Prior conversation summary.",
        "tool_chain_summary": "Prior tool-chain summary.", "tick": 0,
    }, _config(), durability="sync")
    summary_channels = {"conversation_summary", "tool_chain_summary"}
    summary_writes = [commit for commit in observer.commits if (
        commit.checkpoint_ns
        and summary_channels.issubset(commit.updated_channels)
        and commit.values.get("conversation_summary")
        and commit.values.get("tool_chain_summary")
    )]
    assert len(summary_writes) == 2
    assert summary_writes[0].step == 0 and summary_writes[0].source == "loop"
    assert summary_writes[0].values["conversation_summary"] == "Prior conversation summary."
    summary_commits = [commit for commit in summary_writes if commit.step is not None and commit.step > 0]
    assert len(summary_commits) == 1
    commit = summary_commits[0]
    native = await delegate.aget_tuple({"configurable": {
        "thread_id": commit.thread_id, "checkpoint_ns": commit.checkpoint_ns,
        "checkpoint_id": commit.checkpoint_id,
    }})
    assert native is not None
    assert commit.updated_channels == tuple(native.checkpoint["updated_channels"])
    assert any(
        other.checkpoint_ns == commit.checkpoint_ns
        and other.values.get("conversation_summary") == commit.values["conversation_summary"]
        and "tick" in other.updated_channels
        and summary_channels.isdisjoint(other.updated_channels)
        for other in observer.commits
    )
    (tmp_path / "native_summary_channels.json").write_text(json.dumps([
        {"checkpoint_id": item.checkpoint_id, "checkpoint_ns": item.checkpoint_ns,
         "updated_channels": item.updated_channels, "source": item.source, "step": item.step}
        for item in observer.commits
    ], indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_observer_runs_only_after_delegate_persists_checkpoint() -> None:
    delegate = FakeAsyncSaver()
    observer = RecordingObserver()
    saver = ObservingAsyncCheckpointSaver(delegate, observer)

    persisted = await saver.aput(
        _config(),
        _checkpoint(),
        {"source": "loop", "step": 3},
        {},
    )

    assert await delegate.aget_tuple(persisted) is not None
    assert len(observer.commits) == 1
    commit = observer.commits[0]
    assert commit.target_run_id == "run-1"
    assert commit.checkpoint_id == "checkpoint-1"
    assert commit.root_checkpoint_id == "checkpoint-1"
    assert commit.checkpoint_map == (("", "checkpoint-1"),)
    assert commit.source == "loop" and commit.step == 3
    assert commit.operation_id == "operation-1"
    assert commit.values["plan"] == {"version": 1}


@pytest.mark.asyncio
async def test_historical_read_retries_observer_after_post_commit_failure() -> None:
    delegate = FakeAsyncSaver()
    observer = RecordingObserver(fail_once=True)
    saver = ObservingAsyncCheckpointSaver(delegate, observer)

    with pytest.raises(RuntimeError, match="observer unavailable"):
        await saver.aput(_config(), _checkpoint(), {"source": "loop", "step": 1}, {})

    persisted_config = {
        "configurable": {
            "thread_id": "thread-1",
            "checkpoint_ns": "",
            "checkpoint_id": "checkpoint-1",
        }
    }
    assert await saver.aget_tuple(persisted_config) is not None
    assert [item.checkpoint_id for item in observer.commits] == ["checkpoint-1"]


@pytest.mark.asyncio
async def test_display_read_scope_is_task_local_and_preserves_write_and_repair_notifications() -> None:
    delegate = FakeAsyncSaver()
    observer = RecordingObserver()
    saver = ObservingAsyncCheckpointSaver(delegate, observer)
    persisted = await saver.aput(_config(), _checkpoint(), {"source": "loop", "step": 1}, {})
    observer.commits.clear()
    opened, release = asyncio.Event(), asyncio.Event()

    async def display_read():
        with saver.without_read_observation():
            opened.set()
            await release.wait()
            assert await saver.aget_tuple(persisted) is not None
            assert len([item async for item in saver.alist(persisted)]) == 1
            # Suppressing read notifications never suppresses a durable write.
            await saver.aput(_config(), _checkpoint("checkpoint-2"), {"source": "loop", "step": 2}, {})

    display = asyncio.create_task(display_read())
    await opened.wait()
    assert await saver.aget_tuple(persisted) is not None
    assert [item.checkpoint_id for item in observer.commits] == ["checkpoint-1"]
    release.set()
    await display
    assert [item.checkpoint_id for item in observer.commits] == ["checkpoint-1", "checkpoint-2"]
    with pytest.raises(RuntimeError, match="display interrupted"):
        with saver.without_read_observation():
            raise RuntimeError("display interrupted")
    assert await saver.aget_tuple(persisted) is not None
    assert observer.commits[-1].checkpoint_id == "checkpoint-1"


@pytest.mark.asyncio
async def test_volatile_checkpoint_is_not_projected() -> None:
    delegate = FakeAsyncSaver()
    observer = RecordingObserver()
    saver = ObservingAsyncCheckpointSaver(delegate, observer)

    await saver.aput(
        _config(target_run_id="__volatile__"),
        _checkpoint(),
        {"source": "input", "step": -1},
        {},
    )

    assert observer.commits == []


@pytest.mark.asyncio
async def test_nested_checkpoint_requires_and_preserves_exact_branch_map() -> None:
    delegate = FakeAsyncSaver()
    observer = RecordingObserver()
    saver = ObservingAsyncCheckpointSaver(delegate, observer)
    config = _config()
    config["configurable"] = {
        "thread_id": "thread-1",
        "checkpoint_ns": "cad_graph:child",
        "checkpoint_map": {
            "": "checkpoint-root",
            "cad_graph:child": "checkpoint-child",
        },
    }

    await saver.aput(
        config,
        _checkpoint("checkpoint-child"),
        {"source": "loop", "step": 5},
        {},
    )

    commit = observer.commits[0]
    assert commit.checkpoint_ns == "cad_graph:child"
    assert commit.root_checkpoint_id == "checkpoint-root"
    assert dict(commit.checkpoint_map)["cad_graph:child"] == "checkpoint-child"


@pytest.mark.asyncio
async def test_nested_checkpoint_rebuilds_branch_map_from_persisted_parent_metadata() -> None:
    delegate = FakeAsyncSaver()
    observer = RecordingObserver()
    saver = ObservingAsyncCheckpointSaver(delegate, observer)
    config = _config()
    config["configurable"] = {
        "thread_id": "thread-1",
        "checkpoint_ns": "cad_graph:child",
    }

    persisted = await saver.aput(
        config,
        _checkpoint("checkpoint-child"),
        {
            "source": "loop",
            "step": 5,
            "parents": {"": "checkpoint-root"},
        },
        {},
    )
    commit = observer.commits[0]
    assert commit.root_checkpoint_id == "checkpoint-root"
    assert commit.checkpoint_map == (
        ("", "checkpoint-root"),
        ("cad_graph:child", "checkpoint-child"),
    )

    observer.commits.clear()
    assert await saver.aget_tuple(persisted) is not None
    assert observer.commits[0].checkpoint_map == commit.checkpoint_map


@pytest.mark.asyncio
async def test_nested_checkpoint_advances_active_branch_from_persisted_parent() -> None:
    delegate = FakeAsyncSaver()
    observer = RecordingObserver()
    saver = ObservingAsyncCheckpointSaver(delegate, observer)
    config = _config()
    config["configurable"] = {
        "thread_id": "thread-1",
        "checkpoint_ns": "cad_graph:child",
    }

    await saver.aput(
        config,
        _checkpoint("checkpoint-child-next"),
        {
            "source": "loop",
            "step": 6,
            "parents": {
                "": "checkpoint-root",
                "cad_graph:child": "checkpoint-child-previous",
            },
        },
        {},
    )

    commit = observer.commits[0]
    assert commit.root_checkpoint_id == "checkpoint-root"
    assert commit.checkpoint_map == (
        ("", "checkpoint-root"),
        ("cad_graph:child", "checkpoint-child-next"),
    )


@pytest.mark.asyncio
async def test_root_checkpoint_advances_root_from_persisted_parent() -> None:
    delegate = FakeAsyncSaver()
    observer = RecordingObserver()
    saver = ObservingAsyncCheckpointSaver(delegate, observer)

    await saver.aput(
        _config(),
        _checkpoint("checkpoint-root-next"),
        {
            "source": "loop",
            "step": 7,
            "parents": {"": "checkpoint-root-previous"},
        },
        {},
    )

    commit = observer.commits[0]
    assert commit.root_checkpoint_id == "checkpoint-root-next"
    assert commit.checkpoint_map == (("", "checkpoint-root-next"),)


@pytest.mark.asyncio
async def test_non_checkpoint_operations_are_transparently_delegated() -> None:
    delegate = FakeAsyncSaver()
    saver = ObservingAsyncCheckpointSaver(delegate, RecordingObserver())

    await saver.aput_writes(_config(), (("channel", {"value": 1}),), "task-1")
    await saver.adelete_thread("thread-1")

    assert delegate.writes == [("task-1", (("channel", {"value": 1}),))]
    assert delegate.deleted_threads == ["thread-1"]
