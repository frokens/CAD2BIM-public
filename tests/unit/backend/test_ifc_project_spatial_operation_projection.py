from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.domain.data_runtime import (
    IFC_SPATIAL_STRUCTURE_APPLY_INTENT_SUMMARY,
    CatalogResourceKind,
    CriticalOperationErrorPhase,
    CriticalOperationKind,
    CriticalOperationRecord,
    CriticalOperationStatus,
    DataCatalogEntry,
    IfcSpatialStructureApplyOperationDetail,
    PublishedCriticalOperation,
)
from CAD2BIM_backend.domain.ifc_model import IfcModelConflictError
from CAD2BIM_backend.application.data_runtime_service import OperationInspection
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    DeleteIfcSpatialOperation,
    IfcSpatialAppliedItem,
    IfcSpatialBlockerSummary,
    IfcSpatialPatch,
    IfcSpatialStructureApplyResult,
    UpdateIfcSpatialOperation,
)
from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import (
    _operation_detail_json,
)
from CAD2BIM_backend.infrastructure.postgres.repository_ifc_model import (
    PostgresIfcModelRepositoryMixin,
)
from CAD2BIM_backend.internal_data_api import _operation_inspection_json
from CAD2BIM_mcp.direct.operation.application import _operation_get_result
from CAD2BIM_mcp.runner import data_tools as runner_data_tools
from CAD2BIM_mcp.runner.data_tools import register_runner_data_tools


_TARGET_GLOBAL_ID = "3Hj0000000000000000001"
_NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _command(global_id: str = _TARGET_GLOBAL_ID) -> ApplyIfcSpatialStructureCommand:
    return ApplyIfcSpatialStructureCommand(
        operations=(DeleteIfcSpatialOperation(target_global_id=global_id),)
    )


def _result(*, alias: str = "op_005", count: int = 1) -> IfcSpatialStructureApplyResult:
    item = IfcSpatialAppliedItem(
        action="delete_if_empty", global_id=_TARGET_GLOBAL_ID,
        target_global_id=_TARGET_GLOBAL_ID,
    )
    return IfcSpatialStructureApplyResult(
        operation_alias=alias, applied_count=count,
        applied=(item,) * count, spatial_summary=(),
    )


def _detail(
    *, result: IfcSpatialStructureApplyResult | None = None,
    failure_blockers: IfcSpatialBlockerSummary | None = None,
) -> IfcSpatialStructureApplyOperationDetail:
    return IfcSpatialStructureApplyOperationDetail(
        request=_command(),
        result=result,
        failure_blockers=failure_blockers,
    )


def _operation(
    *,
    status: CriticalOperationStatus,
    detail: IfcSpatialStructureApplyOperationDetail,
    **changes: object,
) -> CriticalOperationRecord:
    values: dict[str, object] = {
        "operation_id": "operation_ifc_spatial_apply",
        "owner_id": "owner_local",
        "tenant_id": "tenant_local",
        "task_id": "task_local",
        "run_id": "run_local",
        "operation_kind": CriticalOperationKind.IFC_MODEL_WRITE,
        "tool_name": "ifc_spatial_structure_apply",
        "intent_summary": IFC_SPATIAL_STRUCTURE_APPLY_INTENT_SUMMARY,
        "idempotency_key": "server-only-spatial-apply-key",
        "request_fingerprint": "d" * 64,
        "status": status,
        "detail": detail,
        "base_ifc_revision_id": "ifc_revision_3",
        "expected_ifc_write_epoch": 3,
        "started_at": _NOW,
    }
    values.update(changes)
    return CriticalOperationRecord.model_validate(values)


def _published(operation: CriticalOperationRecord, *, alias: str) -> PublishedCriticalOperation:
    return PublishedCriticalOperation(
        operation=operation,
        catalog_entry=DataCatalogEntry(
            catalog_entry_id=f"catalog_{operation.operation_id}",
            owner_id="owner_local",
            tenant_id="tenant_local",
            task_id="task_local",
            run_id="run_local",
            resource_kind=CatalogResourceKind.OPERATION,
            alias=alias,
            ordinal=int(alias.removeprefix("op_")),
            catalog_revision=int(alias.removeprefix("op_")),
            operation_id=operation.operation_id,
            created_at=_NOW,
        ),
    )


def test_spatial_operation_persists_full_request_without_nonterminal_result() -> None:
    prepared = _operation(
        status=CriticalOperationStatus.PREPARED,
        detail=_detail(),
    )

    payload = _operation_detail_json(prepared)
    assert payload == {
        "detail_kind": "ifc_spatial_structure_apply",
        "request": {
            "operations": [
                {
                    "action": "delete_if_empty",
                    "target_global_id": _TARGET_GLOBAL_ID,
                }
            ]
        },
    }
    assert IfcSpatialStructureApplyOperationDetail.model_validate(payload) == prepared.detail

    with pytest.raises(ValidationError, match="persisted operation count"):
        _detail(result=_result(count=2))


