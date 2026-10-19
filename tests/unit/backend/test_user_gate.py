from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, TypedDict

import pytest
from CAD2BIM_backend.infrastructure.langgraph import user_gate as user_gate_module
from pydantic import ValidationError
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.control_plane import UserGateResumePlan
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest
from CAD2BIM_backend.domain.system_interrupts import (
    InterruptResumeEnvelope,
    SystemInterruptDecision,
    SystemInterruptKind,
)
from CAD2BIM_backend.infrastructure.langgraph.execution_cursor import project_execution_snapshot
from CAD2BIM_backend.infrastructure.langgraph.user_gate import (
    build_user_gate_open_request,
    build_run_pause_open_request,
    gate_route,
    new_business_entry_id,
    reentry_route_update,
    pause_before_llm,
    user_gate_node,
)


class _State(TypedDict):
    business_entry_id: str
    gate_target_role: str
    input_high_watermark: int
    clarifications: list[Any] | None
    gate_route: str
    reenter_requested: bool


def _cursor() -> CheckpointCursor:
    mapping = (("", "root-1"),)
    return CheckpointCursor(
        thread_id="run-thread",
        checkpoint_id="root-1",
        root_checkpoint_id="root-1",
        checkpoint_map=mapping,
        checkpoint_digest=checkpoint_cursor_digest(
            checkpoint_id="root-1", checkpoint_ns="", checkpoint_map=mapping
        ),
    )


def _plan(action: str, *, feedback_batch_id: str | None = None) -> UserGateResumePlan:
    return UserGateResumePlan(
        interrupt_id="interrupt-1",
        source_checkpoint_cursor=_cursor(),
        operation_id="operation-1",
        business_entry_id="entry-1",
        accepted_root_checkpoint_id="root-1",
        response=InterruptResumeEnvelope(
            decision=SystemInterruptDecision(action=action),
            checkpoint_cursor=_cursor(),
            response_high_watermark=0,
            expected_writer_generation=1,
            feedback_batch_id=feedback_batch_id,
            selected_feedback_ids=("feedback-1",) if feedback_batch_id else (),
        ),
    )


def test_gate_routes_only_empty_assessed_clarifications_to_continue() -> None:
    approved = _plan("approve")
    assert gate_route(approved, clarifications=[]) == "continue"
    assert gate_route(approved, clarifications=None) == "reenter"
    assert gate_route(approved, clarifications=[{"id": "clar-1"}]) == "reenter"
    assert gate_route(_plan("revise"), clarifications=[]) == "reenter"
    assert gate_route(_plan("approve", feedback_batch_id="batch-1"), clarifications=[]) == "reenter"
    assert gate_route(_plan("cancel"), clarifications=[]) == "cancel"
    assert new_business_entry_id(_plan("revise")) == new_business_entry_id(_plan("revise"))
    assert reentry_route_update(approved, clarifications=None)["gate_reentry_reason"] == (
        "assess_clarifications"
    )
    assert reentry_route_update(
        approved, clarifications=[{"id": "clar-1"}]
    )["gate_reentry_reason"] == "apply_default_clarifications"


def test_gate_resume_rejects_conflicting_inner_cursor_and_accepted_root() -> None:
    plan = _plan("approve")
    other_map = (("", "root-other"),)
    other = CheckpointCursor(
        thread_id="run-thread", checkpoint_id="root-other",
        root_checkpoint_id="root-other", checkpoint_map=other_map,
        checkpoint_digest=checkpoint_cursor_digest(
            checkpoint_id="root-other", checkpoint_ns="", checkpoint_map=other_map
        ),
    )
    raw = plan.model_dump(mode="json")
    raw["response"]["checkpoint_cursor"] = other.model_dump(mode="json")
    with pytest.raises(ValidationError):
        UserGateResumePlan.model_validate(raw)
    raw = plan.model_dump(mode="json")
    raw["accepted_root_checkpoint_id"] = "root-other"
    with pytest.raises(ValidationError):
        UserGateResumePlan.model_validate(raw)


@pytest.mark.asyncio
async def test_gate_interrupt_binds_saved_parent_root_and_exact_resume() -> None:
    graph = StateGraph(_State)
    graph.add_node("user_gate", user_gate_node)
    graph.add_edge(START, "user_gate")
    graph.add_edge("user_gate", END)
    app = graph.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "run-thread"}}
    await app.ainvoke(
        {
            "business_entry_id": "entry-1",
            "gate_target_role": "requirement",
            "input_high_watermark": 3,
            "clarifications": [],
            "gate_route": "",
            "reenter_requested": False,
        },
        config,
    )
    snapshot = await app.aget_state(config)
    result = project_execution_snapshot(snapshot, thread_id="run-thread")
    run = SimpleNamespace(
        run_id="run-1",
        thread_id="run-thread",
        module="workflow",
        stage_key="cad2bim_workflow",
    )
    request = build_user_gate_open_request(run, result)
    assert request.system_interrupt.feedback_high_watermark == 3
    assert request.runtime_envelope.accepted_root_checkpoint_id == result.checkpoint.checkpoint_id
    assert request.runtime_envelope.business_entry_id == "entry-1"
    assert request.system_interrupt.created_at <= datetime.now(timezone.utc)

    plan = UserGateResumePlan(
        interrupt_id=request.system_interrupt.interrupt_id,
        source_checkpoint_cursor=request.runtime_envelope.checkpoint_cursor,
        operation_id="operation-1",
        business_entry_id="entry-1",
        accepted_root_checkpoint_id=result.checkpoint.checkpoint_id,
        response=InterruptResumeEnvelope(
            decision=SystemInterruptDecision(action="approve"),
            checkpoint_cursor=request.runtime_envelope.checkpoint_cursor,
            response_high_watermark=3,
            expected_writer_generation=1,
        ),
    )
    resumed = await app.ainvoke(Command(resume=plan.model_dump(mode="json")), snapshot.config)
    assert resumed["gate_route"] == "continue"
    assert resumed["business_entry_id"] == "entry-1"


