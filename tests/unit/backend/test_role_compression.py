from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph.message import add_messages

from CAD2BIM_backend.infrastructure.langgraph.role_compression import (
    RoleMemoryCapacityError,
    build_memory_prompt,
    compression_update,
    request_input_tokens,
)
from CAD2BIM_backend.infrastructure.input_token_count import InputTokenCount


def _state():
    return {
        "messages": [
            HumanMessage(id="initial", content=[{"type": "text", "text": "Keep the original request."}, {"type": "image", "url": "https://example.test/original.png"}], additional_kwargs={"input_kind": "initial_input"}),
            AIMessage(id="old-ai", content="Read both sources", tool_calls=[{"name": "read", "args": {"ref": "exact:a"}, "id": "a"}, {"name": "read", "args": {"ref": "exact:b"}, "id": "b"}]),
            ToolMessage(id="old-a", tool_call_id="a", content="a " * 3000, artifact={"structured_content": {"exact_ref": "data_v003"}}),
            ToolMessage(id="old-b", tool_call_id="b", content="b " * 3000),
            AIMessage(id="old-answer", content="Older conclusion."),
            HumanMessage(id="recent-user", content="Use the new constraint."),
            AIMessage(id="recent-ai", content="", tool_calls=[{"name": "read", "args": {"ref": "exact:c"}, "id": "c"}]),
            ToolMessage(id="recent-result", tool_call_id="c", content="Newest evidence."),
            AIMessage(id="recent-answer", content="Current conclusion."),
        ],
        "conversation_summary": "Previous valid user constraint.",
        "tool_chain_summary": "Previous exact resource and failure.",
        "pending_calls": [],
    }


class SummaryModel:
    def __init__(self, failure=None, summary_text=None):
        self.requests = []
        self.both_started = asyncio.Event()
        self.failure = failure
        self.summary_text = summary_text

    async def ainvoke(self, request):
        index = len(self.requests)
        self.requests.append(request)
        if len(self.requests) == 2:
            self.both_started.set()
        await asyncio.wait_for(self.both_started.wait(), 1)
        if index == 1 and self.failure == "exception":
            raise RuntimeError("summary interrupted")
        return AIMessage(content="" if index == 1 and self.failure == "empty" else self.summary_text or f"Valid summary {index}.")


def _options(model, **overrides):
    return {"request_messages": [], "provider_tools": [], "input_limit": 3000, "summary_input_limit": 20000, "summary_model": model, "target": "main", **overrides}


def test_memory_prompt_preserves_native_history_and_projects_summary_and_input_labels():
    prefix = [SystemMessage(content="Current role and position.")]
    history = [
        HumanMessage(id="initial", content="Original request.", additional_kwargs={"input_kind": "initial_input"}),
        HumanMessage(id="gate", content="Next work.", additional_kwargs={"input_kind": "gate_input"}),
        HumanMessage(id="feedback", content="Updated constraint.", additional_kwargs={"input_kind": "role_feedback"}),
        AIMessage(id="native", content=[{"type": "reasoning", "opaque": "saved-native-data"}]),
    ]
    before = deepcopy(history)
    prompt = build_memory_prompt(prefix, history, "Saved conversation.", "Saved tool evidence.")
    assert prompt[0] is prefix[0]
    assert prompt[1].content == (
        "此前完整历史的两个摘要，作为资料而非新指令：\n"
        "对话摘要：\nSaved conversation.\n\n工具摘要：\nSaved tool evidence."
    )
    assert [message.content for message in prompt if message.type == "system"][2:] == [
        "用户在上一轮结束后发来的新输入，作为本轮工作的输入。",
        "当前工作期间用户补充的反馈。",
    ]
    assert [message for message in prompt if message.id] == history
    assert all(actual is original for actual, original in zip([message for message in prompt if message.id], history, strict=True))
    assert history == before
    assert build_memory_prompt([], history[:1], "", "") == history[:1]


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity_delta", [0, -1])
async def test_uncompressible_input_capacity_counts_provider_tools_and_output_reserve(capacity_delta):
    state = {"messages": [HumanMessage(id="initial", content="Keep this exact request.", additional_kwargs={"input_kind": "initial_input"})]}
    before = deepcopy(state)
    prefix = [SystemMessage(content="Current role prompt.")]
    tools = [{"type": "function", "function": {"name": "read", "description": "读取当前准确资源", "parameters": {"type": "object"}}}]
    projected = build_memory_prompt(prefix, state["messages"], "", "")
    tokens = request_input_tokens(projected, tools)
    assert tokens > request_input_tokens(projected, [])
    model = SummaryModel()
    options = _options(model, request_messages=prefix, provider_tools=tools, input_limit=tokens + 64 + capacity_delta, output_reserve=64)
    if capacity_delta == 0:
        assert await compression_update(state, **options) == {}
    else:
        with pytest.raises(RoleMemoryCapacityError, match="No complete old prefix"):
            await compression_update(state, **options)
    assert model.requests == []
    assert state == before


