from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from CAD2BIM_backend.application.artifact_service import ArtifactDownload
from CAD2BIM_backend.application.errors import ConflictError, NotFoundError
from CAD2BIM_backend.application.geometry_inspection_service import (
    GeometryInspectionApplicationError, GeometryInspectionService, _unpack,
)
from CAD2BIM_backend.domain.control_plane import ArtifactRecord, ExecutionMode
from CAD2BIM_backend.domain.data_runtime import AuthoringExecutionClaim, CriticalOperationStatus
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.geometry_inspection import (
    InspectionFragmentLocalCoordinates, InspectionFragmentSelection, InspectionResultCoordinates, InspectionWholeSelection,
)
from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionModelObservation
from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry, CanonicalGeometryFormat, GeometryPreview, GeometryRenderScene
from CAD2BIM_backend.domain.preview_cases import (
    GeometryCaseExpectation, GeometryPreviewCase, PreviewSelectedRender, PreviewSourceSnapshot,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.worker_runtime.capacity import HostWorkerCapacity, current_execution_lease

pytestmark = pytest.mark.asyncio


class _Artifacts:
    def __init__(self):
        self.records = {}
        self.content = {}

    async def upload_bytes(self, **values):
        key = values["idempotency_key"]
        if key in self.records:
            record = self.records[key]
            assert self.content[record.artifact_id] == values["content"]
            return record
        number = len(self.records)
        record = ArtifactRecord(artifact_id=f"asset-{number}", version_id=f"version-{number}",
            owner_id=values["owner_id"], task_id=values["task_id"], run_id=values["run_id"],
            kind=values["kind"], filename=values["filename"], content_type=values["content_type"],
            size_bytes=len(values["content"]), sha256=hashlib.sha256(values["content"]).hexdigest(),
            idempotency_key=key, created_at=datetime.now(UTC), version_metadata=values.get("version_metadata", {}))
        self.records[key], self.content[record.artifact_id] = record, values["content"]
        return record

    async def find_by_idempotency_key(self, owner, task, key):
        return self.records.get(key)

    async def get_version_metadata(self, owner, artifact_id, version_id):
        record = next((record for record in self.records.values()
                       if record.owner_id == owner and record.artifact_id == artifact_id and record.version_id == version_id), None)
        if record is None:
            raise NotFoundError("exact artifact version is unavailable")
        return record

    async def download_version(self, owner, artifact_id, version_id):
        record = await self.get_version_metadata(owner, artifact_id, version_id)
        content = self.content[artifact_id]
        if hashlib.sha256(content).hexdigest() != record.sha256:
            raise ConflictError("actual bytes changed")
        return ArtifactDownload(record, content)


class _Operations:
    def __init__(self):
        self.states, self.published = {}, {}
        self.lose_commit_response = False

    async def get_authoring_execution_state(self, scope, operation_id, **kwargs):
        return self.states.get(operation_id)

    async def prepare_geometry_inspection_operation(self, scope, operation):
        if operation.operation_id not in self.states:
            self.states[operation.operation_id] = SimpleNamespace(operation=operation,
                execution_owner=None, execution_generation=0)
        return self.states[operation.operation_id].operation

    async def claim_authoring_execution(self, scope, operation_id, **kwargs):
        state = self.states[operation_id]
        if state.execution_owner:
            return None
        state.execution_owner, state.execution_generation = "live-owner", 1
        return AuthoringExecutionClaim(operation_id=operation_id, owner="live-owner", generation=1, token="a" * 32)

    async def reclaim_authoring_execution(self, scope, operation_id, **kwargs):
        return None

    async def complete_geometry_inspection_operation(self, scope, operation, **kwargs):
        self.states[operation.operation_id].operation = operation
        published = SimpleNamespace(operation=operation, catalog_entry=SimpleNamespace(
            alias=f"op_{len(self.published)+1:03}", ordinal=len(self.published)+1))
        self.published[operation.operation_id] = published
        if self.lose_commit_response:
            self.lose_commit_response = False
            raise RuntimeError("response lost after commit")
        return published

    async def get_operation(self, scope, operation_id):
        return self.published.get(operation_id)

    async def get_operation_by_locator(self, scope, *, origin_run_id, operation_id):
        published = self.published.get(operation_id)
        if published is None:
            return None
        record = published.operation
        return published if (record.run_id == origin_run_id and record.owner_id == scope.owner_id
            and record.task_id == scope.identity.task_id and record.tenant_id == scope.identity.tenant_id) else None


def _setup():
    artifacts, operations = _Artifacts(), _Operations()
    codec = CursorCodec(base64.urlsafe_b64encode(b"i" * 32).rstrip(b"=").decode())
    scope = InvocationScope(identity=TenantExecutionContext(tenant_id="tenant", principal_id="author",
        task_id="task", actor_kind=TenantActorKind.USER, command_id="call"), owner_id="tenant",
        run_id="run", execution_mode=ExecutionMode.DIRECT_MCP, catalog_frontier=None)
    service = GeometryInspectionService(artifacts, operations, codec)
    arguments = dict(request_identity="call-1", source_kind="geometry_sample", source_id="geometry",
        source_version_id="geometry-v1", source_version_ref="cur.exact-source",
        source_content_digest="a" * 64, request_payload={"parameters": {"length_m": 4}}, tool_name="ifc_geometry_preview")
    return artifacts, operations, codec, scope, service, arguments


class _CapacityRenderer:
    def __init__(self, tmp_path, *, fail_render_at=None):
        self.capacity = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path)
        self.fail_render_at = fail_render_at
        self.leases = []

    def invocation_scope(self, *args, **kwargs):
        return self.capacity.invocation_scope(*args, **kwargs)

    def execution_scope(self, *args, **kwargs):
        return self.capacity.execution_scope(*args, **kwargs)

    def release_execution(self):
        lease = current_execution_lease()
        if lease is not None:
            lease.release()

    async def render(self, request):
        lease = await self.capacity.admit_current_execution()
        self.leases.append(lease)
        if len(self.leases) == self.fail_render_at:
            raise RuntimeError("renderer unavailable")
        payload = b"\x89PNG\r\n\x1a\ncapacity-render"
        return GeometryPreview(width_px=request.width_px, height_px=request.height_px,
            payload=payload, sha256=hashlib.sha256(payload).hexdigest())


