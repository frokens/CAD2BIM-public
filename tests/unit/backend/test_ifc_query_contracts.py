from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.control_plane import ConflictError
from CAD2BIM_backend.domain.ifc_query import (
    IfcQueryArtifactRef,
    IfcQueryAttemptPreparation,
    IfcQueryAttemptRecord,
    IfcQueryAttemptStatus,
    IfcQueryAttemptTerminal,
    IfcQueryBudgetSummary,
    IfcQueryDiagnostic,
    IfcQueryExactInputRef,
    IfcQueryInputBinding,
    IfcQueryPage,
    IfcQueryPrepared,
)
from CAD2BIM_backend.infrastructure.postgres.repository_ifc_query import (
    PostgresIfcQueryRepositoryMixin,
)


NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)
SOURCE = IfcQueryArtifactRef(
    artifact_id="artifact_query_source",
    version_id="artifact_query_source_v1",
    sha256="a" * 64,
)
RESULT = IfcQueryArtifactRef(
    artifact_id="artifact_query_result",
    version_id="artifact_query_result_v1",
    sha256="b" * 64,
)
BUDGET = IfcQueryBudgetSummary(max_result_bytes=1024 * 1024)
DIAGNOSTIC = IfcQueryDiagnostic(
    code="QUERY_FAILED",
    message="query failed",
    phase="execution",
    execution_started=True,
    retryable=False,
    detail={"exception_type": "ValueError", "line": 7},
)


def _input(*, ordinal: int = 1, input_name: str = "records") -> IfcQueryExactInputRef:
    return IfcQueryExactInputRef(
        ordinal=ordinal,
        input_name=input_name,
        package_id="package_a",
        payload_member_id=f"payload_{input_name}",
        schema_member_id=f"schema_{input_name}",
    )


def _preparation(**changes: object) -> IfcQueryAttemptPreparation:
    values: dict[str, object] = {
        "attempt_id": "ifc_query_attempt_a",
        "run_id": "run_a",
        "ifc_model_id": "ifc_model_a",
        "ifc_revision_id": "ifc_revision_a",
        "inputs": (_input(),),
        "source_artifact": SOURCE,
        "request_identity": "request_a",
        "request_digest": "d" * 64,
        "budget": BUDGET,
        "created_at": NOW,
    }
    values.update(changes)
    return IfcQueryAttemptPreparation.model_validate(values)


def _record(**changes: object) -> IfcQueryAttemptRecord:
    values = {
        **_preparation().model_dump(),
        "owner_id": "owner_a",
        "tenant_id": "tenant_a",
        "task_id": "task_a",
        "status": IfcQueryAttemptStatus.PREPARED,
    }
    values.update(changes)
    return IfcQueryAttemptRecord.model_validate(values)


def test_fresh_selector_and_exact_persisted_input_are_distinct_contracts() -> None:
    selector = IfcQueryInputBinding(
        input_name="records",
        package_alias="data_001",
        payload_name="records",
    )
    exact = _input()
    assert selector.package_alias == "data_001"
    assert exact.package_id == "package_a"
    assert exact.schema_member_id == "schema_records"


def test_preparation_requires_ordered_unique_exact_inputs_and_aware_time() -> None:
    assert _preparation(inputs=()).inputs == ()
    inputs = tuple(_input(ordinal=index, input_name=f"input_{index}") for index in range(1, 17))
    assert _preparation(inputs=inputs).inputs[-1].ordinal == 16
    with pytest.raises(ValidationError, match="contiguous"):
        _preparation(inputs=(_input(ordinal=2),))
    with pytest.raises(ValidationError, match="input names must be unique"):
        _preparation(inputs=(_input(), _input(ordinal=2)))
    with pytest.raises(ValidationError, match="cannot be the attempt itself"):
        _preparation(repair_parent_attempt_id="ifc_query_attempt_a")
    with pytest.raises(ValidationError, match="timezone-aware"):
        _preparation(created_at=NOW.replace(tzinfo=None))


