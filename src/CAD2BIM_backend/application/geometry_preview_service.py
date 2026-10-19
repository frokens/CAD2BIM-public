"""One-call geometry previews from exact resources and the shared runtime."""

from __future__ import annotations

from CAD2BIM_backend.application.geometry_resource_service import (
    GeometryResourceBuildError, build_geometry_function_sample, reopen_exact_canonical_geometry,
)

from CAD2BIM_backend.domain.runtime_execution import GEOMETRY_PREVIEW_EXECUTION

import hashlib
import asyncio
import json
import math
from collections.abc import Mapping

from pydantic import JsonValue, TypeAdapter, ValidationError

from CAD2BIM_backend.application.geometry_catalog_service import (
    GeometryCatalogService,
)
from CAD2BIM_backend.application.geometry_resource_service import (
    GeometryResourceService,
    normalize_geometry_function_sample_parameters,
)
from CAD2BIM_backend.application.product_family_catalog_service import (
    ProductFamilyCatalogService,
)
from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionService
from CAD2BIM_backend.application.preview_case_evaluation import case_check_coverage
from CAD2BIM_backend.domain.geometry_inspection import (
    GeometryInspectionDiagnostic, InspectionWholeSelection, InspectionResultCoordinates,
    InspectionComponentSelection, InspectionFragmentSelection, InspectionFragmentLocalCoordinates,
    GeometrySectionObservation,
)
from CAD2BIM_backend.domain.geometry_inspection_asset import (
    GeometryInspectionModelObservation, GeometryInspectionPreviewOutcome,
)
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.geometry_catalog import (
    GeometryCatalogIntegrityError,
    GeometryPreviewAuthorization,
    GeometryPreviewProfile,
    GeometryPreviewReference,
    GeometryPreviewReferenceSnapshot,
    GeometryPreviewUnavailableError,
    ProductRealizationPreviewAuthorization,
)
from CAD2BIM_backend.domain.geometry_interface import (
    GeometryModelInterfaces, CircularInterfaceProfile, resolve_fixed_geometry_interfaces,
)
from CAD2BIM_backend.domain.geometry_frame import (
    RigidTransform3D, compose_rigid_transform, inverse_rigid_transform,
    rigid_transform_from_axis_reference,
)
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
    GeometryResourceVersionBundle,
    geometry_resource_inputs,
)
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyVersionAssetRole,
    ProductFamilyVersionBundle,
)
from CAD2BIM_backend.domain.product_realization import ProductRealizationPreviewEvidence
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometryPreview,
    GeometryPreviewInterfacesOverlayRequest,
    GeometryRenderRequest,
    MAX_GEOMETRY_RENDER_REFERENCE_BYTES,
    GeometryRenderedPreviewMetadata,
    GeometryRenderCoordinateSpace,
    GeometryRenderInterfaceMarkerKind,
    GeometryRenderInterfaceOverlay,
    GeometryRenderScene,
    GeometryRenderPlacement,
    GeometryNormalizeRequest,
    GeometryStoredPreviewMetadata,
    GeometryBuildFixedRequest, GeometryValidateFunctionRequest, _PreparedGeometryFunctionParameters,
    GeometryRuntimeError,
    GeometryRuntimeResourceLimitError, GeometryRuntimeUnavailableError,
    GeometryRuntimeValidationError,
    GeometryPreviewRequestError, GeometryPreviewPipeSegment,
    GeometryPreviewPipesConnectionContext, GeometryRenderStraightProfileOverlay,
    GeometryBuildDisplayContextRequest,
)
from CAD2BIM_backend.domain.preview_cases import (
    GeometryPreviewCase, PreviewCaseBatchOutcome, PreviewCaseDiagnostic, PreviewCaseReport,
    PreviewCaseResult, PreviewCaseReuse, PreviewInspectionReuse, PreviewNoRender, PreviewSelectedRender,
    PreviewSourceSnapshot,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.ports.geometry_runtime import GeometryRuntimePort


_MAX_PARAMETERS_BYTES = 16 * 1024
_PRODUCT_PREVIEW_TARGET_KIND = "product_realization_preview"
_GEOMETRY_TARGET_KIND = "geometry_resource_preview"
_INTERFACES_ADAPTER = TypeAdapter(GeometryModelInterfaces)


class GeometryPreviewService:
    """Render or reuse a preview without making it geometry authority."""

    def __init__(
        self,
        catalog: GeometryCatalogService,
        resources: GeometryResourceService,
        geometry_runtime: GeometryRuntimePort,
        family_catalog: ProductFamilyCatalogService | None = None,
        inspection_service: GeometryInspectionService | None = None,
        family_assets=None,
    ) -> None:
        self._catalog = catalog
        self._resources = resources
        self._geometry_runtime = geometry_runtime
        self._family_catalog = family_catalog
        self._inspection_service = inspection_service
        self._family_assets = family_assets

    async def preview_family_standard_model(self, scope, family_version_ref, observation):
        from CAD2BIM_backend.domain.product_realization_preview import FamilyStandardModelPreviewOutcome
        from CAD2BIM_backend.domain.geometry_runtime import GeometrySectionRequest
        bundle = await self._require_family_catalog().resolve_exact_version(
            scope.identity, family_version_ref, verify_assets=False,
        )
        standard = bundle.version.family_contract.standard_model
        if standard is None:
            raise GeometryPreviewUnavailableError("This exact Family has no formal standard model")
        if self._family_assets is None:
            raise GeometryPreviewUnavailableError("Formal Family asset reading is unavailable")
        manifest, content = await self._family_assets.standard_model_assets(bundle)
        geometry = CanonicalGeometry(format=CanonicalGeometryFormat.OCCT_BREP_V1,
            content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type, payload=content,
            sha256=manifest.geometry_sha256, summary=manifest.geometry_summary)
        common = {"family_version_ref": family_version_ref,
            "public_parameters_json": canonical_json(standard.public_parameters)}
        try:
            if isinstance(observation, GeometrySectionObservation):
                section = await self._geometry_runtime.section(GeometrySectionRequest(
                    geometry=geometry, selection=InspectionWholeSelection(), coordinates=InspectionResultCoordinates(),
                    observation=observation))
                return FamilyStandardModelPreviewOutcome(**common, section=section)
            scene = GeometryRenderScene(coordinate_space=GeometryRenderCoordinateSpace.GEOMETRY_COORDINATES,
                geometry_sha256=geometry.sha256)
            preview = await self._render_canonical(geometry, observation.render_profile or GeometryPreviewProfile(), scene)
            return FamilyStandardModelPreviewOutcome(**common, preview=preview)
        finally:
            self._geometry_runtime.release_execution()

    async def prepare_published_cases(self, scope, resource_version_ref):
        """Load one exact published closure; constructing this input executes no author."""
        bundle = await self._catalog.resolve_exact_version(scope.identity, resource_version_ref)
        source = PreviewSourceSnapshot(source_kind="geometry_sample", source_type="published",
            source_id=bundle.resource.resource_id, source_ref=resource_version_ref,
            snapshot_id=bundle.version.resource_version_id, content_digest=bundle.version.content_digest,
            title=bundle.resource.display_name)
        if bundle.resource.resource_kind is GeometryResourceKind.MAPPED_MODEL:
            asset, payload = await self._resources.read_exact_asset(scope.identity,
                resource_id=bundle.resource.resource_id, resource_version_id=bundle.version.resource_version_id,
                asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY)
            geometry = await self._reopen_canonical_asset(asset.asset_format, payload)
            if geometry.sha256 != asset.sha256:
                raise GeometryCatalogIntegrityError("Published canonical Geometry bytes changed")
            return {"source": source, "build_request": geometry, "interfaces": self._manifest_interfaces(bundle)}
        source_asset, content = await self._resources.read_exact_asset(scope.identity,
            resource_id=bundle.resource.resource_id, resource_version_id=bundle.version.resource_version_id,
            asset_role=GeometryResourceAssetRole.SOURCE_BUNDLE)
        _, schema = await self._resources.read_exact_asset(scope.identity,
            resource_id=bundle.resource.resource_id, resource_version_id=bundle.version.resource_version_id,
            asset_role=GeometryResourceAssetRole.PARAMETER_SCHEMA)
        inputs = geometry_resource_inputs(bundle.version.manifest)
        async def load_materials(source):
            return source.model_copy(update={"geometry_inputs": await self._resources.load_build_inputs(scope.identity, inputs) if inputs else ()})
        dependencies = await self.published_dependency_snapshots(scope,
            tuple(item.resource_version_id for item in inputs))
        return {"source": source,
            "build_request": GeometryBuildFixedRequest(source_bundle=content),
            "material_loader": load_materials,
            "parameter_schema": json.loads(schema), "dependencies": dependencies}

    async def published_dependency_snapshots(self, scope, version_ids):
        snapshots = []
        for version_id in dict.fromkeys(version_ids):
            bundle = await self._resources.get_version_by_id(scope.identity, resource_version_id=version_id)
            if bundle is None:
                raise GeometryCatalogIntegrityError("A pinned Geometry dependency is not visible")
            snapshots.append(PreviewSourceSnapshot(source_kind="geometry_sample", source_type="published",
                source_id=bundle.resource.resource_id, source_ref=self._catalog.version_ref(scope.identity, bundle),
                snapshot_id=bundle.version.resource_version_id, content_digest=bundle.version.content_digest,
                title=bundle.resource.display_name))
        return tuple(snapshots)

    async def preview_cases(self, scope, *, source, cases, build_request, parameter_schema=None,
                            dependencies=(), retain_for_inspection=False, render=None, request_identity=None,
                            interfaces=(), datum_transform=None, reference_geometries=(), material_loader=None):
        """Build finite Geometry samples once and optionally keep their same bytes."""
        render = render or PreviewNoRender()
        if (source.source_kind != "geometry_sample" or not 1 <= len(cases) <= 8
                or any(not isinstance(case, GeometryPreviewCase) for case in cases)
                or len({case.case_key for case in cases}) != len(cases)):
            raise ValueError("Geometry Preview needs between 1 and 8 unique Geometry cases")
        if isinstance(build_request, GeometryValidateFunctionRequest):
            parameter_schema = build_request.parameter_schema
        selected = set(render.case_keys) if isinstance(render, PreviewSelectedRender) else set()
        if selected - {case.case_key for case in cases}:
            raise ValueError("render selection references a missing Geometry case")
        inspections = self._inspection_service
        if retain_for_inspection and inspections is None:
            raise ValueError("Geometry inspection retention is not configured")
        async with self._geometry_runtime.invocation_scope(GEOMETRY_PREVIEW_EXECUTION), self._geometry_runtime.execution_scope(GEOMETRY_PREVIEW_EXECUTION):
            references = await self.resolve_references(scope, reference_geometries) if reference_geometries else ()
            # Normalize before admission so recovery compares the actual inputs,
            # while invalid parameter cases retain their rejected original value.
            normalized, invalid = [], set()
            parameter_rules = _PreparedGeometryFunctionParameters(parameter_schema) if parameter_schema is not None else None
            for index, case in enumerate(cases):
                try:
                    parameters = parameter_rules.normalize(case.parameters) if parameter_rules is not None else case.parameters
                    if parameter_schema is None and parameters:
                        raise ValueError("mapped Geometry accepts no parameters")
                except ValueError:
                    invalid.add(index)
                    parameters = case.parameters
                normalized.append(case.model_copy(update={"parameters": parameters}))
            cases = tuple(normalized)
            admission = (await inspections.begin_cases(scope, source=source, dependencies=dependencies, cases=cases,
                nominal_scenario_key=None, request_identity=request_identity, render=render,
                reference_geometries=references, datum_transform=datum_transform)) if retain_for_inspection else None
            if admission is not None and admission.replay_report is not None:
                return await self.observe_case_report(scope, admission.replay_report, render, admission.operation_alias,
                    inherit_connection_context=True)
            results, images, pending = {}, [], []
            reuse_index = None
            try:
                for index, case in enumerate(cases):
                    if index in invalid and case.reuse is None:
                        expected = case.expectation
                        result = PreviewCaseResult(case=case, execution_status="completed",
                            geometry_status="REJECTED", render_status="no_geometry" if case.case_key in selected else "not_requested",
                            expectation_check=("not_checked" if expected is None else
                                "pass" if expected.expected_status == "REJECTED"
                                and expected.reason_code == "PARAMETERS_INVALID" else "fail"),
                            diagnostics=(PreviewCaseDiagnostic(code="PARAMETERS_INVALID", phase="input",
                                message="The case parameters do not satisfy the declared Geometry contract."),))
                        results[index] = result
                        if admission is not None:
                            await inspections.save_case_result(scope, admission, result)
                    elif case.reuse is not None:
                        reuse_index = index
                        result, loaded = await self._reuse_geometry_case(scope, source, case, datum_transform)
                        if loaded is not None and references and loaded.manifest.reference_geometries != references:
                            raise ValueError("Saved execution observation must use its frozen display references")
                        result, image = await self._render_geometry_case(scope, result, loaded=loaded,
                            geometry=None, scene=None, references=references, render=render, selected=selected)
                        results[index] = result
                        if image is not None:
                            images.append((case.case_key, image))
                        if admission is not None:
                            await inspections.save_case_result(scope, admission, result)
                        reuse_index = None
                    else:
                        pending.append(index)
                built = []
                if pending:
                    if isinstance(build_request, CanonicalGeometry):
                        built = [(build_request, interfaces)] * len(pending)
                    elif parameter_schema is None:
                        fixed = await self._geometry_runtime.build_fixed(build_request)
                        built = [(fixed.geometry, resolve_fixed_geometry_interfaces(
                            interfaces, fixed.interfaces))] * len(pending)
                    else:
                        request = parameter_rules.request_from_normalized_samples(build_request,
                            tuple(cases[index].parameters for index in pending),
                            retain_sample_geometries=retain_for_inspection or bool(selected))
                        evaluated = await self._geometry_runtime.evaluate_function_interfaces(request)
                        actual = await material_loader(request) if material_loader is not None else request
                        batch = await self._geometry_runtime.instantiate_function(actual, evaluated)
                        if len(batch.sample_executions) != len(pending):
                            raise ValueError("Geometry runtime changed the requested sample count")
                        if retain_for_inspection or selected:
                            if len(batch.sample_geometries) != len(pending):
                                raise ValueError("Geometry runtime omitted requested same-batch entities")
                            built = [(geometry, receipt.interfaces) for geometry, receipt in
                                zip(batch.sample_geometries, batch.sample_executions, strict=True)]
                        else:
                            # Only facts are needed; do not keep the remaining
                            # BREP payloads in the application result.
                            built = [(None, receipt.interfaces) for receipt in batch.sample_executions]
                    for ordinal, index in enumerate(pending):
                        case = cases[index]
                        local, actual_interfaces = built[ordinal]
                        check = ("not_checked" if case.expectation is None else
                            "pass" if case.expectation.expected_status == "BUILT" else "fail")
                        receipt = batch.sample_executions[ordinal] if parameter_schema is not None and not isinstance(build_request, CanonicalGeometry) else None
                        result = PreviewCaseResult(case=case, execution_status="completed", geometry_status="BUILT",
                            expectation_check=check, geometry_summary=local.summary if local else receipt.geometry_summary,
                            geometry_sha256=local.sha256 if local else receipt.output_sha256,
                            geometry_interfaces=actual_interfaces)
                        loaded = geometry = scene = None
                        if local is not None:
                            geometry = local
                            scene = self._geometry_scene(local.sha256, actual_interfaces,
                                render.profile or GeometryPreviewProfile() if isinstance(render, PreviewSelectedRender) else GeometryPreviewProfile())
                            if datum_transform is not None:
                                geometry = await self._geometry_runtime.normalize(GeometryNormalizeRequest(
                                    format=local.format, payload=local.payload, transform=datum_transform))
                                scene = scene.model_copy(update={"geometry_sha256": geometry.sha256,
                                    "coordinate_space": GeometryRenderCoordinateSpace.PROJECT_WORLD,
                                    "geometry_placement": GeometryRenderPlacement(datum_transform=datum_transform, local_geometry_sha256=local.sha256),
                                    "interface_overlays": tuple(item.model_copy(update={"frame": compose_rigid_transform(datum_transform, item.frame)}) for item in scene.interface_overlays)})
                            if references:
                                scene = scene.model_copy(update={"reference_geometries": tuple(item.render_reference for item in references)})
                            if admission is not None:
                                display_context = None
                                profile = render.profile or GeometryPreviewProfile() if isinstance(render, PreviewSelectedRender) else GeometryPreviewProfile()
                                try:
                                    scene, display_context = await self._prepare_connection_context(profile, scene)
                                except Exception as exc:
                                    result = result.model_copy(update={"render_status": "failed", "diagnostics": (*result.diagnostics,
                                        PreviewCaseDiagnostic(code="PREVIEW_RENDER_FAILED", phase="render",
                                            message=(" ".join(str(exc).split()) or "Connection display could not be prepared.")[:2000]))})
                                from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionApplicationError
                                try:
                                    loaded, _ = await inspections.retain_sample(scope, admission, bundle=None,
                                        resource_version_ref=source.source_ref, source_snapshot=source, parameters=case.parameters,
                                        local_geometry=local, geometry=geometry, interfaces=actual_interfaces, scene=scene,
                                        datum_transform=datum_transform, references=references,
                                        reference_inputs=await self._load_reference_geometries(scope, references),
                                        display_context=display_context,
                                        case_key=case.case_key, complete_operation=False)
                                except GeometryInspectionApplicationError as exc:
                                    result = result.model_copy(update={"diagnostics": (*result.diagnostics,
                                        PreviewCaseDiagnostic(code=exc.code, phase="storage", message=exc.message))})
                                else:
                                    result = result.model_copy(update={"inspection_ref": inspections.reference(scope, loaded.artifact),
                                        "inspection_artifact": loaded.artifact})
                        result, image = await self._render_geometry_case(scope, result, loaded=loaded,
                            geometry=geometry, scene=scene, references=references, render=render, selected=selected)
                        results[index] = result
                        if image is not None:
                            images.append((case.case_key, image))
                        if admission is not None:
                            await inspections.save_case_result(scope, admission, result)
            except asyncio.CancelledError:
                report = self._geometry_case_report(source, dependencies, cases, results,
                    interrupted=True, message="The original Geometry Preview was cancelled.")
                if admission is not None:
                    await asyncio.shield(inspections.finish_case_report(scope, admission, report))
                raise
            except (GeometryRuntimeError, ValueError, ValidationError) as exc:
                # A failed atomic Geometry build batch supplies no partial
                # receipts. Do not claim any missing case produced a shape.
                failed_reuse = reuse_index is not None and reuse_index not in results
                remaining = [reuse_index] if failed_reuse else [index for index in pending if index not in results]
                if remaining:
                    index = remaining[0]
                    results[index] = PreviewCaseResult(case=cases[index], execution_status="technical_error",
                        execution_source="none" if failed_reuse else "executed",
                        expectation_check="not_checked", diagnostics=(PreviewCaseDiagnostic(
                            code="GEOMETRY_EXECUTION_FAILED", phase="execution",
                            message=(" ".join(str(exc).split()) or "The Geometry batch failed.")[:2000]),))
                    if admission is not None:
                        await inspections.save_case_result(scope, admission, results[index])
                report = self._geometry_case_report(source, dependencies, cases, results,
                    interrupted=len(results) != len(cases), message=str(exc))
                report = report.model_copy(update={"cross_case_diagnostics": (PreviewCaseDiagnostic(
                    code=getattr(exc, "code", "GEOMETRY_EXECUTION_FAILED"), phase="execution",
                    message=(" ".join(str(exc).split()) or "The Geometry batch failed.")[:2000]),)})
            else:
                report = self._geometry_case_report(source, dependencies, cases, results)
            alias = await inspections.finish_case_report(scope, admission, report) if admission is not None else None
            return PreviewCaseBatchOutcome(report, alias, tuple(images))

    @staticmethod
    def _geometry_case_report(source, dependencies, cases, results, *, interrupted=False, message="Case was not run."):
        completed = tuple(results.get(index) or PreviewCaseResult(case=case, execution_status="not_run",
            execution_source="none", expectation_check="not_checked", diagnostics=(PreviewCaseDiagnostic(
                code="CASE_NOT_RUN", phase="interrupted", message=(" ".join(message.split()) or "Case was not run.")[:2000]),))
            for index, case in enumerate(cases))
        return PreviewCaseReport(source=source, dependencies=dependencies, cases=completed,
            completion="interrupted" if interrupted else "complete", coverage_gaps=case_check_coverage(cases))

    async def _reuse_geometry_case(self, scope, source, case, datum_transform=None):
        service = self._require_inspection_service()
        if isinstance(case.reuse, PreviewCaseReuse):
            report = await service.read_case_report(scope, case.reuse.operation_alias)
            original = next((item for item in report.cases if item.case.case_key == case.reuse.case_key), None)
            if original is None or original.execution_status != "completed":
                raise ValueError("Saved Geometry case has no complete execution result")
            if original.geometry_status == "REJECTED":
                if (report.source.source_id != source.source_id
                        or report.source.content_digest != source.content_digest
                        or canonical_json(original.case.parameters) != canonical_json(case.parameters)):
                    raise ValueError("Saved Geometry execution differs from the frozen source or parameters")
                expected = case.expectation
                check = ("not_checked" if expected is None else "pass"
                    if expected.expected_status == "REJECTED"
                    and any(item.code == expected.reason_code and item.phase == "input"
                        for item in original.diagnostics) else "fail")
                return original.model_copy(update={"case": case, "execution_source": "reused",
                    "expectation_check": check, "render_status": "not_requested"}), None
            if original.geometry_status != "BUILT":
                raise ValueError("Saved Geometry case has no built entity")
            loaded = await service.load_case_geometry(scope, report, original.case.case_key)
        else:
            loaded = await service.read(scope, case.reuse.inspection_ref)
        snapshot = loaded.manifest.source_snapshot
        if (snapshot is None or snapshot.source_id != source.source_id or snapshot.content_digest != source.content_digest
                or loaded.manifest.source.source_kind != "geometry_sample"
                or canonical_json(loaded.manifest.source.parameters) != canonical_json(case.parameters)
                or loaded.manifest.source.datum_transform != datum_transform):
            raise ValueError("Saved Geometry execution differs from the frozen source or parameters")
        local = loaded.geometries[("fragment", "sample")]
        return PreviewCaseResult(case=case, execution_status="completed", execution_source="reused", geometry_status="BUILT",
            expectation_check="not_checked" if case.expectation is None else "pass" if case.expectation.expected_status == "BUILT" else "fail",
            geometry_sha256=local.sha256, geometry_summary=local.summary, geometry_interfaces=loaded.manifest.geometry_interfaces,
            inspection_ref=service.reference(scope, loaded.artifact), inspection_artifact=loaded.artifact), loaded

    async def _render_geometry_case(self, scope, result, *, loaded, geometry, scene, references, render, selected):
        if result.render_status == "failed":
            return result, None
        if result.case.case_key not in selected:
            return result, None
        if result.geometry_status != "BUILT":
            return result.model_copy(update={"render_status": "no_geometry"}), None
        try:
            if loaded is not None:
                profile = render.profile
                if profile is not None and result.execution_source != "reused":
                    profile = profile.model_copy(update={"connection_context": None})
                view = await self._observe_inspection(scope, loaded, InspectionWholeSelection(), InspectionResultCoordinates(),
                    GeometryInspectionModelObservation(render_profile=profile))
                if view.preview is None:
                    raise ValueError(view.diagnostics[0].message)
                image = view.preview
            else:
                image = await self._render_canonical(geometry, render.profile or GeometryPreviewProfile(), scene,
                    await self._load_reference_geometries(scope, references))
            return result.model_copy(update={"render_status": "rendered"}), image
        except Exception as exc:
            return result.model_copy(update={"render_status": "failed", "diagnostics": (*result.diagnostics,
                PreviewCaseDiagnostic(code="PREVIEW_RENDER_FAILED", phase="render",
                    message=(" ".join(str(exc).split()) or "The image could not be rendered.")[:2000]))}), None

    async def observe_case_report(self, scope, report, render, operation_alias, *, inherit_connection_context=False):
        images, observed = [], []
        if isinstance(render, PreviewSelectedRender):
            if set(render.case_keys) - {item.case.case_key for item in report.cases}:
                raise ValueError("render selection references a missing saved case")
            for item in report.cases:
                if item.case.case_key not in render.case_keys:
                    observed.append(item)
                    continue
                if item.inspection_artifact is None:
                    observed.append(item.model_copy(update={"render_status": "no_geometry"}))
                    continue
                diagnostics = tuple(value for value in item.diagnostics if value.phase != "render")
                try:
                    loaded = await self._require_inspection_service().load_case_geometry(scope, report, item.case.case_key)
                    profile = render.profile
                    if inherit_connection_context and profile is not None and loaded.display_context is not None:
                        profile = profile.model_copy(update={"connection_context": None})
                    view = await self._observe_inspection(scope, loaded, InspectionWholeSelection(), InspectionResultCoordinates(),
                        GeometryInspectionModelObservation(render_profile=profile))
                    if view.preview is None:
                        raise ValueError(view.diagnostics[0].message if view.diagnostics else "Saved entity rendering failed")
                except Exception as exc:
                    observed.append(item.model_copy(update={"render_status": "failed", "diagnostics": (*diagnostics,
                        PreviewCaseDiagnostic(code="PREVIEW_RENDER_FAILED", phase="render", message=str(exc)[:2000] or "Rendering failed"))}))
                else:
                    images.append((item.case.case_key, view.preview))
                    observed.append(item.model_copy(update={"render_status": "rendered", "diagnostics": diagnostics}))
            report = report.model_copy(update={"cases": tuple(observed)})
        return PreviewCaseBatchOutcome(report, operation_alias, tuple(images))

    async def preview_loaded(self, scope, *, source, build_request, parameters=None, parameter_schema=None,
                             dependencies=(), retain_for_inspection=False, render_profile=None, request_identity=None,
                             interfaces=(), datum_transform=None, reference_geometries=(), material_loader=None):
        """Observe a single draft input; the returned batch has one named result."""
        return await self.preview_cases(scope, source=source, build_request=build_request,
            parameter_schema=parameter_schema, cases=(GeometryPreviewCase(case_key="observation", parameters=parameters or {}),),
            dependencies=dependencies, retain_for_inspection=retain_for_inspection,
            render=PreviewSelectedRender(case_keys=("observation",), profile=render_profile), request_identity=request_identity,
            interfaces=interfaces, datum_transform=datum_transform, reference_geometries=reference_geometries,
            material_loader=material_loader)

    async def resolve_references(
        self, scope: InvocationScope, references: tuple[GeometryPreviewReference, ...],
    ) -> tuple[GeometryPreviewReferenceSnapshot, ...]:
        """Authorize the entire display closure before native work or payload reads."""
        if len(references) > 8 or len({item.reference_key for item in references}) != len(references):
            raise GeometryPreviewUnavailableError("preview requires at most eight uniquely named references")
        resolved = []
        total_bytes = 0
        for reference in sorted(references, key=lambda item: item.reference_key):
            bundle = await self._catalog.resolve_exact_version(scope.identity, reference.resource_version_ref)
            asset = next((item for item in bundle.assets if item.asset_role is GeometryResourceAssetRole.CANONICAL_GEOMETRY), None)
            if bundle.resource.resource_kind is not GeometryResourceKind.MAPPED_MODEL or asset is None:
                raise GeometryPreviewUnavailableError("preview references require exact mapped-model canonical BREP or mesh assets")
            try:
                geometry_format = CanonicalGeometryFormat(asset.asset_format)
            except ValueError as exc:
                raise GeometryPreviewUnavailableError("preview reference has an unsupported canonical format") from exc
            total_bytes += asset.size_bytes
            resolved.append(GeometryPreviewReferenceSnapshot(
                **reference.model_dump(), resource_content_digest=bundle.version.content_digest,
                geometry_sha256=asset.sha256, geometry_format=geometry_format,
            ))
        if total_bytes > MAX_GEOMETRY_RENDER_REFERENCE_BYTES:
            raise GeometryPreviewUnavailableError("preview references exceed the combined 32 MiB payload budget")
        return tuple(resolved)

    async def _authorize_references(self, scope, references):
        requested = tuple(GeometryPreviewReference.model_validate(item.model_dump(
            include={"reference_key", "resource_version_ref", "datum_transform"},
        )) for item in references)
        if await self.resolve_references(scope, requested) != references:
            raise GeometryCatalogIntegrityError("preview reference exact source or placement changed")

    async def _load_reference_geometries(self, scope, references):
        if not references:
            return ()
        await self._authorize_references(scope, references)
        geometries = []
        for reference in references:
            bundle = await self._catalog.resolve_exact_version(scope.identity, reference.resource_version_ref)
            resolved = await self._resources.read_exact_asset(
                scope.identity, resource_id=bundle.resource.resource_id,
                resource_version_id=bundle.version.resource_version_id,
                asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY,
            )
            if resolved is None:
                raise GeometryCatalogIntegrityError("preview reference canonical asset is unavailable")
            asset, payload = resolved
            if (asset.sha256 != reference.render_reference.geometry_sha256
                    or hashlib.sha256(payload).hexdigest() != asset.sha256
                    or len(payload) != asset.size_bytes):
                raise GeometryCatalogIntegrityError("preview reference canonical asset changed")
            geometries.append(await self._reopen_canonical_asset(asset.asset_format, payload))
        return tuple(geometries)

    async def preview_product_realization(
        self,
        scope: InvocationScope,
        *,
        family_version_ref: str,
        canonical_geometry: CanonicalGeometry,
        family_version_content_digest: str,
        contract_digest: str,
        source_bundle_sha256: str,
        binding_digest: str,
        frame_check_status: str | None,
        scene: GeometryRenderScene,
        product_realization: ProductRealizationPreviewEvidence,
        render_profile: GeometryPreviewProfile | None = None,
        reference_geometries: tuple[GeometryPreviewReferenceSnapshot, ...] = (),
    ) -> ProductRealizationPreviewAuthorization:
        """Render one already-solved exact product realization through the shared seam."""

        task_id = scope.identity.task_id
        if task_id is None:
            raise GeometryPreviewUnavailableError("geometry preview requires a Task scope")
        family_catalog = self._require_family_catalog()
        bundle = await family_catalog.resolve_exact_version(
            scope.identity,
            family_version_ref,
        )
        self._product_exact_facts(
            bundle,
            family_version_content_digest=family_version_content_digest,
            contract_digest=contract_digest,
            source_bundle_sha256=source_bundle_sha256,
            binding_digest=binding_digest,
        )
        if (
            canonical_geometry.format is not CanonicalGeometryFormat.OCCT_BREP_V1
            or canonical_geometry.content_type != canonical_geometry.format.content_type
            or hashlib.sha256(canonical_geometry.payload).hexdigest() != canonical_geometry.sha256
        ):
            raise GeometryCatalogIntegrityError(
                "product realization canonical geometry failed exact validation"
            )
        if scene.geometry_sha256 != canonical_geometry.sha256:
            raise GeometryCatalogIntegrityError(
                "product realization render scene changed its canonical geometry"
            )
        if frame_check_status not in {None, "PASS", "FAIL"}:
            raise GeometryCatalogIntegrityError(
                "product realization frame status is outside its closed domain"
            )
        if product_realization is None:
            raise GeometryCatalogIntegrityError("self geometry preview requires same-solve evidence")
        if product_realization is not None:
            result = product_realization.realization
            if (product_realization.contract_digest != contract_digest
                    or product_realization.geometry_sha256 != canonical_geometry.sha256
                    or result.binding_digest != binding_digest
                    or result.source_bundle_sha256 != source_bundle_sha256
                    or result.frame_check_status != frame_check_status):
                raise GeometryCatalogIntegrityError("product preview evidence disagrees with its exact result")
        profile = render_profile or GeometryPreviewProfile()
        self._reject_product_temporary_interfaces(profile)
        if reference_geometries:
            await self._authorize_references(scope, reference_geometries)
            scene = scene.model_copy(update={"reference_geometries": tuple(item.render_reference for item in reference_geometries)})
        return await self._render_once(
            scope,
            target_kind=_PRODUCT_PREVIEW_TARGET_KIND,
            exact_ref=family_version_ref,
            title=bundle.version.display_name,
            geometry=canonical_geometry,
            scene=scene,
            reference_geometries=reference_geometries,
            profile=profile,
            frame_check_status=frame_check_status,
            external_constraints=product_realization.invocation.external_constraints,
            external_checks=product_realization.realization.external_condition_checks,
        )

    async def preview(
        self,
        scope: InvocationScope,
        exact_version_ref: str,
        sample_parameters: Mapping[str, JsonValue] | None,
        render_profile: GeometryPreviewProfile | None,
        *, deadline: float | None = None, datum_transform: RigidTransform3D | None = None,
        reference_geometries: tuple[GeometryPreviewReference, ...] = (),
        retain_for_inspection: bool = False, request_identity: str | None = None,
    ) -> GeometryPreviewAuthorization | GeometryInspectionPreviewOutcome:
        async with self._geometry_runtime.invocation_scope(GEOMETRY_PREVIEW_EXECUTION, deadline=deadline), self._geometry_runtime.execution_scope(GEOMETRY_PREVIEW_EXECUTION):
            return await self._preview_scoped(scope, exact_version_ref, sample_parameters, render_profile, datum_transform,
                reference_geometries, retain_for_inspection=retain_for_inspection, request_identity=request_identity)

    async def _preview_scoped(
        self, scope: InvocationScope, exact_version_ref: str,
        sample_parameters: Mapping[str, JsonValue] | None,
        render_profile: GeometryPreviewProfile | None,
        datum_transform: RigidTransform3D | None = None,
        reference_geometries: tuple[GeometryPreviewReference, ...] = (),
        *, retain_for_inspection: bool = False, request_identity: str | None = None,
    ) -> GeometryPreviewAuthorization | GeometryInspectionPreviewOutcome:
        has_sample_parameters = sample_parameters is not None
        parameters = dict(sample_parameters or {})
        try:
            parameter_json = canonical_json(parameters)
        except (TypeError, ValueError) as exc:
            raise GeometryPreviewUnavailableError(
                "geometry preview parameters are outside canonical JSON"
            ) from exc
        if len(parameter_json.encode("utf-8")) > _MAX_PARAMETERS_BYTES:
            raise GeometryPreviewUnavailableError(
                "geometry preview parameters exceed their byte budget"
            )
        profile = render_profile or GeometryPreviewProfile()
        bundle = await self._catalog.resolve_exact_version(
            scope.identity,
            exact_version_ref,
        )

        if has_sample_parameters and bundle.resource.resource_kind is GeometryResourceKind.MODELING_FUNCTION:
            try:
                parameters = await normalize_geometry_function_sample_parameters(
                    self._resources, scope, bundle, parameters,
                )
            except GeometryResourceBuildError as exc:
                raise GeometryPreviewUnavailableError(str(exc)) from exc

        preview_path_kind = self._preview_path_kind(
            bundle,
            has_sample_parameters=has_sample_parameters,
        )
        task_id = scope.identity.task_id
        if task_id is None:
            raise GeometryPreviewUnavailableError("geometry preview requires a Task scope")
        if datum_transform is not None:
            if preview_path_kind == "modeling_function_precomputed":
                raise GeometryPreviewUnavailableError("stored PNG preview cannot carry a datum placement")
        references = await self.resolve_references(scope, reference_geometries) if reference_geometries else ()
        if references:
            if preview_path_kind == "modeling_function_precomputed":
                raise GeometryPreviewUnavailableError("mixed preview of a modeling function requires explicit sample parameters")
        admission = None
        if retain_for_inspection:
            if not has_sample_parameters or bundle.resource.resource_kind is not GeometryResourceKind.MODELING_FUNCTION:
                raise GeometryPreviewUnavailableError("retention requires a modeling-function sample with explicit parameters")
            inspection = self._require_inspection_service()
            admission = await inspection.begin(scope, request_identity=request_identity,
                source_kind="geometry_sample", source_id=bundle.resource.resource_id,
                source_version_id=bundle.version.resource_version_id, source_version_ref=exact_version_ref,
                source_content_digest=bundle.version.content_digest, tool_name="ifc_geometry_preview",
                request_payload={"resource_version_ref": exact_version_ref, "sample_parameters": parameters,
                    "datum_transform": datum_transform.model_dump(mode="json") if datum_transform else None,
                    "render_profile": profile.model_dump(mode="json"),
                    "reference_geometries": [item.model_dump(mode="json") for item in references]})
            if admission.replay is not None:
                if admission.replay.display_context is not None:
                    profile = profile.model_copy(update={"connection_context": None})
                return await self._observe_inspection(scope, admission.replay, InspectionWholeSelection(),
                    InspectionResultCoordinates(), GeometryInspectionModelObservation(render_profile=profile),
                    operation_alias=admission.operation_alias)
        geometry: CanonicalGeometry | None = None
        local_geometry: CanonicalGeometry | None = None
        scene: GeometryRenderScene | None = None
        stored_preview_bytes: bytes | None = None
        if bundle.resource.resource_kind is GeometryResourceKind.MODELING_FUNCTION:
            if has_sample_parameters:
                try:
                    geometry, interfaces = await self._build_function_sample(scope, bundle, parameters)
                except Exception as exc:
                    if admission is not None:
                        failed = await self._require_inspection_service().fail(scope, admission,
                            code="INSPECTION_BUILD_FAILED", message=str(exc) or "Geometry sample execution failed")
                        from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionApplicationError
                        raise GeometryInspectionApplicationError("INSPECTION_BUILD_FAILED", str(exc), phase="execution",
                            operation_alias=failed.catalog_entry.alias) from exc
                    raise
                local_geometry = geometry
                scene = self._geometry_scene(geometry.sha256, interfaces, profile)
            else:
                if profile != GeometryPreviewProfile():
                    raise GeometryPreviewUnavailableError(
                        "a stored modeling-function preview accepts only its original clean view"
                    )
                resolved = await self._resources.read_exact_asset(
                    scope.identity,
                    resource_id=bundle.resource.resource_id,
                    resource_version_id=bundle.version.resource_version_id,
                    asset_role=GeometryResourceAssetRole.PREVIEW,
                )
                if resolved is None:
                    raise GeometryPreviewUnavailableError(
                        "this modeling-function version has no precomputed preview"
                    )
                asset, stored_preview_bytes = resolved
                if (
                    asset.content_type != "image/png"
                    or hashlib.sha256(stored_preview_bytes).hexdigest() != asset.sha256
                ):
                    raise GeometryCatalogIntegrityError(
                        "the exact geometry preview asset is not an exact PNG"
                    )
        else:
            resolved = await self._resources.read_exact_asset(
                scope.identity,
                resource_id=bundle.resource.resource_id,
                resource_version_id=bundle.version.resource_version_id,
                asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY,
            )
            if resolved is None:
                raise GeometryCatalogIntegrityError(
                    "mapped geometry resource has no canonical geometry asset"
                )
            asset, geometry_bytes = resolved
            geometry = await self._reopen_canonical_asset(asset.asset_format, geometry_bytes)
            if geometry.sha256 != asset.sha256:
                raise GeometryCatalogIntegrityError(
                    "canonical geometry changed while reopening for preview"
                )
            scene = self._geometry_scene(
                geometry.sha256,
                self._manifest_interfaces(bundle),
                profile,
            )

        if datum_transform is not None:
            assert geometry is not None and scene is not None
            placement = GeometryRenderPlacement(
                datum_transform=datum_transform,
                local_geometry_sha256=geometry.sha256,
            )
            geometry = await self._geometry_runtime.normalize(GeometryNormalizeRequest(
                format=geometry.format, payload=geometry.payload, transform=datum_transform,
            ))
            scene = scene.model_copy(update={
                "coordinate_space": GeometryRenderCoordinateSpace.PROJECT_WORLD,
                "geometry_sha256": geometry.sha256,
                "geometry_placement": placement,
                "interface_overlays": tuple(item.model_copy(update={"frame": compose_rigid_transform(datum_transform, item.frame)}) for item in scene.interface_overlays),
            })

        if references:
            assert scene is not None
            scene = scene.model_copy(update={"reference_geometries": tuple(item.render_reference for item in references)})
        if admission is not None:
            assert geometry is not None and local_geometry is not None and scene is not None
            display_context, render_failure = None, None
            try:
                scene, display_context = await self._prepare_connection_context(profile, scene)
            except Exception as exc:
                render_failure = _inspection_render_failure(exc)
            loaded, operation_alias = await self._require_inspection_service().retain_sample(scope, admission,
                bundle=bundle, resource_version_ref=exact_version_ref, parameters=parameters,
                local_geometry=local_geometry, geometry=geometry, interfaces=interfaces,
                scene=scene, datum_transform=datum_transform, references=references,
                display_context=display_context,
                reference_inputs=await self._load_reference_geometries(scope, references))
            if render_failure is not None:
                return GeometryInspectionPreviewOutcome(inspection_ref=self._inspection_service.reference(scope, loaded.artifact),
                    source_kind="geometry_sample", source_version_ref=exact_version_ref,
                    result_coordinate_space=loaded.manifest.result_coordinate_space,
                    selection=InspectionWholeSelection(), coordinates=InspectionResultCoordinates(),
                    diagnostics=(render_failure,), **await self._inspection_service.origin_navigation(scope, loaded))
            return await self._observe_inspection(scope, loaded, InspectionWholeSelection(),
                InspectionResultCoordinates(), GeometryInspectionModelObservation(render_profile=profile.model_copy(update={"connection_context": None})),
                operation_alias=operation_alias)
        return await self._render_once(
            scope,
            target_kind=_GEOMETRY_TARGET_KIND,
            reference_geometries=references,
            exact_ref=exact_version_ref,
            title=bundle.resource.display_name,
            normalized_sample_parameters=parameters if has_sample_parameters else None,
            geometry=geometry,
            scene=scene,
            profile=profile,
            stored_preview_bytes=stored_preview_bytes,
            datum_transform=datum_transform,
        )

    async def preview_inspection(self, scope, inspection_ref, selection, coordinates, observation):
        async with self._geometry_runtime.invocation_scope(GEOMETRY_PREVIEW_EXECUTION), self._geometry_runtime.execution_scope(GEOMETRY_PREVIEW_EXECUTION):
            loaded = await self._require_inspection_service().read(scope, inspection_ref)
            return await self._observe_inspection(scope, loaded, selection, coordinates, observation)

    async def _observe_inspection(self, scope, loaded, selection, coordinates, observation, *, operation_alias=None):
        from CAD2BIM_backend.domain.geometry_runtime import GeometrySectionRequest
        service = self._require_inspection_service()
        service.validate_selection(loaded, selection, coordinates)
        source = loaded.manifest.source
        version_ref = getattr(source, "resource_version_ref", None) or source.family_version_ref
        origin = await service.origin_navigation(scope, loaded)
        common = dict(inspection_ref=service.reference(scope, loaded.artifact),
            source_kind=source.source_kind, source_version_ref=version_ref,
            result_coordinate_space=loaded.manifest.result_coordinate_space,
            selection=selection, coordinates=coordinates, **origin)
        if isinstance(observation, GeometrySectionObservation):
            section = await self._geometry_runtime.section(GeometrySectionRequest(
                geometry=loaded.geometry, selection=selection, coordinates=coordinates,
                observation=observation, fragment_inputs=loaded.fragment_inputs))
            return GeometryInspectionPreviewOutcome(**common, section=section)
        profile = observation.render_profile or GeometryPreviewProfile()
        scene = self._inspection_scene(loaded, selection, coordinates)
        reference_inputs = loaded.reference_inputs if scene.reference_geometries else ()
        try:
            if not isinstance(selection, InspectionWholeSelection) and profile.connection_context is not None:
                if profile.connection_context.mode != "none":
                    raise GeometryPreviewRequestError("Connection context requires whole/result observation")
            display_context = loaded.display_context if scene.display_context_sha256 is not None else None
            scene, display_context = await self._prepare_connection_context(
                profile, scene, display_context=display_context,
                external_constraints=getattr(source, "invocation", None).external_constraints
                    if getattr(source, "invocation", None) is not None else None,
                external_checks=loaded.manifest.realization.external_condition_checks
                    if loaded.manifest.realization is not None else (),
            )
            preview = await self._render_canonical(loaded.geometry, profile, scene, reference_inputs,
                display_context=display_context, context_prepared=True,
                inspection_selection=selection, inspection_coordinates=coordinates,
                fragment_inputs=loaded.fragment_inputs)
            return GeometryInspectionPreviewOutcome(**common, preview=preview)
        except Exception as exc:
            return GeometryInspectionPreviewOutcome(**common, diagnostics=(_inspection_render_failure(exc),))

    @staticmethod
    def _inspection_scene(loaded, selection, coordinates):
        scene = loaded.manifest.scene
        if isinstance(selection, InspectionWholeSelection):
            return scene
        fragments = {item.object_key: item for item in loaded.manifest.geometries if item.role == "fragment"}
        selected = ({selection.fragment_key} if isinstance(selection, InspectionFragmentSelection) else
                    {key for key, item in fragments.items() if item.component_key == selection.component_key})
        result = loaded.manifest.realization
        if result is not None:
            keys = {item.interface_key for item in result.interface_project_world_frames if item.source.fragment_key in selected}
            overlays = tuple(item for item in scene.interface_overlays if item.overlay_id in {f"family:{key}" for key in keys})
        else:
            overlays = tuple(item for item in scene.interface_overlays if item.overlay_id.startswith("model:"))
        updates = {"interface_overlays": overlays, "reference_geometries": (),
                   "straight_profile_overlays": (), "source_path_overlays": (), "host_overlays": (),
                   "display_context_sha256": None, "geometry_placement": None,
                   "connection_context": None, "connection_context_diagnostics": ()}
        if isinstance(coordinates, InspectionFragmentLocalCoordinates):
            inverse = inverse_rigid_transform(fragments[coordinates.fragment_key].result_from_local)
            updates.update(coordinate_space=GeometryRenderCoordinateSpace.GEOMETRY_COORDINATES,
                interface_overlays=tuple(item.model_copy(update={"frame": compose_rigid_transform(inverse, item.frame)}) for item in overlays))
        return scene.model_copy(update=updates)

    def _require_inspection_service(self):
        if self._inspection_service is None:
            raise GeometryPreviewUnavailableError("geometry inspection retention is not configured")
        return self._inspection_service

    @staticmethod
    def _preview_path_kind(
        bundle: GeometryResourceVersionBundle,
        *,
        has_sample_parameters: bool,
    ) -> str:
        if bundle.resource.resource_kind is GeometryResourceKind.MODELING_FUNCTION:
            if has_sample_parameters:
                return "modeling_function_sample"
            return "modeling_function_precomputed"
        if has_sample_parameters:
            raise GeometryPreviewUnavailableError(
                "mapped-model preview does not accept sample parameters"
            )
        return "mapped_model_canonical"

    async def _build_function_sample(
        self,
        scope: InvocationScope,
        bundle: GeometryResourceVersionBundle,
        parameters: dict[str, JsonValue],
    ) -> tuple[CanonicalGeometry, GeometryModelInterfaces]:
        try:
            return await build_geometry_function_sample(self._resources, self._geometry_runtime, scope, bundle, parameters)
        except GeometryResourceBuildError as exc:
            raise GeometryPreviewUnavailableError(str(exc)) from exc

    async def _prepare_connection_context(
        self, profile, scene, *, display_context=None, external_constraints=None, external_checks=(),
    ):
        option = profile.connection_context
        if option is None:
            return scene, display_context
        overlays = tuple(item for item in scene.straight_profile_overlays
                         if not item.recipe_key.startswith("context:"))
        if any(item.display_kind != "original" for item in overlays):
            raise GeometryPreviewRequestError("Connection context cannot replace an existing Host display layer")
        updates = dict(straight_profile_overlays=overlays, display_context_sha256=None,
                       connection_context=None, connection_context_diagnostics=())
        if option.mode == "none":
            return scene.model_copy(update=updates), None
        diagnostics = []
        if option.mode == "pipes":
            pipes = option.items
        else:
            if external_constraints is None:
                raise GeometryPreviewRequestError("Auto connection context requires saved or same-result Family installation conditions")
            pipes = []
            actual = {item.overlay_id.removeprefix("family:"): item for item in scene.interface_overlays
                      if item.marker_kind is GeometryRenderInterfaceMarkerKind.FAMILY_EXTERNAL}
            checks = {item.interface_key: item.evaluation for item in external_checks}
            for external in external_constraints:
                key, condition = external.interface_key, external.condition
                port, check = actual.get(key), checks.get(key)
                if port is None or check is None or not check.satisfied:
                    diagnostics.append(f"{key}: skipped; actual external interface or satisfied condition check is unavailable")
                    continue
                diameter = condition.outer_diameter_m
                if diameter is None:
                    diagnostics.append(f"{key}: skipped; installation condition has no outer diameter")
                    continue
                start = port.frame.translation_m
                axis = tuple(row[2] for row in port.frame.rotation)
                end = tuple(p + 3.0 * diameter * n for p, n in zip(start, axis))
                pipes.append(GeometryPreviewPipeSegment(start_m=start, end_m=end, outer_diameter_m=diameter))
            pipes = tuple(pipes)
        updates["connection_context_diagnostics"] = tuple(diagnostics)
        if not pipes:
            return scene.model_copy(update=updates), None
        recipes = []
        for index, pipe in enumerate(pipes):
            length = math.dist(pipe.start_m, pipe.end_m)
            axis = tuple((end - start) / length for start, end in zip(pipe.start_m, pipe.end_m))
            seed = min(((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                       key=lambda value: abs(sum(a * b for a, b in zip(axis, value))))
            projection = sum(a * b for a, b in zip(axis, seed))
            reference = tuple(value - projection * n for value, n in zip(seed, axis))
            reference_length = math.sqrt(sum(value * value for value in reference))
            frame = rigid_transform_from_axis_reference(pipe.start_m, axis,
                tuple(value / reference_length for value in reference))
            recipes.append(GeometryRenderStraightProfileOverlay(recipe_key=f"context:pipe_{index}",
                display_kind="illustrative", frame=frame,
                profile=CircularInterfaceProfile(diameter_m=pipe.outer_diameter_m), length_m=length))
        request = GeometryBuildDisplayContextRequest(recipes=tuple(recipes))
        built = await self._geometry_runtime.build_display_context(request)
        if built.recipe_keys != tuple(item.recipe_key for item in recipes) or built.recipe_digest != sha256_digest(request):
            raise GeometryCatalogIntegrityError("Connection display builder changed its requested straight segments")
        updates.update(straight_profile_overlays=(*overlays, *recipes),
                       display_context_sha256=built.geometry.sha256,
                       connection_context=GeometryPreviewPipesConnectionContext(items=pipes))
        return GeometryRenderScene.model_validate(scene.model_copy(update=updates).model_dump()), built.geometry

    async def _render_canonical(
        self,
        geometry: CanonicalGeometry,
        profile: GeometryPreviewProfile,
        scene: GeometryRenderScene,
        reference_geometries: tuple[CanonicalGeometry, ...] = (),
        display_context: CanonicalGeometry | None = None,
        context_prepared: bool = False,
        external_constraints=None, external_checks=(),
        **inspection_target,
    ) -> GeometryPreview:
        if not context_prepared:
            scene, display_context = await self._prepare_connection_context(profile, scene,
                display_context=display_context, external_constraints=external_constraints, external_checks=external_checks)
        request = GeometryRenderRequest(
            geometry=geometry,
            scene=scene,
            reference_geometries=reference_geometries,
            display_context=display_context,
            **inspection_target,
            **profile.model_dump(mode="python"),
        )
        rendered = await self._geometry_runtime.render(request)
        preview_bytes = rendered.payload
        if (
            rendered.content_type != "image/png"
            or hashlib.sha256(preview_bytes).hexdigest() != rendered.sha256
        ):
            raise GeometryCatalogIntegrityError("geometry runtime returned an invalid preview")
        return rendered.model_copy(update={"connection_context": scene.connection_context,
            "connection_context_diagnostics": scene.connection_context_diagnostics})

    async def _render_once(
        self,
        scope: InvocationScope,
        *,
        target_kind: str,
        exact_ref: str,
        title: str,
        geometry: CanonicalGeometry | None,
        scene: GeometryRenderScene | None,
        profile: GeometryPreviewProfile,
        reference_geometries: tuple[GeometryPreviewReferenceSnapshot, ...] = (),
        normalized_sample_parameters: dict[str, JsonValue] | None = None,
        stored_preview_bytes: bytes | None = None,
        datum_transform: RigidTransform3D | None = None,
        frame_check_status: str | None = None,
        external_constraints=None, external_checks=(),
    ) -> GeometryPreviewAuthorization | ProductRealizationPreviewAuthorization:
        if stored_preview_bytes is not None:
            if geometry is not None or scene is not None or reference_geometries:
                raise GeometryCatalogIntegrityError("stored preview acquired invented scene facts")
            preview_bytes = stored_preview_bytes
            render_metadata = GeometryStoredPreviewMetadata()
        else:
            if geometry is None or scene is None:
                raise GeometryCatalogIntegrityError("rendered preview lacks exact geometry or scene")
            scene, display_context = await self._prepare_connection_context(profile, scene,
                external_constraints=external_constraints, external_checks=external_checks)
            rendered = await self._render_canonical(
                geometry, profile, scene,
                await self._load_reference_geometries(scope, reference_geometries),
                display_context=display_context, context_prepared=True,
            )
            if not rendered.resolved_panels:
                raise GeometryCatalogIntegrityError("geometry runtime omitted resolved preview cameras")
            preview_bytes = rendered.payload
            render_metadata = GeometryRenderedPreviewMetadata(
                scene_sha256=sha256_digest(scene),
                coordinate_space=scene.coordinate_space,
                resolved_panels=rendered.resolved_panels,
                display_quality_warnings=rendered.display_quality_warnings,
                connection_context=rendered.connection_context,
                connection_context_diagnostics=rendered.connection_context_diagnostics,
            )
        self._geometry_runtime.release_execution()
        common = {
            "title": title,
            "size_bytes": len(preview_bytes),
            "content_sha256": hashlib.sha256(preview_bytes).hexdigest(),
            "content": preview_bytes,
            "render_metadata": render_metadata,
            "interface_overlays": scene.interface_overlays if scene else (),
            "host_overlays": scene.host_overlays if scene else (),
            "straight_profile_overlays": scene.straight_profile_overlays if scene else (),
            "reference_geometries": reference_geometries,
        }
        if target_kind == _PRODUCT_PREVIEW_TARGET_KIND:
            return ProductRealizationPreviewAuthorization(
                family_version_ref=exact_ref,
                frame_check_status=frame_check_status,
                **common,
            )
        return GeometryPreviewAuthorization(
            resource_version_ref=exact_ref,
            sample_parameters_json=(canonical_json(normalized_sample_parameters)
                                    if normalized_sample_parameters is not None else None),
            datum_transform=datum_transform,
            **common,
        )

    def _product_exact_facts(
        self,
        bundle: ProductFamilyVersionBundle,
        *,
        family_version_content_digest: str,
        contract_digest: str,
        source_bundle_sha256: str,
        binding_digest: str,
    ) -> None:
        for label, digest in (
            ("family version content", family_version_content_digest),
            ("contract", contract_digest),
            ("source bundle", source_bundle_sha256),
            ("binding", binding_digest),
        ):
            _require_sha256(digest, label=label)
        source = next(
            (
                asset
                for asset in bundle.assets
                if asset.asset_role is ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE
            ),
            None,
        )
        expected_binding_digest = sha256_digest(
            [
                {
                    "binding_key": item.binding_key,
                    "resource_kind": item.expected_resource_kind.value,
                    "geometry_content_digest": item.geometry_content_digest,
                }
                for item in sorted(bundle.geometry_bindings, key=lambda value: value.binding_key)
            ]
        )
        if (
            source is None
            or bundle.version.content_digest != family_version_content_digest
            or bundle.version.contract_digest != contract_digest
            or source.sha256 != source_bundle_sha256
            or expected_binding_digest != binding_digest
        ):
            raise GeometryCatalogIntegrityError(
                "product realization preview facts disagree with the exact family version"
            )

    @staticmethod
    def _manifest_interfaces(
        bundle: GeometryResourceVersionBundle,
    ) -> GeometryModelInterfaces:
        try:
            return _INTERFACES_ADAPTER.validate_python(
                bundle.version.manifest.get("interfaces", ())
            )
        except ValidationError as exc:
            raise GeometryCatalogIntegrityError(
                "geometry resource manifest contains invalid model interfaces"
            ) from exc

    @staticmethod
    def _geometry_scene(
        geometry_sha256: str,
        interfaces: GeometryModelInterfaces,
        profile: GeometryPreviewProfile,
    ) -> GeometryRenderScene:
        overlays = tuple(
            GeometryRenderInterfaceOverlay(
                overlay_id=f"model:{item.interface_key}",
                label=item.interface_key,
                marker_kind=GeometryRenderInterfaceMarkerKind.MODEL,
                frame=item.local_frame,
                profile=item.profile,
            )
            for item in interfaces
        )
        presentation = profile.presentation
        if presentation is not None and isinstance(
            presentation.overlay,
            GeometryPreviewInterfacesOverlayRequest,
        ):
            temporary = presentation.overlay.temporary_interfaces
            if temporary is not None:
                overlays += tuple(
                    GeometryRenderInterfaceOverlay(
                        overlay_id=f"temporary:{item.interface_key}",
                        label=item.interface_key,
                        marker_kind=GeometryRenderInterfaceMarkerKind.TEMPORARY,
                        frame=item.local_frame,
                        profile=item.profile,
                    )
                    for item in temporary
                )
        return GeometryRenderScene(
            coordinate_space=GeometryRenderCoordinateSpace.GEOMETRY_COORDINATES,
            geometry_sha256=geometry_sha256,
            interface_overlays=overlays,
        )

    @staticmethod
    def _reject_product_temporary_interfaces(profile: GeometryPreviewProfile) -> None:
        presentation = profile.presentation
        if (
            presentation is not None
            and isinstance(
                presentation.overlay,
                GeometryPreviewInterfacesOverlayRequest,
            )
            and "temporary_interfaces" in presentation.overlay.model_fields_set
        ):
            raise GeometryPreviewUnavailableError(
                "product realization preview does not accept temporary model interfaces"
            )

    async def _reopen_canonical_asset(self, asset_format: str, payload: bytes) -> CanonicalGeometry:
        return await reopen_exact_canonical_geometry(self._geometry_runtime, asset_format, payload)

    def _require_family_catalog(self) -> ProductFamilyCatalogService:
        if self._family_catalog is None:
            raise GeometryPreviewUnavailableError(
                "product realization preview authorization is not configured"
            )
        return self._family_catalog

def _inspection_render_failure(exc: Exception) -> GeometryInspectionDiagnostic:
    message = (" ".join(str(exc).split()) or "The saved model could not be rendered")[:2000]
    action = "Keep this inspection_ref. Correct an invalid display request; otherwise report the runtime failure before retrying."
    if isinstance(exc, GeometryRuntimeResourceLimitError):
        message = "Saved-model display processing exceeded its resource limit."
        if exc.diagnostic is not None:
            message += exc.diagnostic.limit_summary()
        action = ("Keep this inspection_ref; this timeout or resource limit does not establish invalid geometry. "
                  "Do not rebuild or modify Geometry solely for this error. Resolve the reported runtime limit before retrying the saved-model display.")
    elif isinstance(exc, GeometryRuntimeUnavailableError):
        action = "Keep this inspection_ref. Restore runtime availability before retrying; no Geometry change is justified by this error."
    elif isinstance(exc, GeometryRuntimeValidationError):
        action = ("Keep this inspection_ref. Follow the reported cause and object: for a selected-model display-quality failure, "
                  "Query this same saved target and selection with ctx.display_diagnostics(). Reference objects require their own exact target; "
                  "background objects may have no queryable target. Correct the request or construction only as supported by that diagnosis.")
    return GeometryInspectionDiagnostic(code="INSPECTION_RENDER_FAILED", phase="render", message=message, action=action)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_sha256(value: object, *, label: str) -> None:
    if not _is_sha256(value):
        raise GeometryCatalogIntegrityError(
            f"product realization preview {label} digest is invalid"
        )


__all__ = ["GeometryPreviewService"]
