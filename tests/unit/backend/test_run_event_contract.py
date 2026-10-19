from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.run_events import (
    RunEvent,
    RunEventAudience,
    RunEventName,
    RunEventScope,
    RunEventType,
    make_run_event,
    project_progress_tree,
    reclaimed_runtime_terminal_events,
    stable_run_item_id,
    validate_run_event_stream,
)


NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _scope(*, stage: str = "ifc_product_stage_001", attempt: int = 1) -> RunEventScope:
    return RunEventScope(
        module="ifc",
        graph="ifc_stage_graph",
        stage_key=stage,
        graph_node="modeler",
        agent="modeler",
        attempt=attempt,
        model_turn=2,
    )


def _lifecycle() -> tuple[RunEvent, ...]:
    scope = _scope()
    agent_id = stable_run_item_id(run_id="run-1", scope=scope, local_key="agent")
    tool_id = stable_run_item_id(run_id="run-1", scope=scope, local_key="tool:call-1")
    return (
        make_run_event(
            source_event_key="agent:started",
            sequence=1,
            task_id="task-1",
            run_id="run-1",
            event_type=RunEventName.AGENT_STARTED,
            scope=scope,
            item_id=agent_id,
            status=RunEventType.RUNNING,
            summary="Modeler started.",
            occurred_at=NOW,
        ),
        make_run_event(
            source_event_key="tool:call-1:started",
            sequence=2,
            task_id="task-1",
            run_id="run-1",
            event_type=RunEventName.TOOL_CALL_STARTED,
            scope=scope,
            item_id=tool_id,
            parent_item_id=agent_id,
            status=RunEventType.RUNNING,
            subject_refs=("check-1",),
            summary="Create one pipe segment.",
            occurred_at=NOW,
        ),
        make_run_event(
            source_event_key="tool:call-1:completed",
            sequence=3,
            task_id="task-1",
            run_id="run-1",
            event_type=RunEventName.TOOL_CALL_COMPLETED,
            scope=scope,
            item_id=tool_id,
            parent_item_id=agent_id,
            status=RunEventType.SUCCEEDED,
            subject_refs=("check-1",),
            summary="Pipe segment receipt committed.",
            metrics={"tool_calls": 1},
            payload_ref="artifact://evidence/1",
            occurred_at=NOW,
        ),
        make_run_event(
            source_event_key="agent:completed",
            sequence=4,
            task_id="task-1",
            run_id="run-1",
            event_type=RunEventName.AGENT_COMPLETED,
            scope=scope,
            item_id=agent_id,
            status=RunEventType.SUCCEEDED,
            summary="Modeler completed.",
            occurred_at=NOW,
        ),
    )


def test_started_and_terminal_events_share_item_but_not_event_identity() -> None:
    events = _lifecycle()
    assert events[1].item_id == events[2].item_id
    assert events[1].event_id != events[2].event_id


def test_runtime_activity_stream_pairs_independent_calls_and_allows_observed_stage_changes():
    def activity(index, call, stage, phase):
        return RunEvent(event_id=f"activity-{index}", sequence=index, task_id="task-1", run_id="run-1",
            event_type="runtime.activity.changed", occurred_at=NOW, scope=_scope(), item_id="runtime-stream",
            status={"start": RunEventType.RUNNING, "end": RunEventType.SUCCEEDED, "error": RunEventType.FAILED}[phase],
            attributes={"activity_id": call, "stage": stage, "phase": phase})

    events = (activity(1, "model", "model", "start"),
              activity(2, "model", "tool_arguments", "start"),
              activity(3, "model", "tool_arguments", "end"),
              activity(4, "tool-a", "tools", "start"),
              activity(5, "tool-b", "tools", "start"),
              activity(6, "tool-b", "tool_results", "end"),
              activity(7, "tool-a", "tools", "error"))
    validate_run_event_stream(events)
    assert project_progress_tree(events).last_sequence == 7
    with pytest.raises(ValueError, match="before started"):
        validate_run_event_stream((events[2],))
    with pytest.raises(ValueError, match="more than one terminal"):
        validate_run_event_stream((*events, activity(8, "model", "model", "error")))
    with pytest.raises(ValueError, match="after terminal"):
        validate_run_event_stream((*events, activity(8, "tool-b", "tools", "start")))
    with pytest.raises(ValueError, match="identity changed"):
        validate_run_event_stream((*events, activity(8, "other", "model", "start").model_copy(update={"parent_item_id": "other"})))
    with pytest.raises(ValidationError, match="does not match"):
        RunEvent.model_validate({**events[0].model_dump(), "status": "succeeded"})


