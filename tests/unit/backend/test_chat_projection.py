from __future__ import annotations

import base64
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Command

from CAD2BIM_backend.application.chat_projection import public_text
from CAD2BIM_backend.domain.control_plane import ResourceNotFoundError, RunRecord, RunStatus, UserTextContent
from CAD2BIM_backend.application.task_run_service import TaskRunService
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.run_events import RunEvent, RunEventAudience, RunEventScope, RunEventType
from CAD2BIM_backend.infrastructure.chat_stream import MainDeltaBridge, MainTextDeltaCallback, create_component_presentation
from CAD2BIM_backend.infrastructure.agent_skills import AgentSkills
from CAD2BIM_backend.infrastructure.langgraph.chat_observer import message_item_id, tool_item_id
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context, current_tenant_execution_context
from CAD2BIM_backend.ports.checkpoint_observers import DurableCheckpointCommit


NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)


class OriginalSources:
    def __init__(self):
        self.run = RunRecord(
            run_id="run", task_id="task", owner_id="tenant", tenant_id="tenant",
            module="component", stage_key="component", idempotency_key="request",
            thread_id="thread", status=RunStatus.RUNNING, created_at=NOW, updated_at=NOW,
        )
        self.identity = SimpleNamespace(tenant_id="tenant")
        self.task = SimpleNamespace(task_id="task", title="Component")
        self.events = []
        self.states = {}
        self.reads = []
        self.feedbacks = {}
        self.authorized = True

    async def authorized_run_context(self, subject, task_id, run_id):
        if not self.authorized or (task_id, run_id) != ("task", "run"):
            raise ResourceNotFoundError("run not found")
        return self.identity, self.run

    async def checkpoint_observation_context(self, run_id, thread_id):
        assert (run_id, thread_id) == ("run", "thread")
        return self.identity, self.run

    async def get_run(self, owner_id, run_id):
        assert (owner_id, run_id) == ("tenant", "run")
        return self.run

    async def get_task(self, owner_id, task_id):
        assert (owner_id, task_id) == ("tenant", "task")
        return self.task

    async def append_chat_events(self, owner, run_id, events):
        by_id = {event.event_id: event for event in self.events}
        for event in events:
            if event.event_id not in by_id:
                stored = event.model_copy(update={"sequence": len(self.events) + 1})
                self.events.append(stored)
                by_id[stored.event_id] = stored

    async def get_item_events(self, owner, run_id, item_id):
        return tuple(event for event in self.events if event.item_id == item_id)

    async def read_chat_snapshot(self, owner, run_id, *, before_sequence, limit):
        selected = [event for event in self.events if event.audience is RunEventAudience.PUBLIC and (before_sequence is None or event.sequence < before_sequence)][-limit:]
        item_ids = {event.item_id for event in selected}
        histories = tuple(event for event in self.events if event.item_id in item_ids)
        activity = tuple(event for event in self.events if event.event_type.startswith(("runtime.activity.", "tool.call."))
                         or event.event_type == "run.status.changed")
        return self.run, histories, len(self.events), None, activity

    async def list_events(self, owner, run_id, *, after_sequence, limit):
        return tuple(event for event in self.events if event.sequence > after_sequence)[:limit]

    async def get_checkpoint_state(self, *, thread_id, checkpoint_id, checkpoint_ns, observe=True):
        assert observe is False, "display reads must not replay checkpoint notifications"
        self.reads.append((thread_id, checkpoint_id, checkpoint_ns))
        values = self.states.get((checkpoint_id, checkpoint_ns))
        if values is None:
            return None
        return SimpleNamespace(values=values, target_run_id="run", checkpoint=SimpleNamespace(
            thread_id=thread_id, checkpoint_id=checkpoint_id, checkpoint_ns=checkpoint_ns,
        ))

    async def get_feedback(self, owner, run_id, feedback_id):
        return self.feedbacks.get(feedback_id)

    def service(self):
        return create_component_presentation(
            task_runs=self, repository=self, graph_runtime=self, feedback_repository=self,
            artifact_service=None, bind_identity=lambda _: nullcontext(),
        )

    def commit(self, checkpoint_id, values, namespace="role:entry", *, source="loop", step=1, updated_channels=()):
        self.states[(checkpoint_id, namespace)] = values
        return DurableCheckpointCommit.model_construct(
            target_run_id="run", thread_id="thread", checkpoint_id=checkpoint_id,
            checkpoint_ns=namespace, created_at=NOW.isoformat(), values=values,
            source=source, step=step, updated_channels=updated_channels,
        )


@pytest.mark.asyncio
async def test_snapshot_reuses_exact_checkpoint_for_distinct_messages_only_within_request():
    sources = OriginalSources()
    service = sources.service()
    messages = [AIMessage(id="first", content="First saved answer"),
                AIMessage(id="second", content="Second saved answer")]
    await service.checkpoint_observer.checkpoint_committed(sources.commit("shared", {"messages": messages}))
    expected = [await service.item(None, "task", "run", message_item_id("run", message.id))
                for message in messages]
    sources.reads.clear()

    snapshot = await service.snapshot(None, "task", "run")
    assert snapshot["items"] == expected
    assert [item["text"] for item in snapshot["items"]] == [message.content for message in messages]
    assert sources.reads == [("thread", "shared", "role:entry")]

    assert (await service.snapshot(None, "task", "run"))["items"] == expected
    assert sources.reads == [("thread", "shared", "role:entry")] * 2


@pytest.mark.asyncio
async def test_snapshot_uses_one_repository_basis_without_per_item_or_activity_history_reads():
    sources = OriginalSources()
    service = sources.service()
    ai = AIMessage(id="main", content="Saved message", tool_calls=[{
        "id": "call", "name": "read", "args": {"saved": True},
    }])
    await service.checkpoint_observer.checkpoint_committed(sources.commit("saved", {"messages": [ai]}))
    await service.checkpoint_observer.activity_changed("tool-activity", "tools", "start", {
        "run_id": "run", "thread_id": "thread", "call_id": "call", "main_message_id": "main",
    })
    original_events = tuple(sources.events)

    async def unexpected_read(*args, **kwargs):
        raise AssertionError("snapshot repeated an item or event-history read")

    sources.get_item_events = sources.list_events = unexpected_read
    snapshot = await service.snapshot(None, "task", "run")
    tool = next(item for item in snapshot["items"] if item["kind"] == "tool")
    assert tool["status"] == "running" and tool["arguments"] == {"saved": True}
    assert snapshot["activity"]["call_id"] == "call"
    assert sources.reads == [("thread", "saved", "role:entry")]
    assert tuple(sources.events) == original_events


