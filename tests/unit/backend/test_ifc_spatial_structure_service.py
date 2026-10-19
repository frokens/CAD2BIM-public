from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from CAD2BIM_backend.application.ifc_revision_writer import (
    VerifiedCommittedIfcRevision,
    VerifiedExactIfcRevision,
)
from CAD2BIM_backend.application.ifc_spatial_structure_service import (
    IfcSpatialStructureService,
)
from CAD2BIM_backend.application.ifc_write_coordinator import IfcWriteCoordinator
from CAD2BIM_backend.domain.ifc_model import (
    ExactIfcArtifactVersion,
    ExactIfcRevision,
    IfcExecutionClaim,
    IfcModelState,
    IfcRunIdentity,
    PreparedIfcRevision,
)
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    CreateIfcSpatialOperation,
    DeleteIfcSpatialOperation,
    IfcProjectParentReference,
    IfcProjectWorldPlacement,
    IfcSpatialCreatedBinding,
    IfcSpatialAppliedItem,
    IfcSpatialCursorError,
    IfcSpatialCursorExpiredError,
    IfcSpatialStructureIntegrityError,
    IfcSpatialStructureApplyResult,
    IfcSpatialStructureListQuery,
    IfcSpatialTargetNotFoundError,
)
from CAD2BIM_backend.infrastructure.data_query.cursor_codec import CursorCodec
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.ifc.spatial_structure import (
    IfcOpenShellSpatialStructureCompiler,
)
from tests.process_support.ifc_revision_runtime import (
    BytesIfcCandidate,
    InProcessIfcRevisionRuntime,
)
from CAD2BIM_backend.ports.ifc_revision_runtime import (
    ApplyIfcSpatialStructureRevisionRequest,
    GeneratedIfcRevision,
    IfcSpatialRevisionEffect,
)


_PROJECT_GLOBAL_ID = "1xvJu1wbjMywJMjRBpLw$1"
_NOW = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


def _scope(run_id: str = "run_001") -> IfcRunIdentity:
    return IfcRunIdentity(
        owner_id="owner_001",
        tenant_id="tenant_001",
        task_id="task_001",
        run_id=run_id,
    )


def _placement(x: float = 0.0) -> IfcProjectWorldPlacement:
    return IfcProjectWorldPlacement(
        origin=(x, 0.0, 0.0),
        axis=(0.0, 0.0, 1.0),
        ref_direction=(1.0, 0.0, 0.0),
    )


def _create_sites(*names: str) -> ApplyIfcSpatialStructureCommand:
    return ApplyIfcSpatialStructureCommand(
        operations=tuple(
            CreateIfcSpatialOperation(
                client_ref=f"site_{index}",
                spatial_class="site",
                parent=IfcProjectParentReference(),
                project_world_placement=_placement(float(index)),
                name=name,
                composition_type="ELEMENT",
            )
            for index, name in enumerate(names)
        )
    )


def _exact(
    scope: IfcRunIdentity,
    content: bytes,
    codec: IfcOpenShellModelCodec,
    *,
    revision_id: str,
    revision_no: int,
) -> ExactIfcRevision:
    inspection = codec.inspect(content)
    return ExactIfcRevision(
        scope=scope,
        model_id="ifcm_001",
        model_state=IfcModelState.ACTIVE,
        revision_id=revision_id,
        revision_no=revision_no,
        write_epoch=revision_no,
        artifact=ExactIfcArtifactVersion(
            artifact_id=f"art_{revision_id}",
            version_id=f"artv_{revision_id}",
            size_bytes=inspection.size_bytes,
            sha256=inspection.sha256,
        ),
        project_global_id=_PROJECT_GLOBAL_ID,
    )


