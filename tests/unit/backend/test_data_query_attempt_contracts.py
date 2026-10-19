from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.control_plane import ConflictError
from CAD2BIM_backend.domain.data_query_attempt import (
    DataQueryArtifactRef,
    DataQueryAttemptPreparation,
    DataQueryAttemptRecord,
    DataQueryAttemptStatus,
    DataQueryAttemptTerminal,
    DataQueryDiagnostic,
    DataQueryEngine,
    DataQueryExactInputRef,
    DataQueryResultSummary,
)
from CAD2BIM_backend.infrastructure.memory.data_query_attempt_repository import (
    DataQueryAttemptRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_data_query_attempt import (
    PostgresDataQueryAttemptRepositoryMixin,
)


NOW = datetime(2026, 9, 4, 9, tzinfo=UTC)
SOURCE = DataQueryArtifactRef(
    artifact_id="artifact-query-source",
    version_id="artifact-query-source-v1",
    sha256="a" * 64,
)
SUMMARY = DataQueryResultSummary(
    exact_input_digest="b" * 64,
    query_fingerprint="c" * 64,
    row_schema_digest="d" * 64,
    snapshot_sha256="e" * 64,
    row_count=3,
    snapshot_size_bytes=128,
)
DIAGNOSTIC = DataQueryDiagnostic(
    code="QUERY_EXECUTION_FAILED",
    message="query failed",
    phase="execution",
)


def _input(
    *,
    ordinal: int = 1,
    input_name: str = "records",
    payload_member_id: str = "payload-records",
) -> DataQueryExactInputRef:
    return DataQueryExactInputRef(
        ordinal=ordinal,
        input_name=input_name,
        package_id="package-a",
        payload_member_id=payload_member_id,
    )


def _preparation(**changes: object) -> DataQueryAttemptPreparation:
    values: dict[str, object] = {
        "attempt_id": "dqa-test-a",
        "run_id": "run-a",
        "engine": DataQueryEngine.DUCKDB_SQL,
        "source_artifact": SOURCE,
        "inputs": (_input(),),
        "parameters": (),
        "inline_tables": (),
        "page_size": 100,
        "request_identity": "request-a",
        "request_digest": "e" * 64,
        "created_at": NOW,
    }
    values.update(changes)
    return DataQueryAttemptPreparation.model_validate(values)


def _record(**changes: object) -> DataQueryAttemptRecord:
    values: dict[str, object] = {
        **_preparation().model_dump(mode="python"),
        "sequence": 1,
        "owner_id": "owner-a",
        "tenant_id": "tenant-a",
        "task_id": "task-a",
        "status": DataQueryAttemptStatus.PREPARED,
    }
    values.update(changes)
    return DataQueryAttemptRecord.model_validate(values)


def test_preparation_has_one_exact_input_authority_and_engine_specific_shape() -> None:
    assert set(DataQueryExactInputRef.model_fields) == {
        "ordinal",
        "input_name",
        "package_id",
        "payload_member_id",
        "schema_member_id",
    }
    assert "package_alias" not in DataQueryExactInputRef.model_fields
    assert "payload_name" not in DataQueryExactInputRef.model_fields

    with pytest.raises(ValidationError, match="at least 1 item"):
        _preparation(inputs=())
    with pytest.raises(ValidationError, match="contiguous"):
        _preparation(inputs=(_input(ordinal=2),))
    with pytest.raises(ValidationError, match="input names must be unique"):
        _preparation(
            inputs=(
                _input(),
                _input(ordinal=2, payload_member_id="payload-other"),
            )
        )
    with pytest.raises(ValidationError, match="same exact Payload"):
        _preparation(
            inputs=(
                _input(),
                _input(ordinal=2, input_name="other"),
            )
        )
    with pytest.raises(ValidationError, match="do not accept inline tables"):
        _preparation(inline_tables=({"input_name": "temporary"},))

    python = _preparation(
        engine=DataQueryEngine.PYTHON_ARROW,
        inline_tables=({"input_name": "temporary"},),
    )
    assert python.inline_tables == ({"input_name": "temporary"},)


def test_attempt_state_machine_is_prepared_then_exactly_one_terminal_shape() -> None:
    prepared = _record()
    assert prepared.finished_at is None

    success = _record(
        status=DataQueryAttemptStatus.SUCCEEDED,
        result_summary=SUMMARY,
        finished_at=NOW + timedelta(seconds=1),
    )
    assert success.result_summary == SUMMARY

    failed = _record(
        status=DataQueryAttemptStatus.FAILED,
        diagnostic=DIAGNOSTIC,
        finished_at=NOW + timedelta(seconds=1),
    )
    assert failed.diagnostic == DIAGNOSTIC

    with pytest.raises(ValidationError, match="prepared.*terminal facts"):
        _record(diagnostic=DIAGNOSTIC)
    with pytest.raises(ValidationError, match="requires only a result summary"):
        DataQueryAttemptTerminal(
            status=DataQueryAttemptStatus.SUCCEEDED,
            diagnostic=DIAGNOSTIC,
            finished_at=NOW,
        )
    with pytest.raises(ValidationError, match="requires only a diagnostic"):
        DataQueryAttemptTerminal(
            status=DataQueryAttemptStatus.FAILED,
            result_summary=SUMMARY,
            finished_at=NOW,
        )


@pytest.mark.parametrize(
    "repository_type",
    [DataQueryAttemptRepositoryMixin, PostgresDataQueryAttemptRepositoryMixin],
)
def test_memory_and_postgres_replay_compare_the_same_persisted_facts(repository_type) -> None:
    stored = _record()
    repository_type._require_same_data_query_preparation(
        stored,
        _preparation(created_at=NOW + timedelta(seconds=30)),
    )
    with pytest.raises(ConflictError, match="different facts"):
        repository_type._require_same_data_query_preparation(
            stored,
            _preparation(request_digest="f" * 64),
        )


@pytest.mark.parametrize(
    "repository_type",
    [DataQueryAttemptRepositoryMixin, PostgresDataQueryAttemptRepositoryMixin],
)
def test_memory_and_postgres_terminal_replay_is_immutable(repository_type) -> None:
    finished_at = NOW + timedelta(seconds=1)
    stored = _record(
        status=DataQueryAttemptStatus.FAILED,
        diagnostic=DIAGNOSTIC,
        finished_at=finished_at,
    )
    terminal = DataQueryAttemptTerminal(
        status=DataQueryAttemptStatus.FAILED,
        diagnostic=DIAGNOSTIC,
        finished_at=finished_at,
    )
    repository_type._require_same_data_query_terminal(stored, terminal)
    with pytest.raises(ConflictError, match="terminal state is immutable"):
        repository_type._require_same_data_query_terminal(
            stored,
            terminal.model_copy(
                update={
                    "diagnostic": DIAGNOSTIC.model_copy(update={"retryable": True})
                }
            ),
        )
