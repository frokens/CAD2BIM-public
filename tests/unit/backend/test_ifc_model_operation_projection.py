from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef,
    IFC_GEOMETRY_BASIS_CONFIGURE_INTENT_SUMMARY,
    IFC_MODEL_INITIALIZE_INTENT_SUMMARY,
    IFC_PROJECT_UPDATE_INTENT_SUMMARY,
    CatalogResourceKind,
    CriticalOperationErrorPhase,
    CriticalOperationKind,
    CriticalOperationRecord,
    CriticalOperationStatus,
    DataCatalogEntry,
    IfcGeometryBasisConfigureOperationDetail,
    IfcModelInitializeOperationDetail,
    IfcProjectUpdateOperationDetail,
    PublishedCriticalOperation,
)
from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    IfcLengthUnit,
    IfcProjectUnitChange,
    UpdateSubcontextOperation,
)
from CAD2BIM_backend.domain.ifc_project import IfcProjectSummary
from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import (
    _operation_detail_json,
)
from CAD2BIM_backend.domain.ifc_project import IfcProjectPatch, UpdateIfcProjectCommand
from CAD2BIM_backend.domain.ifc_model import IfcModelConflictError
from CAD2BIM_backend.infrastructure.postgres.repository_ifc_model import (
    PostgresIfcModelRepositoryMixin,
)
from CAD2BIM_mcp.direct.operation.application import _operation_get_result


