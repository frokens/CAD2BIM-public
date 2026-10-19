from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.safe_point_publications import (
    MAX_CHECKPOINT_MAP_ENTRIES,
    CheckpointSafePointPublicationRequest,
    LinkedCursorScope,
    PrepareSafePointPublicationRequest,
    SafePointPublicationPhase,
    SafePointPublicationRecord,
    checkpoint_cursor_digest,
)


CHECKPOINT_MAP = (
    ("", "root-checkpoint-1"),
    ("stage_dispatch:child", "checkpoint-1"),
)
CHECKPOINT_DIGEST = checkpoint_cursor_digest(
    checkpoint_id="checkpoint-1",
    checkpoint_ns="stage_dispatch:child",
    checkpoint_map=CHECKPOINT_MAP,
)


def _record_values() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "operation_id": "safe-point-operation-1",
        "owner_id": "owner-1",
        "task_id": "task-1",
        "run_id": "run-1",
        "thread_id": "thread-1",
        "stage_key": "ifc_product_stage_001",
        "safe_point_kind": "pre_stage",
        "mcp_session_id": "mcp-session-1",
        "mcp_snapshot_id": "mcp-snapshot-1",
        "artifact_id": "artifact-1",
        "artifact_version_id": "artifact-version-1",
        "checksum": "a" * 64,
        "writer_generation": 1,
        "idempotency_key": "safe-point-idempotency-1",
        "created_at": now,
        "updated_at": now,
    }


def test_publication_phase_requires_exact_phase_attachments() -> None:
    prepared = SafePointPublicationRecord(**_record_values())
    assert prepared.phase is SafePointPublicationPhase.PREPARED
    assert prepared.checkpoint_id is None
    assert prepared.savepoint_id is None

    checkpointed = SafePointPublicationRecord(
        **_record_values(),
        phase="checkpointed",
        checkpoint_id="checkpoint-1",
        checkpoint_ns="stage_dispatch:child",
        root_checkpoint_id="root-checkpoint-1",
        checkpoint_map=CHECKPOINT_MAP,
        checkpoint_map_digest=CHECKPOINT_DIGEST,
        checkpoint_next=("modeler",),
    )
    assert checkpointed.phase is SafePointPublicationPhase.CHECKPOINTED

    linked = checkpointed.model_copy(
        update={"phase": SafePointPublicationPhase.LINKED, "savepoint_id": "savepoint-1"}
    )
    assert SafePointPublicationRecord.model_validate(linked).phase is (
        SafePointPublicationPhase.LINKED
    )

    with pytest.raises(ValidationError, match="checkpoint attachments"):
        SafePointPublicationRecord(
            **_record_values(),
            phase="checkpointed",
            checkpoint_id="checkpoint-1",
        )
    with pytest.raises(ValidationError, match="savepoint_id"):
        SafePointPublicationRecord(
            **_record_values(),
            phase="linked",
            checkpoint_id="checkpoint-1",
            checkpoint_ns="",
            root_checkpoint_id="checkpoint-1",
            checkpoint_map=(("", "checkpoint-1"),),
            checkpoint_map_digest=checkpoint_cursor_digest(
                checkpoint_id="checkpoint-1",
                checkpoint_ns="",
                checkpoint_map=(("", "checkpoint-1"),),
            ),
            checkpoint_next=(),
        )
    with pytest.raises(ValidationError, match="prepared publication"):
        SafePointPublicationRecord(
            **_record_values(),
            checkpoint_id="checkpoint-1",
            checkpoint_ns="",
            root_checkpoint_id="checkpoint-1",
            checkpoint_map=(("", "checkpoint-1"),),
            checkpoint_map_digest=checkpoint_cursor_digest(
                checkpoint_id="checkpoint-1",
                checkpoint_ns="",
                checkpoint_map=(("", "checkpoint-1"),),
            ),
            checkpoint_next=(),
        )


