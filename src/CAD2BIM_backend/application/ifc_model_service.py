"""Application core for the Run-bound durable IFC model."""

from __future__ import annotations

import hashlib
from time import monotonic
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from CAD2BIM_backend.domain.data_runtime import CriticalOperationStatus
from CAD2BIM_backend.domain.runtime_execution import IFC_REVISION_INVOCATION_EXECUTION, IFC_REVISION_EXECUTION
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
    IfcModelNotInitializedError,
    IfcModelState,
    IfcRevisionInspection,
    IfcRunIdentity,
    InitializeIfcModelCommand,
    PreparedIfcInitialize,
)
from CAD2BIM_backend.application.ifc_revision_writer import IfcRevisionWriter
from CAD2BIM_backend.application.ifc_write_coordinator import IfcWriteCoordinator
from CAD2BIM_backend.ports.ifc_model import (
    IfcInitializeOperationState,
    IfcModelCodecPort,
    IfcModelRepository,
)
from CAD2BIM_backend.ports.ifc_revision_runtime import (
    GeneratedIfcRevision,
    IfcRevisionRuntimeError,
    IfcRevisionRuntimePort,
    InitializeIfcRevisionRequest,
)
from CAD2BIM_backend.ports.storage import StorageKey, StoragePort, StoredObject


_MAX_IFC_INVOCATION_ATTEMPTS = 3
_T = TypeVar("_T")


class IfcInvocationSupervisor:
    """Bound technical retries without changing one logical IFC Operation."""

    def __init__(self, *, max_attempts: int = _MAX_IFC_INVOCATION_ATTEMPTS) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._max_attempts = max_attempts

    async def invoke(
        self,
        attempt: Callable[[], Awaitable[_T]],
        *,
        on_exhausted: Callable[[Exception], Awaitable[None]],
        non_retryable_errors: tuple[type[Exception], ...] = (),
    ) -> _T:
        last_error: Exception | None = None
        for _ in range(self._max_attempts):
            try:
                return await attempt()
            except (
                IfcModelAlreadyInitializedError,
                IfcModelConflictError,
                IfcModelFrozenError,
                IfcModelNotInitializedError,
            ):
                raise
            except Exception as exc:
                if isinstance(exc, non_retryable_errors):
                    raise
                if (
                    isinstance(exc, IfcRevisionRuntimeError)
                    and exc.diagnostic is not None
                    and exc.diagnostic.get("reason") in {
                        "queue_full", "queue_timeout", "capacity_unavailable",
                    }
                ):
                    # Admission cannot change an IFC revision. Preserve the
                    # existing Operation for reconciliation or a later replay;
                    # repeating this call cannot replenish its admission budget.
                    raise
                if (
                    isinstance(exc, IfcRevisionRuntimeError)
                    and exc.diagnostic is not None
                    and exc.diagnostic.get("reason") == "execution_budget_exhausted"
                ):
                    last_error = exc
                    break
                if isinstance(exc, IfcRevisionRuntimeError) and not exc.retryable:
                    raise
                last_error = exc
        assert last_error is not None
        try:
            await on_exhausted(last_error)
        except Exception as pause_error:
            raise IfcModelIntegrityError(
                "IFC invocation failed and its system pause could not be confirmed"
            ) from pause_error
        raise IfcModelIntegrityError(
            "IFC invocation exhausted technical retries and is paused"
        ) from last_error


@dataclass(slots=True)
class _InitializeAttemptState:
    prepared: PreparedIfcInitialize | None = None
    claim: IfcExecutionClaim | None = None
    generated: GeneratedIfcRevision | None = None


@dataclass(slots=True)
class _ReadAttemptState:
    exact: ExactIfcRevision | None = None


