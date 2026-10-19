"""Exact, server-owned feedback subject and target contracts."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Self
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from CAD2BIM_backend.domain.control_plane import (
        FeedbackClaimCursor,
        FeedbackScope,
    )


class FeedbackTargetConflictError(ValueError):
    """A syntactically valid target does not identify the active subject."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FeedbackSubjectActivation(_FrozenModel):
    """Runtime-owned identity proposed at one safe interaction boundary."""

    task_id: str = Field(min_length=1, max_length=255)
    run_id: str = Field(min_length=1, max_length=255)
    business_entry_id: str = Field(min_length=1, max_length=255)
    stage_key: str = Field(min_length=1, max_length=255)
    agent: str = Field(min_length=1, max_length=100)
    attempt: int = Field(ge=1)
    writer_generation: int = Field(ge=1)
    checkpoint_id: str | None = Field(default=None, min_length=1, max_length=255)
    stage_epoch: int = Field(default=0, ge=0)
    plan_id: str | None = Field(default=None, min_length=1, max_length=255)
    plan_version: int | None = Field(default=None, ge=1)
    task_ids: tuple[str, ...] = Field(default=(), max_length=4_096)
    check_ids: tuple[str, ...] = Field(default=(), max_length=16_384)

    @field_validator("agent")
    @classmethod
    def normalize_agent(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("agent must not be empty")
        return normalized

    @field_validator("task_ids", "check_ids", mode="before")
    @classmethod
    def normalize_reference_ids(cls, value: object, info) -> tuple[str, ...]:
        items = tuple(str(item).strip() for item in (value or ()))
        label = "task" if info.field_name == "task_ids" else "check"
        if any(not item for item in items):
            raise ValueError(f"{label} IDs must not be empty")
        if len(items) != len(set(items)):
            raise ValueError(f"{label} IDs must be unique")
        return tuple(sorted(items))

    @model_validator(mode="after")
    def validate_plan_identity(self) -> Self:
        if (self.plan_id is None) != (self.plan_version is None):
            raise ValueError("plan identity requires both plan_id and plan_version")
        return self


class ActiveFeedbackSubject(_FrozenModel):
    """The exact subject that API target resolution and one Runner boundary share."""

    subject_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)
    task_id: str = Field(min_length=1, max_length=255)
    run_id: str = Field(min_length=1, max_length=255)
    business_entry_id: str = Field(min_length=1, max_length=255)
    stage_key: str = Field(min_length=1, max_length=255)
    agent: str = Field(min_length=1, max_length=100)
    attempt: int = Field(ge=1)
    writer_generation: int = Field(ge=1)
    checkpoint_id: str | None = Field(default=None, min_length=1, max_length=255)
    stage_epoch: int = Field(ge=0)
    plan_id: str | None = Field(default=None, min_length=1, max_length=255)
    plan_version: int | None = Field(default=None, ge=1)
    task_ids: tuple[str, ...] = Field(default=(), max_length=4_096)
    check_ids: tuple[str, ...] = Field(default=(), max_length=16_384)
    activated_at: datetime

    @model_validator(mode="after")
    def validate_compiled_identity(self) -> Self:
        activation = FeedbackSubjectActivation(
            task_id=self.task_id,
            run_id=self.run_id,
            business_entry_id=self.business_entry_id,
            stage_key=self.stage_key,
            agent=self.agent,
            attempt=self.attempt,
            writer_generation=self.writer_generation,
            checkpoint_id=self.checkpoint_id,
            stage_epoch=self.stage_epoch,
            plan_id=self.plan_id,
            plan_version=self.plan_version,
            task_ids=self.task_ids,
            check_ids=self.check_ids,
        )
        if self.subject_id != feedback_subject_id(activation):
            raise ValueError("active feedback subject identity changed")
        return self