class _ConnectionRenderer(_CapacityRenderer):
    def __init__(self, tmp_path, *, fail_context=False):
        super().__init__(tmp_path)
        self.built_contexts, self.requests = [], []
        self.context_attempts = 0
        self.fail_context = fail_context

    async def build_display_context(self, request):
        from CAD2BIM_backend.domain.canonical import sha256_digest
        from CAD2BIM_backend.domain.geometry_runtime import GeometryBuildDisplayContextResult
        self.context_attempts += 1
        if self.fail_context:
            raise RuntimeError("background construction failed")
        self.built_contexts.append(request)
        payload = request.model_dump_json().encode()
        return GeometryBuildDisplayContextResult(geometry=CanonicalGeometry(
            format=CanonicalGeometryFormat.OCCT_BREP_V1, content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
            payload=payload, sha256=hashlib.sha256(payload).hexdigest()), recipe_digest=sha256_digest(request),
            recipe_keys=tuple(item.recipe_key for item in request.recipes))

    async def render(self, request):
        self.requests.append(request)
        return await super().render(request)


async def _retain(service, scope, admission):
    payload = b"unit-test-actual-build-bytes"
    geometry = CanonicalGeometry(format=CanonicalGeometryFormat.OCCT_BREP_V1,
        content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
        payload=payload, sha256=hashlib.sha256(payload).hexdigest())
    bundle = SimpleNamespace(resource=SimpleNamespace(resource_id="geometry", display_name="Sample"),
        version=SimpleNamespace(resource_version_id="geometry-v1", content_digest="a" * 64))
    return await service.retain_sample(scope, admission, bundle=bundle,
        resource_version_ref="cur.exact-source", parameters={"length_m": 4},
        local_geometry=geometry, geometry=geometry, interfaces=(),
        scene=GeometryRenderScene(geometry_sha256=geometry.sha256, coordinate_space="geometry_coordinates"))


async def test_retired_family_snapshots_are_unavailable_while_geometry_stays_readable():
    artifacts, _, _, scope, service, arguments = _setup()
    admission = await service.begin(scope, **arguments)
    loaded, _ = await _retain(service, scope, admission)
    assert (await service.read(scope, service.reference(scope, loaded.artifact))).geometry.payload
    original = artifacts.content[loaded.artifact.artifact_id]
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        members = {item.filename: archive.read(item.filename) for item in archive.infolist()}
    manifest = json.loads(members["manifest.json"])
    manifest.update(source={"source_kind": "family_realization"},
        family_contract={"schema": "product_family_contract_v4"},
        realization={"abi_profile": "product_realization_v4"})
    members["manifest.json"] = json.dumps(manifest).encode()
    saved = io.BytesIO()
    with zipfile.ZipFile(saved, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    with pytest.raises(ValueError):
        _unpack(saved.getvalue())
    snapshot = await artifacts.upload_bytes(owner_id=scope.owner_id, task_id=scope.identity.task_id,
        run_id=scope.run_id, filename="draft.json", content_type="application/json",
        kind="authoring_draft", idempotency_key="old-family-draft",
        content=json.dumps({"definition": {"family_contract": {"schema": "product_family_contract_v4"}}}).encode())
    with pytest.raises(ValueError):
        await service._require_current_family_source(scope, {"source_kind": "family_realization",
            "source_type": "draft", "draft_artifact": {"artifact_id": snapshot.artifact_id,
                "version_id": snapshot.version_id, "sha256": snapshot.sha256}})
    assert artifacts.content[loaded.artifact.artifact_id] == original
    assert (await service.read(scope, service.reference(scope, loaded.artifact))).artifact == loaded.artifact


async def test_authoring_composition_supplies_the_exact_family_catalog(tmp_path):
    from CAD2BIM_backend.ifc_application_resources import create_authoring_application_resources
    artifacts, operations, cursor, _, _, _ = _setup()
    resources = create_authoring_application_resources(repository=operations, storage=object(),
        artifacts=artifacts, cursor_codec=cursor,
        capacity=HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path / "capacity"))
    try:
        assert resources.geometry_inspection._family_catalog is resources.family_catalog
        assert resources.geometry_query._family_catalog is resources.family_catalog
        assert resources.realization_preview._family_catalog is resources.family_catalog
    finally:
        await resources.close()