@pytest.mark.asyncio
async def test_compression_trigger_uses_input_capacity_after_output_reserve():
    state = _state()
    tokens = request_input_tokens(build_memory_prompt([], state["messages"], state["conversation_summary"], state["tool_chain_summary"]), [])
    input_capacity = (tokens * 10 + 8) // 9
    model = SummaryModel()
    assert await compression_update(state, **_options(
        model, input_limit=input_capacity + 1024, output_reserve=1024,
    )) == {}
    assert model.requests == []
    input_limit = input_capacity - 2 + 1024
    assert tokens <= int(input_limit * 0.90)
    update = await compression_update(state, **_options(model, input_limit=input_limit, output_reserve=1024))
    assert len(model.requests) == 2
    assert update["conversation_summary"] == "Valid summary 0."


async def test_official_count_skips_false_local_trigger_and_counts_each_summary_input():
    state = _state()
    before = deepcopy(state)

    class CountedSummary(SummaryModel):
        def __init__(self):
            super().__init__()
            self.counted = []

        async def acount_input_tokens(self, request):
            self.counted.append(request)
            return InputTokenCount(100, "official")

    model = CountedSummary()
    assert request_input_tokens(state["messages"], []) > 1976
    assert await compression_update(state, **_options(model)) == {}
    assert len(model.counted) == 3 and model.requests == []
    assert [message for message in model.counted[0] if message.id] == state["messages"]
    assert [message for message in model.counted[1] if message.id] == state["messages"]
    assert [message for message in model.counted[2] if message.id] == state["messages"]
    assert model.counted[1][0].content != model.counted[2][0].content
    assert state == before


@pytest.mark.parametrize("candidate_tokens,expected", [(1977, "capacity"), (1900, "no_gain"), (1000, "commit")])
async def test_official_candidate_count_precedes_atomic_compression_update(candidate_tokens, expected):
    state = _state()
    before = deepcopy(state)

    class RequestCounter:
        def __init__(self):
            self.counted = []

        async def acount_input_tokens(self, request):
            self.counted.append(request)
            assert state == before
            return InputTokenCount(1900 if len(self.counted) == 1 else candidate_tokens, "official")

    class CountedSummary(SummaryModel):
        def __init__(self):
            super().__init__()
            self.counted = []

        async def acount_input_tokens(self, request):
            self.counted.append(request)
            return InputTokenCount(100, "official")

    request_model, summaries = RequestCounter(), CountedSummary()
    options = _options(summaries, request_model=request_model)
    if expected == "capacity":
        with pytest.raises(RoleMemoryCapacityError, match="Both summaries succeeded"):
            await compression_update(state, **options)
    else:
        update = await compression_update(state, **options)
        assert bool(update) == (expected == "commit")
    assert len(summaries.counted) == len(summaries.requests) == 2
    assert summaries.counted == summaries.requests
    assert len(request_model.counted) == 2
    assert state == before


async def test_official_summary_capacity_checks_both_full_inputs_before_model_calls():
    state = _state()
    before = deepcopy(state)

    class RequestCounter:
        async def acount_input_tokens(self, request):
            return InputTokenCount(1900, "official")

    class CountedSummary(SummaryModel):
        def __init__(self):
            super().__init__()
            self.counted = []

        async def acount_input_tokens(self, request):
            self.counted.append(request)
            return InputTokenCount(100 if len(self.counted) == 1 else 20_000, "official")

    summaries = CountedSummary()
    with pytest.raises(RoleMemoryCapacityError, match="complete shared summary material"):
        await compression_update(state, **_options(summaries, request_model=RequestCounter()))
    assert len(summaries.counted) == 2 and summaries.requests == []
    assert all([message for message in request if message.id] == state["messages"]
               for request in summaries.counted)
    assert state == before


