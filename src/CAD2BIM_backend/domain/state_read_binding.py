"""Immutable authority to read one saved Graph state branch."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.control_plane import RunRecord, RunStatus
from CAD2BIM_backend.domain.run_events import (
    RunEvent,
    RunEventAudience,
    RunEventName,
    RunEventScope,
    RunEventType,
    make_run_event,
    stable_run_item_id,
)


class StateReadBindingError(RuntimeError):
    """A claimed exact state reference is absent or contradictory."""


StateResourceField = Literal["requirements", "clarifications", "requirement_md"]
STATE_RESOURCE_FIELDS: tuple[StateResourceField, ...] = (
    "requirements",
    "clarifications",
    "requirement_md",
)


class StateFieldSource(BaseModel):
    """One field owner's exact saved checkpoint within a signed branch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: StateResourceField
    checkpoint_ns: str
    checkpoint_id: str = Field(min_length=1)


class ExactStateReadBinding(BaseModel):
    """Serializable, server-issued checkpoint and authorization identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    checkpoint_cursor: CheckpointCursor
    field_sources: tuple[StateFieldSource, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_cursor_thread(self) -> "ExactStateReadBinding":
        if self.checkpoint_cursor.thread_id != self.thread_id:
            raise ValueError("state binding cursor belongs to another thread")
        fields = tuple(source.field for source in self.field_sources)
        if len(set(fields)) != len(STATE_RESOURCE_FIELDS) or set(fields) != set(
            STATE_RESOURCE_FIELDS
        ):
            raise ValueError("state binding must declare each Resource field once")
        checkpoint_map = dict(self.checkpoint_cursor.checkpoint_map)
        if any(
            checkpoint_map.get(source.checkpoint_ns) != source.checkpoint_id
            for source in self.field_sources
        ):
            raise ValueError("state field source is outside the exact checkpoint map")
        return self

    def source_for(self, field: StateResourceField) -> StateFieldSource:
        for source in self.field_sources:
            if source.field == field:
                return source
        raise ValueError("unknown State Resource field")


def root_field_sources(root_checkpoint_id: str) -> tuple[StateFieldSource, ...]:
    """All finite Resource fields belong to the terminal or read-only root."""

    return tuple(
        StateFieldSource(field=field, checkpoint_ns="", checkpoint_id=root_checkpoint_id)
        for field in STATE_RESOURCE_FIELDS
    )


class CompletionCheckpointRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root_checkpoint_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
    graph_operation_id: str = Field(min_length=1, max_length=255)


def make_completion_checkpoint_event(
    run: RunRecord, *, root_checkpoint_id: str, graph_operation_id: str
) -> RunEvent:
    """Create the one internal completion reference committed with the Run."""

    CompletionCheckpointRef(
        root_checkpoint_id=root_checkpoint_id, graph_operation_id=graph_operation_id
    )
    scope = RunEventScope(module=run.module, stage_key=run.stage_key)
    return make_run_event(
        source_event_key="run-completion-checkpoint",
        sequence=1,
        task_id=run.task_id,
        run_id=run.run_id,
        event_type=RunEventName.RUN_STATUS_CHANGED,
        scope=scope,
        item_id=stable_run_item_id(run_id=run.run_id, scope=scope, local_key="completion"),
        status=RunEventType.SUCCEEDED,
        audience=RunEventAudience.INTERNAL,
        attributes={
            "terminal_root_checkpoint_id": root_checkpoint_id,
            "graph_operation_id": graph_operation_id,
        },
    )


def completion_ref_from_event(run: RunRecord, event: RunEvent) -> CompletionCheckpointRef:
    """Reject every event that is not this Run's unique terminal reference."""

    expected_scope = RunEventScope(module=run.module, stage_key=run.stage_key)
    if (
        run.status is not RunStatus.COMPLETED
        or event.task_id != run.task_id
        or event.run_id != run.run_id
        or event.event_type != RunEventName.RUN_STATUS_CHANGED.value
        or event.audience is not RunEventAudience.INTERNAL
        or event.status is not RunEventType.SUCCEEDED
        or event.scope != expected_scope
        or event.item_id
        != stable_run_item_id(run_id=run.run_id, scope=expected_scope, local_key="completion")
        or set(event.attributes) != {"terminal_root_checkpoint_id", "graph_operation_id"}
    ):
        raise StateReadBindingError("Run has no valid terminal checkpoint reference")
    try:
        return CompletionCheckpointRef(
            root_checkpoint_id=event.attributes["terminal_root_checkpoint_id"],
            graph_operation_id=event.attributes["graph_operation_id"],
        )
    except ValueError as exc:
        raise StateReadBindingError("Run terminal checkpoint reference is malformed") from exc


__all__ = [
    "STATE_RESOURCE_FIELDS",
    "CompletionCheckpointRef",
    "ExactStateReadBinding",
    "StateFieldSource",
    "StateReadBindingError",
    "StateResourceField",
    "completion_ref_from_event",
    "make_completion_checkpoint_event",
    "root_field_sources",
]
