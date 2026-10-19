from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.geometry_query import (
    GeometryQueryArtifactRef,
    GeometryQueryAttemptPreparation,
    GeometryQueryAttemptRecord,
    GeometryQueryAttemptStatus,
    GeometryQueryAttemptTerminal,
    GeometryQueryDiagnostic,
    GeometryQueryTargetKind,
)
from CAD2BIM_backend.domain.geometry_inspection import (
    InspectionFragmentLocalCoordinates,
    InspectionFragmentSelection,
    InspectionResultCoordinates,
    InspectionWholeSelection,
)
from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometryFormat


NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)
SOURCE = GeometryQueryArtifactRef(
    artifact_id="artifact_source",
    version_id="artifact_version_source",
    sha256="a" * 64,
)
RESULT = GeometryQueryArtifactRef(
    artifact_id="artifact_result",
    version_id="artifact_version_result",
    sha256="b" * 64,
)
PREVIEW = GeometryQueryArtifactRef(
    artifact_id="artifact_preview",
    version_id="artifact_version_preview",
    sha256="d" * 64,
)
DIAGNOSTIC = GeometryQueryDiagnostic(code="WORKER_FAILED", message="query failed")


def _preparation(**changes: object) -> GeometryQueryAttemptPreparation:
    values: dict[str, object] = {
        "attempt_id": "geometry_query_attempt_one",
        "origin_run_id": "run_one",
        "resource_version_id": "geometry_version_one",
        "source_artifact": SOURCE,
        "request_identity": "request_one",
        "request_digest": "c" * 64,
        "created_at": NOW,
    }
    values.update(changes)
    return GeometryQueryAttemptPreparation.model_validate(values)


def test_preparation_requires_exact_source_digest_and_aware_time() -> None:
    assert _preparation().source_artifact == SOURCE
    with pytest.raises(ValidationError, match="sha256"):
        _preparation(source_artifact={**SOURCE.model_dump(), "sha256": "A" * 64})
    with pytest.raises(ValidationError, match="timezone-aware"):
        _preparation(created_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="cannot be the attempt itself"):
        _preparation(repair_parent_attempt_id="geometry_query_attempt_one")


def test_family_standard_query_uses_exact_family_instead_of_inspection_or_geometry_id():
    values = dict(target_kind=GeometryQueryTargetKind.FAMILY_STANDARD_MODEL,
                  resource_version_id=None, family_version_id="family-version-one")
    assert _preparation(**values).family_version_id == "family-version-one"
    with pytest.raises(ValidationError, match="exact Family version"):
        _preparation(**{**values, "resource_version_id": "geometry-version-one"})
    with pytest.raises(ValidationError, match="Family version disagree"):
        _preparation(family_version_id="family-version-one")


def test_preparation_requires_one_closed_exact_target_branch() -> None:
    inspection = _preparation(
        target_kind=GeometryQueryTargetKind.GEOMETRY_INSPECTION,
        resource_version_id=None,
        target_artifact=PREVIEW,
        selection=InspectionWholeSelection(),
        coordinate_space=InspectionResultCoordinates(),
    )
    assert inspection.target_artifact == PREVIEW
    assert inspection.target_geometry_format is None

    with pytest.raises(ValidationError, match="requires resource_version_id"):
        _preparation(resource_version_id=None)
    with pytest.raises(ValidationError, match="cannot carry preview"):
        _preparation(target_artifact=PREVIEW)
    with pytest.raises(ValidationError, match="requires an exact ArtifactVersion"):
        _preparation(
            target_kind=GeometryQueryTargetKind.GEOMETRY_INSPECTION,
            resource_version_id=None,
            selection=InspectionWholeSelection(),
            coordinate_space=InspectionResultCoordinates(),
        )
    with pytest.raises(ValidationError, match="persistent selection and coordinates"):
        _preparation(
            target_kind=GeometryQueryTargetKind.GEOMETRY_INSPECTION,
            resource_version_id=None,
            target_artifact=PREVIEW,
        )
    with pytest.raises(ValidationError, match="exact fragment"):
        _preparation(
            target_kind=GeometryQueryTargetKind.GEOMETRY_INSPECTION,
            resource_version_id=None, target_artifact=PREVIEW,
            selection=InspectionFragmentSelection(fragment_key="piece_a"),
            coordinate_space=InspectionFragmentLocalCoordinates(fragment_key="piece_b"),
        )


