"""Durable control-plane receipt for savepoint resume operations."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResumeOperationPhase(StrEnum):
    INTENT = "intent"
    GRAPH_INFLIGHT = "graph_inflight"
    GRAPH_OBSERVED = "graph_observed"
    FINALIZED = "finalized"


class ResumeObservedOutcome(StrEnum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


class ResumeCommand(FrozenModel):
    """Logical command whose exact cursor guards idempotent replay."""

    savepoint_id: str = Field(min_length=1)
    source_checkpoint_cursor: CheckpointCursor
    resume_value: dict[str, Any] = Field(default_factory=dict)


def resume_command_fingerprint(command: ResumeCommand) -> str:
    payload = json.dumps(
        command.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def require_command_fingerprint(value: str) -> None:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError("command_fingerprint must be a lowercase SHA-256 digest")


class ResumeOperationRecord(FrozenModel):
    operation_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    savepoint_id: str = Field(min_length=1)
    command_fingerprint: str
    source_checkpoint_cursor: CheckpointCursor
    phase: ResumeOperationPhase = ResumeOperationPhase.INTENT
    target_mcp_session_id: str | None = None
    observed_checkpoint_cursor: CheckpointCursor | None = None
    observed_outcome: ResumeObservedOutcome | None = None
    attempt_count: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_receipt(self) -> "ResumeOperationRecord":
        require_command_fingerprint(self.command_fingerprint)
        has_checkpoint = self.observed_checkpoint_cursor is not None
        has_outcome = self.observed_outcome is not None
        if has_checkpoint != has_outcome:
            raise ValueError("observed checkpoint and outcome must be present or absent together")
        observed_phase = self.phase in {
            ResumeOperationPhase.GRAPH_OBSERVED,
            ResumeOperationPhase.FINALIZED,
        }
        if observed_phase != has_checkpoint:
            raise ValueError("resume operation phase does not match its graph observation")
        return self


__all__ = [
    "ResumeCommand",
    "ResumeObservedOutcome",
    "ResumeOperationPhase",
    "ResumeOperationRecord",
    "require_command_fingerprint",
    "resume_command_fingerprint",
]