@pytest.mark.asyncio
async def test_repository_snapshot_batches_history_and_keeps_current_activity_cycle():
    from CAD2BIM_backend.infrastructure.memory.control_plane_repository import InMemoryControlPlaneRepository

    sources = OriginalSources()
    service = sources.service()
    ai = AIMessage(id="main", content="", tool_calls=[{"id": "old-call", "name": "read", "args": {"original": 1}}])
    await service.checkpoint_observer.checkpoint_committed(sources.commit("start", {"messages": [ai]}))
    old_tool = "old-tool-activity"
    await service.checkpoint_observer.activity_changed(old_tool, "tools", "start", {
        "run_id": "run", "thread_id": "thread", "call_id": "old-call", "main_message_id": "main",
    })
    await service.checkpoint_observer.activity_changed("old-model", "model", "start", {
        "run_id": "run", "thread_id": "thread", "main_message_id": "old-main",
    })
    boundary_before_pause = len(sources.events) + 1
    sources.events.append(RunEvent(event_id="pause-status", sequence=boundary_before_pause,
        task_id="task", run_id="run", event_type="run.status.changed", item_id="run-status",
        occurred_at=NOW, scope=RunEventScope(module="component"), status=RunEventType.RUNNING,
        attributes={"run_status": "paused"}))
    for index in range(110):
        sources.events.append(RunEvent(event_id=f"context-{index}", sequence=len(sources.events) + 1,
            task_id="task", run_id="run", event_type="context.loaded", item_id=f"context-{index}",
            occurred_at=NOW, scope=RunEventScope(module="component"), status=RunEventType.SUCCEEDED))
    await service.checkpoint_observer.activity_changed("current-model", "model", "start", {
        "run_id": "run", "thread_id": "thread", "main_message_id": "new-main",
    })
    tool = ToolMessage(id="result", tool_call_id="old-call", content="Saved result")
    await service.checkpoint_observer.checkpoint_committed(sources.commit("result", {"messages": [tool]}))
    repository = InMemoryControlPlaneRepository()
    repository._runs["run"] = sources.run
    repository._tasks["task"] = SimpleNamespace(owner_id="tenant", status="active", task_id="task", title="Component")
    repository._events["run"] = list(sources.events)
    projection = create_component_presentation(task_runs=sources, repository=repository, graph_runtime=sources,
        feedback_repository=sources, artifact_service=None, bind_identity=lambda _: nullcontext())
    recent = await projection.snapshot(None, "task", "run", limit=1)
    assert recent["items"][0]["arguments"] == {"original": 1}
    assert recent["items"][0]["text"] == "Saved result"
    assert recent["activity"]["activity_id"] == "current-model"
    assert recent["next_before_sequence"] == len(sources.events)
    older = await projection.snapshot(None, "task", "run", before_sequence=boundary_before_pause, limit=3)
    assert older["activity"] == recent["activity"]
    assert older["items"][0]["text"] == "Saved result"
    _, _, _, _, activities = await repository.read_chat_snapshot("tenant", "run", limit=1)
    assert "old-model" not in {event.attributes.get("activity_id") for event in activities}
    assert old_tool in {event.attributes.get("activity_id") for event in activities}
    assert tuple(repository._events["run"]) == tuple(sources.events)


@pytest.mark.asyncio
@pytest.mark.parametrize(("other_id", "other_namespace"), [
    ("same", "role:other"), ("other", "role:entry"),
])
async def test_snapshot_checkpoint_reuse_keeps_namespace_and_id_distinct(other_id, other_namespace):
    sources = OriginalSources()
    service = sources.service()
    for checkpoint_id, namespace, message in (
        ("same", "role:entry", AIMessage(id="first", content="Entry checkpoint")),
        (other_id, other_namespace, AIMessage(id="second", content="Different checkpoint")),
    ):
        await service.checkpoint_observer.checkpoint_committed(
            sources.commit(checkpoint_id, {"messages": [message]}, namespace=namespace),
        )

    snapshot = await service.snapshot(None, "task", "run")
    assert [(item["message_id"], item["text"]) for item in snapshot["items"]] == [
        ("first", "Entry checkpoint"), ("second", "Different checkpoint"),
    ]
    assert sources.reads == [("thread", "same", "role:entry"),
                             ("thread", other_id, other_namespace)]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_snapshot_checkpoint_read_failures_still_propagate(missing):
    class FailedSources(OriginalSources):
        async def get_checkpoint_state(self, **kwargs):
            state = await super().get_checkpoint_state(**kwargs)
            if missing:
                return None
            raise RuntimeError("checkpoint read interrupted")

    sources = FailedSources()
    service = sources.service()
    await service.checkpoint_observer.checkpoint_committed(sources.commit("saved", {"messages": [
        AIMessage(id="first", content="First"), AIMessage(id="second", content="Second"),
    ]}))
    error, reason = (ResourceNotFoundError, "saved chat checkpoint not found") if missing else (
        RuntimeError, "checkpoint read interrupted",
    )
    with pytest.raises(error, match=reason):
        await service.snapshot(None, "task", "run")
    assert sources.reads == [("thread", "saved", "role:entry")]


@pytest.mark.asyncio
async def test_runtime_activity_uses_public_events_and_survives_refresh_and_late_old_end():
    sources = OriginalSources()
    service = sources.service()
    callback = service.callback_factory()
    initial, title, main = uuid4(), uuid4(), uuid4()
    metadata = {"run_id": "run", "thread_id": "thread"}
    await callback.on_chain_start(None, {}, run_id=initial, name="initialize_component",
                                  metadata={**metadata, "langgraph_node": "initialize_component"})
    assert (await service.snapshot(None, "task", "run"))["activity"]["stage"] == "initializing"
    await callback.on_chat_model_start({}, [], run_id=title, tags=["role_title"], metadata=metadata)
    snapshot = await service.snapshot(None, "task", "run", before_sequence=1)
    assert snapshot["activity"]["stage"] == "title" and snapshot["items"] == []
    await callback.on_llm_new_token("PRIVATE TITLE RESPONSE", run_id=title)
    await callback.on_llm_end(None, run_id=title)
    await callback.on_chat_model_start({}, [], run_id=main, tags=["role_main"],
                                      metadata={**metadata, "main_message_id": "main", "model_continuation": True})
    await callback.on_chain_end({}, run_id=initial)
    snapshot = await service.snapshot(None, "task", "run")
    assert snapshot["activity"]["activity_id"] == str(main)
    assert snapshot["activity"]["label"] == "模型继续思考"
    stream = service.stream(None, "task", "run", after_sequence=snapshot["activity"]["event_sequence"] - 1)
    event = await anext(stream)
    assert event.startswith(f'id: {snapshot["activity"]["event_sequence"]}\nevent: activity\n')
    assert json.loads(event.split("data: ", 1)[1]) == snapshot["activity"]
    await stream.aclose()
    assert "PRIVATE" not in json.dumps([event.model_dump(mode="json") for event in sources.events])
    await callback.on_llm_end(None, run_id=main)
    assert (await service.snapshot(None, "task", "run"))["activity"]["phase"] == "end"
    sources.run = sources.run.model_copy(update={"status": RunStatus.WAITING_USER})
    assert (await service.snapshot(None, "task", "run"))["activity"] is None


@pytest.mark.asyncio
async def test_pause_resume_boundary_does_not_resurrect_unclosed_old_model_after_recent_page():
    sources = OriginalSources()
    service = sources.service()
    callback = service.callback_factory()
    metadata = {"run_id": "run", "thread_id": "thread", "main_message_id": "old-main"}
    await callback.on_chat_model_start({}, [], run_id=uuid4(), tags=["role_main"], metadata=metadata)
    for status in ("paused", "running"):
        sources.events.append(RunEvent(event_id=f"status-{status}", sequence=len(sources.events) + 1,
            task_id="task", run_id="run", event_type="run.status.changed", item_id=f"run-{status}",
            occurred_at=NOW, scope=RunEventScope(module="component"), status=RunEventType.RUNNING,
            attributes={"run_status": status}))
    for index in range(110):
        sources.events.append(RunEvent(event_id=f"other-{index}", sequence=len(sources.events) + 1,
            task_id="task", run_id="run", event_type="context.loaded", item_id=f"context-{index}",
            occurred_at=NOW, scope=RunEventScope(module="component"), status=RunEventType.SUCCEEDED))
    resumed = uuid4()
    await callback.on_chat_model_start({}, [], run_id=resumed, tags=["role_main"],
                                      metadata={**metadata, "main_message_id": "new-main"})
    await callback.on_llm_end(None, run_id=resumed)
    snapshot = await service.snapshot(None, "task", "run")
    assert snapshot["activity"]["activity_id"] == str(resumed)
    assert snapshot["activity"]["phase"] == "end"


