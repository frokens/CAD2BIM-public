"""Entry-neutral orchestration for one read-only Product Realization preview."""

from __future__ import annotations

from time import monotonic
import asyncio
import math
from contextlib import aclosing

from pydantic import ValidationError

from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionService
from CAD2BIM_backend.domain.geometry_inspection import InspectionWholeSelection, InspectionResultCoordinates
from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionModelObservation
from CAD2BIM_backend.application.product_family_catalog_service import (
    ProductFamilyCatalogService,
)
from CAD2BIM_backend.application.product_realization_input_service import (
    ProductRealizationInputError,
    ProductRealizationInputService,
)
from CAD2BIM_backend.domain.runtime_execution import PRODUCT_PREVIEW_EXECUTION
from CAD2BIM_backend.domain.geometry_catalog import (
    GeometryCatalogError,
    GeometryPreviewReference,
    ProductRealizationPreviewAuthorization,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    GeometryPreviewInterfacesOverlayRequest,
    GeometryPreviewProfile,
    GeometryPreviewRequestError,
    GeometryRuntimeResourceLimitError,
    GeometryRuntimeResourceDiagnostic,
    GeometryRuntimeError,
    GeometryRuntimeUnavailableError,
    GeometryRuntimeValidationError,
    GeometryRenderedPreviewMetadata,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.product_family import ProductFamilyVersionBundle
from CAD2BIM_backend.domain.preview_cases import (
    FamilyPreviewCase, PreviewCaseBatchOutcome, PreviewCaseDiagnostic, PreviewCaseReport,
    PreviewCaseResult, PreviewCaseReuse, PreviewInspectionReuse, PreviewNoRender,
    PreviewSelectedRender, PreviewSourceSnapshot,
)
from CAD2BIM_backend.application.preview_case_evaluation import (
    evaluate_family_case, case_check_coverage, compare_standard_constructions,
)
from CAD2BIM_backend.domain.product_family_catalog import (
    ProductFamilyCatalogError, ProductFamilyCatalogIntegrityError,
)
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationInvocation,
    ProductRealizationOutputNeed,
    ProductRealizationGeometryExecution,
    ProductRealizationPreviewEvidence,
    ProductRealizationRequest,
    ProductRealizationStatus,
    validate_product_realization_result_integrity,
)
from CAD2BIM_backend.domain.product_realization_preview import (
    ProductRealizationPreviewError,
    ProductRealizationPreviewErrorCode,
    ProductRealizationPreviewOutcome,
    product_realization_render_scene,
)
from CAD2BIM_backend.ports.product_realization_runtime import (
    ProductRealizationRuntimeError,
    ProductRealizationRuntimePort,
    ProductRealizationRuntimeResourceLimitError,
    ProductRealizationRuntimeUnavailableError,
    ProductRealizationRuntimeValidationError,
)


