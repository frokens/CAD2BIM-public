"""Durable parent Gate interrupt and deterministic business routing."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from langchain_core.runnables.config import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.control_plane import (
    FeedbackWakeResumePlan,
    GateResumePlan,
    OpenInterruptRequest,
    RunRecord,
    UserGateResumePlan,
)
from CAD2BIM_backend.domain.identity import stable_internal_identity_id
from CAD2BIM_backend.domain.runner_context import RunnerGraphContext
from CAD2BIM_backend.domain.system_interrupts import (
    InterruptResumeEnvelope,
    SystemInterrupt,
    SystemInterruptKind,
    SystemInterruptRecoveryPolicy,
    SystemInterruptRuntimeEnvelope,
    SystemInterruptSource,
)
from CAD2BIM_backend.ports.graph_runtime import GraphCheckpointDescriptor, GraphExecutionResult

from .feedback_nodes import exact_node_input_cursor


class UserGateSignal(BaseModel):
    """Only business context saved by the trusted Gate node before parking."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["user_gate"] = "user_gate"
    business_entry_id: str = Field(min_length=1, max_length=255)
    target_role: str = Field(min_length=1, max_length=64)
    input_high_watermark: int = Field(ge=0)


def _gate_signal(state: Mapping[str, Any]) -> UserGateSignal:
    return UserGateSignal(
        business_entry_id=state.get("business_entry_id"),
        target_role=state.get("gate_target_role"),
        input_high_watermark=state.get("input_high_watermark"),
    )


def _checkpoint_cursor(checkpoint: GraphCheckpointDescriptor) -> CheckpointCursor:
    if checkpoint.checkpoint_map_digest is None:
        raise ValueError("interrupt checkpoint has no exact map digest")
    return CheckpointCursor(
        thread_id=checkpoint.thread_id,
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_ns=checkpoint.checkpoint_ns,
        root_checkpoint_id=checkpoint.root_checkpoint_id,
        checkpoint_map=checkpoint.checkpoint_map,
        checkpoint_digest=checkpoint.checkpoint_map_digest,
    )


def build_user_gate_open_request(
    run: RunRecord, result: GraphExecutionResult
) -> OpenInterruptRequest:
    """Bind one Graph interrupt to the saved parent result and business entry."""

    if len(result.interrupts) != 1 or not isinstance(result.values, Mapping):
        raise ValueError("user Gate requires one saved Graph interrupt")
    pending = result.interrupts[0]
    graph_interrupt_id = str(pending.get("id") or "")
    if not graph_interrupt_id:
        raise ValueError("user Gate has no Graph interrupt identity")
    signal = UserGateSignal.model_validate(pending.get("value"))
    if (
        result.values.get("business_entry_id") != signal.business_entry_id
        or result.values.get("gate_target_role") != signal.target_role
        or result.values.get("input_high_watermark") != signal.input_high_watermark
    ):
        raise ValueError("user Gate signal differs from its saved parent State")
    checkpoint = result.checkpoint
    if checkpoint.checkpoint_ns or checkpoint.root_checkpoint_id != checkpoint.checkpoint_id:
        raise ValueError("user Gate must wait at an exact root checkpoint")
    if checkpoint.thread_id != run.thread_id or not checkpoint.created_at:
        raise ValueError("user Gate result belongs to another Run or has no saved time")
    cursor = _checkpoint_cursor(checkpoint)
    interrupt_id = stable_internal_identity_id(
        "interrupt_",
        "user-gate",
        run.run_id,
        signal.business_entry_id,
        checkpoint.checkpoint_id,
    )
    system_interrupt = SystemInterrupt(
        interrupt_id=interrupt_id,
        kind=(
            SystemInterruptKind.USER_INPUT_REQUIRED
            if signal.target_role == "component"
            else SystemInterruptKind.APPROVAL_REQUIRED
        ),
        reason_code="user_gate",
        source=SystemInterruptSource(
            module=run.module,
            graph=run.module,
            stage_key=run.stage_key,
            node="user_gate",
            agent=signal.target_role,
        ),
        question=(
            "What would you like to do next?"
            if signal.target_role == "component"
            else "Continue with the completed result?"
        ),
        context_summary=(
            "The current Component result is saved and waiting for your next message."
            if signal.target_role == "component"
            else "The current role result is saved and waiting for a decision."
        ),
        allowed_actions=() if signal.target_role == "component" else ("approve", "revise", "cancel"),
        feedback_high_watermark=signal.input_high_watermark,
        checkpoint_cursor=cursor,
        recovery_policy=SystemInterruptRecoveryPolicy(mode="resume_exact_checkpoint"),
        created_at=datetime.fromisoformat(checkpoint.created_at),
    )
    return OpenInterruptRequest(
        system_interrupt=system_interrupt,
        runtime_envelope=SystemInterruptRuntimeEnvelope(
            graph_interrupt_id=graph_interrupt_id,
            checkpoint_cursor=cursor,
            business_entry_id=signal.business_entry_id,
            accepted_root_checkpoint_id=checkpoint.checkpoint_id,
        ),
        idempotency_key=interrupt_id,
    )


