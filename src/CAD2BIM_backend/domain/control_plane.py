"""Domain contracts and state rules for the backend control plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.identity import stable_internal_identity_id
from CAD2BIM_backend.domain.feedback_target import ActiveFeedbackSubject
from CAD2BIM_backend.domain.artifact_refs import FinalIFCArtifactRef
from CAD2BIM_backend.domain.safe_point_publications import (
    checkpoint_cursor_digest as checkpoint_cursor_digest,
)
from CAD2BIM_backend.domain.run_input_manifest import RunInputManifest
from CAD2BIM_backend.domain.system_interrupts import (
    InterruptResumeEnvelope,
    SystemInterruptEnvelope,
    SystemInterruptKind,
    SystemInterruptRuntimeEnvelope,
    SystemInterrupt,
    interrupt_envelope_payload,
    parse_interrupt_envelope,
)


_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ControlPlaneError(Exception):
    """Base class for expected control-plane failures."""


class ResourceNotFoundError(ControlPlaneError, LookupError):
    """The resource does not exist or is outside the caller's owner scope."""


class ConflictError(ControlPlaneError, ValueError):
    """The requested operation conflicts with existing state."""


class IdempotencyConflictError(ConflictError):
    """An idempotency key was reused with a different request."""


class InvalidTransitionError(ConflictError):
    """A state transition is not legal."""


class LeaseConflictError(ConflictError):
    """A Runner attempted to use a lease it no longer owns."""


class TaskStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionMode(StrEnum):
    """Select whether a Run is executed by LangGraph or a Direct MCP session."""

    GRAPH = "graph"
    DIRECT_MCP = "direct_mcp"


class ExecutionProtocolStatus(StrEnum):
    """Whether a Run has an executable immutable protocol binding."""

    PINNED = "pinned"
    QUARANTINED = "quarantined"
    NOT_APPLICABLE = "not_applicable"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})
ACTIVE_RUN_STATUSES = frozenset(
    {
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.PAUSED,
        RunStatus.WAITING_USER,
    }
)

_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.QUEUED,
            RunStatus.PAUSED,
            RunStatus.WAITING_USER,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.PAUSED: frozenset({RunStatus.QUEUED, RunStatus.WAITING_USER, RunStatus.CANCELLED}),
    RunStatus.WAITING_USER: frozenset({RunStatus.QUEUED, RunStatus.PAUSED, RunStatus.CANCELLED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def require_run_transition(current: RunStatus, target: RunStatus) -> None:
    """Reject illegal run transitions while allowing exact idempotent replay."""

    if current == target:
        return
    if target not in _RUN_TRANSITIONS[current]:
        raise InvalidTransitionError(f"run cannot transition from {current} to {target}")


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CreateTaskRequest(FrozenModel):
    title: str = Field(min_length=1, max_length=500)
    instruction: str | None = Field(default=None, max_length=20_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    active_module: str = Field(default="workflow", min_length=1)
    workspace_uri: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1)


class UserTextContent(FrozenModel):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1)


class UserArtifactContent(FrozenModel):
    type: Literal["image", "file"]
    artifact_id: str = Field(min_length=1, max_length=80)
    version_id: str = Field(min_length=1, max_length=80)


UserContentBlock = Annotated[UserTextContent | UserArtifactContent, Field(discriminator="type")]


class StartRunRequest(FrozenModel):
    module: str = Field(default="workflow", min_length=1)
    stage_key: str = Field(default="cad2bim_workflow", min_length=1)
    input_payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=200)
    thread_id: str | None = None
    content: tuple[UserContentBlock, ...] = ()


class FeedbackDeliveryMode(StrEnum):
    STEER = "steer"
    INTERRUPT = "interrupt"