def test_prepared_result_distinguishes_insert_from_exact_replay() -> None:
    record = _record()
    assert IfcQueryPrepared(attempt=record, created=True).created is True
    assert IfcQueryPrepared(attempt=record, created=False).created is False


def test_diagnostic_detail_is_json_typed_and_bounded() -> None:
    assert DIAGNOSTIC.detail == {"exception_type": "ValueError", "line": 7}
    with pytest.raises(ValidationError, match="detail exceeds"):
        IfcQueryDiagnostic.model_validate(
            {**DIAGNOSTIC.model_dump(), "detail": {"text": "x" * 2100}}
        )


def test_terminal_and_record_enforce_one_way_terminal_shapes() -> None:
    success = IfcQueryAttemptTerminal(
        status=IfcQueryAttemptStatus.SUCCEEDED,
        result_artifact=RESULT,
        finished_at=NOW + timedelta(seconds=1),
    )
    assert success.result_artifact == RESULT
    with pytest.raises(ValidationError, match="must be terminal"):
        IfcQueryAttemptTerminal(status=IfcQueryAttemptStatus.PREPARED, finished_at=NOW)
    with pytest.raises(ValidationError, match="requires a result"):
        IfcQueryAttemptTerminal(status=IfcQueryAttemptStatus.SUCCEEDED, finished_at=NOW)
    with pytest.raises(ValidationError, match="requires a diagnostic"):
        IfcQueryAttemptTerminal(status=IfcQueryAttemptStatus.FAILED, finished_at=NOW)
    with pytest.raises(ValidationError, match="cannot contain diagnostics"):
        _record(diagnostics=(DIAGNOSTIC,))
    with pytest.raises(ValidationError, match="cannot finish before"):
        _record(
            status=IfcQueryAttemptStatus.SUCCEEDED,
            result_artifact=RESULT,
            finished_at=NOW - timedelta(seconds=1),
        )


def test_page_accepts_json_values_and_checks_counts() -> None:
    page = IfcQueryPage(
        attempt_alias="query_001",
        source_sha256="a" * 64,
        result_bytes=10,
        total_count=3,
        returned_count=3,
        results=({"name": "Project"}, [1, 2], "done"),
    )
    assert page.results[0] == {"name": "Project"}
    with pytest.raises(ValidationError, match="returned_count"):
        IfcQueryPage.model_validate({**page.model_dump(), "returned_count": 2})


def test_repository_replay_comparison_ignores_only_insert_owned_fields() -> None:
    stored = _record()
    replay = _preparation(
        attempt_id="ignored_retry_attempt",
        created_at=NOW + timedelta(seconds=5),
    )
    PostgresIfcQueryRepositoryMixin._require_same_ifc_query_preparation(stored, replay)
    with pytest.raises(ConflictError, match="different inputs"):
        PostgresIfcQueryRepositoryMixin._require_same_ifc_query_preparation(
            stored,
            replay.model_copy(update={"request_digest": "e" * 64}),
        )


def test_repository_terminal_replay_requires_the_same_durable_projection() -> None:
    finished_at = NOW + timedelta(seconds=1)
    stored = _record(
        status=IfcQueryAttemptStatus.FAILED,
        diagnostics=(DIAGNOSTIC,),
        finished_at=finished_at,
    )
    same = IfcQueryAttemptTerminal(
        status=IfcQueryAttemptStatus.FAILED,
        diagnostics=(DIAGNOSTIC,),
        finished_at=finished_at + timedelta(seconds=1),
    )
    PostgresIfcQueryRepositoryMixin._require_same_ifc_query_terminal(stored, same)
    with pytest.raises(ConflictError, match="different terminal state"):
        PostgresIfcQueryRepositoryMixin._require_same_ifc_query_terminal(
            stored,
            same.model_copy(
                update={"diagnostics": (DIAGNOSTIC.model_copy(update={"retryable": True}),)}
            ),
        )
