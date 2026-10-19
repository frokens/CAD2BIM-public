from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import ForeignKeyConstraint

from CAD2BIM_backend.domain.control_plane import ConflictError, IdempotencyConflictError
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef, AuthoringExecutionState, CriticalOperationErrorPhase,
    CriticalOperationKind, CriticalOperationRecord, CriticalOperationStatus,
    GeometryInspectionOperationDetail,
)
from CAD2BIM_backend.infrastructure.postgres.models import Base
from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import PostgresDataRuntimeRepositoryMixin


NOW = datetime(2026, 9, 29, tzinfo=UTC)
REQUEST = ArtifactVersionRef(artifact_id="inspection-request", version_id="inspection-request-version", sha256="a" * 64)
RESULT = ArtifactVersionRef(artifact_id="inspection-result", version_id="inspection-result-version", sha256="b" * 64)


def _operation(source_kind="geometry_sample", **changes):
    values = {
        "operation_id": "inspection-operation", "owner_id": "inspection-owner",
        "tenant_id": "inspection-tenant", "task_id": "inspection-task", "run_id": "inspection-run",
        "operation_kind": CriticalOperationKind.GEOMETRY_INSPECTION,
        "tool_name": "ifc_geometry_preview" if source_kind == "geometry_sample" else "ifc_product_realization_preview",
        "intent_summary": "Retain the same actual result for investigation",
        "idempotency_key": "inspection-call", "request_fingerprint": "c" * 64,
        "status": CriticalOperationStatus.PREPARED, "started_at": NOW,
        "detail": GeometryInspectionOperationDetail(
            source_kind=source_kind, source_id="source-one", source_version_id="source-version-one",
            source_content_digest="d" * 64, source_version_ref="source-version-reference",
            request_artifact=REQUEST,
        ),
    }
    values.update(changes)
    return CriticalOperationRecord.model_validate(values)


@pytest.mark.parametrize("source_kind", ["geometry_sample", "family_realization"])
def test_inspection_admission_and_terminal_require_closed_same_source_and_exact_result(source_kind):
    prepared = _operation(source_kind)
    assert AuthoringExecutionState(operation=prepared, execution_generation=0).execution_owner is None
    succeeded = _operation(source_kind, status=CriticalOperationStatus.SUCCEEDED,
                           result_inspection_artifact=RESULT, finished_at=NOW)
    assert succeeded.result_inspection_artifact == RESULT
    with pytest.raises(ValidationError, match="requires an exact inspection Artifact"):
        _operation(source_kind, status=CriticalOperationStatus.SUCCEEDED, finished_at=NOW)
    with pytest.raises(ValidationError, match="terminal result"):
        _operation(source_kind, result_inspection_artifact=RESULT)
    with pytest.raises(ValidationError, match="exact Preview tool"):
        _operation(source_kind, tool_name="ifc_geometry_resource_write")
    with pytest.raises(ValidationError, match="cannot carry authoring"):
        _operation(source_kind, target_geometry_resource_id="source-one")
    with pytest.raises(ValidationError, match="Extra inputs"):
        GeometryInspectionOperationDetail.model_validate({**prepared.detail.model_dump(), "input_geometry": {}})


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["geometry_sample", "family_realization"])
async def test_inspection_repo_roundtrip_retains_actual_dependency_refs(source_kind):
    operation = _operation(source_kind, status=CriticalOperationStatus.SUCCEEDED,
                           result_inspection_artifact=RESULT, finished_at=NOW)
    model = PostgresDataRuntimeRepositoryMixin._operation_model(operation)
    assert model.inspection_request_artifact_id == REQUEST.artifact_id
    assert model.inspection_result_artifact_version_id == RESULT.version_id
    assert (model.inspection_source_geometry_version_id, model.inspection_source_family_version_id) == (
        ("source-version-one", None) if source_kind == "geometry_sample" else (None, "source-version-one")
    )
    assert await PostgresDataRuntimeRepositoryMixin._operation_record(None, model) == operation
    model.inspection_request_artifact_sha256 = "e" * 64
    with pytest.raises(ConflictError, match="dependency changed"):
        await PostgresDataRuntimeRepositoryMixin._operation_record(None, model)


def test_inspection_replay_compares_original_request_and_shared_claim_fences_terminal():
    first = _operation()
    repeated = _operation(started_at=NOW + timedelta(seconds=1))
    owner = PostgresDataRuntimeRepositoryMixin
    owner._require_same_inspection_request(first, repeated, admission=True)
    with pytest.raises(IdempotencyConflictError, match="different inputs"):
        owner._require_same_inspection_request(first, _operation(request_fingerprint="e" * 64), admission=True)
    model = owner._operation_model(first)
    model.authoring_execution_generation = 0
    claim = owner._set_authoring_claim(model)
    owner.require_authoring_claim(model, claim)
    model.authoring_execution_generation += 1
    with pytest.raises(ConflictError, match="stale"):
        owner.require_authoring_claim(model, claim)


def _render(direction):
    config = Config(Path(__file__).resolve().parents[3] / "alembic.ini")
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:secret@example.invalid/cad2bim")
    output = StringIO()
    config.output_buffer = output
    if direction == "upgrade":
        command.upgrade(config, "20260929_0098:20260929_0099", sql=True)
    else:
        command.downgrade(config, "20260929_0099:20260929_0098", sql=True)
    return " ".join(output.getvalue().split())


def test_inspection_migration_keeps_existing_owner_and_exact_cleanup_dependencies():
    table = Base.metadata.tables["critical_operations"]
    foreign_keys = {
        (tuple(item.column_keys), tuple(element.target_fullname for element in item.elements), item.ondelete)
        for item in table.constraints if isinstance(item, ForeignKeyConstraint)
    }
    for role in ("request", "result"):
        assert (("owner_id", "task_id", f"inspection_{role}_artifact_id", f"inspection_{role}_artifact_version_id"),
                ("artifact_versions.owner_id", "artifact_versions.task_id", "artifact_versions.artifact_id", "artifact_versions.version_id"),
                "RESTRICT") in foreign_keys
    sql = _render("upgrade")
    assert "CREATE TABLE" not in sql
    assert "CREATE POLICY geometry_inspection_task_isolation" in sql
    assert "exact result Artifact is invalid" in sql
    assert "terminal publication requires its original execution claim" in sql
    assert "'gi'" in sql
    assert "CREATE UNIQUE INDEX" not in sql
    sql = _render("downgrade")
    assert "Cannot remove retained geometry inspection Operations or references" in sql
    assert sql.index("Cannot remove retained") < sql.index("DROP COLUMN inspection_request_artifact_id")
    assert "DELETE FROM" not in sql.upper()