class FeedbackScope(FrozenModel):
    run_id: str = Field(min_length=1, max_length=255)
    stage_key: str | None = Field(default=None, min_length=1, max_length=255)
    role_execution_id: str | None = Field(default=None, min_length=1, max_length=255)
    task_id: str | None = Field(default=None, min_length=1, max_length=255)
    check_id: str | None = Field(default=None, min_length=1, max_length=255)
    expected_checkpoint_id: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_nested_scope(self) -> "FeedbackScope":
        if self.check_id is not None and self.task_id is None:
            raise ValueError("check_id requires task_id")
        if self.role_execution_id is not None and self.stage_key is None:
            raise ValueError("role_execution_id requires stage_key")
        return self


class SubmitFeedbackCommand(FrozenModel):
    client_request_id: str = Field(min_length=1, max_length=200)
    delivery_mode: FeedbackDeliveryMode
    content: tuple[UserContentBlock, ...] = Field(min_length=1)
    scope: FeedbackScope


class FeedbackCommandStatus(StrEnum):
    ACCEPTED = "accepted"
    CLAIMED = "claimed"
    DELIVERED = "delivered"
    SUPERSEDED = "superseded"


class FeedbackCommand(FrozenModel):
    feedback_id: str = Field(min_length=1, max_length=255)
    accepted_sequence: int = Field(ge=1)
    owner_id: str = Field(min_length=1, max_length=255)
    author_principal_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)
    run_id: str = Field(min_length=1, max_length=255)
    client_request_id: str = Field(min_length=1, max_length=200)
    request_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    delivery_mode: FeedbackDeliveryMode
    content: tuple[UserContentBlock, ...] = Field(min_length=1)
    resolved_scope: FeedbackScope
    status: FeedbackCommandStatus = FeedbackCommandStatus.ACCEPTED
    claimed_batch_id: str | None = Field(default=None, min_length=1, max_length=255)
    delivered_checkpoint_id: str | None = Field(default=None, min_length=1, max_length=255)
    created_at: datetime
    delivered_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "FeedbackCommand":
        if self.resolved_scope.run_id != self.run_id:
            raise ValueError("resolved feedback scope must identify its Run")
        if self.status is FeedbackCommandStatus.ACCEPTED:
            if any(
                value is not None
                for value in (
                    self.claimed_batch_id,
                    self.delivered_checkpoint_id,
                    self.delivered_at,
                )
            ):
                raise ValueError("accepted feedback cannot have claim or delivery fields")
        elif self.status is FeedbackCommandStatus.CLAIMED:
            if self.claimed_batch_id is None:
                raise ValueError("claimed feedback requires a batch")
            if self.delivered_checkpoint_id is not None or self.delivered_at is not None:
                raise ValueError("claimed feedback cannot already be delivered")
        elif self.status is FeedbackCommandStatus.DELIVERED:
            if (
                self.claimed_batch_id is None
                or self.delivered_checkpoint_id is None
                or self.delivered_at is None
            ):
                raise ValueError("delivered feedback requires batch and checkpoint identity")
        elif self.status is FeedbackCommandStatus.SUPERSEDED:
            if self.delivered_checkpoint_id is not None or self.delivered_at is not None:
                raise ValueError("superseded feedback cannot be delivered")
        return self


class FeedbackCommandReceipt(FrozenModel):
    feedback_id: str = Field(min_length=1, max_length=255)
    accepted_sequence: int = Field(ge=1)
    delivery_mode: FeedbackDeliveryMode
    resolved_scope: FeedbackScope
    status: FeedbackCommandStatus


class FeedbackClaimCursor(FrozenModel):
    """Exact safe-boundary target used to freeze one delivery batch."""

    run_id: str = Field(min_length=1, max_length=255)
    stage_key: str = Field(min_length=1, max_length=255)
    role_execution_id: str = Field(min_length=1, max_length=255)
    task_ids: tuple[str, ...] = Field(default=(), max_length=4_096)
    check_ids: tuple[str, ...] = Field(default=(), max_length=16_384)
    checkpoint_id: str | None = Field(default=None, min_length=1, max_length=255)
    writer_generation: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_references(self) -> "FeedbackClaimCursor":
        if len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError("feedback cursor task IDs must be unique")
        if len(self.check_ids) != len(set(self.check_ids)):
            raise ValueError("feedback cursor check IDs must be unique")
        return self