def build_run_pause_open_request(
    run: RunRecord, result: GraphExecutionResult
) -> OpenInterruptRequest:
    """Bind a requested pause to its actual saved root or child interrupt."""

    if len(result.interrupts) != 1 or not isinstance(result.values, Mapping):
        raise ValueError("run pause requires one saved Graph interrupt")
    pending = result.interrupts[0]
    graph_interrupt_id = str(pending.get("id") or "")
    signal = pending.get("value")
    if (
        not graph_interrupt_id
        or not isinstance(signal, Mapping)
        or set(signal) != {"kind", "business_entry_id"}
        or signal.get("kind") != "run_pause"
        or not isinstance(signal.get("business_entry_id"), str)
        or not signal["business_entry_id"]
        or signal["business_entry_id"] != result.values.get("business_entry_id")
    ):
        raise ValueError("run pause signal differs from its saved State")
    checkpoint = result.checkpoint
    if checkpoint.thread_id != run.thread_id or not checkpoint.created_at:
        raise ValueError("run pause belongs to another Run or has no saved time")
    cursor = _checkpoint_cursor(checkpoint)
    interrupt_id = stable_internal_identity_id(
        "interrupt_", "run-pause", run.run_id, graph_interrupt_id,
    )
    system_interrupt = SystemInterrupt(
        interrupt_id=interrupt_id,
        kind=SystemInterruptKind.RUN_PAUSED,
        reason_code="run_pause",
        source=SystemInterruptSource(
            module=run.module, graph=run.module, stage_key=run.stage_key,
            node="pause_before_llm", agent=result.values.get("role"),
        ),
        question="Continue this run?",
        context_summary="The run is paused before its next model request.",
        allowed_actions=("continue",),
        feedback_high_watermark=result.values.get("input_high_watermark", 0),
        checkpoint_cursor=cursor,
        recovery_policy=SystemInterruptRecoveryPolicy(mode="resume_exact_checkpoint"),
        created_at=datetime.fromisoformat(checkpoint.created_at),
    )
    return OpenInterruptRequest(
        system_interrupt=system_interrupt,
        runtime_envelope=SystemInterruptRuntimeEnvelope(
            graph_interrupt_id=graph_interrupt_id, checkpoint_cursor=cursor,
        ),
        idempotency_key=interrupt_id,
    )


