"""Public draft lifecycle contracts without native execution or a database."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import FastMCP
from pydantic import TypeAdapter, ValidationError
from jsonschema import Draft202012Validator

from CAD2BIM_mcp.direct.ifc.geometry.authoring_application import IfcGeometryAuthoringApplicationMixin
from CAD2BIM_mcp.direct.ifc.geometry.authoring_contracts import validate_geometry_resource_write_input
from CAD2BIM_mcp.direct.ifc.geometry.authoring_tools import register_ifc_geometry_resource_write
from CAD2BIM_mcp.direct.ifc.geometry.contracts import validate_geometry_preview_input
from CAD2BIM_mcp.direct.ifc.geometry.tools import register_ifc_geometry_preview, register_ifc_geometry_query_python
from CAD2BIM_mcp.direct.ifc.product_family.write_contracts import validate_product_family_write_input
from CAD2BIM_mcp.direct.ifc.product_family.write_tools import register_ifc_product_family_write
from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import validate_product_realization_preview_provider_input
from CAD2BIM_mcp.direct.ifc.product_family.preview_tools import register_ifc_product_realization_preview
from CAD2BIM_mcp.direct.ifc.authoring_publish import register_ifc_authoring_publish, DirectAuthoringPublishInput
from CAD2BIM_mcp.runner.authoring_tools import register_runner_authoring_tools
from CAD2BIM_mcp.tool_docs.ifc_geometry import GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE
from CAD2BIM_mcp.tool_docs.ifc_product_family import PRODUCT_FAMILY_CREATE_EXAMPLE


def _schema_check(value):
    if isinstance(value, dict):
        assert not set(value).intersection({"allOf", "if", "then", "else", "not"})
        assert value.get("type") != "unknown"
        for item in value.values():
            _schema_check(item)
    elif isinstance(value, list):
        for item in value:
            _schema_check(item)


async def test_direct_and_runner_final_provider_projection_is_closed():
    direct = FastMCP("draft-direct")
    for register in (register_ifc_geometry_resource_write, register_ifc_product_family_write,
                     register_ifc_geometry_preview, register_ifc_product_realization_preview,
                     register_ifc_authoring_publish, register_ifc_geometry_query_python):
        register(direct, object())
    runner = FastMCP("draft-runner")
    register_runner_authoring_tools(runner, object())
    names = {tool.name for tool in await direct.list_tools()}
    for server in (direct, runner):
        for tool in await server.list_tools():
            if tool.name not in names:
                continue
            _schema_check(tool.parameters)
            _schema_check(tool.output_schema)
            if server is runner:
                serialized = json.dumps(tool.parameters)
                assert "direct_session_alias" not in serialized
                assert "source_path" not in serialized


async def test_formal_family_preview_branch_uses_exact_version_and_observation_only():
    from CAD2BIM_backend.internal_authoring_contracts import GeometryPreviewInput
    direct, runner = FastMCP("formal-direct"), FastMCP("formal-runner")
    register_ifc_geometry_preview(direct, object())
    register_runner_authoring_tools(runner, object())
    value = {"mode": "family_standard_model", "family_version_alias": "cur.exact-family",
        "observation": {"observation_kind": "model"}}
    for server in (direct, runner):
        tool = next(tool for tool in await server.list_tools() if tool.name == "ifc_geometry_preview")
        payload = {"direct_session_alias": "direct_001", **value} if server is direct else value
        validator = Draft202012Validator(tool.parameters)
        assert validator.is_valid(payload if server is direct else {"request": payload})
        parsed = validate_geometry_preview_input(payload) if server is direct else TypeAdapter(GeometryPreviewInput).validate_python(payload)
        assert parsed.family_version_alias == value["family_version_alias"]
        for extra in ({"inspection_ref": "cur.private"}, {"parameters_json": "{}"}, {"retain_for_inspection": True}):
            invalid = {**payload, **extra}
            assert not validator.is_valid(invalid if server is direct else {"request": invalid})


def test_formal_family_read_failures_preserve_domain_code_in_internal_api():
    from CAD2BIM_backend.internal_authoring_api import _domain_error
    from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogNotFoundError
    from CAD2BIM_backend.domain.geometry_catalog import GeometryPreviewUnavailableError
    from CAD2BIM_backend.application.product_family_asset_service import ProductFamilyAssetIntegrityError
    for error, code in ((ProductFamilyCatalogNotFoundError("inaccessible"), "PRODUCT_FAMILY_NOT_FOUND"),
                        (GeometryPreviewUnavailableError("no standard model"), "GEOMETRY_PREVIEW_UNAVAILABLE"),
                        (ProductFamilyAssetIntegrityError("corrupt bytes"), "PRODUCT_FAMILY_INTEGRITY_ERROR")):
        projected = _domain_error(error)
        assert projected.status_code == 409 and projected.detail["code"] == code


async def test_write_updates_allow_omission_and_reject_null_in_final_provider_schema():
    from CAD2BIM_backend.internal_authoring_contracts import GeometryWriteInput, FamilyWriteInput
    direct, runner = FastMCP("draft-null-direct"), FastMCP("draft-null-runner")
    register_ifc_geometry_resource_write(direct, object())
    register_ifc_product_family_write(direct, object())
    register_runner_authoring_tools(runner, object())
    contracts = {
        "ifc_geometry_resource_write": (validate_geometry_resource_write_input, GeometryWriteInput),
        "ifc_product_family_write": (validate_product_family_write_input, FamilyWriteInput),
    }
    for server in (direct, runner):
        for tool in await server.list_tools():
            if tool.name not in contracts:
                continue
            is_direct = server is direct
            parser, domain_type = contracts[tool.name]
            value = {"mode":"update", "draft_ref":"cur.draft", "expected_edit_token":1,
                     "display_name":"Edited draft"}
            if is_direct:
                value["direct_session_alias"] = "direct_001"
            public_value = value if is_direct else {"request":value}
            validator = Draft202012Validator(tool.parameters)
            assert validator.is_valid(public_value)
            parsed = parser(value) if is_direct else TypeAdapter(domain_type).validate_python(value)
            declaration = parsed.to_domain_declaration() if is_direct else parsed.model_dump(exclude_unset=True)
            assert set(declaration) == {"mode", "draft_ref", "expected_edit_token", "display_name"}
            update = type(parsed).model_json_schema()
            for name, field in update["properties"].items():
                if name in update["required"]:
                    continue
                assert "default" not in field
                invalid = {**value, name:None}
                assert not validator.is_valid(invalid if is_direct else {"request":invalid}), name
                with pytest.raises(ValidationError):
                    parser(invalid) if is_direct else TypeAdapter(domain_type).validate_python(invalid)


def test_geometry_input_budget_matches_direct_and_runner_create_update():
    from CAD2BIM_backend.internal_authoring_contracts import GeometryWriteInput
    for count in (8, 9):
        inputs = [{"input_key":f"input_{index}", "resource_version_alias":"cur.exact"} for index in range(count)]
        for mode in ("create", "update"):
            if mode == "create":
                direct = json.loads(GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE)
                direct["source"]["geometry_inputs"] = inputs
                domain = {key:value for key,value in direct.items() if key != "direct_session_alias"}
                domain["source"] = {**direct["source"], "source_files":[{"path":f["source_path"], "source":f["source"]} for f in direct["source"]["source_files"]]}
                domain["source"]["parameter_schema"] = json.loads(domain["source"].pop("parameter_schema_json"))
            else:
                domain = {"mode":"update", "draft_ref":"cur.draft", "expected_edit_token":1, "geometry_inputs":inputs}
                direct = {"direct_session_alias":"direct_001", **domain}
            for parser, value in ((validate_geometry_resource_write_input, direct), (TypeAdapter(GeometryWriteInput).validate_python, domain)):
                if count == 8:
                    parser(value)
                else:
                    with pytest.raises(ValidationError, match="8"):
                        parser(value)


@pytest.mark.parametrize("parser,example", [
    (validate_geometry_resource_write_input, GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE),
    (validate_product_family_write_input, PRODUCT_FAMILY_CREATE_EXAMPLE),
])
def test_write_examples_and_omission_project_to_shared_domain_without_validation_inputs(parser, example):
    create = parser(json.loads(example)).to_domain_declaration()
    assert create["mode"] == "create"
    assert "direct_session_alias" not in create
    assert create["source"]["source_files"][0]["path"] == "main.py"
    assert "validation_scenarios" not in json.dumps(create)
    update = {"direct_session_alias": "direct_001", "mode": "update", "draft_ref": "cur.draft",
              "expected_edit_token": 3, "usage_instructions": {"action":"set", "value":"Updated instructions for the same draft."}}
    result = parser(update).to_domain_declaration()
    assert set(result) == {"mode", "draft_ref", "expected_edit_token", "usage_instructions"}
    with pytest.raises(ValidationError):
        parser({**update, "usage_instructions": None})
    with pytest.raises(ValidationError):
        parser({**update, "expected_edit_token": "3"})
    with pytest.raises(ValidationError):
        parser({**update, "mode": "repair"})


async def test_direct_write_delegates_one_save_and_returns_no_published_version():
    scope = object()
    draft = SimpleNamespace(generation=4)
    saved = SimpleNamespace(draft=draft, operation=SimpleNamespace(catalog_entry=SimpleNamespace(alias="op_001")))
    service = SimpleNamespace(save_draft_from_aliases=AsyncMock(return_value=saved),
                              draft_reference=Mock(return_value="cur.draft"))

    class Probe(IfcGeometryAuthoringApplicationMixin):
        async def _authoring_call(self, alias, name, action):
            return await action(scope)

    probe = Probe()
    probe._geometry_authoring_service = service
    request = validate_geometry_resource_write_input({
        "direct_session_alias": "direct_001", "mode": "update", "draft_ref": "cur.draft",
        "expected_edit_token": 3, "display_name": "Edited draft",
    })
    result = await probe.write_ifc_geometry_resource(request, request_identity="save-1")
    assert result.model_dump() == {"draft_ref": "cur.draft", "edit_token": 4,
                                   "operation_alias": "op_001", "saved": True, "runtime_checked": False}
    service.save_draft_from_aliases.assert_awaited_once_with(
        scope, declaration=request.to_domain_declaration(), request_identity="save-1",
    )


def test_preview_modes_keep_draft_observation_and_case_expectations_separate():
    from CAD2BIM_backend.internal_authoring_contracts import GeometryCasesPreviewInput, FamilyCasesPreviewInput
    from CAD2BIM_mcp.direct.ifc.preview_cases import parse_cases
    single = validate_geometry_preview_input({"direct_session_alias": "direct_001", "mode": "single",
        "source": {"source_kind": "draft", "draft_ref": "cur.draft"}})
    assert single.retain is False
    cases = {"mode": "inline", "items": [{"case_kind": "geometry", "case_key": "invalid_size",
        "parameters": {"length_m": -1}, "expectation": {"expected_status": "REJECTED", "reason_code": "PARAMETERS_INVALID"}}]}
    request = {"direct_session_alias": "direct_001", "mode": "cases", "source": single.source,
               "cases_json": json.dumps(cases), "retain": True}
    assert validate_geometry_preview_input(request).retain is True
    saved = {"mode": "from_operation", "operation_alias": "op_001",
             "changes": [{"change_kind": "reuse", "case_key": "invalid_size"}]}
    validate_geometry_preview_input({**request, "cases_json": json.dumps(saved)})
    for kind, validator in (("geometry", GeometryCasesPreviewInput), ("family", FamilyCasesPreviewInput)):
        parsed = parse_cases(json.dumps(saved), case_kind=kind)
        shared = validator(mode="cases", source=single.source, cases=saved)
        assert shared.cases == parsed and shared.cases.changes[0].case_key == "invalid_size"
    with pytest.raises(ValidationError):
        validate_geometry_preview_input({**request, "sample_parameters_json": "{}"})
    with pytest.raises(ValidationError):
        validate_product_realization_preview_provider_input({"direct_session_alias": "direct_001", "mode": "solve"})
    with pytest.raises(ValidationError):
        DirectAuthoringPublishInput(direct_session_alias="direct_001", root_kind="geometry",
            draft_ref="cur.draft", expected_edit_token=1, evidence=[])
    publication = DirectAuthoringPublishInput(direct_session_alias="direct_001", root_kind="geometry",
        draft_ref="cur.draft", expected_edit_token=1)
    assert publication.preview_examples == ()


class _PreviewReadReferences:
    def __init__(self):
        self.values = {}

    def operation_reference(self, *args, **kwargs):
        return "cur.original-operation"

    def operation_locator(self, *args):
        return "run", "original-operation"

    async def shorten(self, scope, value, kinds):
        if not value.startswith("cur."):
            return value
        key = (value, tuple(kinds))
        if key not in self.values:
            self.values[key] = kinds[0] + "_" + str(len(self.values)).zfill(22)
        return self.values[key]

    async def expand(self, scope, value, kinds):
        return next((key[0] for key, short in self.values.items() if short == value), value)


async def test_failed_case_report_recovery_pages_actual_completed_and_unstarted_cases(monkeypatch):
    from CAD2BIM_backend.domain.product_realization import ProductRealizationStatus
    from CAD2BIM_mcp.direct.operation.application import _operation_get_result
    from tests.unit.backend.test_preview_cases import _setup, _family_cases, _Runtime, _family_service, _source

    _, operations, _, scope, inspections, _ = _setup()
    request, cases = _family_cases()
    cases = (*cases, cases[1].model_copy(update={"case_key": "unstarted"}))
    runtime = _Runtime((ProductRealizationStatus.SOLVED, ProductRealizationStatus.INFEASIBLE,
                        ProductRealizationStatus.INFEASIBLE), fail_after=1)
    await _family_service(inspections, runtime).preview_cases(
        scope, source=_source(), requests=(request,) * 3, cases=cases,
        retain_for_inspection=True, request_identity="partial-provider-recovery",
    )
    published = next(iter(operations.published.values()))
    published.catalog_entry.ordinal = 1
    saved = await inspections.operation_result(scope, published, include_request=True)
    inspection = SimpleNamespace(operation=published, result_package=None)
    references = _PreviewReadReferences()
    first = await inspections.read_preview_operation(scope, published, references=references, saved=saved)
    assert "snapshot_id" not in first.detail.model_dump()
    assert first.operation.outcome == "failed"
    assert first.detail.cases[0].execution_status == "completed"
    assert first.detail.cases[0].inspection_ref
    assert first.detail.next_after_case_key == "success"
    rest = await inspections.read_preview_operation(scope, published, references=references, saved=saved, after_case_key="success", page_size=2)
    assert [item.execution_status for item in rest.detail.cases] == ["technical_error", "not_run"]
    assert rest.detail.complete and rest.detail.next_after_case_key is None
    assert not rest.detail.checks_passed
    from CAD2BIM_backend.domain.preview_report_read import PreviewCaseTextSelection
    text_read = await inspections.read_preview_operation(scope, published, references=references, saved=saved,
        selection=PreviewCaseTextSelection(mode="preview_report", operation_ref=first.operation.operation_ref,
            content_kind="case_outcome", case_key="success", character_count=16384))
    assert text_read.detail.complete
    public_outcome = json.loads(text_read.detail.text)
    assert "inspection_artifact" not in public_outcome
    internal_outcome = saved.case_report.cases[0].model_dump(mode="json")
    assert "geometry_sha256" not in public_outcome
    for field in ("binding_digest", "source_bundle_sha256"):
        assert field in internal_outcome["realization"]
        assert field not in public_outcome["realization"]
    assert "geometry_sha256" not in public_outcome["realization"]["instance_receipts"][0]
    assert public_outcome["realization"]["proposal"] == internal_outcome["realization"]["proposal"]
    assert public_outcome["realization"]["interface_project_world_frames"] == internal_outcome["realization"]["interface_project_world_frames"]
    assert saved.case_report.source.snapshot_id

    from CAD2BIM_mcp.runner import data_tools
    backend = SimpleNamespace(call_data_tool=AsyncMock(return_value={"preview_result":first.model_dump(mode="json")}))
    monkeypatch.setattr(data_tools, "request_domain_credential", lambda:"token")
    server = FastMCP("Case Operation projection")
    data_tools.register_runner_data_tools(server, backend)
    recovered = (await server.call_tool("operation_get", {"request":{"operation_alias":published.catalog_entry.alias}})).structured_content
    assert "snapshot_id" not in recovered["detail"]
    assert recovered == first.model_dump(mode="json")


async def test_case_images_keep_actual_bytes_and_case_keys_through_direct_and_runner():
    from dataclasses import replace
    from hashlib import sha256
    from CAD2BIM_backend.domain.geometry_runtime import GeometryPreview
    from CAD2BIM_backend.internal_authoring_api import _case_batch_payload
    from CAD2BIM_mcp.runner.authoring_tools import _case_batch
    from tests.unit.mcp.test_direct_ifc_geometry_provider import _batch_outcome, _GeometryFacadeProbe

    pixels = b"actual-test-image-bytes"
    image = GeometryPreview(width_px=64, height_px=64, payload=pixels, sha256=sha256(pixels).hexdigest())
    outcome = replace(_batch_outcome(), images=(("observation", image),))
    outcome = replace(outcome, report=outcome.report.model_copy(update={"dependencies":(outcome.report.source,)}))
    application = _GeometryFacadeProbe(SimpleNamespace(identity=object()))
    application._geometry_preview_service = SimpleNamespace(
        prepare_published_cases=AsyncMock(return_value={}), preview_loaded=AsyncMock(return_value=outcome))
    direct = await application.preview_ifc_geometry(validate_geometry_preview_input({
        "direct_session_alias": "direct_001", "mode": "single",
        "source": {"source_kind": "published", "resource_version_alias": "cur.version"},
    }))
    runner, images = _case_batch(_case_batch_payload(outcome))
    assert images == direct.images == (pixels,)
    assert runner.rendered_case_keys == direct.preview.rendered_case_keys == ("observation",)
    for result in (runner, direct.preview):
        public = result.model_dump(mode="json")
        assert "snapshot_id" not in public
        assert public["dependencies"] == [{"source_ref":outcome.report.source.source_ref}]
    assert outcome.report.dependencies[0].snapshot_id == outcome.report.source.snapshot_id


async def _large_preview_read_fixture():
    from dataclasses import replace
    from CAD2BIM_backend.domain.product_realization import ProductRealizationStatus
    from CAD2BIM_backend.domain.preview_cases import PreviewCaseDiagnostic
    from tests.unit.backend.test_preview_cases import _setup, _family_cases, _Runtime, _family_service, _source
    _, operations, _, scope, service, _ = _setup()
    request, cases = _family_cases()
    await _family_service(service, _Runtime((ProductRealizationStatus.SOLVED,)*2)).preview_cases(
        scope, source=_source(), requests=(request,)*2, cases=cases,
        retain_for_inspection=True, request_identity="large-read")
    published=next(iter(operations.published.values()))
    saved=await service.operation_result(scope,published,include_request=True)
    text='中文🙂"\\\n'*180
    diagnostics=tuple(PreviewCaseDiagnostic(code="large",phase="execution",message=text) for _ in range(80))
    original=saved.case_report.cases[0]
    invocation=original.case.invocation.model_copy(update={"public_parameters":{"literal_source_ref":"cur.business-literal","long":text*20}})
    items=tuple(original.model_copy(update={"case":original.case.model_copy(update={"case_key":f"case_{i}","invocation":invocation}),"diagnostics":diagnostics}) for i in range(8))
    report=saved.case_report.model_copy(update={"cases":items,"dependencies":(saved.case_report.source,)*3000,
        "coverage_gaps":(text*180,),"cross_case_diagnostics":diagnostics,"nominal_scenario_key":None})
    saved=replace(saved,case_report=report,request_json=json.dumps({"render":{"mode":"none"},"reference_geometries":[{
        "reference_key":"reference", "resource_version_ref":"cur.reference-geometry", "datum_transform":{},
        "resource_content_digest":"a"*64,"geometry_sha256":"b"*64,"geometry_format":"brep"}]}))
    return scope,service,published,saved,_PreviewReadReferences()


async def test_preview_large_case_report_unicode_and_reference_fragments_are_complete():
    from CAD2BIM_backend.domain.preview_report_read import PreviewCaseTextSelection, PreviewReportTextSelection
    from CAD2BIM_backend.application.preview_report_projection import public_response_bytes
    from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionApplicationError
    scope,service,published,saved,refs=await _large_preview_read_fixture()
    all_keys=[];after=None
    while True:
        page=await service.read_preview_operation(scope,published,references=refs,saved=saved,page_size=4,after_case_key=after)
        assert public_response_bytes(page)<=65536
        assert 'outcome_json' not in page.model_dump_json()
        all_keys.extend(r.case_key for r in page.detail.cases)
        if page.detail.complete:break
        assert page.detail.next_after_case_key==all_keys[-1]
        after=page.detail.next_after_case_key
    assert all_keys==[f'case_{i}' for i in range(8)]
    for kind in ('case_request','case_outcome','dependencies','coverage_gaps','cross_case_diagnostics','placement_input','observation_input'):
        start=0;parts=[];selection_cls=PreviewCaseTextSelection if kind.startswith('case_') else PreviewReportTextSelection
        while True:
            selection=selection_cls(mode='preview_report',operation_ref=page.operation.operation_ref,content_kind=kind,
                start_character=start,character_count=16384,**({'case_key':'case_0'} if kind.startswith('case_') else {}))
            result=await service.read_preview_operation(scope,published,references=refs,saved=saved,selection=selection)
            assert public_response_bytes(result)<=65536
            assert result.detail.start_character==start
            assert result.detail.returned_characters==len(result.detail.text)
            parts.append(result.detail.text)
            if result.detail.complete:break
            assert result.detail.next_character>start
            start=result.detail.next_character
        text=''.join(parts)
        assert len(text)==result.detail.total_characters
        assert len(text.encode('utf-8'))==result.detail.total_utf8_bytes
        value=json.loads(text)
        if kind in {'case_outcome','dependencies','coverage_gaps','cross_case_diagnostics'}:assert len(parts)>1
        if kind=='dependencies':assert len(value)==3000 and all(r['source_ref'].startswith('gv_') for r in value)
        if kind=='case_request':assert value['invocation']['public_parameters']['literal_source_ref']=='cur.business-literal'
        if kind=='observation_input':
            reference=value['reference_geometries'][0]
            assert set(reference)=={'reference_key','resource_version_ref','datum_transform'}
            assert reference['resource_version_ref'].startswith('gv_')
        if kind=='case_outcome':
            assert value['inspection_ref'].startswith('gi_')
            assert 'inspection_artifact' not in value and 'binding_digest' not in value['realization']
            assert len(value['diagnostics'])==80
        eof=selection.model_copy(update={'start_character':len(text)})
        end=await service.read_preview_operation(scope,published,references=refs,saved=saved,selection=eof)
        assert end.detail.text=='' and end.detail.complete and end.detail.next_character is None
        with pytest.raises(GeometryInspectionApplicationError,match='start_character'):
            await service.read_preview_operation(scope,published,references=refs,saved=saved,selection=selection.model_copy(update={'start_character':len(text)+1}))
    with pytest.raises(GeometryInspectionApplicationError,match='case_key'):
        await service.read_preview_operation(scope,published,references=refs,saved=saved,case_key='unknown')
    with pytest.raises(GeometryInspectionApplicationError,match='cannot be combined'):
        await service.read_preview_operation(scope,published,references=refs,saved=saved,case_key='case_0',after_case_key='case_0')


async def test_preview_direct_runner_sdk_final_reference_middleware_preserves_text(monkeypatch):
    from contextlib import nullcontext
    from fastmcp import Client
    from CAD2BIM_mcp.direct.operation.tools import register_operation_get
    from CAD2BIM_mcp.direct.references import DirectReferenceMiddleware
    from CAD2BIM_mcp.runner.references import RunnerReferenceMiddleware
    from CAD2BIM_mcp.runner import data_tools
    scope,service,published,saved,refs=await _large_preview_read_fixture()
    async def read(selection):
        return await service.read_preview_operation(scope,published,references=refs,saved=saved,selection=selection if selection.mode=='preview_report' else None,
            case_key=getattr(selection,'case_key',None),after_case_key=getattr(selection,'after_case_key',None),page_size=getattr(selection,'page_size',1))
    from CAD2BIM_mcp.direct.operation.application import OperationApplicationMixin
    class DirectApplication(OperationApplicationMixin):
        reference_service=refs
        _application=SimpleNamespace(get_operation=AsyncMock(return_value=SimpleNamespace(operation=published,result_package=None)),
            get_operation_by_locator=AsyncMock(return_value=SimpleNamespace(operation=published,result_package=None)))
        async def _run_runtime_read(self,alias,tool,callback):return await callback(scope)
        async def _authoring_operation_result(self,*args,**kwargs):return None
        def _require_geometry_inspection_service(self):
            return SimpleNamespace(operation_result=AsyncMock(return_value=saved),read_preview_operation=service.read_preview_operation)
    direct=FastMCP('preview-direct')
    register_operation_get(direct,DirectApplication())
    direct.add_middleware(DirectReferenceMiddleware(refs,SimpleNamespace(resolve=AsyncMock(return_value=scope)),lambda _:nullcontext()))
    from CAD2BIM_mcp.runner.data_contracts import OperationGetInput
    class Backend:
        async def call_data_tool(self,*,body,**kwargs):
            result=await read(TypeAdapter(OperationGetInput).validate_python(body))
            return {'preview_result':result.model_dump(mode='json')}
        async def translate_references(self,*,items,direction,**kwargs):
            return [await getattr(refs,direction)(scope.identity,r['value'],r['kinds']) for r in items]
    runner=FastMCP('preview-runner');backend=Backend();data_tools.register_runner_data_tools(runner,backend)
    runner.add_middleware(RunnerReferenceMiddleware(backend))
    monkeypatch.setattr(data_tools,'request_domain_credential',lambda:'token')
    monkeypatch.setattr('CAD2BIM_mcp.runner.references.request_domain_credential',lambda:'token')
    outputs=[]
    for server,is_direct in ((direct,True),(runner,False)):
        async with Client(server) as client:
            tool=next(t for t in await client.list_tools() if t.name=='operation_get')
            _schema_check(tool.inputSchema);_schema_check(tool.outputSchema)
            request={'mode':'preview_report','operation_ref':'cur.original-operation','content_kind':'case_outcome','case_key':'case_0','character_count':16384}
            response=await client.call_tool('operation_get',{'direct_session_alias':'direct_001',**request} if is_direct else {'request':request})
            for block in response.content:
                if block.type=='text':assert len(block.text.encode('utf-8'))<=65536
            outputs.append(response.structured_content)
            invalid={**request,'case_key':'unknown'}
            bad=await client.call_tool('operation_get',{'direct_session_alias':'direct_001',**invalid} if is_direct else {'request':invalid},raise_on_error=False)
            assert bad.is_error
    assert outputs[0]['detail']==outputs[1]['detail']


def test_case_outcome_projection_keeps_geometry_and_business_keys_without_mutating_record():
    from CAD2BIM_backend.domain.preview_cases import PreviewCaseResult
    from CAD2BIM_backend.domain.product_realization import ProductRealizationStatus
    from CAD2BIM_mcp.direct.ifc.preview_cases import project_case_outcome_json
    from tests.unit.backend.test_preview_cases import _family_cases
    from tests.unit.backend.test_product_realization_preview_service import _execution, _summary
    from tests.unit.mcp.test_direct_ifc_geometry_provider import _batch_outcome

    realization = _execution(ProductRealizationStatus.SOLVED).realization
    business = {"input_digest":"business value", "geometry_sha256":"business geometry label"}
    realization = realization.model_copy(update={
        "normalized_public_parameters":business,
        "invocation":realization.invocation.model_copy(update={"public_parameters":business}),
        "proposal":realization.proposal.model_copy(update={"internal_parameters":business}),
        "instance_receipts":tuple(receipt.model_copy(update={"normalized_parameters":business}) for receipt in realization.instance_receipts),
    })
    family = PreviewCaseResult(case=_family_cases()[1][0], execution_status="completed",
        expectation_check="pass", realization=realization)
    geometry = _batch_outcome().report.cases[0].model_copy(update={
        "geometry_summary":_summary(), "geometry_sha256":"a" * 64,
        "geometry_interfaces":realization.instance_receipts[0].interfaces,
    })
    for result in (family, geometry):
        original = result.model_dump_json()
        public = json.loads(project_case_outcome_json(result))
        assert "geometry_sha256" not in public
        assert result.model_dump_json() == original
        if result.realization is not None:
            assert public["realization"]["normalized_public_parameters"] == business
            assert public["realization"]["proposal"]["internal_parameters"] == business
            receipt = public["realization"]["instance_receipts"][0]
            assert receipt["normalized_parameters"] == business
            assert "geometry_sha256" not in receipt
            assert receipt["interfaces"] == result.realization.instance_receipts[0].model_dump(mode="json")["interfaces"]
        else:
            assert public["geometry_summary"] == result.geometry_summary.model_dump(mode="json")
            assert public["geometry_interfaces"] == result.model_dump(mode="json")["geometry_interfaces"]


def test_geometry_case_budget_is_eight_in_domain_provider_document_and_parser():
    from pydantic import TypeAdapter
    from CAD2BIM_backend.domain.preview_cases import GeometryCasesInput, InlinePreviewCases, GeometryPreviewCase
    from CAD2BIM_mcp.direct.ifc.preview_cases import parse_cases
    schema = TypeAdapter(GeometryCasesInput).json_schema()
    inline = next(value for value in schema["$defs"].values()
                  if value.get("properties", {}).get("mode", {}).get("const") == "inline")
    assert inline["properties"]["items"]["maxItems"] == 8
    cases = tuple(GeometryPreviewCase(case_key=f"case_{index}") for index in range(9))
    with pytest.raises(ValidationError, match="at most 8"):
        InlinePreviewCases(items=cases)
    with pytest.raises(ValidationError, match="at most 8"):
        parse_cases(json.dumps({"mode": "inline", "items": [item.model_dump() for item in cases]}), case_kind="geometry")


async def test_publication_conflict_is_actionable_and_does_not_make_unknown_errors_retryable():
    from contextlib import nullcontext
    from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftError
    from CAD2BIM_backend.domain.control_plane import ConflictError
    from CAD2BIM_backend.internal_authoring_api import _domain_error
    from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error

    application = IfcGeometryAuthoringApplicationMixin()
    application._resolve_scope = AsyncMock(return_value=SimpleNamespace(identity=object()))
    application._scope_binder = lambda identity: nullcontext()
    conflict = ConflictError("private dependency IDs")
    with pytest.raises(DirectApplicationError) as caught:
        await application._authoring_call("direct_001", "ifc_authoring_publish", AsyncMock(side_effect=conflict))
    assert caught.value.code == "AUTHORING_CONFLICT" and not caught.value.retryable
    assert caught.value.phase == "preflight" and "private dependency" not in str(caught.value)
    backend = _domain_error(conflict, authoring_publish=True)
    assert backend.status_code == 409 and not backend.detail["retryable"]
    unknown = _domain_error(ValueError("private"), authoring_publish=True)
    assert unknown.status_code == 500 and not unknown.detail["retryable"]
    rejected = AuthoringDraftError("AUTHORING_PUBLICATION_COMMIT_REJECTED",
        "Publication was rejected by an integrity check; the draft remains available.",
        operation_alias="op_007", phase="commit_fence")
    rejected.__cause__ = ValueError("INSERT private product_family_versions")
    with pytest.raises(DirectApplicationError) as projected:
        await application._authoring_call("direct_001", "ifc_authoring_publish", AsyncMock(side_effect=rejected))
    assert projected.value.code == rejected.code and projected.value.phase == "commit_fence"
    assert projected.value.operation_alias == "op_007" and not projected.value.retryable
    assert "INSERT" not in str(_tool_error(projected.value))