class FeedbackDeliveryBatch(FrozenModel):
    batch_id: str = Field(min_length=1, max_length=255)
    owner_id: str = Field(min_length=1, max_length=255)
    run_id: str = Field(min_length=1, max_length=255)
    target_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    claim_high_watermark: int = Field(ge=1)
    member_feedback_ids: tuple[str, ...] = Field(min_length=1)
    contains_interrupt: bool
    claimed_writer_generation: int = Field(ge=1)
    claimed_checkpoint_id: str = Field(min_length=1, max_length=255)
    claimed_checkpoint_cursor: CheckpointCursor
    resume_interrupt_id: str | None = Field(default=None, min_length=1, max_length=255)
    delivered_checkpoint_id: str | None = Field(default=None, min_length=1, max_length=255)
    created_at: datetime
    delivered_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "FeedbackDeliveryBatch":
        if len(self.member_feedback_ids) != len(set(self.member_feedback_ids)):
            raise ValueError("feedback batch members must be unique")
        if self.claimed_checkpoint_id != self.claimed_checkpoint_cursor.checkpoint_id:
            raise ValueError("batch claimed checkpoint must match its exact cursor")
        if (self.delivered_checkpoint_id is None) != (self.delivered_at is None):
            raise ValueError("batch delivery fields must be stored together")
        return self


def feedback_message_id(batch_id: str, feedback_id: str) -> str:
    """Stable Graph message identity for exact retry-safe feedback delivery."""

    return stable_internal_identity_id(
        "feedback_message_", "feedback-delivery", batch_id, feedback_id
    )


class FrozenFeedbackBatch(FrozenModel):
    """Persisted claim and full ordered member bodies for one Graph write."""

    batch: FeedbackDeliveryBatch
    members: tuple[FeedbackCommand, ...] = Field(min_length=1)
    message_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_exact_members(self) -> "FrozenFeedbackBatch":
        member_ids = tuple(item.feedback_id for item in self.members)
        if member_ids != self.batch.member_feedback_ids:
            raise ValueError("frozen batch members differ from persisted membership")
        if tuple(item.accepted_sequence for item in self.members) != tuple(
            sorted(item.accepted_sequence for item in self.members)
        ):
            raise ValueError("frozen batch members must follow acceptance order")
        if any(
            item.run_id != self.batch.run_id
            or item.owner_id != self.batch.owner_id
            or item.claimed_batch_id != self.batch.batch_id
            for item in self.members
        ):
            raise ValueError("frozen batch member belongs to another claim")
        if self.message_ids != tuple(
            feedback_message_id(self.batch.batch_id, feedback_id) for feedback_id in member_ids
        ):
            raise ValueError("frozen batch message IDs are not stable")
        if any(
            member.accepted_sequence > self.batch.claim_high_watermark
            for member in self.members
        ):
            raise ValueError("frozen batch member exceeds its accepted high watermark")
        return self


class RespondGateCommand(FrozenModel):
    interrupt_id: str = Field(min_length=1, max_length=80)
    action: Literal["approve", "revise", "cancel"]
    content: tuple[UserContentBlock, ...] = ()
    idempotency_key: str = Field(min_length=1, max_length=200)


