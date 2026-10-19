"""Component initialization and title recovery without an external model call."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from types import SimpleNamespace

from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.identity import (
    TenantActorKind,
    TenantExecutionContext,
    stable_internal_identity_id,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.role_memory import RoleMemory
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant, RunnerGraphContext
from CAD2BIM_backend.infrastructure.langgraph.component_graph import build_component_graph
from CAD2BIM_backend.infrastructure.langgraph.role_graph import RoleProfile


class _TitleModel:
    def __init__(self, *, fail_once=False):
        self.calls = []
        self.fail_once = fail_once

    async def ainvoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.fail_once:
            self.fail_once = False
            raise OSError("title stream interrupted")
        return AIMessage(content="圆环构件尺寸核验")


class _InitialFeedback:
    def __init__(self):
        self.messages = [HumanMessage(id="first-user-input", content=[
            {"type": "text", "text": "请核对图片中的圆环尺寸"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n"}},
        ], additional_kwargs={"input_kind": "initial_input", "feedback_id": "feedback-first"})]
        self.calls = []

    async def prepare_main(self, state, config, runtime):
        self.calls.append((state, config, runtime))
        return {"messages": self.messages, "feedback_batch_ids": ("batch-first",),
                "input_high_watermark": 1}


class _TitleTasks:
    def __init__(self, *, lose_ack=False):
        self.title = None
        self.writes = 0
        self.lose_ack = lose_ack

    async def generated_task_title(self, grant):
        return self.title

    async def set_generated_task_title(self, grant, title):
        self.title = title
        self.writes += 1
        if self.lose_ack:
            self.lose_ack = False
            raise OSError("title saved but response was lost")
        return SimpleNamespace(title=title)


def _context(command_id: str) -> RunnerGraphContext:
    return RunnerGraphContext(scope=InvocationScope(
        identity=TenantExecutionContext(
            tenant_id="tenant-unit", principal_id="principal-unit", task_id="task-unit",
            actor_kind=TenantActorKind.RUNNER, command_id=command_id,
        ),
        owner_id="tenant-unit", run_id="run_component_unit",
        execution_mode=ExecutionMode.GRAPH, catalog_frontier=None,
    ))


def _graph(model, feedback, tasks, context, *, initial_reference_text=None):
    grant = RunnerCheckpointGrant(context, "thread-run-component-unit", "runner-unit", "lease-unit", 1)
    return build_component_graph(
        MemorySaver(), feedback, artifacts=object(), task_runs=tasks,
        grant_provider=lambda: grant,
        initial_reference_text=initial_reference_text,
        profile=RoleProfile(role="component", core_prompt="Unit initialization.", model=model),
    )


@pytest.mark.asyncio
async def test_real_initialization_node_publishes_observed_boundaries_without_model_reasoning():
    from CAD2BIM_backend.infrastructure.chat_stream import MainDeltaBridge, MainTextDeltaCallback

    observed = []
    async def observe(activity_id, stage, phase, metadata):
        observed.append((activity_id, stage, phase, metadata["run_id"]))

    context = _context("generation-1")
    callback = MainTextDeltaCallback(MainDeltaBridge(), observe)
    graph = _graph(_TitleModel(), _InitialFeedback(), _TitleTasks(), context)
    await graph.ainvoke({}, {"configurable": {"thread_id": "thread-run-component-unit"},
                             "callbacks": [callback],
                             "metadata": {"run_id": "run_component_unit", "thread_id": "thread-run-component-unit"}},
                        context=context, interrupt_after=["initialize_component"], durability="sync")
    assert [(stage, phase, run) for _, stage, phase, run in observed] == [
        ("initializing", "start", "run_component_unit"),
        ("initializing", "end", "run_component_unit"),
    ]
    assert observed[0][0] == observed[1][0]
    assert not callback._runtime_activities and not callback._chain_parents


@pytest.mark.asyncio
async def test_initial_entry_is_materialized_from_saved_run_on_root_retry():
    model, feedback, tasks = _TitleModel(), _InitialFeedback(), _TitleTasks()
    context = _context("generation-1")
    graph = _graph(model, feedback, tasks, context)
    config = {"configurable": {"thread_id": "thread-run-component-unit"}}
    await graph.ainvoke({}, config, context=_context("generation-1"),
                        interrupt_before=["initialize_component"], durability="sync")
    original = await graph.aget_state(config)
    assert original.next == ("initialize_component",)
    assert "business_entry_id" not in original.values

    await graph.ainvoke(None, config, context=context,
                        interrupt_after=["initialize_component"], durability="sync")
    restored = await graph.aget_state(config)
    expected = stable_internal_identity_id("entry_", "component_initial", "run_component_unit")
    assert restored.values["business_entry_id"] == expected
    assert restored.values["role_entry_index"] == 1
    assert restored.next == ("component_role",)
    assert restored.config["configurable"]["checkpoint_id"] != original.config["configurable"]["checkpoint_id"]
    assert tasks.title == "圆环构件尺寸核验" and tasks.writes == 1
    memory = RoleMemory.model_validate(restored.values["role_memories"]["component:"])
    assert memory.messages == feedback.messages
    assert tuple(restored.values["feedback_batch_ids"]) == ("batch-first",)
    assert restored.values["input_high_watermark"] == 1
    title_input, options = model.calls[0]
    assert title_input[1:] == feedback.messages
    assert title_input[1].content[1]["image_url"]["url"] == "data:image/png;base64,cG5n"
    assert "role_title" in options["config"]["tags"]
    assert "role_main" not in options["config"]["tags"]
    assert all(isinstance(message, HumanMessage) for message in memory.messages)

    fresh_config = {"configurable": {"thread_id": "thread-same-run-fresh-unit"}}
    await graph.ainvoke({}, fresh_config, context=context,
                        interrupt_after=["initialize_component"], durability="sync")
    fresh = await graph.aget_state(fresh_config)
    assert fresh.values["business_entry_id"] == expected
    assert len(model.calls) == 1 and tasks.writes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_title_write", [False, True])
async def test_title_failure_retries_original_node_without_losing_input_or_repeating_committed_llm(lost_title_write):
    model = _TitleModel(fail_once=not lost_title_write)
    feedback, tasks = _InitialFeedback(), _TitleTasks(lose_ack=lost_title_write)
    context = _context("generation-1")
    graph = _graph(model, feedback, tasks, context)
    config = {"configurable": {"thread_id": "thread-run-component-unit"}}
    with pytest.raises(OSError):
        await graph.ainvoke({}, config, context=context, durability="sync")
    failed = await graph.aget_state(config)
    assert failed.next == ("initialize_component",)
    assert "business_entry_id" not in failed.values
    original = feedback.messages[0].model_copy(deep=True)
    await graph.ainvoke(None, config, context=context,
                        interrupt_after=["initialize_component"], durability="sync")
    restored = await graph.aget_state(config)
    assert RoleMemory.model_validate(restored.values["role_memories"]["component:"]).messages == [original]
    assert tasks.title == "圆环构件尺寸核验" and tasks.writes == 1
    assert len(model.calls) == (1 if lost_title_write else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("keyword_source,expected", [("initial_input", True), ("gate_input", False), ("system", False), ("none", False)])
async def test_initial_reference_matches_only_original_user_text_and_survives_checkpoint(keyword_source, expected):
    model, feedback, tasks = _TitleModel(), _InitialFeedback(), _TitleTasks()
    keyword = "请帮我创建带压力表的紧凑过滤阀组。"
    if keyword_source == "initial_input":
        feedback.messages[0].content[0]["text"] += keyword
    elif keyword_source == "gate_input":
        feedback.messages.append(HumanMessage(id="later-user", content=keyword,
                                             additional_kwargs={"input_kind": "gate_input"}))
    elif keyword_source == "system":
        feedback.messages.append(SystemMessage(id="unrelated", content=keyword))
    original = [message.model_copy(deep=True) for message in feedback.messages]
    context = _context("generation-1")
    graph = _graph(model, feedback, tasks, context, initial_reference_text="Complete supplied code and JSON.")
    config = {"configurable": {"thread_id": "thread-run-component-unit"}}
    await graph.ainvoke({}, config, context=context,
                        interrupt_after=["initialize_component"], durability="sync")
    saved = await graph.aget_state(config)
    memory = RoleMemory.model_validate(saved.values["role_memories"]["component:"])
    reference = [message for message in memory.messages if message.additional_kwargs.get("source") == "initial_reference"]
    assert len(reference) == int(expected)
    assert memory.messages[:len(original)] == original
    assert model.calls[0][0][1:] == [original[0]]  # Titles exclude deployment reference and later inputs.
    if reference:
        assert reference[0].content == "Complete supplied code and JSON."
        assert reference[0].additional_kwargs["trigger_message_id"] == original[0].id
        expected_id = stable_internal_identity_id("message_", "component_initial_reference",
                                                  context.scope.run_id, original[0].id)
        assert reference[0].id == expected_id
        # Reload the same saved node state through its checkpointer, not a second input path.
        reloaded = await graph.aget_state(saved.config)
        assert RoleMemory.model_validate(reloaded.values["role_memories"]["component:"]).messages == memory.messages


@pytest.mark.asyncio
async def test_reference_is_added_when_initialization_retries_after_title_was_saved():
    model, feedback, tasks = _TitleModel(), _InitialFeedback(), _TitleTasks(lose_ack=True)
    feedback.messages[0].content[0]["text"] += "请帮我创建带压力表的紧凑过滤阀组。"
    context = _context("generation-1")
    graph = _graph(model, feedback, tasks, context, initial_reference_text="Full reference.")
    config = {"configurable": {"thread_id": "thread-run-component-unit"}}
    with pytest.raises(OSError, match="title saved"):
        await graph.ainvoke({}, config, context=context, durability="sync")
    await graph.ainvoke(None, config, context=_context("generation-2"),
                        interrupt_after=["initialize_component"], durability="sync")
    saved = await graph.aget_state(config)
    memory = RoleMemory.model_validate(saved.values["role_memories"]["component:"])
    assert len(model.calls) == tasks.writes == 1
    reference = [message for message in memory.messages if message.additional_kwargs.get("source") == "initial_reference"]
    assert len(reference) == 1 and reference[0].content == "Full reference."