def test_unknown_named_event_is_readable_for_forward_compatibility() -> None:
    event = RunEvent(
        event_id="event-future",
        sequence=1,
        task_id="task-1",
        run_id="run-1",
        event_type="future.capability.published",
        occurred_at=NOW,
        scope=_scope(),
        item_id="item-future",
        status=RunEventType.SUCCEEDED,
        summary="A newer producer emitted this event.",
    )
    assert event.event_type == "future.capability.published"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("summary", "authorization=Bearer private-token"),
        ("summary", "failed at /Users/private/model.ifc"),
        ("payload_ref", "file:///private/tenant/model.ifc"),
        ("attributes", {"api_key": "private"}),
        ("attributes", {"detail": "DEEPSEEK_API_KEY=private"}),
    ),
)
def test_public_event_rejects_sensitive_runtime_content(field: str, value: object) -> None:
    values = {
        "source_event_key": "agent:started",
        "sequence": 1,
        "task_id": "task-1",
        "run_id": "run-1",
        "event_type": RunEventName.AGENT_STARTED,
        "scope": _scope(),
        "item_id": "item-agent",
        "status": RunEventType.RUNNING,
        field: value,
    }
    with pytest.raises(ValidationError, match="sensitive|physical path"):
        make_run_event(**values)


def test_public_event_enforces_serialized_16_kib_limit() -> None:
    with pytest.raises(ValidationError, match="16 KiB"):
        make_run_event(
            source_event_key="agent:started",
            sequence=1,
            task_id="task-1",
            run_id="run-1",
            event_type=RunEventName.AGENT_STARTED,
            scope=_scope(),
            item_id="item-agent",
            status=RunEventType.RUNNING,
            attributes={f"field-{index}": "x" * 500 for index in range(40)},
        )


def test_stream_and_projection_are_order_and_replay_stable() -> None:
    events = _lifecycle()
    validate_run_event_stream(events)
    expected = project_progress_tree(events)
    replayed = project_progress_tree((events[2], events[0], events[1], events[1], events[3]))
    assert replayed == expected
    assert replayed.last_sequence == 4
    assert replayed.nodes_by_id[events[1].item_id].payload_ref == "artifact://evidence/1"


def test_stream_rejects_terminal_before_start_second_terminal_and_identity_drift() -> None:
    started, completed = _lifecycle()[1:3]
    with pytest.raises(ValueError, match="before started"):
        validate_run_event_stream((completed,))
    failed = completed.model_copy(
        update={
            "event_id": "event-failed",
            "sequence": 4,
            "event_type": RunEventName.TOOL_CALL_FAILED.value,
            "status": RunEventType.FAILED,
        }
    )
    with pytest.raises(ValueError, match="more than one terminal"):
        validate_run_event_stream((started, completed, failed))
    changed = completed.model_copy(update={"parent_item_id": "another-agent"})
    with pytest.raises(ValueError, match="identity changed"):
        validate_run_event_stream((started, changed))


def test_same_agent_identity_isolated_by_attempt_and_stage() -> None:
    scopes = (_scope(stage="stage-a"), _scope(stage="stage-a", attempt=2), _scope(stage="stage-b"))
    identities = {
        stable_run_item_id(run_id="run-1", scope=scope, local_key="agent")
        for scope in scopes
    }
    assert len(identities) == 3


def test_reclaimed_claim_closes_each_open_runtime_lifecycle_once() -> None:
    agent, model = _lifecycle()[:2]
    terminals = reclaimed_runtime_terminal_events(
        (agent, model), task_id="task-1", run_id="run-1", claim_count=2
    )
    assert [event.event_type for event in terminals] == [
        RunEventName.AGENT_FAILED,
        RunEventName.TOOL_CALL_FAILED,
    ]
    validate_run_event_stream((agent, model, *terminals))
    assert reclaimed_runtime_terminal_events(
        (agent, model, *terminals), task_id="task-1", run_id="run-1", claim_count=2
    ) == ()


def test_reclaimed_activity_closes_execution_but_keeps_saved_tool_proposal_pending():
    proposal = _lifecycle()[1].model_copy(update={"attributes": {"source_kind": "tool_request"}})
    activity = RunEvent(event_id="runtime-active", sequence=3, task_id="task-1", run_id="run-1",
        event_type="runtime.activity.changed", scope=_scope(), item_id="runtime-stream", occurred_at=NOW,
        status=RunEventType.RUNNING, attributes={"activity_id": "actual-attempt", "stage": "tools", "phase": "start"})
    closed = reclaimed_runtime_terminal_events((proposal, activity), task_id="task-1", run_id="run-1", claim_count=2)
    assert len(closed) == 1 and closed[0].event_type == "runtime.activity.changed"
    assert closed[0].attributes["phase"] == "error"
    assert closed[0].attributes["activity_id"] == "actual-attempt"
    completed = _lifecycle()[2].model_copy(update={"sequence": 5, "attributes": {"source_kind": "tool_result"}})
    validate_run_event_stream((proposal, activity, *closed, completed))
    assert reclaimed_runtime_terminal_events((proposal, activity, *closed, completed),
        task_id="task-1", run_id="run-1", claim_count=3) == ()


def test_internal_event_keeps_the_same_contract() -> None:
    event = make_run_event(
        source_event_key="safe-point:linked",
        sequence=1,
        task_id="task-1",
        run_id="run-1",
        event_type=RunEventName.RUN_STATUS_CHANGED,
        audience=RunEventAudience.INTERNAL,
        scope=_scope(),
        item_id="item-safe-point",
        status=RunEventType.SUCCEEDED,
        attributes={"checkpoint_ref": "checkpoint-logical-id"},
    )
    assert event.audience is RunEventAudience.INTERNAL
