"""Core barriers and one real native/Core/DB representative; no SYSTEM rebuild."""
from __future__ import annotations

import hashlib
import io
import json
import os
import logging
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from CAD2BIM_backend.application.product_apply_service import IfcProductsApplyService
from CAD2BIM_backend.application.product_realization_input_service import _PreparedPublishedProductRealizationInputs
from CAD2BIM_backend.application.exact_payload_resolver import ExactParquetPayload
from CAD2BIM_backend.domain.data_runtime import DataPackageMemberRecord, PackageMemberKind, ArtifactVersionRef, ArrowColumnRecord
from CAD2BIM_backend.domain.product_apply import ApplyIfcProductsCommand
from CAD2BIM_backend.domain.product_realization import (ProductRealizationOutputNeed, SolvedProductRealizationProposal,
    ProductGeometryInterfaceEvaluation, finalize_product_realization)
from CAD2BIM_backend.ports.ifc_revision_runtime import InspectExactIfcRevisionRequest, IfcRevisionRuntimeError
from CAD2BIM_backend.ports.product_realization_runtime import ProductRealizationRuntimeUnavailableError
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from tests.unit.backend.test_product_realization_runtime import _request, _mapped_binding
from tests.unit.backend.test_product_realization_preview_service import _scope
from tests.unit.backend.test_ifc_revision_writer import _fixture, _claim


SCHEMA = pa.schema([pa.field("product_key", pa.string()),
    *[pa.field(k, pa.float64()) for k in ("origin_x_m", "origin_y_m", "origin_z_m")],
    pa.field("parameters_json", pa.string()), pa.field("external_constraints_json", pa.string())])


def record(key, **overrides):
    return {"product_key": key, "origin_x_m": 0., "origin_y_m": 0., "origin_z_m": 0.,
            "parameters_json": "{}", "external_constraints_json": "[]", **overrides}


def numeric_result(request):
    proposal = SolvedProductRealizationProposal.model_validate({"status": "SOLVED",
        "components": [{"component_key": "body"}],
        "fragments": [{"fragment_key": "body_shape", "binding_key": "body_geometry", "geometry_parameters": {}}]})
    binding = request.geometry_bindings[0]
    evaluation = ProductGeometryInterfaceEvaluation(fragment_key="body_shape", binding_key="body_geometry",
        normalized_parameters={}, interfaces=binding.interfaces)
    return finalize_product_realization(request, proposal, (), output_need=ProductRealizationOutputNeed.INTERFACES,
                                        interface_evaluations=(evaluation,))


class Inputs:
    def __init__(self, prototype, events):
        self.prototype, self.events = prototype, events
        numeric = prototype.model_copy(update={"geometry_bindings": tuple(
            binding.model_copy(update={"geometry": None}) for binding in prototype.geometry_bindings)})
        self.closure = _PreparedPublishedProductRealizationInputs(numeric)
        self.hydrated = []
    async def prepare_published_closure(self, scope, *, family_version):
        self.events.append("closure")
        return self.closure
    async def load_geometry_materials(self, scope, *, family_version, requests, prechecks):
        self.events.append("hydrate")
        assert all(result.proposal.status.value == "SOLVED" and result.frame_check_status == "PASS" for result in prechecks)
        self.hydrated.extend(prechecks)
        return tuple(request.model_copy(update={"geometry_bindings": self.prototype.geometry_bindings}) for request in requests)


class NumericRuntime:
    def __init__(self, events, *, fail_frame=False, fail_frame_at=None, technical=None):
        self.events, self.fail_frame, self.technical = events, fail_frame, technical
        self.fail_frame_at = fail_frame_at
        self.groups, self.results, self.builds = [], [], []
    def invocation_scope(self, *args, **kwargs):
        return nullcontext()
    async def precheck_batch(self, requests, *, window_size=None):
        if window_size is not None:
            for offset in range(0, len(requests), window_size):
                try:
                    async for result in self.precheck_batch(requests[offset:offset+window_size]):
                        yield result
                except ProductRealizationRuntimeUnavailableError as exc:
                    if exc.item_index is not None:
                        exc.item_index += offset
                    raise
            return
        self.events.append(f"precheck:{len(requests)}")
        self.groups.append(tuple(requests))
        for index, request in enumerate(requests):
            if self.technical is not None and index == 1:
                raise ProductRealizationRuntimeUnavailableError("observed numeric technical first error",
                    item_index=1 if self.technical == "precise" else None, retryable=False)
            result = numeric_result(request)
            if (self.fail_frame and index == 0) or self.fail_frame_at == len(self.results):
                result = result.model_copy(update={"frame_check_status": "FAIL"})
            self.results.append(result)
            yield result
    async def instantiate_batch(self, requests, prechecks, *, output_need, source_cache=None, window_size=None):
        self.events.append("build")
        self.builds.extend(prechecks)
        assert output_need is ProductRealizationOutputNeed.LOCAL_FRAGMENTS
        assert all(precheck is original for precheck, original in zip(prechecks, self.results))
        raise ProductRealizationRuntimeUnavailableError("stop after global numeric barrier", item_index=0, retryable=False)
        yield


