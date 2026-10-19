from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationErrorPhase,
    CriticalOperationStatus,
    IfcProductsApplyOperationDetail,
)
from CAD2BIM_backend.domain.product_apply import ApplyIfcProductsCommand, ProductApplyInputBinding, ProductApplySummary
from CAD2BIM_backend.application.product_apply_report import stage_product_apply_report, empty_report_rows
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from CAD2BIM_backend.application.ifc_geometry_basis_service import (
    IfcGeometryBasisService,
)
from CAD2BIM_backend.application.ifc_model_service import IfcModelService
from CAD2BIM_backend.application.ifc_revision_reader import IfcRevisionReader
from CAD2BIM_backend.application.ifc_revision_writer import IfcRevisionWriter
from CAD2BIM_backend.application.ifc_write_coordinator import IfcWriteCoordinator
from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    IfcGeometryBasisNoChangeError,
    IfcLocalCoordinateReferenceChange,
    IfcRepresentationContextCatalog,
)
from CAD2BIM_backend.domain.ifc_model import (
    EncodedIfcRevision,
    ExactIfcArtifactVersion,
    ExactIfcRevision,
    IfcExecutionClaim,
    IfcModelConflictError,
    IfcModelFrozenError,
    IfcModelIntegrityError,
    IfcModelInitializeResult,
    IfcModelState,
    IfcRevisionInspection,
    IfcRunIdentity,
    IfcSettledOperationError,
    PreparedIfcInitialize,
    PreparedIfcRevision,
)
from CAD2BIM_backend.ports.ifc_model import (
    CommittedIfcRevision,
    IfcRevisionOperationState,
    IfcSelectedCandidate,
)
from CAD2BIM_backend.ports.storage import StorageConflictError, StorageKey, StoredObject
from CAD2BIM_backend.ports.ifc_revision_runtime import (
    ConfigureIfcGeometryBasisRevisionRequest,
    GeneratedIfcRevision,
    InspectedIfcRevision,
    InspectExactIfcRevisionRequest,
)
from tests.process_support.ifc_revision_runtime import BytesIfcCandidate, read_source


_NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


def _claim(prepared: PreparedIfcInitialize | PreparedIfcRevision) -> IfcExecutionClaim:
    return IfcExecutionClaim(
        operation_id=prepared.operation_id, request_digest=prepared.request_digest,
        owner="ifc:test:1:1:nonce", generation=1, token="test-token",
        deadline_at=_NOW + timedelta(hours=1), remaining_seconds=3600,
    )
_GLOBAL_ID = "0" * 22


async def _product_report(storage, scope, prepared, *, committed):
    summary = ProductApplySummary(outcome="committed" if committed else "technical_failure",
        input_count=1, solve_executed_count=1, solved_count=1, interface_constraints_evaluated_product_count=1,
        build_executed_count=1, built_count=1, committed_count=1 if committed else 0,
        not_executed_count=0)
    rows = empty_report_rows()
    rows["products"] = [{"input_row_index": 0, "product_key": "product-a", "solve_status": "SOLVED",
        "build_status": "BUILT", "commit_status": "COMMITTED" if committed else "NOT_COMMITTED",
        "root_global_id": _GLOBAL_ID if committed else None}]
    return await stage_product_apply_report(storage, scope, prepared,
        detail=IfcProductsApplyOperationDetail(
            request=ApplyIfcProductsCommand(family_version_id="family_version_exact",
                package_id="data_package_input", payload_name="input"),
            input_binding=ProductApplyInputBinding(package_id="data_package_input", member_id="member_input",
                payload_name="input", logical_key_fields=("product_key",))),
        summary=summary, rows=rows)


def _inspection(content: bytes) -> IfcRevisionInspection:
    return IfcRevisionInspection(
        schema_identifier="IFC4X3_ADD2",
        project_global_id=_GLOBAL_ID,
        project_name="Writer Test",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        site_count=0,
        element_count=0,
        unit_assignment_count=0,
        geometric_context_count=0,
        product_body_contexts=(),
        project_length_unit_m=None,
        geometry_basis_configured=False,
        geometry_basis=None,
    )


class _Codec:
    def inspect(self, content: bytes) -> IfcRevisionInspection:
        return _inspection(content)


