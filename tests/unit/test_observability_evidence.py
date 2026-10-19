from __future__ import annotations

import pytest

from tests.process_support.observability import (
    assert_public_event_evidence,
    nearest_rank_percentile,
    sse_event_ids,
)


def _event(
    sequence: int,
    event_type: str,
    item_id: str,
    *,
    agent: str = "modeler",
    subject_refs: list[str] | None = None,
) -> dict:
    return {
        "schema_version": "2",
        "event_id": f"event-{sequence}",
        "sequence": sequence,
        "event_type": event_type,
        "item_id": item_id,
        "scope": {
            "module": "ifc",
            "stage_key": "ifc_product_stage_001",
            "agent": agent,
            "attempt": 1,
            "model_turn": 1,
        },
        "subject_refs": subject_refs or [],
    }


def test_public_observability_evidence_accepts_paired_bounded_items() -> None:
    events = [
        _event(1, "llm.call.started", "llm-1"),
        _event(2, "llm.call.completed", "llm-1"),
        _event(3, "tool.call.started", "tool-1", subject_refs=["check-1"]),
        _event(4, "tool.call.completed", "tool-1", subject_refs=["check-1"]),
    ]

    evidence = assert_public_event_evidence(events)

    assert evidence["event_count"] == 4
    assert evidence["lifecycle_item_count"] == 2
    assert evidence["linked_tool_item_count"] == 1
    assert evidence["modeler_tool_groups"] == {
        "ifc_product_stage_001|1|1": 1,
    }


@pytest.mark.parametrize(
    "events, message",
    [
        ([_event(2, "llm.call.started", "llm-1"), _event(1, "llm.call.completed", "llm-1")], "sequence"),
        ([_event(1, "llm.call.started", "llm-1")], "one started then one terminal"),
        ([_event(1, "tool.call.completed", "tool-1")], "one started then one terminal"),
    ],
)
def test_public_observability_evidence_rejects_incomplete_streams(
    events: list[dict],
    message: str,
) -> None:
    with pytest.raises(AssertionError, match=message):
        assert_public_event_evidence(events)


def test_nearest_rank_and_sse_ids_are_deterministic() -> None:
    assert nearest_rank_percentile([0.4, 0.1, 0.2], 95) == 0.4
    assert sse_event_ids("id: 2\ndata: {}\n\nid: 5\ndata: {}\n\n") == [2, 5]