async def cheap_core(tmp_path, records, *, missing_body=False, preflight_fault=None, resume_fault=None, observed_events=None, **runtime_options):
    scope = _scope()
    prototype = _request(_mapped_binding(tmp_path))
    events = [] if observed_events is None else observed_events
    inputs, realizations = Inputs(prototype, events), NumericRuntime(events, **runtime_options)
    table = pa.Table.from_pylist(records, schema=SCHEMA)
    sink = pa.BufferOutputStream(); pq.write_table(table, sink)
    content = sink.getvalue().to_pybytes()
    member = DataPackageMemberRecord(member_id="member_input", owner_id=scope.owner_id,
        tenant_id=scope.identity.tenant_id, task_id=scope.identity.task_id, package_id="data_package_input",
        member_name="payloads/input/part-00000.parquet", member_kind=PackageMemberKind.PAYLOAD,
        artifact=ArtifactVersionRef(artifact_id="artifact_input", version_id="artifact_version_input", sha256=hashlib.sha256(content).hexdigest()),
        media_type="application/vnd.apache.parquet", created_at=datetime.now(timezone.utc), logical_payload_name="input",
        row_count=table.num_rows, logical_key_fields=("product_key",),
        arrow_schema=tuple(ArrowColumnRecord(name=f.name, type=str(f.type), nullable=f.nullable) for f in table.schema))
    package = SimpleNamespace(members=(member,), package=SimpleNamespace(package_id="data_package_input"))
    class Data:
        reports = {}
        async def get_package(self, owner_scope, package_id):
            if package_id == "data_package_input": return package
            report = self.reports.get(package_id)
            return None if report is None else SimpleNamespace(package=report.package, members=report.members,
                catalog_entry=SimpleNamespace(alias="data_002"))
    data = Data()
    class Payloads:
        def transform_payloads(self, published):
            return {m.logical_payload_name: ExactParquetPayload(m.logical_payload_name,m.logical_key_fields,m)
                    for m in published.members if m.member_kind is PackageMemberKind.PAYLOAD}
        async def materialize_members(self, members, **kwargs):
            path = tmp_path / "materialized-input.parquet"; path.write_bytes(content)
            return {member.member_id: path}
    _, _, repo, _ = _fixture()
    class Writer:
        report = None
        async def recover_revision(self, *args, **kwargs):
            events.append("recover")
            return None
        async def prepare(self, owner_scope, **kwargs):
            before = repo.prepared.before.model_copy(update={"scope":owner_scope})
            self.prepared = repo.prepared.model_copy(update={"before":before, "request_digest":kwargs["request_digest"], "idempotency_key":kwargs["idempotency_key"]})
            return self.prepared
        async def claim_execution(self, *args): return _claim(self.prepared)
        def guard_registration(self, *args): return None
        async def resume_selected_revision(self, *args):
            if resume_fault is not None: raise resume_fault
            return None
        async def open_prepared_base(self, *args): return object()
        async def preflight_prepared_product_body(self, *args):
            events.append("body")
            if preflight_fault is not None: raise preflight_fault
            if missing_body: raise IfcRevisionRuntimeError("IFC_REPRESENTATION_CONTEXT_NOT_FOUND","Body is missing",phase="business",execution_started=True)
            return SimpleNamespace(body=True)
        async def fail_prepared(self, *args, product_report=None, **kwargs):
            events.append("fail")
            self.report=product_report
            if product_report is not None: data.reports[product_report.package.package_id]=product_report
            return "op_002"
        async def retire_execution(self, *args): events.append("retire")
        async def commit(self, *args, **kwargs): raise AssertionError("cheap barrier tests must not construct or commit IFC")
    class Revision:
        def invocation_scope(self, *args, **kwargs): return nullcontext()
        async def execute(self, request, **kwargs):
            raise AssertionError("cheap barrier tests must not execute an IFC Worker")
    class Coordinator:
        async def invoke(self, *args, operation, **kwargs): return await operation()
    family = SimpleNamespace(version=SimpleNamespace(family_contract=prototype.family_contract),
        geometry_bindings=tuple(SimpleNamespace(binding_key=binding.binding_key,
            geometry_resource_version_id=f"geometry_version_{binding.binding_key}")
            for binding in prototype.geometry_bindings))
    class Families:
        async def get_product_family_version_by_id(self, *args, **kwargs): return family
    writer=Writer(); storage=LocalStorage(tmp_path/"report-storage")
    service=IfcProductsApplyService(families=Families(), data=data, payloads=Payloads(), inputs=inputs,
        realization_runtime=realizations, revision_runtime=Revision(), writer=writer, coordinator=Coordinator(),storage=storage)
    result=await service.apply(scope,ApplyIfcProductsCommand(family_version_id="family_version_exact",package_id="data_package_input",payload_name="input"),idempotency_key="core-barrier")
    tables={}
    for item in writer.report.members:
        if item.member_kind is PackageMemberKind.PAYLOAD:
            from CAD2BIM_backend.ports.storage import StorageKey
            tables[item.logical_payload_name]=pq.read_table(io.BytesIO(await storage.read(StorageKey(scope.owner_id,scope.identity.task_id,item.artifact.artifact_id,1)))).to_pylist()
    return result, tables, events, inputs, realizations


def test_committed_interface_component_guids_come_from_this_apply_bindings_only():
    from CAD2BIM_backend.application.product_apply_report import empty_report_rows, report_for_commit, REPORT_SCHEMAS
    from CAD2BIM_backend.domain.ifc_product_compilation import IfcProductComponentBinding
    rows = empty_report_rows()
    rows["products"] = [{"product_key": key} for key in ("first", "last")]
    rows["interfaces"] = [{"product_key": key, "component_key": "body", "interface_key": "port"}
        for key in ("last", "first")]
    bindings = tuple(IfcProductComponentBinding(product_key=key, input_row_index=index, component_key="body",
        parent_component_key=None, global_id=str(index)*22) for index,key in enumerate(("first", "last")))
    actual = report_for_commit(rows, bindings, committed=True)
    assert [r["component_global_id"] for r in actual["interfaces"]] == ["1"*22, "0"*22]
    assert all(r["commit_status"] == "COMMITTED" for r in actual["interfaces"])
    rejected = report_for_commit(actual, bindings, committed=False)
    assert all(r["component_global_id"] is None and r["commit_status"] == "NOT_COMMITTED" for r in rejected["interfaces"])
    field = REPORT_SCHEMAS["interfaces"].field("component_global_id")
    assert field.nullable and pa.types.is_string(field.type)
    assert all("component_global_id" not in r for r in rows["interfaces"])


@pytest.mark.asyncio
async def test_input_errors_preserve_all_rows_and_precheck_legal_rows(tmp_path):
    rows=[record("duplicate"),record("duplicate"),record("bad-json",parameters_json="null"),
          record("bad-parameter",parameters_json='{"undeclared":42}'),record("valid")]
    result,tables,events,inputs,runtime=await cheap_core(tmp_path,rows)
    assert result.summary.outcome=="rejected" and result.summary.input_count==5
    assert [r["solve_status"] for r in tables["products"]]==["INPUT_INVALID"]*4+["SOLVED"]
    assert len(runtime.results)==1 and not inputs.hydrated and not runtime.builds
    assert set(tables)=={"products","components","interfaces","checks"}


@pytest.mark.asyncio
async def test_normal_interface_fail_is_global_zero_geometry_barrier(tmp_path):
    result,tables,events,inputs,runtime=await cheap_core(tmp_path,[record("a"),record("b")],fail_frame=True)
    assert result.summary.outcome=="rejected" and result.summary.solve_executed_count==2
    assert len(runtime.results)==2 and not inputs.hydrated and "build" not in events
    assert events.count("body")==1 and all(r["build_status"]=="NOT_EXECUTED" for r in tables["products"])