class _Writer:
    def __init__(self, scope: IfcRunIdentity, content: bytes, codec: IfcOpenShellModelCodec) -> None:
        self.scope = scope
        self.codec = codec
        first = _exact(scope, content, codec, revision_id="ifcr_001", revision_no=1)
        self.contents = {first.revision_id: content}
        self.exacts = {first.revision_id: first}
        self.current_revision_id = first.revision_id
        self.prepare_count = 0
        self.commit_count = 0
        self.fail_count = 0
        self.pin_current_count = 0
        self.pin_exact_ids: list[str] = []
        self.recovered = None

    async def prepare(self, scope: IfcRunIdentity, **kwargs: object) -> PreparedIfcRevision:
        assert scope == self.scope
        self.prepare_count += 1
        before = self.exacts[self.current_revision_id]
        return PreparedIfcRevision(
            operation_id="ifcop_001",
            idempotency_key=str(kwargs["idempotency_key"]),
            request_digest=str(kwargs["request_digest"]),
            before=before,
            after_revision_id="ifcr_002",
            artifact_id="art_ifcr_002",
            artifact_version_id="artv_ifcr_002",
            prepared_at=_NOW,
        )

    async def recover_revision(self, scope: IfcRunIdentity, **kwargs: object):
        assert scope == self.scope
        return self.recovered

    async def claim_execution(
        self, scope: IfcRunIdentity, prepared: PreparedIfcRevision,
    ) -> IfcExecutionClaim:
        assert scope == self.scope
        return IfcExecutionClaim(
            operation_id=prepared.operation_id, request_digest=prepared.request_digest,
            owner="ifc:test:1:1:nonce", generation=1, token="test-token",
            deadline_at=_NOW + timedelta(hours=1), remaining_seconds=3600,
        )

    def guard_registration(self, scope: IfcRunIdentity, claim: IfcExecutionClaim):
        assert scope == self.scope and claim.operation_id == "ifcop_001"
        async def register(_pid: int, _birth_us: int) -> None:
            pass
        return register

    async def retire_execution(self, scope: IfcRunIdentity, claim: IfcExecutionClaim) -> None:
        assert scope == self.scope and claim.operation_id == "ifcop_001"

    async def resume_selected_revision(
        self, scope: IfcRunIdentity, prepared: PreparedIfcRevision,
        claim: IfcExecutionClaim,
    ):
        assert scope == self.scope and claim.operation_id == prepared.operation_id
        return None

    async def open_prepared_base(
        self, scope: IfcRunIdentity, prepared: PreparedIfcRevision
    ) -> BytesIfcCandidate:
        assert scope == self.scope
        content = self.contents[prepared.before.revision_id]
        return BytesIfcCandidate(content, sha256=self.codec.inspect(content).sha256)

    async def commit(
        self, scope: IfcRunIdentity, prepared: PreparedIfcRevision, candidate: object,
        **kwargs: object,
    ) -> VerifiedCommittedIfcRevision:
        assert scope == self.scope
        self.commit_count += 1
        content = candidate.candidate.content
        exact = _exact(
            scope,
            content,
            self.codec,
            revision_id=prepared.after_revision_id,
            revision_no=prepared.before.revision_no + 1,
        )
        self.exacts[exact.revision_id] = exact
        self.contents[exact.revision_id] = content
        self.current_revision_id = exact.revision_id
        return VerifiedCommittedIfcRevision(
            operation_alias="op_002",
            exact=exact,
            inspection=self.codec.inspect(content),
        )

    async def fail_prepared(self, scope: IfcRunIdentity, prepared: PreparedIfcRevision, **_: object) -> str:
        assert scope == self.scope
        assert prepared.operation_id == "ifcop_001"
        self.fail_count += 1
        return "op_002"

    async def freeze_indeterminate(self, *_: object, **__: object) -> None:
        raise AssertionError("the unit scenario must not freeze")

    async def pin_current(self, scope: IfcRunIdentity) -> ExactIfcRevision:
        assert scope == self.scope
        self.pin_current_count += 1
        return self.exacts[self.current_revision_id]

    async def pin_exact(self, scope: IfcRunIdentity, *, revision_id: str) -> ExactIfcRevision:
        assert scope == self.scope
        self.pin_exact_ids.append(revision_id)
        return self.exacts[revision_id]

    async def read_exact(self, exact: ExactIfcRevision) -> VerifiedExactIfcRevision:
        content = self.contents[exact.revision_id]
        return VerifiedExactIfcRevision(
            exact=exact,
            content=content,
            inspection=self.codec.inspect(content),
        )

    async def read_projection(self, exact, *, request):
        from CAD2BIM_backend.ports.ifc_revision_runtime import InspectedIfcRevision

        content = self.contents[exact.revision_id]
        return InspectedIfcRevision(
            inspection=self.codec.inspect(content),
            spatial_page=IfcOpenShellSpatialStructureCompiler(self.codec).list_spatial_structure(content, request.spatial_query),
        )

    async def freeze_exact(self, *_: object, **__: object) -> None:
        raise AssertionError("the unit scenario must not freeze")