@pytest.mark.asyncio
async def test_retained_request_must_fit_capacity_after_both_summaries_succeed():
    state = _state()
    before = deepcopy(state)
    summary = "summary " * 450
    retained = state["messages"][:1] + state["messages"][4:]
    retained_tokens = request_input_tokens(build_memory_prompt([], retained, summary, summary), [])
    output_reserve = 1024
    input_limit = retained_tokens + output_reserve - 1
    assert input_limit - output_reserve < retained_tokens <= input_limit
    model = SummaryModel(summary_text=summary)
    with pytest.raises(RoleMemoryCapacityError, match="Both summaries succeeded"):
        await compression_update(state, **_options(model, input_limit=input_limit, output_reserve=output_reserve))
    assert len(model.requests) == 2
    assert [message for message in model.requests[0] if message.id] == state["messages"]
    assert state == before


@pytest.mark.asyncio
async def test_short_selected_boundary_does_not_replace_history_with_larger_summaries():
    state = {
        "messages": [
            HumanMessage(id="initial", content="Keep this requirement.", additional_kwargs={"input_kind": "initial_input"}),
            SystemMessage(id="boundary", content="Begin this role entry."),
            AIMessage(id="call", content="", tool_calls=[{"name": "read", "args": {}, "id": "recent"}]),
            ToolMessage(id="result", tool_call_id="recent", content="recent evidence " * 600),
            AIMessage(id="answer", content="Continue from the saved result."),
        ],
    }
    before = deepcopy(state)
    prefix = [SystemMessage(content="Required role instructions. " * 800)]
    current = request_input_tokens(build_memory_prompt(prefix, state["messages"], "", ""), [])
    model = SummaryModel(summary_text="Replacement summary with no additional evidence. " * 8)
    update = await compression_update(state, **_options(
        model, request_messages=prefix, input_limit=current + 200 + 1024,
    ))
    assert len(model.requests) == 2
    assert update == {}
    assert state == before


@pytest.mark.asyncio
async def test_large_request_prefix_does_not_make_all_recent_history_mandatory():
    state = {
        "messages": [
            HumanMessage(id="initial", content="Keep the original image and requirement.", additional_kwargs={"input_kind": "initial_input"}),
            AIMessage(id="old-call", content="", tool_calls=[{"name": "read", "args": {}, "id": "old"}]),
            ToolMessage(id="old-result", tool_call_id="old", content="Earlier saved evidence. " * 20),
            AIMessage(id="latest-call", content="", tool_calls=[{"name": "read", "args": {}, "id": "latest"}]),
            ToolMessage(id="latest-result", tool_call_id="latest", content="Current saved result."),
        ],
        "conversation_summary": "The still valid user requirement.",
        "tool_chain_summary": "Previous successful operations.",
        "pending_calls": [],
    }
    before = deepcopy(state)
    tools = [{"type": "function", "name": "read", "description": "Required tool contract. " * 1500}]
    current = request_input_tokens(build_memory_prompt(
        [], state["messages"], state["conversation_summary"], state["tool_chain_summary"],
    ), tools)
    model = SummaryModel()
    update = await compression_update(state, **_options(
        model, provider_tools=tools, input_limit=current - 1 + 128, output_reserve=128,
    ))
    assert {message.id for message in update["messages"]} == {"old-call", "old-result"}
    remaining = add_messages(state["messages"], update["messages"])
    assert [message.id for message in remaining] == ["initial", "latest-call", "latest-result"]
    assert request_input_tokens(build_memory_prompt(
        [], remaining, update["conversation_summary"], update["tool_chain_summary"],
    ), tools) < current - 1
    assert state == before