async def test_retained_operation_replay_restart_and_list_get_share_original_bytes():
    artifacts, operations, codec, scope, service, arguments = _setup()
    admission = await service.begin(scope, **arguments)
    operations.lose_commit_response = True
    loaded, alias = await _retain(service, scope, admission)
    assert alias == "op_001" and operations.states[admission.operation.operation_id].operation.status is CriticalOperationStatus.SUCCEEDED
    restart = GeometryInspectionService(artifacts, operations, codec)
    replay = await restart.begin(scope, **arguments)
    assert replay.claim is None and replay.replay.artifact == loaded.artifact
    assert replay.replay.geometry.payload == loaded.geometry.payload
    published = operations.published[admission.operation.operation_id]
    listing = await restart.operation_result(scope, published)
    details = await restart.operation_result(scope, published, include_request=True)
    assert listing.inspection_ref == details.inspection_ref == restart.reference(scope, loaded.artifact)
    assert listing.request_json is None and details.request_json == '{"parameters":{"length_m":4}}'
    assert len(artifacts.records) == 2


async def test_same_inputs_new_call_gets_new_action_and_result_identity():
    artifacts, operations, codec, scope, service, arguments = _setup()
    first = await service.begin(scope, **arguments)
    first_result, _ = await _retain(service, scope, first)
    second = await service.begin(scope, **{**arguments, "request_identity": "call-2"})
    second_result, _ = await _retain(service, scope, second)
    assert first.operation.operation_id != second.operation.operation_id
    assert first_result.artifact != second_result.artifact
    assert first_result.geometry.payload == second_result.geometry.payload


async def test_pending_identity_never_admits_second_build_and_conflicting_input_is_rejected():
    _, _, _, scope, service, arguments = _setup()
    await service.begin(scope, **arguments)
    with pytest.raises(GeometryInspectionApplicationError) as pending:
        await service.begin(scope, **arguments)
    assert pending.value.code == "INSPECTION_ORIGINAL_RESULT_UNAVAILABLE"
    with pytest.raises(GeometryInspectionApplicationError) as conflict:
        await service.begin(scope, **{**arguments, "request_payload": {"parameters": {"length_m": 5}}})
    assert conflict.value.code == "INSPECTION_IDEMPOTENCY_CONFLICT"


async def test_exact_ref_rejects_other_task_and_corruption_without_rebuilding():
    artifacts, operations, _, scope, service, arguments = _setup()
    loaded, _ = await _retain(service, scope, await service.begin(scope, **arguments))
    reference = service.reference(scope, loaded.artifact)
    other = scope.model_copy(update={"identity": scope.identity.model_copy(update={"task_id": "other-task"})})
    with pytest.raises(GeometryInspectionApplicationError) as hidden:
        await service.read(other, reference)
    assert hidden.value.code == "INSPECTION_NOT_FOUND"
    service.validate_selection(loaded, InspectionFragmentSelection(fragment_key="sample"),
                               InspectionFragmentLocalCoordinates(fragment_key="sample"))
    with pytest.raises(GeometryInspectionApplicationError):
        service.validate_selection(loaded, InspectionFragmentSelection(fragment_key="absent"), InspectionResultCoordinates())
    artifacts.content[loaded.artifact.artifact_id] += b"damaged"
    with pytest.raises(GeometryInspectionApplicationError) as corrupt:
        await service.read(scope, reference)
    assert corrupt.value.code == "INSPECTION_INTEGRITY_FAILED"
    assert not corrupt.value.retryable
    assert len(operations.published) == 1


@pytest.mark.parametrize(("method", "failure"), [
    ("get_version_metadata", ConnectionError("database unavailable")),
    ("download_version", TimeoutError("storage read timed out")),
])
async def test_transient_saved_result_read_keeps_same_reference_and_action_for_retry(monkeypatch, method, failure):
    artifacts, operations, _, scope, service, arguments = _setup()
    admission = await service.begin(scope, **arguments)
    loaded, _ = await _retain(service, scope, admission)
    reference = service.reference(scope, loaded.artifact)
    original = getattr(artifacts, method)
    async def unavailable(*_args):
        raise failure
    monkeypatch.setattr(artifacts, method, unavailable)
    with pytest.raises(GeometryInspectionApplicationError) as error:
        await service.read(scope, reference)
    assert error.value.code == "INSPECTION_READ_UNAVAILABLE"
    assert error.value.phase == "target" and error.value.retryable
    assert error.value.__cause__ is failure
    with pytest.raises(GeometryInspectionApplicationError) as replay_error:
        await service.begin(scope, **arguments)
    assert replay_error.value.code == "INSPECTION_READ_UNAVAILABLE"
    monkeypatch.setattr(artifacts, method, original)
    assert (await service.read(scope, reference)).artifact == loaded.artifact
    replay = await service.begin(scope, **arguments)
    assert replay.replay.artifact == loaded.artifact and replay.claim is None
    assert len(artifacts.records) == 2 and len(operations.published) == 1
    assert operations.states[admission.operation.operation_id].operation.status is CriticalOperationStatus.SUCCEEDED