def test_spatial_operation_lifecycle_requires_minimal_truthful_success() -> None:
    succeeded = _operation(
        status=CriticalOperationStatus.SUCCEEDED,
        detail=_detail(result=_result()),
        result_ifc_revision_id="ifc_revision_4",
        finished_at=_NOW,
    )
    assert _operation_detail_json(succeeded) == {
        "detail_kind": "ifc_spatial_structure_apply",
        "request": {
            "operations": [
                {
                    "action": "delete_if_empty",
                    "target_global_id": _TARGET_GLOBAL_ID,
                }
            ]
        },
        "result": _result().model_dump(mode="json"),
    }

    failed = _operation(
        status=CriticalOperationStatus.FAILED,
        detail=_detail(failure_blockers=IfcSpatialBlockerSummary(
            child_spatial_count=1, sample_global_ids=(_TARGET_GLOBAL_ID,),
        )),
        error_code="IFC_SPATIAL_NOT_EMPTY",
        error_message="The selected spatial object is not empty.",
        error_phase=CriticalOperationErrorPhase.IFC_VALIDATION,
        finished_at=_NOW,
    )
    assert "result" not in _operation_detail_json(failed)
    assert _operation_detail_json(failed)["failure_blockers"]["child_spatial_count"] == 1

    with pytest.raises(ValidationError, match="requires its durable result"):
        _operation(
            status=CriticalOperationStatus.SUCCEEDED,
            detail=_detail(),
            result_ifc_revision_id="ifc_revision_4",
            finished_at=_NOW,
        )


def test_spatial_typed_replay_compares_request_not_terminal_result() -> None:
    stored = _detail(
        result=_result(),
    ).persisted_payload()
    replay = SimpleNamespace(
        operation_kind=CriticalOperationKind.IFC_MODEL_WRITE.value,
        tool_name="ifc_spatial_structure_apply",
        request_fingerprint="d" * 64,
        intent_summary=IFC_SPATIAL_STRUCTURE_APPLY_INTENT_SUMMARY,
        base_ifc_revision_id="ifc_revision_3",
        expected_ifc_write_epoch=3,
        operation_detail_json=stored,
    )
    expected = _detail().persisted_payload()

    PostgresIfcModelRepositoryMixin._assert_revision_replay(
        replay,
        tool_name="ifc_spatial_structure_apply",
        request_digest="d" * 64,
        intent_summary=IFC_SPATIAL_STRUCTURE_APPLY_INTENT_SUMMARY,
        operation_detail=expected,
    )

    changed = IfcSpatialStructureApplyOperationDetail(
        request=_command("3Hj0000000000000000002")
    ).persisted_payload()
    with pytest.raises(IfcModelConflictError, match="request detail changed"):
        PostgresIfcModelRepositoryMixin._assert_revision_replay(
            replay,
            tool_name="ifc_spatial_structure_apply",
            request_digest="d" * 64,
            intent_summary=IFC_SPATIAL_STRUCTURE_APPLY_INTENT_SUMMARY,
            operation_detail=changed,
        )


def test_revision_repository_accepts_only_approved_tool_and_stable_intent() -> None:
    PostgresIfcModelRepositoryMixin._validate_revision_operation_input(
        tool_name="ifc_spatial_structure_apply",
        idempotency_key="server-only-spatial-apply-key",
        request_digest="d" * 64,
        intent_summary=IFC_SPATIAL_STRUCTURE_APPLY_INTENT_SUMMARY,
    )

    with pytest.raises(ValueError, match="stable tool template"):
        PostgresIfcModelRepositoryMixin._validate_revision_operation_input(
            tool_name="ifc_spatial_structure_apply",
            idempotency_key="server-only-spatial-apply-key",
            request_digest="d" * 64,
            intent_summary="Apply some spatial changes.",
        )
    with pytest.raises(ValueError, match="tool_name is invalid"):
        PostgresIfcModelRepositoryMixin._validate_revision_operation_input(
            tool_name="ifc_generic_write",
            idempotency_key="server-only-generic-key",
            request_digest="d" * 64,
            intent_summary="Generic IFC write.",
        )