@pytest.mark.asyncio
async def test_same_complete_ordered_material_parallel_summaries_and_atomic_old_exchange_removal():
    state = _state()
    before = deepcopy(state)
    model = SummaryModel()
    update = await compression_update(state, **_options(model))
    assert len(model.requests) == 2
    assert model.requests[0][1:] == model.requests[1][1:]
    assert model.requests[0][0].content != model.requests[1][0].content
    material = model.requests[0][1:]
    assert "Previous valid user constraint" in material[0].content
    assert "Previous exact resource and failure" in material[0].content
    sections = [json.loads(message.content) for message in material
                if isinstance(message.content, str) and message.content.startswith('{"history_section":')]
    assert sections[1]["tool_artifacts_by_call_id"]["a"] == {"structured_content": {"exact_ref": "data_v003"}}
    originals = [message for message in material if message.id]
    assert originals == state["messages"]
    assert all(actual is original for actual, original in zip(originals, state["messages"], strict=True))
    assert originals[0].content[1]["type"] == "image"
    assert {message.id for message in update["messages"]} == {"old-ai", "old-a", "old-b"}
    kept = add_messages(state["messages"], update["messages"])
    assert [message.id for message in kept] == ["initial", "old-answer", "recent-user", "recent-ai", "recent-result", "recent-answer"]
    assert update["conversation_summary"] == "Valid summary 0."
    assert update["tool_chain_summary"] == "Valid summary 1."
    assert state == before


@pytest.mark.asyncio
async def test_summary_sections_survive_final_provider_projection_with_native_images_and_reasoning():
    from CAD2BIM_backend.infrastructure.chatgpt_model import create_chatgpt_plan_model

    async def unused_token():
        raise AssertionError("Payload projection must not make a network request")

    state = _state()
    state["messages"][1].content = [
        {"type": "reasoning", "id": "rs_saved", "summary": [], "encrypted_content": "opaque-saved-reasoning"},
        {"type": "text", "text": "Read both sources"},
    ]
    state["messages"][2].content = [
        {"type": "text", "text": state["messages"][2].content},
        {"type": "image", "base64": "aW1hZ2U=", "mime_type": "image/png"},
    ]
    before = deepcopy(state)
    summary = SummaryModel()
    await compression_update(state, **_options(summary))
    payload = create_chatgpt_plan_model(unused_token)._get_request_payload(summary.requests[0])
    sections = []
    for item in payload["input"]:
        content = item.get("content")
        if isinstance(content, str) and content.startswith('{"history_section":'):
            assert item["role"] == "developer"
            sections.append((json.loads(content), []))
        elif sections:
            sections[-1][1].append(item)
    assert [marker["history_section"] for marker, _ in sections] == [
        "retained_for_context", "selected_to_compress", "retained_for_context",
    ]
    initial, selected, recent = [items for _, items in sections]
    assert initial[0]["content"][1]["type"] == "input_image"
    assert initial[0]["content"][1]["image_url"] == "https://example.test/original.png"
    assert next(item for item in selected if item.get("type") == "reasoning")["encrypted_content"] == "opaque-saved-reasoning"
    assert [item["call_id"] for item in selected if item.get("type") == "function_call"] == ["a", "b"]
    outputs = [item for item in selected if item.get("type") == "function_call_output"]
    assert [item["call_id"] for item in outputs] == ["a", "b"]
    assert outputs[0]["output"][1] == {"type": "input_image", "image_url": "data:image/png;base64,aW1hZ2U="}
    assert sections[1][0]["tool_artifacts_by_call_id"][outputs[0]["call_id"]] == state["messages"][2].artifact
    assert [item["call_id"] for item in recent if item.get("type") == "function_call_output"] == ["c"]
    assert state == before


@pytest.mark.asyncio
async def test_second_compression_replaces_the_same_two_summaries_without_replaying_removed_history():
    state = _state()
    reference = SystemMessage(id="initial-reference", content="Complete source and JSON reference. " * 40,
                              additional_kwargs={"source": "initial_reference", "trigger_message_id": "initial"})
    state["messages"].insert(1, reference)
    first = await compression_update(state, **_options(SummaryModel(summary_text="First replacement.")))
    state = {**state, **first, "messages": add_messages(state["messages"], first["messages"])}
    state["messages"] = add_messages(state["messages"], [
        HumanMessage(id="next-user", content="Continue with the saved resource."),
        AIMessage(id="next-ai", content="", tool_calls=[{"name": "read", "args": {"ref": "exact:d"}, "id": "d"}]),
        ToolMessage(id="next-result", tool_call_id="d", content="More evidence. " * 800),
        AIMessage(id="latest-ai", content="", tool_calls=[{"name": "read", "args": {"ref": "exact:e"}, "id": "e"}]),
        ToolMessage(id="latest-result", tool_call_id="e", content="Latest evidence."),
    ])
    model = SummaryModel(summary_text="Second replacement.")
    second = await compression_update(state, **_options(model))
    metadata = json.loads(model.requests[0][1].content)
    assert metadata["previous_summaries"] == {
        "conversation_summary": "First replacement.", "tool_chain_summary": "First replacement.",
    }
    assert not {"old-ai", "old-a", "old-b"} & {message.id for message in model.requests[0]}
    state = {**state, **second, "messages": add_messages(state["messages"], second["messages"])}
    assert state["conversation_summary"] == state["tool_chain_summary"] == "Second replacement."
    assert all(message.content not in ("First replacement.", "Second replacement.") for message in state["messages"])
    assert {"latest-ai", "latest-result"} <= {message.id for message in state["messages"]}
    assert next(message for message in state["messages"] if message.id == reference.id) == reference
    assert all(reference in request for request in model.requests)