def test_publication_validates_digests_timestamps_and_is_immutable() -> None:
    record = SafePointPublicationRecord(**_record_values())

    with pytest.raises(ValidationError, match="checksum"):
        SafePointPublicationRecord(**{**_record_values(), "checksum": "not-a-digest"})
    with pytest.raises(ValidationError, match="checkpoint_map_digest"):
        SafePointPublicationRecord(
            **_record_values(),
            phase="checkpointed",
            checkpoint_id="checkpoint-1",
            checkpoint_ns="",
            root_checkpoint_id="checkpoint-1",
            checkpoint_map=(("", "checkpoint-1"),),
            checkpoint_map_digest="not-a-digest",
            checkpoint_next=(),
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        SafePointPublicationRecord(
            **{
                **_record_values(),
                "created_at": datetime.now(),
            }
        )
    with pytest.raises(ValidationError):
        record.phase = SafePointPublicationPhase.LINKED  # type: ignore[misc]


def test_publication_requires_complete_optional_source_cursor() -> None:
    with pytest.raises(ValidationError, match="source checkpoint"):
        SafePointPublicationRecord(
            **_record_values(),
            source_checkpoint_ns="stage:child",
        )

    record = SafePointPublicationRecord(
        **_record_values(),
        source_checkpoint_id="checkpoint-source",
        source_checkpoint_ns="stage:child",
        checkpoint_as_node="safe_point_boundary",
    )
    assert record.source_checkpoint_id == "checkpoint-source"


@pytest.mark.parametrize(
    "field",
    (
        "task_id",
        "run_id",
        "mcp_session_id",
        "mcp_snapshot_id",
        "artifact_id",
        "artifact_version_id",
        "savepoint_id",
    ),
)
def test_publication_fk_identifiers_match_database_length(field: str) -> None:
    values = _record_values()
    values.update(
        {
            "phase": "linked",
            "checkpoint_id": "checkpoint-1",
            "checkpoint_ns": "",
            "root_checkpoint_id": "checkpoint-1",
            "checkpoint_map": (("", "checkpoint-1"),),
            "checkpoint_map_digest": checkpoint_cursor_digest(
                checkpoint_id="checkpoint-1",
                checkpoint_ns="",
                checkpoint_map=(("", "checkpoint-1"),),
            ),
            "checkpoint_next": (),
            "savepoint_id": "savepoint-1",
            field: "x" * 81,
        }
    )

    with pytest.raises(ValidationError, match="at most 80 characters"):
        SafePointPublicationRecord(**values)


def test_prepare_fk_identifiers_match_database_length() -> None:
    values = {
        key: value
        for key, value in _record_values().items()
        if key
        not in {
            "owner_id",
            "task_id",
            "run_id",
            "created_at",
            "updated_at",
        }
    }
    values["mcp_session_id"] = "x" * 81

    with pytest.raises(ValidationError, match="at most 80 characters"):
        PrepareSafePointPublicationRequest(**values)


def test_checkpoint_publication_rejects_lineage_over_shared_topology_cap() -> None:
    checkpoint_map = (("", "checkpoint-root"),) + tuple(
        (f"stage_{index}:task-{index}", f"checkpoint-{index}")
        for index in range(1, MAX_CHECKPOINT_MAP_ENTRIES + 1)
    )
    namespace, checkpoint_id = checkpoint_map[-1]

    with pytest.raises(ValidationError, match="at most 64 items"):
        CheckpointSafePointPublicationRequest(
            operation_id="over-cap-publication",
            writer_generation=1,
            checkpoint_id=checkpoint_id,
            checkpoint_ns=namespace,
            root_checkpoint_id="checkpoint-root",
            checkpoint_map=checkpoint_map,
            checkpoint_map_digest=checkpoint_cursor_digest(
                checkpoint_id=checkpoint_id,
                checkpoint_ns=namespace,
                checkpoint_map=checkpoint_map,
            ),
        )


def test_linked_cursor_scope_requires_run_identity_without_a_discriminator() -> None:
    scope = LinkedCursorScope(
        owner_id="owner-1",
        task_id="task-1",
        run_id="run-1",
        stage_key="ifc_product_stage_001",
        safe_point_kind="pre_stage",
    )

    assert "schema_version" not in scope.model_dump(mode="json")
    with pytest.raises(ValidationError):
        LinkedCursorScope(owner_id="owner-1", task_id="task-1", run_id="")