class _Runtime:
    def invocation_scope(self, policy, *, deadline=None, on_guard_registered=None):
        from contextlib import nullcontext
        return nullcontext()

    def __init__(self, codec: _Codec) -> None:
        self.codec = codec
        self.context_compiler = None

    async def execute(self, request, *, source=None):
        if not isinstance(request, InspectExactIfcRevisionRequest) or source is None:
            raise AssertionError("this test runtime only supports exact inspection")
        content = await read_source(source)
        return InspectedIfcRevision(
            inspection=self.codec.inspect(content),
            contexts=self.context_compiler.inspect_contexts(content) if request.projection == "contexts" else None,
        )

    async def verify_ready(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _generated(content: bytes) -> GeneratedIfcRevision:
    inspection = _inspection(content)
    return GeneratedIfcRevision(
        candidate=BytesIfcCandidate(content, sha256=inspection.sha256),
        inspection=inspection,
    )


def _writer(repository, storage, codec=None, *, runtime=None) -> IfcRevisionWriter:
    selected_codec = codec or _Codec()
    selected_runtime = runtime or _Runtime(selected_codec)
    reader = IfcRevisionReader(repository, storage, selected_runtime)
    return IfcRevisionWriter(
        repository,
        storage,
        reader,
        selected_runtime,
    )


class _FlakyReadCodec(_Codec):
    def __init__(self) -> None:
        self.inspect_calls = 0

    def inspect(self, content: bytes) -> IfcRevisionInspection:
        self.inspect_calls += 1
        if self.inspect_calls == 1:
            raise OSError("injected transient exact read failure")
        return super().inspect(content)


class _Storage:
    def __init__(self, base_key: StorageKey, base_content: bytes) -> None:
        self.objects: dict[StorageKey, tuple[StoredObject, bytes]] = {}
        self._put(base_key, base_content)

    def _put(self, key: StorageKey, content: bytes) -> StoredObject:
        stored = StoredObject(
            key=key,
            filename="model.ifc",
            content_type="application/x-ifc",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=_NOW,
        )
        self.objects[key] = (stored, content)
        return stored

    async def write(self, key, *, filename, content_type, chunks):
        content = b"".join([chunk async for chunk in chunks])
        existing = self.objects.get(key)
        if existing is not None:
            assert existing[1] == content
            return existing[0]
        assert filename == "model.ifc"
        assert content_type == "application/x-ifc"
        return self._put(key, content)

    async def stat(self, key):
        return self.objects[key][0]

    async def read(self, key):
        return self.objects[key][1]

    async def iter_chunks(self, key, *, chunk_size=1024 * 1024, expected=None):
        stored, content = self.objects[key]
        assert expected is None or expected == stored
        for offset in range(0, len(content), chunk_size):
            yield content[offset : offset + chunk_size]


class _FailingWriteStorage:
    def __init__(self, storage: _Storage) -> None:
        self._storage = storage

    def __getattr__(self, name: str):
        return getattr(self._storage, name)

    async def write(self, *args, **kwargs):
        del args, kwargs
        raise OSError("injected immutable publication failure")


class _BlockingWriteStorage:
    def __init__(self, storage: _Storage) -> None:
        self._storage = storage
        self.entered = asyncio.Event()

    def __getattr__(self, name: str):
        return getattr(self._storage, name)

    async def write(self, *args, **kwargs):
        del args, kwargs
        self.entered.set()
        await asyncio.Event().wait()


class _Repository:
    def __init__(self, prepared: PreparedIfcRevision, *, response_loss: bool) -> None:
        self.prepared = prepared
        self.response_loss = response_loss
        self.current = prepared.before
        self.after: ExactIfcRevision | None = None
        self.commit_calls = 0
        self.failed: tuple[str, str] | None = None
        self.prepare_kwargs: dict[str, object] | None = None
        self.get_current_calls = 0
        self.selected: IfcSelectedCandidate | None = None

    async def require_invocation_allowed(self, scope):
        assert scope == self.prepared.before.scope

    async def prepare_revision(self, scope, **kwargs):
        assert scope == self.prepared.before.scope
        assert len(kwargs["request_digest"]) == 64
        self.prepare_kwargs = kwargs
        return self.prepared

    async def claim_execution(self, scope, *, operation_id, request_digest):
        assert scope == self.prepared.before.scope
        assert (operation_id, request_digest) == (
            self.prepared.operation_id, self.prepared.request_digest,
        )
        return _claim(self.prepared)

    async def retire_execution(self, scope, claim):
        assert scope == self.prepared.before.scope
        assert claim.operation_id == self.prepared.operation_id

    async def get_selected_candidate(self, scope, prepared, claim):
        assert scope == self.prepared.before.scope and prepared == self.prepared
        assert claim.operation_id == prepared.operation_id
        return self.selected

    async def select_candidate(self, scope, prepared, claim, candidate):
        assert scope == self.prepared.before.scope and prepared == self.prepared
        assert claim.generation == candidate.origin_generation
        if self.selected is not None:
            assert self.selected == candidate
        self.selected = candidate
        return candidate

    async def get_revision_operation_state(self, scope, **kwargs):
        assert scope == self.prepared.before.scope
        return None

    async def get_current(self, scope):
        assert scope == self.prepared.before.scope
        self.get_current_calls += 1
        return self.current

    async def get_exact(self, scope, *, revision_id):
        assert scope == self.prepared.before.scope
        if revision_id == self.prepared.before.revision_id:
            return self.prepared.before
        if revision_id == self.prepared.after_revision_id:
            return self.after
        return None

    async def commit_revision(self, scope, *, prepared, publication, claim, **business_result):
        assert scope == self.prepared.before.scope
        assert prepared == self.prepared
        assert claim.operation_id == prepared.operation_id
        assert self.selected is not None
        assert publication.inspection.matches_recorded_facts(self.selected.inspection)
        assert all(value is None for value in business_result.values())
        self.commit_calls += 1
        if self.after is None:
            self.after = ExactIfcRevision(
                scope=scope,
                model_id=prepared.before.model_id,
                model_state=IfcModelState.ACTIVE,
                revision_id=prepared.after_revision_id,
                revision_no=2,
                write_epoch=2,
                artifact=publication.artifact,
                project_global_id=prepared.before.project_global_id,
            )
            self.current = self.after
            if self.response_loss:
                raise RuntimeError("injected commit response loss")
        return CommittedIfcRevision(operation_alias="op_002", exact=self.after)

    async def record_failed_ifc_operation(
        self,
        scope,
        *,
        operation_id,
        error_code,
        error_message,
        error_phase,
        claim,
        spatial_blockers=None,
    ):
        assert scope == self.prepared.before.scope
        assert operation_id == self.prepared.operation_id
        assert claim.operation_id == operation_id
        assert error_phase.value == "ifc_validation"
        self.failed = (error_code, error_message)
        return "op_002"


class _BlockingResponseLossRepository(_Repository):
    def __init__(self, prepared: PreparedIfcRevision) -> None:
        super().__init__(prepared, response_loss=False)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def commit_revision(self, scope, *, prepared, publication, **business_result):
        first_commit = self.after is None
        committed = await super().commit_revision(
            scope,
            prepared=prepared,
            publication=publication,
            **business_result,
        )
        if first_commit:
            self.entered.set()
            await self.release.wait()
            raise RuntimeError("injected commit response loss after cancellation")
        return committed


class _BlockingInitializeResponseLossRepository:
    def __init__(self, prepared: PreparedIfcInitialize) -> None:
        self.prepared = prepared
        self.exact: ExactIfcRevision | None = None
        self.result: IfcModelInitializeResult | None = None
        self.commit_calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.selected: IfcSelectedCandidate | None = None

    async def get_selected_candidate(self, scope, prepared, claim):
        assert scope == self.prepared.scope and prepared == self.prepared
        assert claim.operation_id == prepared.operation_id
        return self.selected

    async def select_candidate(self, scope, prepared, claim, candidate):
        assert scope == self.prepared.scope and prepared == self.prepared
        assert claim.generation == candidate.origin_generation
        self.selected = candidate
        return candidate

    async def commit_initialize(self, scope, *, prepared, publication, claim):
        assert scope == self.prepared.scope
        assert prepared == self.prepared
        assert claim.operation_id == prepared.operation_id
        assert self.selected is not None
        assert publication.inspection.matches_recorded_facts(self.selected.inspection)
        self.commit_calls += 1
        if self.exact is None:
            self.exact = ExactIfcRevision(
                scope=scope,
                model_id=prepared.model_id,
                model_state=IfcModelState.ACTIVE,
                revision_id=prepared.revision_id,
                revision_no=1,
                write_epoch=1,
                artifact=publication.artifact,
                project_global_id=prepared.project_global_id,
            )
            self.result = IfcModelInitializeResult(
                operation_alias="op_001",
                model=publication.inspection.to_safe_info(),
            )
            self.entered.set()
            await self.release.wait()
            raise RuntimeError("injected initialize response loss after cancellation")
        assert self.result is not None
        return self.result

    async def get_exact(self, scope, *, revision_id):
        assert scope == self.prepared.scope
        if revision_id == self.prepared.revision_id:
            return self.exact
        return None


class _FrozenRepository(_Repository):
    def __init__(
        self,
        prepared: PreparedIfcRevision,
        *,
        status: CriticalOperationStatus,
        after: ExactIfcRevision | None,
    ) -> None:
        super().__init__(prepared, response_loss=False)
        self.status = status
        self.after = after
        self.allowed = False
        self.unfreeze_calls = 0
        self.reopen_calls = 0

    async def require_invocation_allowed(self, scope):
        assert scope == self.prepared.before.scope
        if not self.allowed:
            raise IfcModelFrozenError("paused")

    async def get_revision_operation_state(self, scope, **kwargs):
        assert scope == self.prepared.before.scope
        assert kwargs["idempotency_key"] == self.prepared.idempotency_key
        return IfcRevisionOperationState(
            prepared=self.prepared,
            status=self.status,
        )

    async def unfreeze_succeeded_revision(
        self,
        scope,
        *,
        operation_id,
        revision_id,
    ):
        assert scope == self.prepared.before.scope
        assert operation_id == self.prepared.operation_id
        assert revision_id == self.prepared.after_revision_id
        self.unfreeze_calls += 1
        self.allowed = True

    async def reopen_indeterminate_revision(self, scope, *, operation_id):
        assert scope == self.prepared.before.scope
        assert operation_id == self.prepared.operation_id
        self.reopen_calls += 1
        self.allowed = True
        self.status = CriticalOperationStatus.PREPARED


class _NoChangeCompiler:
    def apply(self, base_content, command):
        del command
        return EncodedIfcRevision(
            content=base_content,
            inspection=_inspection(base_content),
        )

    def inspect_geometry_basis(self, content):
        del content
        return None

    def inspect_contexts(self, content):
        del content
        return IfcRepresentationContextCatalog()


class _NoChangeRuntime(_Runtime):
    async def execute(self, request, *, source=None):
        if isinstance(request, ConfigureIfcGeometryBasisRevisionRequest):
            raise IfcGeometryBasisNoChangeError("no effective change")
        return await super().execute(request, source=source)


class _FlakyContextCompiler(_NoChangeCompiler):
    def __init__(self) -> None:
        self.inspect_context_calls = 0

    def inspect_contexts(self, content):
        del content
        self.inspect_context_calls += 1
        if self.inspect_context_calls == 1:
            raise OSError("injected transient context inspection failure")
        return IfcRepresentationContextCatalog()


def _fixture(*, response_loss: bool = False):
    scope = IfcRunIdentity(
        owner_id="owner",
        tenant_id="tenant",
        task_id="task",
        run_id="run",
    )
    base_content = b"ISO-10303-21;base"
    base_artifact = ExactIfcArtifactVersion(
        artifact_id="artifact_base",
        version_id="av_base",
        size_bytes=len(base_content),
        sha256=hashlib.sha256(base_content).hexdigest(),
    )
    before = ExactIfcRevision(
        scope=scope,
        model_id="ifcmodel_test",
        model_state=IfcModelState.ACTIVE,
        revision_id="ifcrev_base",
        revision_no=1,
        write_epoch=1,
        artifact=base_artifact,
        project_global_id=_GLOBAL_ID,
    )
    request_digest = hashlib.sha256(b"request").hexdigest()
    prepared = PreparedIfcRevision(
        operation_id="operation_test",
        idempotency_key="request-id",
        request_digest=request_digest,
        before=before,
        after_revision_id="ifcrev_after",
        artifact_id="artifact_after",
        artifact_version_id="av_after",
        prepared_at=_NOW,
    )
    base_key = StorageKey(
        owner_id=scope.owner_id,
        task_id=scope.task_id,
        object_id=base_artifact.artifact_id,
        version=1,
    )
    storage = _Storage(base_key, base_content)
    repository = _Repository(prepared, response_loss=response_loss)
    return scope, base_content, repository, storage


@pytest.mark.asyncio
@pytest.mark.parametrize("response_loss", (False, True))
async def test_trusted_baseline_uses_ordinary_inspection_and_existing_commit(response_loss):
    scope, _, repository, storage = _fixture(response_loss=response_loss)
    runtime = _Runtime(_Codec())
    runtime.execute = AsyncMock(wraps=runtime.execute)
    writer = _writer(repository, storage, runtime=runtime)
    content = b"ISO-10303-21;trusted-baseline"
    source = BytesIfcCandidate(content, sha256=hashlib.sha256(content).hexdigest())

    committed = await writer.commit_trusted_baseline(
        scope, repository.prepared, source, claim=_claim(repository.prepared),
    )

    assert committed.exact == repository.current
    assert committed.exact.revision_no == 2
    assert committed.exact.project_global_id == repository.prepared.before.project_global_id
    assert repository.commit_calls == (2 if response_loss else 1)
    runtime.execute.assert_awaited_once()
    request = runtime.execute.call_args.args[0]
    assert isinstance(request, InspectExactIfcRevisionRequest)
    assert request.expected_project_global_id is None and request.expected_project_name is None
    borrowed = runtime.execute.call_args.kwargs["source"]
    assert borrowed.closed and not source.closed
    with pytest.raises(IfcModelIntegrityError, match="candidate is closed"):
        await read_source(borrowed)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("inspection", "project", "storage"))
