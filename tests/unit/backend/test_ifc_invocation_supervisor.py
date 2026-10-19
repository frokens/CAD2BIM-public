from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from CAD2BIM_backend.application.ifc_model_service import (
    IfcInvocationSupervisor,
    IfcModelService,
    IfcOperationReconciler,
)
from CAD2BIM_backend.application.ifc_write_coordinator import IfcWriteCoordinator
from CAD2BIM_backend.application.ifc_revision_writer import IfcRevisionWriter
from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationErrorPhase,
    CriticalOperationStatus,
)
from CAD2BIM_backend.domain.ifc_model import (
    ExactIfcArtifactVersion,
    ExactIfcRevision,
    IfcExecutionClaim,
    IfcModelAlreadyInitializedError,
    IfcModelConflictError,
    IfcModelFrozenError,
    IfcModelInfo,
    IfcModelInitializeResult,
    IfcModelIntegrityError,
    IfcModelState,
    IfcProjectSummary,
    IfcRunIdentity,
    InitializeIfcModelCommand,
    PreparedIfcInitialize,
    IfcSettledOperationError,
)
from CAD2BIM_backend.ports.ifc_model import IfcInitializeOperationState
from CAD2BIM_backend.ports.ifc_revision_runtime import IfcRevisionRuntimeError


_PROJECT_GLOBAL_ID = "3Hj0000000000000000000"


def _scope() -> IfcRunIdentity:
    return IfcRunIdentity(
        owner_id="owner-1",
        tenant_id="tenant-1",
        task_id="task-1",
        run_id="run-1",
    )


def _command() -> InitializeIfcModelCommand:
    return InitializeIfcModelCommand(project_name="Gas network")


def _prepared() -> PreparedIfcInitialize:
    command = _command()
    return PreparedIfcInitialize(
        scope=_scope(),
        operation_id="op_internal_1",
        idempotency_key="ifc-model-initialize:run-1",
        request_digest=hashlib.sha256(
            command.model_dump_json().encode("utf-8")
        ).hexdigest(),
        model_id="ifcmodel_1",
        revision_id="ifcrev_1",
        artifact_id="artifact_1",
        artifact_version_id="av_1",
        project_global_id=_PROJECT_GLOBAL_ID,
        prepared_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )


def _success() -> IfcModelInitializeResult:
    return IfcModelInitializeResult(
        operation_alias="op_001",
        model=IfcModelInfo(
            size_bytes=1,
            project=IfcProjectSummary(
                global_id=_PROJECT_GLOBAL_ID,
                name="Gas network",
            )
        ),
    )


def _exact() -> ExactIfcRevision:
    return ExactIfcRevision(
        scope=_scope(),
        model_id="ifcmodel_1",
        model_state=IfcModelState.ACTIVE,
        revision_id="ifcrev_1",
        revision_no=1,
        write_epoch=1,
        artifact=ExactIfcArtifactVersion(
            artifact_id="artifact_1",
            version_id="av_1",
            size_bytes=1,
            sha256="a" * 64,
        ),
        project_global_id=_PROJECT_GLOBAL_ID,
    )


@pytest.mark.asyncio
async def test_supervisor_retries_technical_failure_but_not_business_failure() -> None:
    technical = Mock(side_effect=[RuntimeError("one"), RuntimeError("two"), None])
    paused = AsyncMock()

    async def technical_attempt() -> str:
        technical()
        return "ok"

    assert (
        await IfcInvocationSupervisor().invoke(
            technical_attempt,
            on_exhausted=paused,
        )
        == "ok"
    )
    assert technical.call_count == 3
    paused.assert_not_awaited()

    business = AsyncMock(side_effect=IfcModelAlreadyInitializedError("exists"))
    with pytest.raises(IfcModelAlreadyInitializedError):
        await IfcInvocationSupervisor().invoke(
            business,
            on_exhausted=paused,
        )
    business.assert_awaited_once()
    paused.assert_not_awaited()

    deterministic_runtime = AsyncMock(
        side_effect=IfcRevisionRuntimeError(
            "IFC_REVISION_INPUT_INVALID",
            "invalid closed runtime input",
            phase="input_validation",
            execution_started=False,
            retryable=False,
        )
    )
    with pytest.raises(IfcRevisionRuntimeError, match="invalid closed runtime input"):
        await IfcInvocationSupervisor().invoke(
            deterministic_runtime,
            on_exhausted=paused,
        )
    deterministic_runtime.assert_awaited_once()
    paused.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["queue_full", "queue_timeout", "capacity_unavailable"])