@pytest.mark.asyncio
async def test_spatial_operation_list_and_get_expose_only_safe_minimal_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    succeeded = _operation(
        status=CriticalOperationStatus.SUCCEEDED,
        detail=_detail(result=_result()),
        result_ifc_revision_id="ifc_revision_4",
        finished_at=_NOW,
    )
    inspection = OperationInspection(
        operation=_published(succeeded, alias="op_005"),
        result_package=None, input_packages={},
    )
    result = _operation_get_result(inspection)
    payload = result.model_dump(mode="json")
    assert payload["operation"]["primary_result"] == {
        "result": "spatial_structure_applied",
        "applied_count": 1,
    }
    assert payload["detail"]["request"]["operations"][0] == {
        "action": "delete_if_empty",
        "target_global_id": _TARGET_GLOBAL_ID,
    }
    assert payload["detail"]["result"] == "spatial_structure_applied"
    assert payload["detail"]["applied_count"] == 1
    serialized = result.model_dump_json()
    assert len(serialized.encode("utf-8")) <= 64 * 1024
    for server_only_name in (
        "operation_ifc_spatial_apply",
        "owner_local",
        "tenant_local",
        "task_local",
        "run_local",
        "ifc_revision_3",
        "ifc_revision_4",
        "server-only-spatial-apply-key",
    ):
        assert server_only_name not in serialized

    class Backend:
        async def call_data_tool(self, **kwargs):
            assert kwargs["tool_name"] == "operation_get"
            return {"inspection": _operation_inspection_json(inspection),
                    "authoring_result": None, "inspection_result": None,
                    "operation_ref": None, "same_run": True, "case_key": None, "family_version_ref": None}

    monkeypatch.setattr(runner_data_tools, "request_domain_credential", lambda: "token")
    server = FastMCP("Spatial Operation projection")
    register_runner_data_tools(server, Backend())
    runner_result = await server.call_tool(
        "operation_get", {"request": {"operation_alias": "op_005"}}
    )
    assert runner_result.structured_content == payload

    failed = _operation(
        status=CriticalOperationStatus.FAILED,
        detail=_detail(),
        error_code="IFC_SPATIAL_NOT_EMPTY",
        error_message="The selected spatial object is not empty.",
        error_phase=CriticalOperationErrorPhase.IFC_VALIDATION,
        finished_at=_NOW,
    )
    failed_result = _operation_get_result(
        SimpleNamespace(
            operation=_published(failed, alias="op_006"),
            result_package=None,
            input_packages={},
        )
    )
    assert failed_result.operation.tool_name == "ifc_spatial_structure_apply"
    assert failed_result.detail.result is None
    assert "failure_blockers" not in failed_result.detail.model_dump()
    assert failed_result.error.code == "IFC_SPATIAL_NOT_EMPTY"


@pytest.mark.asyncio
async def test_large_spatial_operation_get_reports_budget_without_changing_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = tuple("3Hj" + f"{index:019d}" for index in range(20))
    request = ApplyIfcSpatialStructureCommand(operations=tuple(
        UpdateIfcSpatialOperation(
            target_global_id=target,
            patch=IfcSpatialPatch(description="界" * 2000),
        )
        for target in targets
    ))
    applied = tuple(
        IfcSpatialAppliedItem(
            action="update", global_id=target, target_global_id=target,
        )
        for target in targets
    )
    detail = IfcSpatialStructureApplyOperationDetail(
        request=request,
        result=IfcSpatialStructureApplyResult(
            operation_alias="op_005", applied_count=len(applied),
            applied=applied, spatial_summary=(),
        ),
    )
    succeeded = _operation(
        status=CriticalOperationStatus.SUCCEEDED,
        detail=detail,
        result_ifc_revision_id="ifc_revision_4",
        finished_at=_NOW,
    )

    inspection = OperationInspection(
        operation=_published(succeeded, alias="op_005"),
        result_package=None, input_packages={},
    )
    with pytest.raises(ToolError, match="OPERATION_GET_RESPONSE_TOO_LARGE") as caught:
        _operation_get_result(inspection)

    assert "phase=output_validation" in str(caught.value)
    assert "retryable=false" in str(caught.value)
    assert "operation_alias=op_005" in str(caught.value)
    assert "original Operation remains succeeded" in str(caught.value)
    assert succeeded.status is CriticalOperationStatus.SUCCEEDED
    assert succeeded.detail.result == detail.result
    assert len(succeeded.detail.request.operations) == 20
    stored_detail = _operation_detail_json(succeeded)
    assert stored_detail["result"] == detail.result.model_dump(mode="json")
    assert len(stored_detail["request"]["operations"]) == 20

    class Backend:
        async def call_data_tool(self, **kwargs):
            assert kwargs["tool_name"] == "operation_get"
            return {"inspection": _operation_inspection_json(inspection),
                    "authoring_result": None, "inspection_result": None,
                    "operation_ref": None, "same_run": True, "case_key": None, "family_version_ref": None}

    monkeypatch.setattr(runner_data_tools, "request_domain_credential", lambda: "token")
    server = FastMCP("Oversize Operation projection")
    register_runner_data_tools(server, Backend())
    transport = _operation_inspection_json(inspection)
    try:
        TypeAdapter(OperationInspection).validate_python(transport)
    except ValidationError as exc:
        raise AssertionError([
            (error["loc"], error["type"])
            for error in exc.errors(include_input=False)[:5]
        ]) from None
    with pytest.raises(ToolError, match="OPERATION_GET_RESPONSE_TOO_LARGE") as runner_error:
        await server.call_tool("operation_get", {"request": {"operation_alias": "op_005"}})
    assert "phase=output_validation" in str(runner_error.value)
    assert "original Operation remains succeeded" in str(runner_error.value)