class ProductRealizationPreviewService:
    """Reauthorize, solve once, and return one rendered image."""

    def __init__(
        self,
        family_catalog: ProductFamilyCatalogService,
        inputs: ProductRealizationInputService,
        runtime: ProductRealizationRuntimePort,
        geometry_previews: GeometryPreviewService,
        inspection_service: GeometryInspectionService | None = None,
    ) -> None:
        self._family_catalog = family_catalog
        self._inputs = inputs
        self._runtime = runtime
        self._geometry_previews = geometry_previews
        self._inspection_service = inspection_service

    async def prepare_published_cases(self, scope, family_version_ref, cases):
        family = await self._family_catalog.resolve_exact_version(scope.identity, family_version_ref)
        requests = await self._inputs.load_published_requests(scope, family_version=family,
            invocations=tuple(case.invocation for case in cases), numeric_only=True)
        dependencies = await self._geometry_previews.published_dependency_snapshots(scope,
            tuple(item.geometry_resource_version_id for item in family.geometry_bindings))
        return {"source": PreviewSourceSnapshot(source_kind="family_realization", source_type="published",
                    source_id=family.family.family_id, source_ref=family_version_ref,
                    snapshot_id=family.version.family_version_id, content_digest=family.version.content_digest,
                    title=family.version.display_name),
                "requests": requests, "geometry_bindings": family.geometry_bindings, "dependencies": dependencies,
                "material_loader": self._published_material_loader(scope, family)}

    def _published_material_loader(self, scope, family):
        async def load(requests, prechecks):
            return await self._inputs.load_geometry_materials(scope, family_version=family,
                requests=requests, prechecks=prechecks)
        return load

    async def preview_cases(self, scope, *, source, requests, cases, geometry_bindings=(),
                            dependencies=(), nominal_scenario_key=None, retain_for_inspection=False,
                            render=None, request_identity=None, reference_geometries=(),
                            request_loader=None, family_contract=None, invocations=(), material_loader=None):
        """Evaluate one frozen closure through the existing supervised batch runtime."""
        render = render or PreviewNoRender()
        if (source.source_kind != "family_realization" or not 1 <= len(cases) <= 16
                or any(not isinstance(case, FamilyPreviewCase) for case in cases)):
            raise ValueError("Family Preview requires matching finite cases and prepared requests")
        if requests is not None:
            if len(requests) != len(cases):
                raise ValueError("Family Preview requests must cover every case")
            family_contract = requests[0].family_contract
            invocations = tuple(request.invocation for request in requests)
            self._require_same_requests(requests)
        elif request_loader is None or family_contract is None or len(invocations) != len(cases):
            raise ValueError("Deferred Family inputs require a frozen contract and every normalized invocation")
        if len({case.case_key for case in cases}) != len(cases):
            raise ValueError("Family case keys must be unique")
        cases = tuple(case.model_copy(update={"invocation": invocation})
            for case, invocation in zip(cases, invocations, strict=True))
        selected = set(render.case_keys) if isinstance(render, PreviewSelectedRender) else set()
        if selected - {case.case_key for case in cases}:
            raise ValueError("render selection references a missing case")
        inspections = self._inspection_service
        if retain_for_inspection and inspections is None:
            raise ValueError("Geometry inspection retention is not configured")
        deadline = monotonic() + PRODUCT_PREVIEW_EXECUTION.total_seconds
        async with self._runtime.invocation_scope(PRODUCT_PREVIEW_EXECUTION, deadline=deadline):
            references = (await self._geometry_previews.resolve_references(scope, reference_geometries)
                          if reference_geometries else ())
            admission = (await inspections.begin_cases(scope, source=source, dependencies=dependencies,
                cases=cases, nominal_scenario_key=nominal_scenario_key, request_identity=request_identity,
                render=render, reference_geometries=references)) if retain_for_inspection else None
            if admission is not None and admission.replay_report is not None:
                return await self._observe_case_report(scope, admission.replay_report, render, admission.operation_alias)
            results, images, pending = {}, [], []
            reuse_index = None
            try:
                for index, case in enumerate(cases):
                    request = requests[index] if requests is not None else None
                    if case.reuse is None:
                        pending.append(index)
                        continue
                    reuse_index = index
                    result, loaded = await self._reuse_family_case(scope, source, case, request,
                        geometry_bindings=geometry_bindings)
                    if loaded is not None and references and loaded.manifest.reference_geometries != references:
                        raise ValueError("Saved execution observation must use its frozen display references")
                    result, rendered = await self._render_case(scope, request, result, loaded=loaded,
                        geometry=None, render=render, selected=selected, references=references)
                    results[index] = result
                    if rendered is not None:
                        images.append((case.case_key, rendered))
                    if admission is not None:
                        await inspections.save_case_result(scope, admission, result)
                    reuse_index = None
                if pending:
                    if requests is None:
                        async with self._runtime.execution_scope(PRODUCT_PREVIEW_EXECUTION, deadline=deadline):
                            requests = await request_loader(self._geometry_previews._geometry_runtime)
                        self._require_same_requests(requests)
                        if (len(requests) != len(cases) or requests[0].family_contract != family_contract
                                or tuple(item.invocation for item in requests) != invocations):
                            raise ValueError("Deferred inputs changed the frozen Family contract or invocations")
                    needs = tuple(ProductRealizationOutputNeed.PREVIEW_WORLD
                        if retain_for_inspection or cases[index].case_key in selected or cases[index].expectation is None
                        else (ProductRealizationOutputNeed.SOLVE_ONLY
                            if cases[index].expectation.expected_status is not ProductRealizationStatus.SOLVED
                            else ProductRealizationOutputNeed.LOCAL_FRAGMENTS) for index in pending)
                    async with self._runtime.execution_scope(PRODUCT_PREVIEW_EXECUTION, deadline=deadline):
                        stream = self._case_executions(tuple(requests[index] for index in pending), needs,
                            material_loader=material_loader)
                        ordinal = 0
                        async with aclosing(stream):
                            async for execution in stream:
                                if ordinal >= len(pending):
                                    raise ValueError("Family runtime returned an extra case result")
                                index = pending[ordinal]
                                case, request = cases[index], requests[index]
                                ordinal += 1
                                check, diagnostics = evaluate_family_case(case, execution.realization)
                                result = PreviewCaseResult(case=case, execution_status="completed", expectation_check=check,
                                    realization=execution.realization, geometry_sha256=execution.geometry.sha256 if execution.geometry else None,
                                    geometry_summary=execution.geometry.summary if execution.geometry else None, diagnostics=diagnostics)
                                loaded = None
                                if admission is not None and execution.geometry is not None:
                                    scene = product_realization_render_scene(request, execution.realization,
                                        geometry_sha256=execution.geometry.sha256)
                                    if references:
                                        scene = scene.model_copy(update={"reference_geometries": tuple(item.render_reference for item in references)})
                                    display_context = None
                                    profile = render.profile or GeometryPreviewProfile() if isinstance(render, PreviewSelectedRender) else GeometryPreviewProfile()
                                    try:
                                        scene, display_context = await self._geometry_previews._prepare_connection_context(
                                            profile, scene, external_constraints=request.invocation.external_constraints,
                                            external_checks=execution.realization.external_condition_checks)
                                    except Exception as exc:
                                        result = result.model_copy(update={"render_status": "failed", "diagnostics": (*result.diagnostics,
                                            PreviewCaseDiagnostic(code="PREVIEW_RENDER_FAILED", phase="render",
                                                message=(" ".join(str(exc).split()) or "Connection display could not be prepared.")[:2000]))})
                                    from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionApplicationError
                                    try:
                                        loaded, _ = await inspections.retain_family(scope, admission, family_version=None,
                                            family_version_ref=source.source_ref, source_snapshot=source,
                                            geometry_bindings=geometry_bindings, request=request, execution=execution, scene=scene,
                                            case_key=case.case_key, complete_operation=False, references=references,
                                            display_context=display_context,
                                            reference_inputs=await self._geometry_previews._load_reference_geometries(scope, references))
                                    except GeometryInspectionApplicationError as exc:
                                        result = result.model_copy(update={"diagnostics": (*result.diagnostics,
                                            PreviewCaseDiagnostic(code=exc.code, phase="storage", message=exc.message))})
                                    else:
                                        result = result.model_copy(update={"inspection_ref": inspections.reference(scope, loaded.artifact),
                                            "inspection_artifact": loaded.artifact})
                                result, rendered = await self._render_case(scope, request, result, loaded=loaded,
                                    geometry=execution.geometry, render=render, selected=selected, references=references)
                                results[index] = result
                                if rendered is not None:
                                    images.append((case.case_key, rendered))
                                if admission is not None:
                                    await inspections.save_case_result(scope, admission, result)
                        if ordinal != len(pending):
                            raise ValueError("Family runtime ended before every case result")
            except asyncio.CancelledError:
                report = self._case_report(source, dependencies, cases,
                    results, nominal_scenario_key, interrupted=True, error="The original Preview batch was cancelled.")
                if admission is not None:
                    await asyncio.shield(inspections.finish_case_report(scope, admission, report))
                raise
            except (ProductRealizationRuntimeError, GeometryRuntimeError, ValueError, ValidationError) as exc:
                # A stopped batch has no trustworthy results for the remaining
                # calls. Keep prior cases; never synthesize INFEASIBLE for them.
                if isinstance(exc, ProductRealizationRuntimeValidationError):
                    diagnostic = _runtime_validation_case_diagnostic(exc)
                elif isinstance(exc, ProductRealizationRuntimeResourceLimitError):
                    phase = exc.diagnostic.get("phase") if isinstance(exc.diagnostic, dict) else None
                    diagnostic = PreviewCaseDiagnostic(code="WORKER_RESOURCE_LIMIT", phase=(
                        "output_validation" if phase == "codec" else "execution"),
                        message=_runtime_resource_limit_message(exc))
                else:
                    diagnostic = PreviewCaseDiagnostic(
                        code=getattr(exc, "code", "PREVIEW_EXECUTION_FAILED"), phase="execution",
                        message=(" ".join(str(exc).split()) or "The execution failed.")[:2000])
                failed_reuse = reuse_index is not None and reuse_index not in results
                remaining = [reuse_index] if failed_reuse else [index for index in pending if index not in results]
                if remaining:
                    index = remaining[0]
                    results[index] = PreviewCaseResult(case=cases[index], execution_status="technical_error",
                        execution_source="none" if failed_reuse else "executed",
                        expectation_check="not_checked", diagnostics=(diagnostic,))
                    if admission is not None:
                        await inspections.save_case_result(scope, admission, results[index])
                report = self._case_report(source, dependencies, cases,
                    results, nominal_scenario_key, interrupted=len(results) < len(cases), error=diagnostic.message)
                report = report.model_copy(update={"cross_case_diagnostics": (*report.cross_case_diagnostics,
                    diagnostic)})
            else:
                report = self._case_report(source, dependencies, cases,
                    results, nominal_scenario_key)
            alias = await inspections.finish_case_report(scope, admission, report) if admission is not None else None
            return PreviewCaseBatchOutcome(report, alias, tuple(images))

    @staticmethod
    def _require_same_requests(requests):
        if not requests or any(request.family_contract != requests[0].family_contract
                or request.source_bundle_sha256 != requests[0].source_bundle_sha256
                or request.geometry_bindings != requests[0].geometry_bindings for request in requests):
            raise ValueError("Family cases must share one frozen execution closure")

    async def _case_executions(self, requests, needs, *, material_loader=None):
        """Cases are independent: one numeric check precedes each selected build."""
        for index, (request, need) in enumerate(zip(requests, needs, strict=True)):
            try:
                numeric_need = (ProductRealizationOutputNeed.SOLVE_ONLY if need is ProductRealizationOutputNeed.SOLVE_ONLY
                                else ProductRealizationOutputNeed.INTERFACES)
                values = [execution async for execution in self._runtime.realize_batch((request,), output_need=numeric_need)]
                if len(values) != 1:
                    raise ValueError("Family numeric runtime did not return one complete case")
                checked = values[0].realization
                if (need is ProductRealizationOutputNeed.SOLVE_ONLY or checked.proposal.status is not ProductRealizationStatus.SOLVED
                        or checked.frame_check_status != "PASS"):
                    yield ProductRealizationGeometryExecution(realization=checked, output_need=need,
                        instance_geometries={} if need is ProductRealizationOutputNeed.LOCAL_FRAGMENTS else None)
                    continue
                actual = await material_loader((request,), (checked,)) if material_loader is not None else (request,)
                outputs = [execution async for execution in self._runtime.instantiate_batch(actual, (checked,), output_need=need)]
                if len(outputs) != 1:
                    raise ValueError("Family instantiation runtime did not return one complete case")
                yield outputs[0]
            except (ProductRealizationRuntimeValidationError, ProductRealizationRuntimeUnavailableError) as exc:
                exc.item_index = index
                raise

    async def preview_loaded(self, scope, *, source, request=None, geometry_bindings=(), dependencies=(),
                             retain_for_inspection=False, render_profile=None, request_identity=None,
                             reference_geometries=(), request_loader=None, family_contract=None, invocations=(), material_loader=None):
        """Single draft observation, using the same frozen request as cases."""
        invocation = request.invocation if request is not None else invocations[0]
        async def load_single(runtime):
            nonlocal request
            prepared = await request_loader(runtime)
            request = prepared[0]
            return prepared
        outcome = await self.preview_cases(scope, source=source, requests=(request,) if request is not None else None,
            request_loader=load_single if request is None else None, family_contract=family_contract, invocations=invocations,
            material_loader=material_loader,
            cases=(FamilyPreviewCase(case_key="observation", invocation=invocation),),
            geometry_bindings=geometry_bindings, dependencies=dependencies,
            retain_for_inspection=retain_for_inspection, request_identity=request_identity,
            render=PreviewSelectedRender(case_keys=("observation",), profile=render_profile),
            reference_geometries=reference_geometries)
        item = outcome.report.cases[0]
        if item.realization is None:
            raise ProductRealizationPreviewError(ProductRealizationPreviewErrorCode.RUNTIME_EXECUTION_FAILED,
                item.diagnostics[0].message if item.diagnostics else "The single Preview did not complete",
                diagnostic={"operation_alias": outcome.operation_alias} if outcome.operation_alias else None)
        image = dict(outcome.images).get(item.case.case_key)
        preview = None
        if image is not None:
            if item.inspection_ref is not None:
                loaded = await self._inspection_service.read(scope, item.inspection_ref)
                scene, references = loaded.manifest.scene, loaded.manifest.reference_geometries
            else:
                scene = product_realization_render_scene(request, item.realization, geometry_sha256=item.geometry_sha256)
                references = await self._geometry_previews.resolve_references(scope, reference_geometries) if reference_geometries else ()
                if references:
                    scene = scene.model_copy(update={"reference_geometries": tuple(value.render_reference for value in references)})
            preview = ProductRealizationPreviewAuthorization(family_version_ref=source.source_ref,
                title=source.title, content=image.payload, size_bytes=len(image.payload), content_sha256=image.sha256,
                frame_check_status=item.realization.frame_check_status,
                render_metadata=GeometryRenderedPreviewMetadata(scene_sha256=sha256_digest(scene),
                    coordinate_space=scene.coordinate_space, resolved_panels=image.resolved_panels,
                    display_quality_warnings=image.display_quality_warnings), interface_overlays=scene.interface_overlays,
                reference_geometries=references)
        from CAD2BIM_backend.domain.geometry_inspection import GeometryInspectionDiagnostic
        diagnostics = tuple(GeometryInspectionDiagnostic(code=value.code,
            phase="publication" if value.phase == "storage" else "render", message=value.message,
            action="Inspect the saved Operation report before requesting another retained execution." if value.phase == "storage" else
                "Use the saved inspection reference to retry observation." if item.inspection_ref else
                "Run a new observation when an image is needed; the temporary shape was not retained.")
            for value in item.diagnostics if value.phase in {"render", "storage"})
        return ProductRealizationPreviewOutcome(realization=item.realization, preview=preview,
            operation_alias=outcome.operation_alias, inspection_ref=item.inspection_ref, diagnostics=diagnostics)

    @staticmethod
    def _case_report(source, dependencies, cases, results, nominal_scenario_key,
                     *, interrupted=False, error="Batch execution stopped before this case."):
        complete = tuple(results.get(index) or PreviewCaseResult(case=case, execution_status="not_run",
            execution_source="none", expectation_check="not_checked", diagnostics=(PreviewCaseDiagnostic(
                code="CASE_NOT_RUN", phase="interrupted", message=(" ".join(error.split()) or "Case was not run.")[:2000]),))
            for index, case in enumerate(cases))
        return PreviewCaseReport(source=source, dependencies=dependencies, cases=complete,
            nominal_scenario_key=nominal_scenario_key, completion="interrupted" if interrupted else "complete",
            coverage_gaps=case_check_coverage(cases),
            cross_case_diagnostics=compare_standard_constructions(complete))

    async def _reuse_family_case(self, scope, source, case, request, *, geometry_bindings=()):
        if self._inspection_service is None:
            raise ValueError("Saved Preview evidence is unavailable")
        service, loaded = self._inspection_service, None
        if isinstance(case.reuse, PreviewCaseReuse):
            report = await service.read_case_report(scope, case.reuse.operation_alias)
            original = next((item for item in report.cases if item.case.case_key == case.reuse.case_key), None)
            if original is None or original.execution_status != "completed":
                raise ValueError("Saved case has no complete execution result")
            original_source, result = report.source, original.realization
            original_input = original.case.invocation
            if original.inspection_artifact is not None:
                loaded = await service.load_case_geometry(scope, report, original.case.case_key)
        elif isinstance(case.reuse, PreviewInspectionReuse):
            loaded = await service.read(scope, case.reuse.inspection_ref)
            original_source, result = loaded.manifest.source_snapshot, loaded.manifest.realization
            original_input = getattr(loaded.manifest.source, "invocation", None)
        else:
            raise ValueError("Unknown saved execution source")
        if (original_source is None or original_source.source_id != source.source_id
                or original_source.content_digest != source.content_digest or original_input != case.invocation
                or (result is not None and result.invocation != case.invocation)
                or result is None or (request is not None and result.source_bundle_sha256 != request.source_bundle_sha256)):
            raise ValueError("Saved execution does not match the frozen source and invocation")
        from CAD2BIM_backend.domain.product_realization import product_realization_binding_digest, product_geometry_binding_digest
        binding_digest = (product_realization_binding_digest(request) if request is not None else
            product_geometry_binding_digest((item.binding_key, item.expected_resource_kind, item.geometry_content_digest)
                for item in geometry_bindings))
        if result.binding_digest != binding_digest:
            raise ValueError("Saved execution has a different exact dependency closure")
        if request is not None:
            validate_product_realization_result_integrity(request, result)
        check, diagnostics = evaluate_family_case(case, result)
        return PreviewCaseResult(case=case, execution_status="completed", execution_source="reused",
            expectation_check=check, realization=result, diagnostics=diagnostics,
            inspection_ref=service.reference(scope, loaded.artifact) if loaded else None,
            inspection_artifact=loaded.artifact if loaded else None,
            geometry_sha256=loaded.geometry.sha256 if loaded else None,
            geometry_summary=loaded.geometry.summary if loaded else None), loaded

    async def _render_case(self, scope, request, result, *, loaded, geometry, render, selected, references=()):
        if result.render_status == "failed":
            return result, None
        if result.case.case_key not in selected:
            return result, None
        if result.realization.execution_stage != "instantiated" or result.realization.proposal.status is not ProductRealizationStatus.SOLVED:
            return result.model_copy(update={"render_status": "no_geometry"}), None
        try:
            if loaded is not None:
                profile = render.profile
                if profile is not None and result.execution_source != "reused":
                    profile = profile.model_copy(update={"connection_context": None})
                view = await self._geometry_previews._observe_inspection(scope, loaded,
                    InspectionWholeSelection(), InspectionResultCoordinates(),
                    GeometryInspectionModelObservation(render_profile=profile))
                if view.preview is None:
                    raise ValueError(view.diagnostics[0].message)
                image = view.preview
            else:
                if geometry is None:
                    raise ValueError("The same execution did not retain a world geometry for rendering")
                scene = product_realization_render_scene(request, result.realization, geometry_sha256=geometry.sha256)
                if references:
                    scene = scene.model_copy(update={"reference_geometries": tuple(value.render_reference for value in references)})
                image = await self._geometry_previews._render_canonical(geometry, render.profile or GeometryPreviewProfile(), scene,
                    await self._geometry_previews._load_reference_geometries(scope, references),
                    external_constraints=request.invocation.external_constraints,
                    external_checks=result.realization.external_condition_checks)
            return result.model_copy(update={"render_status": "rendered"}), image
        except Exception as exc:
            diagnostic = PreviewCaseDiagnostic(code="PREVIEW_RENDER_FAILED", phase="render",
                message=(" ".join(str(exc).split()) or "Rendering failed; execution facts remain valid.")[:2000])
            return result.model_copy(update={"render_status": "failed", "diagnostics": (*result.diagnostics, diagnostic)}), None

    async def _observe_case_report(self, scope, report, render, operation_alias):
        if not isinstance(render, PreviewSelectedRender):
            return PreviewCaseBatchOutcome(report, operation_alias)
        return await self._geometry_previews.observe_case_report(scope, report, render, operation_alias,
            inherit_connection_context=True)

    async def build(
        self,
        scope: InvocationScope,
        *,
        family_version: ProductFamilyVersionBundle,
        invocation: ProductRealizationInvocation,
        render_profile: GeometryPreviewProfile | None = None,
        deadline: float | None = None,
    ) -> tuple[
        ProductRealizationRequest,
        ProductRealizationGeometryExecution,
        ProductRealizationPreviewOutcome | None,
    ]:
        """Build exact inputs for engineering inspection, optionally publishing a preview.

        The request and local instance geometries belong to this same supervised
        execution. This application entry creates no IFC or product fact and does
        not accept caller-supplied requests, receipts, or geometry executions.
        """
        maximum_deadline = monotonic() + PRODUCT_PREVIEW_EXECUTION.total_seconds
        deadline = maximum_deadline if deadline is None else min(deadline, maximum_deadline)
        if render_profile is not None:
            self._validate_profile(render_profile)
        async with self._runtime.invocation_scope(PRODUCT_PREVIEW_EXECUTION, deadline=deadline):
            exact_family, family_version_ref = await self._reauthorize_family(scope, family_version)
            request = await self._load_request(scope, exact_family, invocation)
            async with self._runtime.execution_scope(PRODUCT_PREVIEW_EXECUTION, deadline=deadline):
                execution = await self._build_loaded(request, include_instances=True,
                    material_loader=self._published_material_loader(scope, exact_family))
                preview = None
                if render_profile is not None:
                    preview = await self._preview_execution(
                        scope, exact_family, family_version_ref, request,
                        execution, render_profile,
                    )
                return request, execution, preview

    async def preview(
        self,
        scope: InvocationScope,
        *,
        family_version: ProductFamilyVersionBundle,
        invocation: ProductRealizationInvocation,
        render_profile: GeometryPreviewProfile | None = None,
        deadline: float | None = None,
        reference_geometries: tuple[GeometryPreviewReference, ...] = (),
        retain_for_inspection: bool = False, request_identity: str | None = None,
    ) -> ProductRealizationPreviewOutcome:
        """Execute one exact realization without creating any product or IFC fact."""

        maximum_deadline = monotonic() + PRODUCT_PREVIEW_EXECUTION.total_seconds
        deadline = maximum_deadline if deadline is None else min(deadline, maximum_deadline)
        async with self._runtime.invocation_scope(PRODUCT_PREVIEW_EXECUTION, deadline=deadline):
            return await self._preview(
                scope, family_version=family_version, invocation=invocation,
                render_profile=render_profile, deadline=deadline,
                reference_geometries=reference_geometries,
                retain_for_inspection=retain_for_inspection, request_identity=request_identity,
            )

    async def _preview(
        self, scope: InvocationScope, *, family_version: ProductFamilyVersionBundle,
        invocation: ProductRealizationInvocation,
        render_profile: GeometryPreviewProfile | None, deadline: float,
        reference_geometries: tuple[GeometryPreviewReference, ...] = (),
        retain_for_inspection: bool = False, request_identity: str | None = None,
    ) -> ProductRealizationPreviewOutcome:
        profile = render_profile or GeometryPreviewProfile()
        self._validate_profile(profile)
        exact_family, family_version_ref = await self._reauthorize_family(
            scope,
            family_version,
        )
        try:
            references = await self._geometry_previews.resolve_references(scope, reference_geometries) if reference_geometries else ()
        except GeometryCatalogError as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.INPUT_LOADING_FAILED,
                f"Preview reference geometry could not be loaded: {exc}",
            ) from exc
        except Exception as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.INPUT_LOADING_FAILED,
                "Exact Product Realization inputs failed integrity validation.",
            ) from exc
        request = await self._load_request(scope, exact_family, invocation)

        if retain_for_inspection:
            source = PreviewSourceSnapshot(source_kind="family_realization", source_type="published",
                source_id=exact_family.family.family_id, source_ref=family_version_ref,
                snapshot_id=exact_family.version.family_version_id,
                content_digest=exact_family.version.content_digest, title=exact_family.version.display_name)
            return await self.preview_loaded(scope, source=source, request=request,
                geometry_bindings=exact_family.geometry_bindings, retain_for_inspection=True,
                material_loader=self._published_material_loader(scope, exact_family),
                render_profile=profile, request_identity=request_identity,
                reference_geometries=reference_geometries)

        async with self._runtime.execution_scope(PRODUCT_PREVIEW_EXECUTION, deadline=deadline):
            return await self._preview_loaded(
                scope, exact_family, family_version_ref, request, profile,
                references,
            )

    @staticmethod
    def _validate_profile(profile: GeometryPreviewProfile) -> None:
        overlay = profile.presentation.overlay if profile.presentation is not None else None
        if (
            isinstance(overlay, GeometryPreviewInterfacesOverlayRequest)
            and "temporary_interfaces" in overlay.model_fields_set
        ):
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.INPUT_LOADING_FAILED,
                "Product Realization preview does not accept temporary model interfaces.",
            )

    async def _load_request(self, scope, exact_family, invocation) -> ProductRealizationRequest:
        try:
            return await self._inputs.load_published_request(
                scope,
                family_version=exact_family,
                invocation=invocation,
                numeric_only=True,
            )
        except ProductRealizationInputError as exc:
            if exc.code in {"GEOMETRY_ASSET_INTEGRITY_FAILED", "PUBLISHED_SOURCE_INTEGRITY_FAILED"}:
                raise ProductRealizationPreviewError(
                    ProductRealizationPreviewErrorCode.ARTIFACT_INTEGRITY_FAILED,
                    "Exact Product Realization source assets failed integrity validation.",
                ) from exc
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.INPUT_LOADING_FAILED,
                exc.public_message or "Exact Product Realization inputs could not be loaded.",
                retryable=exc.retryable,
            ) from exc
        except Exception as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.INPUT_LOADING_FAILED,
                "Exact Product Realization inputs failed integrity validation.",
            ) from exc

    async def _preview_loaded(
        self, scope, exact_family, family_version_ref, request, profile, references=(),
    ) -> ProductRealizationPreviewOutcome:
        execution = await self._build_loaded(request, material_loader=self._published_material_loader(scope, exact_family))
        return await self._preview_execution(
            scope, exact_family, family_version_ref, request, execution, profile, references,
        )

    async def _build_loaded(
        self, request: ProductRealizationRequest, *, include_instances: bool = False, material_loader=None,
    ) -> ProductRealizationGeometryExecution:
        execution = await self._execute_once(request, include_instances=include_instances, material_loader=material_loader)
        realization = execution.realization
        if realization.execution_stage == "instantiated" and realization.proposal.status is ProductRealizationStatus.SOLVED and execution.geometry is None:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.RESULT_INTEGRITY_FAILED,
                "A solved Product Realization did not return canonical geometry.",
            )
        return execution

    async def _preview_execution(
        self, scope, exact_family, family_version_ref, request, execution, profile, references=(),
    ) -> ProductRealizationPreviewOutcome:
        realization = execution.realization
        if realization.execution_stage != "instantiated" or realization.proposal.status is not ProductRealizationStatus.SOLVED:
            return ProductRealizationPreviewOutcome(realization=realization)

        geometry = execution.geometry
        assert geometry is not None  # Validated by the shared build step.
        try:
            scene = product_realization_render_scene(
                request,
                realization,
                geometry_sha256=geometry.sha256,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.RESULT_INTEGRITY_FAILED,
                "The Product Realization world facts could not form an exact render scene.",
            ) from exc
        try:
            preview = await self._geometry_previews.preview_product_realization(
                scope,
                family_version_ref=family_version_ref,
                canonical_geometry=geometry,
                family_version_content_digest=exact_family.version.content_digest,
                contract_digest=exact_family.version.contract_digest,
                source_bundle_sha256=realization.source_bundle_sha256,
                binding_digest=realization.binding_digest,
                frame_check_status=realization.frame_check_status,
                scene=scene,
                product_realization=ProductRealizationPreviewEvidence(
                    contract_digest=request.family_contract_digest,
                    invocation=request.invocation,
                    realization=realization,
                    geometry_sha256=geometry.sha256,
                ),
                render_profile=profile,
                reference_geometries=references,
            )
        except GeometryPreviewRequestError as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.PREVIEW_REQUEST_INVALID,
                str(exc),
            ) from exc
        except GeometryRuntimeError as exc:
            raise _geometry_runtime_preview_error(exc, "render") from exc
        except GeometryCatalogError as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.PREVIEW_AUTHORIZATION_FAILED,
                "The solved Product Realization preview failed exact authorization.",
            ) from exc
        except Exception as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.RUNTIME_EXECUTION_FAILED,
                "The solved Product Realization preview could not be rendered or reopened.",
            ) from exc
        return ProductRealizationPreviewOutcome(
            realization=realization,
            preview=preview,
        )

    async def _reauthorize_family(
        self,
        scope: InvocationScope,
        supplied: ProductFamilyVersionBundle,
    ) -> tuple[ProductFamilyVersionBundle, str]:
        try:
            family_version_ref = self._family_catalog.version_ref(scope.identity, supplied)
            reopened = await self._family_catalog.resolve_exact_version(
                scope.identity,
                family_version_ref,
            )
        except ProductFamilyCatalogIntegrityError as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.ARTIFACT_INTEGRITY_FAILED,
                "The exact ProductFamilyVersion failed stored asset integrity validation.",
            ) from exc
        except ProductFamilyCatalogError as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.FAMILY_REAUTHORIZATION_FAILED,
                "The exact ProductFamilyVersion is not authorized in this scope.",
            ) from exc
        except (ConnectionError, TimeoutError) as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.FAMILY_REAUTHORIZATION_FAILED,
                "The exact ProductFamilyVersion could not be reopened.",
                retryable=True,
            ) from exc
        except Exception as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.FAMILY_REAUTHORIZATION_FAILED,
                "The exact ProductFamilyVersion could not be reopened due to an internal error.",
            ) from exc
        if reopened != supplied:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.FAMILY_REAUTHORIZATION_FAILED,
                "The reopened ProductFamilyVersion disagrees with the supplied exact version.",
            )
        return reopened, family_version_ref

    async def _execute_once(self, request, *, include_instances: bool = False, material_loader=None) -> ProductRealizationGeometryExecution:
        try:
            values = [value async for value in self._case_executions((request,), (ProductRealizationOutputNeed.PREVIEW_WORLD,),
                material_loader=material_loader)]
            return values[0] if include_instances else values[0].model_copy(update={"instance_geometries": None})
        except ProductRealizationRuntimeValidationError as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.RUNTIME_EXECUTION_FAILED,
                _runtime_validation_message(exc),
            ) from exc
        except ProductRealizationRuntimeResourceLimitError as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.RUNTIME_EXECUTION_FAILED,
                _runtime_resource_limit_message(exc),
                diagnostic=exc.diagnostic,
            ) from exc
        except ProductRealizationRuntimeUnavailableError as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.RUNTIME_EXECUTION_FAILED,
                "The Product Realization runtime is temporarily unavailable.",
                retryable=exc.retryable,
                diagnostic=exc.diagnostic,
            ) from exc
        except ProductRealizationRuntimeError as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.RUNTIME_EXECUTION_FAILED,
                "The Product Realization runtime rejected the exact request.",
            ) from exc
        except (TypeError, ValueError, ValidationError) as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.RESULT_INTEGRITY_FAILED,
                "The Product Realization runtime returned an invalid typed execution.",
            ) from exc
        except Exception as exc:
            raise ProductRealizationPreviewError(
                ProductRealizationPreviewErrorCode.RUNTIME_EXECUTION_FAILED,
                "The Product Realization runtime failed.",
                retryable=True,
            ) from exc