@pytest.mark.asyncio
async def test_initial_reference_is_part_of_official_budget_and_cannot_be_silently_removed():
    state = {"messages": [
        HumanMessage(id="initial", content="Original requirement.", additional_kwargs={"input_kind": "initial_input"}),
        SystemMessage(id="reference", content="Full reference material.",
                      additional_kwargs={"source": "initial_reference", "trigger_message_id": "initial"}),
    ]}
    before = deepcopy(state)

    class Counter(SummaryModel):
        async def acount_input_tokens(self, request):
            assert state["messages"][1] in request
            return InputTokenCount(tokens=4000, source="official")

    model = Counter()
    with pytest.raises(RoleMemoryCapacityError, match="No complete old prefix"):
        await compression_update(state, **_options(model))
    assert state == before and not model.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "empty"])
async def test_either_summary_failure_preserves_both_summaries_and_all_history(failure):
    state = _state()
    before = deepcopy(state)
    with pytest.raises((RuntimeError, ValueError)):
        await compression_update(state, **_options(SummaryModel(failure)))
    assert state == before


@pytest.mark.asyncio
async def test_summary_capacity_uses_whole_material_and_never_slices_recent_context():
    state = _state()
    before = deepcopy(state)
    model = SummaryModel()
    with pytest.raises(RoleMemoryCapacityError, match="complete shared summary material"):
        await compression_update(state, **_options(model, summary_input_limit=2000))
    assert model.requests == []
    assert state == before


@pytest.mark.asyncio
async def test_sufficient_request_budget_skips_models_and_pending_exchange_is_never_removed():
    state = _state()
    model = SummaryModel()
    assert await compression_update(state, **_options(model, input_limit=30000, summary_input_limit=30000)) == {}
    assert model.requests == []
    state["messages"] = state["messages"][:1] + state["messages"][5:7]
    state["pending_calls"] = [{"ai_message_id": "recent-ai", "tool_call_id": "c", "action_id": "action-c"}]
    with pytest.raises(RoleMemoryCapacityError, match="No complete old prefix"):
        await compression_update(state, **_options(model, provider_tools=[{"description": "schema " * 10000}]))
    assert model.requests == []


@pytest.mark.asyncio
async def test_delivered_old_feedback_can_compress_without_pinning_its_entire_later_tool_loop():
    state = _state()
    state["messages"] = state["messages"][:1] + [HumanMessage(id="old-feedback", content="A still-valid revised constraint.")] + state["messages"][1:5] + state["messages"][6:]
    update = await compression_update(state, **_options(SummaryModel()))
    removed = {message.id for message in update["messages"]}
    assert {"old-feedback", "old-ai", "old-a", "old-b"} <= removed
    assert not {"initial", "recent-ai", "recent-result", "recent-answer"} & removed


def test_all_unprocessed_feedback_is_retained_after_delivery_is_settled():
    from CAD2BIM_backend.infrastructure.langgraph.role_compression import _selected_prefix

    state = _state()
    state["messages"].extend([
        HumanMessage(id="unprocessed-a", content="First constraint. " * 300),
        HumanMessage(id="unprocessed-b", content="Second constraint. " * 300),
    ])
    state["entry_input_pending"] = False
    state["feedback_batch_ids"] = ()
    selected = {message.id for message in _selected_prefix(state["messages"], state, 1)}
    assert not {"unprocessed-a", "unprocessed-b"} & selected