class _SingleAttemptSupervisor:
    async def invoke(self, attempt, *, on_exhausted, non_retryable_errors=()):  # type: ignore[no-untyped-def]
        del on_exhausted, non_retryable_errors
        return await attempt()


class _MismatchedSpatialRuntime(InProcessIfcRevisionRuntime):
    async def execute(self, request, *, source=None):  # type: ignore[no-untyped-def]
        result = await super().execute(request, source=source)
        if not isinstance(request, ApplyIfcSpatialStructureRevisionRequest):
            return result
        assert isinstance(result, GeneratedIfcRevision)
        assert isinstance(result.effect, IfcSpatialRevisionEffect)
        first = result.effect.created[0]
        return GeneratedIfcRevision(
            candidate=result.candidate,
            inspection=result.inspection,
            effect=IfcSpatialRevisionEffect(
                project_global_id=result.effect.project_global_id,
                created=(
                    IfcSpatialCreatedBinding(
                        client_ref="unexpected_ref",
                        global_id=first.global_id,
                    ),
                ),
                affected_global_ids=result.effect.affected_global_ids,
                spatial_summary=result.effect.spatial_summary,
            ),
        )


def _service(*, clock=lambda: 1_000.0) -> tuple[
    IfcSpatialStructureService,
    _Writer,
    IfcOpenShellSpatialStructureCompiler,
]:
    scope = _scope()
    codec = IfcOpenShellModelCodec()
    compiler = IfcOpenShellSpatialStructureCompiler(codec)
    base = codec.create_revision1(
        project_global_id=_PROJECT_GLOBAL_ID,
        project_name="Spatial Application Test",
        header_timestamp=_NOW,
    ).content
    writer = _Writer(scope, base, codec)
    runtime = InProcessIfcRevisionRuntime(codec)
    runtime.spatial = compiler
    return (
        IfcSpatialStructureService(
            writer,
            CursorCodec("A" * 43),
            runtime,
            IfcWriteCoordinator(),
            clock=clock,
            cursor_ttl_seconds=60,
        ),
        writer,
        compiler,
    )


@pytest.mark.asyncio
async def test_apply_commits_once_and_returns_closed_request_local_bindings() -> None:
    service, writer, _ = _service()
    command = _create_sites("Root", "Secondary")

    result = await service.apply(_scope(), command, idempotency_key="invoke_001")

    assert writer.prepare_count == writer.commit_count == 1
    assert writer.fail_count == 0
    assert result.operation_alias == "op_002"
    assert result.applied_count == 2
    assert [item.client_ref for item in result.applied] == ["site_0", "site_1"]
    assert all(item.action == "create" and item.target_global_id is None for item in result.applied)
    assert {item.global_id for item in result.applied} == {
        item.global_id for item in result.spatial_summary
    }


@pytest.mark.asyncio
async def test_success_replay_returns_original_spatial_result_without_compilation() -> None:
    service, writer, _ = _service()
    original = IfcSpatialStructureApplyResult(
        operation_alias="op_002", applied_count=1,
        applied=(IfcSpatialAppliedItem(
            action="create", global_id=_PROJECT_GLOBAL_ID, client_ref="site_0",
        ),), spatial_summary=(),
    )
    writer.recovered = SimpleNamespace(spatial_result=original, operation_alias="op_002")

    result = await service.apply(
        _scope(), _create_sites("Original Site"), idempotency_key="original_spatial_action",
    )

    assert result == original
    assert writer.prepare_count == writer.commit_count == 0