class UserGateResumePlan(FrozenModel):
    origin: Literal["user_response"] = "user_response"
    interrupt_id: str = Field(min_length=1, max_length=80)
    source_checkpoint_cursor: CheckpointCursor
    operation_id: str = Field(min_length=1, max_length=255)
    business_entry_id: str = Field(min_length=1, max_length=255)
    accepted_root_checkpoint_id: str = Field(min_length=1, max_length=255)
    response: InterruptResumeEnvelope
    frozen_batch: FrozenFeedbackBatch | None = None

    @model_validator(mode="after")
    def validate_saved_gate_address(self) -> "UserGateResumePlan":
        if self.response.checkpoint_cursor != self.source_checkpoint_cursor:
            raise ValueError("Gate response cursor contradicts its saved source")
        if self.accepted_root_checkpoint_id != self.source_checkpoint_cursor.root_checkpoint_id:
            raise ValueError("Gate accepted root contradicts its saved source")
        if (
            self.response.decision.action not in {"approve", "revise", "cancel"}
            or self.response.decision.message is not None
        ):
            raise ValueError("Gate response must use its allowed action and feedback body")
        return self


class FeedbackWakeResumePlan(FrozenModel):
    origin: Literal["feedback_wake"] = "feedback_wake"
    interrupt_id: str = Field(min_length=1, max_length=80)
    source_checkpoint_cursor: CheckpointCursor
    operation_id: str = Field(min_length=1, max_length=255)
    business_entry_id: str = Field(min_length=1, max_length=255)
    accepted_root_checkpoint_id: str = Field(min_length=1, max_length=255)
    frozen_batch: FrozenFeedbackBatch

    @model_validator(mode="after")
    def validate_saved_gate_address(self) -> "FeedbackWakeResumePlan":
        if self.accepted_root_checkpoint_id != self.source_checkpoint_cursor.root_checkpoint_id:
            raise ValueError("Gate accepted root contradicts its saved source")
        return self


GateResumePlan = Annotated[
    UserGateResumePlan | FeedbackWakeResumePlan,
    Field(discriminator="origin"),
]


class TerminalGateReentryPlan(FrozenModel):
    """Persisted terminal-root choice that resumes the original Gate operation."""

    source_checkpoint_id: str = Field(min_length=1, max_length=255)
    operation_id: str = Field(min_length=1, max_length=255)
    gate_plan: GateResumePlan


class OpenInterruptRequest(FrozenModel):
    system_interrupt: SystemInterrupt
    runtime_envelope: SystemInterruptRuntimeEnvelope
    idempotency_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_envelope(self) -> "OpenInterruptRequest":
        if self.system_interrupt.checkpoint_cursor != self.runtime_envelope.checkpoint_cursor:
            raise ValueError("public and runtime interrupt cursors must match")
        public = self.system_interrupt
        if public.recovery_policy.mode != "resume_exact_checkpoint":
            raise ValueError("interrupt must resume its exact saved checkpoint")
        if public.kind.value == "run_paused":
            if public.reason_code != "run_pause" or public.source.node != "pause_before_llm":
                raise ValueError("pause source must be the saved Main boundary")
            if (self.runtime_envelope.recovery_binding is not None
                or self.runtime_envelope.business_entry_id is not None
                or self.runtime_envelope.accepted_root_checkpoint_id is not None):
                raise ValueError("run pause cannot carry a Gate recovery binding")
        else:
            if (public.kind.value not in {"approval_required", "user_input_required"}
                or public.reason_code != "user_gate" or public.source.node != "user_gate"):
                raise ValueError("Gate source must be the saved user Gate")
            self.runtime_envelope.require_gate_binding()
        return self

    @property
    def persisted_payload(self) -> dict[str, Any]:
        return interrupt_envelope_payload(self.system_interrupt, self.runtime_envelope)

    @property
    def requested_interrupt_id(self) -> str:
        return self.system_interrupt.interrupt_id

    @property
    def requested_created_at(self) -> datetime:
        return self.system_interrupt.created_at


@dataclass(frozen=True, slots=True)
class DurableRunDeferral:
    """Lease-fenced delay for one exact, retry-safe reclaim restore."""

    reason_code: str
    delay_seconds: float
    expected_writer_generation: int
    expected_recovery_generation: int

    def __post_init__(self) -> None:
        if self.reason_code != "mcp_restore_transient":
            raise ValueError("unsupported durable deferral reason")
        if not 0 < self.delay_seconds <= 300:
            raise ValueError("delay_seconds must be between 0 and 300")
        if self.expected_writer_generation < 1:
            raise ValueError("durable deferral requires a writer generation")
        if self.expected_recovery_generation < 1:
            raise ValueError("durable deferral requires a recovery generation")


