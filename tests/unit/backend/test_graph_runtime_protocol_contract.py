from __future__ import annotations

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest
from CAD2BIM_backend.ports import graph_runtime as graph_runtime_module
from CAD2BIM_backend.ports.graph_runtime import (
    GraphCheckpointDescriptor,
    GraphCheckpointState,
    GraphExecutionResult,
)


@pytest.mark.parametrize("field", ["execution_protocol_id", "execution_protocol_digest", "topology_fingerprint"])
def test_checkpoint_rejects_retired_execution_qualification_fields(field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        GraphCheckpointDescriptor(
            thread_id="thread-1", checkpoint_id="checkpoint-1", **{field: "retired"},
        )


def test_checkpoint_descriptor_contains_execution_position() -> None:
    checkpoint = GraphCheckpointDescriptor(
        thread_id="thread-1",
        checkpoint_id="checkpoint-1",
    )
    assert checkpoint.thread_id == "thread-1"
    assert checkpoint.checkpoint_id == "checkpoint-1"


def test_checkpoint_descriptor_rejects_inconsistent_exact_lineage() -> None:
    mapping = (("", "root-one"),)
    digest = checkpoint_cursor_digest(
        checkpoint_id="root-one", checkpoint_ns="", checkpoint_map=mapping
    )
    GraphCheckpointDescriptor(
        thread_id="thread-one", checkpoint_id="root-one", root_checkpoint_id="root-one",
        checkpoint_map=mapping, checkpoint_map_digest=digest,
    )
    with pytest.raises(ValidationError, match="digest"):
        GraphCheckpointDescriptor(
            thread_id="thread-one", checkpoint_id="root-one", root_checkpoint_id="root-one",
            checkpoint_map=mapping, checkpoint_map_digest="f" * 64,
        )


@pytest.mark.parametrize(
    "lineage",
    [
        {"root_checkpoint_id": "root-one"},
        {"checkpoint_map": (("", "root-one"),)},
        {"root_checkpoint_id": "root-one", "checkpoint_map": (("", "root-one"),)},
    ],
)
def test_checkpoint_descriptor_rejects_partial_lineage(lineage: dict) -> None:
    with pytest.raises(ValidationError, match="lineage"):
        GraphCheckpointDescriptor(
            thread_id="thread-one", checkpoint_id="root-one", **lineage
        )


@pytest.mark.parametrize(
    "root_id,checkpoint_ns,checkpoint_id,mapping",
    [
        ("wrong-root", "", "root-one", (("", "root-one"),)),
        ("root-one", "role:main", "child-one", (("", "root-one"),)),
    ],
)
def test_checkpoint_descriptor_rejects_wrong_root_or_nested_address(
    root_id: str, checkpoint_ns: str, checkpoint_id: str,
    mapping: tuple[tuple[str, str], ...],
) -> None:
    with pytest.raises(ValidationError, match="lineage"):
        GraphCheckpointDescriptor(
            thread_id="thread-one", checkpoint_id=checkpoint_id,
            checkpoint_ns=checkpoint_ns, root_checkpoint_id=root_id,
            checkpoint_map=mapping,
            checkpoint_map_digest=checkpoint_cursor_digest(
                checkpoint_id=checkpoint_id,
                checkpoint_ns=checkpoint_ns,
                checkpoint_map=mapping,
            ),
        )


def test_valid_descriptor_is_reused_by_saved_and_execution_results(monkeypatch) -> None:
    mapping = (("", "root-one"),)
    digest = checkpoint_cursor_digest(
        checkpoint_id="root-one", checkpoint_ns="", checkpoint_map=mapping
    )
    calculations = 0

    def count_digest(**kwargs):
        nonlocal calculations
        calculations += 1
        return checkpoint_cursor_digest(**kwargs)

    monkeypatch.setattr(graph_runtime_module, "checkpoint_cursor_digest", count_digest)
    descriptor = GraphCheckpointDescriptor(
        thread_id="thread-one", checkpoint_id="root-one", root_checkpoint_id="root-one",
        checkpoint_map=mapping, checkpoint_map_digest=digest,
    )
    assert calculations == 1

    saved = GraphCheckpointState(values={"requirement_md": "saved"}, checkpoint=descriptor)
    executed = GraphExecutionResult(values={"requirement_md": "saved"}, checkpoint=descriptor)
    assert saved.checkpoint is descriptor
    assert executed.checkpoint is descriptor
    assert calculations == 1