async def test_trusted_baseline_failure_closes_candidate_without_advancing_current(failure):
    scope, _, repository, storage = _fixture()

    class Codec(_Codec):
        def inspect(self, content):
            inspection = super().inspect(content)
            return inspection.model_copy(update={"project_global_id": "1" * 22}) if failure == "project" else inspection

    runtime = _Runtime(Codec())
    runtime.execute = AsyncMock(wraps=runtime.execute)
    if failure == "inspection":
        runtime.execute.side_effect = OSError("source inspection failed")
    writer = _writer(repository, _FailingWriteStorage(storage) if failure == "storage" else storage,
                     runtime=runtime)
    content = b"ISO-10303-21;rejected-trusted-baseline"
    source = BytesIfcCandidate(content, sha256=hashlib.sha256(content).hexdigest())

    with pytest.raises(IfcModelIntegrityError if failure == "project" else OSError):
        await writer.commit_trusted_baseline(scope, repository.prepared, source, claim=_claim(repository.prepared))

    assert repository.current == repository.prepared.before
    assert repository.commit_calls == 0 and repository.selected is None
    assert len(storage.objects) == 1
    assert runtime.execute.call_args.kwargs["source"].closed
    assert not source.closed


@pytest.mark.asyncio
async def test_trusted_baseline_selected_resume_uses_stored_candidate_without_source():
    scope, _, repository, storage = _fixture()
    runtime = _Runtime(_Codec())
    runtime.execute = AsyncMock(wraps=runtime.execute)
    writer = _writer(repository, storage, runtime=runtime)
    original_commit = repository.commit_revision
    repository.commit_revision = AsyncMock(side_effect=OSError("commit unavailable before selection settlement"))
    content = b"ISO-10303-21;selected-trusted-baseline"
    source = BytesIfcCandidate(content, sha256=hashlib.sha256(content).hexdigest())
    with pytest.raises(OSError):
        await writer.commit_trusted_baseline(scope, repository.prepared, source, claim=_claim(repository.prepared))
    selected = repository.selected
    assert selected is not None and repository.current == repository.prepared.before
    source.closed = True
    repository.commit_revision = original_commit

    committed, recovered = await writer.resume_selected_revision(scope, repository.prepared, _claim(repository.prepared))

    assert recovered == selected
    assert committed.exact == repository.current
    assert committed.exact.revision_no == 2
    assert runtime.execute.await_count == 3  # source, selected storage, committed exact storage