@pytest.mark.parametrize("execution_started", [False, True])
async def test_admission_failure_is_returned_without_retry_or_model_freeze(
    reason: str, execution_started: bool,
) -> None:
    failure = IfcRevisionRuntimeError(
        "IFC_REVISION_SERVICE_UNAVAILABLE", "admission rejected",
        phase="runtime", execution_started=execution_started, retryable=True,
        diagnostic={"reason": reason, "execution_started": execution_started},
    )
    attempt = AsyncMock(side_effect=failure)
    pause = AsyncMock()
    with pytest.raises(IfcRevisionRuntimeError) as error:
        await IfcInvocationSupervisor().invoke(attempt, on_exhausted=pause)
    assert error.value is failure
    attempt.assert_awaited_once()
    pause.assert_not_awaited()


@pytest.mark.asyncio
async def test_initialize_exhaustion_pauses_prepared_operation_without_model() -> None:
    prepared = _prepared()
    repository = SimpleNamespace(
        get_initialize_operation_state=AsyncMock(return_value=None),
        require_invocation_allowed=AsyncMock(),
        prepare_initialize=AsyncMock(return_value=prepared),
        get_exact=AsyncMock(return_value=None),
        freeze_indeterminate=AsyncMock(),
    )
    codec = SimpleNamespace(stable_project_global_id=Mock(return_value=_PROJECT_GLOBAL_ID))
    from contextlib import nullcontext
    runtime = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("codec unavailable")),
        invocation_scope=Mock(side_effect=lambda *args, **kwargs: nullcontext()),
    )
    claim = IfcExecutionClaim(
        operation_id=prepared.operation_id, request_digest=prepared.request_digest,
        owner="ifc:test:1:1:nonce", generation=1, token="test-token",
        deadline_at=datetime.now(timezone.utc) + timedelta(hours=1),
        remaining_seconds=3600,
    )
    writer = SimpleNamespace(
        claim_execution=AsyncMock(return_value=claim),
        resume_selected_initialize=AsyncMock(return_value=None),
        guard_registration=Mock(return_value=lambda _pid, _birth_us: None),
        retire_execution=AsyncMock(),
    )
    service = IfcModelService(
        repository,
        object(),
        codec,
        writer,
        runtime,
        IfcWriteCoordinator(),
    )

    with pytest.raises(IfcModelIntegrityError, match="technical retries"):
        await service.initialize(_scope(), _command())

    assert runtime.execute.await_count == 3
    repository.prepare_initialize.assert_awaited_once()
    repository.freeze_indeterminate.assert_awaited_once_with(
        _scope(),
        model_id=None,
        operation_id=prepared.operation_id,
        reason_code="IFC_INVOCATION_RETRIES_EXHAUSTED",
        claim=claim,
    )


@pytest.mark.asyncio
async def test_initialize_success_replay_inspects_original_without_building() -> None:
    from contextlib import nullcontext

    original = _success()
    state = IfcInitializeOperationState(
        prepared=_prepared(), status=CriticalOperationStatus.SUCCEEDED,
        operation_alias=original.operation_alias,
    )
    repository = SimpleNamespace(
        get_initialize_operation_state=AsyncMock(return_value=state),
        get_exact=AsyncMock(return_value=_exact()),
        prepare_initialize=AsyncMock(),
    )
    writer = SimpleNamespace(inspect_exact=AsyncMock(return_value=SimpleNamespace(
        project_name="Gas network",
        to_safe_info=lambda: original.model,
    )))
    runtime = SimpleNamespace(
        invocation_scope=Mock(side_effect=lambda *args, **kwargs: nullcontext()),
        execute=AsyncMock(),
    )
    service = IfcModelService(
        repository, object(), SimpleNamespace(stable_project_global_id=Mock(
            return_value=_PROJECT_GLOBAL_ID,
        )), writer, runtime, IfcWriteCoordinator(),
    )

    assert await service.initialize(_scope(), _command()) == original
    repository.prepare_initialize.assert_not_awaited()
    runtime.execute.assert_not_awaited()
    writer.inspect_exact.assert_awaited_once_with(
        _exact(), expected_revision1_project_name="Gas network",
    )