@pytest.mark.asyncio
async def test_final_numeric_page_failure_keeps_pass_prefix_at_zero_geometry(tmp_path, monkeypatch):
    import CAD2BIM_backend.application.product_apply_service as core
    monkeypatch.setattr(core, "_NUMERIC_GROUP_ITEMS", 2)
    result,tables,events,inputs,runtime=await cheap_core(
        tmp_path,[record("pass-a"),record("pass-b"),record("last-fail")],fail_frame_at=2)
    assert [len(group) for group in runtime.groups]==[2,1]
    assert [row.frame_check_status for row in runtime.results]==["PASS","PASS","FAIL"]
    assert result.summary.solve_executed_count==3 and result.summary.interface_constraints_evaluated_product_count==3
    summary = result.summary.model_dump(mode="json")
    assert summary["interface_constraints_evaluated_product_count"] == len(runtime.results)
    assert "interface_checked_count" not in summary
    assert all(row.execution_stage == "interface_constraints_evaluated" for row in runtime.results)
    assert result.summary.outcome=="rejected" and result.summary.build_executed_count==0
    assert not inputs.hydrated and not runtime.builds and "hydrate" not in events
    assert all(row["build_status"]=="NOT_EXECUTED" and row["root_global_id"] is None
               for row in tables["products"])


@pytest.mark.asyncio
async def test_candidate_capacity_diagnostic_is_retained_in_failed_report(tmp_path):
    fault = IfcRevisionRuntimeError(
        "IFC_REVISION_CANDIDATE_TOO_LARGE", "candidate is 9 bytes; limit is 8 bytes",
        phase="candidate_save", execution_started=True,
        diagnostic={"actual_bytes": 9, "limit_bytes": 8},
    )
    result, tables, events, _, _ = await cheap_core(tmp_path, [record("a")], preflight_fault=fault)
    assert result.error_code == fault.code and result.summary.outcome == "technical_failure"
    assert result.summary.committed_count == 0 and "fail" in events
    failure = tables["checks"][-1]
    assert failure["phase"] == "candidate_save" and failure["error_code"] == fault.code
    assert json.loads(failure["diagnostic_json"]) == fault.diagnostic
    assert all(row["root_global_id"] is None for row in tables["products"])


@pytest.mark.asyncio
async def test_realization_source_diagnostic_is_retained_in_failed_report(tmp_path, monkeypatch):
    from CAD2BIM_backend.infrastructure.product_realization_runtime.child import _ChildError, _failure_payload
    from CAD2BIM_backend.infrastructure.product_realization_runtime.adapter import _raise_child_failure
    diagnostic = {"exception_type": "ValueError", "message": "exact authored source failed",
        "source_path": "helpers.py", "line": 17, "source_line": "    raise ValueError(reason)",
        "code_sha256": "a" * 64, "binding_key": "body_geometry", "source_bundle_sha256": "b" * 64}
    failure = _failure_payload(_ChildError("REALIZATION_EXECUTION_FAILED", diagnostic["message"],
        "interface_evaluation", diagnostic=diagnostic))
    async def failed_precheck(self, requests, *, window_size=None):
        _raise_child_failure({**failure, "item_index": 0})
        yield
    monkeypatch.setattr(NumericRuntime, "precheck_batch", failed_precheck)
    result, tables, events, _, _ = await cheap_core(tmp_path, [record("a")])
    assert result.error_code == failure["code"] and result.summary.outcome == "technical_failure"
    assert result.summary.committed_count == 0 and "fail" in events
    check = tables["checks"][-1]
    assert check["phase"] == "interface_evaluation" and check["error_code"] == failure["code"]
    assert json.loads(check["diagnostic_json"]) == diagnostic
    assert all(row["root_global_id"] is None for row in tables["products"])


@pytest.mark.asyncio
async def test_committed_readback_failure_propagates_without_metadata_recover_or_failed_rewrite(tmp_path):
    from CAD2BIM_backend.domain.ifc_model import IfcCommittedVerificationError
    events=[]
    fault=IfcCommittedVerificationError("actual Writer inspection failed",operation_alias="op_original",revision_id="revision_original")
    with pytest.raises(IfcCommittedVerificationError) as caught:
        await cheap_core(tmp_path,[record("a")],resume_fault=fault,observed_events=events)
    assert caught.value is fault
    assert fault.operation_id == _fixture()[2].prepared.operation_id
    assert events==["recover","retire"]


@pytest.mark.asyncio
async def test_numeric_groups_finish_global_precheck_before_same_proposal_hydration(tmp_path):
    result,tables,events,inputs,runtime=await cheap_core(tmp_path,[record(f"p{i}") for i in range(1025)])
    assert [len(group) for group in runtime.groups]==[1024,1]
    assert events.index("precheck:1")<events.index("hydrate")<events.index("build")
    assert all(check is original for check,original in zip(inputs.hydrated,runtime.results))
    assert result.summary.solve_executed_count==1025 and result.summary.build_executed_count==0


@pytest.mark.asyncio
@pytest.mark.parametrize("technical,unknown,unexecuted",[("precise",1,1024),("unknown",1023,2)])
async def test_technical_first_error_preserves_prefix_unknown_and_unexecuted_without_frames(tmp_path,technical,unknown,unexecuted):
    result,tables,events,inputs,runtime=await cheap_core(tmp_path,[record(f"p{i}") for i in range(1026)],technical=technical)
    assert result.summary.outcome=="technical_failure"
    assert (result.summary.solve_executed_count,result.summary.solve_unconfirmed_count,result.summary.not_executed_count)==(1,unknown,unexecuted)
    assert len(runtime.groups)==1 and not inputs.hydrated
    assert {r["product_key"] for r in tables["components"]}=={"p0"}
    assert tables["interfaces"]==[]


@pytest.mark.asyncio
async def test_missing_body_stops_before_numeric_solver(tmp_path):
    result,tables,events,inputs,runtime=await cheap_core(tmp_path,[record("a")],missing_body=True)
    assert result.error_code=="IFC_REPRESENTATION_CONTEXT_NOT_FOUND"
    assert result.summary.outcome=="rejected"
    assert not runtime.groups and "closure" not in events and not inputs.hydrated
    assert result.summary.not_executed_count==1
    call_check=next(row for row in tables["checks"] if row["check_kind"]=="call_failure")
    assert call_check["phase"]=="body_preflight"
    assert "ifc_model_configure_geometry_basis" in call_check["recommended_action"]


def _recovery_service(original, *, permission_error=None, apply_error=None):
    data = SimpleNamespace(get_ifc_products_recovery_operation=AsyncMock(return_value=original))
    writer = SimpleNamespace(require_recovery_allowed=AsyncMock(side_effect=permission_error))
    service = IfcProductsApplyService(families=None, data=data, payloads=None, inputs=None,
        realization_runtime=None, revision_runtime=None, writer=writer, coordinator=None, storage=None)
    service.apply = AsyncMock(return_value=object(), side_effect=apply_error)
    return service, data, writer