@pytest.mark.asyncio
async def test_trusted_baseline_real_runtime_inspects_nonempty_source_without_revision1_mode(tmp_path):
    import ifcopenshell
    import sys
    from CAD2BIM_backend.infrastructure.ifc_revision_runtime import IfcOpenShellRevisionRuntime

    scope, _, repository, storage = _fixture()
    model = ifcopenshell.file(schema="IFC4X3_ADD2")
    model.create_entity("IfcProject", GlobalId=_GLOBAL_ID, Name="Writer Test")
    model.create_entity("IfcSite", GlobalId="1" * 22, Name="Trusted source site")
    content = model.to_string().encode()
    source = BytesIfcCandidate(content, sha256=hashlib.sha256(content).hexdigest())
    runtime = IfcOpenShellRevisionRuntime(python_executable=sys.executable, capacity_root=tmp_path / "capacity")
    writer = _writer(repository, storage, runtime=runtime)
    try:
        committed = await writer.commit_trusted_baseline(scope, repository.prepared, source, claim=_claim(repository.prepared))
    finally:
        await runtime.close()
    assert committed.inspection.schema_identifier == "IFC4X3_ADD2"
    assert committed.inspection.site_count == 1
    assert committed.exact.project_global_id == _GLOBAL_ID
    assert committed.exact.revision_no == 2


@pytest.mark.asyncio
async def test_trusted_baseline_committed_readback_failure_cannot_recover_until_readable(tmp_path):
    from CAD2BIM_backend.domain.ifc_model import IfcCommittedVerificationError

    scope, _, repository, _ = _fixture()
    storage = LocalStorage(tmp_path / "storage")
    writer = _writer(repository, storage)
    prepared = repository.prepared
    content = b"ISO-10303-21;trusted-baseline"
    source = BytesIfcCandidate(content, sha256=hashlib.sha256(content).hexdigest())
    repository.freeze_indeterminate = AsyncMock()
    original_commit = repository.commit_revision

    async def corrupt_after_commit(*args, **kwargs):
        committed = await original_commit(*args, **kwargs)
        key = StorageKey(scope.owner_id, scope.task_id, committed.exact.artifact.artifact_id, 1)
        (storage._logical_directory(key) / "payload").write_bytes(content[:-1] + b"!")
        return committed

    repository.commit_revision = corrupt_after_commit
    with pytest.raises(IfcCommittedVerificationError) as committed_failure:
        await writer.commit_trusted_baseline(scope, prepared, source, claim=_claim(prepared))
    assert committed_failure.value.operation_alias == "op_002"
    assert repository.current == repository.after
    state = IfcRevisionOperationState(prepared=prepared, status=CriticalOperationStatus.SUCCEEDED,
                                      operation_alias="op_002")
    repository.get_revision_operation_state = AsyncMock(return_value=state)
    request = dict(tool_name="ifc_trusted_baseline_load", idempotency_key=prepared.idempotency_key,
                   request_digest=prepared.request_digest, intent_summary="trusted baseline",
                   operation_detail={"detail_kind": "ifc_trusted_baseline_load"})
    for _ in range(2):
        with pytest.raises(IfcCommittedVerificationError) as recovered_failure:
            await writer.recover_revision(scope, **request)
        assert recovered_failure.value.operation_alias == "op_002"
        assert recovered_failure.value.revision_id == prepared.after_revision_id
    assert repository.freeze_indeterminate.await_count == 3
    assert repository.commit_calls == 1 and repository.failed is None

    key = StorageKey(scope.owner_id, scope.task_id, prepared.artifact_id, 1)
    (storage._logical_directory(key) / "payload").write_bytes(content)
    assert await writer.recover_revision(scope, **request) is state
    assert repository.freeze_indeterminate.await_count == 3
    assert repository.commit_calls == 1


@pytest.mark.asyncio
async def test_settled_revision_replay_reads_original_after_current_moves_without_commit() -> None:
    scope, _base, repository, storage = _fixture()
    writer = _writer(repository, storage)
    prepared = repository.prepared
    await writer.commit(scope, prepared, _generated(b"ISO-10303-21;original"), claim=_claim(prepared))
    original = repository.after
    assert original is not None
    repository.current = original.model_copy(update={
        "revision_id": "ifcrev_later", "revision_no": 3, "write_epoch": 3,
    })
    repository.get_revision_operation_state = AsyncMock(return_value=IfcRevisionOperationState(
        prepared=prepared, status=CriticalOperationStatus.SUCCEEDED,
        operation_alias="op_002",
    ))

    state = await writer.recover_revision(
        scope, tool_name="ifc_project_update", idempotency_key=prepared.idempotency_key,
        request_digest=prepared.request_digest, intent_summary="original intent",
        operation_detail={"detail_kind": "ifc_project_update"},
    )

    assert state is not None and state.operation_alias == "op_002"
    assert repository.commit_calls == 1
    repository.get_revision_operation_state.assert_awaited_once()
    assert await repository.get_exact(scope, revision_id=prepared.after_revision_id) == original


@pytest.mark.asyncio
async def test_failed_revision_replay_returns_original_error_without_commit() -> None:
    scope, _base, repository, storage = _fixture()
    writer = _writer(repository, storage)
    prepared = repository.prepared
    repository.get_revision_operation_state = AsyncMock(return_value=IfcRevisionOperationState(
        prepared=prepared, status=CriticalOperationStatus.FAILED,
        error_code="IFC_NO_CHANGES", error_message="original no-change reason",
        error_phase=CriticalOperationErrorPhase.IFC_VALIDATION,
        operation_alias="op_002",
    ))

    with pytest.raises(IfcSettledOperationError) as caught:
        await writer.recover_revision(
            scope, tool_name="ifc_project_update", idempotency_key=prepared.idempotency_key,
            request_digest=prepared.request_digest, intent_summary="original intent",
            operation_detail={"detail_kind": "ifc_project_update"},
        )

    assert (caught.value.code, str(caught.value), caught.value.phase,
            caught.value.operation_alias, caught.value.retryable) == (
        "IFC_NO_CHANGES", "original no-change reason", "ifc_validation", "op_002", False,
    )
    assert repository.commit_calls == 0


