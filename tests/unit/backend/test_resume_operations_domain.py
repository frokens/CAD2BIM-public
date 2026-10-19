from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.resume_operations import (
    ResumeCommand,
    ResumeOperationRecord,
    resume_command_fingerprint,
)
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest


def _cursor(
    checkpoint_id: str = "checkpoint-1",
    *,
    checkpoint_ns: str = "ifc:gas",
    root_checkpoint_id: str = "checkpoint-root",
) -> CheckpointCursor:
    checkpoint_map = (
        ("", root_checkpoint_id),
        (checkpoint_ns, checkpoint_id),
    )
    return CheckpointCursor(
        thread_id="thread-1",
        checkpoint_id=checkpoint_id,
        checkpoint_ns=checkpoint_ns,
        root_checkpoint_id=root_checkpoint_id,
        checkpoint_map=checkpoint_map,
        checkpoint_digest=checkpoint_cursor_digest(
            checkpoint_id=checkpoint_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_map=checkpoint_map,
        ),
    )


def test_resume_command_fingerprint_is_stable_and_content_sensitive() -> None:
    command = ResumeCommand(
        savepoint_id="savepoint-1",
        source_checkpoint_cursor=_cursor(),
        resume_value={"feedback": "continue"},
    )

    assert resume_command_fingerprint(command) == resume_command_fingerprint(command)
    assert resume_command_fingerprint(command) != resume_command_fingerprint(
        command.model_copy(update={"resume_value": {"feedback": "regenerate"}})
    )
    assert resume_command_fingerprint(command) != resume_command_fingerprint(
        command.model_copy(
            update={"source_checkpoint_cursor": _cursor(root_checkpoint_id="checkpoint-other-root")}
        )
    )


def test_resume_operation_requires_observation_fields_to_match_phase() -> None:
    values = {
        "operation_id": "resume-operation-1",
        "owner_id": "owner-1",
        "task_id": "task-1",
        "run_id": "run-1",
        "savepoint_id": "savepoint-1",
        "command_fingerprint": "a" * 64,
        "source_checkpoint_cursor": _cursor(),
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    assert ResumeOperationRecord(**values).phase.value == "intent"

    with pytest.raises(ValidationError, match="phase does not match"):
        ResumeOperationRecord(
            **values,
            phase="graph_observed",
        )
    with pytest.raises(ValidationError, match="present or absent together"):
        ResumeOperationRecord(
            **values,
            phase="graph_inflight",
            observed_checkpoint_cursor=_cursor("checkpoint-2"),
        )


def test_resume_operation_requires_exact_observed_cursor() -> None:
    now = datetime.now(timezone.utc)
    values = {
        "operation_id": "resume-operation-1",
        "owner_id": "owner-1",
        "task_id": "task-1",
        "run_id": "run-1",
        "savepoint_id": "savepoint-1",
        "command_fingerprint": "a" * 64,
        "source_checkpoint_cursor": _cursor(),
        "created_at": now,
        "updated_at": now,
    }
    intent = ResumeOperationRecord(**values)
    assert intent.source_checkpoint_cursor == _cursor()

    with pytest.raises(ValidationError, match="present or absent together"):
        ResumeOperationRecord(
            **values,
            phase="graph_observed",
            observed_outcome="completed",
        )

    observed = _cursor("checkpoint-observed")
    receipt = ResumeOperationRecord(
        **values,
        phase="graph_observed",
        observed_checkpoint_cursor=observed,
        observed_outcome="completed",
    )
    assert receipt.observed_checkpoint_cursor == observed

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ResumeOperationRecord(
            **values,
            phase="graph_observed",
            observed_checkpoint_cursor=observed,
            observed_outcome="completed",
            observed_checkpoint_id="another-checkpoint",
        )