@pytest.mark.parametrize("target_kind", [
    GeometryQueryTargetKind.PRODUCT_REALIZATION_PREVIEW,
    GeometryQueryTargetKind.GEOMETRY_PREVIEW,
])
def test_retired_targets_are_decodable_history_but_cannot_be_prepared(target_kind) -> None:
    values = {
        **_preparation().model_dump(), "target_kind": target_kind,
        "resource_version_id": None, "target_artifact": PREVIEW,
        "target_geometry_format": CanonicalGeometryFormat.OCCT_BREP_V1,
    }
    with pytest.raises(ValidationError, match="retired preview targets"):
        GeometryQueryAttemptPreparation.model_validate(values)
    historical = GeometryQueryAttemptRecord.model_validate({
        **values, "tenant_id": "tenant_one", "task_id": "task_one",
        "status": GeometryQueryAttemptStatus.PREPARED,
    })
    assert historical.target_kind is target_kind


def test_terminal_contract_requires_exact_success_result_or_bounded_failure() -> None:
    succeeded = GeometryQueryAttemptTerminal(
        status=GeometryQueryAttemptStatus.SUCCEEDED,
        result_artifact=RESULT,
        finished_at=NOW,
    )
    assert succeeded.result_artifact == RESULT

    with pytest.raises(ValidationError, match="requires a result artifact"):
        GeometryQueryAttemptTerminal(
            status=GeometryQueryAttemptStatus.SUCCEEDED,
            finished_at=NOW,
        )
    with pytest.raises(ValidationError, match="requires a diagnostic"):
        GeometryQueryAttemptTerminal(
            status=GeometryQueryAttemptStatus.FAILED,
            finished_at=NOW,
        )
    with pytest.raises(ValidationError, match="cannot publish a result"):
        GeometryQueryAttemptTerminal(
            status=GeometryQueryAttemptStatus.FAILED,
            result_artifact=RESULT,
            diagnostics=(DIAGNOSTIC,),
            finished_at=NOW,
        )
    with pytest.raises(ValidationError, match="at most 32"):
        GeometryQueryAttemptTerminal(
            status=GeometryQueryAttemptStatus.FAILED,
            diagnostics=(DIAGNOSTIC,) * 33,
            finished_at=NOW,
        )


def test_attempt_record_enforces_prepared_and_terminal_shapes() -> None:
    base = {
        **_preparation().model_dump(),
        "tenant_id": "tenant_one",
        "task_id": "task_one",
        "status": GeometryQueryAttemptStatus.PREPARED,
    }
    prepared = GeometryQueryAttemptRecord.model_validate(base)
    assert prepared.finished_at is None

    with pytest.raises(ValidationError, match="cannot contain diagnostics"):
        GeometryQueryAttemptRecord.model_validate({**base, "diagnostics": (DIAGNOSTIC,)})
    with pytest.raises(ValidationError, match="requires a result"):
        GeometryQueryAttemptRecord.model_validate(
            {
                **base,
                "status": GeometryQueryAttemptStatus.SUCCEEDED,
                "finished_at": NOW + timedelta(seconds=1),
            }
        )
    with pytest.raises(ValidationError, match="cannot finish before"):
        GeometryQueryAttemptRecord.model_validate(
            {
                **base,
                "status": GeometryQueryAttemptStatus.SUCCEEDED,
                "result_artifact": RESULT,
                "finished_at": NOW - timedelta(seconds=1),
            }
        )