@pytest.mark.asyncio
async def test_geometry_basis_success_replay_inspects_original_without_mutation() -> None:
    scope, _base, repository, storage = _fixture()
    writer = _writer(repository, storage)
    prepared = repository.prepared
    await writer.commit(scope, prepared, _generated(b"ISO-10303-21;basis"), claim=_claim(prepared))
    repository.get_revision_operation_state = AsyncMock(return_value=IfcRevisionOperationState(
        prepared=prepared, status=CriticalOperationStatus.SUCCEEDED,
        operation_alias="op_002",
    ))
    runtime = _NoChangeRuntime(_Codec())
    service = IfcGeometryBasisService(writer, runtime, IfcWriteCoordinator())

    result = await service.configure(
        scope, ConfigureIfcGeometryBasisCommand(
            coordinate_reference_change=IfcLocalCoordinateReferenceChange(),
        ), idempotency_key=prepared.idempotency_key,
    )

    assert result.operation_alias == "op_002"
    assert result.model.project.global_id == _GLOBAL_ID
    assert repository.commit_calls == 1
    assert repository.prepare_kwargs is None


@pytest.mark.asyncio
async def test_writer_reconciles_commit_response_loss_to_the_exact_revision() -> None:
    scope, base_content, repository, storage = _fixture(response_loss=True)
    writer = _writer(repository, storage)
    prepared = repository.prepared
    source = await writer.open_prepared_base(scope, prepared)
    assert await read_source(source) == base_content
    candidate_content = b"ISO-10303-21;candidate"

    committed = await writer.commit(
        scope,
        prepared,
        _generated(candidate_content),
        claim=_claim(prepared),
    )

    assert repository.commit_calls == 2
    assert committed.operation_alias == "op_002"
    assert committed.exact == repository.current
    assert committed.inspection == _inspection(candidate_content)
    assert repository.current.revision_no == repository.current.write_epoch == 2


@pytest.mark.asyncio
async def test_normal_writer_reuses_same_call_inspection_after_formal_bytes_check() -> None:
    scope, _, repository, storage = _fixture()
    runtime = _Runtime(_Codec())
    runtime.execute = AsyncMock(wraps=runtime.execute)
    writer = _writer(repository, storage, runtime=runtime)
    generated = _generated(b"ISO-10303-21;candidate")

    committed = await writer.commit(scope, repository.prepared, generated,
                                    claim=_claim(repository.prepared))

    assert committed.inspection == generated.inspection
    assert repository.commit_calls == 1
    runtime.execute.assert_not_called()
    final = await storage.stat(StorageKey(scope.owner_id, scope.task_id,
                                         committed.exact.artifact.artifact_id, 1))
    assert final.sha256 == generated.inspection.sha256


@pytest.mark.asyncio
async def test_same_call_reuse_still_freezes_corrupt_formal_bytes_after_commit(tmp_path) -> None:
    from CAD2BIM_backend.domain.ifc_model import IfcCommittedVerificationError

    scope, _, repository, _ = _fixture()
    storage = LocalStorage(tmp_path / "storage")
    runtime = _Runtime(_Codec())
    runtime.execute = AsyncMock(wraps=runtime.execute)
    writer = _writer(repository, storage, runtime=runtime)
    original_commit = repository.commit_revision
    repository.freeze_indeterminate = AsyncMock()

    async def corrupt_formal_after_commit(*args, **kwargs):
        committed = await original_commit(*args, **kwargs)
        key = StorageKey(scope.owner_id, scope.task_id, committed.exact.artifact.artifact_id, 1)
        path = storage._logical_directory(key) / "payload"
        payload = path.read_bytes()
        path.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
        return committed

    repository.commit_revision = corrupt_formal_after_commit
    with pytest.raises(IfcCommittedVerificationError):
        await writer.commit(scope, repository.prepared, _generated(b"ISO-10303-21;candidate"),
                            claim=_claim(repository.prepared))
    assert repository.commit_calls == 1
    repository.freeze_indeterminate.assert_awaited_once()
    runtime.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ("valid", "missing", "duplicate", "wrong_view", "wrong_parent", "no_units"))
async def test_body_preflight_reuses_exact_revision_facts_without_runtime(state) -> None:
    from CAD2BIM_backend.domain.ifc_geometry_basis import (
        IfcGeometryBasisSummary, IfcProductBodyContextFacts,
        IfcRepresentationContextIncompatibleError, IfcRepresentationContextNotFoundError,
    )

    scope, content, repository, storage = _fixture()
    context = IfcProductBodyContextFacts(
        context_type="Model", target_view="MODEL_VIEW",
        parent_entity_type="IfcGeometricRepresentationContext",
        parent_context_type="Model", parent_coordinate_space_dimension=3,
    )
    if state == "wrong_view":
        context = context.model_copy(update={"target_view": "PLAN_VIEW"})
    if state == "wrong_parent":
        context = context.model_copy(update={"parent_coordinate_space_dimension": 2})
    contexts = () if state == "missing" else (context, context) if state == "duplicate" else (context,)
    basis = IfcGeometryBasisSummary(
        length_unit={"name": "METRE", "prefix": "MILLI"}, plane_angle_unit={"name": "RADIAN"},
        precision=1e-5, coordinate_reference={"mode": "local"}, main_context_identifier="Model",
    )
    inspection = _inspection(content).model_copy(update={
        "product_body_contexts": contexts,
        "geometry_basis": None if state == "no_units" else basis,
        "geometry_basis_configured": state != "no_units",
        "project_length_unit_m": None if state == "no_units" else .001,
    })
    repository.get_revision_inspection = AsyncMock(return_value=inspection)
    runtime = _Runtime(_Codec())
    runtime.execute = AsyncMock(wraps=runtime.execute)
    writer = _writer(repository, storage, runtime=runtime)
    if state == "valid":
        body = await writer.preflight_prepared_product_body(repository.prepared)
        assert body.project_length_unit_m == .001
    else:
        error = IfcRepresentationContextNotFoundError if state == "missing" else IfcRepresentationContextIncompatibleError
        with pytest.raises(error):
            await writer.preflight_prepared_product_body(repository.prepared)
    repository.get_revision_inspection.assert_awaited_once_with(repository.prepared.before)
    runtime.execute.assert_not_called()