async def test_confirmed_missing_saved_result_is_not_retryable():
    artifacts, _, _, scope, service, arguments = _setup()
    loaded, _ = await _retain(service, scope, await service.begin(scope, **arguments))
    reference = service.reference(scope, loaded.artifact)
    artifacts.records = {key: value for key, value in artifacts.records.items()
                         if value.artifact_id != loaded.artifact.artifact_id}
    with pytest.raises(GeometryInspectionApplicationError) as error:
        await service.read(scope, reference)
    assert error.value.code == "INSPECTION_NOT_FOUND" and not error.value.retryable


async def test_damaged_bundle_format_is_not_a_transient_read_failure(monkeypatch):
    artifacts, _, _, scope, service, arguments = _setup()
    loaded, _ = await _retain(service, scope, await service.begin(scope, **arguments))
    async def malformed(owner, artifact_id, version_id):
        return ArtifactDownload(await artifacts.get_version_metadata(owner, artifact_id, version_id), b"invalid zip")
    monkeypatch.setattr(artifacts, "download_version", malformed)
    with pytest.raises(GeometryInspectionApplicationError) as error:
        await service.read(scope, service.reference(scope, loaded.artifact))
    assert error.value.code == "INSPECTION_INTEGRITY_FAILED" and not error.value.retryable


async def test_failed_operation_get_keeps_original_request_without_a_result_ref():
    artifacts, operations, codec, scope, service, arguments = _setup()
    admission = await service.begin(scope, **arguments)
    operations.lose_commit_response = True
    published = await service.fail(scope, admission, code="INSPECTION_BUILD_FAILED", message="  已确认执行失败 " * 1000)
    terminal = operations.states[admission.operation.operation_id].operation
    assert terminal.status is CriticalOperationStatus.FAILED and terminal.finished_at is not None
    assert terminal.error_message.startswith("已确认执行失败")
    assert len(terminal.error_message.encode("utf-8")) <= 2000
    details = await service.operation_result(scope, published, include_request=True)
    assert details.inspection_ref is None and details.source_kind == "geometry_sample"
    assert details.request_json == '{"parameters":{"length_m":4}}'
    assert len(artifacts.records) == 1


async def test_known_publication_budget_failure_is_terminal_without_partial_asset(monkeypatch):
    artifacts, operations, _, scope, service, arguments = _setup()
    admission = await service.begin(scope, **arguments)
    monkeypatch.setattr("CAD2BIM_backend.application.geometry_inspection_service.GEOMETRY_INSPECTION_MANIFEST_MAX_BYTES", 1)
    with pytest.raises(GeometryInspectionApplicationError) as failure:
        await _retain(service, scope, admission)
    assert failure.value.code == "INSPECTION_RESULT_BUDGET_EXCEEDED" and failure.value.operation_alias
    assert operations.states[admission.operation.operation_id].operation.status is CriticalOperationStatus.FAILED
    assert len(artifacts.records) == 1


@pytest.mark.parametrize("resource_timeout", [False, True])
async def test_saved_result_and_operation_survive_model_render_failure(tmp_path, resource_timeout):
    from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
    artifacts, operations, codec, scope, service, arguments = _setup()
    admission = await service.begin(scope, **arguments)
    loaded, alias = await _retain(service, scope, admission)
    renderer = _CapacityRenderer(tmp_path, fail_render_at=1)
    if resource_timeout:
        from CAD2BIM_backend.domain.geometry_runtime import (
            GeometryRuntimeResourceDiagnostic, GeometryRuntimeResourceLimitError,
        )
        from unittest.mock import AsyncMock
        renderer.render = AsyncMock(side_effect=GeometryRuntimeResourceLimitError(
            "private codec detail", diagnostic=GeometryRuntimeResourceDiagnostic(
                operation="render", resource_kind="wall", elapsed_seconds=42.1,
                effective_wall_seconds=42.0, configured_limits={"wall_seconds": 42.0},
            )))
    previews = GeometryPreviewService(None, None, renderer, inspection_service=service)
    result = await previews.preview_inspection(scope, service.reference(scope, loaded.artifact),
        InspectionWholeSelection(), InspectionResultCoordinates(), GeometryInspectionModelObservation())
    assert result.inspection_ref == service.reference(scope, loaded.artifact)
    assert result.operation_alias == alias and result.preview is None
    assert result.diagnostics[0].code == "INSPECTION_RENDER_FAILED"
    assert result.diagnostics[0].phase == "render"
    if resource_timeout:
        assert "42.100 observed, 42.000 limit" in result.diagnostics[0].message
        assert "Stage: render" in result.diagnostics[0].message
        assert "private" not in result.diagnostics[0].message
        assert "Query" not in result.diagnostics[0].action
        assert "does not establish" in result.diagnostics[0].action
    else:
        assert result.diagnostics[0].message == "renderer unavailable"
        assert len(renderer.leases) == 1
    assert not renderer.capacity.locked()
    assert operations.states[admission.operation.operation_id].operation.status is CriticalOperationStatus.SUCCEEDED
    assert (await service.read(scope, result.inspection_ref)).geometry.payload == loaded.geometry.payload


