from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from CAD2BIM_backend.application.ifc_project_service import IfcProjectService
from CAD2BIM_backend.application.ifc_write_coordinator import IfcWriteCoordinator
from CAD2BIM_backend.application.ifc_revision_writer import VerifiedCommittedIfcRevision
from CAD2BIM_backend.domain.ifc_model import (
    ExactIfcArtifactVersion,
    ExactIfcRevision,
    IfcExecutionClaim,
    IfcModelState,
    IfcRunIdentity,
    PreparedIfcRevision,
)
from CAD2BIM_backend.domain.ifc_project import (
    IfcProjectSummary,
    IfcProjectNoChangeError,
    IfcProjectPatch,
    UpdateIfcProjectCommand,
)
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.ifc.project import IfcOpenShellProjectCompiler
from tests.process_support.ifc_revision_runtime import (
    BytesIfcCandidate,
    InProcessIfcRevisionRuntime,
)


_PROJECT_GLOBAL_ID = "1xvJu1wbjMywJMjRBpLw$1"
_NOW = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


def _scope() -> IfcRunIdentity:
    return IfcRunIdentity(
        owner_id="owner_001",
        tenant_id="tenant_001",
        task_id="task_001",
        run_id="run_001",
    )


def _exact(content: bytes, codec: IfcOpenShellModelCodec) -> ExactIfcRevision:
    inspection = codec.inspect(content)
    return ExactIfcRevision(
        scope=_scope(),
        model_id="ifcm_001",
        model_state=IfcModelState.ACTIVE,
        revision_id="ifcr_001",
        revision_no=1,
        write_epoch=1,
        artifact=ExactIfcArtifactVersion(
            artifact_id="art_ifcr_001",
            version_id="artv_ifcr_001",
            size_bytes=inspection.size_bytes,
            sha256=inspection.sha256,
        ),
        project_global_id=_PROJECT_GLOBAL_ID,
    )


class _CompilerSpy:
    def __init__(self, codec: IfcOpenShellModelCodec) -> None:
        self.delegate = IfcOpenShellProjectCompiler(codec)
        self.apply_count = 0
        self.inspect_count = 0

    def apply(self, base_content: bytes, command: UpdateIfcProjectCommand) -> Any:
        self.apply_count += 1
        return self.delegate.apply(base_content, command)

    def inspect_project(self, content: bytes) -> Any:
        self.inspect_count += 1
        return self.delegate.inspect_project(content)