class DurableRestoreDeferred(RuntimeError):
    """A retry-safe exact restore should release its Run lease, not fail."""

    def __init__(
        self,
        deferral: DurableRunDeferral,
        *,
        waiting_request: OpenInterruptRequest,
    ) -> None:
        self.deferral = deferral
        self.waiting_request = waiting_request
        super().__init__(deferral.reason_code)


class CreateSavepointRequest(FrozenModel):
    safe_point_key: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)
    checkpoint_ns: str = Field(default="", max_length=1_024)
    stage_key: str = Field(min_length=1)
    checkpoint_next: tuple[str, ...] = ()
    safe_point_context: str = ""
    default_route: str = ""
    mcp_session_id: str | None = None
    mcp_snapshot_id: str | None = None
    active_writer_generation: int | None = Field(default=None, ge=1, strict=True)
    writer_generation: int = Field(default=0, ge=0, strict=True)
    artifact_ids: tuple[str, ...] = ()
    manifest_artifact_id: str = Field(min_length=1, max_length=80)
    manifest_version_no: int = Field(ge=1)
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    is_user_visible: bool = True
    idempotency_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_mcp_reference_pair(self) -> "CreateSavepointRequest":
        if (self.mcp_session_id is None) != (self.mcp_snapshot_id is None):
            raise ValueError("mcp_session_id and mcp_snapshot_id must be provided together")
        if self.mcp_session_id is None and self.writer_generation != 0:
            raise ValueError("Graph-only savepoints cannot claim a writer generation")
        if self.mcp_session_id is None and self.active_writer_generation is not None:
            raise ValueError("Graph-only savepoints cannot claim an active writer generation")
        if self.mcp_session_id is not None and self.writer_generation < 1:
            raise ValueError("MCP savepoints require a writer generation")
        if (
            self.active_writer_generation is not None
            and self.writer_generation > self.active_writer_generation
        ):
            raise ValueError("snapshot writer generation cannot be newer than the active writer")
        if self.manifest_artifact_id not in self.artifact_ids:
            raise ValueError("savepoint manifest Artifact must be in artifact_ids")
        return self

    @property
    def expected_active_writer_generation(self) -> int:
        return self.active_writer_generation or self.writer_generation


class ResumeSavepointRequest(FrozenModel):
    idempotency_key: str = Field(min_length=1)
    feedback: str | None = None
    input_payload: dict[str, Any] = Field(default_factory=dict)


class TaskRecord(FrozenModel):
    task_id: str
    owner_id: str
    tenant_id: str | None = None
    title: str
    instruction: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: TaskStatus
    active_module: str
    workspace_uri: str | None = None
    idempotency_key: str | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def id(self) -> str:
        return self.task_id