class IfcModelService:
    """Coordinate exact IFC bytes, repository state, and safe read projections."""

    def __init__(
        self,
        repository: IfcModelRepository,
        storage: StoragePort,
        codec: IfcModelCodecPort,
        writer: IfcRevisionWriter,
        runtime: IfcRevisionRuntimePort,
        coordinator: IfcWriteCoordinator,
        *,
        supervisor: IfcInvocationSupervisor | None = None,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._codec = codec
        self._writer = writer
        self._runtime = runtime
        self._coordinator = coordinator
        self._supervisor = supervisor or IfcInvocationSupervisor()

    async def initialize(
        self,
        scope: IfcRunIdentity,
        command: InitializeIfcModelCommand,
    ) -> IfcModelInitializeResult:
        """Create or replay the one create-only revision-1 initialization."""

        request_digest = hashlib.sha256(command.model_dump_json().encode("utf-8")).hexdigest()
        project_global_id = self._codec.stable_project_global_id(scope.model_dump_json())
        state = _InitializeAttemptState()
        deadline = monotonic() + IFC_REVISION_INVOCATION_EXECUTION.total_seconds

        async def attempt() -> IfcModelInitializeResult:
            prepared = state.prepared
            claim = state.claim
            if prepared is None or claim is None:
                raise IfcModelIntegrityError("IFC initialization execution was not claimed")
            generated = state.generated
            if generated is None:
                resumed = await self._writer.resume_selected_initialize(scope, prepared, claim)
                if resumed is not None:
                    return resumed.result
                runtime_result = await self._runtime.execute(
                    InitializeIfcRevisionRequest(
                        command=command,
                        project_global_id=prepared.project_global_id,
                        header_timestamp=prepared.prepared_at,
                    )
                )
                if not isinstance(runtime_result, GeneratedIfcRevision):
                    raise IfcModelIntegrityError(
                        "IFC revision runtime did not generate revision 1"
                    )
                generated = runtime_result
                state.generated = generated
            committed = await self._writer.commit_initialize(
                scope, prepared, generated, claim=claim,
            )
            if committed.result.model != committed.inspection.to_safe_info():
                raise IfcModelIntegrityError(
                    "committed IFC result does not match exact revision bytes"
                )
            return committed.result

        async def pause(last_error: Exception) -> None:
            del last_error
            prepared = state.prepared
            assert prepared is not None and state.claim is not None
            exact = await self._repository.get_exact(
                scope,
                revision_id=prepared.revision_id,
            )
            await self._repository.freeze_indeterminate(
                scope,
                model_id=prepared.model_id if exact is not None else None,
                operation_id=prepared.operation_id,
                reason_code="IFC_INVOCATION_RETRIES_EXHAUSTED",
                claim=state.claim,
            )

        async def supervised() -> IfcModelInitializeResult:
            original = await self._repository.get_initialize_operation_state(
                scope, request_digest=request_digest,
                project_global_id=project_global_id,
                project_name=command.project_name,
            )
            if original is not None:
                if original.status is CriticalOperationStatus.FAILED:
                    self._writer.raise_settled_failure(original)
                if original.status is CriticalOperationStatus.SUCCEEDED:
                    result, _ = await self._recover_initialize(scope, command, original)
                    return result
            prepared = await self._repository.prepare_initialize(
                scope, request_digest=request_digest,
                project_global_id=project_global_id, project_name=command.project_name,
            )
            state.prepared = prepared
            claim = await self._writer.claim_execution(scope, prepared)
            state.claim = claim
            execution_deadline = monotonic() + claim.remaining_seconds
            try:
                async with self._runtime.invocation_scope(
                    IFC_REVISION_INVOCATION_EXECUTION,
                    deadline=execution_deadline,
                    on_guard_registered=self._writer.guard_registration(scope, claim),
                ):
                    return await self._supervisor.invoke(attempt, on_exhausted=pause)
            finally:
                generated = state.generated
                try:
                    if generated is not None:
                        await generated.candidate.aclose()
                finally:
                    await self._writer.retire_execution(scope, claim)

        return await self._coordinator.invoke(
            scope,
            logical_operation_identity=f"ifc_model_initialize:{request_digest}",
            operation=supervised,
            deadline=deadline,
        )

    async def _recover_initialize(
        self,
        scope: IfcRunIdentity,
        command: InitializeIfcModelCommand,
        state: IfcInitializeOperationState,
    ) -> tuple[IfcModelInitializeResult, ExactIfcRevision]:
        if state.operation_alias is None:
            raise IfcModelIntegrityError("successful IFC initialization lost its catalog alias")
        exact = await self._repository.get_exact(scope, revision_id=state.prepared.revision_id)
        if exact is None:
            raise IfcModelIntegrityError("successful IFC initialization lost its exact revision")
        inspection = await self._inspect_initialize_exact(
            exact, state.prepared, command.project_name,
        )
        return (
            IfcModelInitializeResult(
                operation_alias=state.operation_alias,
                model=inspection.to_safe_info(),
            ),
            exact,
        )

    async def _inspect_initialize_exact(
        self,
        exact: ExactIfcRevision,
        prepared: PreparedIfcInitialize,
        project_name: str,
    ) -> IfcRevisionInspection:
        if (
            exact.scope != prepared.scope
            or exact.model_id != prepared.model_id
            or exact.revision_id != prepared.revision_id
            or exact.revision_no != 1
            or exact.write_epoch != 1
            or exact.project_global_id != prepared.project_global_id
            or exact.artifact.artifact_id != prepared.artifact_id
            or exact.artifact.version_id != prepared.artifact_version_id
        ):
            raise IfcModelIntegrityError("IFC initialization exact identity is inconsistent")
        deadline = monotonic() + IFC_REVISION_EXECUTION.total_seconds
        async with self._runtime.invocation_scope(IFC_REVISION_EXECUTION, deadline=deadline):
            inspection = await self._writer.inspect_exact(
                exact, expected_revision1_project_name=project_name,
            )
        if inspection.project_name != project_name:
            raise IfcModelIntegrityError("IFC initialization changed the IfcProject Name")
        return inspection

    async def info(self, scope: IfcRunIdentity) -> IfcModelInfo:
        """Inspect the exact current revision and return only bounded model facts."""

        deadline = monotonic() + IFC_REVISION_EXECUTION.total_seconds
        state = _ReadAttemptState()

        async def attempt() -> IfcModelInfo:
            await self._repository.require_invocation_allowed(scope)
            current = state.exact
            if current is None:
                current = await self._repository.get_current(scope)
                if current is None:
                    raise IfcModelNotInitializedError("the selected Run has no IfcModel")
                state.exact = current
            if current.scope != scope:
                raise IfcModelIntegrityError(
                    "current IFC revision does not match the requested Run"
                )
            if current.model_state is IfcModelState.FROZEN:
                raise IfcModelFrozenError("IFC model is frozen pending reconciliation")
            inspection = await self._read_and_inspect(current, revision1=False)
            return inspection.to_safe_info().model_copy(update={"source_exact": current})

        async def pause(last_error: Exception) -> None:
            del last_error
            current = state.exact or await self._repository.get_current(scope)
            if current is None:
                raise IfcModelIntegrityError(
                    "model-less IFC read failure cannot create a write Operation"
                )
            await self._repository.freeze_indeterminate(
                scope,
                model_id=current.model_id,
                operation_id=None,
                reason_code="IFC_INVOCATION_RETRIES_EXHAUSTED",
            )

        async with self._runtime.invocation_scope(IFC_REVISION_EXECUTION, deadline=deadline):
            return await self._supervisor.invoke(
                attempt, on_exhausted=pause, non_retryable_errors=(IfcRevisionRuntimeError,),
            )

    async def get_current_exact(
        self,
        scope: IfcRunIdentity,
    ) -> ExactIfcRevision | None:
        """Return one verified current reference for server-only recovery callers."""

        current = await self._repository.get_current(scope)
        if current is None:
            return None
        if current.scope != scope:
            await self._freeze_and_raise(
                scope,
                model_id=current.model_id,
                operation_id=None,
                reason_code="COMMITTED_IFC_SCOPE_DRIFT",
                message="current IFC revision does not match the requested Run",
            )
        if current.model_state is IfcModelState.FROZEN:
            raise IfcModelFrozenError("IFC model is frozen pending reconciliation")
        await self._inspect_committed_or_freeze(scope, current)
        return current

    async def get_exact_info(
        self,
        scope: IfcRunIdentity,
        *,
        revision_id: str,
    ) -> IfcModelInfo:
        """Inspect one pinned revision for server-only recovery and closure checks."""

        exact = await self._repository.get_exact(scope, revision_id=revision_id)
        if exact is None:
            raise IfcModelNotInitializedError("exact IFC revision was not found")
        if exact.scope != scope:
            await self._freeze_and_raise(
                scope,
                model_id=exact.model_id,
                operation_id=None,
                reason_code="COMMITTED_IFC_SCOPE_DRIFT",
                message="exact IFC revision does not match the requested Run",
            )
        if exact.model_state is IfcModelState.FROZEN:
            raise IfcModelFrozenError("IFC model is frozen pending reconciliation")
        inspection = await self._inspect_committed_or_freeze(scope, exact)
        return inspection.to_safe_info()

    async def _read_and_inspect(
        self,
        exact: ExactIfcRevision,
        *,
        revision1: bool,
        expected_project_name: str | None = None,
    ) -> IfcRevisionInspection:
        if revision1 and expected_project_name is None:
            raise IfcModelIntegrityError("revision 1 inspection requires its project name")
        return await self._writer.inspect_exact(
            exact,
            expected_revision1_project_name=expected_project_name if revision1 else None,
        )

    async def _inspect_committed_or_freeze(
        self,
        scope: IfcRunIdentity,
        exact: ExactIfcRevision,
    ) -> IfcRevisionInspection:
        try:
            return await self._read_and_inspect(exact, revision1=False)
        except IfcRevisionRuntimeError:
            # Capacity and supervised execution failures say nothing about the
            # integrity of an already committed model.
            raise
        except Exception as exc:
            await self._freeze_and_raise(
                scope,
                model_id=exact.model_id,
                operation_id=None,
                reason_code="COMMITTED_IFC_INTEGRITY_FAILURE",
                message="committed IFC revision failed exact integrity validation",
                cause=exc,
            )

    @staticmethod
    def _require_stored_matches_exact(
        stored: StoredObject,
        artifact: ExactIfcArtifactVersion,
    ) -> None:
        if (
            stored.filename != artifact.filename
            or stored.content_type != artifact.content_type
            or stored.size_bytes != artifact.size_bytes
            or stored.sha256 != artifact.sha256
        ):
            raise IfcModelIntegrityError("Storage metadata does not match exact ArtifactVersion")

    async def _freeze_and_raise(
        self,
        scope: IfcRunIdentity,
        *,
        model_id: str,
        operation_id: str | None,
        reason_code: str,
        message: str,
        cause: Exception | None = None,
    ) -> None:
        try:
            await self._repository.freeze_indeterminate(
                scope,
                model_id=model_id,
                operation_id=operation_id,
                reason_code=reason_code,
            )
        except Exception as freeze_error:
            raise IfcModelIntegrityError(
                f"{message}; fail-closed freeze could not be confirmed"
            ) from freeze_error
        if cause is None:
            raise IfcModelIntegrityError(message)
        raise IfcModelIntegrityError(message) from cause


class IfcOperationReconciler:
    """Server-only recovery for one create-only initialization Operation."""

    def __init__(
        self,
        service: IfcModelService,
        repository: IfcModelRepository,
        codec: IfcModelCodecPort,
    ) -> None:
        self._service = service
        self._repository = repository
        self._codec = codec

    async def reconcile_initialize(
        self,
        scope: IfcRunIdentity,
        command: InitializeIfcModelCommand,
    ) -> IfcModelInitializeResult:
        request_digest = hashlib.sha256(command.model_dump_json().encode("utf-8")).hexdigest()
        project_global_id = self._codec.stable_project_global_id(scope.model_dump_json())
        state = await self._repository.get_initialize_operation_state(
            scope,
            request_digest=request_digest,
            project_global_id=project_global_id,
            project_name=command.project_name,
        )
        if state is None or state.status is CriticalOperationStatus.PREPARED:
            return await self._service.initialize(scope, command)
        if state.status is CriticalOperationStatus.FAILED:
            self._service._writer.raise_settled_failure(state)

        if state.status is CriticalOperationStatus.SUCCEEDED:
            try:
                result, exact = await self._service._recover_initialize(scope, command, state)
            except Exception as cause:
                raise IfcModelFrozenError(
                    "successful IFC initialization has invalid exact evidence"
                ) from cause
            if exact.model_state is IfcModelState.FROZEN:
                await self._repository.unfreeze_reconciled_model(
                    scope,
                    model_id=exact.model_id,
                )
            return result

        if state.status is not CriticalOperationStatus.INDETERMINATE:
            raise IfcModelFrozenError("IFC initialization state requires reconciliation")

        exact = await self._read_reconcile_evidence(scope, state.prepared)
        if exact is None:
            try:
                current = await self._repository.get_current(scope)
            except Exception as exc:
                raise IfcModelFrozenError(
                    "IFC initialization remains paused because commit state is unknown"
                ) from exc
            if current is not None:
                await self._repository.fail_indeterminate_initialize(
                    scope,
                    operation_id=state.prepared.operation_id,
                    error_code="IFC_ALREADY_INITIALIZED",
                    error_message="the selected Run already has an IfcModel",
                )
                raise IfcModelAlreadyInitializedError(
                    "the selected Run already has an IfcModel"
                )
            await self._repository.reopen_indeterminate_initialize(
                scope,
                operation_id=state.prepared.operation_id,
            )
            return await self._service.initialize(scope, command)

        await self._verify_reconcile_exact(exact, state.prepared, command.project_name)
        await self._repository.reopen_indeterminate_initialize(
            scope,
            operation_id=state.prepared.operation_id,
        )
        if exact.model_state is IfcModelState.FROZEN:
            await self._repository.unfreeze_reconciled_model(
                scope,
                model_id=exact.model_id,
            )
        return await self._service.initialize(scope, command)

    async def _read_reconcile_evidence(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcInitialize,
    ) -> ExactIfcRevision | None:
        try:
            return await self._repository.get_exact(
                scope,
                revision_id=prepared.revision_id,
            )
        except Exception as exc:
            raise IfcModelFrozenError(
                "IFC initialization remains paused because commit state is unknown"
            ) from exc

    async def _verify_reconcile_exact(
        self,
        exact: ExactIfcRevision,
        prepared: PreparedIfcInitialize,
        project_name: str,
    ) -> None:
        try:
            await self._service._inspect_initialize_exact(
                exact, prepared, project_name,
            )
        except Exception as exc:
            raise IfcModelFrozenError(
                "IFC initialization remains paused because exact evidence is invalid"
            ) from exc


def _storage_key(scope: IfcRunIdentity, artifact_id: str) -> StorageKey:
    return StorageKey(
        owner_id=scope.owner_id,
        task_id=scope.task_id,
        object_id=artifact_id,
        version=1,
    )


__all__ = ["IfcInvocationSupervisor", "IfcModelService", "IfcOperationReconciler"]