class _Writer:
    def __init__(
        self,
        content: bytes,
        codec: IfcOpenShellModelCodec,
    ) -> None:
        self.content = content
        self.codec = codec
        self.before = _exact(content, codec)
        self.prepare_count = 0
        self.base_read_count = 0
        self.commit_count = 0
        self.exact_reread_count = 0
        self.fail_count = 0
        self.freeze_count = 0
        self.prepare_requests: list[dict[str, object]] = []
        self.failed_error_code: str | None = None
        self.recovered = None

    async def prepare(self, scope: IfcRunIdentity, **kwargs: object) -> PreparedIfcRevision:
        assert scope == _scope()
        self.prepare_count += 1
        self.prepare_requests.append(dict(kwargs))
        return PreparedIfcRevision(
            operation_id="ifcop_001",
            idempotency_key=str(kwargs["idempotency_key"]),
            request_digest=str(kwargs["request_digest"]),
            before=self.before,
            after_revision_id="ifcr_002",
            artifact_id="art_ifcr_002",
            artifact_version_id="artv_ifcr_002",
            prepared_at=_NOW,
        )

    async def recover_revision(self, scope: IfcRunIdentity, **kwargs: object):
        assert scope == _scope()
        return self.recovered

    async def claim_execution(
        self, scope: IfcRunIdentity, prepared: PreparedIfcRevision,
    ) -> IfcExecutionClaim:
        assert scope == _scope()
        return IfcExecutionClaim(
            operation_id=prepared.operation_id, request_digest=prepared.request_digest,
            owner="ifc:test:1:1:nonce", generation=1, token="test-token",
            deadline_at=_NOW + timedelta(hours=1), remaining_seconds=3600,
        )

    def guard_registration(self, scope: IfcRunIdentity, claim: IfcExecutionClaim):
        async def register(_pid: int, _birth_us: int) -> None:
            pass
        return register

    async def retire_execution(self, scope: IfcRunIdentity, claim: IfcExecutionClaim) -> None:
        assert scope == _scope()

    async def resume_selected_revision(
        self, scope: IfcRunIdentity, prepared: PreparedIfcRevision,
        claim: IfcExecutionClaim,
    ):
        assert scope == _scope() and claim.operation_id == prepared.operation_id
        return None

    async def open_prepared_base(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
    ) -> BytesIfcCandidate:
        assert scope == _scope()
        assert prepared.before == self.before
        self.base_read_count += 1
        inspection = self.codec.inspect(self.content)
        return BytesIfcCandidate(self.content, sha256=inspection.sha256)

    async def commit(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
        candidate: Any,
        **kwargs: object,
    ) -> VerifiedCommittedIfcRevision:
        assert scope == _scope()
        self.commit_count += 1
        # IfcRevisionWriter.commit owns the exact post-save reread.  This stub
        # records that single boundary operation and returns its verified bytes.
        self.exact_reread_count += 1
        inspection = self.codec.inspect(candidate.candidate.content)
        exact = ExactIfcRevision(
            scope=scope,
            model_id=self.before.model_id,
            model_state=IfcModelState.ACTIVE,
            revision_id=prepared.after_revision_id,
            revision_no=2,
            write_epoch=2,
            artifact=ExactIfcArtifactVersion(
                artifact_id=prepared.artifact_id,
                version_id=prepared.artifact_version_id,
                size_bytes=inspection.size_bytes,
                sha256=inspection.sha256,
            ),
            project_global_id=_PROJECT_GLOBAL_ID,
        )
        return VerifiedCommittedIfcRevision(
            operation_alias="op_002",
            exact=exact,
            inspection=inspection,
        )

    async def fail_prepared(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
        **kwargs: object,
    ) -> str:
        assert scope == _scope()
        assert prepared.operation_id == "ifcop_001"
        self.fail_count += 1
        self.failed_error_code = str(kwargs["error_code"])
        return "op_002"

    async def freeze_indeterminate(self, *_: object, **__: object) -> None:
        self.freeze_count += 1


class _BlockingWriter(_Writer):
    def __init__(self, content: bytes, codec: IfcOpenShellModelCodec) -> None:
        super().__init__(content, codec)
        self.commit_entered = asyncio.Event()
        self.release_commit = asyncio.Event()
        self.candidate: BytesIfcCandidate | None = None

    async def commit(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
        candidate: Any,
        **kwargs: object,
    ) -> VerifiedCommittedIfcRevision:
        self.candidate = candidate.candidate
        self.commit_entered.set()
        await self.release_commit.wait()
        return await super().commit(scope, prepared, candidate, **kwargs)


def _service() -> tuple[IfcProjectService, _Writer, _CompilerSpy]:
    codec = IfcOpenShellModelCodec()
    content = codec.create_revision1(
        project_global_id=_PROJECT_GLOBAL_ID,
        project_name="Project Service Test",
        header_timestamp=_NOW,
    ).content
    writer = _Writer(content, codec)
    compiler = _CompilerSpy(codec)
    runtime = InProcessIfcRevisionRuntime(codec)
    runtime.project = compiler  # type: ignore[assignment]
    return (
        IfcProjectService(writer, runtime, IfcWriteCoordinator()),
        writer,
        compiler,
    )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_success_replay_returns_original_project_without_compilation() -> None:
    service, writer, compiler = _service()
    original = IfcProjectSummary(
        global_id=_PROJECT_GLOBAL_ID, name="Original committed name",
        description="Original description",
    )
    writer.recovered = SimpleNamespace(project_result=original, operation_alias="op_002")

    result = await service.update(
        _scope(), UpdateIfcProjectCommand(patch=IfcProjectPatch(name="Updated")),
        idempotency_key="original_project_action",
    )

    assert result.project == original
    assert result.operation_alias == "op_002"
    assert writer.prepare_count == writer.commit_count == 0
    assert compiler.apply_count == 0