class RunRecord(FrozenModel):
    run_id: str
    task_id: str
    owner_id: str
    tenant_id: str | None = None
    principal_id: str | None = None
    module: str
    stage_key: str
    input_payload: dict[str, Any] = Field(default_factory=dict)
    input_manifest: RunInputManifest | None = None
    initial_feedback: FeedbackCommandReceipt | None = None
    pause_request_id: str | None = None
    continue_request_id: str | None = None
    idempotency_key: str
    thread_id: str
    execution_mode: ExecutionMode = ExecutionMode.GRAPH
    execution_protocol_status: ExecutionProtocolStatus = ExecutionProtocolStatus.QUARANTINED
    execution_protocol: ExecutionProtocolBinding | None = None
    execution_protocol_quarantine_reason: str | None = "legacy.unmapped"
    status: RunStatus = RunStatus.QUEUED
    claimed_by: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    claim_count: int = Field(default=0, ge=0)
    writer_generation: int = Field(default=0, ge=0)
    reclaimed_lease: bool = False
    claim_not_before: datetime | None = None
    recovery_generation: int = Field(default=0, ge=0)
    restore_defer_count: int = Field(default=0, ge=0)
    restore_defer_reason_code: str | None = Field(default=None, max_length=100)
    pause_requested: bool = False
    cancel_requested: bool = False
    feedback_pending: bool = False
    feedback_sealed: bool = False
    feedback_transition_subject_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
    )
    active_feedback_subject: ActiveFeedbackSubject | None = None
    resume_from_savepoint_id: str | None = None
    error: str | None = None
    final_artifact_ref: FinalIFCArtifactRef | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None

    @property
    def id(self) -> str:
        return self.run_id

    def require_execution_identity(self) -> tuple[str, str]:
        """Return the server-bound tenant actor for production Graph execution."""

        if self.tenant_id is None or self.principal_id is None:
            raise RuntimeError(
                "production Graph execution requires tenant and principal identity"
            )
        return self.tenant_id, self.principal_id

    @model_validator(mode="after")
    def validate_execution_protocol_mapping(self) -> "RunRecord":
        if self.initial_feedback is not None and (
            self.initial_feedback.resolved_scope.run_id != self.run_id
            or self.initial_feedback.accepted_sequence != 1
            or self.initial_feedback.resolved_scope.role_execution_id is not None
        ):
            raise ValueError("initial input receipt must identify the first unbound input of this Run")
        if self.execution_mode is ExecutionMode.DIRECT_MCP:
            if self.execution_protocol_status is not ExecutionProtocolStatus.NOT_APPLICABLE:
                raise ValueError("Direct MCP Run requires not_applicable protocol status")
            if self.execution_protocol is not None:
                raise ValueError("Direct MCP Run cannot have an execution protocol")
            if self.execution_protocol_quarantine_reason is not None:
                raise ValueError("Direct MCP Run cannot have a protocol quarantine reason")
            if self.status is RunStatus.QUEUED:
                raise ValueError("Direct MCP Run cannot enter the Runner queue")
            if any(
                value is not None
                for value in (
                    self.claimed_by,
                    self.lease_token,
                    self.lease_expires_at,
                    self.heartbeat_at,
                    self.claim_not_before,
                )
            ) or self.claim_count != 0:
                raise ValueError("Direct MCP Run cannot own a Runner lease")
        elif self.execution_protocol_status is ExecutionProtocolStatus.PINNED:
            if self.execution_protocol is None:
                raise ValueError("pinned Run requires an execution protocol")
            if self.execution_protocol_quarantine_reason is not None:
                raise ValueError("pinned Run cannot have a quarantine reason")
        elif self.execution_protocol_status is ExecutionProtocolStatus.QUARANTINED:
            if self.execution_protocol is not None:
                raise ValueError("quarantined Run cannot have an execution protocol")
            if not str(self.execution_protocol_quarantine_reason or "").strip():
                raise ValueError("quarantined Run requires a reason")
        else:
            raise ValueError("Graph Run cannot use not_applicable protocol status")
        if self.input_manifest is not None:
            if (
                self.input_manifest.run_id != self.run_id
                or self.input_manifest.task_id != self.task_id
                or (self.tenant_id is not None and self.input_manifest.tenant_id != self.tenant_id)
                or self.execution_protocol is None
                or self.input_manifest.bundle_id != self.execution_protocol.bundle_id
            ):
                raise ValueError("Run input manifest scope does not match its Run")
        if self.active_feedback_subject is not None:
            subject = self.active_feedback_subject
            if subject.run_id != self.run_id or subject.task_id != self.task_id:
                raise ValueError("active feedback subject scope does not match its Run")
            if subject.writer_generation > self.writer_generation:
                raise ValueError("active feedback subject has a future writer generation")
        if self.feedback_transition_subject_id is not None:
            if not self.feedback_sealed:
                raise ValueError("feedback transition subject requires a sealed Run")
            if (
                self.active_feedback_subject is None
                or self.active_feedback_subject.subject_id != self.feedback_transition_subject_id
            ):
                raise ValueError("feedback transition seal does not match the active subject")
        return self


