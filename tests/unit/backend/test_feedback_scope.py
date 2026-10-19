from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.control_plane import FeedbackScope
from CAD2BIM_backend.domain.feedback_target import (
    FeedbackSubjectActivation,
    FeedbackTargetConflictError,
    compile_active_feedback_subject,
    feedback_claim_cursor,
    feedback_cursor_fingerprint,
    feedback_scope_matches_cursor,
    feedback_subject_id,
    resolve_feedback_scope,
)


def _activation(**updates: object) -> FeedbackSubjectActivation:
    values: dict[str, object] = {
        "task_id": "task-root",
        "run_id": "run-1",
        "business_entry_id": "entry-1",
        "stage_key": "ifc_product_stage_001",
        "agent": "modeler",
        "attempt": 1,
        "writer_generation": 3,
        "checkpoint_id": "checkpoint-1",
        "task_ids": ("task-root", "task-stage"),
        "check_ids": ("check-1",),
    }
    values.update(updates)
    return FeedbackSubjectActivation.model_validate(values)


def test_subject_and_cursor_identities_are_stable_and_server_owned() -> None:
    activation = _activation(task_ids=("task-stage", "task-root"))
    compiled = compile_active_feedback_subject(
        activation,
        revision=1,
        activated_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
    )
    replay = compile_active_feedback_subject(
        activation,
        revision=1,
        activated_at=compiled.activated_at,
    )
    cursor = feedback_claim_cursor(
        run_id=compiled.run_id,
        stage_key=compiled.stage_key,
        role_execution_id=compiled.subject_id,
        writer_generation=compiled.writer_generation,
        checkpoint_id=compiled.checkpoint_id,
        task_ids=compiled.task_ids,
        check_ids=compiled.check_ids,
    )

    assert replay == compiled
    assert compiled.subject_id == feedback_subject_id(activation)
    assert feedback_subject_id(_activation(business_entry_id="entry-2")) != compiled.subject_id
    assert feedback_cursor_fingerprint(cursor) == feedback_cursor_fingerprint(
        cursor.model_copy(update={"writer_generation": 4})
    )


def test_scope_resolution_and_cursor_matching_are_exact() -> None:
    active = compile_active_feedback_subject(
        _activation(),
        revision=1,
        activated_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
    )
    requested = FeedbackScope(
        run_id=active.run_id,
        stage_key=active.stage_key,
        role_execution_id=active.subject_id,
        task_id="task-stage",
        check_id="check-1",
        expected_checkpoint_id=active.checkpoint_id,
    )
    resolved = resolve_feedback_scope(
        task_id=active.task_id,
        run_id=active.run_id,
        run_stage_key=active.stage_key,
        requested_scope=requested,
        active_subject=active,
    )
    cursor = feedback_claim_cursor(
        run_id=active.run_id,
        stage_key=active.stage_key,
        role_execution_id=active.subject_id,
        writer_generation=active.writer_generation,
        checkpoint_id=active.checkpoint_id,
        task_ids=active.task_ids,
        check_ids=active.check_ids,
    )

    assert resolved == requested
    assert feedback_scope_matches_cursor(resolved, cursor)
    assert not feedback_scope_matches_cursor(
        resolved.model_copy(update={"check_id": "check-old"}),
        cursor,
    )
    assert feedback_scope_matches_cursor(
        resolved, cursor.model_copy(update={"writer_generation": 4})
    )
    assert feedback_scope_matches_cursor(
        resolved, cursor.model_copy(update={"checkpoint_id": "checkpoint-next-main"})
    )
    assert not feedback_scope_matches_cursor(
        resolved, cursor.model_copy(update={"role_execution_id": "role-other"})
    )


def test_expected_checkpoint_is_checked_at_acceptance_before_main_advances() -> None:
    active = compile_active_feedback_subject(
        _activation(checkpoint_id="checkpoint-current"),
        revision=1,
        activated_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
    )
    with pytest.raises(FeedbackTargetConflictError, match="checkpoint is stale"):
        resolve_feedback_scope(
            task_id=active.task_id,
            run_id=active.run_id,
            run_stage_key=active.stage_key,
            requested_scope=FeedbackScope(
                run_id=active.run_id,
                stage_key=active.stage_key,
                role_execution_id=active.subject_id,
                expected_checkpoint_id="checkpoint-old",
            ),
            active_subject=active,
        )


def test_public_feedback_scope_has_no_writer_generation_and_survives_takeover() -> None:
    with pytest.raises(ValidationError):
        FeedbackScope.model_validate(
            {"run_id": "run-1", "expected_writer_generation": 3}
        )
    active = compile_active_feedback_subject(
        _activation(writer_generation=3),
        revision=1,
        activated_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
    )
    resolved = resolve_feedback_scope(
        task_id=active.task_id,
        run_id=active.run_id,
        run_stage_key=active.stage_key,
        requested_scope=FeedbackScope(run_id=active.run_id, stage_key=active.stage_key),
        active_subject=active,
    )
    assert resolved.stage_key == active.stage_key


@pytest.mark.parametrize(
    "scope",
    (
        FeedbackScope(
            run_id="run-other",
        ),
        FeedbackScope(
            run_id="run-1",
            stage_key="stage-other",
        ),
        FeedbackScope(
            run_id="run-1",
            stage_key="ifc_product_stage_001",
            role_execution_id="role-other",
        ),
    ),
)
def test_invalid_scope_fails_closed(scope: FeedbackScope) -> None:
    active = compile_active_feedback_subject(
        _activation(),
        revision=1,
        activated_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
    )
    with pytest.raises(FeedbackTargetConflictError):
        resolve_feedback_scope(
            task_id=active.task_id,
            run_id=active.run_id,
            run_stage_key=active.stage_key,
            requested_scope=scope,
            active_subject=active,
        )
