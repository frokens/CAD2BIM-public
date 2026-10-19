from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from contextlib import nullcontext

import pytest
from unittest.mock import AsyncMock

from CAD2BIM_backend.application.artifact_service import ArtifactDownload
from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionService
from CAD2BIM_backend.application.geometry_query_service import (
    GeometryQueryApplicationError,
    GeometryQueryService,
)
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.control_plane import ArtifactRecord, ExecutionMode
from CAD2BIM_backend.domain.geometry_query import (
    GeometryQueryArtifactRef,
    GeometryQueryAttemptPreparation,
    GeometryQueryAttemptRecord,
    GeometryQueryAttemptStatus,
    GeometryQueryAttemptTerminal,
    GeometryQueryDiagnostic,
    GeometryQueryTargetKind,
)
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.geometry_inspection import (
    InspectionFragmentLocalCoordinates,
    InspectionFragmentSelection,
    InspectionComponentSelection,
    InspectionResultCoordinates,
    InspectionWholeSelection,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometryBounds,
    GeometryInspectionFragmentInput,
    GeometryNormalizeRequest,
    GeometryQueryResult,
    GeometryRuntimeValidationError,
    GeometryRuntimeResourceLimitError,
    GeometryRuntimeResourceDiagnostic,
    GeometrySummary,
    GeometryTopologySummary,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.text_source import TextSourceView
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.source_text.source_patch import SourceEdit


pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
GEOMETRY_BYTES = b"canonical-brep"
GEOMETRY_SHA = hashlib.sha256(GEOMETRY_BYTES).hexdigest()


def _scope(*, task_id: str = "task-query", command_id: str = "command-query") -> InvocationScope:
    return InvocationScope(
        identity=TenantExecutionContext(
            tenant_id="tenant-query",
            principal_id="principal-query",
            task_id=task_id,
            actor_kind=TenantActorKind.USER,
            command_id=command_id,
        ),
        owner_id="tenant-query",
        run_id="run-query",
        execution_mode=ExecutionMode.DIRECT_MCP,
        catalog_frontier=None,
    )


def _cursor_codec() -> CursorCodec:
    key = base64.urlsafe_b64encode(b"g" * 32).rstrip(b"=").decode("ascii")
    return CursorCodec(key)


class _Artifacts:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.by_key: dict[str, tuple[ArtifactRecord, bytes]] = {}
        self.by_id: dict[str, tuple[ArtifactRecord, bytes]] = {}

    async def upload_bytes(self, **values) -> ArtifactRecord:
        key = values["idempotency_key"]
        content = values["content"]
        label = "source" if values["kind"] == "geometry_query_source" else "result"
        self.events.append(f"artifact:{label}")
        existing = self.by_key.get(key)
        if existing is not None:
            assert existing[1] == content
            return existing[0]
        suffix = hashlib.sha256(key.encode()).hexdigest()[:24]
        record = ArtifactRecord(
            artifact_id=f"art_{suffix}",
            version_id=f"av_{suffix}",
            owner_id=values["owner_id"],
            task_id=values["task_id"],
            kind=values["kind"],
            filename=values["filename"],
            content_type=values["content_type"],
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            idempotency_key=key,
            created_at=NOW,
            run_id=values["run_id"],
            version_metadata=values["version_metadata"],
        )
        self.by_key[key] = record, content
        self.by_id[record.artifact_id] = record, content
        return record

    async def download_version(
        self,
        owner_id: str,
        artifact_id: str,
        version_id: str,
        *,
        run_id: str | None = None,
    ) -> ArtifactDownload:
        record, content = self.by_id[artifact_id]
        assert record.owner_id == owner_id
        assert record.version_id == version_id
        assert record.run_id == run_id
        self.events.append(f"artifact:read:{record.kind}")
        return ArtifactDownload(artifact=record, content=content)

    def add_preview(self, *, metadata: dict[str, str] | None = None) -> GeometryQueryArtifactRef:
        record = ArtifactRecord(
            artifact_id="art_preview",
            version_id="av_preview",
            owner_id="tenant-query",
            task_id="task-query",
            kind="product_realization_preview_geometry",
            filename="product-realization-preview.brep",
            content_type="application/vnd.opencascade.brep",
            size_bytes=len(GEOMETRY_BYTES),
            sha256=GEOMETRY_SHA,
            idempotency_key="product-realization-preview:test",
            created_at=NOW,
            run_id="run-query",
            version_metadata=metadata
            or {
                "target_kind": "product_realization_preview",
                "geometry_format": "occt_brep_v1",
                "geometry_sha256": GEOMETRY_SHA,
                "family_version_id": "family-version-one",
                "family_version_content_digest": "1" * 64,
            },
        )
        self.by_id[record.artifact_id] = record, GEOMETRY_BYTES
        return GeometryQueryArtifactRef(
            artifact_id=record.artifact_id,
            version_id=record.version_id,
            sha256=record.sha256,
        )


class _Attempts:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.records: dict[str, GeometryQueryAttemptRecord] = {}
        self.fail_after_next_complete = False
        self.list_calls = []

    async def create_geometry_query_attempt(
        self,
        context,
        preparation: GeometryQueryAttemptPreparation,
    ) -> GeometryQueryAttemptRecord:
        self.events.append("attempt:create")
        existing = self.records.get(preparation.attempt_id)
        if existing is not None:
            assert existing.request_digest == preparation.request_digest
            assert existing.source_artifact == preparation.source_artifact
            return existing
        record = GeometryQueryAttemptRecord(
            **preparation.model_dump(),
            tenant_id=context.tenant_id,
            task_id=context.task_id,
            status=GeometryQueryAttemptStatus.PREPARED,
        )
        self.records[record.attempt_id] = record
        return record

    async def complete_geometry_query_attempt(
        self,
        context,
        *,
        attempt_id: str,
        terminal: GeometryQueryAttemptTerminal,
    ) -> GeometryQueryAttemptRecord:
        self.events.append(f"attempt:complete:{terminal.status.value}")
        current = self.records[attempt_id]
        if current.status is not GeometryQueryAttemptStatus.PREPARED:
            return current
        completed = current.model_copy(
            update={
                "status": terminal.status,
                "result_artifact": terminal.result_artifact,
                "diagnostics": terminal.diagnostics,
                "finished_at": terminal.finished_at,
            }
        )
        completed = GeometryQueryAttemptRecord.model_validate(completed)
        self.records[attempt_id] = completed
        if self.fail_after_next_complete:
            self.fail_after_next_complete = False
            raise RuntimeError("simulated response loss")
        return completed

    async def get_geometry_query_attempt(self, context, *, attempt_id: str):
        record = self.records.get(attempt_id)
        if record is None or record.task_id != context.task_id:
            return None
        return record

    async def list_terminal_geometry_query_attempts(
        self,
        context,
        *,
        statuses,
        before_created_at,
        before_attempt_id,
        limit,
    ):
        self.list_calls.append((context, statuses, before_created_at, before_attempt_id, limit))
        records = [
            record
            for record in self.records.values()
            if record.task_id == context.task_id and record.status in statuses
        ]
        if before_created_at is not None:
            records = [
                record
                for record in records
                if (record.created_at, record.attempt_id) < (before_created_at, before_attempt_id)
            ]
        records.sort(key=lambda record: (record.created_at, record.attempt_id), reverse=True)
        return tuple(records[:limit])


class _Resources:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.bundle = SimpleNamespace(
            resource=SimpleNamespace(
                resource_id="resource-1",
                resource_kind=GeometryResourceKind.MAPPED_MODEL,
            ),
            version=SimpleNamespace(resource_version_id="version-1"),
        )

    async def get_exact_version(self, context, *, resource_id: str, resource_version_id: str):
        self.events.append("resource:resolve-exact")
        if resource_id == "resource-1" and resource_version_id == "version-1":
            return self.bundle
        return None

    async def get_version_by_id(self, context, *, resource_version_id: str):
        self.events.append("resource:resolve-version")
        return self.bundle if resource_version_id == "version-1" else None

    async def read_exact_asset(self, context, **values):
        self.events.append(f"resource:read:{values['resource_version_id']}")
        return (
            SimpleNamespace(asset_format="occt_brep_v1", sha256=GEOMETRY_SHA),
            GEOMETRY_BYTES,
        )


class _Runtime:
    def invocation_scope(self, policy, *, deadline=None):
        from contextlib import nullcontext
        return nullcontext()

    def execution_scope(self, *args, **kwargs):
        return nullcontext()

    def __init__(self, events: list[str], *, result=None) -> None:
        self.events = events
        self.result = [1, 2, 3] if result is None else result
        self.query_calls = 0
        self.requests = []
        self.failure: Exception | None = None

    async def normalize(self, request):
        self.events.append("runtime:normalize")
        return CanonicalGeometry(
            format=request.format,
            content_type=request.format.content_type,
            payload=request.payload,
            sha256=GEOMETRY_SHA,
            summary=GeometrySummary(
                format=request.format,
                bounds=GeometryBounds(
                    minimum_m=(0.0, 0.0, 0.0),
                    maximum_m=(1.0, 1.0, 1.0),
                ),
                topology=GeometryTopologySummary(
                    vertex_count=8,
                    edge_count=12,
                    wire_count=6,
                    face_count=6,
                    shell_count=1,
                    solid_count=1,
                    triangle_count=0,
                    boundary_edge_count=0,
                ),
                surface_area_m2=6.0,
                volume_m3=1.0,
            ),
        )

    async def query(self, request):
        self.events.append("runtime:query")
        self.query_calls += 1
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return GeometryQueryResult(
            result=self.result,
            result_bytes=len(canonical_json(self.result).encode("utf-8")),
            geometry_sha256=request.geometry.sha256,
        )


def _service(*, result=None):
    events: list[str] = []
    attempts = _Attempts(events)
    artifacts = _Artifacts(events)
    resources = _Resources(events)
    runtime = _Runtime(events, result=result)
    service = GeometryQueryService(
        attempts,
        artifacts,
        resources,
        runtime,
        _cursor_codec(),
        clock=lambda: NOW,
    )
    return service, attempts, artifacts, runtime, events


def _source_with_exact_utf8_bytes(size: int) -> str:
    prefix = "def query(ctx):\n    return []\n#"
    remaining = size - len(prefix.encode("utf-8"))
    assert remaining >= 0
    return prefix + ("你" * (remaining // 3)) + ("a" * (remaining % 3))


async def test_formal_family_query_preserves_target_for_repair_and_pages_without_rebuilding():
    service, attempts, artifacts, runtime, events = _service()
    family = SimpleNamespace(version=SimpleNamespace(family_version_id="family-version-1",
        family_contract=SimpleNamespace(standard_model={})))
    catalog = SimpleNamespace(resolve_exact_version=AsyncMock(return_value=family),
        resolve_visible_version=AsyncMock(return_value=family))
    assets = SimpleNamespace(standard_model_assets=AsyncMock(return_value=(
        SimpleNamespace(geometry_sha256=GEOMETRY_SHA), GEOMETRY_BYTES)))
    service._family_catalog, service._family_assets = catalog, assets
    page = await service.fresh_family_standard_model(_scope(), family_version_alias="family-ref",
        source_code="def query(ctx):\n    return [False]\n", request_identity="family-query", page_size=1)
    parent = next(iter(attempts.records.values()))
    assert parent.family_version_id == page.family_version_id == "family-version-1"
    assert parent.resource_version_id is None and parent.target_artifact is None
    await service.continue_page(_scope(), page.next_cursor)
    assert runtime.query_calls == 1 and assets.standard_model_assets.await_count == 1
    child = await service.repair(_scope(), parent_attempt_id=parent.attempt_id,
        edits=[SourceEdit(old_text="False",new_text="True")], request_identity="family-repair")
    assert child.family_version_id == parent.family_version_id and runtime.query_calls == 2
    # Removing this version's declared standard capability rejects a new query before preparation.
    family.version.family_contract.standard_model = None
    with pytest.raises(GeometryQueryApplicationError, match="does not define"):
        await service.fresh_family_standard_model(_scope(),family_version_alias="family-ref",
            source_code="def query(ctx):\n    return []\n",request_identity="family-missing")
    assert len(attempts.records) == 2


class _Inspections:
    validate_selection = GeometryInspectionService.validate_selection

    def __init__(self, geometry, events):
        self.events = events
        self.artifact = GeometryQueryArtifactRef(
            artifact_id="inspection-artifact", version_id="inspection-version", sha256="f" * 64,
        )
        self.loaded = SimpleNamespace(
            artifact=self.artifact, geometry=geometry,
            manifest=SimpleNamespace(geometries=(SimpleNamespace(
                role="fragment", object_key="piece_a", component_key="component_a",
            ),)),
            fragment_inputs=(GeometryInspectionFragmentInput(
                fragment_key="piece_a", component_key="component_a", binding_key="binding_a",
                geometry=geometry, result_from_local=RigidTransform3D(translation_m=(3.0, 4.0, 5.0)),
            ),),
            context={"source": {"source_kind": "family_realization"}, "interfaces": []},
        )
        self.failure = None

    def reference(self, scope, artifact):
        assert scope.identity.task_id == "task-query" and artifact == self.artifact
        return "inspection-exact-reference"

    async def read(self, scope, reference):
        assert reference == self.reference(scope, self.artifact)
        return await self.read_artifact(scope, self.artifact)

    async def read_artifact(self, scope, artifact):
        assert artifact == self.artifact and scope.identity.task_id == "task-query"
        self.events.append("inspection:read-exact")
        if self.failure is not None:
            raise self.failure
        return self.loaded


async def test_inspection_fresh_restart_repair_and_pages_preserve_exact_bundle_choices() -> None:
    service, attempts, artifacts, runtime, events = _service(result=[1, 2, 3])
    geometry = await runtime.normalize(GeometryNormalizeRequest(
        format=CanonicalGeometryFormat.OCCT_BREP_V1, payload=GEOMETRY_BYTES,
    ))
    inspections = _Inspections(geometry, events)
    service._inspections = inspections
    events.clear()
    selection = InspectionFragmentSelection(fragment_key="piece_a")
    coordinates = InspectionFragmentLocalCoordinates(fragment_key="piece_a")
    source = "def query(ctx):\n    return [1, 2, 3]\n"
    page = await service.fresh_inspection(
        _scope(), inspection_ref="inspection-exact-reference", selection=selection,
        coordinate_space=coordinates, source_code=source,
        request_identity="inspection-query", page_size=1,
    )
    parent = next(iter(attempts.records.values()))
    assert parent.target_artifact == inspections.artifact
    assert parent.target_geometry_format is None and parent.resource_version_id is None
    assert parent.selection == selection and parent.coordinate_space == coordinates
    request = runtime.requests[0]
    assert request.geometry is geometry and request.geometry.payload == GEOMETRY_BYTES
    assert request.fragment_inputs == inspections.loaded.fragment_inputs
    assert request.inspection_context == inspections.loaded.context
    assert request.selection == selection and request.coordinates == coordinates
    assert "runtime:normalize" not in events
    assert events.index("artifact:source") < events.index("attempt:create") < events.index("runtime:query")

    restarted = GeometryQueryService(
        attempts, artifacts, service._resources, runtime, _cursor_codec(),
        clock=lambda: NOW, inspections=inspections,
    )
    next_page = await restarted.continue_page(_scope(), page.next_cursor)
    replay = await restarted.fresh_inspection(
        _scope(), inspection_ref=page.inspection_ref, selection=selection,
        coordinate_space=coordinates, source_code=source,
        request_identity="inspection-query", page_size=1,
    )
    assert replay == page and runtime.query_calls == 1
    child_page = await restarted.repair(
        _scope(), parent.attempt_id,
        [SourceEdit(old_text="return [1, 2, 3]", new_text="return [4, 5]")],
        "inspection-repair", page_size=2,
    )
    child = next(item for item in attempts.records.values() if item.repair_parent_attempt_id is not None)
    assert child.target_artifact == parent.target_artifact
    assert child.selection == parent.selection and child.coordinate_space == parent.coordinate_space
    for result_page in (page, next_page, child_page):
        assert result_page.inspection_ref == "inspection-exact-reference"
        assert result_page.selection == selection and result_page.coordinate_space == coordinates
    assert runtime.query_calls == 2 and "runtime:normalize" not in events
    visible = await restarted.list_terminal_geometry_query_attempts(
        _scope(), statuses=(GeometryQueryAttemptStatus.SUCCEEDED,),
        before_created_at=None, before_attempt_id=None, limit=10,
    )
    assert {item.attempt_id for item in visible} == {parent.attempt_id, child.attempt_id}
    viewed = await restarted.read_source(_scope(), child.attempt_id, TextSourceView(view_mode="full"))
    assert viewed.sha256 == child.source_artifact.sha256

    inspections.failure = GeometryQueryApplicationError(
        "INSPECTION_INTEGRITY_FAILED", "The saved bundle is corrupt", phase="preflight",
    )
    with pytest.raises(GeometryQueryApplicationError) as missing:
        await restarted.continue_page(_scope(), page.next_cursor)
    assert missing.value.code == "INSPECTION_INTEGRITY_FAILED"
    assert runtime.query_calls == 2


async def test_actual_inspection_bundle_corruption_failure_replays_its_original_phase() -> None:
    from tests.unit.backend.test_geometry_inspection_service import _retain, _setup

    stored_artifacts, _, _, scope, store, arguments = _setup()
    admission = await store.begin(scope, **arguments)
    loaded, _ = await _retain(store, scope, admission)
    service, attempts, _, runtime, _ = _service()
    service._inspections = store
    source = "def query(ctx):\n    return [1, 2, 3]\n"
    await service.fresh_inspection(
        scope, inspection_ref=store.reference(scope, loaded.artifact),
        selection=InspectionWholeSelection(), coordinate_space=InspectionResultCoordinates(),
        source_code=source, request_identity="inspect-before-corruption",
    )
    parent = next(iter(attempts.records.values()))
    original_bundle = stored_artifacts.content[loaded.artifact.artifact_id]
    stored_artifacts.content[loaded.artifact.artifact_id] = b"corrupted exact saved bundle"
    repair_arguments = dict(
        parent_attempt_id=parent.attempt_id, edits=[SourceEdit(old_text="return [1, 2, 3]", new_text="return [4]")],
        request_identity="inspect-corrupted-repair",
    )
    with pytest.raises(GeometryQueryApplicationError) as first:
        await service.repair(scope, **repair_arguments)
    failed = next(item for item in attempts.records.values() if item.status is GeometryQueryAttemptStatus.FAILED)
    assert first.value.code == "INSPECTION_INTEGRITY_FAILED" and first.value.phase == "target"
    assert failed.diagnostics[0].phase == "target" and failed.diagnostics[0].retryable is False
    assert runtime.query_calls == 1

    # Restoring the original bytes cannot turn this exact failed attempt into a new execution.
    stored_artifacts.content[loaded.artifact.artifact_id] = original_bundle
    with pytest.raises(GeometryQueryApplicationError) as replay:
        await service.repair(scope, **repair_arguments)
    assert replay.value.code == first.value.code and replay.value.phase == first.value.phase
    assert replay.value.retryable == first.value.retryable
    assert replay.value.attempt_alias == first.value.attempt_alias
    assert runtime.query_calls == 1 and len(attempts.records) == 2


@pytest.mark.parametrize("selection", (
    InspectionFragmentSelection(fragment_key="missing_fragment"),
    InspectionComponentSelection(component_key="missing_component"),
))
async def test_exact_inspection_selection_is_checked_before_source_attempt_or_query(selection) -> None:
    from tests.unit.backend.test_geometry_inspection_service import _retain, _setup

    _, _, _, scope, store, arguments = _setup()
    admission = await store.begin(scope, **arguments)
    loaded, _ = await _retain(store, scope, admission)
    service, attempts, artifacts, runtime, events = _service()
    service._inspections = store
    with pytest.raises(GeometryQueryApplicationError) as invalid:
        await service.fresh_inspection(
            scope, inspection_ref=store.reference(scope, loaded.artifact),
            selection=selection, coordinate_space=InspectionResultCoordinates(),
            source_code="def query(ctx):\n    return []\n", request_identity="invalid-inspection-selection",
        )
    assert invalid.value.code == "INSPECTION_SELECTION_INVALID" and invalid.value.phase == "preflight"
    assert attempts.records == {} and artifacts.by_id == {} and runtime.query_calls == 0
    assert events == []


async def test_fresh_persists_source_and_attempt_before_runtime_and_pages_result() -> None:
    service, attempts, _artifacts, runtime, events = _service()

    page = await service.fresh(
        _scope(),
        "resource-1",
        "version-1",
        "def query(ctx):\n    return [1, 2, 3]\n",
        "request-fresh",
        page_size=2,
    )

    assert page.results == (1, 2)
    assert page.total_count == 3
    assert page.next_cursor is not None
    attempt = next(iter(attempts.records.values()))
    assert attempt.status is GeometryQueryAttemptStatus.SUCCEEDED
    assert attempt.result_artifact is not None
    assert runtime.query_calls == 1
    assert events.index("artifact:source") < events.index("attempt:create")
    assert events.index("attempt:create") < events.index("runtime:query")
    assert "attempt:complete:SUCCEEDED" in events
    assert all("operation" not in event for event in events)


async def test_fresh_restart_replays_the_same_exact_attempt_without_execution() -> None:
    service, attempts, artifacts, runtime, events = _service()
    source = "def query(ctx):\n    return [1, 2, 3]\n"
    first = await service.fresh(
        _scope(), "resource-1", "version-1", source, "exact-fresh", page_size=2,
    )
    restarted = GeometryQueryService(
        attempts, artifacts, service._resources, runtime, _cursor_codec(), clock=lambda: NOW,
    )
    replay = await restarted.fresh(
        _scope(command_id="restarted-query"), "resource-1", "version-1", source,
        "exact-fresh", page_size=2,
    )
    assert replay == first and runtime.query_calls == 1 and len(attempts.records) == 1
    assert next(iter(attempts.records.values())).resource_version_id == "version-1"
    assert events.count("runtime:normalize") == 1


async def test_repair_restart_replays_the_same_parent_and_child_exact_source() -> None:
    service, attempts, artifacts, runtime, _events = _service()
    await service.fresh(
        _scope(), "resource-1", "version-1",
        "def query(ctx):\n    return [False]\n", "exact-parent",
    )
    parent = next(iter(attempts.records.values()))
    edits = [SourceEdit(old_text="False", new_text="True")]
    first = await service.repair(
        _scope(), parent_attempt_id=parent.attempt_id,
        edits=edits, request_identity="exact-repair", page_size=100,
    )
    restarted = GeometryQueryService(
        attempts, artifacts, service._resources, runtime, _cursor_codec(), clock=lambda: NOW,
    )
    replay = await restarted.repair(
        _scope(command_id="restarted-repair"), parent_attempt_id=parent.attempt_id,
        edits=edits, request_identity="exact-repair", page_size=100,
    )
    assert replay == first and runtime.query_calls == 2
    child = next(attempt for attempt in attempts.records.values() if attempt.attempt_id != parent.attempt_id)
    assert child.repair_parent_attempt_id == parent.attempt_id and child.resource_version_id == parent.resource_version_id
    child_source = await restarted.read_source(_scope(), child.attempt_id, TextSourceView(view_mode="full"))
    assert child_source.text == "def query(ctx):\n    return [True]\n"


async def test_fresh_resumes_prepared_attempt_using_its_read_saved_exact_source() -> None:
    service, attempts, artifacts, runtime, _events = _service()
    source = "def query(ctx):\n    return [1, 2, 3]\n"
    first = await service.fresh(
        _scope(), "resource-1", "version-1", source, "exact-prepared", page_size=2,
    )
    original = next(iter(attempts.records.values()))
    attempts.records[original.attempt_id] = GeometryQueryAttemptRecord.model_validate(
        {**original.model_dump(), **{
            "status": GeometryQueryAttemptStatus.PREPARED,
            "result_artifact": None,
            "finished_at": None,
        }}
    )
    restarted = GeometryQueryService(
        attempts, artifacts, service._resources, runtime, _cursor_codec(), clock=lambda: NOW,
    )
    saved_source = await restarted.read_source(_scope(), original.attempt_id, TextSourceView(view_mode="full"))
    replay = await restarted.fresh(
        _scope(), "resource-1", original.resource_version_id, saved_source.text,
        original.request_identity, page_size=2,
    )
    assert replay == first
    assert runtime.query_calls == 2 and len(attempts.records) == 1


async def test_repeated_cancellation_settles_attempt_without_reexecuting(monkeypatch) -> None:
    service, attempts, _artifacts, runtime, events = _service()
    running, settling, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_complete = attempts.complete_geometry_query_attempt

    async def query(_request):
        running.set()
        await asyncio.Future()

    async def complete(*args, **kwargs):
        settling.set()
        await finish.wait()
        return await original_complete(*args, **kwargs)

    monkeypatch.setattr(runtime, "query", query)
    monkeypatch.setattr(attempts, "complete_geometry_query_attempt", complete)
    task = asyncio.create_task(service.fresh(
        _scope(), "resource-1", "version-1", "def query(ctx):\n    return []\n", "cancel-query",
    ))
    await asyncio.wait_for(running.wait(), 1)
    task.cancel()
    await asyncio.wait_for(settling.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    attempt = next(iter(attempts.records.values()))
    assert attempt.status is GeometryQueryAttemptStatus.FAILED
    assert attempt.diagnostics[0].code == "GEOMETRY_QUERY_CANCELLED"
    assert events.count("attempt:complete:FAILED") == 1


async def test_source_preflight_accepts_exact_64_kib_utf8_and_rejects_one_byte_more() -> None:
    service, attempts, _artifacts, runtime, events = _service(result=[])
    exact = _source_with_exact_utf8_bytes(65_536)

    page = await service.fresh(
        _scope(),
        "resource-1",
        "version-1",
        exact,
        "request-max-source",
    )

    assert page.results == ()
    assert (
        next(iter(attempts.records.values())).source_artifact.sha256
        == hashlib.sha256(exact.encode("utf-8")).hexdigest()
    )
    assert runtime.query_calls == 1
    event_count = len(events)

    with pytest.raises(GeometryQueryApplicationError) as caught:
        await service.fresh(
            _scope(),
            "resource-1",
            "version-1",
            exact + "a",
            "request-over-max-source",
        )

    assert caught.value.code == "GEOMETRY_QUERY_SOURCE_INVALID"
    assert len(events) == event_count


async def test_fresh_rejects_modeling_function_before_source_or_worker() -> None:
    service, attempts, artifacts, runtime, events = _service(result=[])
    service._resources.bundle.resource.resource_kind = GeometryResourceKind.MODELING_FUNCTION

    with pytest.raises(GeometryQueryApplicationError) as caught:
        await service.fresh(
            _scope(),
            "resource-1",
            "version-1",
            "def query(ctx):\n    return ctx.summary()\n",
            "request-modeling-function",
        )

    assert caught.value.code == "GEOMETRY_QUERY_RESOURCE_KIND_UNSUPPORTED"
    assert caught.value.phase == "preflight"
    assert "exact mapped_model versions" in str(caught.value)
    assert not attempts.records
    assert not artifacts.by_key
    assert runtime.query_calls == 0
    assert events == ["resource:resolve-exact"]


async def test_retired_preview_attempt_cannot_be_repaired_or_read_online() -> None:
    service, attempts, artifacts, runtime, events = _service(result=[1, 2])
    page = await service.fresh(
        _scope(), "resource-1", "version-1",
        "def query(ctx):\n    return [1, 2]\n", "request-historical",
        page_size=1,
    )
    current = next(iter(attempts.records.values()))
    historical_target = artifacts.add_preview()
    with pytest.raises(ValueError, match="exact mapped resource version"):
        GeometryQueryAttemptPreparation.model_validate({
            **current.model_dump(mode="python", exclude={"tenant_id", "task_id", "status", "result_artifact", "diagnostics", "finished_at"}),
            "target_kind": GeometryQueryTargetKind.PRODUCT_REALIZATION_PREVIEW,
            "resource_version_id": None,
            "target_artifact": historical_target,
            "target_geometry_format": CanonicalGeometryFormat.OCCT_BREP_V1,
        })
    historical = GeometryQueryAttemptRecord.model_validate({
        **current.model_dump(mode="python"),
        "target_kind": GeometryQueryTargetKind.PRODUCT_REALIZATION_PREVIEW,
        "resource_version_id": None,
        "target_artifact": historical_target,
        "target_geometry_format": CanonicalGeometryFormat.OCCT_BREP_V1,
    })
    attempts.records[current.attempt_id] = historical
    event_count = len(events)

    with pytest.raises(GeometryQueryApplicationError, match="no longer available") as repair:
        await service.repair(
            _scope(), current.attempt_id,
            [SourceEdit(old_text="return [1, 2]", new_text="return [3]")],
            "request-historical-repair",
        )
    assert repair.value.code == "GEOMETRY_QUERY_TARGET_UNSUPPORTED"
    with pytest.raises(GeometryQueryApplicationError, match="no longer available"):
        await service.read_source(_scope(), current.attempt_id, TextSourceView(view_mode="full"))
    with pytest.raises(GeometryQueryApplicationError, match="no longer available"):
        await service.resolve_attempt_alias(_scope(), page.attempt_alias)
    with pytest.raises(GeometryQueryApplicationError, match="no longer available"):
        await service.continue_page(_scope(), page.next_cursor)
    assert await service.list_terminal_geometry_query_attempts(
        _scope(), statuses=(GeometryQueryAttemptStatus.SUCCEEDED,),
        before_created_at=None, before_attempt_id=None, limit=10,
    ) == ()
    assert runtime.query_calls == 1
    assert len(attempts.records) == 1
    assert events == events[:event_count]



async def test_cursor_reads_exact_result_artifact_without_rerunning_query() -> None:
    service, _attempts, _artifacts, runtime, events = _service()
    first = await service.fresh(
        _scope(),
        "resource-1",
        "version-1",
        "def query(ctx):\n    return [1, 2, 3]\n",
        "request-page",
        page_size=2,
    )
    calls_before = runtime.query_calls

    second = await service.continue_page(
        _scope(command_id="another-command"), first.next_cursor or ""
    )

    assert second.results == (3,)
    assert second.next_cursor is None
    assert runtime.query_calls == calls_before
    assert events[-1] == "artifact:read:geometry_query_result"


async def test_cursor_is_task_bound_before_repository_or_artifact_read() -> None:
    service, _attempts, _artifacts, _runtime, _events = _service()
    first = await service.fresh(
        _scope(),
        "resource-1",
        "version-1",
        "def query(ctx):\n    return [1, 2, 3]\n",
        "request-scope",
        page_size=1,
    )

    with pytest.raises(GeometryQueryApplicationError) as caught:
        await service.continue_page(_scope(task_id="other-task"), first.next_cursor or "")

    assert caught.value.code == "GEOMETRY_QUERY_CURSOR_SCOPE_MISMATCH"


async def test_attempt_alias_resolution_validates_domain_task_and_rls_visibility() -> None:
    service, _attempts, _artifacts, _runtime, _events = _service(result=[])
    page = await service.fresh(
        _scope(),
        "resource-1",
        "version-1",
        "def query(ctx):\n    return []\n",
        "request-alias",
    )

    attempt_id = await service.resolve_attempt_alias(
        _scope(command_id="resolve-command"), page.attempt_alias
    )
    assert attempt_id.startswith("gqa_")

    with pytest.raises(GeometryQueryApplicationError) as wrong_task:
        await service.resolve_attempt_alias(_scope(task_id="other-task"), page.attempt_alias)
    assert wrong_task.value.code == "GEOMETRY_QUERY_ATTEMPT_SCOPE_MISMATCH"

    with pytest.raises(GeometryQueryApplicationError) as wrong_domain:
        await service.resolve_attempt_alias(
            _scope(),
            page.next_cursor or _cursor_codec().encode({"domain": "wrong"}),
        )
    assert wrong_domain.value.code == "GEOMETRY_QUERY_ATTEMPT_ALIAS_INVALID"


async def test_terminal_attempt_list_is_task_bound_keyset_and_reuses_alias() -> None:
    service, attempts, _artifacts, runtime, _events = _service(result=[])
    await service.fresh(
        _scope(),
        "resource-1",
        "version-1",
        "def query(ctx):\n    return []\n",
        "request-list-success",
    )
    runtime.failure = GeometryRuntimeValidationError("geometry query receipt does not reference its input")
    with pytest.raises(GeometryQueryApplicationError):
        await service.fresh(
            _scope(),
            "resource-1",
            "version-1",
            "def query(ctx):\n    return None\n",
            "request-list-failure",
        )

    statuses = (
        GeometryQueryAttemptStatus.SUCCEEDED,
        GeometryQueryAttemptStatus.FAILED,
    )
    first = await service.list_terminal_geometry_query_attempts(
        _scope(),
        statuses=statuses,
        before_created_at=None,
        before_attempt_id=None,
        limit=1,
    )
    second = await service.list_terminal_geometry_query_attempts(
        _scope(command_id="list-next"),
        statuses=statuses,
        before_created_at=first[-1].created_at,
        before_attempt_id=first[-1].attempt_id,
        limit=2,
    )

    assert len(first) == len(second) == 1
    assert (first[0].created_at, first[0].attempt_id) > (
        second[0].created_at,
        second[0].attempt_id,
    )
    assert attempts.list_calls[0][0] == _scope().identity
    alias = service.attempt_alias(_scope(), first[0])
    assert await service.resolve_attempt_alias(_scope(), alias) == first[0].attempt_id
    assert await service.attempt_alias_for_visible_id(_scope(), first[0].attempt_id) == alias
    with pytest.raises(GeometryQueryApplicationError) as hidden_parent:
        await service.attempt_alias_for_visible_id(
            _scope(task_id="other-task"), first[0].attempt_id
        )
    assert hidden_parent.value.code == "GEOMETRY_QUERY_ATTEMPT_NOT_FOUND"
    assert (
        await service.list_terminal_geometry_query_attempts(
            _scope(task_id="other-task"),
            statuses=statuses,
            before_created_at=None,
            before_attempt_id=None,
            limit=2,
        )
        == ()
    )


@pytest.mark.parametrize(
    ("statuses", "before_created_at", "before_attempt_id", "limit"),
    [
        ((GeometryQueryAttemptStatus.PREPARED,), None, None, 1),
        ((GeometryQueryAttemptStatus.SUCCEEDED,), NOW, None, 1),
        ((GeometryQueryAttemptStatus.SUCCEEDED,), None, "gqa_boundary", 1),
        ((GeometryQueryAttemptStatus.SUCCEEDED,), None, None, 202),
    ],
)
async def test_terminal_attempt_list_rejects_invalid_internal_query(
    statuses,
    before_created_at,
    before_attempt_id,
    limit,
) -> None:
    service, attempts, _artifacts, _runtime, _events = _service(result=[])

    with pytest.raises(GeometryQueryApplicationError) as caught:
        await service.list_terminal_geometry_query_attempts(
            _scope(),
            statuses=statuses,
            before_created_at=before_created_at,
            before_attempt_id=before_attempt_id,
            limit=limit,
        )

    assert caught.value.code == "INVALID_ARGUMENT"
    assert attempts.list_calls == []


async def test_repair_uses_parent_exact_source_sha_and_resource_version() -> None:
    service, attempts, artifacts, runtime, events = _service(result={"ok": True})
    original_source = "def query(ctx):\n    return {'ok': False}\n"
    await service.fresh(
        _scope(),
        "resource-1",
        "version-1",
        original_source,
        "request-parent",
    )
    parent = next(iter(attempts.records.values()))

    repaired = await service.repair(
        _scope(command_id="repair-command"),
        parent.attempt_id,
        [SourceEdit(old_text="False", new_text="True")],
        "request-repair",
    )

    child = next(item for item in attempts.records.values() if item.attempt_id != parent.attempt_id)
    source_record, source_content = artifacts.by_id[child.source_artifact.artifact_id]
    assert repaired.resource_version_id == parent.resource_version_id == "version-1"
    assert child.repair_parent_attempt_id == parent.attempt_id
    assert source_record.sha256 == child.source_artifact.sha256
    assert source_content == original_source.replace("False", "True").encode()
    assert runtime.query_calls == 2
    assert events.count("resource:read:version-1") == 2


async def test_repair_rejects_mismatched_parent_source_before_creating_child_attempt() -> None:
    service, attempts, artifacts, _runtime, _events = _service()
    await service.fresh(
        _scope(),
        "resource-1",
        "version-1",
        "def query(ctx):\n    return []\n",
        "request-stale-parent",
    )
    parent = next(iter(attempts.records.values()))
    source_record, _source_bytes = artifacts.by_id[parent.source_artifact.artifact_id]
    artifacts.by_id[parent.source_artifact.artifact_id] = (
        source_record.model_copy(update={"sha256": "0" * 64}),
        _source_bytes,
    )

    with pytest.raises(GeometryQueryApplicationError) as caught:
        await service.repair(
            _scope(),
            parent.attempt_id,
            [SourceEdit(old_text="[]", new_text="[1]")],
            "request-stale-child",
        )

    assert caught.value.code == "GEOMETRY_QUERY_ARTIFACT_INTEGRITY_FAILED"
    assert len(attempts.records) == 1


async def test_runtime_failure_persists_source_attempt_and_bounded_diagnostic() -> None:
    service, attempts, _artifacts, runtime, events = _service()
    runtime.failure = GeometryRuntimeValidationError("geometry query receipt does not reference its input")

    with pytest.raises(GeometryQueryApplicationError) as caught:
        await service.fresh(
            _scope(),
            "resource-1",
            "version-1",
            "def query(ctx):\n    return None\n",
            "request-failure",
        )

    attempt = next(iter(attempts.records.values()))
    assert caught.value.code == "GEOMETRY_QUERY_INVALID"
    assert caught.value.attempt_alias
    assert attempt.status is GeometryQueryAttemptStatus.FAILED
    assert attempt.source_artifact is not None
    assert attempt.result_artifact is None
    assert attempt.diagnostics[0].code == caught.value.code
    assert events.index("attempt:create") < events.index("attempt:complete:FAILED")

    with pytest.raises(GeometryQueryApplicationError) as replayed:
        await service.fresh(
            _scope(command_id="replay-command"),
            "resource-1",
            "version-1",
            "def query(ctx):\n    return None\n",
            "request-failure",
        )
    assert replayed.value.code == caught.value.code
    assert runtime.query_calls == 1

    source_view = await service.read_source(
        _scope(command_id="source-read-command"),
        attempt.attempt_id,
        TextSourceView(view_mode="lines", start_line=1, line_count=1),
    )
    assert source_view.sha256 == attempt.source_artifact.sha256
    assert source_view.lines[0].text == "def query(ctx):\n"


async def test_runtime_resource_limit_is_terminal_nonretryable_and_not_reexecuted() -> None:
    service, attempts, _artifacts, runtime, events = _service()
    runtime.failure = GeometryRuntimeResourceLimitError("private supervisor detail")

    with pytest.raises(GeometryQueryApplicationError) as caught:
        await service.fresh(
            _scope(),
            "resource-1",
            "version-1",
            "def query(ctx):\n    return None\n",
            "request-resource-limit",
        )

    attempt = next(iter(attempts.records.values()))
    assert caught.value.code == "GEOMETRY_QUERY_RESOURCE_LIMIT"
    assert str(caught.value) == "The geometry query exceeded its supervised execution limit"
    assert caught.value.phase == "execution"
    assert caught.value.retryable is False
    assert attempt.status is GeometryQueryAttemptStatus.FAILED
    assert attempt.result_artifact is None
    assert attempt.diagnostics == (
        GeometryQueryDiagnostic(
            code="GEOMETRY_QUERY_RESOURCE_LIMIT",
            message="The geometry query exceeded its supervised execution limit",
        ),
    )
    assert events.index("attempt:create") < events.index("runtime:query")
    assert events.index("runtime:query") < events.index("attempt:complete:FAILED")

    with pytest.raises(GeometryQueryApplicationError) as replayed:
        await service.fresh(
            _scope(command_id="resource-limit-replay"),
            "resource-1",
            "version-1",
            "def query(ctx):\n    return None\n",
            "request-resource-limit",
        )
    assert replayed.value.code == "GEOMETRY_QUERY_RESOURCE_LIMIT"
    assert replayed.value.retryable is False
    assert runtime.query_calls == 1


@pytest.mark.parametrize("sampled_peaks", [None, {"peak_cpu_seconds": 180.02, "peak_rss_bytes": 4096}])
async def test_resource_diagnostic_is_safe_persisted_and_replayed_without_execution(sampled_peaks) -> None:
    service, attempts, _artifacts, runtime, _events = _service()
    diagnostic = GeometryRuntimeResourceDiagnostic(
        resource_kind="cpu",
        configured_limits={"cpu_seconds": 180.0, "rss_bytes": 3 * 1024**3},
        sampled_peaks=sampled_peaks,
        elapsed_seconds=181.2,
        effective_wall_seconds=180.0,
    )
    runtime.failure = GeometryRuntimeResourceLimitError(
        "/private/worker/path SECRET_SOURCE", diagnostic=diagnostic,
    )
    messages = []
    for command_id in ("first-resource-failure", "replay-resource-failure"):
        with pytest.raises(GeometryQueryApplicationError) as caught:
            await service.fresh(
                _scope(command_id=command_id), "resource-1", "version-1",
                "def query(ctx):\n    return None\n", "request-safe-resource-limit",
            )
        assert caught.value.code == "GEOMETRY_QUERY_RESOURCE_LIMIT"
        assert caught.value.retryable is False
        messages.append(str(caught.value))
    assert messages[0] == messages[1]
    assert diagnostic.model_dump_json() in messages[0]
    assert "lower bounds" in messages[0]
    assert "SECRET_SOURCE" not in messages[0]
    assert "/private/" not in messages[0]
    assert len(messages[0].encode()) < 2048
    attempt = next(iter(attempts.records.values()))
    assert attempt.diagnostics[0].message == messages[0]
    assert attempt.result_artifact is None
    assert runtime.query_calls == 1


async def test_terminal_response_loss_reconciles_and_request_replay_does_not_execute() -> None:
    service, attempts, _artifacts, runtime, _events = _service()
    attempts.fail_after_next_complete = True

    first = await service.fresh(
        _scope(),
        "resource-1",
        "version-1",
        "def query(ctx):\n    return [1]\n",
        "request-response-loss",
    )
    replayed = await service.fresh(
        _scope(command_id="replay-command"),
        "resource-1",
        "version-1",
        "def query(ctx):\n    return [1]\n",
        "request-response-loss",
    )

    assert first == replayed
    assert runtime.query_calls == 1
    assert next(iter(attempts.records.values())).status is GeometryQueryAttemptStatus.SUCCEEDED


async def test_source_read_revalidates_task_visibility_and_exact_artifact_version() -> None:
    service, attempts, _artifacts, _runtime, _events = _service(result=[])
    await service.fresh(
        _scope(),
        "resource-1",
        "version-1",
        "def query(ctx):\n    return []\n",
        "request-source-integrity",
    )
    attempt = next(iter(attempts.records.values()))

    with pytest.raises(GeometryQueryApplicationError) as hidden:
        await service.read_source(
            _scope(task_id="other-task"),
            attempt.attempt_id,
            TextSourceView(view_mode="full"),
        )
    assert hidden.value.code == "GEOMETRY_QUERY_ATTEMPT_NOT_FOUND"

    attempts.records[attempt.attempt_id] = attempt.model_copy(
        update={
            "source_artifact": attempt.source_artifact.model_copy(update={"version_id": "av_wrong"})
        }
    )
    with pytest.raises(GeometryQueryApplicationError) as corrupt:
        await service.read_source(
            _scope(),
            attempt.attempt_id,
            TextSourceView(view_mode="full"),
        )
    assert corrupt.value.code == "GEOMETRY_QUERY_ARTIFACT_INTEGRITY_FAILED"
