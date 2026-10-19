"""Role builder unit evidence; model doubles here are not native-model proof."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.runtime import Runtime

from CAD2BIM_backend.infrastructure.langgraph.role_graph import RoleProfile, build_role_react


class _UnusedUnitModel:
    def with_config(self, config):
        return self

    async def ainvoke(self, messages, config=None):
        raise AssertionError("role_enter unit test must stop before model execution")


def _input():
    return {
        "role": "component", "stage_key": None, "business_entry_id": "entry-first",
        "role_entry_index": 1, "messages": [HumanMessage(id="user-first", content="First request")],
        "conversation_summary": "", "tool_chain_summary": "",
        "input_high_watermark": 0, "entry_input_pending": True, "entry_resources": (),
        "feedback_subject_id": None, "feedback_batch_ids": (), "feedback_task_ids": (),
        "feedback_check_ids": (), "pending_calls": [], "compression_return_to": "main",
        "role_exit_route": None, "role_card_draft": None, "role_card_ref": None,
    }


def test_summary_callback_scope_uses_saved_graph_namespace_with_real_task_metadata():
    from langgraph.runtime import ExecutionInfo

    from CAD2BIM_backend.infrastructure.langgraph.role_graph import _purpose_config

    graph_ns = "component_role:current"
    task_ns = f"{graph_ns}|compress_memory:task-one"
    config = {
        "metadata": {"langgraph_step": 12, "langgraph_checkpoint_ns": task_ns},
        "configurable": {
            "thread_id": "thread-one", "checkpoint_ns": task_ns,
            "checkpoint_map": {"": "root-one", "component_role:earlier": "earlier-one", graph_ns: "input-one"},
        },
    }
    runtime = Runtime(
        context=SimpleNamespace(scope=SimpleNamespace(
            identity=SimpleNamespace(task_id="task-one"), run_id="run-one",
        )),
        execution_info=ExecutionInfo(
            checkpoint_id="input-one", checkpoint_ns=task_ns,
            task_id="node-task-one", thread_id="thread-one",
        ),
    )
    prepared = _purpose_config(config, _input(), runtime, "summary")
    assert prepared["metadata"]["role_graph_checkpoint_ns"] == graph_ns
    assert prepared["metadata"]["langgraph_checkpoint_ns"] == task_ns
    assert prepared["metadata"]["langgraph_step"] == 12
    assert "role_graph_checkpoint_ns" not in config["metadata"]


@pytest.mark.asyncio
async def test_official_skill_catalog_is_transient_and_counts_in_role_request(tmp_path):
    from CAD2BIM_backend.infrastructure.agent_skills import AgentSkills
    from CAD2BIM_backend.infrastructure.langgraph.role_graph import build_prompt
    from CAD2BIM_backend.infrastructure.langgraph.role_compression import request_input_tokens
    package = tmp_path / "method"
    package.mkdir()
    (package / "SKILL.md").write_text("---\nname: method\ndescription: Useful method.\n---\nPRIVATE BODY\n")
    skills = AgentSkills(tmp_path)
    await skills.discover()
    profile = RoleProfile(role="component", core_prompt="Unit role.", model=_UnusedUnitModel(), skills=skills)
    app = build_role_react(profile, object(), artifacts=object()).compile(
        checkpointer=MemorySaver(), interrupt_after=["role_enter"],
    )
    config = {"configurable": {"thread_id": "skill-entry"}}
    await app.ainvoke(_input(), config, durability="sync")
    saved = (await app.aget_state(config)).values
    prompt = build_prompt(saved, profile)
    assert "/method/SKILL.md" in str(prompt[0].content)
    assert "PRIVATE BODY" not in str(prompt[0].content)
    assert "SKILL.md" not in str(saved["messages"])
    assert "loaded_skills" not in saved and "skills_metadata" not in saved
    bare = build_prompt(saved, RoleProfile(role="component", core_prompt="Unit role.", model=_UnusedUnitModel()))
    assert request_input_tokens(prompt, []) > request_input_tokens(bare, [])


@pytest.mark.asyncio
async def test_card_publisher_preserves_idempotency_without_a_stage():
    from tests.unit.backend.test_component_graph import _context

    class Artifacts:
        def __init__(self):
            self.calls = []

        async def upload_bytes(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(artifact_id="artifact_card_unit", version_id="version_card_unit", sha256="a" * 64)

    artifacts = Artifacts()
    builder = build_role_react(
        RoleProfile(role="component", core_prompt="Unit role.", model=_UnusedUnitModel()),
        object(), artifacts=artifacts,
    )
    publisher = builder.nodes["publish_role_card"].runnable.afunc
    state = {**_input(), "stage_key": None, "role_card_draft": "Saved Card draft."}
    first = await publisher(state, Runtime(context=_context("generation-1")))
    second = await publisher(state, Runtime(context=_context("generation-2")))
    assert artifacts.calls[0]["idempotency_key"] == artifacts.calls[1]["idempotency_key"]
    assert len(artifacts.calls[0]["idempotency_key"]) <= 128
    assert artifacts.calls[0]["task_id"] == "task-unit"
    assert artifacts.calls[0]["content"] == b"Saved Card draft."
    assert first["role_card_ref"] == second["role_card_ref"]
    assert first["messages"][0].additional_kwargs["version_id"] == "version_card_unit"


@pytest.mark.asyncio
async def test_main_saves_over_capacity_feedback_before_compression_and_stops_without_progress():
    from CAD2BIM_backend.infrastructure.langgraph.role_compression import RoleMemoryCapacityError
    from CAD2BIM_backend.infrastructure.input_token_count import InputTokenCount
    from tests.unit.backend.test_component_graph import _context

    initial = HumanMessage(id="initial-image-request", content="Keep the original request.",
                           additional_kwargs={"input_kind": "initial_input"})
    feedback = HumanMessage(id="saved-feedback-message", content="New required input. " * 8_000,
                            additional_kwargs={"feedback_id": "original-feedback"})

    class CountedMain(_UnusedUnitModel):
        def __init__(self):
            self.counted = []

        async def acount_input_tokens(self, request):
            self.counted.append(request)
            return InputTokenCount(5000 if any(message.id == feedback.id for message in request) else 20,
                                   "official")

    class Feedback:
        async def pause_requested(self, *_args):
            return False

        async def prepare_main(self, state, *_args):
            messages = list(state["messages"])
            if all(message.id != feedback.id for message in messages):
                messages.append(feedback)
            return {"messages": messages, "feedback_batch_ids": ("original-batch",),
                    "feedback_subject_id": "original-subject", "input_high_watermark": 2}

    model = CountedMain()
    builder = build_role_react(
        RoleProfile(role="component", core_prompt="Unit role.", model=model,
                    input_limit=4_096, output_reserve=512),
        Feedback(), artifacts=object(),
    )
    app = builder.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "over-capacity-feedback"}}
    await app.ainvoke({**_input(), "messages": [initial]}, config,
                     context=_context("generation-1"), interrupt_after=["main"], durability="sync")
    saved = await app.aget_state(config)
    assert saved.next == ("compress_memory",)
    assert tuple(saved.values["feedback_batch_ids"]) == ("original-batch",)
    assert saved.values["input_high_watermark"] == 2
    assert saved.values["entry_input_pending"] is True and saved.values["pending_calls"] == []
    assert saved.values["messages"][:2] == [initial, feedback]
    assert any(message.id == feedback.id for message in model.counted[-1])
    begin_id = "entry:entry-first:begin"
    assert [message.id for message in saved.values["messages"]].count(begin_id) == 1
    repeated = await builder.nodes["main"].runnable.afunc(
        saved.values, config, Runtime(context=_context("generation-1")),
    )
    assert repeated.goto == "compress_memory"
    assert [message.id for message in repeated.update["messages"]].count(begin_id) == 1
    # The required input itself cannot fit. The independent compression owner
    # must stop clearly rather than dropping it or sending Main around a loop.
    with pytest.raises(RoleMemoryCapacityError, match="No complete old prefix"):
        await app.ainvoke(None, config, context=_context("generation-2"), durability="sync")
    preserved = await app.aget_state(config)
    assert preserved.values["messages"] == saved.values["messages"]
    assert preserved.values["feedback_batch_ids"] == saved.values["feedback_batch_ids"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "summary", "main"])
async def test_checkpointed_overflow_preserves_whole_input_cohort_and_collects_late_feedback(failure):
    import asyncio

    from langchain_core.messages import AIMessage

    from CAD2BIM_backend.application.feedback_service import delivery_update
    from CAD2BIM_backend.domain.control_plane import FrozenFeedbackBatch, feedback_message_id
    from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
    from CAD2BIM_backend.infrastructure.langgraph.feedback_nodes import FeedbackNodes
    from CAD2BIM_backend.infrastructure.langgraph.role_compression import _selected_prefix
    from tests.unit.backend.test_component_graph import _context
    from tests.unit.backend.test_feedback_service import _frozen

    context = _context("generation-one")
    grant = RunnerCheckpointGrant(graph_context=context, thread_id="cohort-overflow",
                                  runner_id="runner-unit", lease_token="lease-unit", writer_generation=1)

    def batch(label, sequence, repeats):
        original = _frozen()
        batch_id, feedback_id = f"batch-{label}", f"feedback-{label}"
        member = original.members[0].model_copy(update={
            "feedback_id": feedback_id, "accepted_sequence": sequence,
            "claimed_batch_id": batch_id,
            "content": (original.members[0].content[0].model_copy(update={"text": f"Constraint {label}. " * repeats}),),
        })
        return FrozenFeedbackBatch(
            batch=original.batch.model_copy(update={
                "batch_id": batch_id, "claim_high_watermark": sequence,
                "member_feedback_ids": (feedback_id,),
            }), members=(member,), message_ids=(feedback_message_id(batch_id, feedback_id),),
        )

    class Service:
        def __init__(self):
            self.submitted = [batch("A", 1, 150), batch("B", 2, 100)]
            self.delivered = set()
            self.freeze_calls = []

        async def activate_feedback_subject(self, _grant, **_kwargs):
            return SimpleNamespace(subject_id="active-subject", stage_key=None, task_ids=(), check_ids=())

        async def freeze_for_main(self, _grant, **kwargs):
            self.freeze_calls.append(kwargs)
            self.delivered.update(kwargs["delivered_batch_ids"])
            return tuple(item for item in self.submitted if item.batch.batch_id not in self.delivered)

        async def delivery_update(self, _grant, state, frozen):
            return delivery_update(state, frozen)

        async def pause_requested(self, _grant):
            return False

    service = Service()
    nodes = FeedbackNodes(service)
    nodes.bind_grant_provider(lambda: grant)

    class Main:
        def __init__(self):
            self.requests = []
            self.message_ids = []
            self.fail_once = failure == "main"

        async def ainvoke(self, request, config=None):
            self.requests.append(request)
            self.message_ids.append(config["metadata"]["main_message_id"])
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("Main interrupted before complete response")
            return AIMessage(content="Complete reply to A, B and C.")

    class Summary:
        def __init__(self):
            self.requests = []
            self.started = asyncio.Event()
            self.fail_once = failure == "summary"

        def with_config(self, _config):
            return self

        async def ainvoke(self, request):
            self.requests.append(request)
            if len(self.requests) == 2:
                service.submitted.append(batch("C", 3, 1))
                self.started.set()
            await self.started.wait()
            if self.fail_once:
                self.fail_once = False
                return AIMessage(content="")
            return AIMessage(content="Replacement summary of the completed older work.")

    main, summary, saver = Main(), Summary(), MemorySaver()
    profile = RoleProfile(role="component", core_prompt="Unit cohort recovery.", model=main,
                          summary_model=summary, input_limit=3_000, output_reserve=512,
                          summary_input_limit=20_000)

    def restored_graph():
        return build_role_react(profile, nodes, artifacts=object()).compile(checkpointer=saver)

    app = restored_graph()
    config = {"configurable": {"thread_id": "cohort-overflow"}}
    initial = HumanMessage(id="initial", content="Original input.", additional_kwargs={"input_kind": "initial_input"})
    older = AIMessage(id="older", content="evidence " * 650)
    last_main = AIMessage(id="previous-main", content="Previous complete reply.")
    await app.ainvoke({**_input(), "messages": [initial, older, last_main]}, config,
                     context=context, interrupt_after=["main"], durability="sync")
    saved = await app.aget_state(config)
    a_id, b_id = (item.message_ids[0] for item in service.submitted)
    assert saved.next == ("compress_memory",) and main.requests == []
    assert saved.values["input_high_watermark"] == 2
    assert tuple(saved.values["feedback_batch_ids"]) == ("batch-A", "batch-B")
    assert {a_id, b_id} <= {message.id for message in saved.values["messages"]}

    # Rebuild the production graph on the same saver before resuming its exact
    # compression node. C arrives only after both summary requests have begun.
    app = restored_graph()
    if failure is not None:
        with pytest.raises((ValueError, RuntimeError)):
            await app.ainvoke(None, config, context=context, interrupt_after=["main"], durability="sync")
        failed = await app.aget_state(config)
        assert {a_id, b_id} <= {message.id for message in failed.values["messages"]}
        assert tuple(failed.values["feedback_batch_ids"]) == ("batch-A", "batch-B")
        if failure == "summary":
            assert failed.next == ("compress_memory",)
            assert failed.values["messages"] == saved.values["messages"]
            assert failed.values["conversation_summary"] == ""
        else:
            assert failed.next == ("main",)
            assert not any(isinstance(message, AIMessage) for message in failed.values["messages"])
        app = restored_graph()

    await app.ainvoke(None, config, context=context, interrupt_after=["main"], durability="sync")
    final = await app.aget_state(config)
    c_id = service.submitted[-1].message_ids[0]
    expected_inputs = ["initial", a_id, b_id, c_id]
    assert final.next == ("decide_role_exit",)
    assert final.values["input_high_watermark"] == 3
    assert tuple(final.values["feedback_batch_ids"]) == ("batch-C",)
    assert final.values["entry_input_pending"] is False
    assert [message.id for message in final.values["messages"] if isinstance(message, HumanMessage)] == expected_inputs
    assert [message.id for message in main.requests[-1] if isinstance(message, HumanMessage)] == expected_inputs
    assert c_id not in {message.id for request in summary.requests[:2] for message in request}
    assert service.freeze_calls[1]["delivered_batch_ids"] == ("batch-A", "batch-B")
    assert service.delivered == {"batch-A", "batch-B"}
    if failure == "main":
        assert main.message_ids[0] == main.message_ids[1]
    releasable = {message.id for message in _selected_prefix(final.values["messages"], final.values, 1)}
    assert {a_id, b_id, c_id} <= releasable
