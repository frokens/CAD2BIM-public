"""Edit-specific barriers and exact Family reuse; shared Writer is tested elsewhere."""
from contextlib import nullcontext
from datetime import UTC, datetime
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from CAD2BIM_backend.application.exact_payload_resolver import ExactParquetPayload
from CAD2BIM_backend.application.product_edit_service import IfcProductsEditService
from CAD2BIM_backend.domain.data_runtime import ArtifactVersionRef, DataPackageMemberRecord, PackageMemberKind
from CAD2BIM_backend.domain.product_edit import EditIfcProductsCommand, IfcProductsEditPreflightResult, IfcProductEditPreflightProduct
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from CAD2BIM_backend.ports.ifc_revision_runtime import InspectExactIfcRevisionRequest
from tests.unit.backend.test_ifc_revision_writer import _fixture, _claim
from tests.unit.backend.test_product_apply_service import Inputs, NumericRuntime, _request, _mapped_binding, _scope


async def _run(tmp_path, records, *, accepted=True, observe_interfaces=False, fail_frame_at=None):
    scope, events = _scope(), []
    prototype = _request(_mapped_binding(tmp_path))
    if observe_interfaces:
        from CAD2BIM_backend.domain.canonical import sha256_digest
        from tests.unit.backend.product_family_fixtures import current_family_contract
        family = current_family_contract(interfaces=[{
            "interface_key": "datum", "owner_component_key": "body", "interface_kind": "internal",
            "source": {"fragment_key": "body_shape", "model_interface_key": "datum"}}])
        prototype = prototype.model_copy(update={"family_contract": family, "family_contract_digest": sha256_digest(family)})
    inputs, numeric = Inputs(prototype, events), NumericRuntime(events, fail_frame_at=fail_frame_at)
    types = {name: pa.float64() if name.startswith("origin_") else pa.string() for row in records for name in row}
    table = pa.Table.from_pylist(records, schema=pa.schema([pa.field(name, kind) for name, kind in types.items()]))
    sink = pa.BufferOutputStream(); pq.write_table(table, sink)
    content = sink.getvalue().to_pybytes()
    member = DataPackageMemberRecord(member_id="member", owner_id=scope.owner_id,
        tenant_id=scope.identity.tenant_id, task_id=scope.identity.task_id, package_id="package",
        member_name="payloads/edits/part-00000.parquet", member_kind=PackageMemberKind.PAYLOAD,
        artifact=ArtifactVersionRef(artifact_id="asset", version_id="version", sha256=hashlib.sha256(content).hexdigest()),
        media_type="application/vnd.apache.parquet", logical_payload_name="edits", logical_key_fields=("product_key",),
        row_count=len(records), created_at=datetime.now(UTC))
    package = SimpleNamespace(members=(member,))
    reports = {}
    async def get_package(authority, package_id):
        if package_id == "package": return package
        report = reports.get(package_id)
        return SimpleNamespace(catalog_entry=SimpleNamespace(alias="data_002")) if report else None
    data = SimpleNamespace(get_package=AsyncMock(side_effect=get_package), get_operation_for_result_package=AsyncMock(return_value=None))
    async def materialize(*args, **kwargs):
        path = tmp_path / "input.parquet"; path.write_bytes(content)
        return {"member": path}
    payloads = SimpleNamespace(transform_payloads=lambda package: {"edits": ExactParquetPayload("edits", ("product_key",), member)},
        materialize_members=materialize)
    _, _, repository, _ = _fixture()
    prepared = repository.prepared.model_copy(update={"before": repository.prepared.before.model_copy(update={"scope": {
        "owner_id": scope.owner_id, "tenant_id": scope.identity.tenant_id, "task_id": scope.identity.task_id, "run_id": scope.run_id}})})
    async def fail(*args, product_report=None, **kwargs):
        if product_report: reports[product_report.package.package_id] = product_report
        return "op_002"
    writer = SimpleNamespace(recover_revision=AsyncMock(return_value=None), prepare=AsyncMock(return_value=prepared),
        claim_execution=AsyncMock(return_value=_claim(prepared)), guard_registration=lambda *args: None,
        resume_selected_revision=AsyncMock(return_value=None), open_prepared_base=AsyncMock(return_value=object()),
        preflight_prepared_product_body=AsyncMock(), fail_prepared=AsyncMock(side_effect=fail), retire_execution=AsyncMock(), commit=AsyncMock())
    class Runtime:
        def invocation_scope(self, *args, **kwargs): return nullcontext()
        async def execute(self, request, **kwargs):
            if isinstance(request, InspectExactIfcRevisionRequest):
                events.append("targets")
                return SimpleNamespace(edit_targets=IfcProductsEditPreflightResult(accepted=accepted,
                    products=tuple(IfcProductEditPreflightProduct(input_row_index=t.input_row_index,
                        target_global_id=t.target_global_id, before_ifc_class="IfcWall") for t in request.edit_targets_request.targets)))
            events.append("edit")
            raise RuntimeError("known candidate execution failure")
    async def resolve(identity, reference):
        return SimpleNamespace(version=SimpleNamespace(family_version_id=reference,
            family_contract=prototype.family_contract))
    catalog = SimpleNamespace(resolve_exact_version=AsyncMock(side_effect=resolve))
    references = SimpleNamespace(expand=AsyncMock(side_effect=lambda identity, value, kinds: "cur." + value))
    class Coordinator:
        async def invoke(self, *args, operation, **kwargs): return await operation()
    service = IfcProductsEditService(families=None, data=data, payloads=payloads, inputs=inputs,
        realization_runtime=numeric, revision_runtime=Runtime(), writer=writer, coordinator=Coordinator(),
        storage=LocalStorage(tmp_path / "storage"), family_catalog=catalog, references=references)
    result = await service.edit(scope, EditIfcProductsCommand(package_id="package", payload_name="edits",
        base_revision_id=prepared.before.revision_id), idempotency_key="one-edit")
    report = next(iter(reports.values()), None)
    return result, report, events, writer, catalog, references


