from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from CAD2BIM_backend.application.artifact_service import ArtifactDownload
from CAD2BIM_backend.application.exact_payload_resolver import (
    ExactParquetPayload,
    ExactPayloadResolutionError,
    ExactPayloadResolver,
)
from CAD2BIM_backend.application.ifc_query_service import (
    IfcQueryApplicationError,
    IfcQueryInputBinding,
    IfcQueryService,
)
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.control_plane import ArtifactRecord, ExecutionMode
from CAD2BIM_backend.domain.ifc_model import (
    ExactIfcArtifactVersion,
    ExactIfcRevision,
    IfcModelState,
    IfcRevisionInspection,
    IfcRunIdentity,
)
from CAD2BIM_backend.domain.ifc_query import (
    IfcQueryAttemptRecord,
    IfcQueryAttemptStatus,
    IfcQueryPrepared,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.text_source import SourceEdit, TextSourceView
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.source_text.source_patch_adapter import TextSourcePatchAdapter
from CAD2BIM_backend.ports.ifc_query_worker import IfcQueryWorkerError, IfcQueryWorkerResult
from CAD2BIM_backend.ports.ifc_revision_reader import VerifiedExactIfcRevision
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.query.application import IfcQueryApplicationMixin
from CAD2BIM_mcp.direct.ifc.query.contracts import DirectIfcQueryRepairInput


pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
GLOBAL_ID = "0" * 22


def _scope() -> InvocationScope:
    return InvocationScope(
        identity=TenantExecutionContext(
            tenant_id="tenant-query",
            principal_id="principal-query",
            task_id="task-query",
            actor_kind=TenantActorKind.USER,
            command_id="command-query",
        ),
        owner_id="tenant-query",
        run_id="run-query",
        execution_mode=ExecutionMode.DIRECT_MCP,
        catalog_frontier=None,
    )


def _exact(revision_no: int) -> tuple[ExactIfcRevision, bytes]:
    content = f"ISO-10303-21;exact-{revision_no}".encode()
    digest = hashlib.sha256(content).hexdigest()
    ifc_scope = IfcRunIdentity(
        owner_id="tenant-query",
        tenant_id="tenant-query",
        task_id="task-query",
        run_id="run-query",
    )
    return (
        ExactIfcRevision(
            scope=ifc_scope,
            model_id="ifcmodel-query",
            model_state=IfcModelState.ACTIVE,
            revision_id=f"ifcrev-{revision_no}",
            revision_no=revision_no,
            write_epoch=revision_no,
            artifact=ExactIfcArtifactVersion(
                artifact_id=f"ifcart-{revision_no}",
                version_id=f"ifcav-{revision_no}",
                size_bytes=len(content),
                sha256=digest,
            ),
            project_global_id=GLOBAL_ID,
        ),
        content,
    )


def _inspection(exact: ExactIfcRevision) -> IfcRevisionInspection:
    return IfcRevisionInspection(
        schema_identifier="IFC4X3_ADD2",
        project_global_id=GLOBAL_ID,
        project_name="Query test",
        size_bytes=exact.artifact.size_bytes,
        sha256=exact.artifact.sha256,
        site_count=0,
        element_count=0,
        unit_assignment_count=0,
        geometric_context_count=0,
        product_body_contexts=(),
        project_length_unit_m=None,
        geometry_basis_configured=False,
    )


def _cursor_codec() -> CursorCodec:
    key = base64.urlsafe_b64encode(b"q" * 32).rstrip(b"=").decode("ascii")
    return CursorCodec(key)


class _Artifacts:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.by_key: dict[str, tuple[ArtifactRecord, bytes]] = {}
        self.by_id: dict[str, tuple[ArtifactRecord, bytes]] = {}

    async def upload_bytes(self, **values) -> ArtifactRecord:
        key = values["idempotency_key"]
        content = values["content"]
        label = "source" if values["kind"] == "ifc_query_source" else "result"
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
        assert record.owner_id == owner_id and record.version_id == version_id
        assert record.run_id == run_id
        return ArtifactDownload(record, content)


class _Repository:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.records: dict[str, IfcQueryAttemptRecord] = {}
        self.request_ids: dict[str, str] = {}
        self.lose_next_terminal_response = False
        self.fail_next_terminal_before_commit = False
        self.list_calls = []

    async def prepare_ifc_query_attempt(self, scope, preparation) -> IfcQueryPrepared:
        self.events.append("attempt:prepare")
        existing_id = self.request_ids.get(preparation.request_identity)
        if existing_id is not None:
            return IfcQueryPrepared(attempt=self.records[existing_id], created=False)
        record = IfcQueryAttemptRecord(
            owner_id="tenant-query",
            tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id,
            status=IfcQueryAttemptStatus.PREPARED,
            **preparation.model_dump(),
        )
        self.records[record.attempt_id] = record
        self.request_ids[record.request_identity] = record.attempt_id
        return IfcQueryPrepared(attempt=record, created=True)

    async def get_ifc_query_attempt(self, scope, *, attempt_id: str):
        record = self.records.get(attempt_id)
        if (
            record is None
            or record.task_id != scope.identity.task_id
            or record.run_id != scope.run_id
        ):
            return None
        return record

    async def commit_ifc_query_attempt_terminal(self, scope, *, attempt_id: str, terminal):
        self.events.append(f"attempt:{terminal.status.value.lower()}")
        if self.fail_next_terminal_before_commit:
            self.fail_next_terminal_before_commit = False
            raise RuntimeError("simulated unknown terminal state")
        current = self.records[attempt_id]
        completed = current.model_copy(
            update={
                "status": terminal.status,
                "result_artifact": terminal.result_artifact,
                "diagnostics": terminal.diagnostics,
                "finished_at": terminal.finished_at,
            }
        )
        completed = IfcQueryAttemptRecord.model_validate(completed)
        self.records[attempt_id] = completed
        if self.lose_next_terminal_response:
            self.lose_next_terminal_response = False
            raise RuntimeError("simulated response loss")
        return completed

    async def list_ifc_query_inputs(self, context, *, attempt_id: str):
        return self.records[attempt_id].inputs

    async def list_terminal_ifc_query_attempts(
        self,
        scope,
        *,
        statuses,
        before_created_at,
        before_attempt_id,
        limit,
    ):
        self.list_calls.append((scope, statuses, before_created_at, before_attempt_id, limit))
        records = [
            record
            for record in self.records.values()
            if record.task_id == scope.identity.task_id
            and record.run_id == scope.run_id
            and record.status in statuses
        ]
        if before_created_at is not None:
            records = [
                record
                for record in records
                if (record.created_at, record.attempt_id) < (before_created_at, before_attempt_id)
            ]
        records.sort(key=lambda record: (record.created_at, record.attempt_id), reverse=True)
        return tuple(records[:limit])


class _Revisions:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        first, first_bytes = _exact(1)
        second, second_bytes = _exact(2)
        self.current = first
        self.values = {
            first.revision_id: (first, first_bytes),
            second.revision_id: (second, second_bytes),
        }

    async def pin_current(self, scope: IfcRunIdentity) -> ExactIfcRevision:
        self.events.append(f"revision:pin-current:{self.current.revision_id}")
        assert scope == self.current.scope
        return self.current

    async def pin_exact(self, scope: IfcRunIdentity, *, revision_id: str) -> ExactIfcRevision:
        self.events.append(f"revision:pin-exact:{revision_id}")
        exact = self.values[revision_id][0]
        assert scope == exact.scope
        return exact

    async def read_exact(self, exact: ExactIfcRevision) -> VerifiedExactIfcRevision:
        self.events.append(f"revision:read:{exact.revision_id}")
        content = self.values[exact.revision_id][1]
        return VerifiedExactIfcRevision(exact=exact, content=content, inspection=_inspection(exact))


class _Payloads:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.alias_resolutions = 0
        self.exact_resolutions: list[tuple[str, str, str | None]] = []
        self.member = SimpleNamespace(member_id="member-facts", row_count=2)
        self.payload = ExactParquetPayload(
            payload_name="facts",
            logical_key_fields=("id",),
            member=self.member,
        )

    async def package_by_alias(self, scope, package_alias: str):
        self.alias_resolutions += 1
        assert package_alias == "data_001"
        return SimpleNamespace(package=SimpleNamespace(package_id="package-facts"))

    def transform_payloads(self, package):
        return {"facts": self.payload}

    async def payload_by_exact_member(
        self,
        scope,
        *,
        package_id: str,
        payload_member_id: str,
        schema_member_id: str | None = None,
    ):
        self.exact_resolutions.append((package_id, payload_member_id, schema_member_id))
        assert (package_id, payload_member_id, schema_member_id) == (
            "package-facts",
            "member-facts",
            None,
        )
        return SimpleNamespace(package=SimpleNamespace(package_id=package_id)), self.payload

    async def materialize_members(self, members, **_kwargs):
        if not members:
            return {}
        path = self.tmp_path / f"payload-{len(self.exact_resolutions)}.parquet"
        pq.write_table(pa.table({"id": [1, 2]}), path)
        return {"member-facts": path}

    @staticmethod
    def remove_paths(paths) -> None:
        for path in paths:
            path.unlink(missing_ok=True)


class _Worker:
    def execution_scope(self, policy, *, deadline=None):
        from contextlib import nullcontext
        return nullcontext()

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.requests = []
        self.inputs = []
        self.failure: IfcQueryWorkerError | None = None
        self.result = [{"n": 1}, {"n": 2}, {"n": 3}]

    async def execute_query(self, request, inputs=(), *, deadline=None):
        self.events.append("worker:execute")
        self.requests.append(request)
        self.inputs.append(tuple((item.input_name, item.reader.read_all()) for item in inputs))
        if self.failure is not None:
            raise self.failure
        result_json = canonical_json(self.result)
        return IfcQueryWorkerResult(
            result=self.result,
            result_json=result_json,
            result_bytes=len(result_json.encode()),
            ifc_sha256=hashlib.sha256(request.ifc_bytes).hexdigest(),
        )

    async def close(self) -> None:
        return None


def _service(tmp_path: Path, *, now: list[datetime] | None = None):
    events: list[str] = []
    repository = _Repository(events)
    artifacts = _Artifacts(events)
    payloads = _Payloads(tmp_path)
    revisions = _Revisions(events)
    worker = _Worker(events)
    current_time = now or [NOW]
    service = IfcQueryService(
        repository,
        artifacts,
        payloads,
        revisions,
        worker,
        _cursor_codec(),
        TextSourcePatchAdapter(),
        clock=lambda: current_time[0],
    )
    return service, repository, artifacts, payloads, revisions, worker, events


async def test_repeated_cancellation_settles_attempt(tmp_path: Path, monkeypatch) -> None:
    service, repository, _artifacts, _payloads, _revisions, worker, events = _service(tmp_path)
    running, settling, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_commit = repository.commit_ifc_query_attempt_terminal

    async def execute(*args, **kwargs):
        running.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError as exc:
            exc.worker_failure = SimpleNamespace(execution_started=True)
            raise

    async def commit(*args, **kwargs):
        settling.set()
        await finish.wait()
        return await original_commit(*args, **kwargs)

    monkeypatch.setattr(worker, "execute_query", execute)
    monkeypatch.setattr(repository, "commit_ifc_query_attempt_terminal", commit)
    task = asyncio.create_task(service.fresh(
        _scope(), "def query(ctx):\n    return []\n", "cancel-query",
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
    attempt = next(iter(repository.records.values()))
    assert attempt.status is IfcQueryAttemptStatus.FAILED
    assert attempt.diagnostics[0].code == "IFC_QUERY_CANCELLED"
    assert attempt.diagnostics[0].execution_started is True


async def test_fresh_pins_current_before_source_and_pages_persisted_result(tmp_path: Path) -> None:
    service, repository, _artifacts, payloads, revisions, worker, events = _service(tmp_path)
    page = await service.fresh(
        _scope(),
        "def query(ctx):\n    return []\n",
        "request-fresh",
        inputs=(
            IfcQueryInputBinding(
                input_name="facts", package_alias="data_001", payload_name="facts"
            ),
        ),
        page_size=2,
    )

    attempt = next(iter(repository.records.values()))
    assert attempt.ifc_revision_id == "ifcrev-1"
    assert attempt.inputs[0].payload_member_id == "member-facts"
    assert events.index("revision:pin-current:ifcrev-1") < events.index("artifact:source")
    assert events.index("artifact:source") < events.index("worker:execute")
    assert payloads.alias_resolutions == 1
    assert payloads.exact_resolutions == [("package-facts", "member-facts", None)]
    assert worker.requests[0].ifc_bytes.endswith(b"exact-1")
    assert worker.inputs[0][0][0] == "facts"
    assert page.returned_count == 2 and page.next_cursor is not None

    revisions.current = revisions.values["ifcrev-2"][0]
    replay = await service.fresh(
        _scope(),
        "def query(ctx):\n    return []\n",
        "request-fresh",
        inputs=(
            IfcQueryInputBinding(
                input_name="facts", package_alias="data_001", payload_name="facts"
            ),
        ),
        page_size=2,
    )
    assert replay == page
    assert payloads.alias_resolutions == 1
    assert len(worker.requests) == 1
    assert len([event for event in events if event.startswith("revision:pin-current")]) == 1
    with pytest.raises(IfcQueryApplicationError) as changed:
        await service.fresh(
            _scope(), "def query(ctx):\n    return [1]\n", "request-fresh",
            inputs=(
                IfcQueryInputBinding(
                    input_name="facts", package_alias="data_001", payload_name="facts"
                ),
            ),
        )
    assert changed.value.code == "IFC_QUERY_IDEMPOTENCY_CONFLICT"
    assert len([event for event in events if event.startswith("revision:pin-current")]) == 1
    second = await service.fresh(
        _scope(), "def query(ctx):\n    return []\n", "request-new",
        inputs=(
            IfcQueryInputBinding(
                input_name="facts", package_alias="data_001", payload_name="facts"
            ),
        ),
        page_size=2,
    )
    assert second.attempt_alias != page.attempt_alias
    assert len(repository.records) == 2
    assert len(worker.requests) == 2
    assert worker.requests[-1].ifc_bytes.endswith(b"exact-2")
    assert len([event for event in events if event.startswith("revision:pin-current")]) == 2
    continuation = await service.continue_page(_scope(), page.next_cursor)
    assert continuation.results == ({"n": 3},)
    assert len(worker.requests) == 2


@pytest.mark.parametrize("execution_started", [False, True])
async def test_exact_inspection_runtime_failure_persists_admission_diagnostic(
    tmp_path: Path, monkeypatch, execution_started: bool,
) -> None:
    from CAD2BIM_backend.ports.ifc_revision_runtime import IfcRevisionRuntimeError

    service, repository, _artifacts, _payloads, revisions, worker, _events = _service(tmp_path)
    diagnostic = {
        "reason": "queue_timeout", "execution_started": execution_started,
        "current_stage_started": False, "resource_pool": "python",
    }

    async def fail_inspection(exact):
        raise IfcRevisionRuntimeError(
            "IFC_REVISION_SERVICE_UNAVAILABLE", "queue expired", phase="runtime",
            execution_started=execution_started, retryable=True, diagnostic=diagnostic,
        )

    monkeypatch.setattr(revisions, "read_exact", fail_inspection)
    with pytest.raises(IfcQueryApplicationError) as caught:
        await service.fresh(_scope(), "def query(ctx):\n    return []\n", "inspection-capacity")
    assert caught.value.code == "IFC_QUERY_RUNTIME_UNAVAILABLE"
    assert caught.value.diagnostic == diagnostic
    attempt = next(iter(repository.records.values()))
    assert attempt.status is IfcQueryAttemptStatus.FAILED
    assert attempt.diagnostics[0].execution_started is execution_started
    assert attempt.diagnostics[0].detail == diagnostic
    assert worker.requests == []


async def test_result_validation_diagnostic_persists_and_replays_without_worker_rerun(tmp_path: Path) -> None:
    service, repository, _artifacts, _payloads, _revisions, worker, _events = _service(tmp_path)
    diagnostic = {"json_path": '$["units"][0]["Units"]', "value_type": "tuple", "reason": "unsupported_type"}
    worker.failure = IfcQueryWorkerError(
        "IFC_QUERY_RESULT_INVALID", "query(ctx) must return a canonical JSON value",
        phase="result_validation", execution_started=True, diagnostic=diagnostic,
    )
    for _ in range(2):
        with pytest.raises(IfcQueryApplicationError) as caught:
            await service.fresh(_scope(), "def query(ctx):\n    return {\"units\": (1, 2)}\n", "invalid-result")
        assert caught.value.diagnostic == diagnostic
        assert caught.value.phase == "output_validation"
    attempt_id = await service.resolve_attempt_alias(_scope(), caught.value.attempt_alias)
    assert repository.records[attempt_id].diagnostics[0].detail == diagnostic
    assert len(worker.requests) == 1


async def test_failure_keeps_exact_source_and_repair_reuses_parent_revision(tmp_path: Path) -> None:
    service, repository, _artifacts, _payloads, revisions, worker, events = _service(tmp_path)
    worker.failure = IfcQueryWorkerError(
        "IFC_QUERY_EXECUTION_FAILED",
        "name is not defined",
        phase="execution",
        execution_started=True,
        diagnostic={
            "exception_type": "NameError",
            "line": 2,
            "source_line": "    return missing",
            "host_path": "/Users/private/query.py",
        },
    )
    source = "def query(ctx):\n    return missing\n"
    with pytest.raises(IfcQueryApplicationError) as failed:
        await service.fresh(_scope(), source, "request-failed")
    assert failed.value.attempt_alias is not None
    parent_id = await service.resolve_attempt_alias(_scope(), failed.value.attempt_alias)
    parent = repository.records[parent_id]
    assert parent.status is IfcQueryAttemptStatus.FAILED
    assert parent.diagnostics[0].execution_started is True
    assert parent.diagnostics[0].detail == {
        "exception_type": "NameError",
        "line": 2,
        "source_line": "    return missing",
    }
    assert failed.value.diagnostic == parent.diagnostics[0].detail
    with pytest.raises(IfcQueryApplicationError) as replayed:
        await service.fresh(_scope(), source, "request-failed")
    assert replayed.value.diagnostic == parent.diagnostics[0].detail
    assert len(worker.requests) == 1
    source_view = await service.read_source(_scope(), parent_id, TextSourceView(view_mode="full"))
    assert source_view.text == source

    revisions.current = revisions.values["ifcrev-2"][0]
    worker.failure = None
    worker.result = {"ok": True}
    repaired = await service.repair(
        _scope(),
        parent_id,
        (SourceEdit(old_text="missing", new_text="{'ok': True}"),),
        "request-repair",
    )
    child_id = await service.resolve_attempt_alias(_scope(), repaired.attempt_alias)
    child = repository.records[child_id]
    assert child.repair_parent_attempt_id == parent_id
    assert child.ifc_revision_id == "ifcrev-1"
    assert worker.requests[-1].ifc_bytes.endswith(b"exact-1")
    pinned_before = len([event for event in events if event.startswith("revision:pin-exact")])
    replayed_repair = await service.repair(
        _scope(), parent_id,
        (SourceEdit(old_text="missing", new_text="{'ok': True}"),),
        "request-repair",
    )
    assert replayed_repair == repaired
    assert len(worker.requests) == 2
    assert len([event for event in events if event.startswith("revision:pin-exact")]) == pinned_before
    with pytest.raises(IfcQueryApplicationError) as changed_repair:
        await service.repair(
            _scope(), parent_id,
            (SourceEdit(old_text="missing", new_text="None"),),
            "request-repair",
        )
    assert changed_repair.value.code == "IFC_QUERY_IDEMPOTENCY_CONFLICT"
    assert len([event for event in events if event.startswith("revision:pin-exact")]) == pinned_before

    count = len(repository.records)
    with pytest.raises(IfcQueryApplicationError) as invalid_edit:
        await service.repair(
            _scope(),
            parent_id,
            (SourceEdit(old_text="absent literal", new_text="None"),),
            "request-invalid-edit",
        )
    assert invalid_edit.value.code == "IFC_QUERY_REPAIR_INVALID"
    assert len(repository.records) == count


async def test_repair_alias_replays_original_before_parent_alias_resolution(
    tmp_path: Path, monkeypatch,
) -> None:
    service, repository, _artifacts, _payloads, revisions, worker, _events = _service(tmp_path)
    worker.failure = IfcQueryWorkerError(
        "IFC_QUERY_EXECUTION_FAILED", "missing value", phase="execution",
        execution_started=True,
    )
    with pytest.raises(IfcQueryApplicationError) as failed:
        await service.fresh(_scope(), "def query(ctx):\n    return missing\n", "alias-parent")
    parent_alias = failed.value.attempt_alias
    assert parent_alias is not None
    parent_id = await service.resolve_attempt_alias(_scope(), parent_alias)
    source = await service.read_source(_scope(), parent_id, TextSourceView(view_mode="full"))
    revisions.current = revisions.values["ifcrev-2"][0]
    worker.failure = None
    worker.result = {"ok": True}
    edits = (SourceEdit(old_text="missing", new_text="{'ok': True}"),)
    first = await service.repair_from_alias(
        _scope(), parent_alias,  edits, "alias-repair-1",
    )
    assert repository.records[
        await service.resolve_attempt_alias(_scope(), first.attempt_alias)
    ].ifc_revision_id == "ifcrev-1"
    requests_before = len(worker.requests)

    async def unavailable(_scope_arg, _alias):
        raise AssertionError("same K must not resolve its parent alias")

    monkeypatch.setattr(service, "resolve_attempt_alias", unavailable)
    assert await service.repair_from_alias(
        _scope(), parent_alias,  edits, "alias-repair-1",
    ) == first

    class _DirectProbe(IfcQueryApplicationMixin):
        _ifc_query_service = service

        async def _run_runtime_read(self, _alias, _action_name, operation, **_options):
            return await operation(_scope())

    direct = _DirectProbe()
    direct_request = DirectIfcQueryRepairInput(
        direct_session_alias="direct_001", mode="repair",
        parent_attempt_alias=parent_alias, edits=({"old_text": "missing", "new_text": "{'ok': True}"},),
    )
    direct_replayed = await direct.query_ifc_python(
        direct_request, request_identity="alias-repair-1",
    )
    assert direct_replayed.attempt_alias == first.attempt_alias
    assert len(worker.requests) == requests_before
    changed_direct = DirectIfcQueryRepairInput.model_validate({
        **direct_request.model_dump(mode="json"),
        "edits": [{"old_text": "missing", "new_text": "{'ok': False}"}],
    })
    with pytest.raises(DirectApplicationError) as direct_conflict:
        await direct.query_ifc_python(changed_direct, request_identity="alias-repair-1")
    assert direct_conflict.value.code == "IFC_QUERY_IDEMPOTENCY_CONFLICT"
    with pytest.raises(IfcQueryApplicationError) as conflict:
        await service.repair_from_alias(
            _scope(), "retired-parent-alias",  edits, "alias-repair-1",
        )
    assert conflict.value.code == "IFC_QUERY_IDEMPOTENCY_CONFLICT"
    with pytest.raises(AssertionError, match="same K"):
        await service.repair_from_alias(
            _scope(), parent_alias,  edits, "alias-repair-2",
        )


async def test_terminal_response_loss_reconciles_without_rerunning(tmp_path: Path) -> None:
    service, repository, _artifacts, _payloads, _revisions, worker, _events = _service(tmp_path)
    repository.lose_next_terminal_response = True
    first = await service.fresh(
        _scope(), "def query(ctx):\n    return []\n", "request-response-loss"
    )
    replay = await service.fresh(
        _scope(), "def query(ctx):\n    return []\n", "request-response-loss"
    )
    assert first == replay
    assert len(worker.requests) == 1


async def test_concurrent_replay_of_prepared_attempt_does_not_execute_twice(
    tmp_path: Path, monkeypatch,
) -> None:
    service, repository, _artifacts, _payloads, revisions, worker, events = _service(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()
    original_execute = worker.execute_query
    executions = 0

    async def execute(*args, **kwargs):
        nonlocal executions
        executions += 1
        started.set()
        await finish.wait()
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(worker, "execute_query", execute)
    source = "def query(ctx):\n    return []\n"
    first = asyncio.create_task(service.fresh(_scope(), source, "concurrent-query"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        revisions.current = revisions.values["ifcrev-2"][0]
        with pytest.raises(IfcQueryApplicationError) as pending:
            await service.fresh(_scope(), source, "concurrent-query")
        assert pending.value.code == "IFC_QUERY_ATTEMPT_IN_PROGRESS"
        assert pending.value.retryable is True
        assert len(repository.records) == 1
        assert executions == 1
        assert len([event for event in events if event.startswith("revision:pin-current")]) == 1
    finally:
        finish.set()
    page = await first
    assert (await service.fresh(_scope(), source, "concurrent-query")) == page
    assert executions == 1


async def test_terminal_attempt_list_is_run_bound_keyset_and_reuses_alias(
    tmp_path: Path,
) -> None:
    service, repository, _artifacts, _payloads, _revisions, worker, _events = _service(tmp_path)
    await service.fresh(_scope(), "def query(ctx):\n    return []\n", "request-list-success")
    worker.failure = IfcQueryWorkerError(
        "IFC_QUERY_EXECUTION_FAILED",
        "name is not defined",
        phase="execution",
        execution_started=True,
    )
    with pytest.raises(IfcQueryApplicationError):
        await service.fresh(
            _scope(), "def query(ctx):\n    return missing\n", "request-list-failure"
        )

    statuses = (IfcQueryAttemptStatus.SUCCEEDED, IfcQueryAttemptStatus.FAILED)
    first = await service.list_terminal_ifc_query_attempts(
        _scope(),
        statuses=statuses,
        before_created_at=None,
        before_attempt_id=None,
        limit=1,
    )
    second = await service.list_terminal_ifc_query_attempts(
        _scope(),
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
    assert repository.list_calls[0][0] == _scope()
    alias = service.attempt_alias(_scope(), first[0])
    assert await service.resolve_attempt_alias(_scope(), alias) == first[0].attempt_id
    assert (
        await service.attempt_alias_for_visible_id(_scope(), first[0].attempt_id)
        == alias
    )
    with pytest.raises(IfcQueryApplicationError) as hidden_parent:
        await service.attempt_alias_for_visible_id(
            _scope().model_copy(update={"run_id": "other-run"}), first[0].attempt_id
        )
    assert hidden_parent.value.code == "IFC_QUERY_ATTEMPT_NOT_FOUND"
    assert (
        await service.list_terminal_ifc_query_attempts(
            _scope().model_copy(update={"run_id": "other-run"}),
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
        ((IfcQueryAttemptStatus.PREPARED,), None, None, 1),
        ((IfcQueryAttemptStatus.SUCCEEDED,), NOW, None, 1),
        ((IfcQueryAttemptStatus.SUCCEEDED,), None, "ifc_query_boundary", 1),
        ((IfcQueryAttemptStatus.SUCCEEDED,), None, None, 202),
    ],
)
async def test_terminal_attempt_list_rejects_invalid_internal_query(
    tmp_path: Path,
    statuses,
    before_created_at,
    before_attempt_id,
    limit,
) -> None:
    service, repository, *_rest = _service(tmp_path)

    with pytest.raises(IfcQueryApplicationError) as caught:
        await service.list_terminal_ifc_query_attempts(
            _scope(),
            statuses=statuses,
            before_created_at=before_created_at,
            before_attempt_id=before_attempt_id,
            limit=limit,
        )

    assert caught.value.code == "INVALID_ARGUMENT"
    assert repository.list_calls == []


async def test_unknown_terminal_state_remains_prepared_for_reconciliation(tmp_path: Path) -> None:
    service, repository, _artifacts, _payloads, revisions, worker, events = _service(tmp_path)
    repository.fail_next_terminal_before_commit = True
    source = "def query(ctx):\n    return []\n"
    with pytest.raises(IfcQueryApplicationError) as uncertain:
        await service.fresh(_scope(), source, "request-uncertain-terminal")
    assert uncertain.value.code == "IFC_QUERY_RECONCILIATION_REQUIRED"
    assert uncertain.value.retryable is True
    assert next(iter(repository.records.values())).status is IfcQueryAttemptStatus.PREPARED
    assert len(worker.requests) == 1
    revisions.current = revisions.values["ifcrev-2"][0]
    with pytest.raises(IfcQueryApplicationError) as pending:
        await service.fresh(_scope(), source, "request-uncertain-terminal")
    assert pending.value.code == "IFC_QUERY_ATTEMPT_IN_PROGRESS"
    assert pending.value.retryable is True
    assert len(worker.requests) == 1
    assert len([event for event in events if event.startswith("revision:pin-current")]) == 1


async def test_expired_cursor_never_reruns_worker(tmp_path: Path) -> None:
    now = [NOW]
    service, _repository, _artifacts, _payloads, _revisions, worker, _events = _service(
        tmp_path, now=now
    )
    page = await service.fresh(
        _scope(), "def query(ctx):\n    return []\n", "request-expiry", page_size=1
    )
    assert page.next_cursor is not None
    now[0] += timedelta(minutes=16)
    with pytest.raises(IfcQueryApplicationError) as expired:
        await service.continue_page(_scope(), page.next_cursor)
    assert expired.value.code == "IFC_QUERY_CURSOR_EXPIRED"
    assert len(worker.requests) == 1


async def test_page_obeys_record_and_utf8_byte_budgets(tmp_path: Path) -> None:
    service, _repository, _artifacts, _payloads, _revisions, worker, _events = _service(tmp_path)
    worker.result = [{"ordinal": ordinal, "text": "x" * 40_000} for ordinal in range(3)]
    first = await service.fresh(
        _scope(), "def query(ctx):\n    return []\n", "request-byte-page", page_size=200
    )
    assert first.returned_count == 1
    assert first.total_count == 3
    assert len(canonical_json(list(first.results)).encode("utf-8")) <= 65_536
    assert first.next_cursor is not None
    second = await service.continue_page(_scope(), first.next_cursor)
    assert second.returned_count == 1
    assert len(worker.requests) == 1


async def test_exact_payload_reopen_uses_package_id_and_exact_member_pair() -> None:
    package = SimpleNamespace(package=SimpleNamespace(package_id="package-facts"))
    payload = ExactParquetPayload(
        payload_name="facts",
        logical_key_fields=("id",),
        member=SimpleNamespace(member_id="member-facts", row_count=2),
    )

    class Repository:
        def __init__(self) -> None:
            self.calls = []

        async def get_package(self, scope, package_id: str):
            self.calls.append((scope.run_id, package_id))
            return package if package_id == "package-facts" else None

    class Resolver(ExactPayloadResolver):
        def transform_payloads(self, published):
            assert published is package
            return {"facts": payload}

    repository = Repository()
    resolver = Resolver(repository, SimpleNamespace())
    reopened_package, reopened_payload = await resolver.payload_by_exact_member(
        _scope(),
        package_id="package-facts",
        payload_member_id="member-facts",
    )
    assert reopened_package is package and reopened_payload is payload
    assert repository.calls == [("run-query", "package-facts")]

    with pytest.raises(ExactPayloadResolutionError, match="schema binding"):
        await resolver.payload_by_exact_member(
            _scope(),
            package_id="package-facts",
            payload_member_id="member-facts",
            schema_member_id="unexpected-schema",
        )