def test_persisted_revision_inspection_requires_exact_bytes_and_current_facts() -> None:
    from CAD2BIM_backend.infrastructure.postgres.repository_ifc_model import PostgresIfcModelRepositoryMixin

    _, content, repository, _ = _fixture()
    inspection = _inspection(content)
    read = PostgresIfcModelRepositoryMixin._inspection_from_metadata
    exact = repository.prepared.before
    assert read(exact, {"ifc_inspection": inspection.model_dump(mode="json")}) == inspection
    assert read(exact, {}) is None
    stale = inspection.model_dump(mode="json")
    stale.pop("product_body_contexts")
    assert read(exact, {"ifc_inspection": stale}) is None
    altered = inspection.model_copy(update={"sha256": "0" * 64})
    with pytest.raises(IfcModelIntegrityError, match="ArtifactVersion"):
        read(exact, {"ifc_inspection": altered.model_dump(mode="json")})


@pytest.mark.asyncio
async def test_body_preflight_without_saved_facts_reads_exact_before_missing_body_failure() -> None:
    from CAD2BIM_backend.domain.ifc_geometry_basis import IfcRepresentationContextNotFoundError

    _, _, repository, storage = _fixture()
    repository.get_revision_inspection = AsyncMock(return_value=None)
    runtime = _Runtime(_Codec())
    runtime.execute = AsyncMock(wraps=runtime.execute)
    writer = _writer(repository, storage, runtime=runtime)
    with pytest.raises(IfcRepresentationContextNotFoundError):
        await writer.preflight_prepared_product_body(repository.prepared)
    runtime.execute.assert_awaited_once()
    assert runtime.execute.call_args.kwargs["source"].sha256 == repository.prepared.before.artifact.sha256


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", ("unknown", "wrong"))
async def test_selected_recovery_inspects_missing_facts_and_rejects_recorded_fact_changes(recorded) -> None:
    from CAD2BIM_backend.domain.ifc_geometry_basis import IfcProductBodyContextFacts

    scope, _, repository, storage = _fixture()
    runtime = _Runtime(_Codec())
    runtime.execute = AsyncMock(wraps=runtime.execute)
    writer = _writer(repository, storage, runtime=runtime)
    generated = _generated(b"ISO-10303-21;selected-payload")
    contexts = None if recorded == "unknown" else (IfcProductBodyContextFacts(
        context_type="Model", target_view="MODEL_VIEW",
        parent_entity_type="IfcGeometricRepresentationContext",
        parent_context_type="Model", parent_coordinate_space_dimension=3),)
    generated = GeneratedIfcRevision(candidate=generated.candidate,
        inspection=generated.inspection.model_copy(update={"product_body_contexts": contexts}))
    selected, _ = await writer._stage_and_select(scope, repository.prepared, _claim(repository.prepared), generated)
    if recorded == "unknown":
        committed, recovered = await writer.resume_selected_revision(scope, repository.prepared, _claim(repository.prepared))
        assert committed.inspection.product_body_contexts == ()
        assert recovered == selected and selected.inspection.product_body_contexts is None
        assert repository.commit_calls == 1
        assert runtime.execute.await_count == 2
    else:
        with pytest.raises(IfcModelIntegrityError, match="recovery inspection"):
            await writer.resume_selected_revision(scope, repository.prepared, _claim(repository.prepared))
        assert repository.commit_calls == 0
        assert runtime.execute.await_count == 1


@pytest.mark.asyncio
async def test_revision_artifact_registration_persists_its_actual_inspection(monkeypatch) -> None:
    from CAD2BIM_backend.domain.ifc_model import VerifiedIfcPublication
    from CAD2BIM_backend.infrastructure.postgres import repository_ifc_model as module

    scope, content, repository, _ = _fixture()
    inspection = _inspection(content)
    publication = VerifiedIfcPublication(scope=scope, artifact=repository.prepared.before.artifact,
                                         inspection=inspection, storage_created_at=_NOW)
    register = AsyncMock()
    monkeypatch.setattr(module, "register_artifact_in_transaction", register)
    await module.PostgresIfcModelRepositoryMixin._register_artifact(
        object(), scope, publication, operation_id="operation", model_id="model",
        revision_id="revision", revision_no=1, now=_NOW,
    )
    record = register.call_args.args[1]
    assert record.version_metadata["ifc_inspection"] == inspection.model_dump(mode="json")


@pytest.mark.asyncio
async def test_staged_candidate_corruption_is_rejected_before_selection_without_parsing(tmp_path) -> None:
    scope, _, repository, _ = _fixture()
    storage = LocalStorage(tmp_path / "storage")
    original_stat = storage.stat

    async def corrupt_after_stat(key):
        stored = await original_stat(key)
        # Actual same-length payload damage after stat has checked its digest.
        # Metadata stays unchanged; consuming only metadata would miss this.
        payload_path = storage._logical_directory(key) / "payload"
        payload = payload_path.read_bytes()
        payload_path.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
        return stored

    storage.stat = corrupt_after_stat
    runtime = _Runtime(_Codec())
    runtime.execute = AsyncMock(wraps=runtime.execute)
    writer = _writer(repository, storage, runtime=runtime)
    with pytest.raises(IfcModelIntegrityError, match="bytes do not match"):
        await writer.commit(scope, repository.prepared, _generated(b"ISO-10303-21;candidate"),
                            claim=_claim(repository.prepared))
    assert repository.selected is None and repository.commit_calls == 0
    assert repository.current == repository.prepared.before
    runtime.execute.assert_not_called()


@pytest.mark.asyncio
async def test_writer_finishes_commit_reconcile_after_caller_cancellation() -> None:
    scope, _, original, storage = _fixture()
    repository = _BlockingResponseLossRepository(original.prepared)
    writer = _writer(repository, storage)

    invocation = asyncio.create_task(
        writer.commit(
            scope,
            repository.prepared,
            _generated(b"ISO-10303-21;cancelled-response"),
            claim=_claim(repository.prepared),
        )
    )
    await repository.entered.wait()
    invocation.cancel()
    await asyncio.sleep(0)

    assert not invocation.done()
    repository.release.set()
    with pytest.raises(asyncio.CancelledError):
        await invocation

    assert repository.commit_calls == 2
    assert repository.after is not None
    assert repository.current == repository.after
    assert repository.current.revision_no == repository.current.write_epoch == 2