async def test_geometry_batch_renders_two_retained_cases_in_one_execution_scope(tmp_path):
    from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
    _, _, _, scope, inspections, _ = _setup()
    renderer = _CapacityRenderer(tmp_path)
    preview = GeometryPreviewService(None, None, renderer, inspection_service=inspections)
    geometry = CanonicalGeometry(format=CanonicalGeometryFormat.OCCT_BREP_V1,
        content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
        payload=b"two-cases", sha256=hashlib.sha256(b"two-cases").hexdigest())
    source = PreviewSourceSnapshot(source_kind="geometry_sample", source_type="published",
        source_id="geometry", source_ref="cur.exact", snapshot_id="geometry-v1",
        content_digest="a" * 64, title="Sample")
    cases = tuple(GeometryPreviewCase(case_key=key, expectation=GeometryCaseExpectation())
        for key in ("first", "second"))
    outcome = await preview.preview_cases(scope, source=source, build_request=geometry,
        cases=cases, retain_for_inspection=True, request_identity="two-geometry-cases",
        render=PreviewSelectedRender(case_keys=("first", "second")))
    assert [item.render_status for item in outcome.report.cases] == ["rendered", "rendered"]
    assert [key for key, _ in outcome.images] == ["first", "second"]
    assert len(renderer.leases) == 2 and renderer.leases[0] is renderer.leases[1]
    assert not renderer.capacity.locked()


@pytest.mark.parametrize("saved_parameters,has_entity,later_nondefault", [({"length_m": 4}, True, False),
    ({"length_m": 8}, True, False), ({"length_m": 4}, False, False), ({"length_m": 4}, True, True)])
async def test_published_display_selects_only_exact_snapshot_and_saved_default_case(
        saved_parameters, has_entity, later_nondefault):
    _, operations, _, scope, inspections, _ = _setup()
    bundle = SimpleNamespace(resource=SimpleNamespace(resource_id="geometry", display_name="Geometry"),
        version=SimpleNamespace(resource_version_id="published-version"))
    snapshot = SimpleNamespace(version_id="published-draft-snapshot")
    operations.get_authoring_publication_for_version = AsyncMock(return_value=SimpleNamespace(
        operation=SimpleNamespace(operation_id="publication")))
    operations.get_operation = AsyncMock(return_value=SimpleNamespace(
        operation=SimpleNamespace(detail=SimpleNamespace(expected_drafts=(SimpleNamespace(
            root_kind="geometry", root_id="geometry", snapshot=snapshot),)))))
    report_artifact = SimpleNamespace(artifact_id="report", version_id="report-version", sha256="a" * 64)
    preview = SimpleNamespace(operation=SimpleNamespace(result_inspection_artifact=report_artifact))
    operations.get_geometry_case_reports_for_snapshot = AsyncMock(return_value=(preview,))
    entity = SimpleNamespace(artifact_id="entity", version_id="entity-version", sha256="b" * 64)
    case = SimpleNamespace(case=SimpleNamespace(case_kind="geometry", case_key="actual_case",
        parameters=saved_parameters), inspection_artifact=entity if has_entity else None)
    report = SimpleNamespace(cases=(case,))
    inspections._read_case_report_artifact = AsyncMock(return_value=report)
    inspections.load_case_geometry = AsyncMock(return_value=SimpleNamespace(artifact=entity))
    if later_nondefault:
        # A full later page of other samples cannot conceal the earlier saved
        # default entity of this same immutable draft snapshot.
        now = datetime.now(UTC)
        later = tuple(SimpleNamespace(operation=SimpleNamespace(result_inspection_artifact=report_artifact,
            started_at=now - timedelta(seconds=index), operation_id=f"later-{index}")) for index in range(20))
        other = SimpleNamespace(cases=(SimpleNamespace(case=SimpleNamespace(case_kind="geometry",
            case_key="other_sample", parameters={"length_m": 8}), inspection_artifact=entity),))
        operations.get_geometry_case_reports_for_snapshot.side_effect = (later, (preview,))
        inspections._read_case_report_artifact.side_effect = [other] * 20 + [report]
    source = await inspections.saved_version_display_source(scope, bundle, {"length_m": 4})
    operations.get_authoring_publication_for_version.assert_awaited_once_with(
        scope, root_kind="geometry", version_id="published-version")
    lookup = operations.get_geometry_case_reports_for_snapshot
    if later_nondefault:
        assert lookup.await_count == 2
        lookup.assert_awaited_with(scope, resource_id="geometry", snapshot_version_id="published-draft-snapshot",
            before=(later[-1].operation.started_at, later[-1].operation.operation_id), page_size=20)
    else:
        lookup.assert_awaited_once_with(scope, resource_id="geometry",
            snapshot_version_id="published-draft-snapshot", before=None, page_size=20)
    if saved_parameters == {"length_m": 4} and has_entity:
        assert source["source_kind"] == "geometry_inspection"
        assert source["source_ref"] == inspections.reference(scope, entity)
        assert source["case_key"] == "actual_case" and source["parameters"] == {"length_m": 4}
        inspections.load_case_geometry.assert_awaited_once_with(scope, report, "actual_case")
    else:
        assert source is None
        inspections.load_case_geometry.assert_not_awaited()


