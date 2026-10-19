from __future__ import annotations

import pytest
from datetime import datetime, timezone
from pydantic import ValidationError

from CAD2BIM_backend.domain.ifc_model import (
    InitializeIfcModelCommand,
    IfcRevisionInspection,
)
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef, CriticalOperationRecord,
    IfcTrustedBaselineLoadOperationDetail, IFC_TRUSTED_BASELINE_LOAD_INTENT_SUMMARY,
)


def test_trusted_baseline_detail_and_terminal_revision_are_exact() -> None:
    source = ArtifactVersionRef(artifact_id="source", version_id="version", sha256="a" * 64)
    detail = IfcTrustedBaselineLoadOperationDetail(source_asset=source)
    now = datetime.now(timezone.utc)
    values = dict(operation_id="op", owner_id="owner", tenant_id="tenant", task_id="task",
                  run_id="run", operation_kind="ifc_model_write", tool_name="ifc_trusted_baseline_load",
                  intent_summary=IFC_TRUSTED_BASELINE_LOAD_INTENT_SUMMARY,
                  idempotency_key="request", request_fingerprint="b" * 64, started_at=now,
                  base_ifc_revision_id="revision1", expected_ifc_write_epoch=1,
                  status="prepared", detail=detail)
    assert CriticalOperationRecord(**values).detail == detail
    assert "result" not in detail.persisted_payload()
    succeeded = dict(values, status="succeeded", finished_at=now, result_ifc_revision_id="revision2",
                     detail=detail.model_copy(update={"result": "baseline_loaded"}))
    assert CriticalOperationRecord(**succeeded).detail.result == "baseline_loaded"
    for invalid in (dict(succeeded, result_ifc_revision_id=None), dict(succeeded, detail=detail),
                    dict(values, detail=succeeded["detail"]), dict(values, base_ifc_revision_id=None)):
        with pytest.raises(ValidationError):
            CriticalOperationRecord(**invalid)
    with pytest.raises(ValidationError):
        IfcTrustedBaselineLoadOperationDetail(source_asset=source, request={})


def test_initialize_command_normalizes_project_name_and_forbids_extra_fields() -> None:
    command = InitializeIfcModelCommand.model_validate(
        {"project_name": "  Gas Network Project  "}
    )

    assert command.model_dump() == {"project_name": "Gas Network Project"}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        InitializeIfcModelCommand.model_validate(
            {"project_name": "Gas Network Project", "revision": 1}
        )


@pytest.mark.parametrize(
    "project_name",
    (
        "",
        "   ",
        "a" * 256,
        "project\x00name",
        "project\nname",
        "\tproject",
    ),
)
def test_initialize_command_rejects_invalid_project_name(project_name: str) -> None:
    with pytest.raises(ValidationError):
        InitializeIfcModelCommand.model_validate({"project_name": project_name})


def test_initialize_command_requires_project_name() -> None:
    with pytest.raises(ValidationError, match="Field required"):
        InitializeIfcModelCommand.model_validate({})


def test_safe_model_info_exposes_only_bounded_business_fields() -> None:
    inspection = IfcRevisionInspection(
        schema_identifier="IFC4X3_ADD2",
        project_global_id="1xvJu1wbjMywJMjRBpLw$1",
        project_name="Gas Network Project",
        size_bytes=320,
        sha256="a" * 64,
        site_count=0,
        element_count=0,
        unit_assignment_count=0,
        geometric_context_count=0,
        product_body_contexts=(),
        project_length_unit_m=None,
        geometry_basis_configured=False,
    )

    payload = inspection.to_safe_info().model_dump(mode="json")

    assert set(payload) == {
        "ifc_schema",
        "project",
        "size_bytes",
        "geometry_basis_configured",
        "geometry_basis",
        "site_count",
        "element_count",
    }
    assert payload["size_bytes"] == inspection.size_bytes
    assert set(payload["project"]) == {"global_id", "name"}
    assert payload["project"]["name"] == "Gas Network Project"
    assert not {
        "tenant_id",
        "principal_id",
        "task_id",
        "run_id",
        "model_id",
        "revision_id",
        "revision_no",
        "artifact_id",
        "artifact_version_id",
        "operation_id",
        "request_digest",
        "sha256",
        "storage_key",
        "write_epoch",
        "writer_generation",
    } & set(payload)
    inspection_only_fields = {
        "sha256",
        "unit_assignment_count",
        "geometric_context_count",
    }
    assert inspection_only_fields <= set(inspection.model_dump())
    assert inspection_only_fields.isdisjoint(payload)
