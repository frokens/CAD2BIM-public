"""Edit's existing Writer transaction and replay kernel, without Family execution."""
from __future__ import annotations

import hashlib
import io
from uuid import uuid4

import pyarrow.parquet as pq
import pytest
from alembic import command
from sqlalchemy import text

from CAD2BIM_backend.application.product_edit_report import empty_edit_report, stage_product_edit_report, terminal_edit_rows
from CAD2BIM_backend.domain.data_runtime import IfcProductsEditOperationDetail, IFC_PRODUCTS_EDIT_INTENT_SUMMARY
from CAD2BIM_backend.domain.ifc_model import IfcModelConflictError
from CAD2BIM_backend.domain.product_edit import EditIfcProductsCommand, ProductsEditActionCounts, ProductsEditSummary
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from CAD2BIM_backend.ports.ifc_revision_runtime import GeneratedIfcRevision
from tests.integration.backend import test_ifc_model_durable_kernel as kernel
from tests.integration.backend.test_data_query_attempt_postgres import _isolated_database
from tests.process_support.ifc_revision_runtime import BytesIfcCandidate, build_in_process_ifc_stack

pytestmark = [pytest.mark.integration, pytest.mark.network]


async def _edit_fixture(tmp_path):
    runtime, context, scope, stack, apply_detail, initial, initial_claim = await kernel._product_transaction_fixture(tmp_path)
    with bind_tenant_execution_context(context):
        # The existing trusted transaction fixture supplies a real saved product
        # before Edit; this candidate-only test does not stand in for Core builds.
        generated, guid, report, failure = await kernel._product_transaction_candidate(
            runtime, scope, stack, apply_detail, initial)
        await stack.writer.commit(scope, initial, generated, claim=initial_claim,
            product_report=report, product_failure_report=failure)
        await runtime.repository.retire_execution(scope, initial_claim)
        await generated.candidate.aclose()
        base = await runtime.repository.get_current(scope)
        detail = IfcProductsEditOperationDetail(request=EditIfcProductsCommand(
            package_id=apply_detail.request.package_id, payload_name="input", base_revision_id=base.revision_id),
            input_binding=apply_detail.input_binding)
        digest = hashlib.sha256(detail.model_dump_json().encode()).hexdigest()
        prepared = await runtime.repository.prepare_revision(scope, tool_name="ifc_products_edit",
            idempotency_key=f"edit-transaction:{uuid4().hex}", request_digest=digest,
            intent_summary=IFC_PRODUCTS_EDIT_INTENT_SUMMARY, operation_detail=detail.persisted_payload())
        claim = await runtime.repository.claim_execution(scope, operation_id=prepared.operation_id, request_digest=digest)
        content = await runtime.storage.read(kernel._storage_key(scope, prepared.before.artifact.artifact_id))
        private = stack.codec.private_copy(content)
        private.model.remove(private.model.by_guid(guid))
        content = private.to_bytes()
        inspection = stack.codec.inspect(content)
        generated = GeneratedIfcRevision(candidate=BytesIfcCandidate(content, sha256=inspection.sha256), inspection=inspection)
        rows = empty_edit_report()
        rows["products"] = [{"input_row_index": 0, "product_key": "transaction-product", "target_global_id": guid,
            "action": "delete", "precheck_status": "PASSED", "solve_status": "NOT_APPLICABLE", "execution_status": "PASSED",
            "commit_status": "NOT_COMMITTED", "before_ifc_class": "IfcPipeSegment"}]
        rows["changes"] = [{"change_index": 0, "input_row_index": 0, "change_kind": "deleted", "global_id": guid,
            "ifc_class": "IfcPipeSegment"}]
        reports = []
        for committed in (True, False):
            summary = ProductsEditSummary(input_count=1, committed_count=int(committed),
                committed_by_action=ProductsEditActionCounts(replace=0, placement=0, delete=int(committed)),
                created_product_count=0, deleted_product_count=int(committed))
            reports.append(await stage_product_edit_report(runtime.storage, scope, prepared, detail=detail,
                summary=summary, rows=terminal_edit_rows(rows, committed=committed), committed=committed))
    return runtime, context, scope, stack, detail, prepared, claim, generated, guid, *reports


async def _visible(runtime, context, scope):
    with bind_tenant_execution_context(context):
        async with runtime.database.session_factory() as session:
            return dict((await session.execute(text("SELECT "
                "(SELECT current_revision_id FROM ifc_models WHERE run_id=:run) AS current, "
                "(SELECT count(*) FROM ifc_model_revisions WHERE run_id=:run) AS revisions, "
                "(SELECT count(*) FROM artifacts WHERE run_id=:run AND kind='product_edit_report') AS reports, "
                "(SELECT count(*) FROM data_packages WHERE created_by_run_id=:run) AS packages"),
                {"run": scope.run_id})).mappings().one())


async def _read_report(runtime, context, scope, package_id):
    from CAD2BIM_backend.domain.control_plane import ExecutionMode
    from CAD2BIM_backend.domain.invocation import InvocationScope
    with bind_tenant_execution_context(context):
        authority = InvocationScope(identity=context, owner_id=scope.owner_id, run_id=scope.run_id,
            execution_mode=ExecutionMode.DIRECT_MCP, catalog_frontier=None)
        report = await runtime.repository.get_package(authority, package_id)
        assert report is not None
        assert {m.logical_payload_name for m in report.members if m.logical_payload_name} == {"products", "changes", "checks"}
        changes = next(m for m in report.members if m.logical_payload_name == "changes")
        content = await runtime.storage.read(kernel._storage_key(scope, changes.artifact.artifact_id))
        return report, pq.read_table(io.BytesIO(content)).to_pylist()


