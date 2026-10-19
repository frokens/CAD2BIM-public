"""Feedback and Gate transactions on the existing Task/Run repository."""

from __future__ import annotations

from typing import Protocol

from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.control_plane import (
    FeedbackClaimCursor,
    FeedbackCommandReceipt,
    FeedbackCommand,
    FrozenFeedbackBatch,
    GateResumePlan,
    OpenInterruptRequest,
    RespondGateCommand,
    RunRecord,
    SubmitFeedbackCommand,
    TerminalGateReentryPlan,
)
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.feedback_target import (
    ActiveFeedbackSubject,
    FeedbackSubjectActivation,
)
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.domain.system_interrupts import (
    SystemInterrupt,
    SystemInterruptEnvelope,
    SystemInterruptResponseReceipt,
    InterruptResumeEnvelope,
)


class FeedbackRepository(Protocol):
    async def get_run(self, owner_id: str, run_id: str) -> RunRecord | None: ...

    async def get_pending_gate(
        self,
        identity: TenantExecutionContext,
        run_id: str,
    ) -> SystemInterrupt | None: ...

    async def activate_feedback_subject(
        self,
        grant: RunnerCheckpointGrant,
        activation: FeedbackSubjectActivation,
    ) -> ActiveFeedbackSubject: ...

    async def accept_feedback(
        self,
        identity: TenantExecutionContext,
        run_id: str,
        command: SubmitFeedbackCommand,
    ) -> FeedbackCommandReceipt: ...

    async def freeze_feedback(
        self,
        grant: RunnerCheckpointGrant,
        cursor: FeedbackClaimCursor,
        *,
        input_checkpoint_cursor: CheckpointCursor,
        delivered_batch_ids: tuple[str, ...] = (),
    ) -> tuple[FrozenFeedbackBatch, ...]: ...

    async def get_frozen_feedback_batch(
        self,
        grant: RunnerCheckpointGrant,
        batch_id: str,
    ) -> FrozenFeedbackBatch: ...

    async def respond_gate(
        self,
        identity: TenantExecutionContext,
        run_id: str,
        interrupt_id: str,
        command: RespondGateCommand,
    ) -> SystemInterruptResponseReceipt: ...

    async def open_gate(
        self,
        grant: RunnerCheckpointGrant,
        request: OpenInterruptRequest,
    ) -> SystemInterruptEnvelope: ...

    async def park_waiting_user(
        self,
        grant: RunnerCheckpointGrant,
        interrupt_id: str,
    ) -> RunRecord: ...

    async def load_gate_resume(
        self,
        grant: RunnerCheckpointGrant,
    ) -> GateResumePlan | None: ...

    async def load_terminal_reentry(
        self,
        grant: RunnerCheckpointGrant,
    ) -> TerminalGateReentryPlan | None: ...

    async def select_terminal_reentry(
        self,
        grant: RunnerCheckpointGrant,
        *,
        source_checkpoint_id: str,
    ) -> TerminalGateReentryPlan | None: ...

    async def seal_role_exit(
        self,
        grant: RunnerCheckpointGrant,
        role_execution_id: str,
        input_high_watermark: int,
        *,
        input_checkpoint_cursor: CheckpointCursor,
        delivered_batch_ids: tuple[str, ...],
    ) -> bool: ...

    async def list_feedback(self, owner_id: str, run_id: str, *, after_sequence: int = 0,
                            limit: int = 100) -> tuple[FeedbackCommand, ...]: ...
    async def get_feedback(self, owner_id: str, run_id: str, feedback_id: str) -> FeedbackCommand | None: ...
    async def pause_requested(self, grant: RunnerCheckpointGrant) -> bool: ...
    async def open_run_pause(self, grant: RunnerCheckpointGrant, request: OpenInterruptRequest) -> RunRecord: ...
    async def load_run_continue(self, grant: RunnerCheckpointGrant) -> tuple[str, str, InterruptResumeEnvelope] | None: ...


__all__ = ["FeedbackRepository"]