async def test_saved_connection_context_inherits_exact_bytes_and_temporary_override_is_read_only(tmp_path):
    from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
    from CAD2BIM_backend.application.geometry_query_service import GeometryQueryService
    from CAD2BIM_backend.domain.geometry_inspection import GeometrySectionObservation
    from CAD2BIM_backend.domain.geometry_runtime import GeometryPreviewProfile
    from unittest.mock import AsyncMock
    artifacts, operations, codec, scope, inspections, arguments = _setup()
    renderer = _ConnectionRenderer(tmp_path)
    previews = GeometryPreviewService(None, None, renderer, inspection_service=inspections)
    payload = b"product-only"
    geometry = CanonicalGeometry(format=CanonicalGeometryFormat.OCCT_BREP_V1,
        content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type, payload=payload,
        sha256=hashlib.sha256(payload).hexdigest())
    source = PreviewSourceSnapshot(source_kind="geometry_sample", source_type="published",
        source_id="geometry", source_ref="cur.exact", snapshot_id="geometry-v1", content_digest="a"*64, title="Sample")
    pipes = {"mode": "pipes", "items": [
        {"start_m": [-1, 0, 0], "end_m": [1, 0, 0], "outer_diameter_m": 0.05},
        {"start_m": [0, -1, 0], "end_m": [0, 1, 0], "outer_diameter_m": 0.05}]}
    outcome = await previews.preview_cases(scope, source=source, build_request=geometry,
        cases=(GeometryPreviewCase(case_key="first", expectation=GeometryCaseExpectation()),),
        retain_for_inspection=True, request_identity="context-case",
        render=PreviewSelectedRender(case_keys=("first",), profile=GeometryPreviewProfile(connection_context=pipes)))
    reference = outcome.report.cases[0].inspection_ref
    loaded = await inspections.read(scope, reference)
    original_bytes = artifacts.content[loaded.artifact.artifact_id]
    counts = (len(artifacts.records), len(operations.states))
    assert loaded.geometry.payload == payload
    assert [item.fragment_key for item in loaded.fragment_inputs] == ["sample"]
    assert loaded.manifest.scene.connection_context.model_dump(mode="json") == pipes
    assert loaded.display_context.payload == renderer.requests[0].display_context.payload
    assert len(renderer.built_contexts) == 1
    renderer.fail_context = True
    replay = await previews.preview_cases(scope, source=source, build_request=geometry,
        cases=(GeometryPreviewCase(case_key="first", expectation=GeometryCaseExpectation()),),
        retain_for_inspection=True, request_identity="context-case",
        render=PreviewSelectedRender(case_keys=("first",), profile=GeometryPreviewProfile(connection_context=pipes)))
    assert replay.images[0][1].connection_context == loaded.manifest.scene.connection_context
    assert len(renderer.built_contexts) == 1
    inherited = await previews.preview_inspection(scope, reference, InspectionWholeSelection(), InspectionResultCoordinates(),
        GeometryInspectionModelObservation(render_profile={"view": "front"}))
    assert inherited.preview.connection_context.items == loaded.manifest.scene.connection_context.items
    assert renderer.requests[-1].display_context.payload == loaded.display_context.payload
    assert len(renderer.built_contexts) == 1
    hidden = await previews.preview_inspection(scope, reference, InspectionWholeSelection(), InspectionResultCoordinates(),
        GeometryInspectionModelObservation(render_profile={"connection_context": {"mode": "none"}}))
    assert hidden.preview.connection_context is None and renderer.requests[-1].display_context is None
    assert renderer.requests[-1].scene.straight_profile_overlays == ()
    renderer.fail_context = False
    replacement = {"mode": "pipes", "items": [{"start_m": [0, 0, 0], "end_m": [0, 0, 1], "outer_diameter_m": 0.1}]}
    changed = await previews.preview_inspection(scope, reference, InspectionWholeSelection(), InspectionResultCoordinates(),
        GeometryInspectionModelObservation(render_profile={"connection_context": replacement}))
    assert changed.preview.connection_context.items[0].outer_diameter_m == 0.1
    assert changed.preview.connection_context.model_dump(mode="json") == replacement
    assert len(renderer.built_contexts) == 2
    fragment = await previews.preview_inspection(scope, reference, InspectionFragmentSelection(fragment_key="sample"),
        InspectionFragmentLocalCoordinates(fragment_key="sample"), GeometryInspectionModelObservation())
    assert fragment.preview.connection_context is None and renderer.requests[-1].display_context is None
    assert renderer.requests[-1].geometry.payload == payload
    renderer.section = AsyncMock(return_value=None)
    await previews.preview_inspection(scope, reference, InspectionWholeSelection(), InspectionResultCoordinates(),
        GeometrySectionObservation(plane={"origin_m": [0, 0, 0], "normal": [0, 0, 1]}))
    section_request = renderer.section.await_args.args[0]
    assert section_request.geometry.payload == payload
    assert [item.fragment_key for item in section_request.fragment_inputs] == ["sample"]
    assert "display_context" not in section_request.model_dump()
    renderer.query = AsyncMock(return_value=None)
    queries = GeometryQueryService(None, artifacts, None, renderer, codec, inspections=inspections)
    await queries._execute_inspection(scope, SimpleNamespace(target_artifact=loaded.artifact,
        selection=InspectionWholeSelection(), coordinate_space=InspectionResultCoordinates()), "result = ctx.summary()")
    query_request = renderer.query.await_args.args[0]
    assert query_request.geometry.payload == payload
    assert [item.fragment_key for item in query_request.fragment_inputs] == ["sample"]
    assert "display_context" not in query_request.model_dump()
    assert artifacts.content[loaded.artifact.artifact_id] == original_bytes
    assert counts == (len(artifacts.records), len(operations.states))
    assert (await GeometryInspectionService(artifacts, operations, codec).read(scope, reference)).display_context == loaded.display_context


