from __future__ import annotations

import pytest

from CAD2BIM_backend.infrastructure.langgraph.checkpoint_lineage import (
    describe_checkpoint_lineage,
)


def test_root_checkpoint_lineage_is_self_rooted_and_stable() -> None:
    first = describe_checkpoint_lineage(
        {
            "thread_id": "thread-1",
            "checkpoint_id": "root-1",
            "checkpoint_ns": "",
        }
    )
    second = describe_checkpoint_lineage(
        {
            "checkpoint_ns": "",
            "checkpoint_id": "root-1",
            "thread_id": "thread-1",
        }
    )

    assert first.root_checkpoint_id == "root-1"
    assert first.checkpoint_map == (("", "root-1"),)
    assert first.checkpoint_map_digest == second.checkpoint_map_digest


def test_nested_checkpoint_lineage_uses_exact_root_and_canonical_map() -> None:
    first = describe_checkpoint_lineage(
        {
            "thread_id": "thread-1",
            "checkpoint_id": "child-2",
            "checkpoint_ns": "stage:child",
            "checkpoint_map": {"stage:child": "child-2", "": "root-1"},
        }
    )
    reordered = describe_checkpoint_lineage(
        {
            "thread_id": "thread-1",
            "checkpoint_id": "child-2",
            "checkpoint_ns": "stage:child",
            "checkpoint_map": {"": "root-1", "stage:child": "child-2"},
        }
    )

    assert first.root_checkpoint_id == "root-1"
    assert first.checkpoint_map == (
        ("", "root-1"),
        ("stage:child", "child-2"),
    )
    assert first.checkpoint_map_digest == reordered.checkpoint_map_digest


@pytest.mark.parametrize(
    "configurable",
    [
        {
            "thread_id": "thread-1",
            "checkpoint_id": "child-2",
            "checkpoint_ns": "stage:child",
            "checkpoint_map": {},
        },
        {
            "thread_id": "thread-1",
            "checkpoint_id": "root-2",
            "checkpoint_ns": "",
            "checkpoint_map": {"": "different-root"},
        },
        {
            "thread_id": "thread-1",
            "checkpoint_id": "child-2",
            "checkpoint_ns": "stage:child",
            "checkpoint_map": {"": "root-1", "stage:child": "sibling-child"},
        },
    ],
)
def test_checkpoint_lineage_rejects_wrong_branch_map(configurable: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="branch"):
        describe_checkpoint_lineage(configurable)
