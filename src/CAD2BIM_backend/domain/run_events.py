"""The single append-only RunEvent contract and its progress projection."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from CAD2BIM_backend.domain.control_plane import RunRecord, RunStatus


MAX_PUBLIC_EVENT_BYTES = 16 * 1024
_EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:^|[_.:-])(?:api[_-]?key|authorization|password|secret|token)(?:$|[_.:-])"
)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:api[_-]?key|authorization|password|secret|token)\s*[:=]"
    r"|bearer\s+[a-z0-9._~+/-]+"
)
_PHYSICAL_PATH = re.compile(
    r"(?i)(?:file://|/(?:users|private|tmp|var|home)/|(?:^|[^a-z0-9])[a-z]:[\\/])"
)


def runtime_activity_item_id(run_id: str) -> str:
    return f"runtime_{uuid5(NAMESPACE_URL, f'{run_id}:activity').hex}"


def compression_activity_id(run_id: str, namespace: str, step: int) -> str:
    return f"compression_{uuid5(NAMESPACE_URL, f'{run_id}:{namespace}:{step}').hex}"


class RunEventType(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


TERMINAL_EVENTS = frozenset(
    {
        RunEventType.SUCCEEDED,
        RunEventType.FAILED,
        RunEventType.CANCELLED,
        RunEventType.SKIPPED,
    }
)


class RunEventAudience(StrEnum):
    PUBLIC = "public"
    OPERATOR = "operator"
    INTERNAL = "internal"


class RunEventName(StrEnum):
    RUNTIME_ACTIVITY_CHANGED = "runtime.activity.changed"
    TASK_TITLE_UPDATED = "task.title.updated"
    RUN_STATUS_CHANGED = "run.status.changed"
    STAGE_STATUS_CHANGED = "stage.status.changed"
    BUSINESS_PLAN_PUBLISHED = "business.plan.published"
    BUSINESS_NODE_CHANGED = "business.node.changed"
    AGENT_STARTED = "agent.started"
    AGENT_COMPLETED = "agent.completed"
    AGENT_FAILED = "agent.failed"
    AGENT_WAITING = "agent.waiting"
    LLM_CALL_STARTED = "llm.call.started"
    LLM_CALL_COMPLETED = "llm.call.completed"
    LLM_CALL_FAILED = "llm.call.failed"
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_COMPLETED = "tool.call.completed"
    TOOL_CALL_FAILED = "tool.call.failed"
    STRUCTURED_FINAL_STARTED = "structured_final.started"
    STRUCTURED_FINAL_COMPLETED = "structured_final.completed"
    STRUCTURED_FINAL_FAILED = "structured_final.failed"
    CONTEXT_LOADED = "context.loaded"
    CONTEXT_OMITTED = "context.omitted"
    DOCUMENT_PUBLISHED = "document.published"
    COMPONENT_PUBLISHED = "component.published"
    COMPONENT_UPDATED = "component.updated"
    ARTIFACT_PUBLISHED = "artifact.published"
    INTERRUPT_OPENED = "interrupt.opened"
    INTERRUPT_RESOLVED = "interrupt.resolved"
    FEEDBACK_ACCEPTED = "feedback.accepted"
    FEEDBACK_BATCH_CLAIMED = "feedback.batch.claimed"
    FEEDBACK_BATCH_DELIVERED = "feedback.batch.delivered"
    SAVEPOINT_REQUESTED = "savepoint.requested"
    SAVEPOINT_LINKED = "savepoint.linked"
    AGENT_COMMENTARY_PUBLISHED = "agent.commentary.published"


class RunEventScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    module: str = Field(min_length=1, max_length=100)
    graph: str | None = Field(default=None, min_length=1, max_length=200)
    stage_key: str | None = Field(default=None, min_length=1, max_length=200)
    graph_node: str | None = Field(default=None, min_length=1, max_length=200)
    agent: str | None = Field(default=None, min_length=1, max_length=200)
    attempt: int | None = Field(default=None, ge=1)
    model_turn: int | None = Field(default=None, ge=1)


class RunEvent(BaseModel):
    """One immutable public or operational change for one stable work item."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, max_length=80)
    sequence: int = Field(ge=1)
    task_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    event_type: str = Field(min_length=3, max_length=200, pattern=_EVENT_TYPE_PATTERN)
    audience: RunEventAudience = RunEventAudience.PUBLIC
    occurred_at: datetime
    scope: RunEventScope
    item_id: str = Field(min_length=1, max_length=255)
    parent_item_id: str | None = Field(default=None, min_length=1, max_length=255)
    subject_refs: tuple[str, ...] = Field(default=(), max_length=64)
    status: RunEventType
    summary: str = Field(default="", max_length=2_000)
    metrics: dict[str, int | float] = Field(default_factory=dict, max_length=64)
    payload_ref: str | None = Field(default=None, max_length=1_000)
    attributes: dict[str, str] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def validate_public_boundary(self) -> "RunEvent":
        if self.parent_item_id == self.item_id:
            raise ValueError("event item cannot be its own parent")
        if self.event_type == "runtime.activity.changed":
            phase = self.attributes.get("phase")
            expected = {"start": RunEventType.RUNNING, "end": RunEventType.SUCCEEDED,
                        "error": RunEventType.FAILED}
            if not self.attributes.get("activity_id") or not self.attributes.get("stage") or phase not in expected:
                raise ValueError("runtime activity requires activity_id, stage and start/end/error phase")
            if self.status is not expected[phase]:
                raise ValueError("runtime activity status does not match its phase")
        _reject_sensitive(self.summary, field_name="summary")
        if self.payload_ref is not None:
            _reject_sensitive(self.payload_ref, field_name="payload_ref")
            if _PHYSICAL_PATH.search(self.payload_ref):
                raise ValueError("payload_ref must not contain a physical path")
        for key, value in self.attributes.items():
            if _SENSITIVE_KEY.search(key):
                raise ValueError("event attributes contain a sensitive key")
            _reject_sensitive(value, field_name=f"attributes.{key}")
        if self.audience is RunEventAudience.PUBLIC:
            _reject_physical_path(self.summary, field_name="summary")
            for key, value in self.attributes.items():
                _reject_physical_path(value, field_name=f"attributes.{key}")
            payload = json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(payload) > MAX_PUBLIC_EVENT_BYTES:
                raise ValueError("public RunEvent must not exceed 16 KiB")
        return self


class ProgressNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    parent_id: str | None
    kind: str
    label: str
    status: RunEventType
    stage_key: str
    agent: str
    attempt: int
    subject_refs: tuple[str, ...] = ()
    metrics: dict[str, int | float] = Field(default_factory=dict)
    attributes: dict[str, str] = Field(default_factory=dict)
    payload_ref: str | None = None
    first_sequence: int
    last_sequence: int


class ProgressTree(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    nodes_by_id: dict[str, ProgressNode] = Field(alias="nodesById")
    children_by_parent: dict[str, tuple[str, ...]] = Field(alias="childrenByParent")
    root_ids: tuple[str, ...] = Field(alias="rootIds")
    last_sequence: int = Field(alias="lastSequence")


def stable_run_item_id(
    *,
    run_id: str,
    scope: RunEventScope,
    local_key: str,
) -> str:
    identity = json.dumps(
        [run_id, scope.model_dump(mode="json"), local_key],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"item_{uuid5(NAMESPACE_URL, f'cad2bim:run-item:{identity}').hex}"


def make_run_event(
    *,
    source_event_key: str,
    sequence: int,
    task_id: str,
    run_id: str,
    event_type: RunEventName,
    scope: RunEventScope,
    item_id: str,
    status: RunEventType,
    audience: RunEventAudience = RunEventAudience.PUBLIC,
    parent_item_id: str | None = None,
    subject_refs: tuple[str, ...] = (),
    summary: str = "",
    metrics: dict[str, int | float] | None = None,
    payload_ref: str | None = None,
    attributes: dict[str, str] | None = None,
    occurred_at: datetime | None = None,
) -> RunEvent:
    event_id = f"event_{uuid5(NAMESPACE_URL, f'cad2bim:run-event:{run_id}:{source_event_key}').hex}"
    return RunEvent(
        event_id=event_id,
        sequence=sequence,
        task_id=task_id,
        run_id=run_id,
        event_type=event_type.value,
        audience=audience,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        scope=scope,
        item_id=item_id,
        parent_item_id=parent_item_id,
        subject_refs=subject_refs,
        status=status,
        summary=summary,
        metrics=metrics or {},
        payload_ref=payload_ref,
        attributes=attributes or {},
    )


def make_task_title_event(run: RunRecord, *, occurred_at: datetime) -> RunEvent:
    """Notify consumers to reread the one Task title saved in this transaction."""
    return make_run_event(
        source_event_key=f"task-title:{run.task_id}", sequence=1,
        task_id=run.task_id, run_id=run.run_id,
        event_type=RunEventName.TASK_TITLE_UPDATED,
        scope=RunEventScope(module=run.module, stage_key=run.stage_key,
                            graph_node="initialize_component"),
        item_id=f"task-title:{run.task_id}", status=RunEventType.SUCCEEDED,
        occurred_at=occurred_at,
    )


def make_run_status_event(
    *, source_event_key: str, task_id: str, run_id: str,
    module: str, stage_key: str | None, run_status: RunStatus,
    pause_requested: bool, reason: str, occurred_at: datetime,
) -> RunEvent:
    """Project one committed Run control change onto the existing public event."""

    scope = RunEventScope(module=module, stage_key=stage_key)
    status = {
        RunStatus.RUNNING: RunEventType.RUNNING,
        RunStatus.QUEUED: RunEventType.QUEUED,
        RunStatus.PAUSED: RunEventType.WAITING,
        RunStatus.WAITING_USER: RunEventType.WAITING,
    }[run_status]
    return make_run_event(
        source_event_key=source_event_key, sequence=1, task_id=task_id, run_id=run_id,
        event_type=RunEventName.RUN_STATUS_CHANGED, scope=scope,
        item_id=stable_run_item_id(run_id=run_id, scope=scope, local_key=source_event_key),
        status=status, occurred_at=occurred_at,
        attributes={
            "run_status": run_status.value,
            "pause_requested": "true" if pause_requested else "false",
            "reason": reason,
        },
    )


def validate_run_event_stream(events: tuple[RunEvent, ...]) -> None:
    """Validate stable item identity and exactly one terminal lifecycle event."""

    items: dict[str, tuple[str | None, RunEventScope, bool, bool]] = {}
    activity_items: dict[str, tuple[str | None, RunEventScope]] = {}
    activities: dict[tuple[str, str], bool] = {}
    for event in sorted(events, key=lambda item: item.sequence):
        if event.event_type == "runtime.activity.changed":
            identity = (event.parent_item_id, event.scope)
            if event.item_id in items or activity_items.get(event.item_id, identity) != identity:
                raise ValueError(f"item identity changed: {event.item_id}")
            activity_items[event.item_id] = identity
            key = (event.item_id, event.attributes["activity_id"])
            phase = event.attributes["phase"]
            if phase == "start":
                if activities.get(key) is True:
                    raise ValueError(f"activity {key[1]} started after terminal")
                activities[key] = False
            else:
                if key not in activities:
                    raise ValueError(f"activity {key[1]} received terminal event before started")
                if activities[key]:
                    raise ValueError(f"activity {key[1]} received more than one terminal event")
                activities[key] = True
            continue
        if event.item_id in activity_items:
            raise ValueError(f"item identity changed: {event.item_id}")
        started = event.event_type.endswith(".started")
        lifecycle = _lifecycle_prefix(event.event_type)
        current = items.get(event.item_id)
        if current is None:
            if lifecycle is not None and not started:
                raise ValueError(
                    f"item {event.item_id} received terminal event before started"
                )
            items[event.item_id] = (
                event.parent_item_id,
                event.scope,
                started,
                not started,
            )
            continue
        parent_item_id, scope, has_started, has_terminal = current
        if parent_item_id != event.parent_item_id or scope != event.scope:
            raise ValueError(f"item identity changed: {event.item_id}")
        if started:
            if has_started:
                raise ValueError(f"item {event.item_id} started more than once")
            if has_terminal:
                raise ValueError(f"item {event.item_id} started after terminal")
            has_started = True
        else:
            if lifecycle is not None and not has_started:
                raise ValueError(
                    f"item {event.item_id} received terminal event before started"
                )
            if has_terminal:
                raise ValueError(
                    f"item {event.item_id} received more than one terminal event"
                )
            has_terminal = True
        items[event.item_id] = (parent_item_id, scope, has_started, has_terminal)


def reclaimed_runtime_terminal_events(
    events: tuple[RunEvent, ...],
    *,
    task_id: str,
    run_id: str,
    claim_count: int,
    occurred_at: datetime | None = None,
) -> tuple[RunEvent, ...]:
    """Close lifecycle items abandoned by an expired Runner claim."""

    if claim_count < 2:
        raise ValueError("reclaimed runtime events require claim_count >= 2")
    ordered = sorted(events, key=lambda item: item.sequence)
    open_items: dict[str, RunEvent] = {}
    open_activities: dict[tuple[str, str], RunEvent] = {}
    for event in ordered:
        if event.task_id != task_id or event.run_id != run_id:
            raise ValueError("runtime event does not belong to the reclaimed Run")
        if event.event_type == "runtime.activity.changed":
            key = (event.item_id, event.attributes["activity_id"])
            if event.attributes["phase"] == "start":
                open_activities[key] = event
            else:
                open_activities.pop(key, None)
            continue
        if event.attributes.get("source_kind") == "tool_request":
            # This is a checkpointed proposal, not proof of an execution lost
            # with the lease. Its original action still needs its ToolMessage.
            continue
        if _lifecycle_prefix(event.event_type) is None:
            continue
        if event.event_type.endswith(".started"):
            open_items[event.item_id] = event
        else:
            open_items.pop(event.item_id, None)

    next_sequence = max((event.sequence for event in ordered), default=0) + 1
    terminal_at = occurred_at or datetime.now(timezone.utc)
    terminals: list[RunEvent] = []
    for started in sorted(open_items.values(), key=lambda item: item.sequence):
        prefix = _lifecycle_prefix(started.event_type)
        if prefix is None:  # pragma: no cover - guarded while collecting
            continue
        terminals.append(
            make_run_event(
                source_event_key=f"runner-lease-reclaimed:{claim_count}:{started.event_id}",
                sequence=next_sequence,
                task_id=task_id,
                run_id=run_id,
                event_type=_reclaimed_failure_name(prefix),
                scope=started.scope,
                item_id=started.item_id,
                parent_item_id=started.parent_item_id,
                status=RunEventType.FAILED,
                subject_refs=started.subject_refs,
                summary=started.summary or started.event_type,
                metrics={"reclaimed_claim_count": claim_count},
                attributes={"reason_code": "runner_lease_reclaimed"},
                occurred_at=terminal_at,
            )
        )
        next_sequence += 1
    for activity in sorted(open_activities.values(), key=lambda item: item.sequence):
        terminals.append(make_run_event(
            source_event_key=f"runner-lease-reclaimed:{claim_count}:{activity.event_id}",
            sequence=next_sequence, task_id=task_id, run_id=run_id,
            event_type=RunEventName.RUNTIME_ACTIVITY_CHANGED, scope=activity.scope,
            item_id=activity.item_id, parent_item_id=activity.parent_item_id,
            status=RunEventType.FAILED, occurred_at=terminal_at,
            attributes={**activity.attributes, "phase": "error", "reason_code": "runner_lease_reclaimed"},
            metrics={"reclaimed_claim_count": claim_count},
        ))
        next_sequence += 1
    return tuple(terminals)


def project_progress_tree(events: Iterable[RunEvent]) -> ProgressTree:
    """Create the deterministic Agent/step projection from the one event stream."""

    ordered = _deduplicate(events)
    validate_run_event_stream(tuple(ordered))
    nodes: dict[str, ProgressNode] = {}
    for event in ordered:
        current = nodes.get(event.item_id)
        nodes[event.item_id] = ProgressNode(
            node_id=event.item_id,
            parent_id=event.parent_item_id,
            kind=_node_kind(event.event_type),
            label=event.summary or event.event_type,
            status=event.status,
            stage_key=event.scope.stage_key or "run",
            agent=event.scope.agent or "system",
            attempt=event.scope.attempt or 1,
            subject_refs=event.subject_refs or (current.subject_refs if current else ()),
            metrics={**(current.metrics if current else {}), **event.metrics},
            attributes={**(current.attributes if current else {}), **event.attributes},
            payload_ref=event.payload_ref or (current.payload_ref if current else None),
            first_sequence=current.first_sequence if current else event.sequence,
            last_sequence=event.sequence,
        )

    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for node in sorted(nodes.values(), key=lambda item: (item.first_sequence, item.node_id)):
        if node.parent_id is None:
            roots.append(node.node_id)
        else:
            if node.parent_id not in nodes:
                raise ValueError(f"missing parent {node.parent_id} for {node.node_id}")
            children.setdefault(node.parent_id, []).append(node.node_id)
    return ProgressTree(
        nodesById=nodes,
        childrenByParent={key: tuple(value) for key, value in children.items()},
        rootIds=tuple(roots),
        lastSequence=ordered[-1].sequence if ordered else 0,
    )


def _deduplicate(events: Iterable[RunEvent]) -> list[RunEvent]:
    by_event_id: dict[str, RunEvent] = {}
    by_sequence: dict[int, str] = {}
    for event in events:
        existing = by_event_id.get(event.event_id)
        if existing is not None:
            if existing != event:
                raise ValueError(f"event_id collision: {event.event_id}")
            continue
        other_event_id = by_sequence.get(event.sequence)
        if other_event_id is not None:
            raise ValueError(
                f"sequence {event.sequence} belongs to both {other_event_id} and {event.event_id}"
            )
        by_event_id[event.event_id] = event
        by_sequence[event.sequence] = event.event_id
    return sorted(by_event_id.values(), key=lambda item: item.sequence)


def _lifecycle_prefix(event_type: str) -> str | None:
    prefix = event_type.rpartition(".")[0]
    return prefix if prefix in {"agent", "llm.call", "tool.call", "structured_final"} else None


def _reclaimed_failure_name(prefix: str) -> RunEventName:
    return {
        "agent": RunEventName.AGENT_FAILED,
        "llm.call": RunEventName.LLM_CALL_FAILED,
        "tool.call": RunEventName.TOOL_CALL_FAILED,
        "structured_final": RunEventName.STRUCTURED_FINAL_FAILED,
    }[prefix]


def _node_kind(event_type: str) -> str:
    if event_type.startswith("agent."):
        return "agent"
    if event_type.startswith("llm.call."):
        return "llm"
    if event_type.startswith("tool.call."):
        return "tool"
    if event_type.startswith("structured_final."):
        return "structured_final"
    return "step"


def _reject_sensitive(value: str, *, field_name: str) -> None:
    if _SENSITIVE_VALUE.search(value):
        raise ValueError(f"{field_name} contains sensitive runtime content")


def _reject_physical_path(value: str, *, field_name: str) -> None:
    if _PHYSICAL_PATH.search(value):
        raise ValueError(f"{field_name} contains a physical path")


__all__ = [
    "MAX_PUBLIC_EVENT_BYTES",
    "ProgressNode",
    "ProgressTree",
    "RunEvent",
    "RunEventAudience",
    "RunEventName",
    "RunEventScope",
    "RunEventType",
    "TERMINAL_EVENTS",
    "make_run_event",
    "make_task_title_event",
    "project_progress_tree",
    "reclaimed_runtime_terminal_events",
    "stable_run_item_id",
    "validate_run_event_stream",
]