@pytest.mark.asyncio
async def test_concurrent_initialize_distinct_requests_do_not_share_result() -> None:
    from contextlib import nullcontext

    original = _success()
    state = IfcInitializeOperationState(
        prepared=_prepared(), status=CriticalOperationStatus.SUCCEEDED,
        operation_alias=original.operation_alias,
    )
    entered_inspection = asyncio.Event()
    release_inspection = asyncio.Event()

    async def read_original(scope, *, request_digest, **kwargs):
        del scope, kwargs
        if request_digest != state.prepared.request_digest:
            raise IfcModelConflictError("initialization request changed")
        return state

    async def inspect_original(exact, *, expected_revision1_project_name):
        assert exact == _exact()
        assert expected_revision1_project_name == "Gas network"
        entered_inspection.set()
        await release_inspection.wait()
        return SimpleNamespace(project_name="Gas network", to_safe_info=lambda: original.model)

    repository = SimpleNamespace(
        get_initialize_operation_state=AsyncMock(side_effect=read_original),
        get_exact=AsyncMock(return_value=_exact()),
        prepare_initialize=AsyncMock(),
    )
    writer = SimpleNamespace(inspect_exact=AsyncMock(side_effect=inspect_original))
    runtime = SimpleNamespace(
        invocation_scope=Mock(side_effect=lambda *args, **kwargs: nullcontext()),
        execute=AsyncMock(),
    )
    service = IfcModelService(
        repository, object(), SimpleNamespace(stable_project_global_id=Mock(
            return_value=_PROJECT_GLOBAL_ID,
        )), writer, runtime, IfcWriteCoordinator(),
    )

    first = asyncio.create_task(service.initialize(_scope(), _command()))
    await entered_inspection.wait()
    second = asyncio.create_task(service.initialize(
        _scope(), InitializeIfcModelCommand(project_name="Different request"),
    ))
    await asyncio.sleep(0)
    assert not second.done()
    release_inspection.set()

    assert await first == original
    with pytest.raises(IfcModelConflictError, match="request changed"):
        await second
    assert repository.get_initialize_operation_state.await_count == 2
    repository.prepare_initialize.assert_not_awaited()
    runtime.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_initialize_failed_replay_returns_original_terminal_error() -> None:
    state = IfcInitializeOperationState(
        prepared=_prepared(), status=CriticalOperationStatus.FAILED,
        error_code="IFC_ALREADY_INITIALIZED", error_message="original rejection",
        error_phase=CriticalOperationErrorPhase.COMMIT_FENCE,
        operation_alias="op_001",
    )
    repository = SimpleNamespace(
        get_initialize_operation_state=AsyncMock(return_value=state),
        prepare_initialize=AsyncMock(),
    )
    runtime = SimpleNamespace(execute=AsyncMock())
    service = IfcModelService(
        repository, object(), SimpleNamespace(stable_project_global_id=Mock(
            return_value=_PROJECT_GLOBAL_ID,
        )), SimpleNamespace(raise_settled_failure=IfcRevisionWriter.raise_settled_failure),
        runtime, IfcWriteCoordinator(),
    )

    with pytest.raises(IfcSettledOperationError) as caught:
        await service.initialize(_scope(), _command())

    assert (caught.value.code, str(caught.value), caught.value.phase,
            caught.value.operation_alias, caught.value.retryable) == (
        "IFC_ALREADY_INITIALIZED", "original rejection", "commit_fence", "op_001", False,
    )
    repository.prepare_initialize.assert_not_awaited()
    runtime.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_reopens_proven_uncommitted_initialize() -> None:
    prepared = _prepared()
    repository = SimpleNamespace(
        get_initialize_operation_state=AsyncMock(
            return_value=IfcInitializeOperationState(
                prepared=prepared,
                status=CriticalOperationStatus.INDETERMINATE,
                error_code="IFC_INVOCATION_RETRIES_EXHAUSTED",
            )
        ),
        get_exact=AsyncMock(return_value=None),
        get_current=AsyncMock(return_value=None),
        reopen_indeterminate_initialize=AsyncMock(),
    )
    service = SimpleNamespace(initialize=AsyncMock(return_value=_success()))
    codec = SimpleNamespace(
        stable_project_global_id=Mock(return_value=_PROJECT_GLOBAL_ID)
    )
    reconciler = IfcOperationReconciler(service, repository, codec)

    assert await reconciler.reconcile_initialize(_scope(), _command()) == _success()
    repository.reopen_indeterminate_initialize.assert_awaited_once_with(
        _scope(),
        operation_id=prepared.operation_id,
    )
    service.initialize.assert_awaited_once_with(_scope(), _command())


