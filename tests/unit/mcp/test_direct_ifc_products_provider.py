from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.application.product_apply_service import ProductApplyCallError
from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError
from CAD2BIM_backend.domain.ifc_model import IfcCommittedVerificationError
from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.ifc.products.application import execute_products_apply, products_apply_error
from CAD2BIM_mcp.direct.ifc.products.contracts import ProductsApplyInput, DirectProductsApplyInput
from CAD2BIM_mcp.direct.operation.application import list_ifc_products_recovery
from CAD2BIM_mcp.direct.operation.inputs import DirectOperationListRequest
from CAD2BIM_mcp.direct.server import create_direct_mcp


def _nodes(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _nodes(item)


@pytest.mark.parametrize("by_reference", [False, True])
async def test_original_operation_read_failure_keeps_known_status_and_origin(by_reference):
    from contextlib import nullcontext
    from CAD2BIM_mcp.direct.application import DirectApplicationFacade
    from CAD2BIM_mcp.direct.transform.common_inputs import OperationReferenceSelection

    error = DataRuntimeApplicationError("OPERATION_INTEGRITY_ERROR", "Original saved report is unavailable",
        operation_alias="op_001")
    error.operation_id = "original-operation"
    error.operation_status = "succeeded"
    application = SimpleNamespace(get_operation=AsyncMock(side_effect=error),
        get_operation_by_locator=AsyncMock(side_effect=error))
    scope = SimpleNamespace(identity=object(), run_id="current-run")
    references = SimpleNamespace(operation_locator=Mock(return_value=("original-run", "original-operation")),
        operation_reference=Mock(return_value="cur.original"))
    facade = DirectApplicationFacade(application, Mock(),
        SimpleNamespace(resolve=AsyncMock(return_value=scope)), lambda _: nullcontext(),
        reference_service=references)
    selection = (OperationReferenceSelection(mode="operation_reference", direct_session_alias="direct_001",
        operation_ref="cur.original") if by_reference else None)
    with pytest.raises(DirectApplicationError) as caught:
        await facade.get_operation("direct_001", "op_001", selection=selection)
    assert caught.value.operation_status == "succeeded"
    assert caught.value.operation_ref == "cur.original" and caught.value.operation_alias == "op_001"
    references.operation_reference.assert_called_once_with(scope.identity,
        origin_run_id="original-run" if by_reference else "current-run", operation_id="original-operation")
    (application.get_operation if by_reference else application.get_operation_by_locator).assert_not_awaited()


def _edit_diagnostics_query_runtime():
    """A real nested Data query failure through the original inspection owner."""
    from CAD2BIM_backend.application.data_runtime_service import DataRuntimeService
    from CAD2BIM_backend.application.data_query_service import DataQueryApplicationError
    from CAD2BIM_backend.domain.identity import TenantExecutionContext, TenantActorKind
    inspection = _edit_inspection()
    record = inspection.operation.operation
    identity = TenantExecutionContext(tenant_id=record.tenant_id, principal_id="person", task_id=record.task_id,
        actor_kind=TenantActorKind.RUNNER, command_id="read-failure-action")
    scope = SimpleNamespace(owner_id=record.owner_id, identity=identity, run_id=record.run_id)
    failure = DataQueryApplicationError("PAYLOAD_INTEGRITY_ERROR", "The exact original checks asset is unavailable")
    failure.diagnostic = {"payload_name": "checks", "reason": "Exact immutable member could not be read"}
    queries = SimpleNamespace(query_rows=AsyncMock(side_effect=failure))
    repository = SimpleNamespace(get_package=AsyncMock(side_effect=lambda scope, package_id:
        inspection.result_package if package_id == record.result_package_id else inspection.input_packages["products"]))
    exact = inspection.committed_ifc_revision
    ifc = SimpleNamespace(get_exact=AsyncMock(side_effect=lambda scope, revision_id:
        exact if revision_id == exact.revision_id else exact.model_copy(update={"revision_id": revision_id, "revision_no": 3})))
    runtime = DataRuntimeService(repository, None, queries=queries, ifc_repository=ifc)

    async def inspect(*_args, **_kwargs):
        return await runtime._inspect_operation(scope, inspection.operation)
    return inspection, scope, failure, queries, inspect


@pytest.mark.parametrize("entry", ["first", "recover", "alias", "reference"])
async def test_edit_actual_query_failure_keeps_public_read_diagnostic_and_original_state(entry):
    from contextlib import nullcontext
    from CAD2BIM_mcp.direct.application import DirectApplicationFacade
    from CAD2BIM_mcp.direct.ifc.products.application import execute_products_edit
    from CAD2BIM_mcp.direct.ifc.products.contracts import ProductsEditApplyRequest, ProductsRecoverRequest
    from CAD2BIM_mcp.direct.transform.common_inputs import OperationReferenceSelection
    from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService
    from tests.unit.backend.test_opaque_reference_service import MemoryReferenceIndex, reference_codec
    inspection, scope, failure, queries, inspect = _edit_diagnostics_query_runtime()
    record = inspection.operation.operation
    refs = OpaqueReferenceService(MemoryReferenceIndex(), reference_codec())
    reference = refs.operation_reference(scope.identity, origin_run_id=scope.run_id, operation_id=record.operation_id)
    data = SimpleNamespace(get_operation=AsyncMock(side_effect=inspect), get_operation_by_locator=AsyncMock(side_effect=inspect),
        get_ifc_products_recovery_operation=AsyncMock(side_effect=inspect),
        resolve_package=AsyncMock(return_value=inspection.input_packages["products"]))
    result = SimpleNamespace(operation_id=record.operation_id, operation_alias=inspection.operation.catalog_entry.alias, outcome="succeeded")
    core = SimpleNamespace(edit=AsyncMock(return_value=result), recover=AsyncMock(side_effect=inspect))
    with pytest.raises(DirectApplicationError) as caught:
        if entry in {"first", "recover"}:
            request = (ProductsEditApplyRequest(mode="apply", package_alias="data_001", payload_name="products")
                if entry == "first" else ProductsRecoverRequest(mode="recover", operation_ref=reference))
            await execute_products_edit(scope, request, request_identity="transport", core=core, data_runtime=data, references=refs)
        else:
            facade = DirectApplicationFacade(data, Mock(), SimpleNamespace(resolve=AsyncMock(return_value=scope)),
                lambda _: nullcontext(), reference_service=refs)
            selection = (OperationReferenceSelection(mode="operation_reference", direct_session_alias="direct_001",
                operation_ref=reference) if entry == "reference" else None)
            await facade.get_operation("direct_001", inspection.operation.catalog_entry.alias, selection=selection)
    error = caught.value
    assert (error.code, error.phase, error.operation_status) == (failure.code, "read", "succeeded")
    assert error.diagnostic == failure.diagnostic and error.operation_ref == reference
    assert error.operation_alias == inspection.operation.catalog_entry.alias
    assert '"payload_name":"checks"' in str(_tool_error(error))
    queries.query_rows.assert_awaited_once()


def _edit_inspection(outcome="succeeded", *, with_report=True):
    from dataclasses import replace
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection
    from CAD2BIM_backend.domain.data_runtime import (
        CriticalOperationRecord, CriticalOperationStatus, IfcProductsEditOperationDetail,
        IfcProductsRecoveryOperation, PublishedCriticalOperation, IFC_PRODUCTS_EDIT_INTENT_SUMMARY,
    )
    from CAD2BIM_backend.domain.product_edit import EditIfcProductsCommand, ProductsEditSummary
    source = _apply_inspection()
    original = source.operation.operation
    terminal = outcome in {"succeeded", "failed"}
    reported = terminal and with_report
    summary = ProductsEditSummary(input_count=3, committed_count=3 if outcome == "succeeded" else 0,
        committed_by_action={name: 1 if outcome == "succeeded" else 0 for name in ("replace", "placement", "delete")},
        created_product_count=10 if outcome == "succeeded" else 0,
        deleted_product_count=11 if outcome == "succeeded" else 0) if reported else None
    detail = IfcProductsEditOperationDetail(request=EditIfcProductsCommand(
        package_id=original.detail.input_binding.package_id, payload_name="products",
        base_revision_id=original.base_ifc_revision_id), input_binding=original.detail.input_binding, result=summary)
    fields = original.model_dump()
    fields.update(tool_name="ifc_products_edit", intent_summary=IFC_PRODUCTS_EDIT_INTENT_SUMMARY,
        status=outcome, detail=detail, finished_at=None if outcome == "prepared" else original.finished_at,
        result_package_id=original.result_package_id if reported else None,
        result_ifc_revision_id=original.result_ifc_revision_id if outcome == "succeeded" else None,
        error_code="PRODUCT_EDIT_REJECTED" if outcome == "failed" else "IFC_COMMIT_UNCERTAIN" if outcome == "indeterminate" else None,
        error_message="An external connection blocks this edit" if outcome == "failed" else "Original commit needs reconciliation" if outcome == "indeterminate" else None,
        error_phase="ifc_validation" if outcome == "failed" else "reconcile" if outcome == "indeterminate" else None)
    record = CriticalOperationRecord.model_validate(fields)
    published = (PublishedCriticalOperation(operation=record, catalog_entry=source.operation.catalog_entry) if terminal
        else IfcProductsRecoveryOperation(operation=record, created_at=record.started_at))
    return replace(source, operation=published, result_package=source.result_package if reported else None,
        committed_ifc_revision=source.committed_ifc_revision if outcome == "succeeded" else None,
        base_revision_no=3, diagnostics=None)


@pytest.mark.parametrize("profile", ["direct", "runner"])
@pytest.mark.parametrize("outcome,with_report", [("succeeded", True), ("failed", True), ("failed", False), ("prepared", False), ("indeterminate", False)])
async def test_edit_actual_sdk_schema_and_same_saved_three_branch_projection(monkeypatch, profile, outcome, with_report):
    from fastmcp import Client, FastMCP
    from CAD2BIM_mcp.direct.operation.application import project_ifc_products_edit_operation
    from CAD2BIM_mcp.runner import ifc_tools, data_tools
    inspection = _edit_inspection(outcome, with_report=with_report)
    projected = project_ifc_products_edit_operation(inspection, operation_ref="cur.original-edit")
    if profile == "direct":
        application = SimpleNamespace(edit_ifc_products=AsyncMock(return_value=projected), get_operation=AsyncMock(return_value=projected))
        server = create_direct_mcp(application)
        request = {"direct_session_alias": "direct_001", "mode": "apply", "package_alias": "data_001", "payload_name": "edits"}
        recover = {"direct_session_alias": "direct_001", "mode": "recover", "operation_ref": "cur.original-edit"}
        read = {"direct_session_alias": "direct_001", "mode": "operation_reference", "operation_ref": "cur.original-edit"}
    else:
        class Backend:
            async def call_ifc_domain(self, **kwargs):
                assert kwargs["endpoint"] == "/internal/domain/ifc/products/edit"
                assert "direct_session_alias" not in kwargs["body"]
                return projected.model_dump(mode="json")
            async def call_data_tool(self, **kwargs):
                assert kwargs["tool_name"] == "operation_get"
                return {"edit_result": projected.model_dump(mode="json")}
        monkeypatch.setattr(ifc_tools, "request_domain_credential", lambda: "token")
        monkeypatch.setattr(data_tools, "request_domain_credential", lambda: "token")
        server = FastMCP("Product edit shared result")
        ifc_tools.register_runner_ifc_tools(server, Backend())
        data_tools.register_runner_data_tools(server, Backend())
        request = {"mode": "apply", "package_alias": "data_001", "payload_name": "edits"}
        recover = {"mode": "recover", "operation_ref": "cur.original-edit"}
        read = {"request": {"mode": "operation_reference", "operation_ref": "cur.original-edit"}}
    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        edit = tools["ifc_products_edit"]
        assert edit.annotations.destructiveHint is True
        for schema in (edit.inputSchema, edit.outputSchema):
            Draft202012Validator.check_schema(schema)
            for node in _nodes(schema):
                assert not {"allOf", "if", "then", "else", "not"} & node.keys()
                assert node.get("type") != "unknown"
                if node.get("type") == "object" and "properties" in node:
                    assert node.get("additionalProperties") is False
            assert not {"model_id", "revision_id", "claim", "package_id", "operation_id", "idempotency_key"} & {
                key for node in _nodes(schema) for key in node.get("properties", {})}
        validator = Draft202012Validator(edit.inputSchema)
        for values in (request, recover):
            validator.validate(values)
        for invalid in ({**request, "operation_ref": "cur.other"}, {**recover, "payload_name": "changed"},
                {**recover, "base_revision_ref": "cur.new-base"}):
            assert list(validator.iter_errors(invalid))
        for name, values in (("ifc_products_edit", request), ("ifc_products_edit", recover), ("operation_get", read)):
            payload = (await client.call_tool(name, values)).structured_content
            Draft202012Validator(tools[name].outputSchema).validate(payload)
            assert payload == projected.model_dump(mode="json")
            if terminal_outcome(outcome) and with_report:
                assert [table["payload_name"] for table in payload["operation"]["primary_result"]["report_tables"]] == [
                    member.logical_payload_name for member in inspection.result_package.members]
        resources = await client.list_resources()
        assert any(str(resource.uri) == "cad2bim://contracts/ifc_products_edit/input" for resource in resources)
    payload = projected.model_dump(mode="json")
    if not terminal_outcome(outcome):
        assert "error" not in payload and "primary_result" not in payload["operation"]
        assert payload["operation"].get("operation_alias") is None and "sequence" not in payload["operation"]
    elif outcome == "failed":
        assert "committed_revision" not in projected.model_dump_json()
        assert (payload["operation"]["primary_result"] is not None) == with_report
    else:
        assert payload["operation"]["primary_result"]["committed_revision"] == {"revision_no": 4, "size_bytes": 987654}


def terminal_outcome(outcome):
    return outcome in {"succeeded", "failed"}


@pytest.mark.parametrize("outcome,with_report", [("succeeded", True), ("failed", True), ("failed", False), ("prepared", False), ("indeterminate", False)])
def test_edit_generic_operation_query_dispatch_keeps_the_original_tool_identity(outcome, with_report):
    from CAD2BIM_mcp.direct.operation.application import (
        _operation_get_result, project_ifc_products_edit_operation, project_ifc_apply_operation,
    )
    inspection = _edit_inspection(outcome, with_report=with_report)
    expected = project_ifc_products_edit_operation(inspection, operation_ref="cur.edit")
    assert _operation_get_result(inspection, operation_ref="cur.edit") == expected
    with pytest.raises(ValueError, match="not a product Apply"):
        project_ifc_apply_operation(inspection, family_version_ref="cur.family", operation_ref="cur.edit")


def test_model_info_projects_only_the_same_exact_read_basis_and_excludes_internal_fact():
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection
    from tests.unit.backend.test_opaque_reference_service import MemoryReferenceIndex, reference_codec
    from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService
    from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
    from CAD2BIM_backend.domain.ifc_model import IfcModelInfo
    from CAD2BIM_mcp.direct.ifc.model.application import _ifc_model_info_result
    exact = _apply_inspection().committed_ifc_revision
    context = TenantExecutionContext(tenant_id=exact.scope.tenant_id, task_id=exact.scope.task_id,
        principal_id="reader", actor_kind=TenantActorKind.USER, command_id="read")
    references = OpaqueReferenceService(MemoryReferenceIndex(), reference_codec())
    info = IfcModelInfo(project={"global_id": "0" * 22, "name": "Current project"},
        size_bytes=exact.artifact.size_bytes, source_exact=exact)
    assert "source_exact" not in info.model_dump()
    public = _ifc_model_info_result(info, references=references, identity=context)
    assert public.source_revision.revision_no == exact.revision_no
    assert references.ifc_revision_locator(context, public.source_revision.revision_ref) == (
        exact.scope.run_id, exact.model_id, exact.revision_id, exact.revision_no)
    for private in ("source_exact", "model_id", "revision_id", "artifact_id", "run_id", "sha256"):
        assert private not in public.model_dump_json()


@pytest.mark.parametrize("corruption", ["missing_report", "missing_revision", "different_revision"])
def test_edit_projection_never_substitutes_missing_original_success_facts(corruption):
    from dataclasses import replace
    from CAD2BIM_mcp.direct.operation.application import project_ifc_products_edit_operation
    inspection = _edit_inspection()
    exact = inspection.committed_ifc_revision
    broken = (replace(inspection, result_package=None) if corruption == "missing_report" else
        replace(inspection, committed_ifc_revision=None if corruption == "missing_revision" else
            exact.model_copy(update={"revision_id": "newer-current", "revision_no": 99})))
    with pytest.raises(ValueError):
        project_ifc_products_edit_operation(broken, operation_ref="cur.original-edit")


@pytest.mark.parametrize("mode", ["apply", "recover"])
async def test_edit_adapter_reuses_exact_original_identity_and_preserves_commit_on_read_failure(mode):
    from CAD2BIM_mcp.direct.ifc.products.application import execute_products_edit
    from CAD2BIM_mcp.direct.ifc.products.contracts import ProductsEditInput
    from CAD2BIM_backend.domain.product_edit import ProductsEditResult
    inspection = _edit_inspection()
    operation = inspection.operation.operation
    outcome = ProductsEditResult(operation_id=operation.operation_id, operation_alias="op_006",
        outcome="succeeded", summary=operation.detail.result, report_package_alias="data_002",
        ifc_revision_id=operation.result_ifc_revision_id)
    core = SimpleNamespace(edit=AsyncMock(return_value=outcome), recover=AsyncMock(return_value=outcome))
    data = SimpleNamespace(resolve_package=AsyncMock(return_value=inspection.input_packages["products"]),
        get_ifc_products_recovery_operation=AsyncMock(side_effect=DataRuntimeApplicationError(
            "OPERATION_INTEGRITY_ERROR", "Original report read failed")))
    refs = SimpleNamespace(operation_reference=Mock(return_value="cur.original-edit"),
        operation_locator=Mock(return_value=(operation.run_id, operation.operation_id)),
        ifc_revision_locator=Mock(return_value=(operation.run_id, "same-model", operation.base_ifc_revision_id, 3)))
    scope = SimpleNamespace(identity=object(), run_id=operation.run_id)
    request = TypeAdapter(ProductsEditInput).validate_python(
        {"mode": "apply", "package_alias": "data_001", "payload_name": "products", "base_revision_ref": "cur.read-basis"}
        if mode == "apply" else {"mode": "recover", "operation_ref": "cur.original-edit"})
    with pytest.raises(DirectApplicationError) as caught:
        await execute_products_edit(scope, request, request_identity="new-transport-identity", core=core,
            data_runtime=data, references=refs)
    assert caught.value.operation_status == "succeeded" and caught.value.operation_ref == "cur.original-edit"
    assert caught.value.operation_alias == "op_006"
    if mode == "apply":
        command = core.edit.await_args.args[1]
        assert command.package_id == operation.detail.input_binding.package_id
        assert command.base_revision_id == operation.base_ifc_revision_id
        assert core.edit.await_args.kwargs == {"idempotency_key": "new-transport-identity"}
        core.recover.assert_not_awaited()
    else:
        core.recover.assert_awaited_once_with(scope, origin_run_id=operation.run_id, operation_id=operation.operation_id)
        core.edit.assert_not_awaited()
        data.resolve_package.assert_not_awaited()
        refs.ifc_revision_locator.assert_not_called()


def test_edit_diagnostics_use_existing_byte_budget_without_fabricating_check_coverage():
    from dataclasses import replace
    from CAD2BIM_backend.domain.product_report import ProductReportDiagnostic, ProductReportDiagnostics
    from CAD2BIM_mcp.direct.operation.application import project_ifc_products_edit_operation
    inspection = _edit_inspection("failed")
    warning = ProductReportDiagnostic(phase="precheck", severity="warning", message="w" * 800,
        recommended_action="Review preserved metadata")
    error = ProductReportDiagnostic(phase="precheck", severity="error", message="e" * 800,
        recommended_action="Remove external blocker")
    result = project_ifc_products_edit_operation(replace(inspection,
        diagnostics=ProductReportDiagnostics(items=(warning,) * 120 + (error,) * 120, complete=True)),
        operation_ref="cur.original-edit")
    assert len(result.model_dump_json().encode()) <= 64 * 1024
    assert result.detail.diagnostics.complete is False
    assert result.detail.diagnostics.items[0].severity == "error"
    assert result.operation.primary_result.summary.input_count == 3


async def test_public_apply_schema_is_closed_narrow_and_has_real_input_guide():
    server = create_direct_mcp(Mock())
    tools = {tool.name: tool for tool in await server.list_tools()}
    tool = tools["ifc_products_apply"]
    for schema in (tool.parameters, tool.output_schema):
        assert schema is not None
        for node in _nodes(schema):
            assert not {"allOf", "if", "then", "else", "not"} & node.keys()
            assert node.get("type") != "unknown"
        properties = {key for node in _nodes(schema) for key in node.get("properties", {})}
        assert not properties & {"idempotency_key", "family_version_id", "package_id", "run_id", "revision_id", "claim"}
    output = str(tool.output_schema)
    assert all(field in output for field in ("committed_revision", "revision_no", "size_bytes"))
    assert "interface_constraints_evaluated_product_count" in output
    assert "interface_checked_count" not in output
    assert "source_ingestion" not in output and "authoring_draft_write" not in output
    validator = Draft202012Validator(tool.parameters)
    valid = {"direct_session_alias": "direct_001", "mode": "apply", "family_version_alias": "cur.family",
        "package_alias": "data_001", "payload_name": "products"}
    assert not list(validator.iter_errors(valid))
    assert not list(validator.iter_errors({"direct_session_alias": "direct_001", "mode": "recover", "operation_ref": "cur.original"}))
    assert list(validator.iter_errors({**valid, "operation_ref": "cur.original"}))
    assert list(validator.iter_errors({"direct_session_alias": "direct_001", "mode": "recover", "operation_ref": "cur.original", "payload_name": "changed"}))
    assert any(str(resource.uri) == "cad2bim://contracts/ifc_products_apply/input" for resource in await server.list_resources())


async def test_product_read_provider_closes_views_groups_and_identity():
    from CAD2BIM_mcp.direct.ifc.products.contracts import DirectProductsReadInput
    server = create_direct_mcp(Mock())
    tool = await server.get_tool("ifc_products_read")
    adapter = TypeAdapter(DirectProductsReadInput)
    validator = Draft202012Validator(tool.parameters)
    applied = {"direct_session_alias": "direct_001", "mode": "from_apply",
        "operation_ref": "cur.original", "product_keys": ["product-a"]}
    current = {**applied, "view": "current", "groups": {"properties": {"mode": "names"}}}
    from_ifc = {"direct_session_alias": "direct_001", "mode": "from_ifc",
        "global_ids": ["0000000000000000000000"], "groups": {"materials": {"mode": "values",
            "selectors": [{"source": "instance", "set_name": "Actual set", "member_names": ["Actual material"]}]}}}
    for value in (applied, current, from_ifc):
        adapter.validate_python(value)
        validator.validate(value)
    assert adapter.validate_python(applied).view == "applied"
    for value in ({**applied, "groups": {}}, {**applied, "global_ids": from_ifc["global_ids"]},
        {**current, "product_keys": ["a", "a"]}, {**applied, "product_keys": []},
        {**from_ifc, "view": "applied"}, {**from_ifc, "product_keys": ["a"]},
        {**from_ifc, "groups": {"materials": {"mode": "values", "member_step_ids": [1]}}},
        {**from_ifc, "global_ids": ["bad"]}, {**applied, "product_keys": [str(i) for i in range(33)]}):
        with pytest.raises(ValidationError):
            adapter.validate_python(value)
    for schema in (tool.parameters, tool.output_schema):
        for node in _nodes(schema):
            assert not {"allOf", "if", "then", "else", "not"} & node.keys()
            assert node.get("type") != "unknown"
        public_fields = {key for node in _nodes(schema) for key in node.get("properties", {})}
        assert not public_fields & {"package_id", "run_id", "revision_id", "operation_id", "member_id", "budget"}
    assert tool.annotations.readOnlyHint is True
    assert any(str(resource.uri) == "cad2bim://contracts/ifc_products_read/usage" for resource in await server.list_resources())
    unit_fields = next(node["properties"] for node in _nodes(tool.output_schema)
        if {"ifc_class", "name", "symbol"} <= node.get("properties", {}).keys())
    value_fields = next(node["properties"] for node in _nodes(tool.output_schema)
        if {"scalar", "values", "unit"} <= node.get("properties", {}).keys())
    assert all(unit_fields[name].get("description") for name in ("name", "symbol"))
    assert all(value_fields[name].get("description") for name in ("scalar", "values"))


@pytest.mark.parametrize("input_alias,report_alias", [("data_007", None), (None, "data_009"), (None, None)])
async def test_product_read_projects_current_aliases_independently_without_historical_alias(input_alias, report_alias):
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection
    from CAD2BIM_backend.domain.ifc_products_read import IfcProductsReadFacts, IfcProductsReadItem, IfcApplyProductMatch
    from CAD2BIM_mcp.direct.ifc.products.contracts import ProductsReadInput
    from CAD2BIM_mcp.direct.ifc.products.application import execute_products_read
    inspection = _apply_inspection()
    facts = IfcProductsReadFacts(view="applied", items=(IfcProductsReadItem(product_key="product-a",
        apply_match=IfcApplyProductMatch(status="matched", report_match_count=1)),))
    core = SimpleNamespace(read=AsyncMock(return_value=SimpleNamespace(
        facts=facts, operation=inspection, input_alias=input_alias, report_alias=report_alias)))
    references = SimpleNamespace(operation_locator=Mock(return_value=("original-run", "original-operation")))
    family = SimpleNamespace(version_ref_for_visible_id=AsyncMock(return_value="cur.family"))
    scope = SimpleNamespace(identity=object(), run_id="different-run")
    request = TypeAdapter(ProductsReadInput).validate_python({"mode": "from_apply", "operation_ref": "cur.original", "product_keys": ["product-a"]})
    result = await execute_products_read(scope, request, core=core, family_catalog=family, references=references)
    assert result.operation.input.details.model_dump() == ({"detail_access": "available", "package_alias": input_alias}
        if input_alias else {"detail_access": "not_in_current_catalog"})
    assert result.operation.report.details.model_dump() == ({"detail_access": "available", "package_alias": report_alias}
        if report_alias else {"detail_access": "not_in_current_catalog"})
    assert result.operation.operation_ref == "cur.original"
    assert result.operation.committed_revision.revision_no == 4
    assert "data_001" not in result.model_dump_json() and "data_002" not in result.model_dump_json()
    assert core.read.await_args.kwargs == {"operation_locator": ("original-run", "original-operation")}
    assert "operation_ref" not in core.read.await_args.args[1].model_dump()


async def test_product_read_error_retains_reference_without_inventing_write_status():
    from CAD2BIM_backend.domain.ifc_products_read import IfcProductsReadError
    from CAD2BIM_mcp.direct.ifc.products.application import products_read_error
    error = products_read_error(IfcProductsReadError("IFC_PRODUCTS_READ_MODEL_MISMATCH", "Different model"), operation_ref="cur.original")
    assert error.operation_ref == "cur.original" and error.operation_status is None
    assert error.code == "IFC_PRODUCTS_READ_MODEL_MISMATCH" and error.phase == "read"


async def test_product_read_usage_examples_are_complete_current_requests():
    from CAD2BIM_mcp.tool_docs.ifc_products import READ_GUIDE
    tool = await create_direct_mcp(Mock()).get_tool("ifc_products_read")
    validator = Draft202012Validator(tool.parameters)
    for value in READ_GUIDE["examples"]:
        validator.validate({"direct_session_alias": "direct_001", **value})
    validator.validate(READ_GUIDE["direct_example"])


async def test_product_read_public_navigation_total_budget_returns_actionable_read_limit():
    from tests.unit.backend.test_ifc_model_operation_projection import _apply_inspection
    from CAD2BIM_backend.domain.ifc_products_read import (
        IfcProductsReadFacts, IfcProductsReadItem, IfcApplyProductMatch, MAX_READ_RESPONSE_BYTES,
    )
    from CAD2BIM_mcp.direct.application import DirectApplicationFacade
    from CAD2BIM_mcp.direct.ifc.products.contracts import DirectProductsReadInput, IfcProductsReadResult

    # A legal original key can consume almost all of the response budget. No
    # semantic field is invalid; only the added public navigation crosses it.
    empty = IfcProductsReadFacts(view="applied", items=(IfcProductsReadItem(product_key="",
        apply_match=IfcApplyProductMatch(status="not_found", report_match_count=0)),))
    key = "k" * (MAX_READ_RESPONSE_BYTES - len(empty.model_dump_json().encode("utf-8")) - 128)
    facts = IfcProductsReadFacts(view="applied", items=(IfcProductsReadItem(product_key=key,
        apply_match=IfcApplyProductMatch(status="not_found", report_match_count=0)),))
    without_navigation = IfcProductsReadResult(**facts.model_dump())
    assert len(facts.model_dump_json().encode("utf-8")) < MAX_READ_RESPONSE_BYTES
    assert len(without_navigation.model_dump_json().encode("utf-8")) < MAX_READ_RESPONSE_BYTES
    core = SimpleNamespace(read=AsyncMock(return_value=SimpleNamespace(facts=facts,
        operation=_apply_inspection(), input_alias="data_007", report_alias="data_009")))
    scope = SimpleNamespace(identity=object(), run_id="calling-run")
    app = DirectApplicationFacade(None, None, SimpleNamespace(resolve=AsyncMock(return_value=scope)),
        products_read_service=core,
        product_family_catalog_service=SimpleNamespace(version_ref_for_visible_id=AsyncMock(return_value="cur.family")),
        reference_service=SimpleNamespace(operation_locator=Mock(return_value=("original-run", "original-operation"))))
    request = TypeAdapter(DirectProductsReadInput).validate_python({"direct_session_alias": "direct_001",
        "mode": "from_apply", "operation_ref": "cur.original", "product_keys": [key]})
    with pytest.raises(DirectApplicationError) as caught:
        await app.read_ifc_products(request)
    error = caught.value
    assert error.code == "IFC_PRODUCTS_READ_LIMIT_EXCEEDED"
    assert error.phase == "read" and error.operation_ref == "cur.original"
    assert error.operation_status is None
    assert "ifc_query_python" in str(error) or "ifc_query_python" in str(error.diagnostic)
    assert "narrow" in str(error).lower() or "smaller" in str(error).lower() or "reduce" in str(error).lower()
    core.read.assert_awaited_once()


def test_recovery_discovery_does_not_accept_catalog_or_write_fields():
    adapter = TypeAdapter(DirectOperationListRequest)
    request = {"direct_session_alias": "direct_001", "mode": "ifc_products_recovery", "tool_name": "ifc_products_apply", "page_size": 2}
    adapter.validate_python(request)
    for field, value in {"before_sequence": 2, "tool_name": "ifc_trusted_baseline_load", "package_alias": "data_001"}.items():
        with pytest.raises(ValidationError):
            adapter.validate_python({**request, field: value})
    # Existing terminal catalog discovery still permits an omitted mode.
    adapter.validate_python({"direct_session_alias": "direct_001", "tool_name": "ifc_products_apply"})


async def test_recover_uses_only_original_locator_and_never_new_transport_key(monkeypatch):
    request = TypeAdapter(ProductsApplyInput).validate_python({"mode": "recover", "operation_ref": "cur.original"})
    record = SimpleNamespace(run_id="original-run", operation_id="original-operation",
        detail=SimpleNamespace(request=SimpleNamespace(family_version_id="exact-family")))
    inspection = SimpleNamespace(operation=SimpleNamespace(operation=record))
    core = SimpleNamespace(apply=AsyncMock(), recover=AsyncMock(return_value=SimpleNamespace(operation_alias="op_001")))
    data = SimpleNamespace(get_ifc_products_recovery_operation=AsyncMock(return_value=inspection))
    refs = SimpleNamespace(operation_locator=Mock(return_value=("original-run", "original-operation")),
        operation_reference=Mock(return_value="cur.original"))
    family = SimpleNamespace(version_ref_for_visible_id=AsyncMock(return_value="cur.family"))
    scope = SimpleNamespace(identity=object(), run_id="original-run")
    marker = object()
    monkeypatch.setattr("CAD2BIM_mcp.direct.ifc.products.application.project_ifc_apply_operation", lambda *a, **k: marker)
    assert await execute_products_apply(scope, request, request_identity="new-transport-key", core=core,
        data_runtime=data, family_catalog=family, references=refs) is marker
    core.recover.assert_awaited_once_with(scope, origin_run_id="original-run", operation_id="original-operation")
    core.apply.assert_not_awaited()


async def test_apply_resolves_exact_public_inputs_and_preserves_original_reference_on_unknown_failure():
    request = TypeAdapter(ProductsApplyInput).validate_python({"mode": "apply", "family_version_alias": "cur.family",
        "package_alias": "data_001", "payload_name": "products"})
    scope = SimpleNamespace(identity=object(), run_id="original-run")
    accepted_error = ProductApplyCallError("IFC_SETTLEMENT_UNKNOWN", "Original action requires reconciliation", phase="reconcile")
    accepted_error.operation_id = "original-operation"
    core = SimpleNamespace(apply=AsyncMock(side_effect=accepted_error), recover=AsyncMock())
    data = SimpleNamespace(resolve_package=AsyncMock(return_value=SimpleNamespace(
        package=SimpleNamespace(package_id="exact-package"))))
    family = SimpleNamespace(resolve_exact_version=AsyncMock(return_value=SimpleNamespace(
        version=SimpleNamespace(family_version_id="exact-family"))))
    refs = SimpleNamespace(operation_reference=Mock(return_value="cur.original"))
    with pytest.raises(DirectApplicationError) as error:
        await execute_products_apply(scope, request, request_identity="server-action", core=core,
            data_runtime=data, family_catalog=family, references=refs)
    family.resolve_exact_version.assert_awaited_once_with(scope.identity, "cur.family", verify_assets=False)
    data.resolve_package.assert_awaited_once_with(scope, "data_001")
    args, kwargs = core.apply.await_args
    assert args[0] is scope and args[1].model_dump() == {
        "family_version_id": "exact-family", "package_id": "exact-package", "payload_name": "products"}
    assert kwargs == {"idempotency_key": "server-action"}
    assert error.value.operation_ref == "cur.original" and error.value.operation_status == "unknown"
    assert error.value.code == "IFC_SETTLEMENT_UNKNOWN"
    core.recover.assert_not_awaited()


def test_guide_condition_example_uses_current_shared_condition_contract():
    from CAD2BIM_backend.domain.product_realization import ProductRealizationExternalConstraint
    from CAD2BIM_mcp.tool_docs.ifc_products import INPUT_GUIDE
    for condition in INPUT_GUIDE["external_conditions"]["example"]:
        ProductRealizationExternalConstraint.model_validate(condition)


@pytest.mark.parametrize("revision_id, status", [("committed-revision", "succeeded"), (None, "failed")])
async def test_settled_apply_metadata_read_failure_preserves_original_action(revision_id, status):
    request = TypeAdapter(ProductsApplyInput).validate_python({"mode": "apply", "family_version_alias": "cur.family",
        "package_alias": "data_001", "payload_name": "products"})
    scope = SimpleNamespace(identity=object(), run_id="original-run")
    result = SimpleNamespace(operation_id="original-operation", operation_alias="op_001", ifc_revision_id=revision_id)
    core = SimpleNamespace(apply=AsyncMock(return_value=result), recover=AsyncMock())
    data = SimpleNamespace(resolve_package=AsyncMock(return_value=SimpleNamespace(
        package=SimpleNamespace(package_id="exact-package"))),
        get_operation=AsyncMock(side_effect=DataRuntimeApplicationError(
            "OPERATION_INTEGRITY_ERROR", "The original committed IFC metadata is unavailable")))
    family = SimpleNamespace(resolve_exact_version=AsyncMock(return_value=SimpleNamespace(
        version=SimpleNamespace(family_version_id="exact-family"))))
    refs = SimpleNamespace(operation_reference=Mock(return_value="cur.original"))
    with pytest.raises(DirectApplicationError) as caught:
        await execute_products_apply(scope, request, request_identity="server-action", core=core,
            data_runtime=data, family_catalog=family, references=refs)
    assert caught.value.code == "OPERATION_INTEGRITY_ERROR"
    assert caught.value.operation_ref == "cur.original"
    assert caught.value.operation_alias == "op_001"
    assert caught.value.operation_status == status
    assert caught.value.phase == "read"
    refs.operation_reference.assert_called_once_with(scope.identity,
        origin_run_id="original-run", operation_id="original-operation")
    core.apply.assert_awaited_once()
    core.recover.assert_not_awaited()


async def test_unsettled_discovery_has_original_reference_and_keyset_cursor_without_catalog():
    now = datetime(2026, 10, 14, tzinfo=UTC)
    records = tuple(SimpleNamespace(created_at=now, catalog_entry=None,
        operation=SimpleNamespace(run_id="run", tool_name="ifc_products_apply", operation_id=f"op-id-{i}", status=SimpleNamespace(value="prepared"),
            started_at=now, finished_at=None, error_code=None)) for i in range(3))
    data = SimpleNamespace(list_ifc_products_recovery_operations=AsyncMock(return_value=records))
    refs = SimpleNamespace(operation_reference=lambda identity, **kw: "cur." + kw["operation_id"],
        ifc_products_recovery_cursor=Mock(return_value="cur.next"))
    scope = SimpleNamespace(identity=object(), run_id="run")
    result = await list_ifc_products_recovery(scope, data_runtime=data, references=refs, tool_name="ifc_products_apply", page_size=2)
    assert result.complete is False and result.next_cursor == "cur.next"
    assert [item.operation_ref for item in result.operations] == ["cur.op-id-0", "cur.op-id-1"]
    assert all(item.operation_alias is None and item.finished_at is None for item in result.operations)
    assert "sequence" not in result.model_dump_json()
    refs.ifc_products_recovery_cursor.assert_called_once_with(scope, tool_name="ifc_products_apply", created_at=now, operation_id="op-id-1")


def test_input_and_committed_readback_errors_preserve_distinct_facts():
    invalid = products_apply_error(ProductApplyCallError("PRODUCT_INPUT_FORMAT_INVALID", "Requires Parquet"))
    assert invalid.code == "PRODUCT_INPUT_FORMAT_INVALID" and invalid.phase == "preflight"
    assert invalid.operation_alias is None and invalid.operation_status == "unknown"
    readback = products_apply_error(IfcCommittedVerificationError("Exact bytes unavailable", operation_alias="op_003",
        revision_id="private-revision"), operation_ref="cur.original")
    rendered = str(_tool_error(readback))
    assert readback.code == "IFC_COMMITTED_READBACK_FAILED"
    assert "operation_status=succeeded" in rendered and "operation_ref=cur.original" in rendered
    assert "private-revision" not in rendered