def _geometry_runtime_preview_error(
    error: GeometryRuntimeError, stage: str,
) -> ProductRealizationPreviewError:
    diagnostic = getattr(error, "diagnostic", None)
    if diagnostic is not None and not isinstance(diagnostic, dict):
        diagnostic = diagnostic.model_dump(mode="json", exclude_none=True)
    if isinstance(error, GeometryRuntimeResourceLimitError):
        message = f"Product Realization {stage} exceeded its fixed resource limit."
        if error.diagnostic is not None:
            message += error.diagnostic.limit_summary()
        message += " This does not establish invalid geometry."
        retryable = False
    elif isinstance(error, GeometryRuntimeUnavailableError):
        message = f"Product Realization {stage} runtime is unavailable."
        retryable = error.retryable
    elif isinstance(error, GeometryRuntimeValidationError):
        message = f"Product Realization {stage} geometry failed validation."
        retryable = False
    else:
        message = f"Product Realization {stage} runtime rejected the exact request."
        retryable = False
    return ProductRealizationPreviewError(
        ProductRealizationPreviewErrorCode.RUNTIME_EXECUTION_FAILED,
        message, retryable=retryable, diagnostic=diagnostic,
    )


def _runtime_resource_limit_message(exc: ProductRealizationRuntimeResourceLimitError) -> str:
    """Expose measured limits, not child exception text or private identities."""
    message = "The Product Realization exceeded its fixed resource limit."
    diagnostic = exc.diagnostic if isinstance(exc.diagnostic, dict) else {}
    if diagnostic.get("phase") == "codec":
        message = "The solver returned results, but backend geometry normalization did not complete."
        try:
            codec = GeometryRuntimeResourceDiagnostic.model_validate(diagnostic.get("codec_diagnostic", {}))
        except ValidationError:
            pass
        else:
            message += codec.limit_summary()
        return message + " Unconfirmed cases are not accepted results; this interruption does not establish invalid geometry or infeasibility. Resolve the runtime limit before retrying."
    if diagnostic.get("phase") == "startup":
        message = "The execution worker did not become ready; no case was dispatched."
        limit, observed = diagnostic.get("limit"), diagnostic.get("observed")
        if all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) and value >= 0 for value in (limit, observed)):
            message += f" Startup wall-clock seconds: {observed:.3f} observed, {limit:.3f} limit."
        return message + " Restore worker readiness; changing authored geometry is not justified by this failure."
    dimension = diagnostic.get("resource_kind")
    if dimension not in {"cpu", "wall"}:
        return message
    scope = diagnostic.get("budget_scope")
    if scope not in {"invocation", "process"}:
        return message
    limit, observed = diagnostic.get("limit"), diagnostic.get("observed")
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value < 0 for value in (limit, observed)):
        return message
    scope_label = "this case" if scope == "invocation" else "the worker process"
    unit = "CPU seconds" if dimension == "cpu" else "wall-clock seconds"
    message += f" Scope: {scope_label}; {unit}: {observed:.3f} observed, {limit:.3f} limit."
    if dimension == "cpu":
        message += " CPU time includes all native threads; reducing the batch size does not raise a case limit."
    return message