@pytest.mark.parametrize("kind,outward,start,expected_end,diameter,satisfied,diagnostic", [
    ("finite_segment", 1, 0.25, 0.4, 0.05, True, None),
    ("finite_segment", -1, 0.25, 0.1, 0.05, True, None),
    ("finite_segment", 1, 1.0, 1.15, 0.05, True, None),
    ("fixed_point", 1, 0.25, 0.4, 0.05, True, None),
    ("infinite_axis", -1, 0.25, 0.1, 0.05, True, None),
    ("fixed_point", 1, 0.25, None, None, True, "no outer diameter"),
    ("finite_segment", 1, 0.25, None, 0.05, False, "satisfied condition"),
])
async def test_auto_connection_context_uses_checked_actual_port_and_outward_segment(
        tmp_path, kind, outward, start, expected_end, diameter, satisfied, diagnostic):
    from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
    from CAD2BIM_backend.domain.geometry_frame import rigid_transform_from_axis_reference
    from CAD2BIM_backend.domain.geometry_runtime import GeometryPreviewProfile, GeometryRenderInterfaceOverlay
    from CAD2BIM_backend.domain.product_realization import ProductRealizationExternalConstraint
    runtime = _ConnectionRenderer(tmp_path)
    service = GeometryPreviewService(None, None, runtime)
    condition = ({"kind": kind, "start_m": [0, 0, 0], "end_m": [1, 0, 0],
                  "direction_relation": "same" if outward > 0 else "opposed"} if kind == "finite_segment" else
                 {"kind": kind, "point_m": [start, 0, 0], "axis": [1, 0, 0],
                  "direction_relation": "same" if outward > 0 else "opposed"})
    if diameter is not None:
        condition["outer_diameter_m"] = diameter
    external = ProductRealizationExternalConstraint(interface_key="outlet", condition=condition)
    scene = GeometryRenderScene(coordinate_space="project_world", geometry_sha256="a"*64,
        interface_overlays=(GeometryRenderInterfaceOverlay(overlay_id="family:outlet", label="outlet",
            marker_kind="family_external", frame=rigid_transform_from_axis_reference(
                (start, 0, 0), (outward, 0, 0), (0, 1, 0))),))
    result, background = await service._prepare_connection_context(GeometryPreviewProfile(connection_context={"mode":"auto"}),
        scene, external_constraints=(external,), external_checks=(SimpleNamespace(interface_key="outlet",
            evaluation=SimpleNamespace(satisfied=satisfied)),))
    assert result.geometry_sha256 == scene.geometry_sha256 and result.interface_overlays == scene.interface_overlays
    if diagnostic is not None:
        assert background is None and result.connection_context is None
        assert diagnostic in result.connection_context_diagnostics[0]
        assert not runtime.built_contexts
    else:
        assert result.connection_context.items[0].start_m == (start, 0, 0)
        assert result.connection_context.items[0].end_m == pytest.approx((expected_end, 0, 0))
        assert result.connection_context.items[0].outer_diameter_m == diameter
        assert background is not None and len(runtime.built_contexts) == 1


