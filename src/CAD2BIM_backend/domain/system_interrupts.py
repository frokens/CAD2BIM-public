"""Current public system-interrupt and private resume contracts."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor


_REASON_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_STABLE_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
_SENSITIVE_PUBLIC_TEXT = re.compile(
    r"(?i)(?:/(?:Users|home|private|tmp|var|etc|opt|root|run|srv|mnt)/|[A-Z]:\\|\\\\)"
    r"|(?:api[_-]?key|token|secret|password|authorization)[\"']?\s*[:=]"
)
_TARGET_REF_PREFIXES = {
    "task": ("task_", "task-"),
    "check": ("check_", "check-", "global_check_"),
    "feedback": ("feedback_", "feedback-"),
    "operation": ("operation_",),
    "plan": ("plan_", "plan-", "global_plan_"),
    "component": ("component_", "component-"),
    "requirement": (
        "requirement_",
        "requirement-",
        "component_requirement_",
    ),
}
_ALLOWED_DECISION_ACTIONS = frozenset(
    {"approve", "cancel", "continue", "fix", "regenerate", "retry", "revise"}
)


def _normalize_decision_action(value: str) -> str:
    action = value.strip().lower()
    if action not in _ALLOWED_DECISION_ACTIONS:
        raise ValueError("unsupported interrupt action")
    return action


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SystemInterruptKind(StrEnum):
    USER_INPUT_REQUIRED = "user_input_required"
    RUN_PAUSED = "run_paused"
    CLARIFICATION_REQUIRED = "clarification_required"
    APPROVAL_REQUIRED = "approval_required"
    POLICY_DECISION_REQUIRED = "policy_decision_required"
    RETRYABLE_FAILURE = "retryable_failure"
    REVIEW_FIX_EXHAUSTED = "review_fix_exhausted"
    RESOURCE_MISSING = "resource_missing"
    FEEDBACK_PREEMPTED = "feedback_preempted"


class SystemInterruptSource(FrozenModel):
    module: str = Field(min_length=1, max_length=64)
    graph: str = Field(min_length=1, max_length=128)
    stage_key: str = Field(min_length=1, max_length=255)
    node: str = Field(min_length=1, max_length=128)
    agent: str | None = Field(default=None, min_length=1, max_length=64)
    attempt: int | None = Field(default=None, ge=1)

    @field_validator("module", "graph", "stage_key", "node", "agent")
    @classmethod
    def validate_public_identifier(cls, value: str | None) -> str | None:
        if value is not None and _STABLE_PUBLIC_ID.fullmatch(value) is None:
            raise ValueError("interrupt source must use stable public identifiers")
        return value


class SystemInterruptTargetRef(FrozenModel):
    kind: Literal[
        "task",
        "check",
        "stage",
        "feedback",
        "operation",
        "plan",
        "component",
        "requirement",
    ]
    ref_id: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_public_reference(self) -> "SystemInterruptTargetRef":
        if not is_public_interrupt_target_ref(self.kind, self.ref_id):
            raise ValueError("target ref must be a stable public domain identifier")
        return self


class SystemInterruptRecoveryPolicy(FrozenModel):
    mode: Literal["resume_exact_checkpoint", "restore_pre_stage_then_resume"]
    retry_node: str | None = Field(default=None, min_length=1, max_length=128)
    requires_mcp_restore: bool = False

    @field_validator("retry_node")
    @classmethod
    def validate_retry_node(cls, value: str | None) -> str | None:
        if value is not None and _STABLE_PUBLIC_ID.fullmatch(value) is None:
            raise ValueError("retry_node must be a stable public identifier")
        return value

    @model_validator(mode="after")
    def validate_restore_mode(self) -> "SystemInterruptRecoveryPolicy":
        if self.mode == "restore_pre_stage_then_resume" and not self.requires_mcp_restore:
            raise ValueError("pre-stage recovery requires MCP restore")
        if self.mode == "resume_exact_checkpoint" and self.requires_mcp_restore:
            raise ValueError("exact checkpoint resume cannot declare MCP restore")
        return self


class SystemInterruptRecoveryBinding(FrozenModel):
    """Runner-only identity of the immutable pre-stage recovery bundle."""

    savepoint_id: str = Field(min_length=1, max_length=80)
    stage_key: str = Field(min_length=1, max_length=255)
    mcp_session_id: str = Field(min_length=1, max_length=80)
    snapshot_id: str = Field(min_length=1, max_length=80)
    artifact_id: str = Field(min_length=1, max_length=80)
    artifact_version_id: str = Field(min_length=1, max_length=80)

    @field_validator(
        "savepoint_id",
        "stage_key",
        "mcp_session_id",
        "snapshot_id",
        "artifact_id",
        "artifact_version_id",
    )
    @classmethod
    def validate_stable_identity(cls, value: str) -> str:
        if _STABLE_PUBLIC_ID.fullmatch(value) is None:
            raise ValueError("recovery binding must use stable identifiers")
        return value


class SystemInterrupt(FrozenModel):
    interrupt_id: str = Field(min_length=1, max_length=80)
    kind: SystemInterruptKind
    reason_code: str
    source: SystemInterruptSource
    question: str = Field(min_length=1, max_length=1_000)
    context_summary: str = Field(min_length=1, max_length=2_000)
    allowed_actions: tuple[str, ...] = Field(max_length=7)
    feedback_high_watermark: int = Field(default=0, ge=0)
    target_refs: tuple[SystemInterruptTargetRef, ...] = Field(default=(), max_length=32)
    checkpoint_cursor: CheckpointCursor
    recovery_policy: SystemInterruptRecoveryPolicy
    created_at: datetime

    @field_validator("reason_code")
    @classmethod
    def validate_reason_code(cls, value: str) -> str:
        if _REASON_CODE.fullmatch(value) is None:
            raise ValueError("reason_code must be a stable lowercase code")
        return value

    @field_validator("interrupt_id")
    @classmethod
    def validate_interrupt_id(cls, value: str) -> str:
        if _STABLE_PUBLIC_ID.fullmatch(value) is None:
            raise ValueError("interrupt_id must be a stable public identifier")
        return value

    @field_validator("question", "context_summary")
    @classmethod
    def reject_sensitive_public_text(cls, value: str) -> str:
        if _SENSITIVE_PUBLIC_TEXT.search(value):
            raise ValueError("public interrupt text contains sensitive runtime detail")
        return value

    @field_validator("allowed_actions")
    @classmethod
    def validate_allowed_actions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip().lower() for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed_actions must be unique")
        if any(item not in _ALLOWED_DECISION_ACTIONS for item in normalized):
            raise ValueError("allowed_actions contains an unsupported action")
        return normalized

    @model_validator(mode="after")
    def validate_actions_for_kind(self) -> "SystemInterrupt":
        if self.kind is SystemInterruptKind.USER_INPUT_REQUIRED:
            if self.allowed_actions:
                raise ValueError("input waiting does not offer decision actions")
        elif not self.allowed_actions:
            raise ValueError("decision interrupts require an allowed action")
        return self

    @field_validator("target_refs")
    @classmethod
    def validate_target_refs(
        cls, value: tuple[SystemInterruptTargetRef, ...]
    ) -> tuple[SystemInterruptTargetRef, ...]:
        identities = tuple((item.kind, item.ref_id) for item in value)
        if len(set(identities)) != len(identities):
            raise ValueError("target_refs must be unique")
        return value

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value


class SystemInterruptRuntimeEnvelope(FrozenModel):
    """Runner-only data required to resume the exact Graph boundary."""

    graph_interrupt_id: str = Field(min_length=1, max_length=255)
    checkpoint_cursor: CheckpointCursor
    recovery_binding: SystemInterruptRecoveryBinding | None = None
    business_entry_id: str | None = Field(default=None, min_length=1, max_length=255)
    # The persisted Gate pause root already contains the accepted parent result.
    accepted_root_checkpoint_id: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_gate_binding_pair(self) -> "SystemInterruptRuntimeEnvelope":
        if (self.business_entry_id is None) != (self.accepted_root_checkpoint_id is None):
            raise ValueError("Gate business entry and accepted root must be bound together")
        if (
            self.accepted_root_checkpoint_id is not None
            and self.accepted_root_checkpoint_id != self.checkpoint_cursor.root_checkpoint_id
        ):
            raise ValueError("Gate accepted root contradicts its exact cursor")
        return self

    def require_gate_binding(self) -> tuple[str, str]:
        if self.business_entry_id is None or self.accepted_root_checkpoint_id is None:
            raise ValueError("Gate resume requires a business entry and accepted root")
        return self.business_entry_id, self.accepted_root_checkpoint_id


class SystemInterruptEnvelope(FrozenModel):
    """Persisted envelope; only ``system_interrupt`` may cross the API boundary."""

    system_interrupt: SystemInterrupt
    runtime: SystemInterruptRuntimeEnvelope

    @model_validator(mode="after")
    def validate_cursor_identity(self) -> "SystemInterruptEnvelope":
        if self.system_interrupt.checkpoint_cursor != self.runtime.checkpoint_cursor:
            raise ValueError("public and runtime interrupt cursors must match")
        requires_restore = self.system_interrupt.recovery_policy.requires_mcp_restore
        binding = self.runtime.recovery_binding
        if requires_restore and binding is None:
            raise ValueError("MCP recovery policy requires a private recovery binding")
        if not requires_restore and binding is not None:
            raise ValueError("exact-checkpoint recovery must not carry a recovery binding")
        if binding is not None and binding.stage_key != self.system_interrupt.source.stage_key:
            raise ValueError("recovery binding belongs to a different Stage")
        return self


class SystemInterruptDecision(FrozenModel):
    action: str = Field(min_length=1, max_length=32)
    message: str | None = Field(default=None, max_length=4_000)

    @field_validator("action")
    @classmethod
    def normalize_action(cls, value: str) -> str:
        return _normalize_decision_action(value)


class InterruptResumeEnvelope(FrozenModel):
    """Exact, immutable response operation used by the Runner to resume a wait."""

    decision: SystemInterruptDecision
    checkpoint_cursor: CheckpointCursor
    response_high_watermark: int = Field(ge=0)
    expected_writer_generation: int = Field(ge=1)
    feedback_batch_id: str | None = Field(default=None, min_length=1, max_length=255)
    selected_feedback_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_feedback_selection(self) -> "InterruptResumeEnvelope":
        if len(self.selected_feedback_ids) != len(set(self.selected_feedback_ids)):
            raise ValueError("resume feedback selection must be unique")
        if (self.feedback_batch_id is None) != (not self.selected_feedback_ids):
            raise ValueError(
                "resume feedback batch identity and selected members must be stored together"
            )
        return self


class SystemInterruptResponseReceipt(FrozenModel):
    interrupt_id: str
    status: Literal["resolved"] = "resolved"
    action: str
    response_high_watermark: int = Field(ge=0)
    feedback_batch_id: str | None = Field(default=None, min_length=1, max_length=255)
    responded_at: datetime

    @field_validator("action")
    @classmethod
    def normalize_action(cls, value: str) -> str:
        return _normalize_decision_action(value)

    @field_validator("interrupt_id")
    @classmethod
    def validate_interrupt_id(cls, value: str) -> str:
        if _STABLE_PUBLIC_ID.fullmatch(value) is None:
            raise ValueError("interrupt_id must be a stable public identifier")
        return value

    @field_validator("responded_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("responded_at must be timezone-aware")
        return value


def interrupt_envelope_payload(
    system_interrupt: SystemInterrupt,
    runtime: SystemInterruptRuntimeEnvelope,
) -> dict[str, Any]:
    return SystemInterruptEnvelope(
        system_interrupt=system_interrupt,
        runtime=runtime,
    ).model_dump(mode="json")


def parse_interrupt_envelope(payload: dict[str, Any]) -> SystemInterruptEnvelope:
    return SystemInterruptEnvelope.model_validate(payload)


def is_public_interrupt_target_ref(kind: str, ref_id: str) -> bool:
    """Return whether a reference is safe and meaningful in the public contract."""

    if _STABLE_PUBLIC_ID.fullmatch(ref_id) is None:
        return False
    if kind == "stage":
        return ref_id in {"ifc_generation", "project_init"} or ref_id.startswith(
            (
                "ifc_product_stage_",
                "ifc_system_stage_",
                "stage_",
                "stage-",
                "requirement_",
                "cad_",
                "workflow_",
            )
        )
    prefixes = _TARGET_REF_PREFIXES.get(kind)
    return prefixes is not None and ref_id.startswith(prefixes)


__all__ = [
    "InterruptResumeEnvelope",
    "SystemInterruptDecision",
    "SystemInterruptEnvelope",
    "SystemInterruptKind",
    "SystemInterruptRecoveryBinding",
    "SystemInterruptRecoveryPolicy",
    "SystemInterruptResponseReceipt",
    "SystemInterruptRuntimeEnvelope",
    "SystemInterruptSource",
    "SystemInterruptTargetRef",
    "SystemInterrupt",
    "interrupt_envelope_payload",
    "is_public_interrupt_target_ref",
    "parse_interrupt_envelope",
]