def _replace(index, family):
    return {"product_key": str(index), "target_global_id": "0" * 21 + str(index), "action": "replace",
        "family_version_ref": family, "origin_x_m": 0., "origin_y_m": 0., "origin_z_m": 0.,
        "parameters_json": "{}", "external_constraints_json": "[]"}


def test_terminal_interfaces_bind_original_rows_and_clear_uncommitted_candidate_guids():
    from CAD2BIM_backend.application.product_edit_report import empty_edit_report, terminal_edit_rows, EDIT_REPORT_SCHEMAS
    from CAD2BIM_backend.domain.ifc_product_compilation import IfcProductComponentBinding
    rows = empty_edit_report()
    rows["products"] = [{"input_row_index": i, "action": action} for i,action in enumerate(("replace", "placement", "replace", "delete"))]
    rows["interfaces"] = [{"input_row_index": index, "product_key": key, "component_key": "body", "interface_key": "port"}
        for index,key in ((2,"last"),(0,"first"))]
    bindings = tuple(IfcProductComponentBinding(input_row_index=index, product_key=key, component_key="body",
        parent_component_key=None, global_id=str(index)*22) for index,key in ((0,"first"),(2,"last")))
    actual = terminal_edit_rows(rows, committed=True, bindings=tuple(reversed(bindings)))
    assert [(r["input_row_index"],r["component_global_id"]) for r in actual["interfaces"]] == [(2,"2"*22),(0,"0"*22)]
    assert {r["input_row_index"] for r in actual["interfaces"]} == {0,2}
    rejected = terminal_edit_rows(actual, committed=False, bindings=bindings)
    assert all(r["component_global_id"] is None and r["commit_status"] == "NOT_COMMITTED" for r in rejected["interfaces"])
    rows["interfaces"] = []
    assert "interfaces" not in terminal_edit_rows(rows, committed=True, bindings=bindings)
    field = EDIT_REPORT_SCHEMAS["interfaces"].field("component_global_id")
    assert field.nullable and pa.types.is_string(field.type)


@pytest.mark.asyncio
async def test_delete_without_body_keeps_unknown_execution_and_three_table_failed_report(tmp_path):
    result, report, events, writer, _, _ = await _run(tmp_path, [
        {"product_key": "delete", "target_global_id": "0" * 22, "action": "delete"}])
    assert result.outcome == "failed" and result.summary.input_count == 1 and result.summary.committed_count == 0
    assert events == ["targets", "edit"]
    assert report.package.name == "Product Edit report"
    writer.preflight_prepared_product_body.assert_not_awaited()
    assert {m.logical_payload_name for m in report.members if m.logical_payload_name} == {"products", "changes", "checks"}
    products = next(m for m in report.members if m.logical_payload_name == "products")
    from CAD2BIM_backend.ports.storage import StorageKey
    content = await LocalStorage(tmp_path / "storage").read(StorageKey(report.package.owner_id, report.package.task_id, products.artifact.artifact_id, 1))
    import io
    row = pq.read_table(io.BytesIO(content)).to_pylist()[0]
    assert row["execution_status"] == "UNKNOWN" and row["before_ifc_class"] == "IfcWall"
    assert row["after_ifc_class"] is None and row["result_global_id"] is None


