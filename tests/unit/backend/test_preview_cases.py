"""Batch Preview evidence uses existing runtime facts and one Operation."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
from CAD2BIM_backend.application.product_realization_preview_service import ProductRealizationPreviewService
from CAD2BIM_backend.domain.product_realization_preview import ProductRealizationPreviewError
from CAD2BIM_backend.domain.preview_cases import (
    FamilyCaseExpectation, FamilyPreviewCase, GeometryCaseExpectation, GeometryPreviewCase,
    PreviewCaseReuse, PreviewSourceSnapshot, SavedPreviewCases, PreviewSelectedRender,
)
from CAD2BIM_backend.domain.product_realization import ProductRealizationStatus
from CAD2BIM_backend.ports.product_realization_runtime import ProductRealizationRuntimeValidationError
from tests.unit.backend.test_geometry_inspection_service import _setup
from tests.unit.backend.test_product_realization_preview_service import _request, _execution, _geometry


class _Runtime:
    def __init__(self, statuses=(), fail_after=None):
        self.statuses, self.fail_after, self.calls = statuses, fail_after, 0
        self._numeric_ordinal = 0

    @asynccontextmanager
    async def invocation_scope(self, *args, **kwargs):
        yield

    execution_scope = invocation_scope

    async def realize_batch(self, requests, *, output_need):
        from CAD2BIM_backend.domain.product_realization import (
            ProductRealizationOutputNeed, ProductRealizationGeometryExecution,
            ProductGeometryInterfaceEvaluation, finalize_product_realization)
        self.calls += 1
        needs = (output_need,) * len(requests) if isinstance(output_need, ProductRealizationOutputNeed) else output_need
        for request, need in zip(requests, needs, strict=True):
            index = self._numeric_ordinal
            self._numeric_ordinal += 1
            if index == self.fail_after:
                raise ValueError("worker stopped after a complete earlier case")
            status = self.statuses[index]
            execution = _execution(status)
            evaluations = tuple(ProductGeometryInterfaceEvaluation.model_validate(receipt.model_dump(
                exclude={"geometry_sha256", "geometry_summary"})) for receipt in execution.realization.instance_receipts)
            result = finalize_product_realization(request, execution.realization.proposal, (),
                output_need=need, interface_evaluations=evaluations if need is ProductRealizationOutputNeed.INTERFACES else ())
            yield ProductRealizationGeometryExecution(realization=result, output_need=need)

    async def instantiate_batch(self, requests, prechecks, *, output_need):
        from CAD2BIM_backend.domain.product_realization import ProductRealizationGeometryExecution, ProductRealizationOutputNeed
        for checked in prechecks:
            built = _execution(ProductRealizationStatus.SOLVED)
            result = checked.model_copy(update={"execution_stage": "instantiated", "instance_receipts": built.realization.instance_receipts})
            yield ProductRealizationGeometryExecution(realization=result, output_need=output_need,
                geometry=_geometry() if output_need is ProductRealizationOutputNeed.PREVIEW_WORLD else None,
                instance_geometries={"body_shape": _geometry()})

    async def evaluate_function_interfaces(self, request):
        from CAD2BIM_backend.domain.geometry_runtime import GeometryFunctionInterfaceEvaluation
        return GeometryFunctionInterfaceEvaluation(source_bundle=request.source_bundle, sample_parameters=request.sample_parameters,
            sample_interfaces=tuple(() for _ in request.sample_parameters))

    async def instantiate_function(self, request, evaluation):
        return await self.validate_function(request)


def _source(kind="family_realization"):
    return PreviewSourceSnapshot(source_kind=kind, source_type="published", source_id="source",
        source_ref="cur.exact", snapshot_id="source-v1", content_digest="a" * 64, title="Example")


def _family_cases():
    request = _request()
    return request, (
        FamilyPreviewCase(case_key="success", invocation=request.invocation,
            expectation=FamilyCaseExpectation(expected_status="SOLVED")),
        FamilyPreviewCase(case_key="rejection", invocation=request.invocation,
            expectation=FamilyCaseExpectation(expected_status="INFEASIBLE", reason_keys=("clearance",))),
    )


def _family_service(inspections, runtime):
    class FamilyCatalog:
        async def resolve_exact_version(self, scope, reference):
            assert reference == _source().source_ref
            request, _ = _family_cases()
            return SimpleNamespace(version=SimpleNamespace(
                family_contract=request.family_contract))
    inspections._family_catalog = FamilyCatalog()
    class Previews:
        _geometry_runtime = object()
        async def _load_reference_geometries(self, *args):
            return ()
    return ProductRealizationPreviewService(None, None, runtime, Previews(), inspections)


@pytest.mark.asyncio
@pytest.mark.parametrize("retain", [False, True])
async def test_family_batch_preserves_success_and_business_rejection_with_optional_storage(retain):
    artifacts, operations, _, scope, inspections, _ = _setup()
    request, cases = _family_cases()
    runtime = _Runtime((ProductRealizationStatus.SOLVED, ProductRealizationStatus.INFEASIBLE))
    service = _family_service(inspections, runtime)
    outcome = await service.preview_cases(scope, source=_source(), requests=(request, request), cases=cases,
        retain_for_inspection=retain, request_identity="batch")
    assert runtime.calls == 2
    assert [item.expectation_check for item in outcome.report.cases] == ["pass", "pass"]
    from CAD2BIM_mcp.direct.ifc.preview_cases import project_case_batch, project_case_outcome_json
    import json
    assert project_case_batch(outcome).cases[0].external_target_comparison == "NOT_COMPARED"
    assert json.loads(project_case_outcome_json(outcome.report.cases[0]))["realization"]["external_target_comparison"] == "NOT_COMPARED"
    assert outcome.report.cases[1].realization.proposal.status is ProductRealizationStatus.INFEASIBLE
    assert outcome.report.cases[1].inspection_ref is None
    assert bool(outcome.report.cases[0].inspection_ref) is retain
    assert len(operations.published) == int(retain)
    if not retain:
        assert not artifacts.records
    else:
        assert [item.kind for item in artifacts.records.values()].count("geometry_inspection") == 1
        repeated = await service.preview_cases(scope, source=_source(), requests=(request, request), cases=cases,
            retain_for_inspection=True, request_identity="batch")
        assert repeated.report == outcome.report
        assert runtime.calls == 2


@pytest.mark.asyncio
async def test_deferred_dependency_build_is_after_admission_and_skipped_on_replay_and_evidence_reuse():
    _, operations, _, scope, inspections, _ = _setup()
    async def resolve_alias(scope, kind, alias):
        return next((SimpleNamespace(operation_id=key) for key, value in operations.published.items()
                     if value.catalog_entry.alias == alias), None)
    operations.get_catalog_entry_by_alias = resolve_alias
    request, cases = _family_cases()
    runtime = _Runtime((ProductRealizationStatus.INFEASIBLE,))
    service = _family_service(inspections, runtime)
    calls = []
    async def prepare(geometry_runtime):
        assert len(operations.states) == 1  # No author execution before admission.
        calls.append(geometry_runtime)
        return (request,)
    options = dict(source=_source(), requests=None, cases=cases[1:], request_loader=prepare,
        family_contract=request.family_contract, invocations=(request.invocation,), retain_for_inspection=True,
        request_identity="deferred")
    first = await service.preview_cases(scope, **options)
    second = await service.preview_cases(scope, **options)
    assert len(calls) == runtime.calls == 1 and second.report == first.report
    reused, = await inspections.resolve_cases(scope, SavedPreviewCases(operation_alias=first.operation_alias,
        changes=({"change_kind": "reuse", "case_key": "rejection"},)), case_kind="family")
    assert reused.invocation == cases[1].invocation and reused.expectation == cases[1].expectation
    async def forbidden_build(_):
        raise AssertionError("Evidence-only check rebuilt a mapped dependency")
    bindings = tuple(SimpleNamespace(binding_key=item.binding_key, expected_resource_kind=item.resource_kind,
        geometry_content_digest=item.geometry_content_digest) for item in request.geometry_bindings)
    checked = await service.preview_cases(scope, source=_source(), requests=None, cases=(reused,),
        request_loader=forbidden_build, family_contract=request.family_contract,
        invocations=(request.invocation,), geometry_bindings=bindings)
    assert checked.report.cases[0].expectation_check == "pass" and runtime.calls == 1
    assert first.report.checks_passed and checked.report.checks_passed
    assert not first.report.coverage_gaps and not checked.report.coverage_gaps


@pytest.mark.asyncio
async def test_partial_batch_keeps_prior_entity_and_never_fabricates_business_rejection():
    _, operations, _, scope, inspections, _ = _setup()
    request, cases = _family_cases()
    cases = (*cases, cases[1].model_copy(update={"case_key": "unstarted"}))
    runtime = _Runtime((ProductRealizationStatus.SOLVED, ProductRealizationStatus.INFEASIBLE,
        ProductRealizationStatus.INFEASIBLE), fail_after=1)
    outcome = await _family_service(inspections, runtime).preview_cases(scope, source=_source(),
        requests=(request,) * 3, cases=cases, retain_for_inspection=True, request_identity="partial")
    assert outcome.report.completion == "interrupted"
    assert not outcome.report.checks_passed
    assert [item.execution_status for item in outcome.report.cases] == ["completed", "technical_error", "not_run"]
    assert [item.execution_source for item in outcome.report.cases] == ["executed", "executed", "none"]
    assert outcome.report.cases[0].inspection_ref
    assert outcome.report.cases[1].realization is None
    assert next(iter(operations.published.values())).operation.status.value == "failed"
    saved = next(iter(operations.published.values()))
    listing = await inspections.operation_result(scope, saved)
    assert [item["execution_status"] for item in listing.case_index] == ["completed", "technical_error", "not_run"]
    assert listing.case_index[0]["inspection_ref"] == outcome.report.cases[0].inspection_ref
    assert [item["inspection_ref"] for item in listing.case_index[1:]] == [None, None]
    from CAD2BIM_mcp.direct.operation.application import _operation_get_result
    from CAD2BIM_backend.domain.canonical import canonical_json
    import json
    recovered = await inspections.operation_result(scope, saved, include_request=True)
    projected = _operation_get_result(SimpleNamespace(operation=saved, result_package=None),
        inspection_result=recovered, case_key="success")
    assert json.loads(projected.detail.cases[0].request_json)["invocation"] == json.loads(canonical_json(cases[0].invocation))
    assert projected.detail.placement_input_json is None
    assert set(json.loads(projected.detail.observation_input_json)) == {"render", "reference_geometries"}


@pytest.mark.asyncio
async def test_family_output_failure_keeps_safe_reason_and_phase_in_report_and_operation():
    _, operations, _, scope, inspections, _ = _setup()
    async def resolve_alias(scope, kind, alias):
        return next((SimpleNamespace(operation_id=key) for key, value in operations.published.items()
                     if value.catalog_entry.alias == alias), None)
    operations.get_catalog_entry_by_alias = resolve_alias
    async def get_operation(scope, operation_id):
        return operations.published.get(operation_id)
    operations.get_operation = get_operation
    request = _request()
    case = FamilyPreviewCase(case_key="micro_001", invocation=request.invocation)

    class FailingRuntime(_Runtime):
        async def realize_batch(self, requests, *, output_need):
            raise ProductRealizationRuntimeValidationError(
                "REALIZATION_OUTPUT_INVALID",
                "Product batch geometry failed independent codec or receipt verification",
                phase="output_validation",
                diagnostic={"reason": "OCCT BREP geometry is invalid; /private/host/path"},
            )
            yield  # Make this an async generator, matching the runtime port.

    service = _family_service(inspections, FailingRuntime())
    outcome = await service.preview_cases(scope, source=_source(), requests=(request,), cases=(case,),
        retain_for_inspection=True, request_identity="output-failure")
    diagnostic = outcome.report.cases[0].diagnostics[0]
    assert diagnostic.code == "REALIZATION_OUTPUT_INVALID"
    assert diagnostic.phase == "output_validation"
    assert "Preview geometry is invalid" in diagnostic.message
    assert "/private/host/path" not in diagnostic.message
    assert outcome.report.cross_case_diagnostics[-1] == diagnostic
    assert "not compared with explicit expectations" in outcome.report.coverage_gaps[0]
    assert "Publication coverage requires" not in str(outcome.report)

    saved = next(iter(operations.published.values()))
    recovered = await inspections.read_case_report(scope, outcome.operation_alias)
    assert recovered.cases[0].diagnostics[0] == diagnostic
    listing = await inspections.operation_result(scope, saved, include_request=True)
    assert listing.case_index[0]["execution_status"] == "technical_error"
    assert listing.case_report.cases[0].diagnostics[0] == diagnostic

    with pytest.raises(ProductRealizationPreviewError) as single:
        await service.preview_loaded(scope, source=_source(), request=request,
            retain_for_inspection=True, request_identity="single-output-failure")
    assert single.value.message == diagnostic.message
    assert "/private/host/path" not in single.value.message


@pytest.mark.asyncio
async def test_family_reuse_mismatch_marks_the_reuse_case_and_keeps_pending_cases_unrun():
    _, operations, _, scope, inspections, _ = _setup()
    async def resolve_alias(scope, kind, alias):
        return next((SimpleNamespace(operation_id=key) for key, value in operations.published.items()
                     if value.catalog_entry.alias == alias), None)
    operations.get_catalog_entry_by_alias = resolve_alias
    request, (_, negative) = _family_cases()
    runtime = _Runtime((ProductRealizationStatus.INFEASIBLE,))
    service = _family_service(inspections, runtime)
    original = await service.preview_cases(scope, source=_source(), requests=(request,), cases=(negative,),
        retain_for_inspection=True, request_identity="reuse-source")
    reuse = PreviewCaseReuse(operation_alias=original.operation_alias, case_key="rejection")
    changed_request = request.model_copy(update={"invocation": request.invocation.model_copy(
        update={"product_origin": (1, 0, 0)})})
    cases = (negative.model_copy(update={"case_key": "prior_reuse", "reuse": reuse}),
        *(negative.model_copy(update={"case_key": key}) for key in ("zero_equal", "zero_unequal", "micro_001")),
        negative.model_copy(update={"case_key": "ordinary_60", "invocation": changed_request.invocation,
            "reuse": reuse}), negative.model_copy(update={"case_key": "after_failure"}))
    outcome = await service.preview_cases(scope, source=_source(),
        requests=(request, request, request, request, changed_request, request), cases=cases,
        retain_for_inspection=True, request_identity="reuse-mismatch")
    assert [item.execution_status for item in outcome.report.cases] == [
        "completed", "not_run", "not_run", "not_run", "technical_error", "not_run"]
    assert [item.execution_source for item in outcome.report.cases] == [
        "reused", "none", "none", "none", "none", "none"]
    assert "Saved execution does not match" in outcome.report.cases[4].diagnostics[0].message
    assert outcome.report.completion == "interrupted" and runtime.calls == 1
    assert await inspections.read_case_report(scope, outcome.operation_alias) == outcome.report
    assert next(value.operation.status.value for value in operations.published.values()
                if value.catalog_entry.alias == outcome.operation_alias) == "failed"


@pytest.mark.asyncio
async def test_geometry_reuse_mismatch_marks_the_reuse_case_and_keeps_pending_cases_unrun():
    _, operations, _, scope, inspections, _ = _setup()
    async def resolve_alias(scope, kind, alias):
        return next((SimpleNamespace(operation_id=key) for key, value in operations.published.items()
                     if value.catalog_entry.alias == alias), None)
    operations.get_catalog_entry_by_alias = resolve_alias
    runtime = _Runtime()
    service = GeometryPreviewService(None, None, runtime, inspection_service=inspections)
    original = await service.preview_cases(scope, source=_source("geometry_sample"), build_request=_geometry(),
        cases=(GeometryPreviewCase(case_key="original"),), retain_for_inspection=True,
        request_identity="geometry-source")
    cases = (GeometryPreviewCase(case_key="pending_first"), GeometryPreviewCase(case_key="pending_second"),
        GeometryPreviewCase(case_key="reuse_mismatch", reuse=PreviewCaseReuse(
            operation_alias=original.operation_alias, case_key="original")),
        GeometryPreviewCase(case_key="after_failure"))
    changed_source = _source("geometry_sample").model_copy(update={"content_digest": "b" * 64})
    outcome = await service.preview_cases(scope, source=changed_source, build_request=_geometry(), cases=cases,
        retain_for_inspection=True, request_identity="geometry-reuse-mismatch")
    assert [item.execution_status for item in outcome.report.cases] == [
        "not_run", "not_run", "technical_error", "not_run"]
    assert [item.execution_source for item in outcome.report.cases] == ["none"] * 4
    assert "Saved Geometry execution differs" in outcome.report.cases[2].diagnostics[0].message
    assert outcome.report.completion == "interrupted" and runtime.calls == 0
    assert await inspections.read_case_report(scope, outcome.operation_alias) == outcome.report
    assert next(value.operation.status.value for value in operations.published.values()
                if value.catalog_entry.alias == outcome.operation_alias) == "failed"


@pytest.mark.asyncio
async def test_geometry_case_limit_rejects_before_admission_or_runtime_scope():
    _, operations, _, scope, inspections, _ = _setup()
    # An object without runtime methods proves even opening a worker scope is
    # unnecessary for an invalid batch.
    service = GeometryPreviewService(None, None, object(), inspection_service=inspections)
    with pytest.raises(ValueError, match="between 1 and 8"):
        await service.preview_cases(scope, source=_source("geometry_sample"), build_request=_geometry(),
            cases=tuple(GeometryPreviewCase(case_key=f"sample_{index}") for index in range(9)),
            retain_for_inspection=True, request_identity="too-many")
    assert not operations.states


@pytest.mark.asyncio
async def test_geometry_invalid_parameters_are_saved_without_shape_or_author_execution():
    artifacts, operations, _, scope, inspections, _ = _setup()
    runtime = _Runtime()
    service = GeometryPreviewService(None, None, runtime, inspection_service=inspections)
    outcome = await service.preview_cases(scope, source=_source("geometry_sample"), build_request=_geometry(),
        cases=(GeometryPreviewCase(case_key="invalid", parameters={"undeclared": 1},
            expectation=GeometryCaseExpectation(expected_status="REJECTED", reason_code="PARAMETERS_INVALID")),),
        retain_for_inspection=True, request_identity="invalid")
    assert outcome.report.cases[0].geometry_status == "REJECTED"
    assert outcome.report.cases[0].expectation_check == "pass"
    assert outcome.report.checks_passed and not outcome.report.coverage_gaps
    assert outcome.report.cases[0].inspection_ref is None
    assert runtime.calls == 0
    assert len(operations.published) == 1
    assert not any(item.kind == "geometry_inspection" for item in artifacts.records.values())


@pytest.mark.asyncio
async def test_geometry_rejected_case_reuse_preserves_fact_and_checks_exact_source_without_entity():
    _, operations, _, scope, inspections, _ = _setup()
    async def resolve_alias(scope, kind, alias):
        return next((SimpleNamespace(operation_id=key) for key, value in operations.published.items()
                     if value.catalog_entry.alias == alias), None)
    operations.get_catalog_entry_by_alias = resolve_alias
    runtime = _Runtime()
    service = GeometryPreviewService(None, None, runtime, inspection_service=inspections)
    source = _source("geometry_sample")
    original = GeometryPreviewCase(case_key="invalid", parameters={"undeclared": 1},
        expectation=GeometryCaseExpectation(expected_status="REJECTED", reason_code="PARAMETERS_INVALID"))
    first = await service.preview_cases(scope, source=source, build_request=_geometry(),
        cases=(original,), retain_for_inspection=True, request_identity="rejected-source")
    async def forbidden_entity(*args, **kwargs):
        raise AssertionError("A rejected execution has no saved entity")
    inspections.load_case_geometry = forbidden_entity
    reused, = await inspections.resolve_cases(scope, SavedPreviewCases(operation_alias=first.operation_alias,
        changes=({"change_kind": "reuse", "case_key": "invalid"},)), case_kind="geometry")
    outcome = await service.preview_cases(scope, source=source, build_request=_geometry(), cases=(reused,),
        render=PreviewSelectedRender(case_keys=("invalid",)))
    item = outcome.report.cases[0]
    assert item.execution_source == "reused" and item.geometry_status == "REJECTED"
    assert item.expectation_check == "pass" and item.render_status == "no_geometry"
    assert item.inspection_ref is None and not outcome.images and outcome.report.checks_passed

    changed_expectation = reused.model_copy(update={"expectation": GeometryCaseExpectation(expected_status="BUILT")})
    mismatch = await service.preview_cases(scope, source=source, build_request=_geometry(), cases=(changed_expectation,))
    assert mismatch.report.cases[0].geometry_status == "REJECTED"
    assert mismatch.report.cases[0].expectation_check == "fail" and not mismatch.report.checks_passed
    for changed_source, changed_case in (
        (source.model_copy(update={"content_digest": "b" * 64}), reused),
        (source, reused.model_copy(update={"parameters": {"different": 2}})),
    ):
        failed = await service.preview_cases(scope, source=changed_source, build_request=_geometry(), cases=(changed_case,))
        assert failed.report.cases[0].execution_status == "technical_error"
        assert failed.report.cases[0].geometry_status is None and not failed.report.checks_passed
    assert runtime.calls == 0 and len(operations.published) == 1


@pytest.mark.asyncio
async def test_geometry_rejected_boolean_parameters_cannot_reuse_as_equal_numbers():
    from CAD2BIM_backend.domain.geometry_runtime import GeometryBuildFixedRequest
    _, operations, _, scope, inspections, _ = _setup()
    async def resolve_alias(scope, kind, alias):
        return next((SimpleNamespace(operation_id=key) for key, value in operations.published.items()
                     if value.catalog_entry.alias == alias), None)
    operations.get_catalog_entry_by_alias = resolve_alias
    runtime, builds = _Runtime(), []
    async def forbidden_build(request):
        builds.append(request)
        raise AssertionError("Explicit reuse must not build a replacement")
    runtime.validate_function = forbidden_build
    service = GeometryPreviewService(None, None, runtime, inspection_service=inspections)
    schema = {"type": "object", "properties": {"size": {"type": "number", "minimum": 1}},
              "required": ["size"], "additionalProperties": False}
    original = GeometryPreviewCase(case_key="invalid", parameters={"size": True},
        expectation=GeometryCaseExpectation(expected_status="REJECTED", reason_code="PARAMETERS_INVALID"))
    source, build_request = _source("geometry_sample"), GeometryBuildFixedRequest(source_bundle=b"source")
    first = await service.preview_cases(scope, source=source, build_request=build_request,
        parameter_schema=schema, cases=(original,), retain_for_inspection=True, request_identity="boolean-source")
    assert first.report.cases[0].geometry_status == "REJECTED"
    changed = original.model_copy(update={"parameters": {"size": 1}, "reuse": PreviewCaseReuse(
        operation_alias=first.operation_alias, case_key="invalid")})
    failed = await service.preview_cases(scope, source=source, build_request=build_request,
        parameter_schema=schema, cases=(changed,))
    assert failed.report.cases[0].execution_status == "technical_error"
    assert failed.report.cases[0].geometry_status is None and not failed.report.checks_passed
    assert not builds and len(operations.published) == 1


@pytest.mark.asyncio
async def test_saved_case_reuse_rechecks_expectation_without_new_runtime_execution():
    _, operations, _, scope, inspections, _ = _setup()
    async def resolve_alias(scope, kind, alias):
        return next((SimpleNamespace(operation_id=key) for key, value in operations.published.items()
                     if value.catalog_entry.alias == alias), None)
    operations.get_catalog_entry_by_alias = resolve_alias
    request, cases = _family_cases()
    runtime = _Runtime((ProductRealizationStatus.SOLVED, ProductRealizationStatus.INFEASIBLE))
    service = _family_service(inspections, runtime)
    first = await service.preview_cases(scope, source=_source(), requests=(request, request), cases=cases,
        retain_for_inspection=True, request_identity="original")
    reused = cases[1].model_copy(update={"expectation": FamilyCaseExpectation(expected_status="AMBIGUOUS", reason_keys=("clocking",)),
        "reuse": PreviewCaseReuse(operation_alias=first.operation_alias, case_key="rejection")})
    second = await service.preview_cases(scope, source=_source(), requests=(request,), cases=(reused,))
    assert runtime.calls == 2
    assert second.report.cases[0].execution_source == "reused"
    assert second.report.cases[0].expectation_check == "fail"
    assert not second.report.checks_passed
    inherited = await inspections.resolve_cases(scope, SavedPreviewCases(operation_alias=first.operation_alias), case_kind="family")
    assert inherited == cases
    reused_success = cases[0].model_copy(update={"reuse": PreviewCaseReuse(
        operation_alias=first.operation_alias, case_key="success")})
    metadata_source = _source().model_copy(update={"snapshot_id": "metadata-only-snapshot", "title": "Updated title"})
    third = await service.preview_cases(scope, source=metadata_source, requests=(request,), cases=(reused_success,),
        retain_for_inspection=True, request_identity="reuse-saved")
    loaded = await inspections.load_case_geometry(scope, third.report, "success")
    assert loaded.manifest.source_snapshot == first.report.source
    assert third.report.cases[0].inspection_ref == first.report.cases[0].inspection_ref
    assert runtime.calls == 2
    inherited = await inspections.resolve_cases(scope, SavedPreviewCases(operation_alias=third.operation_alias), case_kind="family")
    assert inherited[0].reuse is None and inherited[0].invocation == reused_success.invocation


@pytest.mark.asyncio
async def test_interface_fail_case_report_saves_reads_and_reuses_without_geometry(tmp_path):
    from CAD2BIM_backend.domain.product_realization import (
        ProductGeometryInterfaceEvaluation, ProductRealizationAuthorProposal, ProductRealizationGeometryExecution,
        ProductRealizationOutputNeed, finalize_product_realization, validate_product_realization_proposal,
    )
    from pydantic import TypeAdapter
    from tests.unit.backend.test_product_realization_runtime import _model_interface_pair_request
    request = _model_interface_pair_request(tmp_path, right_frame={"translation_m": (0.1, 0, 0)},
        relation={"direction_relation": "same", "orientation_requirement": "full_frame", "profile_requirement": "equal"})
    proposal = validate_product_realization_proposal(request, TypeAdapter(ProductRealizationAuthorProposal).validate_python({
        "status": "SOLVED", "components": [{"component_key": "body"}],
        "fragments": [{"fragment_key": "body_shape", "binding_key": "body_geometry"}]}))
    checked = finalize_product_realization(request, proposal, (), output_need=ProductRealizationOutputNeed.INTERFACES,
        interface_evaluations=(ProductGeometryInterfaceEvaluation(fragment_key="body_shape", binding_key="body_geometry",
            interfaces=request.geometry_bindings[0].interfaces),))
    assert checked.frame_check_status == "FAIL" and checked.instance_receipts == ()
    runtime = _Runtime()
    async def realize_batch(requests, *, output_need):
        runtime.calls += 1
        yield ProductRealizationGeometryExecution(realization=checked, output_need=output_need)
    runtime.realize_batch = realize_batch
    _, operations, _, scope, inspections, _ = _setup()
    async def resolve_alias(scope, kind, alias):
        return next((SimpleNamespace(operation_id=key) for key, value in operations.published.items()
                     if value.catalog_entry.alias == alias), None)
    operations.get_catalog_entry_by_alias = resolve_alias
    service = _family_service(inspections, runtime)
    case = FamilyPreviewCase(case_key="interface_fail", invocation=request.invocation,
        expectation=FamilyCaseExpectation(expected_status="SOLVED"))
    first = await service.preview_cases(scope, source=_source(), requests=(request,), cases=(case,),
        retain_for_inspection=True, request_identity="numeric-fail-report", render=PreviewSelectedRender(case_keys=(case.case_key,)))
    result = first.report.cases[0]
    assert result.execution_status == "completed" and result.expectation_check == "fail"
    assert result.render_status == "no_geometry" and result.inspection_ref is None
    assert result.realization.execution_stage == "interface_constraints_evaluated"
    restored = await inspections.read_case_report(scope, first.operation_alias)
    assert restored == first.report
    reused = case.model_copy(update={"reuse": PreviewCaseReuse(operation_alias=first.operation_alias, case_key=case.case_key)})
    second = await service.preview_cases(scope, source=_source(), requests=(request,), cases=(reused,))
    assert runtime.calls == 1
    assert second.report.cases[0].execution_source == "reused"
    assert second.report.cases[0].realization == checked
    assert second.report.cases[0].expectation_check == "fail"


def test_reuse_change_targets_only_original_cases_and_rejects_repeat_edits():
    from pydantic import ValidationError
    from CAD2BIM_backend.domain.preview_cases import revise_preview_cases
    _, cases = _family_cases()
    missing = SavedPreviewCases(operation_alias="op_001", changes=({"change_kind": "reuse", "case_key": "missing"},))
    with pytest.raises(ValueError, match="does not exist"):
        revise_preview_cases(cases, missing.changes, operation_alias=missing.operation_alias)
    added = cases[0].model_copy(update={"case_key": "new"})
    duplicate = SavedPreviewCases(operation_alias="op_001", changes=(
        {"change_kind": "add", "case": added}, {"change_kind": "reuse", "case_key": "new"}))
    with pytest.raises(ValueError, match="only once"):
        revise_preview_cases(cases, duplicate.changes, operation_alias=duplicate.operation_alias)
    with pytest.raises(ValidationError):
        SavedPreviewCases(operation_alias="op_001", changes=({"change_kind": "reuse", "case_key": "success",
            "expectation": {"expected_status": "SOLVED"}},))


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_kind", ["case", "inspection"])
async def test_saved_single_observation_can_be_checked_without_reexecuting(reuse_kind):
    from CAD2BIM_backend.domain.preview_cases import PreviewInspectionReuse
    _, operations, _, scope, inspections, _ = _setup()
    async def resolve_alias(scope, kind, alias):
        return next((SimpleNamespace(operation_id=key) for key, value in operations.published.items()
                     if value.catalog_entry.alias == alias), None)
    operations.get_catalog_entry_by_alias = resolve_alias
    request, cases = _family_cases()
    runtime = _Runtime((ProductRealizationStatus.SOLVED,))
    service = _family_service(inspections, runtime)
    single = await service.preview_loaded(scope, source=_source(), request=request,
        retain_for_inspection=True, request_identity="single-observation")
    report = await inspections.read_case_report(scope, single.operation_alias)
    assert report.cases[0].case.case_key == "observation"
    assert report.cases[0].expectation_check == "not_checked"
    reuse = (PreviewCaseReuse(operation_alias=single.operation_alias, case_key="observation")
             if reuse_kind == "case" else PreviewInspectionReuse(inspection_ref=single.inspection_ref))
    checked = cases[0].model_copy(update={"reuse": reuse})
    batch = await service.preview_cases(scope, source=_source(), requests=(request,), cases=(checked,),
        retain_for_inspection=True, request_identity="checked-observation")
    result = batch.report.cases[0]
    assert result.execution_source == "reused" and result.expectation_check == "pass"
    assert result.inspection_ref == single.inspection_ref and runtime.calls == 1
    assert batch.operation_alias != single.operation_alias


@pytest.mark.asyncio
async def test_retention_budget_failure_keeps_realized_facts_and_saves_report(monkeypatch):
    artifacts, operations, _, scope, inspections, _ = _setup()
    request, cases = _family_cases()
    runtime = _Runtime((ProductRealizationStatus.SOLVED,))
    monkeypatch.setattr("CAD2BIM_backend.application.geometry_inspection_service.GEOMETRY_INSPECTION_MAX_BYTES", 1)
    outcome = await _family_service(inspections, runtime).preview_cases(scope, source=_source(),
        requests=(request,), cases=cases[:1], retain_for_inspection=True, request_identity="budget")
    item = outcome.report.cases[0]
    assert item.realization.proposal.status is ProductRealizationStatus.SOLVED
    assert item.execution_status == "completed" and item.inspection_ref is None
    assert any(value.phase == "storage" for value in item.diagnostics)
    assert not outcome.report.checks_passed
    assert len(operations.published) == 1
    assert any(value.kind == "geometry_preview_case_report" for value in artifacts.records.values())


@pytest.mark.asyncio
async def test_render_failure_does_not_invalidate_saved_geometry_or_rebuild_on_observe():
    artifacts, operations, _, scope, inspections, _ = _setup()
    runtime = _Runtime()
    service = GeometryPreviewService(None, None, runtime, inspection_service=inspections)
    async def failed_render(*args, **kwargs):
        raise ValueError("renderer unavailable")
    service._observe_inspection = failed_render
    case = GeometryPreviewCase(case_key="shape", expectation=GeometryCaseExpectation())
    rendering = PreviewSelectedRender(case_keys=("shape",))
    first = await service.preview_cases(scope, source=_source("geometry_sample"), build_request=_geometry(),
        cases=(case,), retain_for_inspection=True, render=rendering, request_identity="render")
    item = first.report.cases[0]
    assert item.inspection_ref and item.render_status == "failed"
    assert item.expectation_check == "pass" and first.report.checks_passed
    before = len(artifacts.records)
    second = await service.observe_case_report(scope, first.report, rendering, first.operation_alias)
    assert second.report.cases[0].render_status == "failed"
    assert len(artifacts.records) == before and len(operations.published) == 1
    assert runtime.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("conflicting", [False, True])
async def test_fixed_geometry_preview_preserves_declared_interfaces_and_rejects_conflict(conflicting):
    import hashlib
    from unittest.mock import AsyncMock
    from CAD2BIM_backend.domain.geometry_runtime import GeometryBuildFixedRequest
    from tests.unit.backend.test_product_realization_preview_service import _model_interfaces
    _, _, _, scope, _, _ = _setup()
    declared = _model_interfaces()
    built_interfaces = ()
    if conflicting:
        built_interfaces = (declared[0].model_copy(update={"local_frame":
            declared[0].local_frame.model_copy(update={"translation_m": (0.5, 0., 0.)})}),)
    runtime = _Runtime()
    runtime.build_fixed = AsyncMock(return_value=SimpleNamespace(geometry=_geometry(),
        interfaces=built_interfaces, source_bundle_sha256=hashlib.sha256(b"fixed source").hexdigest()))
    outcome = await GeometryPreviewService(None, None, runtime).preview_cases(
        scope, source=_source("geometry_sample"),
        build_request=GeometryBuildFixedRequest(source_bundle=b"fixed source"),
        interfaces=declared, cases=(GeometryPreviewCase(case_key="declared_frames"),))
    case = outcome.report.cases[0]
    runtime.build_fixed.assert_awaited_once()
    if conflicting:
        assert case.execution_status == "technical_error" and case.geometry_sha256 is None
        assert "Declared Geometry interfaces differ from the actual fixed build" in case.diagnostics[0].message
    else:
        assert case.execution_status == "completed" and case.geometry_interfaces == declared


@pytest.mark.asyncio
@pytest.mark.parametrize("retain", [False, True])
async def test_geometry_function_cases_use_one_batch_and_request_bytes_only_when_needed(retain):
    from CAD2BIM_backend.domain.geometry_runtime import (GeometryBuildFixedRequest,
        GeometryFunctionValidationResult, GeometryFunctionSampleExecution)
    artifacts, operations, _, scope, inspections, _ = _setup()
    runtime = _Runtime()
    schema = {"type": "object", "properties": {"size": {"type": "number", "minimum": 1}},
              "required": ["size"], "additionalProperties": False}
    captured = []
    async def validate(request):
        captured.append(request)
        geometry = _geometry()
        return GeometryFunctionValidationResult(sample_geometry=geometry,
            sample_geometries=(geometry,) * len(request.sample_parameters) if request.retain_sample_geometries else (),
            sample_executions=tuple(GeometryFunctionSampleExecution(
                output_sha256=geometry.sha256, geometry_summary=geometry.summary) for _ in request.sample_parameters))
    runtime.validate_function = validate
    cases = tuple(GeometryPreviewCase(case_key=f"sample_{index}", parameters={"size": index},
        expectation=GeometryCaseExpectation()) for index in (1, 2))
    outcome = await GeometryPreviewService(None, None, runtime, inspection_service=inspections).preview_cases(
        scope, source=_source("geometry_sample"), build_request=GeometryBuildFixedRequest(source_bundle=b"source"),
        parameter_schema=schema, cases=cases, retain_for_inspection=retain, request_identity="functions")
    assert len(captured) == 1 and captured[0].retain_sample_geometries is retain
    assert outcome.report.checks_passed
    assert [bool(item.inspection_ref) for item in outcome.report.cases] == [retain, retain]
    assert len(operations.published) == int(retain)
    if not retain:
        assert not artifacts.records


@pytest.mark.asyncio
async def test_reconciliation_recovers_checkpointed_cases_without_executing_missing_cases():
    from CAD2BIM_backend.domain.data_runtime import AuthoringExecutionClaim
    from CAD2BIM_backend.domain.preview_cases import PreviewCaseResult, PreviewNoRender
    _, operations, _, scope, inspections, _ = _setup()
    request, cases = _family_cases()
    _family_service(inspections, _Runtime())  # Wire the same exact Family source authorization.
    arguments = dict(source=_source(), dependencies=(), cases=cases, nominal_scenario_key=None,
        request_identity="interrupted-owner", render=PreviewNoRender())
    admission = await inspections.begin_cases(scope, **arguments)
    negative = PreviewCaseResult(case=cases[1], execution_status="completed", expectation_check="pass",
        realization=_execution(ProductRealizationStatus.INFEASIBLE).realization)
    await inspections.save_case_result(scope, admission, negative)
    async def reclaim(scope, operation_id, **kwargs):
        return AuthoringExecutionClaim(operation_id=operation_id, owner="recovery", generation=2, token="b" * 32)
    operations.reclaim_authoring_execution = reclaim
    recovered = await inspections.begin_cases(scope, **arguments)
    assert recovered.replay_report.completion == "interrupted"
    assert recovered.replay_report.cases[0].execution_status == "not_run"
    assert recovered.replay_report.cases[1] == negative
    assert recovered.operation_alias and len(operations.published) == 1


@pytest.mark.parametrize("changed_fact", [None, "float_noise", "display_triangles", "summary", "interface"])
def test_standard_origin_comparison_uses_construction_facts_not_brep_bytes(changed_fact):
    from CAD2BIM_backend.application.preview_case_evaluation import compare_standard_constructions
    from CAD2BIM_backend.domain.preview_cases import PreviewCaseResult
    from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
    result = _execution(ProductRealizationStatus.SOLVED).realization
    invocation = _request().invocation.model_copy(update={"realization_kind": "standard_model"})
    first = PreviewCaseResult(case=FamilyPreviewCase(case_key="nominal", invocation=invocation),
        execution_status="completed", expectation_check="pass", realization=result)
    receipt = result.instance_receipts[0].model_copy(update={"geometry_sha256": "b" * 64})
    if changed_fact == "summary":
        receipt = receipt.model_copy(update={"geometry_summary": receipt.geometry_summary.model_copy(
            update={"volume_m3": receipt.geometry_summary.volume_m3 + 1})})
    elif changed_fact == "interface":
        receipt = receipt.model_copy(update={"interfaces": (receipt.interfaces[0].model_copy(
            update={"local_frame": RigidTransform3D(translation_m=(1, 0, 0))}),)})
    elif changed_fact == "float_noise":
        from math import nextafter
        summary = receipt.geometry_summary
        frame = receipt.interfaces[0].local_frame
        receipt = receipt.model_copy(update={
            "geometry_summary": summary.model_copy(update={
                "volume_m3": nextafter(summary.volume_m3, float("inf"))}),
            "interfaces": (receipt.interfaces[0].model_copy(update={
                "local_frame": frame.model_copy(update={
                    "translation_m": tuple(value + 1e-14 for value in frame.translation_m)})}),),
        })
    elif changed_fact == "display_triangles":
        receipt = receipt.model_copy(update={"geometry_summary": receipt.geometry_summary.model_copy(
            update={"topology": receipt.geometry_summary.topology.model_copy(update={"triangle_count": 123})})})
    second = first.model_copy(update={"case": FamilyPreviewCase(case_key="translated",
        invocation=invocation.model_copy(update={"product_origin": (2, 3, 4)})),
        "realization": result.model_copy(update={"instance_receipts": (receipt,)})})
    diagnostics = compare_standard_constructions((first, second))
    assert bool(diagnostics) is (changed_fact in {"summary", "interface"})
    if diagnostics:
        assert diagnostics[0].code == "STANDARD_MODEL_ORIGIN_DEPENDENCE"


@pytest.mark.parametrize("kind,origin,expected", [
    ("standard_model", (0, 0, 0), "SOLVED"),
    ("standard_model", (2, 3, 4), "SOLVED"),
    ("self_geometry", (0, 0, 0), "SOLVED"),
    ("self_geometry", (0, 0, 0), "INFEASIBLE"),
    ("self_geometry", (0, 0, 0), None),
])
def test_family_focused_report_checks_only_its_declared_cases(kind, origin, expected):
    from CAD2BIM_backend.application.preview_case_evaluation import evaluate_family_case
    from CAD2BIM_backend.domain.preview_cases import PreviewCaseResult
    request = _request()
    case = FamilyPreviewCase(case_key="focused",
        invocation=request.invocation.model_copy(update={"realization_kind": kind, "product_origin": origin}),
        expectation=None if expected is None else FamilyCaseExpectation(
            expected_status=expected, reason_keys=("clearance",) if expected == "INFEASIBLE" else ()))
    realized = _execution(ProductRealizationStatus(expected or "SOLVED")).realization
    check, diagnostics = evaluate_family_case(case, realized)
    result = PreviewCaseResult(case=case, execution_status="completed", expectation_check=check,
        realization=realized, diagnostics=diagnostics)
    report = ProductRealizationPreviewService._case_report(_source(), (), (case,), {0: result}, None)
    assert report.checks_passed is (expected is not None)
    assert bool(report.coverage_gaps) is (expected is None)
    assert report.cases[0].expectation_check == ("pass" if expected is not None else "not_checked")