@pytest.mark.asyncio
async def test_tool_argument_activity_records_only_stage_changes_and_retry_resets_transient_text():
    sources = OriginalSources()
    service = sources.service()
    callback = service.callback_factory()
    call = uuid4()
    metadata = {"run_id": "run", "thread_id": "thread", "main_message_id": "main"}
    async with service.delta_bridge.subscribe("run") as subscription:
        await callback.on_chat_model_start({}, [], run_id=call, tags=["role_main"], metadata=metadata)
        subscription.queue.get_nowait()
        await callback.on_llm_new_token("Partial answer", run_id=call)
        await callback.on_llm_new_token("", run_id=call, activity_stage="retry", retry_attempt=2, reset_text=True)
        reset = subscription.queue.get_nowait()
        assert reset["phase"] == "retry" and reset["message_id"] == "main" and reset["text"] == ""
        assert subscription.queue.empty()
        assert (await service.snapshot(None, "task", "run"))["activity"]["retry_attempt"] == 2
        await callback.on_llm_new_token("Recovered answer.", run_id=call)
        resumed = subscription.queue.get_nowait()
        assert resumed["text"] == "Recovered answer." and resumed["delta_index"] == 0
        assert (await service.snapshot(None, "task", "run"))["activity"]["stage"] == "model"
        await callback.on_llm_new_token("", run_id=call, activity_stage="tool_arguments")
        before = len(sources.events)
        for _ in range(4):
            await callback.on_llm_new_token("", run_id=call, activity_stage="tool_arguments")
        assert len(sources.events) == before
        assert (await service.snapshot(None, "task", "run"))["activity"]["stage"] == "tool_arguments"
        await callback.on_llm_error(RuntimeError("PRIVATE transport detail"), run_id=call)
        snapshot = await service.snapshot(None, "task", "run")
        assert snapshot["activity"]["phase"] == "error"
        assert "PRIVATE" not in json.dumps(snapshot)
        assert not callback._runtime_activities and not callback._chain_parents


@pytest.mark.asyncio
async def test_actual_tool_callback_separates_saved_proposal_execution_and_return():
    from langchain_core.tools import StructuredTool

    sources = OriginalSources()
    service = sources.service()
    callback = service.callback_factory()
    proposal = AIMessage(id="main", content="", tool_calls=[{"id": "actual-call", "name": "read_actual", "args": {}}])
    await service.checkpoint_observer.checkpoint_committed(sources.commit("proposal", {"messages": [proposal]}))
    assert (await service.snapshot(None, "task", "run"))["activity"]["label"] == "工具调用已准备，等待执行"
    assert (await service.item(None, "task", "run", tool_item_id("run", "actual-call")))["status"] == "queued"

    async def read_actual() -> str:
        """Read a representative in-memory value without side effects."""
        activity = (await service.snapshot(None, "task", "run"))["activity"]
        assert activity["stage"] == "tools" and activity["label"] == "正在执行工具"
        assert activity["call_id"] == "actual-call"
        assert (await service.item(None, "task", "run", tool_item_id("run", "actual-call")))["status"] == "running"
        return "PRIVATE RESULT REMAINS IN ORIGINAL MESSAGE"

    tool = StructuredTool.from_function(coroutine=read_actual)
    result = await tool.ainvoke({"id": "actual-call", "name": "read_actual", "args": {}, "type": "tool_call"},
                               config={"callbacks": [callback], "metadata": {"run_id": "run", "thread_id": "thread"}})
    activity = (await service.snapshot(None, "task", "run"))["activity"]
    assert activity["stage"] == "tool_results" and activity["phase"] == "end"
    assert activity["tool_item_id"] == tool_item_id("run", "actual-call")
    item = await service.item(None, "task", "run", tool_item_id("run", "actual-call"))
    assert item["status"] == "result_received" and item["status_sequence"] == activity["event_sequence"]
    started_event = next(event for event in sources.events if event.event_type == "runtime.activity.changed" and event.attributes.get("stage") == "tools")
    stream = service.stream(None, "task", "run", after_sequence=started_event.sequence - 1)
    frame = await anext(stream)
    assert frame.startswith(f"id: {started_event.sequence}\nevent: activity\n")
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload["item"]["status"] == "running" and payload["item"]["call_id"] == "actual-call"
    returned_frame = await anext(stream)
    returned = json.loads(returned_frame.split("data: ", 1)[1])
    assert returned["event_sequence"] > payload["event_sequence"]
    assert returned["item"]["status"] == "result_received"
    await stream.aclose()
    result.id = "tool-result:actual-call"
    await service.checkpoint_observer.checkpoint_committed(sources.commit("tool-result", {"messages": [proposal, result]}))
    assert (await service.item(None, "task", "run", tool_item_id("run", "actual-call")))["status"] == "succeeded"
    assert "PRIVATE" not in json.dumps([event.model_dump(mode="json") for event in sources.events])
    assert result.tool_call_id == "actual-call" and not callback._runtime_activities


@pytest.mark.asyncio
async def test_durable_compression_activity_ends_only_after_original_checkpoint_commit():
    sources = OriginalSources()
    service = sources.service()
    callback = service.callback_factory()
    parent, call = uuid4(), uuid4()
    metadata = {"run_id": "run", "thread_id": "thread", "langgraph_node": "compress_memory",
                "role_graph_checkpoint_ns": "role:entry", "langgraph_step": 7}
    await callback.on_chain_start(None, {}, run_id=parent)
    await callback.on_chat_model_start({}, [], run_id=call, parent_run_id=parent,
                                      tags=["role_summary"], metadata=metadata)
    await callback.on_llm_end(None, run_id=call)
    await callback.on_chain_end(Command(update={"conversation_summary": "Actual", "tool_chain_summary": "Saved"}, goto="main"), run_id=parent)
    assert (await service.snapshot(None, "task", "run"))["activity"]["stage"] == "compression"
    assert (await service.snapshot(None, "task", "run"))["activity"]["phase"] == "start"
    commit = sources.commit("compressed-runtime", {"messages": [], "conversation_summary": "Actual", "tool_chain_summary": "Saved"},
                            step=7, updated_channels=("conversation_summary", "tool_chain_summary"))
    await service.checkpoint_observer.checkpoint_committed(commit)
    snapshot = await service.snapshot(None, "task", "run")
    assert snapshot["activity"]["stage"] == "compression" and snapshot["activity"]["phase"] == "end"
    count = len(sources.events)
    await service.checkpoint_observer.checkpoint_committed(commit)
    assert len(sources.events) == count
    assert not callback._runtime_activities and not callback._chain_parents


@pytest.mark.asyncio
async def test_task_title_event_and_snapshot_read_one_saved_task_without_chat_message():
    sources = OriginalSources()
    sources.task.title = "圆环构件尺寸核验"
    sources.events.append(RunEvent(event_id="title-event", sequence=1, task_id="task", run_id="run",
        event_type="task.title.updated", occurred_at=NOW,
        scope=RunEventScope(module="component", graph_node="initialize_component"),
        item_id="task-title:task", status=RunEventType.SUCCEEDED))
    service = sources.service()
    snapshot = await service.snapshot(None, "task", "run")
    expected = {"task_id": "task", "title": "圆环构件尺寸核验"}
    assert snapshot["task"] == expected and snapshot["items"] == []
    stream = service.stream(None, "task", "run")
    event = await anext(stream)
    assert event.startswith("id: 1\nevent: task\n")
    assert json.loads(event.split("data: ", 1)[1]) == expected
    await stream.aclose()
    reconnect = await service.snapshot(None, "task", "run", before_sequence=1)
    assert reconnect["task"] == expected and reconnect["items"] == []