@pytest.mark.asyncio
async def test_success_prepares_compiles_commits_and_exactly_rereads_once() -> None:
    service, writer, compiler = _service()
    command = UpdateIfcProjectCommand(
        patch=IfcProjectPatch(name="Updated Project", phase="Construction")
    )

    result = await service.update(
        _scope(),
        command,
        idempotency_key="server_request_001",
    )

    assert writer.prepare_count == 1
    assert writer.base_read_count == 1
    assert compiler.apply_count == 1
    assert writer.commit_count == 1
    assert writer.exact_reread_count == 1
    assert compiler.inspect_count == 1
    assert writer.fail_count == writer.freeze_count == 0
    assert result.operation_alias == "op_002"
    assert result.project.name == "Updated Project"
    assert result.project.phase == "Construction"


@pytest.mark.asyncio
async def test_no_change_records_failed_operation_alias_without_commit() -> None:
    service, writer, compiler = _service()
    command = UpdateIfcProjectCommand(
        patch=IfcProjectPatch(name="Project Service Test")
    )

    with pytest.raises(IfcProjectNoChangeError) as captured:
        await service.update(
            _scope(),
            command,
            idempotency_key="server_request_002",
        )

    assert captured.value.operation_alias == "op_002"  # type: ignore[attr-defined]
    assert writer.prepare_count == writer.base_read_count == compiler.apply_count == 1
    assert writer.fail_count == 1
    assert writer.failed_error_code == "IFC_NO_CHANGES"
    assert writer.commit_count == writer.exact_reread_count == 0
    assert compiler.inspect_count == 0


@pytest.mark.asyncio
async def test_server_only_idempotency_identity_keeps_the_request_consistent() -> None:
    command = UpdateIfcProjectCommand(
        patch=IfcProjectPatch(description="Stable request"),
        clear_fields=("phase",),
    )
    captured_requests = []
    for _ in range(2):
        service, writer, _ = _service()
        await service.update(
            _scope(),
            command,
            idempotency_key="server_request_replay_001",
        )
        captured_requests.append(writer.prepare_requests[0])

    assert captured_requests[0] == captured_requests[1]
    request = captured_requests[0]
    assert request["idempotency_key"] == "server_request_replay_001"
    assert request["request_digest"] == hashlib.sha256(
        command.model_dump_json().encode("utf-8")
    ).hexdigest()
    assert request["operation_detail"] == {
        "detail_kind": "ifc_project_update",
        "request": {
            "patch": {"description": "Stable request"},
            "clear_fields": ["phase"],
        },
    }
    assert "idempotency_key" not in request["operation_detail"]

    service, writer, _ = _service()
    with pytest.raises(ValueError, match="idempotency_key"):
        await service.update(_scope(), command, idempotency_key=" client supplied ")
    assert writer.prepare_count == 0


@pytest.mark.asyncio
async def test_cancelled_first_caller_cannot_close_shared_candidate_before_replay() -> None:
    codec = IfcOpenShellModelCodec()
    content = codec.create_revision1(
        project_global_id=_PROJECT_GLOBAL_ID,
        project_name="Project Service Test",
        header_timestamp=_NOW,
    ).content
    writer = _BlockingWriter(content, codec)
    runtime = InProcessIfcRevisionRuntime(codec)
    service = IfcProjectService(writer, runtime, IfcWriteCoordinator())  # type: ignore[arg-type]
    command = UpdateIfcProjectCommand(patch=IfcProjectPatch(name="Shared Project"))

    first = asyncio.create_task(
        service.update(_scope(), command, idempotency_key="shared_request_001")
    )
    await writer.commit_entered.wait()
    replay = asyncio.create_task(
        service.update(_scope(), command, idempotency_key="shared_request_001")
    )
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert writer.candidate is not None
    assert writer.candidate.closed is False
    writer.release_commit.set()
    result = await replay

    assert result.operation_alias == "op_002"
    assert result.project.name == "Shared Project"
    assert writer.commit_count == 1
    assert writer.candidate.closed is True