@pytest.mark.asyncio
async def test_recover_uses_original_request_and_action_identity_after_write_authorization():
    from CAD2BIM_backend.domain.data_runtime import IfcProductsApplyOperationDetail
    from CAD2BIM_backend.domain.product_apply import ProductApplyInputBinding
    scope = _scope().model_copy(update={"catalog_frontier": None})
    command = ApplyIfcProductsCommand(family_version_id="family_original",
        package_id="package_original", payload_name="products")
    detail = IfcProductsApplyOperationDetail(request=command, input_binding=ProductApplyInputBinding(
        package_id=command.package_id, member_id="member_original", payload_name="products",
        logical_key_fields=("product_key",)))
    original = SimpleNamespace(operation=SimpleNamespace(tool_name="ifc_products_apply",
        detail=detail, idempotency_key="original-persisted-action"))
    service, data, writer = _recovery_service(original)
    result = await service.recover(scope, origin_run_id=scope.run_id, operation_id="operation_original")
    data.get_ifc_products_recovery_operation.assert_awaited_once_with(scope,
        origin_run_id=scope.run_id, operation_id="operation_original")
    writer.require_recovery_allowed.assert_awaited_once()
    service.apply.assert_awaited_once_with(scope, command, idempotency_key="original-persisted-action")
    assert result is service.apply.return_value


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_run,frontier", [(True, None), (False, 1)])
async def test_recover_rejects_cross_run_or_catalog_read_scope_before_lookup(wrong_run, frontier):
    from CAD2BIM_backend.application.product_apply_service import ProductApplyCallError
    scope = _scope().model_copy(update={"catalog_frontier": frontier})
    service, data, writer = _recovery_service(None)
    with pytest.raises(ProductApplyCallError) as caught:
        await service.recover(scope, origin_run_id="another_run" if wrong_run else scope.run_id,
            operation_id="operation_original")
    assert caught.value.code == "PRODUCT_APPLY_RECOVERY_SCOPE_MISMATCH"
    data.get_ifc_products_recovery_operation.assert_not_awaited()
    writer.require_recovery_allowed.assert_not_awaited()
    service.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_recover_cannot_continue_when_original_run_rejects_writes():
    from CAD2BIM_backend.domain.ifc_model import IfcModelConflictError
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection
    original = _apply_inspection("rejected").operation
    denied = IfcModelConflictError("Run no longer accepts IFC execution or commit")
    service, _, writer = _recovery_service(original, permission_error=denied)
    scope = _scope().model_copy(update={"catalog_frontier": None})
    with pytest.raises(IfcModelConflictError) as caught:
        await service.recover(scope, origin_run_id=scope.run_id, operation_id="operation_original")
    assert caught.value is denied and denied.operation_id == "operation_original"
    service.apply.assert_not_awaited()
    writer.require_recovery_allowed.assert_awaited_once()


@pytest.mark.asyncio
async def test_recover_preserves_committed_readback_failure_and_original_identity():
    from CAD2BIM_backend.domain.ifc_model import IfcCommittedVerificationError
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection
    original = _apply_inspection().operation
    failure = IfcCommittedVerificationError("Exact bytes cannot be verified",
        operation_alias="op_original", revision_id="revision_original")
    service, _, _ = _recovery_service(original, apply_error=failure)
    scope = _scope().model_copy(update={"catalog_frontier": None})
    with pytest.raises(IfcCommittedVerificationError) as caught:
        await service.recover(scope, origin_run_id=scope.run_id, operation_id="operation_original")
    assert caught.value is failure and failure.operation_id == "operation_original"
    assert (failure.operation_alias, failure.revision_id) == ("op_original", "revision_original")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["prepared", "indeterminate"])