@pytest.mark.asyncio
async def test_paired_tool_multimages_and_native_structured_content_are_authorized():
    sources = OriginalSources()
    service = sources.service()
    call = AIMessage(id="main", content="Inspecting", tool_calls=[{"id": "real-call", "name": "preview", "args": {"reference": "family://exact", "source_path": "/Users/private/file", "grant": {"token": "secret"}}}], additional_kwargs={"tool_effects": {"real-call": "READ"}})
    images = [{"type": "image", "base64": base64.b64encode(value).decode(), "mime_type": "image/png"} for value in (b"first", b"second")]
    tool = ToolMessage(id="result", tool_call_id="real-call", content=[{"type": "text", "text": "Saved"}, *images], artifact={"structured_content": {"entity_ref": "entity://exact", "secret": "credential"}})
    await service.checkpoint_observer.checkpoint_committed(sources.commit("old", {"messages": [call, tool]}))
    item_id = tool_item_id("run", "real-call")
    item = await service.item(None, "task", "run", item_id)
    assert item["arguments"] == {"reference": "family://exact"}
    assert item["result"]["structuredContent"] == {"entity_ref": "entity://exact"}
    assert item["result"]["content"] == item["content"]
    assert item["call_id"] == "real-call" and item["status"] == "succeeded"
    assert item["main_message_id"] == "main"
    assert item["effect"] == "READ"
    assert [block["type"] for block in item["content"]] == ["text", "image", "image"]
    assert (await service.content(None, "task", "run", item_id, 1)).content == b"first"
    assert (await service.content(None, "task", "run", item_id, 2)).content == b"second"
    sources.authorized = False
    with pytest.raises(ResourceNotFoundError):
        await service.content(None, "task", "run", item_id, 1)


@pytest.mark.asyncio
async def test_role_saved_binding_effects_reach_observer_and_public_tool_items():
    from langgraph.checkpoint.memory import MemorySaver

    from CAD2BIM_backend.infrastructure.langgraph.role_graph import RoleProfile, build_role_react
    from CAD2BIM_backend.infrastructure.langgraph.role_tools import execute_saved_calls
    from tests.unit.backend.test_component_graph import _context
    from tests.unit.backend.test_role_graph import _UnusedUnitModel, _input
    from tests.unit.backend.test_role_tools import _binding

    class Main(_UnusedUnitModel):
        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages, config=None):
            return AIMessage(content=[{"type": "text", "text": "Reading the saved sources.", "phase": "commentary"}], tool_calls=[
                {"id": "read-first", "name": "first", "args": {}},
                {"id": "read-second", "name": "second", "args": {}},
            ])

    class Feedback:
        async def pause_requested(self, *_args):
            return False

        async def prepare_main(self, state, *_args):
            return {"messages": state["messages"]}

    bindings = {name: _binding(name) for name in ("first", "second")}
    graph = build_role_react(RoleProfile(role="component", core_prompt="Unit effect handoff.", model=Main(), tools=bindings),
                             Feedback(), artifacts=object()).compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "effect-handoff"}}
    await graph.ainvoke(_input(), config, context=_context("effect-unit"), interrupt_after=["main"], durability="sync")
    saved = await graph.aget_state(config)
    main = saved.values["messages"][-1]
    assert main.additional_kwargs["tool_effects"] == {"read-first": "READ", "read-second": "READ"}

    async def execute(*, call, **_kwargs):
        return ToolMessage(tool_call_id=call["id"], content="Saved source read.")

    update = await execute_saved_calls(saved.values, config, None, bindings, execute)
    sources = OriginalSources()
    service = sources.service()
    await service.checkpoint_observer.checkpoint_committed(sources.commit("main-saved", {"messages": saved.values["messages"]}))
    await service.checkpoint_observer.checkpoint_committed(sources.commit("tools-saved", {"messages": [*saved.values["messages"], *update["messages"]]}))
    snapshot = await service.snapshot(None, "task", "run")
    tools = [item for item in snapshot["items"] if item["kind"] == "tool"]
    assert [(item["call_id"], item["main_message_id"], item["effect"], item["status"]) for item in tools] == [
        ("read-first", main.id, "READ", "succeeded"), ("read-second", main.id, "READ", "succeeded"),
    ]
    public_main = next(item for item in snapshot["items"] if item.get("message_id") == main.id)
    assert public_main["message_phase"] == "commentary"


@pytest.mark.asyncio
async def test_parent_copy_and_retry_preserve_original_checkpoint_after_compression():
    sources = OriginalSources()
    service = sources.service()
    original = AIMessage(id="stable", content=[{"type": "text", "text": "Original history", "phase": "commentary"}], additional_kwargs={"encrypted_content": "opaque", "grant": "private"})
    initial_reference = SystemMessage(id="reference", content="Full supplied implementation.",
                                     additional_kwargs={"source": "initial_reference", "trigger_message_id": "initial"})
    await service.checkpoint_observer.checkpoint_committed(sources.commit("first", {"messages": [
        SystemMessage(content="CORE SECRET"), initial_reference, original,
    ]}))
    first_ref = sources.events[0].payload_ref
    copied = AIMessage(id="stable", content=[{"type": "text", "text": "Changed copy must not replace source", "phase": "final_answer"}])
    parent = sources.commit("parent", {"messages": [], "role_memories": {"component:": {"messages": [initial_reference, copied]}}}, namespace="")
    await service.checkpoint_observer.checkpoint_committed(parent)
    await service.checkpoint_observer.checkpoint_committed(parent)
    assert len(sources.events) == 1 and sources.events[0].payload_ref == first_ref
    item = await service.item(None, "task", "run", message_item_id("run", "stable"))
    assert item["text"] == "Original history"
    assert item["message_phase"] == "commentary"
    assert sources.reads[-1] == ("thread", "first", "role:entry")
    assert "opaque" not in json.dumps(item) and "CORE SECRET" not in json.dumps(item)