@pytest.mark.asyncio
async def test_mismatched_runtime_effect_is_rejected_before_commit() -> None:
    scope = _scope()
    codec = IfcOpenShellModelCodec()
    compiler = IfcOpenShellSpatialStructureCompiler(codec)
    base = codec.create_revision1(
        project_global_id=_PROJECT_GLOBAL_ID,
        project_name="Spatial Application Test",
        header_timestamp=_NOW,
    ).content
    writer = _Writer(scope, base, codec)
    runtime = _MismatchedSpatialRuntime(codec)
    runtime.spatial = compiler
    service = IfcSpatialStructureService(
        writer,
        CursorCodec("A" * 43),
        runtime,
        IfcWriteCoordinator(),
        supervisor=_SingleAttemptSupervisor(),  # type: ignore[arg-type]
    )

    with pytest.raises(IfcSpatialStructureIntegrityError, match="create bindings"):
        await service.apply(
            scope,
            _create_sites("Root"),
            idempotency_key="invoke_mismatched_effect",
        )

    assert writer.prepare_count == 1
    assert writer.commit_count == 0
    assert writer.current_revision_id == "ifcr_001"


@pytest.mark.asyncio
async def test_business_preflight_failure_records_failed_operation_without_commit() -> None:
    service, writer, _ = _service()
    command = ApplyIfcSpatialStructureCommand(
        operations=(DeleteIfcSpatialOperation(target_global_id="0" * 22),)
    )

    with pytest.raises(IfcSpatialTargetNotFoundError) as captured:
        await service.apply(_scope(), command, idempotency_key="invoke_002")

    assert captured.value.operation_alias == "op_002"
    assert writer.prepare_count == writer.fail_count == 1
    assert writer.commit_count == 0


@pytest.mark.asyncio
async def test_cursor_continuation_reads_the_first_exact_revision_after_current_advances() -> None:
    service, writer, compiler = _service()
    populated = compiler.apply(writer.contents["ifcr_001"], _create_sites("A", "B", "C"))
    revision_two = _exact(
        _scope(), populated.content, writer.codec, revision_id="ifcr_002", revision_no=2
    )
    writer.exacts[revision_two.revision_id] = revision_two
    writer.contents[revision_two.revision_id] = populated.content
    writer.current_revision_id = revision_two.revision_id

    first = await service.list(
        _scope(),
        IfcSpatialStructureListQuery(page_size=2),
        None,
    )
    assert len(first.items) == 2
    assert first.next_cursor is not None

    advanced = compiler.apply(populated.content, _create_sites("D"))
    revision_three = _exact(
        _scope(), advanced.content, writer.codec, revision_id="ifcr_003", revision_no=3
    )
    writer.exacts[revision_three.revision_id] = revision_three
    writer.contents[revision_three.revision_id] = advanced.content
    writer.current_revision_id = revision_three.revision_id

    second = await service.list(_scope(), None, first.next_cursor)

    assert writer.pin_current_count == 1
    assert writer.pin_exact_ids == ["ifcr_002"]
    assert len(first.items) + len(second.items) == 3
    assert {item.name for item in (*first.items, *second.items)} == {"A", "B", "C"}
    assert second.next_cursor is None


@pytest.mark.asyncio
async def test_cursor_is_scope_bound_tamper_evident_and_expires() -> None:
    now = [1_000.0]
    service, writer, compiler = _service(clock=lambda: now[0])
    populated = compiler.apply(writer.contents["ifcr_001"], _create_sites("A", "B"))
    revision_two = _exact(
        _scope(), populated.content, writer.codec, revision_id="ifcr_002", revision_no=2
    )
    writer.exacts[revision_two.revision_id] = revision_two
    writer.contents[revision_two.revision_id] = populated.content
    writer.current_revision_id = revision_two.revision_id
    first = await service.list(
        _scope(), IfcSpatialStructureListQuery(page_size=1), None
    )
    assert first.next_cursor is not None

    with pytest.raises(IfcSpatialCursorError):
        await service.list(_scope("run_other"), None, first.next_cursor)
    replacement = "B" if first.next_cursor[-1] != "B" else "C"
    with pytest.raises(IfcSpatialCursorError):
        await service.list(_scope(), None, first.next_cursor[:-1] + replacement)

    now[0] = 1_060.0
    with pytest.raises(IfcSpatialCursorExpiredError):
        await service.list(_scope(), None, first.next_cursor)


@pytest.mark.asyncio
async def test_list_requires_query_xor_cursor_and_rejects_public_after_key() -> None:
    service, _, _ = _service()

    with pytest.raises(ValueError, match="exactly one"):
        await service.list(_scope(), None, None)
    with pytest.raises(ValueError, match="server-only"):
        await service.list(
            _scope(),
            IfcSpatialStructureListQuery(after=("IfcSite", "0" * 22)),
            None,
        )
