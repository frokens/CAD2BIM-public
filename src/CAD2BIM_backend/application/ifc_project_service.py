"""Application core for durable IFC Project metadata updates."""

from __future__ import annotations

import hashlib
from time import monotonic
from dataclasses import dataclass
from CAD2BIM_backend.domain.runtime_execution import IFC_REVISION_INVOCATION_EXECUTION

from CAD2BIM_backend.application.ifc_model_service import IfcInvocationSupervisor
from CAD2BIM_backend.application.ifc_revision_writer import IfcRevisionWriter
from CAD2BIM_backend.application.ifc_write_coordinator import IfcWriteCoordinator
from CAD2BIM_backend.application.ifc_write_support import (
    bind_failed_operation,
    require_ifc_idempotency_key,
)
from CAD2BIM_backend.domain.data_runtime import (
    IFC_PROJECT_UPDATE_INTENT_SUMMARY,
    IfcProjectUpdateOperationDetail,
)
from CAD2BIM_backend.domain.ifc_model import IfcExecutionClaim, IfcRunIdentity, PreparedIfcRevision
from CAD2BIM_backend.domain.ifc_project import (
    IfcProjectBusinessError,
    IfcProjectIntegrityError,
    IfcProjectUpdateResult,
    UpdateIfcProjectCommand,
)
from CAD2BIM_backend.ports.ifc_revision_runtime import (
    GeneratedIfcRevision,
    IfcProjectRevisionEffect,
    IfcRevisionRuntimePort,
    UpdateIfcProjectRevisionRequest,
)


_TOOL_NAME = "ifc_project_update"
_RETRY_EXHAUSTED = "IFC_INVOCATION_RETRIES_EXHAUSTED"


@dataclass(slots=True)
class _AttemptState:
    prepared: PreparedIfcRevision | None = None
    claim: IfcExecutionClaim | None = None
    generated: GeneratedIfcRevision | None = None


class IfcProjectService:
    """Apply one typed Project patch through the shared durable revision writer."""

    def __init__(
        self,
        writer: IfcRevisionWriter,
        runtime: IfcRevisionRuntimePort,
        coordinator: IfcWriteCoordinator,
        *,
        supervisor: IfcInvocationSupervisor | None = None,
    ) -> None:
        self._writer = writer
        self._runtime = runtime
        self._coordinator = coordinator
        self._supervisor = supervisor or IfcInvocationSupervisor()

    async def update(
        self,
        scope: IfcRunIdentity,
        command: UpdateIfcProjectCommand,
        *,
        idempotency_key: str,
    ) -> IfcProjectUpdateResult:
        normalized_key = require_ifc_idempotency_key(idempotency_key)
        request_digest = hashlib.sha256(
            command.model_dump_json().encode("utf-8")
        ).hexdigest()
        detail = IfcProjectUpdateOperationDetail(request=command)
        detail_payload = detail.persisted_payload()
        state = _AttemptState()
        deadline = monotonic() + IFC_REVISION_INVOCATION_EXECUTION.total_seconds

        async def attempt() -> IfcProjectUpdateResult:
            prepared = state.prepared
            claim = state.claim
            if prepared is None or claim is None:
                raise IfcProjectIntegrityError("Project execution was not claimed")
            try:
                generated = state.generated
                if generated is None:
                    resumed = await self._writer.resume_selected_revision(scope, prepared, claim)
                    if resumed is not None:
                        committed, selected = resumed
                        if selected.project_result is None:
                            raise IfcProjectIntegrityError("selected Project result is missing")
                        return IfcProjectUpdateResult(
                            operation_alias=committed.operation_alias,
                            project=selected.project_result,
                        )
                    base = await self._writer.open_prepared_base(scope, prepared)
                    runtime_result = await self._runtime.execute(
                        UpdateIfcProjectRevisionRequest(command=command),
                        source=base,
                    )
                    if not isinstance(runtime_result, GeneratedIfcRevision):
                        raise IfcProjectIntegrityError(
                            "IFC revision runtime did not generate a Project revision"
                        )
                    generated = runtime_result
                    state.generated = generated
            except IfcProjectBusinessError as exc:
                operation_alias = await self._writer.fail_prepared(
                    scope,
                    prepared,
                    claim=claim,
                    error_code=exc.code,
                    error_message=str(exc),
                )
                raise bind_failed_operation(
                    exc,
                    operation_alias=operation_alias,
                )

            effect = generated.effect
            if not isinstance(effect, IfcProjectRevisionEffect):
                raise IfcProjectIntegrityError(
                    "IFC revision runtime omitted the Project result"
                )
            if (
                effect.project.global_id != generated.inspection.project_global_id
                or effect.project.name != generated.inspection.project_name
            ):
                raise IfcProjectIntegrityError(
                    "generated Project metadata differs from its IFC inspection"
                )
            committed = await self._writer.commit(
                scope, prepared, generated, claim=claim, project_result=effect.project,
            )
            return IfcProjectUpdateResult(
                operation_alias=committed.operation_alias,
                project=effect.project,
            )

        async def pause(last_error: Exception) -> None:
            del last_error
            prepared = state.prepared
            assert prepared is not None and state.claim is not None
            await self._writer.freeze_indeterminate(
                scope,
                prepared,
                reason_code=_RETRY_EXHAUSTED,
                claim=state.claim,
            )

        async def supervised() -> IfcProjectUpdateResult:
            recovered = await self._writer.recover_revision(
                scope, tool_name=_TOOL_NAME, idempotency_key=normalized_key,
                request_digest=request_digest,
                intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                operation_detail=detail_payload,
            )
            if recovered is not None:
                if recovered.project_result is None or recovered.operation_alias is None:
                    raise IfcProjectIntegrityError("successful Project Operation lost its result")
                return IfcProjectUpdateResult(
                    operation_alias=recovered.operation_alias,
                    project=recovered.project_result,
                )
            prepared = await self._writer.prepare(
                scope, tool_name=_TOOL_NAME, idempotency_key=normalized_key,
                request_digest=request_digest,
                intent_summary=IFC_PROJECT_UPDATE_INTENT_SUMMARY,
                operation_detail=detail_payload,
            )
            state.prepared = prepared
            claim = await self._writer.claim_execution(scope, prepared)
            state.claim = claim
            execution_deadline = monotonic() + claim.remaining_seconds
            try:
                async with self._runtime.invocation_scope(
                    IFC_REVISION_INVOCATION_EXECUTION, deadline=execution_deadline,
                    on_guard_registered=self._writer.guard_registration(scope, claim),
                ):
                    return await self._supervisor.invoke(
                        attempt,
                        on_exhausted=pause,
                        non_retryable_errors=(IfcProjectBusinessError,),
                    )
            finally:
                generated = state.generated
                try:
                    if generated is not None:
                        await generated.candidate.aclose()
                finally:
                    await self._writer.retire_execution(scope, claim)

        return await self._coordinator.invoke(
            scope,
            logical_operation_identity=f"{_TOOL_NAME}:{normalized_key}:{request_digest}",
            operation=supervised,
            deadline=deadline,
        )


__all__ = ["IfcProjectService"]