def _runtime_validation_message(exc: ProductRealizationRuntimeValidationError) -> str:
    """Project a trusted child diagnostic without exposing host-side identities."""

    allowed_codes = {
        "REALIZATION_SOURCE_INVALID",
        "REALIZATION_BINDING_INVALID",
        "REALIZATION_EXECUTION_FAILED",
        "REALIZATION_OUTPUT_INVALID",
    }
    allowed_phases = {"code_validation", "input_validation", "execution", "output_validation"}
    if exc.code not in allowed_codes or exc.phase not in allowed_phases:
        return "The Product Realization runtime rejected the exact request."
    prefix = f"runtime_code={exc.code}; runtime_phase={exc.phase}"
    if exc.code == "REALIZATION_BINDING_INVALID":
        return prefix
    diagnostic = " ".join(exc.message.split())[:320]
    if exc.code == "REALIZATION_OUTPUT_INVALID" and exc.phase == "output_validation":
        reason = exc.diagnostic.get("reason") if isinstance(exc.diagnostic, dict) else None
        if isinstance(reason, str):
            # Only project recognized geometry facts, never the raw child text:
            # it may also carry a private path or process identity.
            if "OCCT BREP geometry is invalid" in reason:
                diagnostic = "Preview geometry is invalid; inspect or revise the authored shape before retrying."
            elif "OCCT BREP payload could not be reopened as non-empty geometry" in reason:
                diagnostic = "Preview geometry could not be reopened as non-empty BREP; inspect the authored shape."
            else:
                diagnostic = "Product Realization output failed geometry verification."
    return f"{prefix}; diagnostic={diagnostic}" if diagnostic else prefix


def _runtime_validation_case_diagnostic(
    exc: ProductRealizationRuntimeValidationError,
) -> PreviewCaseDiagnostic:
    allowed_codes = {
        "REALIZATION_SOURCE_INVALID", "REALIZATION_BINDING_INVALID",
        "REALIZATION_EXECUTION_FAILED", "REALIZATION_OUTPUT_INVALID",
    }
    phase = (exc.phase if exc.phase in {"execution", "output_validation"}
             else "input" if exc.phase in {"code_validation", "input_validation"} else "execution")
    return PreviewCaseDiagnostic(
        code=exc.code if exc.code in allowed_codes else "PREVIEW_EXECUTION_FAILED",
        phase=phase,
        message=_runtime_validation_message(exc),
    )


__all__ = ["ProductRealizationPreviewService"]