def feedback_subject_id(value: FeedbackSubjectActivation) -> str:
    """Return a stable identity that survives a Runner generation takeover."""

    payload = {
        "task_id": value.task_id,
        "run_id": value.run_id,
        "business_entry_id": value.business_entry_id,
        "stage_key": value.stage_key,
        "agent": value.agent,
        "attempt": value.attempt,
        "stage_epoch": value.stage_epoch,
        "plan_id": value.plan_id,
        "plan_version": value.plan_version,
        "task_ids": value.task_ids,
        "check_ids": value.check_ids,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"feedback_subject_{uuid5(NAMESPACE_URL, canonical).hex}"


def resolve_feedback_scope(
    *,
    task_id: str,
    run_id: str,
    run_stage_key: str,
    requested_scope: "FeedbackScope",
    active_subject: ActiveFeedbackSubject | None,
) -> "FeedbackScope":
    """Resolve a user command against the exact active subject in one transaction."""

    from CAD2BIM_backend.domain.control_plane import FeedbackScope

    if requested_scope.run_id != run_id:
        raise FeedbackTargetConflictError("feedback scope belongs to another Run")
    if active_subject is None:
        if requested_scope.stage_key not in {None, run_stage_key}:
            raise FeedbackTargetConflictError("feedback stage is not active")
        if any(
            value is not None
            for value in (
                requested_scope.role_execution_id,
                requested_scope.check_id,
            )
        ):
            raise FeedbackTargetConflictError("run has no active feedback subject")
        if requested_scope.task_id not in {None, task_id}:
            raise FeedbackTargetConflictError("feedback task is not active")
        if requested_scope.expected_checkpoint_id is not None:
            raise FeedbackTargetConflictError("run has no active checkpoint target")
        return FeedbackScope(
            run_id=run_id,
            stage_key=run_stage_key,
            task_id=requested_scope.task_id,
        )

    if (
        active_subject.task_id != task_id
        or active_subject.run_id != run_id
    ):
        raise FeedbackTargetConflictError("active feedback subject is stale")
    if requested_scope.stage_key not in {None, active_subject.stage_key}:
        raise FeedbackTargetConflictError("feedback stage is not active")
    if requested_scope.role_execution_id not in {None, active_subject.subject_id}:
        raise FeedbackTargetConflictError("feedback role execution is not active")
    if requested_scope.task_id is not None and requested_scope.task_id not in (
        active_subject.task_ids
    ):
        raise FeedbackTargetConflictError("feedback task is not active")
    if requested_scope.check_id is not None and requested_scope.check_id not in (
        active_subject.check_ids
    ):
        raise FeedbackTargetConflictError("feedback check is not active")
    if requested_scope.expected_checkpoint_id is not None:
        if requested_scope.expected_checkpoint_id != active_subject.checkpoint_id:
            raise FeedbackTargetConflictError("feedback checkpoint is stale")
    return FeedbackScope(
        run_id=run_id,
        stage_key=active_subject.stage_key,
        role_execution_id=active_subject.subject_id,
        task_id=requested_scope.task_id,
        check_id=requested_scope.check_id,
        expected_checkpoint_id=requested_scope.expected_checkpoint_id,
    )


def feedback_claim_cursor(
    *,
    run_id: str,
    stage_key: str,
    role_execution_id: str,
    writer_generation: int,
    checkpoint_id: str | None = None,
    task_ids: tuple[str, ...] = (),
    check_ids: tuple[str, ...] = (),
) -> "FeedbackClaimCursor":
    from CAD2BIM_backend.domain.control_plane import FeedbackClaimCursor

    return FeedbackClaimCursor(
        run_id=run_id,
        stage_key=stage_key,
        role_execution_id=role_execution_id,
        task_ids=tuple(sorted(set(task_ids))),
        check_ids=tuple(sorted(set(check_ids))),
        checkpoint_id=checkpoint_id,
        writer_generation=writer_generation,
    )


def feedback_scope_matches_cursor(
    scope: "FeedbackScope",
    cursor: "FeedbackClaimCursor",
) -> bool:
    """Match the accepted business target; checkpoint preconditions ran at acceptance."""

    if scope.run_id != cursor.run_id or scope.stage_key != cursor.stage_key:
        return False
    if scope.role_execution_id not in {None, cursor.role_execution_id}:
        return False
    if scope.task_id is not None and scope.task_id not in cursor.task_ids:
        return False
    if scope.check_id is not None and scope.check_id not in cursor.check_ids:
        return False
    return True


def feedback_cursor_fingerprint(cursor: "FeedbackClaimCursor") -> str:
    """Digest the exact target; writer generation remains a separate replay key."""

    payload = cursor.model_dump(mode="json", exclude={"writer_generation"})
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compile_active_feedback_subject(
    activation: FeedbackSubjectActivation,
    *,
    revision: int,
    activated_at: datetime,
) -> ActiveFeedbackSubject:
    return ActiveFeedbackSubject(
        subject_id=feedback_subject_id(activation),
        revision=revision,
        activated_at=activated_at,
        **activation.model_dump(mode="python"),
    )


__all__ = [
    "ActiveFeedbackSubject",
    "FeedbackSubjectActivation",
    "FeedbackTargetConflictError",
    "compile_active_feedback_subject",
    "feedback_claim_cursor",
    "feedback_cursor_fingerprint",
    "feedback_scope_matches_cursor",
    "feedback_subject_id",
    "resolve_feedback_scope",
]