@pytest.mark.asyncio
async def test_short_family_refs_resolve_once_per_exact_family_and_target_barrier_precedes_build(tmp_path):
    result, report, events, writer, catalog, references = await _run(tmp_path,
        [_replace(0, "fv_first"), _replace(1, "fv_second"), _replace(2, "fv_first")], accepted=False)
    assert result.outcome == "failed" and result.summary.input_count == 3
    assert events == ["closure", "closure", "targets"]
    writer.preflight_prepared_product_body.assert_not_awaited()
    assert catalog.resolve_exact_version.await_count == 2
    assert [call.args[1] for call in catalog.resolve_exact_version.await_args_list] == ["cur.fv_first", "cur.fv_second"]
    assert all(call.args[2] == {"fv"} for call in references.expand.await_args_list)


@pytest.mark.asyncio
async def test_mixed_families_finish_numeric_barrier_before_first_geometry(tmp_path):
    result, report, events, writer, catalog, _ = await _run(tmp_path,
        [_replace(0, "fv_first"), _replace(1, "fv_second"), _replace(2, "fv_first")], observe_interfaces=True)
    assert result.outcome == "failed" and result.summary.input_count == 3
    writer.preflight_prepared_product_body.assert_awaited_once()
    assert catalog.resolve_exact_version.await_count == 2
    numeric_events = [index for index, event in enumerate(events) if event.startswith("precheck:")]
    assert len(numeric_events) == 2
    assert events.index("targets") < numeric_events[0] < numeric_events[1] < events.index("hydrate")
    assert "edit" not in events
    # Family groups execute rows 0/2, then 1; retained observations must still
    # identify the original physical input row rather than a group-local row.
    from CAD2BIM_backend.ports.storage import StorageKey
    import io
    member = next(m for m in report.members if m.logical_payload_name == "interfaces")
    content = await LocalStorage(tmp_path / "storage").read(StorageKey(
        report.package.owner_id, report.package.task_id, member.artifact.artifact_id, 1))
    interfaces = pq.read_table(io.BytesIO(content)).to_pylist()
    assert list(dict.fromkeys(row["product_key"] for row in interfaces)) == ["0", "2", "1"]
    assert all(row["input_row_index"] == int(row["product_key"]) for row in interfaces)
    assert all(row["component_global_id"] is None and row["commit_status"] == "NOT_COMMITTED" for row in interfaces)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_rejection", [False, True])
async def test_replacement_group_over_closure_page_keeps_complete_numeric_barrier(tmp_path, late_rejection):
    from CAD2BIM_backend.domain.product_realization import MAX_PRODUCT_REALIZATION_BATCH_ITEMS
    count = MAX_PRODUCT_REALIZATION_BATCH_ITEMS + 1
    records = [{**_replace(i, "fv_first"), "target_global_id": f"{i:022d}"} for i in range(count)]
    result, _, events, writer, catalog, _ = await _run(tmp_path, records,
        fail_frame_at=MAX_PRODUCT_REALIZATION_BATCH_ITEMS if late_rejection else None)
    assert result.outcome == "failed" and result.summary.input_count == count and result.summary.committed_count == 0
    assert [event for event in events if event.startswith("precheck:")] == [f"precheck:{MAX_PRODUCT_REALIZATION_BATCH_ITEMS}", "precheck:1"]
    assert catalog.resolve_exact_version.await_count == 1
    if late_rejection:
        assert "hydrate" not in events and "build" not in events
        assert result.error_code == "PRODUCT_EDIT_PRECHECK_REJECTED"
    else:
        # The fake stops at the first build. Reaching it proves both bounded
        # request pages completed numeric validation before any construction.
        assert events.index("precheck:1") < events.index("hydrate") < events.index("build")
    assert "edit" not in events
    writer.commit.assert_not_awaited()
    writer.fail_prepared.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_report_staging_preserves_original_rejection_for_recovery(tmp_path, monkeypatch):
    async def no_report(*args, **kwargs):
        raise RuntimeError("storage unavailable for diagnostic report")
    monkeypatch.setattr("CAD2BIM_backend.application.product_edit_service.stage_product_edit_report", no_report)
    result, report, events, writer, _, _ = await _run(tmp_path,
        [{"product_key": "delete", "target_global_id": "0" * 22, "action": "delete"}], accepted=False)
    assert result.error_code == "PRODUCT_EDIT_PRECHECK_REJECTED"
    assert result.error_phase == "ifc_validation"
    assert result.message.startswith("The batch has input or target dependency blockers")
    assert "storage unavailable for diagnostic report" in result.message
    recorded = writer.fail_prepared.await_args.kwargs
    assert recorded["error_code"] == result.error_code and recorded["error_message"] == result.message
    assert recorded["product_report"] is None and report is None and result.summary is None
    assert result.report_package_alias is None