@pytest.mark.asyncio
async def test_writer_finishes_initialize_reconcile_after_caller_cancellation() -> None:
    scope = IfcRunIdentity(
        owner_id="owner_test",
        tenant_id="tenant_test",
        task_id="task_test",
        run_id="run_initialize_cancel",
    )
    prepared = PreparedIfcInitialize(
        scope=scope,
        operation_id="operation_initialize",
        idempotency_key="ifc_model_initialize:run_initialize_cancel",
        request_digest=hashlib.sha256(b"initialize").hexdigest(),
        model_id="ifcmodel_initialize",
        revision_id="ifcrev_initialize",
        artifact_id="artifact_initialize",
        artifact_version_id="av_initialize",
        project_global_id=_GLOBAL_ID,
        prepared_at=_NOW,
    )
    repository = _BlockingInitializeResponseLossRepository(prepared)
    unused_key = StorageKey(
        owner_id=scope.owner_id,
        task_id=scope.task_id,
        object_id="unused_base",
        version=1,
    )
    storage = _Storage(unused_key, b"unused")
    writer = _writer(repository, storage)
    invocation = asyncio.create_task(
        writer.commit_initialize(
            scope,
            prepared,
            _generated(b"ISO-10303-21;cancelled-initialize-response"),
            claim=_claim(prepared),
        )
    )
    await repository.entered.wait()
    invocation.cancel()
    await asyncio.sleep(0)

    assert not invocation.done()
    repository.release.set()
    with pytest.raises(asyncio.CancelledError):
        await invocation

    assert repository.commit_calls == 2
    assert repository.exact is not None
    assert repository.exact.revision_no == repository.exact.write_epoch == 1


@pytest.mark.asyncio
async def test_writer_reconciles_frozen_success_using_the_same_exact_operation() -> None:
    scope, _, original, storage = _fixture()
    after_content = b"ISO-10303-21;committed-before-response-loss"
    after_artifact = ExactIfcArtifactVersion(
        artifact_id=original.prepared.artifact_id,
        version_id=original.prepared.artifact_version_id,
        size_bytes=len(after_content),
        sha256=hashlib.sha256(after_content).hexdigest(),
    )
    after = ExactIfcRevision(
        scope=scope,
        model_id=original.prepared.before.model_id,
        model_state=IfcModelState.FROZEN,
        revision_id=original.prepared.after_revision_id,
        revision_no=2,
        write_epoch=2,
        artifact=after_artifact,
        project_global_id=original.prepared.before.project_global_id,
    )
    frozen_before = original.prepared.before.model_copy(
        update={"model_state": IfcModelState.FROZEN}
    )
    prepared = original.prepared.model_copy(update={"before": frozen_before})
    repository = _FrozenRepository(
        prepared,
        status=CriticalOperationStatus.SUCCEEDED,
        after=after,
    )
    storage._put(
        StorageKey(
            owner_id=scope.owner_id,
            task_id=scope.task_id,
            object_id=after_artifact.artifact_id,
            version=1,
        ),
        after_content,
    )
    writer = _writer(repository, storage)

    replay = await writer.prepare(
        scope,
        tool_name="ifc_project_update",
        idempotency_key=prepared.idempotency_key,
        request_digest=prepared.request_digest,
        intent_summary="Update the bounded IfcProject business fields",
        operation_detail={"detail_kind": "ifc_project_update"},
    )

    assert replay == prepared
    assert repository.unfreeze_calls == 1
    assert repository.reopen_calls == 0


@pytest.mark.asyncio
async def test_writer_reopens_only_an_indeterminate_write_with_no_after_revision() -> None:
    scope, _, original, storage = _fixture()
    prepared = original.prepared.model_copy(
        update={
            "before": original.prepared.before.model_copy(
                update={"model_state": IfcModelState.FROZEN}
            )
        }
    )
    repository = _FrozenRepository(
        prepared,
        status=CriticalOperationStatus.INDETERMINATE,
        after=None,
    )
    writer = _writer(repository, storage)

    replay = await writer.prepare(
        scope,
        tool_name="ifc_project_update",
        idempotency_key=prepared.idempotency_key,
        request_digest=prepared.request_digest,
        intent_summary="Update the bounded IfcProject business fields",
        operation_detail={"detail_kind": "ifc_project_update"},
    )

    assert replay == prepared
    assert repository.reopen_calls == 1
    assert repository.unfreeze_calls == 0


@pytest.mark.asyncio
async def test_writer_keeps_success_without_exact_revision_frozen() -> None:
    scope, _, original, storage = _fixture()
    prepared = original.prepared.model_copy(
        update={
            "before": original.prepared.before.model_copy(
                update={"model_state": IfcModelState.FROZEN}
            )
        }
    )
    repository = _FrozenRepository(
        prepared,
        status=CriticalOperationStatus.SUCCEEDED,
        after=None,
    )
    writer = _writer(repository, storage)

    with pytest.raises(IfcModelFrozenError, match="exact evidence is invalid"):
        await writer.prepare(
            scope,
            tool_name="ifc_project_update",
            idempotency_key=prepared.idempotency_key,
            request_digest=prepared.request_digest,
            intent_summary="Update the bounded IfcProject business fields",
            operation_detail={"detail_kind": "ifc_project_update"},
        )

    assert repository.reopen_calls == 0
    assert repository.unfreeze_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [CriticalOperationStatus.FAILED, CriticalOperationStatus.INDETERMINATE],
)
async def test_writer_keeps_contradictory_or_failed_revision_state_closed(
    status: CriticalOperationStatus,
) -> None:
    scope, _, original, storage = _fixture()
    prepared = original.prepared.model_copy(
        update={
            "before": original.prepared.before.model_copy(
                update={"model_state": IfcModelState.FROZEN}
            )
        }
    )
    contradictory_after = prepared.before.model_copy(
        update={
            "revision_id": prepared.after_revision_id,
            "revision_no": 2,
            "write_epoch": 2,
        }
    )
    repository = _FrozenRepository(
        prepared,
        status=status,
        after=contradictory_after,
    )
    writer = _writer(repository, storage)

    expected_error = (
        IfcModelConflictError if status is CriticalOperationStatus.FAILED else IfcModelFrozenError
    )
    with pytest.raises(expected_error):
        await writer.prepare(
            scope,
            tool_name="ifc_project_update",
            idempotency_key=prepared.idempotency_key,
            request_digest=prepared.request_digest,
            intent_summary="Update the bounded IfcProject business fields",
            operation_detail={"detail_kind": "ifc_project_update"},
        )

    assert repository.reopen_calls == 0
    assert repository.unfreeze_calls == 0


@pytest.mark.asyncio
async def test_no_change_configuration_fails_without_revision_or_current_advance() -> None:
    scope, base_content, repository, storage = _fixture()
    writer = _writer(repository, storage)
    service = IfcGeometryBasisService(
        writer,
        _NoChangeRuntime(_Codec()),
        IfcWriteCoordinator(),
    )
    command = ConfigureIfcGeometryBasisCommand(
        coordinate_reference_change=IfcLocalCoordinateReferenceChange()
    )

    with pytest.raises(IfcGeometryBasisNoChangeError) as caught:
        await service.configure(
            scope,
            command,
            idempotency_key=repository.prepared.idempotency_key,
        )

    assert caught.value.operation_alias == "op_002"

    assert repository.commit_calls == 0
    assert repository.after is None
    assert repository.current == repository.prepared.before
    assert repository.failed is not None
    assert repository.failed[0] == IfcGeometryBasisNoChangeError.code
    assert repository.prepare_kwargs is not None
    assert repository.prepare_kwargs["operation_detail"] == {
        "detail_kind": "ifc_model_configure_geometry_basis",
        "request": {"coordinate_reference_change": {"mode": "local"}},
    }
    assert tuple(content for _, content in storage.objects.values()) == (base_content,)


