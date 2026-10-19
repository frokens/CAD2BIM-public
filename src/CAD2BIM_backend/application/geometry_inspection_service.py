"""Operation-backed retention and exact read-only views of actual build results."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.catalog_scope import catalog_scope_digest
from CAD2BIM_backend.application.data_runtime_service import _bounded_error_message
from CAD2BIM_backend.application.errors import ConflictError, NotFoundError
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef, AuthoringExecutionClaim, CriticalOperationErrorPhase,
    CriticalOperationInitiator, CriticalOperationKind, CriticalOperationRecord,
    CriticalOperationStatus, GeometryInspectionOperationDetail, PublishedCriticalOperation,
    CatalogResourceKind,
)
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D, compose_rigid_transform
from CAD2BIM_backend.domain.geometry_inspection import (
    GeometryInspectionCoordinates, GeometryInspectionSelection,
    InspectionComponentSelection, InspectionFragmentSelection,
    require_inspection_coordinate_selection,
)
from CAD2BIM_backend.domain.geometry_inspection_asset import (
    GEOMETRY_INSPECTION_MANIFEST_MAX_BYTES,
    GEOMETRY_INSPECTION_MAX_BYTES, GEOMETRY_INSPECTION_MEDIA_TYPE,
    GeometryInspectionFamilySource, GeometryInspectionGeometryMember,
    GeometryInspectionManifest, GeometryInspectionOperationResult, GeometryInspectionSampleSource,
)
from CAD2BIM_backend.domain.geometry_query import GeometryQueryArtifactRef
from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry
from CAD2BIM_backend.domain.identity import stable_internal_identity_id
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.product_realization import product_realization_structure
from CAD2BIM_backend.domain.preview_cases import (
    PreviewCaseReport, PreviewCaseResult, PreviewSourceSnapshot,
    InlinePreviewCases, SavedPreviewCases, PreviewCaseDiagnostic, revise_preview_cases,
)

if TYPE_CHECKING:
    from CAD2BIM_backend.ports.data_runtime import DataRuntimeRepository
    from CAD2BIM_backend.ports.opaque_cursor import OpaqueCursorCodecPort


class GeometryInspectionApplicationError(RuntimeError):
    def __init__(self, code, message, *, phase="target", retryable=False, operation_alias=None):
        super().__init__(message)
        self.code, self.message, self.phase = code, message, phase
        self.retryable, self.operation_alias = retryable, operation_alias


@dataclass(frozen=True, slots=True)
class LoadedGeometryInspection:
    artifact: GeometryQueryArtifactRef
    manifest: GeometryInspectionManifest
    geometries: dict[tuple[str, str], CanonicalGeometry]

    @property
    def geometry(self):
        whole = next(item for item in self.manifest.geometries if item.role == "whole")
        return self.geometries[("whole", whole.object_key)]

    @property
    def fragment_inputs(self):
        from CAD2BIM_backend.domain.geometry_runtime import GeometryInspectionFragmentInput
        return tuple(GeometryInspectionFragmentInput(
            fragment_key=item.object_key, component_key=item.component_key,
            binding_key=item.binding_key, geometry=self.geometries[("fragment", item.object_key)],
            result_from_local=item.result_from_local, uniform_scale=item.uniform_scale,
        ) for item in self.manifest.geometries if item.role == "fragment")

    @property
    def reference_inputs(self):
        return tuple(self.geometries[("reference", item.reference_key)]
                     for item in self.manifest.reference_geometries)

    @property
    def display_context(self):
        member = next((item for item in self.manifest.geometries if item.role == "display_context"), None)
        return self.geometries[("display_context", member.object_key)] if member is not None else None

    @property
    def context(self):
        manifest = self.manifest
        result = manifest.realization
        components = []
        if manifest.family_contract is not None and result is not None:
            worlds = {item.component_key: item.project_world_transform
                      for item in result.component_project_world_transforms}
            components = [{**item.model_dump(mode="json"),
                           "result_from_local": worlds[item.component_key].model_dump(mode="json")}
                          for item in manifest.family_contract.components]
        interfaces = ([item.model_dump(mode="json") for item in result.interface_project_world_frames]
                      if result is not None else [
                          {**item.model_dump(mode="json"), "result_frame": compose_rigid_transform(
                              manifest.source.datum_transform or RigidTransform3D(), item.local_frame
                          ).model_dump(mode="json")}
                          for item in manifest.geometry_interfaces])
        return {
            "source": {**manifest.source.model_dump(mode="json"),
                       "result_coordinate_space": manifest.result_coordinate_space,
                       "length_unit": "METRE"},
            "components": components,
            "fragments": [{"fragment_key": item.object_key, "component_key": item.component_key,
                           "binding_key": item.binding_key, "geometry_sha256": item.sha256,
                           "result_from_local": item.result_from_local.model_dump(mode="json"),
                           "uniform_scale": item.uniform_scale, "scale_already_applied": True}
                          for item in manifest.geometries if item.role == "fragment"],
            "interfaces": interfaces,
            "checks": ({"frame_check_status": result.frame_check_status,
                        "frame_diagnostics": [item.model_dump(mode="json") for item in result.frame_diagnostics],
                        "external_condition_checks": [item.model_dump(mode="json") for item in result.external_condition_checks],
                        "material_and_connectivity": "NOT_CHECKED"}
                       if result is not None else {"material_and_connectivity": "NOT_CHECKED"}),
            "reference_geometries": [item.model_dump(mode="json") for item in manifest.reference_geometries],
        }


@dataclass(frozen=True, slots=True)
class GeometryInspectionBuildAdmission:
    operation: CriticalOperationRecord
    claim: AuthoringExecutionClaim | None
    replay: LoadedGeometryInspection | None = None
    operation_alias: str | None = None
    replay_report: PreviewCaseReport | None = None


class GeometryInspectionService:
    """Use the existing Operation lifecycle and Artifact owner, without a second index."""

    def __init__(self, artifacts: ArtifactService, repository: DataRuntimeRepository,
                 cursor_codec: OpaqueCursorCodecPort, *, family_catalog=None):
        self._artifacts, self._repository, self._codec = artifacts, repository, cursor_codec
        self._family_catalog = family_catalog

    async def begin_cases(self, scope, *, source, dependencies, cases, nominal_scenario_key,
                          request_identity, render, reference_geometries=(), datum_transform=None):
        return await self.begin(scope, request_identity=request_identity,
            source_kind=source.source_kind, source_id=source.source_id,
            source_version_id=source.snapshot_id, source_version_ref=source.source_ref,
            source_content_digest=source.content_digest,
            source_type=source.source_type, source_draft_artifact=source.draft_artifact,
            result_kind="case_report", tool_name=("ifc_geometry_preview"
                if source.source_kind == "geometry_sample" else "ifc_product_realization_preview"),
            request_payload={"source": source.model_dump(mode="json"),
                "dependencies": [item.model_dump(mode="json") for item in dependencies],
                "cases": [item.model_dump(mode="json") for item in cases],
                "nominal_scenario_key": nominal_scenario_key, "render": render.model_dump(mode="json"),
                "reference_geometries": [item.model_dump(mode="json") for item in reference_geometries],
                "datum_transform": datum_transform.model_dump(mode="json") if datum_transform is not None else None})

    async def resolve_cases(self, scope, cases_input, *, case_kind):
        if isinstance(cases_input, InlinePreviewCases):
            cases = cases_input.items
        elif isinstance(cases_input, SavedPreviewCases):
            report = await self.read_case_report(scope, cases_input.operation_alias)
            cases = revise_preview_cases(tuple(item.case for item in report.cases), cases_input.changes,
                operation_alias=cases_input.operation_alias)
        else:
            raise ValueError("Preview cases require a closed inline or saved-operation input")
        if any(item.case_kind != case_kind for item in cases):
            raise ValueError("case input kind does not match the Preview tool")
        return cases

    async def save_case_result(self, scope, admission, result):
        """Persist each complete case before the next one; no second Operation."""
        return await self._artifacts.upload_bytes(owner_id=scope.owner_id,
            task_id=scope.identity.task_id or "", run_id=scope.run_id,
            filename=f"preview_case_{result.case.case_key}.json", content_type="application/json",
            content=canonical_json(result).encode("utf-8"), kind="geometry_preview_case",
            idempotency_key=f"geometry-preview-case:{admission.operation.operation_id}:{result.case.case_key}",
            version_metadata={"operation_id": admission.operation.operation_id,
                "request_digest": admission.operation.request_fingerprint, "case_key": result.case.case_key})

    async def finish_case_report(self, scope, admission, report):
        operation = admission.operation
        content = canonical_json(report).encode("utf-8")
        if len(content) > GEOMETRY_INSPECTION_MANIFEST_MAX_BYTES:
            raise GeometryInspectionApplicationError("INSPECTION_RESULT_BUDGET_EXCEEDED",
                "The finite case report exceeds its bounded result budget", phase="output_validation")
        record = await self._artifacts.upload_bytes(owner_id=scope.owner_id,
            task_id=scope.identity.task_id or "", run_id=scope.run_id,
            filename="preview_case_report.json", content_type="application/json", content=content,
            idempotency_key=f"geometry-preview-report:{operation.operation_id}", kind="geometry_preview_case_report",
            version_metadata={"operation_id": operation.operation_id,
                "request_digest": operation.request_fingerprint, "source_kind": report.source.source_kind,
                "case_count": len(report.cases), "checks_passed": report.checks_passed,
                "source_title": report.source.title,
                "case_index": _case_index(report)})
        await self._read_case_report_artifact(scope, _operation_artifact(record), operation)
        changes = {"status": CriticalOperationStatus.SUCCEEDED, "finished_at": datetime.now(UTC),
            "result_inspection_artifact": _operation_artifact(record)}
        if report.completion == "interrupted":
            changes.update(status=CriticalOperationStatus.FAILED, error_code="PREVIEW_CASES_INTERRUPTED",
                error_phase=CriticalOperationErrorPhase.EXECUTION,
                error_message="The finite Preview batch stopped before every case completed; saved cases remain available.")
        terminal = CriticalOperationRecord.model_validate(operation.model_copy(update=changes).model_dump(mode="python"))
        published = await self._finish(scope, terminal, admission.claim)
        return published.catalog_entry.alias

    async def _report_operation(self, scope, operation_alias):
        entry = await self._repository.get_catalog_entry_by_alias(scope, CatalogResourceKind.OPERATION, operation_alias)
        published = await self._repository.get_operation(scope, entry.operation_id) if entry is not None else None
        if (published is None or published.operation.operation_kind is not CriticalOperationKind.GEOMETRY_INSPECTION
                or published.operation.detail.result_kind != "case_report"):
            raise GeometryInspectionApplicationError("PREVIEW_REPORT_NOT_FOUND",
                "The exact Preview case report is not visible in this scope", phase="target")
        return published

    async def read_case_report(self, scope, operation_alias):
        published = await self._report_operation(scope, operation_alias)
        operation = published.operation
        if operation.result_inspection_artifact is None:
            raise GeometryInspectionApplicationError("PREVIEW_REPORT_UNAVAILABLE",
                "This Operation has no complete saved report; retain its identity for reconciliation", phase="target")
        return await self._read_case_report_artifact(scope, operation.result_inspection_artifact, operation)

    async def saved_version_display_source(self, scope, bundle, parameters):
        """Select an exact saved default-parameter case; never build an entity."""
        published = await self._repository.get_authoring_publication_for_version(
            scope, root_kind="geometry", version_id=bundle.version.resource_version_id)
        if published is None:
            return None
        published = await self._repository.get_operation(scope, published.operation.operation_id)
        if published is None:
            return None
        selection = next((item for item in published.operation.detail.expected_drafts
            if item.root_kind == "geometry" and item.root_id == bundle.resource.resource_id), None)
        if selection is None:
            raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED",
                "Publication does not bind the Geometry's exact draft")
        before = None
        while True:
            previews = await self._repository.get_geometry_case_reports_for_snapshot(scope,
                resource_id=bundle.resource.resource_id, snapshot_version_id=selection.snapshot.version_id,
                before=before, page_size=20)
            for preview in previews:
                if preview.operation.result_inspection_artifact is None:
                    continue
                report = await self._read_case_report_artifact(scope,
                    preview.operation.result_inspection_artifact, preview.operation)
                case = next((item for item in report.cases if item.inspection_artifact is not None
                    and item.case.case_kind == "geometry" and item.case.parameters == parameters), None)
                if case is not None:
                    loaded = await self.load_case_geometry(scope, report, case.case.case_key)
                    return {"source_kind": "geometry_inspection", "source_ref": self.reference(scope, loaded.artifact),
                        "case_key": case.case.case_key, "parameters": case.case.parameters,
                        "title": f"{bundle.resource.display_name} · {case.case.case_key}（默认参数）"}
            if len(previews) < 20:
                return None
            last = previews[-1].operation
            before = (last.started_at, last.operation_id)

    async def _read_case_report_artifact(self, scope, artifact, operation):
        downloaded = await self._artifacts.download_version(scope.owner_id, artifact.artifact_id, artifact.version_id)
        record = downloaded.artifact
        if (record.task_id != scope.identity.task_id or record.sha256 != artifact.sha256
                or record.kind != "geometry_preview_case_report" or record.content_type != "application/json"
                or record.version_metadata.get("operation_id") != operation.operation_id
                or record.version_metadata.get("request_digest") != operation.request_fingerprint):
            raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED", "Saved case report identity changed")
        raw = json.loads(downloaded.content)
        await self._require_current_family_source(scope, raw["source"])
        report = PreviewCaseReport.model_validate(raw)
        source, detail = report.source, operation.detail
        if (source.source_id != detail.source_id or source.snapshot_id != detail.source_version_id
                or source.content_digest != detail.source_content_digest or source.source_ref != detail.source_version_ref
                or source.source_type != detail.source_type or source.draft_artifact != detail.source_draft_artifact):
            raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED", "Saved report source differs from its Operation")
        if (record.version_metadata.get("case_index") is not None
                and record.version_metadata["case_index"] != _case_index(report)):
            raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED", "Saved case index differs from its report")
        return report

    async def _require_current_family_source(self, scope, source):
        if source.get("source_kind") != "family_realization":
            return
        if source.get("source_type") == "draft":
            snapshot = source.get("draft_artifact")
            if snapshot is None:
                raise GeometryInspectionApplicationError("PREVIEW_REPORT_UNAVAILABLE", "The original Family draft snapshot is unavailable", phase="target")
            downloaded = await self._artifacts.download_version(scope.owner_id, snapshot["artifact_id"], snapshot["version_id"])
            if (downloaded.artifact.task_id != scope.identity.task_id
                    or downloaded.artifact.sha256 != snapshot["sha256"]):
                raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED", "Original Family snapshot changed", phase="target")
            raw = json.loads(downloaded.content)
            from CAD2BIM_backend.domain.authoring_draft_content import FamilyDraftContent
            FamilyDraftContent.model_validate(raw)
            current = True
        else:
            from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogNotFoundError
            if self._family_catalog is None:
                raise GeometryInspectionApplicationError("PREVIEW_REPORT_UNAVAILABLE", "Exact Family source authorization is unavailable", phase="target")
            try:
                await self._family_catalog.resolve_exact_version(scope.identity, source["source_ref"])
                current = True
            except ProductFamilyCatalogNotFoundError:
                current = False
        if not current:
            raise GeometryInspectionApplicationError("PREVIEW_REPORT_UNAVAILABLE", "This saved report's exact Family source uses an unavailable contract", phase="target")

    async def load_case_geometry(self, scope, report, case_key):
        case = next((item for item in report.cases if item.case.case_key == case_key), None)
        if case is None or case.inspection_artifact is None:
            raise GeometryInspectionApplicationError("PREVIEW_CASE_HAS_NO_GEOMETRY",
                "This exact case has no saved entity", phase="target")
        loaded = await self.read_artifact(scope, case.inspection_artifact)
        original = loaded.manifest.source_snapshot
        same_source = original == report.source
        if case.execution_source == "reused" and original is not None:
            same_source = ((original.source_kind, original.source_id, original.content_digest)
                == (report.source.source_kind, report.source.source_id, report.source.content_digest))
        if (not same_source
                or self.reference(scope, loaded.artifact) != case.inspection_ref):
            raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED", "Saved case geometry source differs")
        return loaded

    async def _resume_case_report(self, scope, state):
        operation = state.operation
        artifact = operation.result_inspection_artifact
        if artifact is None:
            saved = await self._artifacts.find_by_idempotency_key(scope.owner_id, scope.identity.task_id or "",
                f"geometry-preview-report:{operation.operation_id}")
            artifact = _operation_artifact(saved) if saved is not None else None
        if artifact is not None:
            report = await self._read_case_report_artifact(scope, artifact, operation)
            if operation.status in {CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED}:
                published = await self._repository.get_operation(scope, operation.operation_id)
                if published is None:
                    raise GeometryInspectionApplicationError("INSPECTION_RECONCILIATION_REQUIRED",
                        "The report terminal receipt is not visible", phase="reconcile", retryable=True)
                return GeometryInspectionBuildAdmission(operation, None, operation_alias=published.catalog_entry.alias,
                    replay_report=report)
        if state.execution_owner is None:
            raise GeometryInspectionApplicationError("INSPECTION_ORIGINAL_RESULT_UNAVAILABLE",
                "Original execution ownership is unknown; do not rerun the same action", phase="reconcile", retryable=True)
        claim = await self._repository.reclaim_authoring_execution(scope, operation.operation_id,
            operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION,
            expected_owner=state.execution_owner, expected_generation=state.execution_generation)
        if claim is None:
            raise GeometryInspectionApplicationError("INSPECTION_RECONCILIATION_REQUIRED",
                "The original Preview batch is active or uncertain; retain its identity", phase="reconcile", retryable=True)
        admission = GeometryInspectionBuildAdmission(operation, claim)
        if artifact is None:
            # Reconcile complete per-case records only. An unrecorded invocation
            # is never executed again to guess what happened before interruption.
            request = await self._artifacts.download_version(scope.owner_id,
                operation.detail.request_artifact.artifact_id, operation.detail.request_artifact.version_id)
            if request.artifact.sha256 != operation.detail.request_artifact.sha256:
                raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED", "Saved Preview request changed")
            raw = json.loads(request.content)
            cases = InlinePreviewCases(items=raw["cases"]).items
            results = []
            for case in cases:
                saved = await self._artifacts.find_by_idempotency_key(scope.owner_id, scope.identity.task_id or "",
                    f"geometry-preview-case:{operation.operation_id}:{case.case_key}")
                if saved is not None:
                    value = await self._artifacts.download_version(scope.owner_id, saved.artifact_id, saved.version_id)
                    if (value.artifact.task_id != scope.identity.task_id or value.artifact.kind != "geometry_preview_case"
                            or value.artifact.version_metadata.get("request_digest") != operation.request_fingerprint):
                        raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED", "Saved case changed")
                    result = PreviewCaseResult.model_validate_json(value.content)
                    if result.case != case:
                        raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED", "Saved case input changed")
                else:
                    result = PreviewCaseResult(case=case, execution_status="not_run", execution_source="none",
                        expectation_check="not_checked", diagnostics=(PreviewCaseDiagnostic(code="ORIGINAL_RESULT_UNAVAILABLE",
                            phase="interrupted", message="No complete result survived the interrupted original execution."),))
                results.append(result)
            report = PreviewCaseReport(source=raw["source"], dependencies=raw["dependencies"], cases=tuple(results),
                nominal_scenario_key=raw["nominal_scenario_key"], completion="interrupted",
                coverage_gaps=("Interrupted original batch; only complete saved case records were recovered.",))
        alias = await self.finish_case_report(scope, admission, report)
        return GeometryInspectionBuildAdmission(operation, None, operation_alias=alias, replay_report=report)

    async def begin(self, scope, *, request_identity, source_kind, source_id,
                    source_version_id, source_version_ref, source_content_digest,
                    request_payload, tool_name, result_kind="inspection", source_type="published",
                    source_draft_artifact=None):
        if not request_identity or len(request_identity) > 128 or "\x00" in request_identity:
            raise GeometryInspectionApplicationError("INSPECTION_IDENTITY_INVALID",
                "Retention requires the original server call identity", phase="preflight")
        fingerprint = sha256_digest({"source_kind": source_kind, "source_id": source_id,
            "source_version_id": source_version_id, "source_content_digest": source_content_digest,
            "tool_name": tool_name, "request": request_payload})
        operation_id = stable_internal_identity_id("operation_", "geometry_inspection_operation",
            scope.identity.tenant_id, scope.identity.task_id or "", scope.run_id, request_identity)
        state = await self._repository.get_authoring_execution_state(
            scope, operation_id, operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION)
        if state is not None:
            return await self._resume(scope, state, fingerprint)
        request_bytes = canonical_json(request_payload).encode("utf-8")
        request = await self._artifacts.upload_bytes(owner_id=scope.owner_id,
            task_id=scope.identity.task_id or "", run_id=scope.run_id,
            filename="geometry_inspection_request.json", content_type="application/json",
            content=request_bytes, idempotency_key=f"geometry-inspection-request:{operation_id}",
            kind="geometry_inspection_request")
        prepared = CriticalOperationRecord(operation_id=operation_id, owner_id=scope.owner_id,
            tenant_id=scope.identity.tenant_id, task_id=scope.identity.task_id or "", run_id=scope.run_id,
            operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION,
            initiator=(CriticalOperationInitiator.RUNNER if scope.execution_mode is ExecutionMode.GRAPH
                       else CriticalOperationInitiator.DIRECT_CLIENT), tool_name=tool_name,
            intent_summary=f"Retain actual {source_kind.replace('_', ' ')} for geometric investigation",
            idempotency_key=f"geometry-inspection:{request_identity}", request_fingerprint=fingerprint,
            status=CriticalOperationStatus.PREPARED, started_at=datetime.now(UTC),
            detail=GeometryInspectionOperationDetail(source_kind=source_kind, source_id=source_id,
                source_version_id=source_version_id, source_version_ref=source_version_ref,
                source_content_digest=source_content_digest, request_artifact=_operation_artifact(request),
                result_kind=result_kind, source_type=source_type, source_draft_artifact=source_draft_artifact))
        operation = await self._repository.prepare_geometry_inspection_operation(scope, prepared)
        state = await self._repository.get_authoring_execution_state(
            scope, operation_id, operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION)
        if state is None or operation.request_fingerprint != fingerprint:
            raise GeometryInspectionApplicationError("INSPECTION_RECONCILIATION_REQUIRED",
                "The admitted Operation could not be read exactly", phase="reconcile", retryable=True)
        if state.operation.status is not CriticalOperationStatus.PREPARED:
            return await self._resume(scope, state, fingerprint)
        claim = await self._repository.claim_authoring_execution(
            scope, operation_id, operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION)
        if claim is None:
            raise GeometryInspectionApplicationError("INSPECTION_RECONCILIATION_REQUIRED",
                "The original retained build is active or its execution outcome is uncertain; keep its identity",
                phase="reconcile", retryable=True)
        return GeometryInspectionBuildAdmission(operation, claim)

    async def _resume(self, scope, state, fingerprint):
        operation = state.operation
        if operation.request_fingerprint != fingerprint:
            raise GeometryInspectionApplicationError("INSPECTION_IDEMPOTENCY_CONFLICT",
                "The original call identity was reused with different arguments", phase="preflight")
        if operation.detail.result_kind == "case_report":
            return await self._resume_case_report(scope, state)
        if operation.status is CriticalOperationStatus.FAILED:
            published = await self._repository.get_operation(scope, operation.operation_id)
            raise GeometryInspectionApplicationError(operation.error_code or "INSPECTION_FAILED",
                operation.error_message or "The original retained build failed", phase="reconcile",
                operation_alias=published.catalog_entry.alias if published else None)
        if operation.status is CriticalOperationStatus.SUCCEEDED:
            assert operation.result_inspection_artifact is not None
            loaded = await self.read_artifact(scope, _query_artifact(operation.result_inspection_artifact))
            published = await self._repository.get_operation(scope, operation.operation_id)
            if published is None:
                raise GeometryInspectionApplicationError("INSPECTION_RECONCILIATION_REQUIRED",
                    "The completed Operation has no visible catalog receipt", phase="reconcile", retryable=True)
            return GeometryInspectionBuildAdmission(operation, None, loaded, published.catalog_entry.alias)
        # A retained original can be completed from its exact published bytes.  A
        # request record or timeout alone never authorizes another model execution.
        artifact = await self._artifacts.find_by_idempotency_key(scope.owner_id,
            scope.identity.task_id or "", f"geometry-inspection-result:{operation.operation_id}")
        if artifact is None or state.execution_owner is None:
            raise GeometryInspectionApplicationError("INSPECTION_ORIGINAL_RESULT_UNAVAILABLE",
                "No complete original result can be recovered; do not rebuild under this call identity",
                phase="reconcile", retryable=True)
        claim = await self._repository.reclaim_authoring_execution(scope, operation.operation_id,
            operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION,
            expected_owner=state.execution_owner, expected_generation=state.execution_generation)
        if claim is None:
            raise GeometryInspectionApplicationError("INSPECTION_RECONCILIATION_REQUIRED",
                "The original execution owner is active or unknown; retain the pending identity",
                phase="reconcile", retryable=True)
        loaded = await self.read_artifact(scope, _query_artifact(artifact))
        if loaded.manifest.operation_id != operation.operation_id or loaded.manifest.request_digest != fingerprint:
            raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED",
                "The saved result does not belong to this original action", phase="target")
        published = await self._complete(scope, operation, claim, _operation_artifact(artifact))
        return GeometryInspectionBuildAdmission(published.operation, None, loaded, published.catalog_entry.alias)

    async def fail(self, scope, admission, *, code, message, phase=CriticalOperationErrorPhase.EXECUTION):
        assert admission.claim is not None
        failed = CriticalOperationRecord.model_validate(admission.operation.model_copy(update={
            "status": CriticalOperationStatus.FAILED, "finished_at": datetime.now(UTC),
            "error_code": code, "error_message": _bounded_error_message(
                " ".join(message.split()), fallback="Geometry Inspection request failed"), "error_phase": phase
        }).model_dump(mode="python"))
        return await self._finish(scope, failed, admission.claim)

    async def retain_sample(self, scope, admission, *, bundle, resource_version_ref, parameters,
                            local_geometry, geometry, interfaces, scene, datum_transform=None,
                            references=(), reference_inputs=(), source_snapshot=None,
                            case_key=None, complete_operation=True, display_context=None):
        entries, payloads = [], {}
        _member(entries, payloads, "whole", "whole", geometry)
        _member(entries, payloads, "sample", "fragment", local_geometry,
                binding_key="sample", result_from_local=datum_transform or RigidTransform3D())
        _references(entries, payloads, references, reference_inputs)
        if display_context is not None:
            _member(entries, payloads, "background", "display_context", display_context)
        manifest = GeometryInspectionManifest(owner_id=scope.owner_id, tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id or "", origin_run_id=scope.run_id,
            operation_id=admission.operation.operation_id, request_digest=admission.operation.request_fingerprint,
            created_at=admission.operation.started_at,
            title=source_snapshot.title if source_snapshot is not None else bundle.resource.display_name,
            source_snapshot=source_snapshot, case_key=case_key,
            result_coordinate_space="project_world" if datum_transform else "geometry_local",
            source=GeometryInspectionSampleSource(
                resource_id=source_snapshot.source_id if source_snapshot is not None else bundle.resource.resource_id,
                resource_version_id=source_snapshot.snapshot_id if source_snapshot is not None else bundle.version.resource_version_id,
                resource_version_ref=resource_version_ref,
                content_digest=source_snapshot.content_digest if source_snapshot is not None else bundle.version.content_digest,
                parameters=parameters, datum_transform=datum_transform),
            geometries=tuple(entries), scene=scene, geometry_interfaces=interfaces,
            reference_geometries=references)
        return await self._publish(scope, admission, manifest, payloads, complete_operation=complete_operation)

    async def retain_family(self, scope, admission, *, family_version, family_version_ref,
                            request, execution, scene, references=(), reference_inputs=(),
                            source_snapshot=None, geometry_bindings=(), case_key=None, complete_operation=True,
                            display_context=None):
        if execution.geometry is None or execution.instance_geometries is None:
            raise GeometryInspectionApplicationError("INSPECTION_RESULT_INCOMPLETE",
                "Retention requires the same execution's world and all local instance bytes", phase="publication")
        result, entries, payloads = execution.realization, [], {}
        _member(entries, payloads, "whole", "whole", execution.geometry)
        structure = product_realization_structure(request)
        definitions = {item.fragment_key: item for item in structure.geometry_fragments}
        worlds = {item.component_key: item.project_world_transform for item in result.component_project_world_transforms}
        receipts = {item.fragment_key: item for item in result.instance_receipts}
        for fragment in result.proposal.fragments:
            definition, receipt = definitions[fragment.fragment_key], receipts[fragment.fragment_key]
            _member(entries, payloads, fragment.fragment_key, "fragment",
                execution.instance_geometries[fragment.fragment_key], component_key=definition.owner_component_key,
                binding_key=receipt.binding_key, uniform_scale=receipt.uniform_scale,
                result_from_local=compose_rigid_transform(worlds[definition.owner_component_key], fragment.local_transform))
        _references(entries, payloads, references, reference_inputs)
        if display_context is not None:
            _member(entries, payloads, "background", "display_context", display_context)
        used_bindings = {item.binding_key for item in result.instance_receipts}
        manifest = GeometryInspectionManifest(owner_id=scope.owner_id, tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id or "", origin_run_id=scope.run_id,
            operation_id=admission.operation.operation_id, request_digest=admission.operation.request_fingerprint,
            created_at=admission.operation.started_at,
            title=source_snapshot.title if source_snapshot is not None else family_version.version.display_name,
            source_snapshot=source_snapshot, case_key=case_key,
            result_coordinate_space="project_world", source=GeometryInspectionFamilySource(
                family_id=source_snapshot.source_id if source_snapshot is not None else family_version.family.family_id,
                family_version_id=source_snapshot.snapshot_id if source_snapshot is not None else family_version.version.family_version_id,
                family_version_ref=family_version_ref,
                content_digest=source_snapshot.content_digest if source_snapshot is not None else family_version.version.content_digest,
                invocation=request.invocation.model_copy(update={"public_parameters": result.normalized_public_parameters}),
                geometry_bindings=tuple(item for item in (geometry_bindings if source_snapshot is not None
                    else family_version.geometry_bindings) if item.binding_key in used_bindings)),
            geometries=tuple(entries), scene=scene, family_contract=structure, realization=result,
            reference_geometries=references)
        return await self._publish(scope, admission, manifest, payloads, complete_operation=complete_operation)

    async def _publish(self, scope, admission, manifest, payloads, *, complete_operation=True):
        assert admission.claim is not None
        encoded = canonical_json(manifest.model_dump(mode="json", by_alias=True)).encode("utf-8")
        if len(encoded) > GEOMETRY_INSPECTION_MANIFEST_MAX_BYTES:
            await self._reject_publication(scope, admission,
                "The actual result context exceeds its manifest budget", complete_operation=complete_operation)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as bundle:
            bundle.writestr("manifest.json", encoded)
            for name, content in payloads.items():
                bundle.writestr(name, content)
        content = stream.getvalue()
        if len(content) > GEOMETRY_INSPECTION_MAX_BYTES:
            await self._reject_publication(scope, admission,
                "The actual result bytes exceed their aggregate retention budget", complete_operation=complete_operation)
        suffix = f":{manifest.case_key}" if manifest.case_key is not None else ""
        artifact = await self._artifacts.upload_bytes(owner_id=scope.owner_id,
            task_id=scope.identity.task_id or "", run_id=scope.run_id,
            filename="geometry_inspection.zip", content_type=GEOMETRY_INSPECTION_MEDIA_TYPE,
            content=content, idempotency_key=f"geometry-inspection-result:{admission.operation.operation_id}{suffix}",
            kind="geometry_inspection", version_metadata={
                "request_digest": admission.operation.request_fingerprint, "source_kind": manifest.source.source_kind})
        loaded = await self.read_artifact(scope, _query_artifact(artifact))
        if not complete_operation:
            return loaded, None
        published = await self._complete(scope, admission.operation, admission.claim, _operation_artifact(artifact))
        return loaded, published.catalog_entry.alias

    async def _reject_publication(self, scope, admission, message, *, complete_operation=True):
        if not complete_operation:
            raise GeometryInspectionApplicationError("INSPECTION_RESULT_BUDGET_EXCEEDED", message, phase="publication")
        failed = await self.fail(scope, admission, code="INSPECTION_RESULT_BUDGET_EXCEEDED",
            message=message, phase=CriticalOperationErrorPhase.OUTPUT_VALIDATION)
        raise GeometryInspectionApplicationError("INSPECTION_RESULT_BUDGET_EXCEEDED", message,
            phase="publication", operation_alias=failed.catalog_entry.alias)

    async def _complete(self, scope, operation, claim, artifact):
        terminal = CriticalOperationRecord.model_validate(operation.model_copy(update={
            "status": CriticalOperationStatus.SUCCEEDED, "finished_at": datetime.now(UTC),
            "result_inspection_artifact": artifact
        }).model_dump(mode="python"))
        return await self._finish(scope, terminal, claim)

    async def _finish(self, scope, terminal, claim):
        try:
            return await self._repository.complete_geometry_inspection_operation(scope, terminal, execution_claim=claim)
        except Exception as exc:
            state = await self._repository.get_authoring_execution_state(
                scope, terminal.operation_id, operation_kind=CriticalOperationKind.GEOMETRY_INSPECTION)
            if (state is not None and state.operation.status is terminal.status
                    and state.operation.request_fingerprint == terminal.request_fingerprint
                    and state.operation.result_inspection_artifact == terminal.result_inspection_artifact
                    and state.operation.error_code == terminal.error_code
                    and state.operation.error_phase == terminal.error_phase):
                published = await self._repository.get_operation(scope, terminal.operation_id)
                if published is not None:
                    return published
            raise GeometryInspectionApplicationError("INSPECTION_RECONCILIATION_REQUIRED",
                "The original Operation terminal commit outcome is unresolved; retain its identity and any saved result",
                phase="reconcile", retryable=True) from exc

    def reference(self, scope, artifact):
        return self._codec.encode({"domain": "geometry_inspection_reference",
            "scope": catalog_scope_digest(scope.identity), "artifact_id": artifact.artifact_id,
            "version_id": artifact.version_id, "sha256": artifact.sha256})

    async def origin_navigation(self, scope, loaded):
        from CAD2BIM_backend.application.opaque_reference_service import encode_operation_reference
        manifest = loaded.manifest
        published = await self._repository.get_operation_by_locator(scope,
            origin_run_id=manifest.origin_run_id, operation_id=manifest.operation_id)
        if published is None:
            return {"operation_alias": None, "operation_ref": None, "case_key": manifest.case_key}
        if published.operation.request_fingerprint != manifest.request_digest:
            raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED", "Saved model origin differs from its Operation")
        return {"operation_alias": published.catalog_entry.alias if manifest.origin_run_id == scope.run_id else None,
            "operation_ref": encode_operation_reference(self._codec, scope.identity,
                origin_run_id=manifest.origin_run_id, operation_id=manifest.operation_id), "case_key": manifest.case_key}

    async def read(self, scope, inspection_ref):
        try:
            claims = self._codec.decode(inspection_ref)
            if (claims.get("domain") != "geometry_inspection_reference"
                    or claims.get("scope") != catalog_scope_digest(scope.identity)):
                raise ValueError("scope or kind differs")
            reference = GeometryQueryArtifactRef(**{key: claims[key] for key in ("artifact_id", "version_id", "sha256")})
        except Exception as exc:
            raise GeometryInspectionApplicationError("INSPECTION_NOT_FOUND",
                "The exact result reference is invalid or unavailable in this scope", phase="target") from exc
        return await self.read_artifact(scope, reference)

    async def read_artifact(self, scope, artifact):
        record = await self._metadata(scope, artifact)
        try:
            downloaded = await self._artifacts.download_version(scope.owner_id, record.artifact_id, record.version_id)
        except NotFoundError as exc:
            raise GeometryInspectionApplicationError("INSPECTION_NOT_FOUND",
                "The exact retained result is no longer available in this scope", phase="target") from exc
        except ConflictError as exc:
            raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED",
                "The exact retained bytes are missing or damaged; do not rebuild them", phase="target") from exc
        except Exception as exc:
            raise GeometryInspectionApplicationError("INSPECTION_READ_UNAVAILABLE",
                "The retained result could not be read; retry the same reference without rebuilding",
                phase="target", retryable=True) from exc
        try:
            manifest, geometries = _unpack(downloaded.content)
            if (manifest.owner_id != scope.owner_id or manifest.tenant_id != scope.identity.tenant_id
                    or manifest.task_id != scope.identity.task_id
                    or manifest.request_digest != record.version_metadata.get("request_digest")
                    or manifest.source.source_kind != record.version_metadata.get("source_kind")):
                raise ValueError("manifest authority differs")
        except (ValueError, KeyError, zipfile.BadZipFile) as exc:
            raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED",
                "The exact retained bytes or manifest are missing or damaged; do not rebuild them", phase="target") from exc
        return LoadedGeometryInspection(artifact, manifest, geometries)

    async def _metadata(self, scope, artifact):
        try:
            record = await self._artifacts.get_version_metadata(scope.owner_id, artifact.artifact_id, artifact.version_id)
        except NotFoundError as exc:
            raise GeometryInspectionApplicationError("INSPECTION_NOT_FOUND",
                "The exact retained result is not available in this scope", phase="target") from exc
        except Exception as exc:
            raise GeometryInspectionApplicationError("INSPECTION_READ_UNAVAILABLE",
                "The retained result metadata could not be read; retry the same reference without rebuilding",
                phase="target", retryable=True) from exc
        if (record.owner_id != scope.owner_id or record.task_id != scope.identity.task_id
                or record.sha256 != artifact.sha256 or record.kind != "geometry_inspection"
                or record.content_type != GEOMETRY_INSPECTION_MEDIA_TYPE):
            raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED",
                "The retained result identity, scope or format does not match", phase="target")
        return record

    def validate_selection(self, loaded, selection, coordinates):
        try:
            require_inspection_coordinate_selection(selection, coordinates)
            fragments = [item for item in loaded.manifest.geometries if item.role == "fragment"]
            if isinstance(selection, InspectionFragmentSelection) and not any(item.object_key == selection.fragment_key for item in fragments):
                raise ValueError("fragment is not present in this exact result")
            if isinstance(selection, InspectionComponentSelection) and not any(item.component_key == selection.component_key for item in fragments):
                raise ValueError("component is not present in this exact result")
        except ValueError as exc:
            raise GeometryInspectionApplicationError("INSPECTION_SELECTION_INVALID", str(exc), phase="preflight") from exc

    async def read_preview_operation(self, scope, published, *, references,
                                     case_key=None, after_case_key=None, page_size=1,
                                     selection=None, saved=None):
        try:
            return await self._preview_operation_page(scope, published, references=references,
                case_key=case_key, after_case_key=after_case_key, page_size=page_size,
                selection=selection, saved=saved)
        except GeometryInspectionApplicationError as exc:
            record = published.operation
            exc.operation_status = record.status.value
            exc.operation_alias = published.catalog_entry.alias if record.run_id == scope.run_id else None
            if references is not None:
                exc.operation_ref = references.operation_reference(scope.identity,
                    origin_run_id=record.run_id, operation_id=record.operation_id)
            raise

    async def _preview_operation_page(self, scope, published, *, references,
                                      case_key=None, after_case_key=None, page_size=1,
                                      selection=None, saved=None):
        """Prepare public references, then bound the original report before HTTP."""
        from CAD2BIM_backend.application.preview_report_projection import (
            project_case_outcome, external_target_comparison, public_response_bytes,
        )
        from CAD2BIM_backend.application.reference_projection import (
            project_reference_fields, reference_schema_projection,
        )
        from CAD2BIM_backend.domain.preview_report_read import (
            PreviewReadOperation, PreviewOperationRead, PreviewContentDescriptor,
            PreviewCaseSummary, PreviewReportSummary, PreviewReportText,
        )
        from CAD2BIM_backend.domain.preview_cases import PreviewCaseResult

        if references is None:
            raise GeometryInspectionApplicationError("PREVIEW_REPORT_NOT_AVAILABLE", "Exact Operation reference navigation is unavailable", phase="read")
        record = published.operation
        saved = saved or await self.operation_result(scope, published, include_request=True)
        if saved is None or saved.case_report is None:
            raise GeometryInspectionApplicationError("PREVIEW_REPORT_NOT_AVAILABLE", "The original Operation has no saved case report", phase="read")
        report = saved.case_report
        keys = tuple(item.case.case_key for item in report.cases)
        if case_key is not None and after_case_key is not None:
            raise GeometryInspectionApplicationError("INVALID_ARGUMENT", "case_key and after_case_key cannot be combined", phase="input_validation")
        for name, value in (("case_key", case_key), ("after_case_key", after_case_key)):
            if value is not None and value not in keys:
                raise GeometryInspectionApplicationError("INVALID_ARGUMENT", f"{name} is absent from this saved report", phase="input_validation")

        prepared_references = {}
        async def prepare(value, schema):
            _, paths = reference_schema_projection(schema)
            async def translate(value, kinds):
                key = (value, tuple(kinds))
                if key not in prepared_references:
                    prepared_references[key] = await references.shorten(scope.identity, value, kinds)
                return prepared_references[key]
            return await project_reference_fields(value, paths, translate)

        reference = references.operation_reference(scope.identity, origin_run_id=record.run_id,
                     operation_id=record.operation_id)
        identity = PreviewReadOperation(operation_ref=reference,
            operation_alias=published.catalog_entry.alias if record.run_id == scope.run_id else None,
            sequence=published.catalog_entry.ordinal, tool_name=record.tool_name,
            outcome=record.status.value, started_at=record.started_at, finished_at=record.finished_at,
            error_code=record.error_code, error_phase=record.error_phase.value if record.error_phase else None,
            error_message=record.error_message)
        identity = PreviewReadOperation.model_validate(await prepare(identity.model_dump(mode="json"), identity.model_json_schema()))
        request = json.loads(saved.request_json) if saved.request_json else {}
        report_values = {
            "dependencies": [{"source_ref": item.source_ref} for item in report.dependencies],
            "coverage_gaps": list(report.coverage_gaps),
            "cross_case_diagnostics": [item.model_dump(mode="json") for item in report.cross_case_diagnostics],
            "placement_input": request.get("datum_transform") if report.source.source_kind == "geometry_sample" else None,
            "observation_input": {key: request.get(key) for key in ("render", "reference_geometries")},
        }
        dependency_schema = {"type":"array", "items":{"type":"object", "properties":{"source_ref":{"type":"string"}}}}
        report_values["dependencies"] = await prepare(report_values["dependencies"], dependency_schema)
        # Observation's reference geometry DTO owns typed references; arbitrary
        # user dictionaries remain opaque under the existing schema walker.
        from CAD2BIM_backend.domain.preview_cases import PreviewCaseRender
        from pydantic import TypeAdapter
        render_schema = TypeAdapter(PreviewCaseRender).json_schema()
        report_values["observation_input"]["render"] = await prepare(report_values["observation_input"]["render"], render_schema)
        from CAD2BIM_backend.domain.geometry_catalog import GeometryPreviewReference
        reference_values = request.get("reference_geometries")
        if reference_values is not None:
            public_references = [GeometryPreviewReference(**{
                key: item[key] for key in GeometryPreviewReference.model_fields
            }).model_dump(mode="json") for item in reference_values]
            report_values["observation_input"]["reference_geometries"] = await prepare(
                public_references, TypeAdapter(list[GeometryPreviewReference]).json_schema())
        text_cache = {}
        async def content(kind, key=None):
            cache_key = (kind, key)
            if cache_key not in text_cache:
                if kind in {"case_request", "case_outcome"}:
                    if key not in keys:
                        raise GeometryInspectionApplicationError("INVALID_ARGUMENT", "case_key is absent from this saved report", phase="input_validation")
                    item = report.cases[keys.index(key)]
                    value = item.case.model_dump(mode="json") if kind == "case_request" else project_case_outcome(item)
                    schema = type(item.case).model_json_schema() if kind == "case_request" else PreviewCaseResult.model_json_schema()
                    value = await prepare(value, schema)
                else:
                    value = report_values[kind]
                text_cache[cache_key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            return text_cache[cache_key]

        async def descriptor(kind, key=None):
            text = await content(kind, key)
            return PreviewContentDescriptor(content_kind=kind, total_characters=len(text), total_utf8_bytes=len(text.encode("utf-8")))

        if selection is not None:
            key = getattr(selection, "case_key", None)
            text = await content(selection.content_kind, key)
            start = selection.start_character
            if start > len(text):
                raise GeometryInspectionApplicationError("INVALID_ARGUMENT", "start_character exceeds total_characters", phase="input_validation")
            end = min(len(text), start + selection.character_count)
            while True:
                detail = PreviewReportText(**(await descriptor(selection.content_kind, key)).model_dump(),
                    case_key=key, start_character=start, returned_characters=end-start,
                    text=text[start:end], complete=end == len(text), next_character=end if end < len(text) else None)
                result = PreviewOperationRead(operation=identity, detail=detail)
                if public_response_bytes(result) <= 64 * 1024:
                    return result
                if end == start:
                    raise GeometryInspectionApplicationError("OPERATION_GET_RESPONSE_TOO_LARGE", "The original Preview navigation metadata exceeds 64 KiB; retain the original Operation reference and outcome", phase="output_validation", operation_alias=identity.operation_alias)
                end = start + (end-start)//2

        selected = (report.cases[keys.index(case_key)],) if case_key else report.cases[
            0 if after_case_key is None else keys.index(after_case_key)+1:][:page_size]
        summaries=[]
        report_contents=tuple([await descriptor(kind) for kind in report_values])
        source_ref = await prepare({"source_ref":report.source.source_ref}, {"type":"object","properties":{"source_ref":{"type":"string"}}})
        def page():
            last = summaries[-1].case_key if summaries else after_case_key
            complete = case_key is not None or (last == keys[-1])
            return PreviewOperationRead(operation=identity, detail=PreviewReportSummary(
                source_kind=report.source.source_kind, source_ref=source_ref["source_ref"],
                completion=report.completion, checks_passed=report.checks_passed, case_count=len(keys),
                cases=tuple(summaries), contents=report_contents, nominal_scenario_key=report.nominal_scenario_key,
                complete=complete, next_after_case_key=None if complete else last))
        for item in selected:
            summary = PreviewCaseSummary(case_key=item.case.case_key, execution_status=item.execution_status,
                execution_source=item.execution_source, status=item.geometry_status if item.realization is None else item.realization.proposal.status.value,
                expectation_check=item.expectation_check, external_target_comparison=None if item.realization is None else external_target_comparison(item.realization),
                inspection_ref=item.inspection_ref, render_status=item.render_status, diagnostic_count=len(item.diagnostics),
                contents=tuple([await descriptor(kind, item.case.case_key) for kind in ("case_request", "case_outcome")]))
            summary = PreviewCaseSummary.model_validate(await prepare(summary.model_dump(mode="json"), summary.model_json_schema()))
            summaries.append(summary)
            if public_response_bytes(page()) > 64 * 1024:
                summaries.pop()
                if not summaries:
                    raise GeometryInspectionApplicationError("OPERATION_GET_RESPONSE_TOO_LARGE", "The original Preview navigation metadata exceeds 64 KiB; use preview_report with this Operation reference and exact case_key", phase="output_validation", operation_alias=identity.operation_alias)
                break
        result=page()
        if public_response_bytes(result)>64*1024:
            raise GeometryInspectionApplicationError("OPERATION_GET_RESPONSE_TOO_LARGE", "The original Preview navigation metadata exceeds 64 KiB", phase="output_validation", operation_alias=identity.operation_alias)
        return result

    async def operation_result(self, scope, published, *, include_request=False):
        operation = published.operation
        if operation.operation_kind is not CriticalOperationKind.GEOMETRY_INSPECTION:
            return None
        detail = operation.detail
        assert isinstance(detail, GeometryInspectionOperationDetail)
        await self._require_current_family_source(scope, {
            "source_kind": detail.source_kind, "source_type": detail.source_type,
            "source_ref": detail.source_version_ref,
            "draft_artifact": detail.source_draft_artifact.model_dump(mode="json") if detail.source_draft_artifact else None,
        })
        if detail.result_kind == "case_report":
            artifact = operation.result_inspection_artifact
            report = None
            count = passed = None
            title = None
            if artifact is not None:
                record = await self._artifacts.get_version_metadata(scope.owner_id, artifact.artifact_id, artifact.version_id)
                if (record.task_id != scope.identity.task_id or record.sha256 != artifact.sha256
                        or record.kind != "geometry_preview_case_report"):
                    raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED", "Saved Preview report metadata changed")
                count, passed = record.version_metadata.get("case_count"), record.version_metadata.get("checks_passed")
                title = record.version_metadata.get("source_title")
                if include_request:
                    report = await self._read_case_report_artifact(scope, artifact, operation)
            case_index = ()
            if artifact is not None:
                if record.version_metadata.get("case_index") is None:
                    report = report or await self._read_case_report_artifact(scope, artifact, operation)
                    case_index = tuple(_case_index(report))
                else:
                    case_index = tuple(record.version_metadata["case_index"])
            request_json = None
            if include_request:
                downloaded = await self._artifacts.download_version(scope.owner_id,
                    detail.request_artifact.artifact_id, detail.request_artifact.version_id)
                if downloaded.artifact.sha256 != detail.request_artifact.sha256:
                    raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED", "Saved Preview request changed")
                request_json = canonical_json(json.loads(downloaded.content))
            return GeometryInspectionOperationResult(None, detail.source_kind, detail.source_version_ref,
                (title or (report.source.title if report else None)
                 or operation.intent_summary or "Saved finite Preview cases"), request_json,
                result_kind="case_report", case_report=report, case_count=count, checks_passed=passed,
                case_index=case_index)
        inspection_ref = None
        if operation.status is CriticalOperationStatus.SUCCEEDED:
            assert operation.result_inspection_artifact is not None
            artifact = _query_artifact(operation.result_inspection_artifact)
            await self._metadata(scope, artifact)  # List projection never loads a BREP or big request.
            inspection_ref = self.reference(scope, artifact)
        request_json = None
        if include_request:
            downloaded = await self._artifacts.download_version(scope.owner_id,
                detail.request_artifact.artifact_id, detail.request_artifact.version_id)
            if (downloaded.artifact.task_id != scope.identity.task_id
                    or downloaded.artifact.sha256 != detail.request_artifact.sha256
                    or downloaded.artifact.kind != "geometry_inspection_request"):
                raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED", "Original retained request changed")
            request_json = canonical_json(json.loads(downloaded.content))
        return GeometryInspectionOperationResult(inspection_ref, detail.source_kind,
            detail.source_version_ref, operation.intent_summary or "Retained actual build result", request_json)


def _case_index(report):
    return [{"case_key": item.case.case_key, "execution_status": item.execution_status,
             "status": item.geometry_status if item.realization is None else item.realization.proposal.status.value,
             "expectation_check": item.expectation_check, "inspection_ref": item.inspection_ref}
            for item in report.cases]


def _operation_artifact(record):
    return ArtifactVersionRef(artifact_id=record.artifact_id, version_id=record.version_id, sha256=record.sha256)


def _query_artifact(record):
    return GeometryQueryArtifactRef(artifact_id=record.artifact_id, version_id=record.version_id, sha256=record.sha256)


def _member(entries, payloads, key, role, geometry, **facts):
    if hashlib.sha256(geometry.payload).hexdigest() != geometry.sha256:
        raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED", "Actual build bytes differ from their receipt")
    member = f"geometry/{geometry.sha256}.bin"
    if member in payloads and payloads[member] != geometry.payload:
        raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED", "Actual byte identities conflict")
    payloads[member] = geometry.payload
    entries.append(GeometryInspectionGeometryMember(object_key=key, role=role, member=member,
        format=geometry.format, content_type=geometry.content_type, sha256=geometry.sha256,
        size_bytes=len(geometry.payload), summary=geometry.summary, **facts))


def _references(entries, payloads, references, inputs):
    if len(references) != len(inputs):
        raise GeometryInspectionApplicationError("INSPECTION_RESULT_INCOMPLETE", "Reference bytes and declared reference layer differ")
    for reference, geometry in zip(references, inputs, strict=True):
        if reference.geometry_sha256 != geometry.sha256:
            raise GeometryInspectionApplicationError("INSPECTION_INTEGRITY_FAILED", "Reference geometry identity changed")
        _member(entries, payloads, reference.reference_key, "reference", geometry,
                result_from_local=reference.datum_transform)


def _unpack(content):
    if not content or len(content) > GEOMETRY_INSPECTION_MAX_BYTES:
        raise ValueError("inspection bundle byte budget exceeded")
    with zipfile.ZipFile(io.BytesIO(content)) as bundle:
        infos = bundle.infolist()
        if len({item.filename for item in infos}) != len(infos):
            raise ValueError("inspection members are repeated")
        if any(item.compress_type != zipfile.ZIP_STORED for item in infos):
            raise ValueError("inspection encoding differs")
        manifest_info = bundle.getinfo("manifest.json")
        if manifest_info.file_size > GEOMETRY_INSPECTION_MANIFEST_MAX_BYTES:
            raise ValueError("inspection manifest byte budget exceeded")
        raw = json.loads(bundle.read("manifest.json"))
        manifest = GeometryInspectionManifest.model_validate(raw)
        expected = {"manifest.json", *(item.member for item in manifest.geometries)}
        if {item.filename for item in infos} != expected:
            raise ValueError("inspection bundle is incomplete or has unclaimed bytes")
        payloads, geometries = {}, {}
        for member in manifest.geometries:
            info = bundle.getinfo(member.member)
            if info.file_size != member.size_bytes:
                raise ValueError("inspection asset size changed")
            payload = payloads.setdefault(member.member, bundle.read(member.member))
            if hashlib.sha256(payload).hexdigest() != member.sha256:
                raise ValueError("inspection asset digest changed")
            geometries[(member.role, member.object_key)] = CanonicalGeometry(format=member.format,
                content_type=member.content_type, payload=payload, sha256=member.sha256, summary=member.summary)
    return manifest, geometries