@pytest.mark.asyncio
async def test_reconciler_replays_proven_committed_initialize() -> None:
    prepared = _prepared()
    repository = SimpleNamespace(
        get_initialize_operation_state=AsyncMock(
            return_value=IfcInitializeOperationState(
                prepared=prepared,
                status=CriticalOperationStatus.SUCCEEDED,
                operation_alias="op_002",
            )
        ),
        get_exact=AsyncMock(return_value=_exact()),
        unfreeze_reconciled_model=AsyncMock(),
    )
    service = SimpleNamespace(
        initialize=AsyncMock(return_value=_success()),
        _recover_initialize=AsyncMock(return_value=(_success(), _exact())),
    )
    codec = SimpleNamespace(
        stable_project_global_id=Mock(return_value=_PROJECT_GLOBAL_ID)
    )

    assert (
        await IfcOperationReconciler(service, repository, codec).reconcile_initialize(
            _scope(),
            _command(),
        )
        == _success()
    )
    service._recover_initialize.assert_awaited_once()
    repository.unfreeze_reconciled_model.assert_not_awaited()
    service.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_rejects_wrong_artifact_binding_before_inspection() -> None:
    from contextlib import nullcontext

    prepared = _prepared()
    wrong = _exact().model_copy(update={
        "artifact": _exact().artifact.model_copy(update={"version_id": "av_wrong"}),
    })
    repository = SimpleNamespace(
        get_initialize_operation_state=AsyncMock(return_value=IfcInitializeOperationState(
            prepared=prepared, status=CriticalOperationStatus.INDETERMINATE,
        )),
        get_exact=AsyncMock(return_value=wrong),
        reopen_indeterminate_initialize=AsyncMock(),
        unfreeze_reconciled_model=AsyncMock(),
    )
    writer = SimpleNamespace(inspect_exact=AsyncMock())
    runtime = SimpleNamespace(
        invocation_scope=Mock(side_effect=lambda *args, **kwargs: nullcontext()),
    )
    codec = SimpleNamespace(stable_project_global_id=Mock(return_value=_PROJECT_GLOBAL_ID))
    service = IfcModelService(
        repository, object(), codec, writer, runtime, IfcWriteCoordinator(),
    )

    with pytest.raises(IfcModelFrozenError, match="exact evidence is invalid"):
        await IfcOperationReconciler(service, repository, codec).reconcile_initialize(
            _scope(), _command(),
        )
    writer.inspect_exact.assert_not_awaited()
    repository.reopen_indeterminate_initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_closes_proven_business_rejection() -> None:
    prepared = _prepared()
    repository = SimpleNamespace(
        get_initialize_operation_state=AsyncMock(
            return_value=IfcInitializeOperationState(
                prepared=prepared,
                status=CriticalOperationStatus.INDETERMINATE,
            )
        ),
        get_exact=AsyncMock(return_value=None),
        get_current=AsyncMock(return_value=_exact()),
        fail_indeterminate_initialize=AsyncMock(),
    )
    service = SimpleNamespace(initialize=AsyncMock())
    codec = SimpleNamespace(
        stable_project_global_id=Mock(return_value=_PROJECT_GLOBAL_ID)
    )

    with pytest.raises(IfcModelAlreadyInitializedError):
        await IfcOperationReconciler(service, repository, codec).reconcile_initialize(
            _scope(),
            _command(),
        )

    repository.fail_indeterminate_initialize.assert_awaited_once_with(
        _scope(),
        operation_id=prepared.operation_id,
        error_code="IFC_ALREADY_INITIALIZED",
        error_message="the selected Run already has an IfcModel",
    )
    service.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_keeps_unknown_initialize_paused() -> None:
    prepared = _prepared()
    repository = SimpleNamespace(
        get_initialize_operation_state=AsyncMock(
            return_value=IfcInitializeOperationState(
                prepared=prepared,
                status=CriticalOperationStatus.INDETERMINATE,
                error_code="IFC_INVOCATION_RETRIES_EXHAUSTED",
            )
        ),
        get_exact=AsyncMock(side_effect=RuntimeError("database unavailable")),
        reopen_indeterminate_initialize=AsyncMock(),
    )
    service = SimpleNamespace(initialize=AsyncMock())
    codec = SimpleNamespace(
        stable_project_global_id=Mock(return_value=_PROJECT_GLOBAL_ID)
    )

    with pytest.raises(IfcModelFrozenError, match="commit state is unknown"):
        await IfcOperationReconciler(service, repository, codec).reconcile_initialize(
            _scope(),
            _command(),
        )

    repository.reopen_indeterminate_initialize.assert_not_awaited()
    service.initialize.assert_not_awaited()