@pytest.mark.asyncio
async def test_public_fields_drop_native_protocol_secrets_paths_and_nonvisible_blocks():
    sources = OriginalSources()
    service = sources.service()
    message = AIMessage(
        id="visible", content=[
            {"type": "text", "text": "Result /Users/private/source.py and password=credential", "phase": "final_answer"},
            {"type": "reasoning", "text": "PRIVATE THOUGHT", "phase": "commentary"},
        ],
        additional_kwargs={"encrypted_content": "OPAQUE", "authorization": "Bearer PRIVATE"},
        response_metadata={"host_path": "/Users/private/backend"},
    )
    await service.checkpoint_observer.checkpoint_committed(sources.commit("visible", {"messages": [message]}))
    item = await service.item(None, "task", "run", message_item_id("run", "visible"))
    encoded = json.dumps(item)
    assert item["text"] == "Result [local path] and [redacted]"
    assert item["message_id"] == "visible" and item["message_phase"] == "final_answer"
    assert item["content"] == [{"type": "text", "text": item["text"]}]
    assert all(secret not in encoded for secret in ("PRIVATE", "OPAQUE", "credential", "/Users/", "additional_kwargs", "response_metadata"))
    with pytest.raises(ResourceNotFoundError):
        await service.content(None, "task", "run", item["item_id"], 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("phases, expected", [
    (("commentary", "commentary"), "commentary"),
    (("final_answer", "final_answer"), "final_answer"),
    (("commentary", "final_answer"), None),
    (("commentary", None), None),
    (("unknown",), None),
    ((None,), None),
])
async def test_message_phase_requires_one_explicit_native_text_phase(phases, expected):
    sources = OriginalSources()
    service = sources.service()
    message = AIMessage(id="main", content=[{"type": "text", "text": "Visible", **({"phase": phase} if phase is not None else {})} for phase in phases])
    await service.checkpoint_observer.checkpoint_committed(sources.commit("phase", {"messages": [message]}))
    item = await service.item(None, "task", "run", message_item_id("run", "main"))
    assert item.get("message_phase") == expected
    assert all("phase" not in block for block in item["content"])


@pytest.mark.asyncio
async def test_card_uses_original_artifact_version_and_does_not_repeat_parent_copy():
    sources = OriginalSources()
    service = sources.service()
    class SavedCards:
        async def download_version(self, owner, artifact, version):
            assert (owner, artifact, version) == ("tenant", "card", "version")
            return SimpleNamespace(content=b"# Saved Role Card\nActual decisions", artifact=SimpleNamespace(task_id="task"))
    service._artifacts = SavedCards()
    reference = {"artifact_id": "card", "version_id": "version"}
    await service.checkpoint_observer.checkpoint_committed(sources.commit("card", {"messages": [], "role_card_ref": reference}))
    await service.checkpoint_observer.checkpoint_committed(sources.commit("parent", {"messages": [], "role_card_ref": reference}, namespace=""))
    snapshot = await service.snapshot(None, "task", "run")
    assert len(snapshot["items"]) == 1
    assert snapshot["items"][0]["kind"] == "card"
    assert snapshot["items"][0]["text"] == "# Saved Role Card\nActual decisions"
    assert snapshot["items"][0]["result"] == reference
    assert sources.reads[-1] == ("thread", "card", "role:entry")


@pytest.mark.asyncio
async def test_official_skill_tool_reads_original_message_without_publishing_system_catalog(tmp_path):
    sources = OriginalSources()
    service = sources.service()
    package = tmp_path / "component-skill"
    package.mkdir()
    (package / "SKILL.md").write_text("---\nname: component-skill\ndescription: Use exact saved references.\n---\nUse exact resource references.", encoding="utf-8")
    skills = AgentSkills(tmp_path)
    await skills.discover()
    call = {"id": "read-skill", "name": "read_file", "args": {"file_path": "/component-skill/SKILL.md"}}
    loaded = await skills.aread(call, state={}, config={}, runtime=SimpleNamespace(context=None))
    loaded = loaded.model_copy(update={"id": "skill-result"})
    catalog = skills.project_system_message(SystemMessage(id="system", content="Core private prompt"))
    values = {"messages": [catalog, AIMessage(id="skill-call", content="", tool_calls=[call]), loaded]}
    await service.checkpoint_observer.checkpoint_committed(sources.commit("skill", values))
    result = await service.item(None, "task", "run", tool_item_id("run", "read-skill"))
    assert result["kind"] == "tool" and result["name"] == "read_file"
    assert result["call_id"] == "read-skill" and "Use exact resource references." in result["text"]
    assert result["arguments"] == {"file_path": "/component-skill/SKILL.md"}
    assert "/Users/" not in json.dumps(result) and "Core private" not in json.dumps(result)
    original_count = len(sources.events)
    copied = {"role_memories": {"component:": values}}
    await service.checkpoint_observer.checkpoint_committed(sources.commit("parent", copied, namespace=""))
    assert len(sources.events) == original_count
    result = await service.item(None, "task", "run", result["item_id"])
    assert "Use exact resource references." in result["text"] and sources.reads[-1] == ("thread", "skill", "role:entry")


@pytest.mark.asyncio
async def test_compression_requires_atomic_loop_summary_writes_and_keeps_exact_old_summaries():
    sources = OriginalSources()
    service = sources.service()
    summary_channels = ("messages", "conversation_summary", "tool_chain_summary")
    values = {"messages": [], "conversation_summary": "Original intent", "tool_chain_summary": "Original tool evidence"}
    await service.checkpoint_observer.checkpoint_committed(sources.commit("initial-child", values, source="loop", step=0, updated_channels=summary_channels))
    await service.checkpoint_observer.checkpoint_committed(sources.commit("initial-input", values, source="input", step=-1, updated_channels=summary_channels))
    await service.checkpoint_observer.checkpoint_committed(sources.commit("root", values, namespace="", source="loop", step=1, updated_channels=summary_channels))
    assert sources.events == []
    commit = sources.commit("compressed", values, updated_channels=summary_channels)
    await service.checkpoint_observer.checkpoint_committed(commit)
    await service.checkpoint_observer.checkpoint_committed(commit)
    newer = {**values, "conversation_summary": "A changed copied summary"}
    await service.checkpoint_observer.checkpoint_committed(sources.commit("parent", {"role_memories": {"component:": newer}}, namespace="", updated_channels=("role_memories",)))
    await service.checkpoint_observer.checkpoint_committed(sources.commit("main", newer, updated_channels=("messages",)))
    snapshot = await service.snapshot(None, "task", "run")
    assert len(snapshot["items"]) == 1 and snapshot["items"][0]["kind"] == "compression"
    assert snapshot["items"][0]["result"] == {"conversation_summary": "Original intent", "tool_chain_summary": "Original tool evidence"}
    assert sources.reads[-1] == ("thread", "compressed", "role:entry")


@pytest.mark.asyncio
async def test_explicit_rejection_marks_actual_calls_not_executed_and_hides_core_system():
    sources = OriginalSources()
    service = sources.service()
    ai = AIMessage(id="proposal", content="", tool_calls=[{"id": "rejected", "name": "write", "args": {"business": "value"}}], additional_kwargs={"tool_effects": {"rejected": "WRITE"}})
    correction = SystemMessage(id="correction", content="This batch was not executed.", additional_kwargs={"source": "tool_batch_rejection", "rejected_ai_message_id": "proposal", "rejected_tool_call_ids": ["rejected"]})
    await service.checkpoint_observer.checkpoint_committed(sources.commit("checkpoint", {"messages": [SystemMessage(content="Core hidden"), ai, correction]}))
    item = await service.item(None, "task", "run", tool_item_id("run", "rejected"))
    assert item["status"] == "not_executed" and item["arguments"] == {"business": "value"}
    assert item["main_message_id"] == "proposal"
    assert item["effect"] == "WRITE"
    snapshot = await service.snapshot(None, "task", "run")
    assert any(item["role"] == "system" and item["text"] == correction.content for item in snapshot["items"])
    assert "Core hidden" not in json.dumps(snapshot)


@pytest.mark.asyncio
async def test_accepted_feedback_owns_user_bubble_and_keeps_original_request_identity():
    sources = OriginalSources()
    service = sources.service()
    sources.feedbacks["feedback"] = SimpleNamespace(run_id="run", client_request_id="browser-request", content=(UserTextContent(text="User intent"),))
    sources.events.append(RunEvent(
        event_id="accepted", sequence=1, task_id="task", run_id="run", event_type="feedback.accepted", occurred_at=NOW,
        scope=RunEventScope(module="component", stage_key="component"), item_id="feedback",
        status=RunEventType.QUEUED, payload_ref="feedback://feedback", attributes={"feedback_id": "feedback"},
    ))
    await service.checkpoint_observer.checkpoint_committed(sources.commit("delivery", {"messages": [HumanMessage(id="delivered", content="User intent", additional_kwargs={"feedback_id": "feedback"})]}))
    snapshot = await service.snapshot(None, "task", "run")
    assert len(snapshot["items"]) == 1
    assert snapshot["items"][0]["client_request_id"] == "browser-request"
    assert snapshot["items"][0]["text"] == "User intent"


@pytest.mark.asyncio
async def test_first_input_attachment_uploaded_before_run_uses_exact_task_artifact():
    sources = OriginalSources()
    service = sources.service()
    sources.feedbacks["feedback"] = SimpleNamespace(run_id="run", client_request_id="input", content=[{"type": "image", "artifact_id": "upload", "version_id": "first"}])
    sources.events.append(RunEvent(
        event_id="accepted", sequence=1, task_id="task", run_id="run", event_type="feedback.accepted", occurred_at=NOW,
        scope=RunEventScope(module="component", stage_key="component"), item_id="feedback",
        status=RunEventType.QUEUED, payload_ref="feedback://feedback", attributes={"feedback_id": "feedback"},
    ))
    calls = []
    class ExactArtifacts:
        task_id = "task"
        async def get_version_metadata(self, owner_id, artifact_id, version_id):
            assert (owner_id, artifact_id, version_id) == ("tenant", "upload", "first")
            return SimpleNamespace(task_id=self.task_id, filename="Input.png", content_type="image/png", size_bytes=11)
        async def download_version(self, owner_id, artifact_id, version_id, **kwargs):
            calls.append((owner_id, artifact_id, version_id, kwargs))
            return SimpleNamespace(content=b"input image", artifact=SimpleNamespace(task_id=self.task_id, run_id=None, content_type="image/png"))
    artifacts = ExactArtifacts()
    service._artifacts = artifacts
    content = await service.content(None, "task", "run", "feedback", 0)
    assert content.content == b"input image" and calls == [("tenant", "upload", "first", {})]
    item = await service.item(None, "task", "run", "feedback")
    assert item["content"][0]["filename"] == "Input.png"
    assert item["content"][0]["content_type"] == "image/png" and item["content"][0]["size_bytes"] == 11
    artifacts.task_id = "other-task"
    with pytest.raises(ResourceNotFoundError):
        await service.item(None, "task", "run", "feedback")
    with pytest.raises(ResourceNotFoundError):
        await service.content(None, "task", "run", "feedback", 0)


@pytest.mark.asyncio
async def test_snapshot_boundary_enriches_tool_start_but_does_not_read_future_results():
    sources = OriginalSources()
    service = sources.service()
    ai = AIMessage(id="main", content="", tool_calls=[{"id": "call", "name": "read", "args": {"a": 1}}], additional_kwargs={"tool_effects": {"other-call": "READ", "call": "unknown"}})
    tool = ToolMessage(id="tool", tool_call_id="call", content="Finished", additional_kwargs={"tool_effect": "unknown"})
    await service.checkpoint_observer.checkpoint_committed(sources.commit("start", {"messages": [ai]}))
    await service.checkpoint_observer.checkpoint_committed(sources.commit("end", {"messages": [ai, tool]}))
    snapshot = await service.snapshot(None, "task", "run", limit=1)
    assert snapshot["items"][0]["arguments"] == {"a": 1}
    assert snapshot["items"][0]["text"] == "Finished"
    assert snapshot["items"][0]["main_message_id"] == "main"
    assert "effect" not in snapshot["items"][0]
    original_reader = sources.read_chat_snapshot
    async def boundary(*args, **kwargs):
        run, _, _, before, activities = await original_reader(*args, **kwargs)
        return run, (sources.events[0],), 1, before, tuple(event for event in activities if event.sequence <= 1)
    sources.read_chat_snapshot = boundary
    before = await service.snapshot(None, "task", "run")
    assert before["items"][0]["status"] == "queued"
    assert before["items"][0]["main_message_id"] == "main"
    assert "result" not in before["items"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_status, public_status", [("success", "succeeded"), ("error", "failed")])
async def test_tool_parent_comes_from_saved_request_across_pages_and_stream_results(tool_status, public_status):
    sources = OriginalSources()
    service = sources.service()
    first = AIMessage(id="main-first", content="", tool_calls=[{"id": "first-call", "name": "read", "args": {"a": 1}}], additional_kwargs={"tool_effects": {"first-call": "READ"}})
    adjacent = AIMessage(id="main-adjacent", content="", tool_calls=[{"id": "adjacent-call", "name": "read", "args": {"a": 2}}], additional_kwargs={"tool_effects": {"adjacent-call": "WRITE"}})
    await service.checkpoint_observer.checkpoint_committed(sources.commit("requests", {"messages": [first, adjacent]}))
    # A later checkpoint can contain only the result; the original request event
    # and its exact checkpoint retain the actual Main source.
    tool = ToolMessage(id="result-main-adjacent", tool_call_id="first-call", content="Actual result", status=tool_status, additional_kwargs={"tool_effect": "READ"})
    await service.checkpoint_observer.checkpoint_committed(sources.commit("result", {"messages": [tool]}))
    assert sources.events[-1].attributes["effect"] == "READ"
    page = await service.snapshot(None, "task", "run", limit=1)
    item = page["items"][0]
    assert item["main_message_id"] == "main-first" and item["status"] == public_status
    assert item["effect"] == "READ"
    assert item["arguments"] == {"a": 1} and item["text"] == "Actual result"
    older_page = await service.snapshot(None, "task", "run", before_sequence=2, limit=1)
    assert older_page["items"][0]["main_message_id"] == "main-first"
    stream = service.stream(None, "task", "run")
    started = json.loads((await anext(stream)).split("data: ", 1)[1])
    assert started["main_message_id"] == "main-first" and started["status"] == "queued"
    assert started["effect"] == "READ"
    other = json.loads((await anext(stream)).split("data: ", 1)[1])
    assert other["main_message_id"] == "main-adjacent"
    assert other["effect"] == "WRITE"
    finished = json.loads((await anext(stream)).split("data: ", 1)[1])
    assert finished == item
    await stream.aclose()


@pytest.mark.asyncio
async def test_deltas_filter_auxiliary_calls_close_committed_ids_and_release_slow_subscribers():
    bridge = MainDeltaBridge(queue_capacity=2)
    callback = MainTextDeltaCallback(bridge)
    main, auxiliary = uuid4(), uuid4()
    async with bridge.subscribe("run") as subscription:
        await callback.on_chat_model_start({}, [], run_id=auxiliary, tags=["role_main", "role_card"], metadata={"run_id": "run", "main_message_id": "card"})
        await callback.on_llm_new_token("hidden card", run_id=auxiliary)
        assert subscription.queue.empty()
        await callback.on_chat_model_start({}, [], run_id=main, tags=["role_main"], metadata={"run_id": "run", "main_message_id": "main"})
        started = subscription.queue.get_nowait()
        assert started["phase"] == "start" and started["delta_index"] == 0
        await callback.on_llm_new_token("Visible ", run_id=main)
        first = subscription.queue.get_nowait()
        assert first == {"phase": "delta", "message_id": "main", "call_id": str(main), "delta_index": 0, "text": "Visible "}
        await callback.on_llm_new_token("next ", run_id=main)
        await callback.on_llm_end(None, run_id=main)
        assert str(main) in bridge._calls
        bridge.message_committed("run", "main")
        await callback.on_llm_new_token("late", run_id=main)
        assert subscription.queue.empty()
        bridge.start("another", "run", "second")
        for token in ("First ", "Second ", "Third "):
            bridge.token("another", token)
        assert subscription.closed and "run" not in bridge._subscribers
    assert bridge._subscribers == {}


@pytest.mark.asyncio
async def test_stream_keeps_durable_sequence_holes_and_deltas_have_no_sse_id():
    sources = OriginalSources()
    service = sources.service()
    await service.checkpoint_observer.checkpoint_committed(sources.commit("first", {"messages": [AIMessage(id="saved", content="Saved")]}))
    public = sources.events[0].model_copy(update={"sequence": 3})
    internal = public.model_copy(update={"event_id": "internal", "sequence": 2, "audience": RunEventAudience.INTERNAL})
    sources.events = [internal, public]
    stream = service.stream(None, "task", "run", after_sequence=1)
    persisted = await anext(stream)
    assert persisted.startswith("id: 3\nevent: item\n")
    service.delta_bridge.start("call", "run", "new")
    service.delta_bridge.token("call", "Real text")
    transient = await anext(stream)
    assert transient.startswith("event: text_delta\n") and "id:" not in transient
    assert '"phase":"start"' in transient
    token = await anext(stream)
    assert '"phase":"delta"' in token and "id:" not in token
    await stream.aclose()
    assert service.delta_bridge._subscribers == {}


@pytest.mark.asyncio
async def test_saved_native_phase_replaces_stream_by_message_identity_and_survives_snapshot():
    sources = OriginalSources()
    service = sources.service()
    await service.checkpoint_observer.checkpoint_committed(sources.commit("previous", {"messages": [AIMessage(id="previous", content="Previous")]}))
    stream = service.stream(None, "task", "run")
    await anext(stream)
    service.delta_bridge.start("call", "run", "main")
    start = json.loads((await anext(stream)).split("data: ", 1)[1])
    assert start["message_id"] == "main" and start["phase"] == "start" and "message_phase" not in start
    service.delta_bridge.token("call", "Working ")
    delta = json.loads((await anext(stream)).split("data: ", 1)[1])
    assert delta["message_id"] == "main" and delta["phase"] == "delta"
    saved = AIMessage(id="main", content=[
        {"type": "text", "text": "Working now", "phase": "commentary"},
        {"type": "reasoning", "summary": [{"text": "PRIVATE"}], "encrypted_content": "OPAQUE"},
    ])
    await service.checkpoint_observer.checkpoint_committed(sources.commit("saved-main", {"messages": [saved]}))
    final = json.loads((await anext(stream)).split("data: ", 1)[1])
    assert final["message_id"] == start["message_id"] and final["message_phase"] == "commentary"
    assert final["text"] == "Working now" and "phase" not in final
    assert "PRIVATE" not in json.dumps(final) and "OPAQUE" not in json.dumps(final)
    assert "call" not in service.delta_bridge._calls
    snapshot = await service.snapshot(None, "task", "run", limit=1)
    assert snapshot["items"] == [final]
    await stream.aclose()


@pytest.mark.asyncio
async def test_stream_reuses_authorized_identity_with_original_strict_binding():
    sources = OriginalSources()
    service = sources.service()
    await service.checkpoint_observer.checkpoint_committed(sources.commit("first", {"messages": [AIMessage(id="saved", content="Saved")]}))
    sources.events.append(RunEvent(
        event_id="status", sequence=2, task_id="task", run_id="run", event_type="run.status.changed", occurred_at=NOW,
        scope=RunEventScope(module="component", stage_key="component"), item_id="run-status", status=RunEventType.RUNNING,
    ))
    authorization_contexts = []
    tasks = TaskRunService(
        repository=sources, identity_directory=None, execution_protocol_factory=lambda *_: None,
        bind_identity=bind_tenant_execution_context,
    )
    async def resolve_identity(subject, *, task_id=None):
        context = TenantExecutionContext(
            tenant_id="tenant", principal_id="principal", task_id=task_id,
            actor_kind=TenantActorKind.USER, command_id=f"read_{uuid4().hex}",
        )
        authorization_contexts.append(context)
        return context
    tasks._identity = resolve_identity
    service._task_runs = tasks
    service._bind_identity = bind_tenant_execution_context
    stream = service.stream(None, "task", "run")
    try:
        assert (await anext(stream)).startswith("id: 1\nevent: item\n")
        assert (await anext(stream)).startswith("id: 2\nevent: run\n")
        assert len(authorization_contexts) == 1
        assert current_tenant_execution_context() == authorization_contexts[0]
    finally:
        await stream.aclose()
    assert current_tenant_execution_context() is None
    assert service.delta_bridge._subscribers == {}


@pytest.mark.asyncio
async def test_main_public_text_holds_split_paths_and_spaced_quoted_credentials_but_streams_ordinary_text():
    bridge = MainDeltaBridge()
    callback = MainTextDeltaCallback(bridge)
    call = uuid4()
    chunks = [
        "Ordinary text is live. ", "/Us", "ers/", "synthetic/", "private.txt", " ",
        "api_", "key", " ", ":", " ", '"', "synthetic", " ", "credential", "\\", '"', " tail", '"', " ",
        "Bearer", " ", "synthetic", "-bearer", " ", "Visible end ", "password", " = ", "'", "synthetic final", "\\",
    ]
    deltas = []
    async with bridge.subscribe("run") as subscription:
        await callback.on_chat_model_start({}, [], run_id=call, tags=["role_main"], metadata={"run_id": "run", "main_message_id": "main"})
        assert subscription.queue.get_nowait()["phase"] == "start"
        await callback.on_llm_new_token(chunks[0], run_id=call)
        live = subscription.queue.get_nowait()
        assert live["text"] == chunks[0] and live["delta_index"] == 0
        deltas.append(live)
        for chunk in chunks[1:]:
            await callback.on_llm_new_token(chunk, run_id=call)
            while not subscription.queue.empty():
                deltas.append(subscription.queue.get_nowait())
        await callback.on_llm_end(None, run_id=call)
        while not subscription.queue.empty():
            deltas.append(subscription.queue.get_nowait())
        displayed = "".join(delta["text"] for delta in deltas)
        assert "synthetic" not in displayed and "credential" not in displayed and "/Users/" not in displayed
        assert displayed == public_text("".join(chunks))
        assert displayed.count("[redacted]") == 3 and displayed.count("[local path]") == 1
        assert [delta["delta_index"] for delta in deltas] == list(range(len(deltas)))
        assert str(call) in bridge._calls
        bridge.message_committed("run", "main")


@pytest.mark.asyncio
async def test_compression_activity_no_gain_ends_without_publishing_or_replacing_summaries():
    sources = OriginalSources()
    service = sources.service()
    bridge = service.delta_bridge
    callback = service.callback_factory()
    parent, first, second = uuid4(), uuid4(), uuid4()
    metadata = {"run_id": "run", "langgraph_node": "compress_memory",
                "role_graph_checkpoint_ns": "role:entry", "langgraph_step": 7}
    async with bridge.subscribe("run") as subscription:
        await callback.on_chain_start(None, {}, run_id=parent)
        for call in (first, second):
            await callback.on_chat_model_start({}, [], run_id=call, parent_run_id=parent,
                tags=["role_summary"], metadata=metadata)
            await callback.on_llm_end(None, run_id=call)
        assert subscription.queue.get_nowait() == {
            "kind": "compression", "phase": "start", "activity_id": str(parent),
        }
        assert subscription.queue.empty()
        await callback.on_chain_end(Command(update={}, goto="main"), run_id=parent)
        assert subscription.queue.get_nowait() == {
            "kind": "compression", "phase": "end", "activity_id": str(parent),
        }
        assert not bridge._compression_activities and not bridge._summary_calls
        assert not bridge._activity_ancestors and not callback._chain_parents
        old = {"messages": [], "conversation_summary": "Original conversation",
               "tool_chain_summary": "Original tools"}
        await service.checkpoint_observer.checkpoint_committed(sources.commit(
            "unchanged", old, step=7, updated_channels=(),
        ))
        assert sources.events == [] and subscription.queue.empty()
        await callback.on_chain_end(Command(update={}, goto="main"), run_id=uuid4())
        assert subscription.queue.empty()


@pytest.mark.asyncio
async def test_compression_activity_merges_real_summary_parent_ignores_tokens_and_waits_for_public_commit():
    sources = OriginalSources()
    service = sources.service()
    bridge = service.delta_bridge
    callback = service.callback_factory()
    await service.checkpoint_observer.checkpoint_committed(sources.commit("saved", {"messages": [AIMessage(id="saved", content="Saved")]}))
    stream = service.stream(None, "task", "run")
    assert (await anext(stream)).startswith("id: 1\nevent: item\n")
    metadata = {"run_id": "run", "task_id": "task", "business_entry_id": "entry", "role_entry_index": 2, "role": "component", "langgraph_node": "compress_memory", "role_graph_checkpoint_ns": "role:entry", "langgraph_step": 7}
    parent, first, second = uuid4(), uuid4(), uuid4()
    await callback.on_chat_model_start({}, [], run_id=first, parent_run_id=parent, tags=["role_summary"], metadata=metadata)
    start = await anext(stream)
    assert start == 'event: activity\ndata: ' + json.dumps({"kind": "compression", "phase": "start", "activity_id": str(parent)}, separators=(",", ":")) + '\n\n'
    await callback.on_chat_model_start({}, [], run_id=second, parent_run_id=parent, tags=["role_summary"], metadata=metadata)
    for call in (first, second):
        await callback.on_llm_new_token("PRIVATE SUMMARY", run_id=call)
        await callback.on_llm_end(None, run_id=call)
    subscription = next(iter(bridge._subscribers["run"]))
    assert subscription.queue.empty() and bridge._compression_activities == {str(parent): ("run", "role:entry", 7)}
    await callback.on_chain_error(ValueError("PRIVATE validation detail"), run_id=parent)
    error = await anext(stream)
    assert '"phase":"error"' in error and '"activity_id":"' + str(parent) + '"' in error and "PRIVATE" not in error and "id:" not in error
    assert not bridge._compression_activities and not bridge._summary_calls

    committed_parent, committed_model = uuid4(), uuid4()
    await callback.on_chat_model_start({}, [], run_id=committed_model, parent_run_id=committed_parent, tags=["role_summary"], metadata=metadata)
    await callback.on_chain_end(Command(update={
        "conversation_summary": "Actual saved conversation", "tool_chain_summary": "Actual saved tools",
    }, goto="main"), run_id=committed_parent)
    assert bridge._compression_activities == {str(committed_parent): ("run", "role:entry", 7)}
    bridge.message_committed("run", "saved")
    await callback.on_chain_error(RuntimeError("old error"), run_id=parent)
    assert bridge._compression_activities == {str(committed_parent): ("run", "role:entry", 7)}
    old = sources.commit("previous-compression", {"messages": [], "conversation_summary": "Old conversation", "tool_chain_summary": "Old tools"},
        namespace="role:previous-entry", step=1, updated_channels=("conversation_summary", "tool_chain_summary"))
    await service.checkpoint_observer.checkpoint_committed(old)
    await service.checkpoint_observer.checkpoint_committed(old)
    assert bridge._compression_activities == {str(committed_parent): ("run", "role:entry", 7)}
    original_append = sources.append_chat_events
    async def append_before_end(*args):
        assert bridge._compression_activities == {str(committed_parent): ("run", "role:entry", 7)}
        return await original_append(*args)
    sources.append_chat_events = append_before_end
    await service.checkpoint_observer.checkpoint_committed(sources.commit(
        "compressed", {"messages": [], "conversation_summary": "Actual saved conversation", "tool_chain_summary": "Actual saved tools"},
        step=7, updated_channels=("conversation_summary", "tool_chain_summary"),
    ))
    assert not bridge._compression_activities and not bridge._summary_calls
    assert (await anext(stream)).startswith("id: 2\nevent: item\n")
    assert (await anext(stream)).startswith("id: 3\nevent: item\n")
    end = await anext(stream)
    assert '"phase":"end"' in end and '"activity_id":"' + str(committed_parent) + '"' in end and '"phase":"start"' not in end and "id:" not in end

    failed_root, failed_parent, failed_model = uuid4(), uuid4(), uuid4()
    other_root, other_parent, other_model = uuid4(), uuid4(), uuid4()
    for root, parent in ((failed_root, failed_parent), (other_root, other_parent)):
        await callback.on_chain_start(None, {}, run_id=root)
        await callback.on_chain_start(None, {}, run_id=parent, parent_run_id=root)
    await callback.on_chat_model_start({}, [], run_id=failed_model, parent_run_id=failed_parent, tags=["role_summary"], metadata=metadata)
    await callback.on_chat_model_start({}, [], run_id=other_model, parent_run_id=other_parent, tags=["role_summary"], metadata={**metadata, "run_id": "other-run"})
    await callback.on_llm_error(RuntimeError("late old error"), run_id=committed_model)
    assert bridge._compression_activities == {str(failed_parent): ("run", "role:entry", 7), str(other_parent): ("other-run", "role:entry", 7)}
    await callback.on_llm_end(None, run_id=failed_model)
    await callback.on_chain_end(Command(update={
        "conversation_summary": "Not yet saved", "tool_chain_summary": "Not yet saved",
    }, goto="main"), run_id=failed_parent)
    await callback.on_chain_error(RuntimeError("PRIVATE checkpoint error"), run_id=failed_root)
    failed = await anext(stream)
    assert '"phase":"error"' in failed and '"activity_id":"' + str(failed_parent) + '"' in failed and "PRIVATE" not in failed
    assert bridge._compression_activities == {str(other_parent): ("other-run", "role:entry", 7)}
    await callback.on_llm_error(RuntimeError("PRIVATE model error"), run_id=other_model)
    await callback.on_chain_end({}, run_id=other_parent)
    await callback.on_chain_end({}, run_id=other_root)
    assert not bridge._compression_activities and not bridge._summary_calls and not bridge._calls and not bridge._activity_ancestors and not callback._chain_parents
    await stream.aclose()
    assert not bridge._subscribers


@pytest.mark.asyncio
async def test_main_error_closes_partial_call_with_public_error_and_keeps_final_commit_authority():
    bridge = MainDeltaBridge()
    callback = MainTextDeltaCallback(bridge)
    call = uuid4()
    async with bridge.subscribe("run") as subscription:
        await callback.on_chat_model_start({}, [], run_id=call, tags=["role_main"], metadata={"run_id": "run", "main_message_id": "main"})
        assert subscription.queue.get_nowait()["phase"] == "start"
        await callback.on_llm_new_token("Partial", run_id=call)
        assert subscription.queue.get_nowait()["delta_index"] == 0
        await callback.on_llm_error(RuntimeError("password=PRIVATE at /Users/private/backend.py"), run_id=call)
        error = subscription.queue.get_nowait()
        assert error == {"phase": "error", "message_id": "main", "call_id": str(call), "delta_index": 1, "text": "", "error": "Response generation failed."}
        await callback.on_llm_new_token("Late", run_id=call)
        assert subscription.queue.empty()
        bridge.message_committed("run", "main")
        assert str(call) not in bridge._calls