@pytest.mark.asyncio
async def test_storage_failure_never_reaches_database_revision_commit() -> None:
    scope, _, repository, storage = _fixture()
    writer = _writer(repository, _FailingWriteStorage(storage))
    candidate_content = b"ISO-10303-21;unpublished-candidate"

    with pytest.raises(OSError, match="publication failure"):
        await writer.commit(
            scope,
            repository.prepared,
            _generated(candidate_content),
            claim=_claim(repository.prepared),
        )

    assert repository.commit_calls == 0
    assert repository.after is None
    assert repository.current == repository.prepared.before


@pytest.mark.asyncio
async def test_cancellation_during_storage_publication_never_reaches_commit() -> None:
    scope, _, repository, storage = _fixture()
    blocking_storage = _BlockingWriteStorage(storage)
    writer = _writer(repository, blocking_storage)
    invocation = asyncio.create_task(
        writer.commit(
            scope,
            repository.prepared,
            _generated(b"ISO-10303-21;cancelled-before-commit"),
            claim=_claim(repository.prepared),
        )
    )
    await blocking_storage.entered.wait()
    invocation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await invocation

    assert repository.commit_calls == 0
    assert repository.after is None
    assert repository.current == repository.prepared.before


@pytest.mark.asyncio
async def test_model_info_retry_keeps_the_first_pinned_exact_revision() -> None:
    scope, _, repository, storage = _fixture()
    codec = _FlakyReadCodec()
    reader = IfcRevisionReader(repository, storage, _Runtime(codec))
    runtime = _Runtime(codec)
    writer = IfcRevisionWriter(repository, storage, reader, runtime)
    service = IfcModelService(
        repository,
        storage,
        codec,
        writer,
        runtime,
        IfcWriteCoordinator(),
    )

    result = await service.info(scope)

    assert result.project.name == "Writer Test"
    assert codec.inspect_calls == 2
    assert repository.get_current_calls == 1
    assert result.source_exact is repository.current
    assert "source_exact" not in result.model_dump()


@pytest.mark.asyncio
async def test_context_list_retry_keeps_the_first_pinned_exact_revision() -> None:
    scope, _, repository, storage = _fixture()
    compiler = _FlakyContextCompiler()
    runtime = _Runtime(_Codec())
    runtime.context_compiler = compiler
    service = IfcGeometryBasisService(
        _writer(repository, storage, runtime=runtime),
        runtime,
        IfcWriteCoordinator(),
    )

    result = await service.list_representation_contexts(scope)

    assert result.contexts == ()
    assert compiler.inspect_context_calls == 2
    assert repository.get_current_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ("missing", "same_size_corruption"))
async def test_product_report_bytes_are_verified_before_immutable_candidate_selection(tmp_path, damage):
    scope, _, repository, _ = _fixture()
    storage = LocalStorage(tmp_path / "storage")
    prepared = repository.prepared
    report = await _product_report(storage, scope, prepared, committed=True)
    failure = await _product_report(storage, scope, prepared, committed=False)
    artifact = failure.artifacts[0]
    key = StorageKey(artifact.owner_id, artifact.task_id, artifact.artifact_id, 1)
    if damage == "missing":
        await storage.delete(key)
    else:
        payload = storage._logical_directory(key) / "payload"
        content = payload.read_bytes()
        payload.write_bytes(bytes((content[0] ^ 1,)) + content[1:])
    writer = _writer(repository, storage)
    with pytest.raises((FileNotFoundError, IfcModelIntegrityError, StorageConflictError)):
        await writer.commit(scope, prepared, _generated(b"ISO-10303-21;product-candidate"),
            claim=_claim(prepared), product_report=report, product_failure_report=failure)
    assert repository.selected is None
    assert repository.commit_calls == 0 and repository.current == prepared.before


@pytest.mark.asyncio
async def test_selected_product_reports_resume_original_storage_without_another_candidate(tmp_path):
    scope, _, repository, _ = _fixture()
    storage = LocalStorage(tmp_path / "storage")
    prepared = repository.prepared
    report = await _product_report(storage, scope, prepared, committed=True)
    failure = await _product_report(storage, scope, prepared, committed=False)
    runtime = _Runtime(_Codec())
    runtime.execute = AsyncMock(wraps=runtime.execute)
    writer = _writer(repository, storage, runtime=runtime)
    selected, _ = await writer._stage_and_select(scope, prepared, _claim(prepared),
        _generated(b"ISO-10303-21;selected-product-candidate"),
        product_report=report, product_failure_report=failure)
    original_upload = selected.upload_id
    resumed, recovered = await writer.resume_selected_revision(scope, prepared, _claim(prepared))
    assert recovered.upload_id == original_upload
    assert recovered.product_report == report and recovered.product_failure_report == failure
    assert resumed.exact.revision_id == prepared.after_revision_id
    assert repository.commit_calls == 1
    # A recovered candidate lacks the original live compiler result. It keeps
    # both the private stored inspection and committed independent readback.
    assert runtime.execute.await_count == 2


@pytest.mark.asyncio
async def test_failed_product_recovery_returns_the_original_report_without_runtime_execution():
    scope, _, repository, storage = _fixture()
    prepared = repository.prepared
    summary = ProductApplySummary(outcome="rejected", input_count=1, solve_executed_count=1,
        solved_count=0, interface_constraints_evaluated_product_count=0, build_executed_count=0, built_count=0,
        committed_count=0, not_executed_count=0)
    state = IfcRevisionOperationState(prepared=prepared, status=CriticalOperationStatus.FAILED,
        error_code="PRODUCT_INTERFACE_REJECTED", error_message="original rejection",
        error_phase=CriticalOperationErrorPhase.IFC_VALIDATION, operation_alias="op_002",
        product_summary=summary, report_package_id="data_package_original_failure")
    repository.get_revision_operation_state = AsyncMock(return_value=state)
    runtime = _Runtime(_Codec())
    runtime.execute = AsyncMock(side_effect=AssertionError("failed replay must not execute"))
    recovered = await _writer(repository, storage, runtime=runtime).recover_revision(scope,
        tool_name="ifc_products_apply", idempotency_key=prepared.idempotency_key,
        request_digest=prepared.request_digest, intent_summary="original intent",
        operation_detail={"detail_kind": "ifc_products_apply"})
    assert recovered is state
    runtime.execute.assert_not_called()
    assert repository.commit_calls == 0 and repository.selected is None