class InterruptStatus(StrEnum):
    PENDING = "pending"
    RESPONDED = "responded"


class InterruptRecord(FrozenModel):
    interrupt_id: str
    owner_id: str
    task_id: str
    run_id: str
    payload: dict[str, Any]
    status: InterruptStatus = InterruptStatus.PENDING
    response: Any = None
    idempotency_key: str
    response_idempotency_key: str | None = None
    created_at: datetime
    responded_at: datetime | None = None

    @model_validator(mode="after")
    def validate_typed_envelope(self) -> "InterruptRecord":
        envelope = parse_interrupt_envelope(self.payload)
        if envelope.system_interrupt.interrupt_id != self.interrupt_id:
            raise ValueError("stored interrupt identity does not match its public envelope")
        return self

    @property
    def id(self) -> str:
        return self.interrupt_id

    @property
    def typed_envelope(self) -> SystemInterruptEnvelope:
        return parse_interrupt_envelope(self.payload)

    @property
    def system_interrupt(self) -> SystemInterrupt:
        return self.typed_envelope.system_interrupt

    @property
    def runtime_envelope(self) -> SystemInterruptRuntimeEnvelope:
        return self.typed_envelope.runtime

    @property
    def resume_envelope(self) -> InterruptResumeEnvelope | None:
        if self.status is not InterruptStatus.RESPONDED or self.response is None:
            return None
        try:
            return InterruptResumeEnvelope.model_validate(self.response)
        except (TypeError, ValueError):
            return None


class ArtifactRecord(FrozenModel):
    artifact_id: str
    version_id: str
    owner_id: str
    task_id: str
    kind: str
    filename: str
    content_type: str
    version_no: int = Field(default=1, ge=1)
    size_bytes: int = Field(ge=0)
    sha256: str
    idempotency_key: str
    created_at: datetime
    run_id: str | None = None
    version_metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.artifact_id


class SavepointRecord(FrozenModel):
    savepoint_id: str
    owner_id: str
    task_id: str
    run_id: str
    thread_id: str
    safe_point_key: str
    checkpoint_id: str
    checkpoint_ns: str = ""
    stage_key: str
    checkpoint_next: tuple[str, ...] = ()
    safe_point_context: str = ""
    default_route: str = ""
    mcp_session_id: str | None = None
    mcp_snapshot_id: str | None = None
    writer_generation: int = Field(default=0, ge=0, strict=True)
    artifact_ids: tuple[str, ...] = ()
    manifest_artifact_id: str | None = Field(default=None, min_length=1, max_length=80)
    manifest_version_no: int | None = Field(default=None, ge=1)
    manifest_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    is_user_visible: bool = True
    idempotency_key: str
    created_at: datetime

    @model_validator(mode="after")
    def validate_execution_protocol_binding(self) -> "SavepointRecord":
        if self.mcp_session_id is None and self.writer_generation != 0:
            raise ValueError("Graph-only savepoints cannot claim a writer generation")
        if self.mcp_session_id is not None and self.writer_generation < 1:
            raise ValueError("MCP savepoints require a writer generation")
        manifest_fields = (
            self.manifest_artifact_id,
            self.manifest_version_no,
            self.manifest_sha256,
        )
        if any(value is not None for value in manifest_fields) and not all(
            value is not None for value in manifest_fields
        ):
            raise ValueError("savepoint manifest binding must be complete")
        if (
            self.manifest_artifact_id is not None
            and self.manifest_artifact_id not in self.artifact_ids
        ):
            raise ValueError("savepoint manifest Artifact must be in artifact_ids")
        return self

    @property
    def id(self) -> str:
        return self.savepoint_id