@pytest.mark.asyncio
async def test_edit_joint_publication_restart_replay_cas_and_registry_rollback(tmp_path, monkeypatch):
    # One isolated migration supplies all three unique transaction risks.
    with _isolated_database("product_edit") as (config, admin, scoped_url):
        command.upgrade(config, "head")
        monkeypatch.setattr(kernel, "TEST_DATABASE_URL", scoped_url)
        for mode in ("restart", "cas", "registry"):
            runtime, context, scope, stack, detail, prepared, claim, generated, guid, report, failure = await _edit_fixture(tmp_path / mode)
            observer = kernel._create_runtime(kernel._runtime_database_url(scoped_url), tmp_path / mode / "observer")
            try:
                before = await _visible(observer, context, scope)
                with bind_tenant_execution_context(context):
                    selected, staged = await stack.writer._stage_and_select(scope, prepared, claim, generated,
                        product_report=report, product_failure_report=failure)
                    if mode == "restart":
                        await runtime.repository.retire_execution(scope, claim)
                        await generated.candidate.aclose()
                        await runtime.close()
                        runtime = kernel._create_runtime(kernel._runtime_database_url(scoped_url), tmp_path / mode / "storage")
                        stack = build_in_process_ifc_stack(runtime.repository, runtime.storage)
                        original = runtime.repository.commit_revision
                        calls = []
                        async def lose_response(*args, **kwargs):
                            result = await original(*args, **kwargs)
                            calls.append(True)
                            if len(calls) == 1:
                                raise RuntimeError("committed Edit response lost")
                            return result
                        monkeypatch.setattr(runtime.repository, "commit_revision", lose_response)
                        claim = await runtime.repository.claim_execution(scope, operation_id=prepared.operation_id,
                            request_digest=prepared.request_digest)
                        committed, recovered = await stack.writer.resume_selected_revision(scope, prepared, claim)
                        assert recovered == selected and committed.exact.revision_id == prepared.after_revision_id
                    else:
                        publication = await stack.writer._publish_selected(scope, prepared, selected, staged=staged)
                        if mode == "cas":
                            from CAD2BIM_backend.application.ifc_project_service import IfcProjectService
                            await runtime.repository.retire_execution(scope, claim)
                            await IfcProjectService(stack.writer, stack.runtime, stack.coordinator).update(scope,
                                kernel.UpdateIfcProjectCommand(patch=kernel.IfcProjectPatch(name="Edit CAS winner")),
                                idempotency_key=f"winner:{uuid4().hex}")
                            claim = await runtime.repository.claim_execution(scope, operation_id=prepared.operation_id,
                                request_digest=prepared.request_digest)
                            with pytest.raises(IfcModelConflictError):
                                await runtime.repository.commit_revision(scope, prepared=prepared, publication=publication, claim=claim)
                        else:
                            original = runtime.repository._register_product_report
                            async def registration_fault(session, owner_scope, operation, candidate_report):
                                await original(session, owner_scope, operation, candidate_report)
                                assert await _visible(observer, context, scope) == before
                                raise RuntimeError("Edit registry failure after actual inserts")
                            monkeypatch.setattr(runtime.repository, "_register_product_report", registration_fault)
                            with pytest.raises(RuntimeError, match="Edit registry failure"):
                                await runtime.repository.commit_revision(scope, prepared=prepared, publication=publication, claim=claim)
                            assert await _visible(observer, context, scope) == before
                            assert await runtime.repository.get_exact(scope, revision_id=prepared.after_revision_id) is None
                            continue
                    state = await stack.writer.recover_revision(scope, tool_name="ifc_products_edit",
                        idempotency_key=prepared.idempotency_key, request_digest=prepared.request_digest,
                        intent_summary=IFC_PRODUCTS_EDIT_INTENT_SUMMARY, operation_detail=detail.persisted_payload())
                    expected = report if mode == "restart" else failure
                    assert state.report_package_id == expected.package.package_id and state.product_summary == expected.summary
                    assert state.status.value == ("succeeded" if mode == "restart" else "failed")
                    discovered, changes = await _read_report(runtime, context, scope, state.report_package_id)
                    if mode == "restart":
                        assert len(changes) == 1 and changes[0]["global_id"] == guid
                    else:
                        assert changes == []
                        assert state.error_code == "IFC_CURRENT_CONFLICT"
                        assert await runtime.repository.get_exact(scope, revision_id=prepared.after_revision_id) is None
                    visible = await _visible(observer, context, scope)
                    assert visible["packages"] == before["packages"] + 1
                    assert visible["reports"] == len(expected.artifacts)
                    replay = await stack.writer.recover_revision(scope, tool_name="ifc_products_edit",
                        idempotency_key=prepared.idempotency_key, request_digest=prepared.request_digest,
                        intent_summary=IFC_PRODUCTS_EDIT_INTENT_SUMMARY, operation_detail=detail.persisted_payload())
                    assert replay == state
                    assert await _visible(observer, context, scope) == visible
            finally:
                with bind_tenant_execution_context(context):
                    await runtime.repository.retire_execution(scope, claim)
                await generated.candidate.aclose()
                await observer.close()
                await runtime.close()