async def test_operation_reference_reads_unpublished_apply_without_writes_and_checks_exact_input(status):
    from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError, DataRuntimeService
    from CAD2BIM_backend.domain.data_runtime import CriticalOperationErrorPhase, CriticalOperationStatus, IfcProductsRecoveryOperation
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection
    original = _apply_inspection("rejected")
    record = original.operation.operation.model_copy(update={"status": CriticalOperationStatus(status),
        "result_package_id": None, "finished_at": None if status == "prepared" else original.operation.operation.finished_at,
        "error_code": None if status == "prepared" else "PRODUCT_APPLY_COMMIT_UNCERTAIN",
        "error_message": None if status == "prepared" else "The original result is not yet determined",
        "error_phase": None if status == "prepared" else CriticalOperationErrorPhase.RECONCILE,
        "detail": original.operation.operation.detail.model_copy(update={"result": None})})
    view = IfcProductsRecoveryOperation(operation=record, created_at=record.started_at)
    source = original.input_packages["products"]
    repo = SimpleNamespace(get_operation_by_locator=AsyncMock(return_value=None),
        get_ifc_products_recovery_operation=AsyncMock(return_value=view), get_package=AsyncMock(return_value=source))
    service, scope = DataRuntimeService(repo, None), _scope()
    result = await service.get_operation_by_locator(scope, origin_run_id=scope.run_id,
        operation_id=record.operation_id)
    assert result.operation is view and result.operation.catalog_entry is None
    assert result.result_package is None and result.input_packages == {"products": source}
    repo.get_package.return_value = source.model_copy(update={"members": ()})
    with pytest.raises(DataRuntimeApplicationError, match="exact member"):
        await service.get_operation_by_locator(scope, origin_run_id=scope.run_id,
            operation_id=record.operation_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin_mode,reader_mode", [("direct_mcp", "graph"), ("graph", "direct_mcp")])
@pytest.mark.parametrize("recovery", [False, True])
async def test_historical_operation_inspection_uses_authoritative_origin_run_mode(origin_mode, reader_mode, recovery):
    from CAD2BIM_backend.application.data_runtime_service import DataRuntimeService
    from CAD2BIM_backend.domain.control_plane import ExecutionMode
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection

    original = _apply_inspection("rejected")
    record = original.operation.operation
    base = _scope()
    scope = base.model_copy(update={"owner_id": record.owner_id, "run_id": "later-run",
        "execution_mode": ExecutionMode(reader_mode), "identity": base.identity.model_copy(update={
            "tenant_id": record.tenant_id, "task_id": record.task_id})})
    source_run = SimpleNamespace(run_id=record.run_id, owner_id=record.owner_id,
        tenant_id=record.tenant_id, task_id=record.task_id, execution_mode=ExecutionMode(origin_mode))
    async def package(read_scope, package_id):
        assert read_scope.run_id == record.run_id
        assert read_scope.execution_mode is source_run.execution_mode
        assert read_scope.identity == scope.identity
        return (original.result_package if package_id == record.result_package_id
            else original.input_packages["products"])
    repository = SimpleNamespace(
        get_operation_by_locator=AsyncMock(return_value=None if recovery else original.operation),
        get_ifc_products_recovery_operation=AsyncMock(return_value=original.operation),
        get_run=AsyncMock(return_value=source_run), get_package=AsyncMock(side_effect=package))
    result = await DataRuntimeService(repository, None).get_operation_by_locator(scope,
        origin_run_id=record.run_id, operation_id=record.operation_id)
    assert result.operation.operation == record and result.result_package == original.result_package
    repository.get_run.assert_awaited_once_with(scope.owner_id, record.run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_historical_operation_origin_run_must_remain_in_authorized_task(missing):
    from CAD2BIM_backend.application.data_runtime_service import DataRuntimeService, DataRuntimeApplicationError
    from CAD2BIM_backend.domain.control_plane import ExecutionMode
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection

    original = _apply_inspection("rejected")
    record = original.operation.operation
    base = _scope()
    scope = base.model_copy(update={"owner_id": record.owner_id, "run_id": "later-run",
        "identity": base.identity.model_copy(update={"tenant_id": record.tenant_id, "task_id": record.task_id})})
    source_run = None if missing else SimpleNamespace(run_id=record.run_id, owner_id=record.owner_id,
        tenant_id=record.tenant_id, task_id="foreign-task", execution_mode=ExecutionMode.DIRECT_MCP)
    repository = SimpleNamespace(get_operation_by_locator=AsyncMock(return_value=original.operation),
        get_run=AsyncMock(return_value=source_run), get_package=AsyncMock())
    with pytest.raises(DataRuntimeApplicationError) as caught:
        await DataRuntimeService(repository, None).get_operation_by_locator(scope,
            origin_run_id=record.run_id, operation_id=record.operation_id)
    assert caught.value.code == "OPERATION_INTEGRITY_ERROR"
    repository.get_package.assert_not_awaited()


async def _copy_input_with_invalid_parameters(database, authority, original):
    """Create another exact input fixture; never overwrite accepted input bytes."""
    from uuid import uuid4
    from CAD2BIM_backend.domain.control_plane import ArtifactRecord
    from CAD2BIM_backend.domain.data_runtime import CatalogResourceKind
    from CAD2BIM_backend.ports.storage import StorageKey
    from CAD2BIM_backend.infrastructure.postgres.repository_artifact import register_artifact_in_transaction
    from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import append_catalog_entry, next_catalog_revision
    from tests.integration.backend.test_ifc_model_durable_kernel import _one_chunk
    package_id=f"data_package_{uuid4().hex}"; artifacts=[]; members=[]
    input_digest=None
    for member in original.members:
        content=await database.storage.read(StorageKey(authority.owner_id,authority.identity.task_id,member.artifact.artifact_id,1))
        if member.member_kind is PackageMemberKind.PAYLOAD:
            table=pq.read_table(io.BytesIO(content))
            index=table.schema.get_field_index("parameters_json")
            table=table.set_column(index,table.schema.field(index),pa.array(['{"undeclared":42}'],type=pa.string()))
            sink=pa.BufferOutputStream(); pq.write_table(table,sink);content=sink.getvalue().to_pybytes()
            input_digest=hashlib.sha256(content).hexdigest()
        elif member.member_kind is PackageMemberKind.MANIFEST:
            manifest=json.loads(content);manifest["package_id"]=package_id;content=json.dumps(manifest).encode()
        artifact_id,version_id=f"artifact_{uuid4().hex}",f"artifact_version_{uuid4().hex}"
        stored=await database.storage.write(StorageKey(authority.owner_id,authority.identity.task_id,artifact_id,1),
            filename=member.member_name.rsplit("/",1)[-1],content_type=member.media_type,chunks=_one_chunk(content))
        artifacts.append(ArtifactRecord(artifact_id=artifact_id,version_id=version_id,owner_id=authority.owner_id,
            task_id=authority.identity.task_id,run_id=authority.run_id,kind="data_input_fixture",filename=stored.filename,
            content_type=stored.content_type,size_bytes=stored.size_bytes,sha256=stored.sha256,
            idempotency_key=f"input-artifact:{artifact_id}",created_at=stored.created_at))
        members.append(member.model_copy(update={"member_id":f"member_{uuid4().hex}","package_id":package_id,
            "artifact":ArtifactVersionRef(artifact_id=artifact_id,version_id=version_id,sha256=stored.sha256)}))
    package=original.package.model_copy(update={"package_id":package_id,"idempotency_key":f"input:{package_id}",
                                                "name":"Normal rejection input", "request_fingerprint":input_digest})
    async with database.database.session_factory() as session,session.begin():
        for artifact in artifacts:
            await register_artifact_in_transaction(session,artifact,tenant_id=authority.identity.tenant_id,
                request_value=artifact.model_dump(mode="json"),lineage={})
        session.add(database.repository._package_model(package));await session.flush()
        session.add_all(database.repository._member_model(member) for member in members);await session.flush()
        await append_catalog_entry(session,owner_id=authority.owner_id,tenant_id=authority.identity.tenant_id,
            task_id=authority.identity.task_id,run_id=authority.run_id,kind=CatalogResourceKind.PACKAGE,
            catalog_revision=await next_catalog_revision(session,authority.run_id),package_id=package_id)
    return package_id


@pytest.mark.integration
@pytest.mark.network
@pytest.mark.asyncio
async def test_real_core_precheck_hydrate_build_native_ifc_commit_and_data_readback(tmp_path):
    """Trusted exact Family fixture, real numerical/Geometry/IFC Workers and DB."""
    import cadquery as cq
    import ifcopenshell
    from CAD2BIM_backend.domain.invocation import InvocationScope
    from CAD2BIM_backend.domain.control_plane import ExecutionMode
    from CAD2BIM_backend.domain.data_query import DataRowsQuery, DataQueryRowsRequest
    from CAD2BIM_backend.application.artifact_service import ArtifactService
    from CAD2BIM_backend.application.exact_payload_resolver import ExactPayloadResolver
    from CAD2BIM_backend.application.data_query_service import DataQueryService
    from CAD2BIM_backend.infrastructure.data_query import CursorCodec
    from CAD2BIM_backend.application.ifc_revision_reader import IfcRevisionReader
    from CAD2BIM_backend.application.ifc_revision_writer import IfcRevisionWriter
    from CAD2BIM_backend.application.ifc_geometry_basis_service import IfcGeometryBasisService
    from CAD2BIM_backend.infrastructure.ifc_revision_runtime.adapter import IfcOpenShellRevisionRuntime
    from CAD2BIM_backend.infrastructure.product_realization_runtime import OcpCadQueryProductRealizationRuntime
    from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
    from CAD2BIM_backend.domain.data_runtime import CriticalOperationErrorPhase
    from tests.integration.backend.test_ifc_model_durable_kernel import _product_transaction_fixture, _storage_key
    from tests.unit.backend.test_ifc_geometry_basis_compiler import _local_basis_command
    from tests.unit.backend.test_product_realization_input_service import _published_loader
    from tests.unit.backend.test_product_family_authoring_service import _geometry
    from tests.unit.backend.product_family_fixtures import current_model_interface
    import base64

    evidence=Path(os.environ.get("CAD2BIM_TEST_PRODUCT_CORE_EVIDENCE_DIR",str(tmp_path/"core-evidence")))
    evidence.mkdir(parents=True,exist_ok=True)
    class Observations(logging.Handler):
        def emit(self, log):
            item=getattr(log,"cad2bim_execution_phase",None)
            if item is not None:
                with (evidence/"observations.jsonl").open("a") as output:output.write(json.dumps(item)+"\n")
    logger=logging.getLogger("CAD2BIM.execution");old_level=logger.level;observations=Observations()
    logger.setLevel(logging.INFO);logger.addHandler(observations)

    database, context, ifc_scope, old_stack, old_detail, abandoned, claim = await _product_transaction_fixture(tmp_path)
    authority = InvocationScope(identity=context, owner_id=ifc_scope.owner_id, run_id=ifc_scope.run_id,
        execution_mode=ExecutionMode.DIRECT_MCP, catalog_frontier=None)
    raw=io.BytesIO(); cq.Workplane().box(1,2,3,centered=(False,False,False)).val().exportBrep(raw)
    # Exact fixture manifest describes this actual 1x2x3 box (area22, volume6).
    inputs, _, family, _, _, _ = await _published_loader(_geometry(content=raw.getvalue(),interfaces=(current_model_interface(),)))
    class Families:
        async def get_product_family_version_by_id(self, identity, *, family_version_id):
            assert identity==context and family_version_id==family.version.family_version_id
            return family
    actual=IfcOpenShellRevisionRuntime(capacity_root=tmp_path/"worker-capacity")
    realizations=OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path/"worker-capacity")
    reader=IfcRevisionReader(database.repository,database.storage,actual)
    writer=IfcRevisionWriter(database.repository,database.storage,reader,actual)
    artifacts=ArtifactService(database.repository,database.storage)
    payloads=ExactPayloadResolver(database.repository,artifacts)
    service=IfcProductsApplyService(families=Families(),data=database.repository,payloads=payloads,inputs=inputs,
        realization_runtime=realizations,revision_runtime=actual,writer=writer,
        coordinator=old_stack.coordinator,storage=database.storage)
    query=DataQueryService(database.repository,artifacts,CursorCodec(base64.urlsafe_b64encode(b"product-core-test-cursor-key-32!!").decode()))
    try:
        with bind_tenant_execution_context(context):
            await database.repository.record_failed_ifc_operation(ifc_scope,operation_id=abandoned.operation_id,
                error_code="UNUSED_FIXTURE_PREPARE",error_message="Input fixture preparation is not the Core operation",
                error_phase=CriticalOperationErrorPhase.IFC_VALIDATION,claim=claim)
            await database.repository.retire_execution(ifc_scope,claim)
            await IfcGeometryBasisService(writer,actual,old_stack.coordinator).configure(ifc_scope,_local_basis_command(prefix="MILLI"),idempotency_key="core-body")
            command=ApplyIfcProductsCommand(family_version_id=family.version.family_version_id,
                package_id=old_detail.request.package_id,payload_name="input")
            result=await service.apply(authority,command,idempotency_key="native-core-add")
            assert result.summary is not None and result.summary.outcome=="committed"
            assert (result.summary.input_count,result.summary.solve_executed_count,result.summary.interface_constraints_evaluated_product_count,
                    result.summary.build_executed_count,result.summary.built_count,result.summary.committed_count)==(1,1,1,1,1,1)
            current=await database.repository.get_current(ifc_scope)
            saved=ifcopenshell.file.from_string((await database.storage.read(_storage_key(ifc_scope,current.artifact.artifact_id))).decode())
            (evidence/"candidate.ifc").write_bytes(await database.storage.read(_storage_key(ifc_scope,current.artifact.artifact_id)))
            assert len(saved.by_type("IfcValve"))==1 and len(saved.by_type("IfcRepresentationMap"))==1
            payload_rows={}
            for name,select in (("products",("product_key","root_global_id","commit_status")),
                                ("components",("component_key","global_id","commit_status")),
                                ("interfaces",("interface_key","commit_status")),("checks",("check_kind","status"))):
                output=await query.query_rows(authority,DataQueryRowsRequest(query=DataRowsQuery(
                    package_alias=result.report_package_alias,payload_name=name,select=select)))
                payload_rows[name]=output.rows
            assert len(payload_rows["products"])==1 and len(payload_rows["components"])==1
            guid=saved.by_type("IfcValve")[0].GlobalId
            assert payload_rows["products"][0].values["root_global_id"]==guid
            assert payload_rows["components"][0].values["global_id"]==guid
            before_files=set(database.storage._root.rglob("payload"))
            replay=await service.apply(authority,command,idempotency_key="native-core-add")
            assert replay==result and set(database.storage._root.rglob("payload"))==before_files
            original=await database.repository.get_package(authority,command.package_id)
            rejected_package=await _copy_input_with_invalid_parameters(database,authority,original)
            rejected=await service.apply(authority,command.model_copy(update={"package_id":rejected_package}),
                                         idempotency_key="native-core-normal-rejection")
            assert rejected.summary.outcome=="rejected" and rejected.summary.committed_count==0
            assert rejected.summary.solve_executed_count==0 and rejected.summary.build_executed_count==0
            assert (await database.repository.get_current(ifc_scope)).revision_id==current.revision_id
            failure_rows={}
            for name,select in (("products",("product_key","root_global_id","commit_status")),
                                ("components",("component_key","global_id","commit_status")),
                                ("interfaces",("interface_key","commit_status")),("checks",("check_kind","status"))):
                output=await query.query_rows(authority,DataQueryRowsRequest(query=DataRowsQuery(
                    package_alias=rejected.report_package_alias,payload_name=name,select=select)))
                if name=="products":assert len(output.rows)==1
                failure_rows[name]=output.rows
            assert failure_rows["products"][0].values["root_global_id"] is None
            (evidence/"summary.json").write_text(json.dumps({"success":result.model_dump(mode="json"),
                "failure":rejected.model_dump(mode="json"),"saved_global_id":guid,
                "success_data_rows":{name:[row.model_dump(mode="json") for row in values] for name,values in payload_rows.items()},
                "failure_data_rows":{name:[row.model_dump(mode="json") for row in values] for name,values in failure_rows.items()}},indent=2))
    finally:
        await realizations.close(); await actual.close(); await database.close()
        logger.removeHandler(observations);logger.setLevel(old_level)


def test_checked_source_spool_survives_memory_eviction_without_duplicate_files(tmp_path):
    from CAD2BIM_backend.application.product_batch_geometry import ProductGeometrySpool
    from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry, CanonicalGeometryFormat
    from CAD2BIM_backend.domain.ifc_geometry_construction import IfcGeometryRepresentationRoute
    spool = ProductGeometrySpool(tmp_path)
    sources = []
    for index in range(129):
        payload = f"independently checked source {index}".encode()
        geometry = CanonicalGeometry(format=CanonicalGeometryFormat.OCCT_BREP_V1,
            content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
            payload=payload, sha256=hashlib.sha256(payload).hexdigest())
        spool.put(str(index), geometry, None, IfcGeometryRepresentationRoute.TFS, {})
        sources.append(geometry)
    assert "0" not in spool._memory
    assert spool.get("0") == (sources[0], None, {})
    original_bytes = spool.byte_count
    spool.put("other-exact-request", sources[0], None, IfcGeometryRepresentationRoute.TFS, {})
    assert spool.byte_count == original_bytes
    assert len(list(tmp_path.glob("*.brep"))) == 129
    assert spool.get("unknown") is None
    spool.store(sources[0], None, IfcGeometryRepresentationRoute.PARAMETRIC)
    assert spool.byte_count == original_bytes == spool.spool_size_bytes
    staged = tmp_path / "construction-raw.json"
    assert spool.reserve_spool_file(staged, 4)
    staged.write_bytes(b"raw!")
    assert not spool.reserve_spool_file(staged, 4)
    assert spool.byte_count == sum(path.stat().st_size for path in tmp_path.iterdir())


@pytest.mark.asyncio
async def test_checked_source_reuse_keeps_current_product_receipts_without_worker(tmp_path, monkeypatch):
    from CAD2BIM_backend.application.product_batch_geometry import ProductGeometrySpool
    from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry, CanonicalGeometryFormat
    from CAD2BIM_backend.domain.ifc_geometry_construction import IfcGeometryRepresentationRoute
    from CAD2BIM_backend.domain.product_realization import ResolvedMappedGeometryBinding, ProductRealizationInvocation
    from CAD2BIM_backend.infrastructure.product_realization_runtime.adapter import OcpCadQueryProductRealizationRuntime, _geometry_source_request_key
    payload = b"previously codec-checked immutable source"
    geometry = CanonicalGeometry(format=CanonicalGeometryFormat.OCCT_BREP_V1,
        content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
        payload=payload, sha256=hashlib.sha256(payload).hexdigest())
    binding = ResolvedMappedGeometryBinding(binding_key="body_geometry", geometry_content_digest="a"*64, geometry=geometry)
    requests = tuple(_request(binding, invocation=ProductRealizationInvocation(product_origin=(x, 0., 0.),
        length_unit={"name": "METRE", "prefix": None})) for x in (0., 2.))
    prechecks = tuple(numeric_result(request) for request in requests)
    spool = ProductGeometrySpool(tmp_path)
    evaluation = prechecks[0].interface_evaluations[0]
    key = _geometry_source_request_key(binding, evaluation)
    spool.put(key, geometry, None, IfcGeometryRepresentationRoute.TFS, {})
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    async def forbidden(*args, **kwargs):
        raise AssertionError("a fully checked source must not restart a Worker")
        yield
    monkeypatch.setattr(runtime, "realize_batch", forbidden)
    values = [value async for value in runtime.instantiate_batch(requests, prechecks, source_cache=spool)]
    assert [value.realization.invocation.product_origin for value in values] == [(0., 0., 0.), (2., 0., 0.)]
    for value, precheck in zip(values, prechecks, strict=True):
        assert value.realization.interface_evaluations == precheck.interface_evaluations
        assert value.realization.instance_receipts[0].geometry_sha256 == geometry.sha256
        assert value.instance_geometries["body_shape"] == geometry
    with pytest.raises(ValueError):
        _ = [value async for value in runtime.instantiate_batch(requests, prechecks,
            output_need=ProductRealizationOutputNeed.PREVIEW_WORLD, source_cache=spool)]
    await runtime.close()


def test_geometry_request_key_preserves_inputs_and_ignores_binding_alias():
    from CAD2BIM_backend.infrastructure.product_realization_runtime.adapter import _geometry_source_request_key
    from CAD2BIM_backend.domain.ifc_geometry_construction import IfcGeometryRepresentationRoute
    binding = SimpleNamespace(resource_kind=SimpleNamespace(value="modeling_function"), geometry_content_digest="a"*64,
        ifc_representation_route=IfcGeometryRepresentationRoute.TFS, source_bundle_sha256="b"*64,
        parameter_schema={"type": "object"}, geometry_inputs=(), binding_key="first", source_reuse_mode="exact")
    evaluation = SimpleNamespace(normalized_parameters={"length": 1, "radius": 2}, uniform_scale=1.)
    key = _geometry_source_request_key(binding, evaluation)
    binding.binding_key = "other"
    assert _geometry_source_request_key(binding, evaluation) == key
    for parameters, scale in (({"radius": 2, "length": 1}, 1.), ({"length": 1., "radius": 2}, 1.), ({"length": 1, "radius": 2}, 2.)):
        assert _geometry_source_request_key(binding, SimpleNamespace(normalized_parameters=parameters, uniform_scale=scale)) != key
    binding.geometry_inputs = (SimpleNamespace(input_key="source", sha256="c"*64),)
    assert _geometry_source_request_key(binding, evaluation) != key


@pytest.mark.asyncio
async def test_serial_second_window_failure_preserves_global_source_row(tmp_path, monkeypatch):
    import CAD2BIM_backend.application.product_apply_service as core
    monkeypatch.setattr(core, "_NUMERIC_GROUP_ITEMS", 2)
    monkeypatch.setattr(core, "_GEOMETRY_GROUP_ITEMS", 2)
    async def fail_second_window(self, requests, prechecks, **kwargs):
        raise ProductRealizationRuntimeUnavailableError("second window failed before Codec released results",
            item_index=3, retryable=False)
        yield
    monkeypatch.setattr(NumericRuntime, "instantiate_batch", fail_second_window)
    result, tables, _, _, _ = await cheap_core(tmp_path, [record(f"p{index}") for index in range(5)])
    assert result.summary.built_count == 0
    assert result.summary.build_unconfirmed_count == 4
    assert [row["build_status"] for row in tables["products"]] == ["UNCONFIRMED"] * 4 + ["NOT_EXECUTED"]
    failure = next(row for row in tables["checks"] if row["check_kind"] == "call_failure")
    assert failure["input_row_index"] == 3 and failure["product_key"] == "p3"


def test_serial_windows_reject_unbounded_or_inconsistent_counts(monkeypatch, tmp_path):
    from CAD2BIM_backend.infrastructure.product_realization_runtime import protocol
    assert protocol.product_window_sizes(2050, 1024) == (1024, 1024, 2)
    for count, window in ((0, 2), (2, 0), (2, 1025), (2, True), (1025, None)):
        with pytest.raises(ValueError):
            protocol.product_window_sizes(count, window)
    monkeypatch.setattr(protocol, "restore_request", lambda *args: "prepared")
    base = {"request": {}, "item_count": 4, "entry_names": ["realize"]}
    for sizes in ([], [3], [1025], [2, True], [2, 0, 2]):
        with pytest.raises(ValueError):
            protocol.restore_batch_header(json.dumps({**base, "window_sizes": sizes}).encode(), b"", tmp_path)
    assert protocol.restore_batch_header(json.dumps({**base, "window_sizes": [2, 2]}).encode(), b"", tmp_path)[0] == "prepared"

@pytest.mark.asyncio
async def test_pending_sources_require_unique_codec_results_and_survive_129_sources(tmp_path):
    from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry, CanonicalGeometryFormat
    from CAD2BIM_backend.domain.product_realization import ResolvedMappedGeometryBinding, ProductGeometryInstanceReceipt, complete_product_realization_instantiation
    from CAD2BIM_backend.infrastructure.product_realization_runtime.adapter import OcpCadQueryProductRealizationRuntime, _PendingGeometrySource, _source_metadata
    geometries, records = {}, []
    first = None
    for index in range(130):
        # The final ordinary record is a second fragment occurrence of source 0.
        payload = f"checked-by-codec-source-{index if index < 129 else 0}".encode()
        geometry = CanonicalGeometry(format=CanonicalGeometryFormat.OCCT_BREP_V1,
            content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
            payload=payload, sha256=hashlib.sha256(payload).hexdigest())
        geometries[geometry.sha256] = geometry
        binding = ResolvedMappedGeometryBinding(binding_key="body_geometry", geometry_content_digest="a"*64, geometry=geometry)
        precheck = numeric_result(_request(binding))
        receipt = ProductGeometryInstanceReceipt.from_evaluation(precheck.interface_evaluations[0], geometry_sha256=geometry.sha256)
        result = complete_product_realization_instantiation(precheck, (receipt,))
        path = tmp_path / f"source-{index}.brep"
        path.write_bytes(payload)
        records.append((result, [("body_shape", _source_metadata(geometry), path)], [], ProductRealizationOutputNeed.LOCAL_FRAGMENTS))
        if index == 0:
            first = (result, geometry)
    final_result = first[0].model_copy()
    pending = _PendingGeometrySource(_source_metadata(first[1]), (id(first[0]), "body_shape"), {})
    records.append((final_result, [], [], ProductRealizationOutputNeed.LOCAL_FRAGMENTS))
    class Codec:
        count = 0
        async def normalize_batch(self, requests):
            self.count += len(requests)
            return tuple((geometries[hashlib.sha256(request.payload).hexdigest()], None) for request in requests)
    codec = Codec()
    runtime = OcpCadQueryProductRealizationRuntime(codec_executor=codec, capacity_root=tmp_path / "capacity")
    values = [value async for value in runtime._normalize_batch_records(records,
        reused_by_result={id(final_result): {"body_shape": (pending, None)}})]
    assert codec.count == 129
    assert len(values) == 131
    assert values[-1].instance_geometries["body_shape"] == first[1]
    assert not list(tmp_path.glob("*.brep"))
    from CAD2BIM_backend.ports.product_realization_runtime import ProductRealizationRuntimeValidationError
    with pytest.raises(ProductRealizationRuntimeValidationError) as failure:
        await runtime._normalize_record_group([records[-1]], start_index=0,
            reused_by_result={id(final_result): {"body_shape": (pending, None)}},
            checked_reused_sources={})
    assert failure.value.diagnostic["reason"] == "reused source has no earlier independent Codec result"
    assert codec.count == 129
    await runtime.close()


@pytest.mark.asyncio
async def test_numeric_window_boundary_cancellation_keeps_cleanup(tmp_path):
    import asyncio
    from contextlib import suppress
    from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry, CanonicalGeometryFormat
    from CAD2BIM_backend.domain.product_realization import ResolvedMappedGeometryBinding
    from CAD2BIM_backend.infrastructure.product_realization_runtime.adapter import OcpCadQueryProductRealizationRuntime
    from CAD2BIM_backend.infrastructure.worker_runtime.protocol import AsyncWorkerChannel, encode_json_frame
    payload = b"fixed source unused by Numeric"
    geometry = CanonicalGeometry(format=CanonicalGeometryFormat.OCCT_BREP_V1,
        content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
        payload=payload, sha256=hashlib.sha256(payload).hexdigest())
    binding = ResolvedMappedGeometryBinding(binding_key="body_geometry", geometry_content_digest="a"*64, geometry=geometry)
    requests = (_request(binding), _request(binding))
    class Supervisor:
        boundary = False
        scratch = None
        async def run(self, spec, *, interaction, **kwargs):
            self.scratch = spec.scratch
            read_fd, write_fd = os.dup(spec.pass_fds[-2]), os.dup(spec.pass_fds[-1])
            peer = AsyncWorkerChannel(read_fd, write_fd)
            owner = asyncio.current_task()
            async def exchange():
                await peer.write(encode_json_frame({"status": "ready"}, limit=1024))
                frame = await peer.read_json(limit=1024*1024)
                assert frame["item_index"] == 0
                await peer.write(encode_json_frame({"status": "succeeded", "item_index": 0,
                    "output_need": "interfaces", "result": numeric_result(requests[0]).model_dump(mode="json"),
                    "geometries": [], "constructions": []}, limit=2*1024*1024))
                assert await peer.read_json(limit=1024) == {"window_end": 1}
                self.boundary = True
                owner.cancel()
            task = asyncio.create_task(exchange())
            try:
                await interaction(SimpleNamespace(remaining_cpu_seconds=10000, invocation=lambda **kwargs: nullcontext()))
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                os.close(read_fd); os.close(write_fd)
    supervisor = Supervisor()
    runtime = OcpCadQueryProductRealizationRuntime(supervisor=supervisor, capacity_root=tmp_path / "capacity")
    async def consume():
        return [value async for value in runtime.precheck_batch(requests, window_size=1)]
    task = asyncio.create_task(consume())
    with pytest.raises(asyncio.CancelledError):
        await task
    assert supervisor.boundary and not supervisor.scratch.exists()
    await runtime.close()