async def test_background_failure_retains_product_facts_and_reports_render_failure(tmp_path):
    from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
    from CAD2BIM_backend.domain.geometry_runtime import GeometryPreviewProfile
    _, _, _, scope, inspections, _ = _setup()
    runtime = _ConnectionRenderer(tmp_path, fail_context=True)
    previews = GeometryPreviewService(None, None, runtime, inspection_service=inspections)
    geometry = CanonicalGeometry(format=CanonicalGeometryFormat.OCCT_BREP_V1,
        content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type, payload=b"product",
        sha256=hashlib.sha256(b"product").hexdigest())
    source = PreviewSourceSnapshot(source_kind="geometry_sample", source_type="published",
        source_id="geometry", source_ref="cur.exact", snapshot_id="geometry-v1", content_digest="a"*64, title="Sample")
    profile = GeometryPreviewProfile(connection_context={"mode": "pipes", "items": [
        {"start_m": [0, 0, 0], "end_m": [1, 0, 0], "outer_diameter_m": 0.05}]})
    outcome = await previews.preview_cases(scope, source=source, build_request=geometry,
        cases=(GeometryPreviewCase(case_key="first", expectation=GeometryCaseExpectation()),),
        retain_for_inspection=True, request_identity="failed-background",
        render=PreviewSelectedRender(case_keys=("first",), profile=profile))
    case = outcome.report.cases[0]
    assert case.geometry_status == "BUILT" and case.render_status == "failed"
    assert case.diagnostics[0].phase == "render" and "background construction failed" in case.diagnostics[0].message
    loaded = await inspections.read(scope, case.inspection_ref)
    assert loaded.geometry.payload == geometry.payload and loaded.display_context is None
    assert not runtime.requests and outcome.images == ()
    assert runtime.context_attempts == 1


async def test_auto_context_requires_family_facts_and_uses_actual_axis_when_direction_is_free(tmp_path):
    from CAD2BIM_backend.application.geometry_preview_service import GeometryPreviewService
    from CAD2BIM_backend.domain.geometry_runtime import GeometryPreviewProfile, GeometryRenderInterfaceOverlay
    from CAD2BIM_backend.domain.product_realization import ProductRealizationExternalConstraint
    runtime = _ConnectionRenderer(tmp_path)
    service = GeometryPreviewService(None, None, runtime)
    profile = GeometryPreviewProfile(connection_context={"mode":"auto"})
    scene = GeometryRenderScene(coordinate_space="project_world", geometry_sha256="a"*64,
        interface_overlays=(GeometryRenderInterfaceOverlay(overlay_id="family:outlet", label="outlet",
            marker_kind="family_external", frame=RigidTransform3D(translation_m=(0.5, 0, 0))),))
    with pytest.raises(ValueError, match="requires.*Family"):
        await service._prepare_connection_context(profile, scene)
    external = ProductRealizationExternalConstraint(interface_key="outlet", condition={"kind":"finite_segment",
        "start_m":[0,0,0], "end_m":[1,0,0], "outer_diameter_m":0.05, "direction_relation":"none"})
    result, background = await service._prepare_connection_context(profile, scene, external_constraints=(external,),
        external_checks=(SimpleNamespace(interface_key="outlet", evaluation=SimpleNamespace(satisfied=True)),))
    assert background is not None and result.connection_context_diagnostics == ()
    pipe = result.connection_context.items[0]
    assert pipe.start_m == (0.5, 0, 0)
    assert pipe.end_m == pytest.approx((0.5, 0, 0.15))
    assert len(runtime.built_contexts) == 1


async def test_inspection_operation_get_follows_exact_origin_across_run_without_alias_or_write():
    from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService
    from CAD2BIM_mcp.direct.operation.application import OperationApplicationMixin
    from CAD2BIM_mcp.direct.transform.common_inputs import OperationInspectionSelection
    from unittest.mock import AsyncMock
    artifacts, operations, codec, scope, service, arguments = _setup()
    admission = await service.begin(scope, **arguments)
    loaded, _ = await _retain(service, scope, admission)
    original = operations.published[admission.operation.operation_id]
    reader_scope = scope.model_copy(update={"run_id": "next-run"})
    reader = AsyncMock(return_value=SimpleNamespace(operation=original, result_package=None))
    class Application(OperationApplicationMixin):
        reference_service = OpaqueReferenceService(None, codec)
        _application = SimpleNamespace(get_operation_by_locator=reader)
        async def _run_runtime_read(self, _session, _name, action):
            return await action(reader_scope)
        def _require_geometry_inspection_service(self):
            return service
    count = len(artifacts.records), len(operations.published)
    result = await Application().get_operation("direct_001", selection=OperationInspectionSelection(
        direct_session_alias="direct_001", inspection_ref=service.reference(scope, loaded.artifact)))
    reader.assert_awaited_once_with(reader_scope, origin_run_id=scope.run_id, operation_id=admission.operation.operation_id)
    assert result.operation.operation_alias is None
    assert Application.reference_service.operation_locator(reader_scope.identity, result.operation.operation_ref) == (
        scope.run_id, admission.operation.operation_id)
    assert result.detail.inspection_ref == service.reference(scope, loaded.artifact)
    assert count == (len(artifacts.records), len(operations.published))