def pause_before_llm(
    state: Mapping[str, Any], config: RunnableConfig, runtime: Runtime[RunnerGraphContext]
) -> Command[Literal["compress_memory"]]:
    """Wait at the original dynamic interrupt until an explicit continue."""

    received = interrupt({
        "kind": "run_pause", "business_entry_id": state["business_entry_id"],
    })
    resume = InterruptResumeEnvelope.model_validate(received)
    if (
        resume.decision.action != "continue"
        or resume.decision.message is not None
        or resume.feedback_batch_id is not None
        or resume.selected_feedback_ids
    ):
        raise ValueError("run pause requires explicit continue without business input")
    if resume.checkpoint_cursor != exact_node_input_cursor(config, runtime):
        raise ValueError("continue must target the saved pause")
    return Command(update={"compression_return_to": "main"}, goto="compress_memory")


def gate_route(
    plan: GateResumePlan, *, clarifications: list[Any] | None
) -> Literal["reenter", "continue", "cancel"]:
    """Choose only a route; the Gate never edits the accepted business result."""

    if isinstance(plan, FeedbackWakeResumePlan):
        return "reenter"
    if not isinstance(plan, UserGateResumePlan):
        raise TypeError("unsupported Gate resume plan")
    decision = plan.response.decision
    if decision.message is not None:
        raise ValueError("Gate business text must use content")
    if decision.action == "cancel":
        return "cancel"
    if (
        plan.frozen_batch is not None
        or plan.response.feedback_batch_id is not None
        or decision.action == "revise"
    ):
        return "reenter"
    if decision.action != "approve":
        raise ValueError("unsupported Gate action")
    return "continue" if clarifications == [] else "reenter"


def new_business_entry_id(plan: GateResumePlan) -> str:
    """Use saved Gate, batch/response, original entry and source; never generation."""

    if isinstance(plan, FeedbackWakeResumePlan):
        trigger = plan.frozen_batch.batch.batch_id
    else:
        trigger = (
            plan.frozen_batch.batch.batch_id
            if plan.frozen_batch is not None
            else plan.response.feedback_batch_id or plan.operation_id
        )
    return stable_internal_identity_id(
        "business_entry_",
        "user-gate-reentry",
        plan.interrupt_id,
        trigger,
        plan.business_entry_id,
        plan.source_checkpoint_cursor.checkpoint_id,
    )


def reentry_route_update(
    plan: GateResumePlan, *, clarifications: list[Any] | None = None
) -> dict[str, Any]:
    if isinstance(plan, FeedbackWakeResumePlan) or (
        isinstance(plan, UserGateResumePlan)
        and (plan.frozen_batch is not None or plan.response.feedback_batch_id is not None)
    ):
        reason = "user_feedback"
    elif isinstance(plan, UserGateResumePlan) and plan.response.decision.action == "approve":
        reason = "assess_clarifications" if clarifications is None else "apply_default_clarifications"
    else:
        reason = "revise_requested"
    return {
        "business_entry_id": new_business_entry_id(plan),
        "gate_route": "reenter",
        "reenter_requested": True,
        "gate_reentry_reason": reason,
    }


async def user_gate_node(
    state: Mapping[str, Any], config: RunnableConfig, runtime: Runtime[RunnerGraphContext]
) -> dict[str, Any]:
    """Pause after parent acceptance; on resume, route the saved exact decision."""

    signal = _gate_signal(state)
    received = interrupt(signal.model_dump(mode="json"))
    plan = TypeAdapter(GateResumePlan).validate_python(received)
    source = exact_node_input_cursor(config, runtime)
    if (
        plan.source_checkpoint_cursor != source
        or plan.business_entry_id != signal.business_entry_id
        or plan.accepted_root_checkpoint_id != source.root_checkpoint_id
    ):
        raise ValueError("Gate response does not belong to this saved wait")
    route = gate_route(plan, clarifications=state.get("clarifications"))
    if route == "reenter":
        return reentry_route_update(plan, clarifications=state.get("clarifications"))
    return {"gate_route": route, "reenter_requested": False}


__all__ = [
    "UserGateSignal",
    "build_user_gate_open_request",
    "build_run_pause_open_request",
    "gate_route",
    "new_business_entry_id",
    "pause_before_llm",
    "reentry_route_update",
    "user_gate_node",
]