_PROJECT_GLOBAL_ID = "3Hj0000000000000000000"
_NOW = datetime(2026, 8, 30, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_kind", ["apply", "edit"])
async def test_product_diagnostics_prioritize_late_failure_and_preserve_original_measurements_and_state(tool_kind):
    from dataclasses import replace
    from unittest.mock import AsyncMock
    from CAD2BIM_backend.application.data_runtime_service import DataRuntimeService, DataRuntimeApplicationError
    from CAD2BIM_backend.application.data_query_service import DataQueryApplicationError
    from CAD2BIM_backend.domain.data_runtime import IfcProductsEditOperationDetail, IFC_PRODUCTS_EDIT_INTENT_SUMMARY
    from CAD2BIM_backend.domain.product_edit import EditIfcProductsCommand, ProductsEditSummary, ProductsEditActionCounts

    original = _apply_inspection("rejected")
    operation = original.operation.operation
    detail = IfcProductsEditOperationDetail(request=EditIfcProductsCommand(package_id="package-input-private",
        payload_name="products", base_revision_id=operation.base_ifc_revision_id),
        input_binding=operation.detail.input_binding,
        result=ProductsEditSummary(input_count=1, committed_count=0,
            committed_by_action=ProductsEditActionCounts(replace=0, placement=0, delete=0), created_product_count=0, deleted_product_count=0))
    values = operation.model_dump()
    values.update(tool_name="ifc_products_edit", intent_summary=IFC_PRODUCTS_EDIT_INTENT_SUMMARY, detail=detail)
    operation = CriticalOperationRecord.model_validate(values) if tool_kind == "edit" else operation
    published = original.operation.model_copy(update={"operation": operation})
    stored_checks = [dict(check_index=index, status="WARN", message=f"warning {index}") for index in range(110)]
    stored_checks.append(dict(check_index=110, input_row_index=7, product_key="original-row-key",
        status="FAIL", message="late target dependency failure", metric="axis_distance", residual=.04,
        tolerance=.001, unit="m", target_global_id="A" * 22 if tool_kind == "edit" else None))
    async def query(scope, request):
        status = request.query.filters[0].value
        matched = [item for item in stored_checks if item["status"] == status]
        return SimpleNamespace(rows=tuple(SimpleNamespace(values={**item, "phase": "target_preflight"})
            for item in matched[:request.query.page_size]),
            page=SimpleNamespace(complete=len(matched) <= request.query.page_size))
    queries = SimpleNamespace(query_rows=AsyncMock(side_effect=query))
    repository = SimpleNamespace(get_package=AsyncMock(side_effect=lambda scope, package_id:
        original.result_package if package_id == "package-report-private" else original.input_packages["products"]))
    runtime = DataRuntimeService(repository, None, queries=queries)
    scope = SimpleNamespace(owner_id="owner_local", identity=SimpleNamespace(tenant_id="tenant_local", task_id="task_local"))
    inspection = await runtime._inspect_operation(scope, published)
    assert len(inspection.diagnostics.items) == 101  # one error page plus the original bounded warning page
    assert inspection.diagnostics.items[0].severity == "error"
    assert inspection.diagnostics.items[0].message == "late target dependency failure"
    fact = inspection.diagnostics.items[0]
    assert (fact.check_index, fact.input_row_index, fact.product_key) == (110, 7, "original-row-key")
    assert (fact.metric, fact.residual, fact.tolerance, fact.unit) == ("axis_distance", .04, .001, "m")
    assert fact.target_global_id == ("A" * 22 if tool_kind == "edit" else None)
    assert inspection.diagnostics.complete is False
    assert [call.args[1].query.filters[0].value for call in queries.query_rows.await_args_list] == ["FAIL", "WARN"]
    query_failure = DataQueryApplicationError("PAYLOAD_INTEGRITY_ERROR", "Original checks asset is unavailable: " + "读取失败" * 600)
    query_failure.diagnostic = {"payload_name": "checks", "reason": "Exact immutable member could not be read"}
    queries.query_rows.side_effect = query_failure
    with pytest.raises(DataRuntimeApplicationError) as captured:
        await runtime._inspect_operation(scope, published)
    assert captured.value.code == query_failure.code
    assert captured.value.phase == "read"
    assert str(captured.value).startswith("Original checks asset is unavailable")
    assert len(str(captured.value).encode("utf-8")) <= 2000
    assert captured.value.diagnostic == query_failure.diagnostic
    assert captured.value.__cause__ is query_failure
    assert captured.value.operation_id == operation.operation_id
    assert captured.value.operation_status == operation.status.value
    assert captured.value.operation_alias == published.catalog_entry.alias


def _apply_inspection(outcome="committed", *, with_report=True):
    from CAD2BIM_backend.application.data_runtime_service import OperationInspection
    from CAD2BIM_backend.domain.data_runtime import IfcProductsApplyOperationDetail, IFC_PRODUCTS_APPLY_INTENT_SUMMARY
    from CAD2BIM_backend.domain.product_apply import ApplyIfcProductsCommand, ProductApplyInputBinding, ProductApplySummary
    summary = ProductApplySummary(outcome=outcome, input_count=1, solve_executed_count=1,
        solved_count=1, interface_constraints_evaluated_product_count=1, build_executed_count=1 if outcome == "committed" else 0,
        built_count=1 if outcome == "committed" else 0, committed_count=1 if outcome == "committed" else 0,
        not_executed_count=0) if with_report else None
    detail = IfcProductsApplyOperationDetail(request=ApplyIfcProductsCommand(family_version_id="family-private",
        package_id="package-input-private", payload_name="products"), input_binding=ProductApplyInputBinding(
            package_id="package-input-private", member_id="member-private", payload_name="products", logical_key_fields=("product_key",)),
        result=summary)
    operation = _operation(status=CriticalOperationStatus.SUCCEEDED if outcome == "committed" else CriticalOperationStatus.FAILED,
        detail=detail, tool_name="ifc_products_apply", intent_summary=IFC_PRODUCTS_APPLY_INTENT_SUMMARY,
        base_ifc_revision_id="ifc_revision_private_base", expected_ifc_write_epoch=1, finished_at=_NOW,
        result_ifc_revision_id="ifc_revision_private_commit" if outcome == "committed" else None,
        result_package_id="package-report-private" if with_report else None,
        error_code=None if outcome == "committed" else "PRODUCT_APPLY_REJECTED",
        error_phase=None if outcome == "committed" else CriticalOperationErrorPhase.IFC_VALIDATION,
        error_message=None if outcome == "committed" else "Some products did not pass")
    from CAD2BIM_backend.domain.data_runtime import DataPackageRecord, DataPackageMemberRecord, PublishedDataPackage, PackageMemberKind
    def package(package_id, alias, members=()):
        return PublishedDataPackage(package=DataPackageRecord(package_id=package_id, owner_id="owner_local",
            tenant_id="tenant_local", task_id="task_local", created_by_run_id="run_local", name="Apply report",
            package_kind="derived", summary="Exact projection fixture", idempotency_key=package_id,
            request_fingerprint="a" * 64, created_at=_NOW), members=members, catalog_entry=DataCatalogEntry(
                catalog_entry_id="entry-" + alias, owner_id="owner_local", tenant_id="tenant_local", task_id="task_local",
                run_id="run_local", resource_kind=CatalogResourceKind.PACKAGE, alias=alias, ordinal=int(alias[-3:]),
                catalog_revision=2, package_id=package_id, created_at=_NOW))
    member = DataPackageMemberRecord(member_id="member-private", owner_id="owner_local", tenant_id="tenant_local",
        task_id="task_local", package_id="package-input-private", member_name="payloads/products/part-00000.parquet",
        member_kind=PackageMemberKind.PAYLOAD, artifact=ArtifactVersionRef(artifact_id="artifact-input-private",
            version_id="version-input-private", sha256="b" * 64), media_type="application/vnd.apache.parquet",
        logical_payload_name="products", logical_key_fields=("product_key",), row_count=1, created_at=_NOW)
    input_package = package("package-input-private", "data_001", (member,))
    report_members = tuple(member.model_copy(update={"member_id": "report-" + name,
        "package_id": "package-report-private", "member_name": f"payloads/{name}/part-00000.parquet",
        "logical_payload_name": name, "logical_key_fields": keys, "row_count": count})
        for name, count, keys in (("products", 1, ("input_row_index",)),
            ("components", 2, ("product_key", "component_key")),
            ("interfaces", 0, ("product_key", "interface_key")), ("checks", 5, ("check_index",))))
    report = package("package-report-private", "data_002", report_members) if with_report else None
    from CAD2BIM_backend.domain.ifc_model import ExactIfcRevision
    exact = ExactIfcRevision(scope={"owner_id": "owner_local", "tenant_id": "tenant_local",
        "task_id": "task_local", "run_id": "run_local"}, model_id="ifc-model-private",
        model_state="active", revision_id="ifc_revision_private_commit", revision_no=4, write_epoch=4,
        project_global_id="A" * 22, artifact={"artifact_id": "ifc-artifact-private",
            "version_id": "ifc-version-private", "size_bytes": 987654, "sha256": "f" * 64}) if outcome == "committed" else None
    return OperationInspection(operation=_published_geometry_authoring(operation), result_package=report,
        input_packages={"products": input_package}, committed_ifc_revision=exact)


@pytest.mark.parametrize("outcome,with_report", [("committed", True), ("rejected", True), ("technical_failure", False)])
def test_apply_operation_projects_exact_input_report_and_commit_provenance(outcome, with_report):
    inspection = _apply_inspection(outcome, with_report=with_report)
    result = _operation_get_result(inspection, family_version_ref="cur.authorized-family", operation_ref="cur.original-operation")
    assert result.detail.family_version_ref == "cur.authorized-family"
    assert result.detail.input_binding.model_dump() == {"package_alias": "data_001", "payload_name": "products",
        "logical_key_fields": ("product_key",)}
    assert result.operation.operation_ref == "cur.original-operation"
    if with_report:
        assert result.operation.primary_result.report_package.package_alias == "data_002"
        assert [table.model_dump() for table in result.operation.primary_result.report_tables] == [
            {"payload_name": member.logical_payload_name, "format": "parquet", "row_count": member.row_count,
             "logical_key_fields": member.logical_key_fields} for member in inspection.result_package.members]
        assert result.operation.primary_result.summary == inspection.operation.operation.detail.result
        if outcome == "committed":
            assert result.operation.primary_result.committed_revision.model_dump() == {
                "revision_no": 4, "size_bytes": 987654}
        else:
            assert "committed_revision" not in result.model_dump_json()
    else:
        assert result.operation.primary_result is None
    for private in ("family-private", "package-input-private", "member-private", "ifc_revision_private_commit"):
        assert private not in result.model_dump_json()
    with pytest.raises(ValueError, match="could not be authorized"):
        _operation_get_result(inspection, operation_ref="cur.original-operation")


@pytest.mark.parametrize("corruption", ["missing", "different_revision", "different_run"])
def test_apply_metadata_projection_never_substitutes_current_or_another_operation(corruption):
    from dataclasses import replace
    inspection = _apply_inspection()
    exact = inspection.committed_ifc_revision
    if corruption == "missing":
        exact = None
    elif corruption == "different_revision":
        exact = exact.model_copy(update={"revision_id": "newer-current-private", "revision_no": 9})
    else:
        exact = exact.model_copy(update={"scope": exact.scope.model_copy(update={"run_id": "other-run"})})
    with pytest.raises(ValueError, match="original committed IFC metadata"):
        _operation_get_result(replace(inspection, committed_ifc_revision=exact),
            family_version_ref="cur.family", operation_ref="cur.original")


@pytest.mark.asyncio
async def test_apply_metadata_read_uses_original_operation_exact_revision_without_current_or_inspection():
    from unittest.mock import AsyncMock, Mock
    from CAD2BIM_backend.application.data_runtime_service import DataRuntimeService, DataRuntimeApplicationError
    inspection = _apply_inspection()
    exact = inspection.committed_ifc_revision
    repository = SimpleNamespace(get_package=AsyncMock(side_effect=lambda scope, package_id:
        inspection.result_package if package_id == "package-report-private" else inspection.input_packages["products"]))
    revisions = SimpleNamespace(get_exact=AsyncMock(return_value=exact), get_current=AsyncMock())
    artifacts = Mock()
    runtime = DataRuntimeService(repository, artifacts, ifc_repository=revisions)
    scope = SimpleNamespace(owner_id="owner_local", run_id="new-call-run",
        identity=SimpleNamespace(tenant_id="tenant_local", task_id="task_local"))
    readback = await runtime._inspect_operation(scope, inspection.operation)
    assert readback.committed_ifc_revision is exact
    revisions.get_exact.assert_awaited_once_with(exact.scope, revision_id="ifc_revision_private_commit")
    revisions.get_current.assert_not_awaited()
    assert artifacts.mock_calls == []
    revisions.get_exact.return_value = None
    with pytest.raises(DataRuntimeApplicationError, match="unavailable or mismatched"):
        await runtime._inspect_operation(scope, inspection.operation)


@pytest.mark.parametrize("status", [CriticalOperationStatus.PREPARED, CriticalOperationStatus.INDETERMINATE])
def test_apply_unsettled_projection_preserves_original_status_and_input_without_catalog(status):
    from dataclasses import replace
    from CAD2BIM_backend.domain.data_runtime import IfcProductsRecoveryOperation
    from CAD2BIM_mcp.direct.operation.application import project_ifc_apply_operation

    inspection = _apply_inspection("technical_failure", with_report=False)
    unsettled = status is CriticalOperationStatus.INDETERMINATE
    values = inspection.operation.operation.model_dump()
    values.update(status=status, finished_at=_NOW if unsettled else None,
        error_code="IFC_SETTLEMENT_UNKNOWN" if unsettled else None,
        error_message="Settlement is not confirmed" if unsettled else None,
        error_phase=CriticalOperationErrorPhase.RECONCILE if unsettled else None)
    record = CriticalOperationRecord.model_validate(values)
    inspection = replace(inspection, operation=IfcProductsRecoveryOperation(
        operation=record, created_at=_NOW, catalog_entry=None))
    result = project_ifc_apply_operation(inspection, operation_ref="cur.original-operation",
        family_version_ref="cur.exact-family")
    assert result.operation.outcome == status.value
    assert result.operation.finished_at == (_NOW if unsettled else None)
    assert result.operation.operation_alias is None
    assert result.detail.input_binding.package_alias == "data_001"
    assert result.detail.family_version_ref == "cur.exact-family"
    payload = result.model_dump_json()
    for absent in ("sequence", "primary_result", "retryable", "package-input-private", "family-private", "server-only"):
        assert absent not in payload


@pytest.mark.asyncio
async def test_apply_operation_input_authorization_and_exact_member_fail_closed():
    from unittest.mock import AsyncMock
    from CAD2BIM_backend.application.data_runtime_service import DataRuntimeService, DataRuntimeApplicationError
    inspection = _apply_inspection("rejected")
    source = inspection.input_packages["products"]
    repo = SimpleNamespace(get_package=AsyncMock(side_effect=lambda scope, package_id:
        inspection.result_package if package_id == "package-report-private" else source))
    service = DataRuntimeService(repo, None)
    scope = object()
    readback = await service._inspect_operation(scope, inspection.operation)
    assert readback.input_packages["products"] is source
    assert all(call.args[0] is scope for call in repo.get_package.await_args_list)
    source = source.model_copy(update={"members": (source.members[0].model_copy(update={"logical_key_fields": ("different_key",)}),)})
    with pytest.raises(DataRuntimeApplicationError, match="exact member"):
        await service._inspect_operation(scope, inspection.operation)
    repo.get_package.side_effect = lambda scope, package_id: inspection.result_package if package_id == "package-report-private" else None
    with pytest.raises(DataRuntimeApplicationError, match="not visible"):
        await service._inspect_operation(scope, inspection.operation)


@pytest.mark.asyncio
async def test_apply_operation_entry_authorizes_family_in_current_scope_before_projection():
    from unittest.mock import AsyncMock
    from CAD2BIM_mcp.direct.operation.application import OperationApplicationMixin
    from CAD2BIM_mcp.direct.errors import DirectApplicationError
    from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogNotFoundError
    inspection = _apply_inspection("rejected")
    scope = SimpleNamespace(identity=object(), run_id="run_local")
    catalog = SimpleNamespace(version_ref_for_visible_id=AsyncMock(return_value="cur.authorized-family"))
    class Entry(OperationApplicationMixin):
        _application = SimpleNamespace(get_operation=AsyncMock(return_value=inspection))
        reference_service = SimpleNamespace(operation_reference=lambda *args, **kwargs: "cur.original-operation")
        async def _run_runtime_read(self, alias, tool, read):
            return await read(scope)
        def _require_product_family_catalog_service(self):
            return catalog
    result = await Entry().get_operation("direct_001", "op_006")
    assert result.detail.family_version_ref == "cur.authorized-family"
    catalog.version_ref_for_visible_id.assert_awaited_once_with(scope.identity, "family-private", verify_assets=False)
    catalog.version_ref_for_visible_id.side_effect = ProductFamilyCatalogNotFoundError("exact Family unavailable")
    with pytest.raises(DirectApplicationError) as error:
        await Entry().get_operation("direct_001", "op_006")
    assert error.value.code == "PRODUCT_FAMILY_NOT_FOUND"


@pytest.mark.asyncio
async def test_retired_apply_detail_cannot_restore_current_online_projection():
    from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import PostgresDataRuntimeRepositoryMixin
    model = PostgresDataRuntimeRepositoryMixin._operation_model(_apply_inspection("rejected").operation.operation)
    retained = {"detail_kind": "ifc_products_apply", "result": None}
    model.operation_detail_json = retained
    with pytest.raises(ValidationError) as error:
        await PostgresDataRuntimeRepositoryMixin._operation_record(None, model)
    assert {item["loc"][0] for item in error.value.errors()} == {"request", "input_binding"}
    assert model.operation_detail_json == retained


def test_authoring_draft_history_recovers_original_edit_token_without_internal_identity():
    from CAD2BIM_backend.domain.data_runtime import AuthoringDraftWriteOperationDetail

    operation = CriticalOperationRecord(operation_id="operation_geometry_authoring", owner_id="owner_local",
        tenant_id="tenant_local", task_id="task_local", run_id="run_local",
        operation_kind=CriticalOperationKind.GEOMETRY_RESOURCE_WRITE, tool_name="ifc_geometry_resource_write",
        intent_summary="Save draft", idempotency_key="save-draft", request_fingerprint="a" * 64,
        status=CriticalOperationStatus.SUCCEEDED, started_at=_NOW, finished_at=_NOW,
        code_artifact=ArtifactVersionRef(artifact_id="artifact_private", version_id="snapshot_private", sha256="b" * 64),
        target_geometry_resource_id="geometry_private",
        detail=AuthoringDraftWriteOperationDetail(root_kind="geometry", root_id="geometry_private",
            expected_generation=2, result_generation=3))
    inspection = SimpleNamespace(operation=_published_geometry_authoring(operation), result_package=None, input_packages={})
    result = _operation_get_result(inspection,
        authoring_result={"root_kind": "geometry", "draft_ref": "gd_opaque", "edit_token": 3})
    assert result.detail.edit_token == 3
    assert result.operation.primary_result.draft_ref == "gd_opaque"
    text = result.model_dump_json()
    assert all(value not in text for value in ("geometry_private", "artifact_private", "snapshot_private", "run_local"))
    with pytest.raises(ValueError, match="original saved edit token"):
        _operation_get_result(inspection,
            authoring_result={"root_kind": "geometry", "draft_ref": "gd_opaque", "edit_token": 4})


def _detail(*, result: str | None = None) -> IfcModelInitializeOperationDetail:
    return IfcModelInitializeOperationDetail(
        project_name="  Gas Network Project  ",
        project_global_id=_PROJECT_GLOBAL_ID,
        result=result,
    )


def _operation(
    *,
    status: CriticalOperationStatus,
    detail: IfcModelInitializeOperationDetail,
    **changes: object,
) -> CriticalOperationRecord:
    values: dict[str, object] = {
        "operation_id": "operation_ifc_initialize",
        "owner_id": "owner_local",
        "tenant_id": "tenant_local",
        "task_id": "task_local",
        "run_id": "run_local",
        "operation_kind": CriticalOperationKind.IFC_MODEL_WRITE,
        "tool_name": "ifc_model_initialize",
        "intent_summary": IFC_MODEL_INITIALIZE_INTENT_SUMMARY,
        "idempotency_key": "server-only-initialize-key",
        "request_fingerprint": "a" * 64,
        "status": status,
        "detail": detail,
        "started_at": _NOW,
    }
    values.update(changes)
    return CriticalOperationRecord.model_validate(values)


def _published_geometry_authoring(
    operation: CriticalOperationRecord,
) -> PublishedCriticalOperation:
    return PublishedCriticalOperation(
        operation=operation,
        catalog_entry=DataCatalogEntry(
            catalog_entry_id="catalog_operation_geometry_authoring",
            owner_id="owner_local",
            tenant_id="tenant_local",
            task_id="task_local",
            run_id="run_local",
            resource_kind=CatalogResourceKind.OPERATION,
            alias="op_006",
            ordinal=6,
            catalog_revision=6,
            operation_id=operation.operation_id,
            created_at=_NOW,
        ),
    )


def _configure_request() -> ConfigureIfcGeometryBasisCommand:
    return ConfigureIfcGeometryBasisCommand(
        project_unit_change=IfcProjectUnitChange(
            length_unit=IfcLengthUnit(name="METRE", prefix="MILLI")
        )
    )


def _configure_detail(*, result: str | None = None) -> IfcGeometryBasisConfigureOperationDetail:
    return IfcGeometryBasisConfigureOperationDetail(
        request=_configure_request(),
        result=result,
    )


def _configure_operation(
    *,
    status: CriticalOperationStatus,
    detail: IfcGeometryBasisConfigureOperationDetail,
    **changes: object,
) -> CriticalOperationRecord:
    values: dict[str, object] = {
        "operation_id": "operation_ifc_geometry_basis",
        "owner_id": "owner_local",
        "tenant_id": "tenant_local",
        "task_id": "task_local",
        "run_id": "run_local",
        "operation_kind": CriticalOperationKind.IFC_MODEL_WRITE,
        "tool_name": "ifc_model_configure_geometry_basis",
        "intent_summary": IFC_GEOMETRY_BASIS_CONFIGURE_INTENT_SUMMARY,
        "idempotency_key": "server-only-geometry-basis-key",
        "request_fingerprint": "b" * 64,
        "status": status,
        "detail": detail,
        "base_ifc_revision_id": "ifc_revision_1",
        "expected_ifc_write_epoch": 1,
        "started_at": _NOW,
    }
    values.update(changes)
    return CriticalOperationRecord.model_validate(values)


def test_initialize_operation_detail_normalizes_and_projects_only_business_facts() -> None:
    detail = _detail()

    assert detail.project_name == "Gas Network Project"
    assert detail.model_dump(mode="json") == {
        "detail_kind": "ifc_model_initialize",
        "ifc_schema": "IFC4X3_ADD2",
        "project_name": "Gas Network Project",
        "project_global_id": _PROJECT_GLOBAL_ID,
        "result": None,
    }

    for invalid_name in ("Gas\nProject", "Gas Project\n"):
        with pytest.raises(ValidationError, match="control characters"):
            IfcModelInitializeOperationDetail(
                project_name=invalid_name,
                project_global_id=_PROJECT_GLOBAL_ID,
            )
    with pytest.raises(ValidationError, match="must not be empty"):
        IfcModelInitializeOperationDetail(
            project_name="   ",
            project_global_id=_PROJECT_GLOBAL_ID,
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IfcModelInitializeOperationDetail.model_validate(
            {
                "project_name": "Gas Project",
                "project_global_id": _PROJECT_GLOBAL_ID,
                "direct_session_alias": "direct_001",
            }
        )


def test_initialize_operation_lifecycle_keeps_result_truthful() -> None:
    prepared = _operation(status=CriticalOperationStatus.PREPARED, detail=_detail())
    assert prepared.detail is not None
    assert prepared.detail.result is None

    succeeded = _operation(
        status=CriticalOperationStatus.SUCCEEDED,
        detail=_detail(result="model_initialized"),
        result_ifc_revision_id="ifc_revision_1",
        finished_at=_NOW,
    )
    assert succeeded.detail is not None
    assert succeeded.detail.result == "model_initialized"

    failed = _operation(
        status=CriticalOperationStatus.FAILED,
        detail=_detail(),
        error_code="IFC_BYTES_INVALID",
        error_message="Generated IFC did not satisfy initialization invariants.",
        error_phase=CriticalOperationErrorPhase.IFC_VALIDATION,
        finished_at=_NOW,
    )
    assert failed.detail is not None
    assert failed.detail.result is None

    with pytest.raises(ValidationError, match="requires its durable result"):
        _operation(
            status=CriticalOperationStatus.SUCCEEDED,
            detail=_detail(),
            result_ifc_revision_id="ifc_revision_1",
            finished_at=_NOW,
        )
    with pytest.raises(ValidationError, match="cannot report a result"):
        _operation(
            status=CriticalOperationStatus.FAILED,
            detail=_detail(result="model_initialized"),
            error_code="IFC_BYTES_INVALID",
            error_message="Generated IFC did not satisfy initialization invariants.",
            error_phase=CriticalOperationErrorPhase.IFC_VALIDATION,
            finished_at=_NOW,
        )


def test_postgres_operation_json_round_trip_has_no_server_internal_bindings() -> None:
    operation = _operation(
        status=CriticalOperationStatus.SUCCEEDED,
        detail=_detail(result="model_initialized"),
        result_ifc_revision_id="ifc_revision_1",
        finished_at=_NOW,
    )

    payload = _operation_detail_json(operation)
    restored = IfcModelInitializeOperationDetail.model_validate(payload)

    assert restored == operation.detail
    assert set(payload) == {
        "detail_kind",
        "ifc_schema",
        "project_name",
        "project_global_id",
        "result",
    }
    assert not {
        "direct_session_alias",
        "request_fingerprint",
        "idempotency_key",
        "run_id",
        "ifc_model_id",
        "revision_id",
        "artifact_id",
        "storage_key",
        "writer_generation",
    } & set(payload)


def test_non_terminal_initialize_operation_cannot_enter_the_catalog() -> None:
    catalog_entry = DataCatalogEntry(
        catalog_entry_id="catalog_operation_initialize",
        owner_id="owner_local",
        tenant_id="tenant_local",
        task_id="task_local",
        run_id="run_local",
        resource_kind=CatalogResourceKind.OPERATION,
        alias="op_001",
        ordinal=1,
        catalog_revision=1,
        operation_id="operation_ifc_initialize",
        created_at=_NOW,
    )
    prepared = _operation(status=CriticalOperationStatus.PREPARED, detail=_detail())
    indeterminate = _operation(
        status=CriticalOperationStatus.INDETERMINATE,
        detail=_detail(),
        error_code="IFC_COMMIT_UNCERTAIN",
        error_message="Initialization commit outcome requires reconciliation.",
        error_phase=CriticalOperationErrorPhase.RECONCILE,
        finished_at=_NOW,
    )

    for operation in (prepared, indeterminate):
        with pytest.raises(ValidationError, match="only terminal business Operations"):
            PublishedCriticalOperation(
                operation=operation,
                catalog_entry=catalog_entry,
            )


def test_geometry_basis_operation_detail_keeps_exact_business_request_only() -> None:
    detail = _configure_detail()

    assert detail.model_dump(mode="json") == {
        "detail_kind": "ifc_model_configure_geometry_basis",
        "request": {
            "project_unit_change": {
                "length_unit": {"name": "METRE", "prefix": "MILLI"},
                "plane_angle_unit": None,
            },
            "coordinate_reference_change": None,
            "context_operations": [],
        },
        "result": None,
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IfcGeometryBasisConfigureOperationDetail.model_validate(
            {
                "request": detail.request.model_dump(mode="json"),
                "direct_session_alias": "direct_001",
            }
        )


def test_geometry_basis_persisted_detail_distinguishes_omission_from_explicit_null() -> None:
    detail = IfcGeometryBasisConfigureOperationDetail(
        request=ConfigureIfcGeometryBasisCommand(
            context_operations=(
                UpdateSubcontextOperation(
                    context_identifier="Body",
                    target_scale=None,
                ),
            )
        )
    )

    assert detail.persisted_payload() == {
        "detail_kind": "ifc_model_configure_geometry_basis",
        "request": {
            "context_operations": [
                {
                    "action": "update_subcontext",
                    "context_identifier": "Body",
                    "target_scale": None,
                }
            ]
        },
    }


def test_geometry_basis_operation_lifecycle_requires_base_and_truthful_result() -> None:
    prepared = _configure_operation(
        status=CriticalOperationStatus.PREPARED,
        detail=_configure_detail(),
    )
    assert prepared.detail is not None
    assert prepared.detail.result is None

    succeeded = _configure_operation(
        status=CriticalOperationStatus.SUCCEEDED,
        detail=_configure_detail(result="geometry_basis_configured"),
        result_ifc_revision_id="ifc_revision_2",
        finished_at=_NOW,
    )
    assert succeeded.detail is not None
    assert succeeded.detail.result == "geometry_basis_configured"

    with pytest.raises(ValidationError, match="requires base revision bindings"):
        _configure_operation(
            status=CriticalOperationStatus.PREPARED,
            detail=_configure_detail(),
            base_ifc_revision_id=None,
            expected_ifc_write_epoch=None,
        )
    with pytest.raises(ValidationError, match="requires its durable result"):
        _configure_operation(
            status=CriticalOperationStatus.SUCCEEDED,
            detail=_configure_detail(),
            result_ifc_revision_id="ifc_revision_2",
            finished_at=_NOW,
        )
    with pytest.raises(ValidationError, match="cannot report a result"):
        _configure_operation(
            status=CriticalOperationStatus.FAILED,
            detail=_configure_detail(result="geometry_basis_configured"),
            error_code="IFC_BYTES_INVALID",
            error_message="Generated IFC failed validation.",
            error_phase=CriticalOperationErrorPhase.IFC_VALIDATION,
            finished_at=_NOW,
        )


def test_geometry_basis_operation_history_projects_business_input_without_server_ids() -> None:
    operation = _configure_operation(
        status=CriticalOperationStatus.SUCCEEDED,
        detail=_configure_detail(result="geometry_basis_configured"),
        result_ifc_revision_id="ifc_revision_2",
        finished_at=_NOW,
    )
    catalog_entry = DataCatalogEntry(
        catalog_entry_id="catalog_operation_geometry_basis",
        owner_id="owner_local",
        tenant_id="tenant_local",
        task_id="task_local",
        run_id="run_local",
        resource_kind=CatalogResourceKind.OPERATION,
        alias="op_002",
        ordinal=2,
        catalog_revision=2,
        operation_id=operation.operation_id,
        created_at=_NOW,
    )
    published = PublishedCriticalOperation(
        operation=operation,
        catalog_entry=catalog_entry,
    )

    result = _operation_get_result(
        SimpleNamespace(
            operation=published,
            result_package=None,
            input_packages={},
        )
    )
    payload = result.model_dump(mode="json")

    assert payload["operation"]["tool_name"] == "ifc_model_configure_geometry_basis"
    assert payload["operation"]["primary_result"] == {"result": "geometry_basis_configured"}
    assert payload["detail"] == operation.detail.model_dump(mode="json")
    serialized = result.model_dump_json()
    for server_only_name in (
        "operation_ifc_geometry_basis",
        "owner_local",
        "tenant_local",
        "task_local",
        "run_local",
        "ifc_revision_1",
        "ifc_revision_2",
        "server-only-geometry-basis-key",
        "request_fingerprint",
        "artifact_id",
        "writer_generation",
    ):
        assert server_only_name not in serialized


def test_failed_geometry_basis_history_keeps_request_and_no_success_result() -> None:
    operation = _configure_operation(
        status=CriticalOperationStatus.FAILED,
        detail=_configure_detail(),
        error_code="IFC_NO_CHANGES",
        error_message="The submitted geometry basis does not change the exact IFC model.",
        error_phase=CriticalOperationErrorPhase.IFC_VALIDATION,
        finished_at=_NOW,
    )
    published = PublishedCriticalOperation(
        operation=operation,
        catalog_entry=DataCatalogEntry(
            catalog_entry_id="catalog_operation_geometry_basis_failed",
            owner_id="owner_local",
            tenant_id="tenant_local",
            task_id="task_local",
            run_id="run_local",
            resource_kind=CatalogResourceKind.OPERATION,
            alias="op_003",
            ordinal=3,
            catalog_revision=3,
            operation_id=operation.operation_id,
            created_at=_NOW,
        ),
    )

    result = _operation_get_result(
        SimpleNamespace(
            operation=published,
            result_package=None,
            input_packages={},
        )
    )

    assert result.operation.tool_name == "ifc_model_configure_geometry_basis"
    assert result.detail.request.project_unit_change is not None
    assert result.detail.result is None
    assert result.error.code == "IFC_NO_CHANGES"
    assert result.error.phase == "ifc_validation"


def _project_result() -> IfcProjectSummary:
    return IfcProjectSummary(
        global_id=_PROJECT_GLOBAL_ID,
        name="Updated Gas Network",
        phase="Construction",
    )


def _project_update_detail(
    *, result: IfcProjectSummary | None = None,
) -> IfcProjectUpdateOperationDetail:
    return IfcProjectUpdateOperationDetail(
        request=UpdateIfcProjectCommand(
            patch=IfcProjectPatch(name="Updated Gas Network", phase="Construction"),
            clear_fields=("description",),
        ),
        result=result,
    )


def _project_update_operation(
    *,
    status: CriticalOperationStatus,
    detail: IfcProjectUpdateOperationDetail,
    **changes: object,
) -> CriticalOperationRecord:
    values: dict[str, object] = {
        "operation_id": "operation_ifc_project_update",
        "owner_id": "owner_local",
        "tenant_id": "tenant_local",
        "task_id": "task_local",
        "run_id": "run_local",
        "operation_kind": CriticalOperationKind.IFC_MODEL_WRITE,
        "tool_name": "ifc_project_update",
        "intent_summary": IFC_PROJECT_UPDATE_INTENT_SUMMARY,
        "idempotency_key": "server-only-project-update-key",
        "request_fingerprint": "c" * 64,
        "status": status,
        "detail": detail,
        "base_ifc_revision_id": "ifc_revision_2",
        "expected_ifc_write_epoch": 2,
        "started_at": _NOW,
    }
    values.update(changes)
    return CriticalOperationRecord.model_validate(values)


def test_project_update_operation_persists_exact_request_and_truthful_result() -> None:
    prepared = _project_update_operation(
        status=CriticalOperationStatus.PREPARED,
        detail=_project_update_detail(),
    )
    prepared_payload = _operation_detail_json(prepared)
    assert prepared_payload == {
        "detail_kind": "ifc_project_update",
        "request": {
            "patch": {"name": "Updated Gas Network", "phase": "Construction"},
            "clear_fields": ["description"],
        },
    }
    assert IfcProjectUpdateOperationDetail.model_validate(prepared_payload) == prepared.detail

    succeeded = _project_update_operation(
        status=CriticalOperationStatus.SUCCEEDED,
        detail=_project_update_detail(result=_project_result()),
        result_ifc_revision_id="ifc_revision_3",
        finished_at=_NOW,
    )
    assert _operation_detail_json(succeeded)["result"] == _project_result().model_dump(mode="json")

    failed = _project_update_operation(
        status=CriticalOperationStatus.FAILED,
        detail=_project_update_detail(),
        error_code="IFC_NO_CHANGES",
        error_message="The Project patch does not change the exact IFC model.",
        error_phase=CriticalOperationErrorPhase.IFC_VALIDATION,
        finished_at=_NOW,
    )
    assert "result" not in _operation_detail_json(failed)

    with pytest.raises(ValidationError, match="requires its durable result"):
        _project_update_operation(
            status=CriticalOperationStatus.SUCCEEDED,
            detail=_project_update_detail(),
            result_ifc_revision_id="ifc_revision_3",
            finished_at=_NOW,
        )


def test_project_update_typed_replay_compares_exact_request() -> None:
    stored = _project_update_detail(result=_project_result()).persisted_payload()
    replay = SimpleNamespace(
        operation_kind=CriticalOperationKind.IFC_MODEL_WRITE.value,
        tool_name="ifc_project_update",
        request_fingerprint="c" * 64,
        intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
        base_ifc_revision_id="ifc_revision_2",
        expected_ifc_write_epoch=2,
        operation_detail_json=stored,
    )
    PostgresIfcModelRepositoryMixin._assert_revision_replay(
        replay,
        tool_name="ifc_project_update",
        request_digest="c" * 64,
        intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
        operation_detail=_project_update_detail().persisted_payload(),
    )

    changed = IfcProjectUpdateOperationDetail(
        request=UpdateIfcProjectCommand(patch=IfcProjectPatch(name="Another Project"))
    ).persisted_payload()
    with pytest.raises(IfcModelConflictError, match="request detail changed"):
        PostgresIfcModelRepositoryMixin._assert_revision_replay(
            replay,
            tool_name="ifc_project_update",
            request_digest="c" * 64,
            intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
            operation_detail=changed,
        )


def test_project_update_operation_history_is_provider_safe() -> None:
    operation = _project_update_operation(
        status=CriticalOperationStatus.SUCCEEDED,
        detail=_project_update_detail(result=_project_result()),
        result_ifc_revision_id="ifc_revision_3",
        finished_at=_NOW,
    )
    published = PublishedCriticalOperation(
        operation=operation,
        catalog_entry=DataCatalogEntry(
            catalog_entry_id="catalog_operation_project_update",
            owner_id="owner_local",
            tenant_id="tenant_local",
            task_id="task_local",
            run_id="run_local",
            resource_kind=CatalogResourceKind.OPERATION,
            alias="op_004",
            ordinal=4,
            catalog_revision=4,
            operation_id=operation.operation_id,
            created_at=_NOW,
        ),
    )

    result = _operation_get_result(
        SimpleNamespace(operation=published, result_package=None, input_packages={})
    )
    payload = result.model_dump(mode="json")
    assert payload["operation"]["tool_name"] == "ifc_project_update"
    assert payload["operation"]["primary_result"] == {"result": "project_updated"}
    assert payload["detail"] == {**operation.detail.model_dump(mode="json"), "result": "project_updated"}
    serialized = result.model_dump_json()
    for server_only_name in (
        "operation_ifc_project_update",
        "owner_local",
        "tenant_local",
        "task_local",
        "run_local",
        "ifc_revision_2",
        "ifc_revision_3",
        "server-only-project-update-key",
    ):
        assert server_only_name not in serialized