@pytest.mark.asyncio
async def test_component_waiting_builder_has_no_approval_actions() -> None:
    graph = StateGraph(_State)
    graph.add_node("user_gate", user_gate_node)
    graph.add_edge(START, "user_gate")
    graph.add_edge("user_gate", END)
    app = graph.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "run-thread"}}
    await app.ainvoke({
        "business_entry_id": "entry-component", "gate_target_role": "component",
        "input_high_watermark": 2, "gate_route": "", "reenter_requested": False,
    }, config)
    result = project_execution_snapshot(await app.aget_state(config), thread_id="run-thread")
    request = build_user_gate_open_request(SimpleNamespace(
        run_id="run-component", thread_id="run-thread", module="component", stage_key="component",
    ), result)
    assert request.system_interrupt.kind is SystemInterruptKind.USER_INPUT_REQUIRED
    assert request.system_interrupt.allowed_actions == ()
    assert request.runtime_envelope.business_entry_id == "entry-component"
    assert request.runtime_envelope.accepted_root_checkpoint_id == result.checkpoint.checkpoint_id


class _PauseState(TypedDict):
    business_entry_id: str
    role: str
    role_entry_index: int
    input_high_watermark: int
    compression_return_to: str
    resumed: bool


@pytest.mark.asyncio
async def test_child_pause_restores_same_dynamic_interrupt_without_new_entry(tmp_path) -> None:
    child = StateGraph(_PauseState)
    child.add_node("pause_before_llm", pause_before_llm)
    child.add_node("compress_memory", lambda state: {"resumed": True})
    child.add_edge(START, "pause_before_llm")
    child.add_edge("compress_memory", END)
    parent = StateGraph(_PauseState)
    parent.add_node("component_role", child.compile())
    parent.add_edge(START, "component_role")
    parent.add_edge("component_role", END)
    app = parent.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "run-thread"}}
    await app.ainvoke({
        "business_entry_id": "entry-1", "role": "component", "role_entry_index": 4,
        "input_high_watermark": 3, "compression_return_to": "main", "resumed": False,
    }, config, durability="sync")
    snapshot = await app.aget_state(config, subgraphs=True)
    result = project_execution_snapshot(snapshot, thread_id="run-thread")
    assert result.checkpoint.checkpoint_ns.startswith("component_role:")
    assert not result.values["resumed"]
    request = build_run_pause_open_request(SimpleNamespace(
        run_id="run-1", thread_id="run-thread", module="component", stage_key="component",
    ), result)
    assert request.system_interrupt.kind is SystemInterruptKind.RUN_PAUSED
    assert request.system_interrupt.allowed_actions == ("continue",)
    assert request.runtime_envelope.business_entry_id is None
    assert request.runtime_envelope.accepted_root_checkpoint_id is None
    assert request.runtime_envelope.recovery_binding is None
    envelope = InterruptResumeEnvelope(
        decision=SystemInterruptDecision(action="continue"),
        checkpoint_cursor=request.runtime_envelope.checkpoint_cursor,
        response_high_watermark=3, expected_writer_generation=1,
    )
    resumed = await app.ainvoke(Command(resume={
        request.runtime_envelope.graph_interrupt_id: envelope.model_dump(mode="json"),
    }), snapshot.config, durability="sync")
    assert resumed["resumed"]
    assert resumed["business_entry_id"] == "entry-1"
    assert resumed["role_entry_index"] == 4
    assert resumed["compression_return_to"] == "main"
    (tmp_path / "pause_open_request.json").write_text(request.model_dump_json(indent=2), encoding="utf-8")
    (tmp_path / "pause_resume_state.json").write_text(json.dumps(resumed, indent=2), encoding="utf-8")


@pytest.mark.parametrize("action,message", [("approve", None), ("continue", "New input")])
def test_pause_rejects_approval_or_business_text(monkeypatch, action, message) -> None:
    envelope = InterruptResumeEnvelope(
        decision=SystemInterruptDecision(action=action, message=message),
        checkpoint_cursor=_cursor(), response_high_watermark=0, expected_writer_generation=1,
    )
    monkeypatch.setattr(user_gate_module, "interrupt", lambda signal: envelope.model_dump(mode="json"))
    with pytest.raises(ValueError, match="explicit continue without business input"):
        pause_before_llm({"business_entry_id": "entry-1"}, {}, SimpleNamespace())


def test_pause_continue_rejects_another_saved_cursor(monkeypatch) -> None:
    envelope = InterruptResumeEnvelope(
        decision=SystemInterruptDecision(action="continue"),
        checkpoint_cursor=_cursor(), response_high_watermark=0, expected_writer_generation=1,
    )
    monkeypatch.setattr(user_gate_module, "interrupt", lambda signal: envelope.model_dump(mode="json"))
    monkeypatch.setattr(user_gate_module, "exact_node_input_cursor", lambda config, runtime: None)
    with pytest.raises(ValueError, match="target the saved pause"):
        pause_before_llm({"business_entry_id": "entry-1"}, {}, SimpleNamespace())
