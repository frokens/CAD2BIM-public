"""Application core for durable project geometry-basis configuration and reads."""

from __future__ import annotations

import hashlib
from time import monotonic
from dataclasses import dataclass
from CAD2BIM_backend.domain.runtime_execution import IFC_REVISION_INVOCATION_EXECUTION, IFC_REVISION_EXECUTION

from CAD2BIM_backend.application.ifc_model_service import IfcInvocationSupervisor
from CAD2BIM_backend.application.ifc_revision_writer import IfcRevisionWriter
from CAD2BIM_backend.application.ifc_write_coordinator import IfcWriteCoordinator
from CAD2BIM_backend.application.ifc_write_support import (
    bind_failed_operation,
    require_ifc_idempotency_key,
)
from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    IfcGeometryBasisBusinessError,
    IfcRepresentationContextCatalog,
)
from CAD2BIM_backend.domain.data_runtime import (
    IFC_GEOMETRY_BASIS_CONFIGURE_INTENT_SUMMARY,
    IfcGeometryBasisConfigureOperationDetail,
)
from CAD2BIM_backend.domain.ifc_model import (
    ExactIfcRevision,
    IfcExecutionClaim,
    IfcGeometryBasisConfigureResult,
    IfcModelIntegrityError,
    IfcRunIdentity,
    PreparedIfcRevision,
)
from CAD2BIM_backend.ports.ifc_revision_runtime import (
    ConfigureIfcGeometryBasisRevisionRequest,
    GeneratedIfcRevision,
    IfcRevisionRuntimePort,
    IfcRevisionRuntimeError,
    InspectExactIfcRevisionRequest,
)


_CONFIGURE_TOOL_NAME = "ifc_model_configure_geometry_basis"
_RETRY_EXHAUSTED = "IFC_INVOCATION_RETRIES_EXHAUSTED"


@dataclass(slots=True)
class _ConfigureAttemptState:
    prepared: PreparedIfcRevision | None = None
    claim: IfcExecutionClaim | None = None
    generated: GeneratedIfcRevision | None = None


@dataclass(slots=True)
class _ReadAttemptState:
    exact: ExactIfcRevision | None = None


class IfcGeometryBasisService:
    """Compile typed geometry decisions into durable IFC revisions."""

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

    async def configure(
        self,
        scope: IfcRunIdentity,
        command: ConfigureIfcGeometryBasisCommand,
        *,
        idempotency_key: str,
    ) -> IfcGeometryBasisConfigureResult:
        """Apply one explicit command using a private copy and CURRENT CAS."""

        normalized_key = require_ifc_idempotency_key(idempotency_key)
        request_digest = hashlib.sha256(
            command.model_dump_json().encode("utf-8")
        ).hexdigest()
        state = _ConfigureAttemptState()
        deadline = monotonic() + IFC_REVISION_INVOCATION_EXECUTION.total_seconds
        operation_detail = IfcGeometryBasisConfigureOperationDetail(request=command)
        operation_detail_payload = operation_detail.persisted_payload()

        async def attempt() -> IfcGeometryBasisConfigureResult:
            prepared = state.prepared
            claim = state.claim
            if prepared is None or claim is None:
                raise IfcModelIntegrityError("geometry-basis execution was not claimed")
            try:
                generated = state.generated
                if generated is None:
                    resumed = await self._writer.resume_selected_revision(scope, prepared, claim)
                    if resumed is not None:
                        committed, _ = resumed
                        return IfcGeometryBasisConfigureResult(
                            operation_alias=committed.operation_alias,
                            model=committed.inspection.to_safe_info(),
                        )
                    base = await self._writer.open_prepared_base(scope, prepared)
                    runtime_result = await self._runtime.execute(
                        ConfigureIfcGeometryBasisRevisionRequest(command=command),
                        source=base,
                    )
                    if not isinstance(runtime_result, GeneratedIfcRevision):
                        raise IfcModelIntegrityError(
                            "IFC revision runtime did not generate a geometry-basis revision"
                        )
                    generated = runtime_result
                    state.generated = generated
            except IfcGeometryBasisBusinessError as exc:
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

            committed = await self._writer.commit(scope, prepared, generated, claim=claim)
            return IfcGeometryBasisConfigureResult(
                operation_alias=committed.operation_alias,
                model=committed.inspection.to_safe_info(),
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

        async def supervised() -> IfcGeometryBasisConfigureResult:
            recovered = await self._writer.recover_revision(
                scope, tool_name=_CONFIGURE_TOOL_NAME, idempotency_key=normalized_key,
                request_digest=request_digest,
                intent_summary=IFC_GEOMETRY_BASIS_CONFIGURE_INTENT_SUMMARY,
                operation_detail=operation_detail_payload,
            )
            if recovered is not None:
                if recovered.operation_alias is None:
                    raise IfcModelIntegrityError("successful geometry-basis Operation lost its alias")
                async with self._runtime.invocation_scope(IFC_REVISION_INVOCATION_EXECUTION, deadline=deadline):
                    exact = await self._writer.pin_exact(
                        scope, revision_id=recovered.prepared.after_revision_id,
                    )
                    inspection = await self._writer.inspect_exact(exact)
                return IfcGeometryBasisConfigureResult(
                    operation_alias=recovered.operation_alias,
                    model=inspection.to_safe_info(),
                )
            prepared = await self._writer.prepare(
                scope, tool_name=_CONFIGURE_TOOL_NAME, idempotency_key=normalized_key,
                request_digest=request_digest,
                intent_summary=IFC_GEOMETRY_BASIS_CONFIGURE_INTENT_SUMMARY,
                operation_detail=operation_detail_payload,
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
                        non_retryable_errors=(IfcGeometryBasisBusinessError,),
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
            logical_operation_identity=(
                f"{_CONFIGURE_TOOL_NAME}:{normalized_key}:{request_digest}"
            ),
            operation=supervised,
            deadline=deadline,
        )

    async def list_representation_contexts(
        self,
        scope: IfcRunIdentity,
    ) -> IfcRepresentationContextCatalog:
        """Return the strict Context Catalog from one pinned exact CURRENT."""

        deadline = monotonic() + IFC_REVISION_EXECUTION.total_seconds
        state = _ReadAttemptState()

        async def attempt() -> IfcRepresentationContextCatalog:
            exact = state.exact
            if exact is None:
                exact = await self._writer.pin_current(scope)
                state.exact = exact
            current = await self._writer.read_projection(
                exact, request=InspectExactIfcRevisionRequest(projection="contexts"),
            )
            if current.contexts is None:
                raise IfcModelIntegrityError("IFC runtime omitted the requested Context catalog")
            return current.contexts

        async def pause(last_error: Exception) -> None:
            del last_error
            exact = state.exact
            if exact is None:
                raise IfcModelIntegrityError(
                    "IFC context read failure could not pin an exact revision"
                )
            await self._writer.freeze_exact(
                scope,
                exact,
                reason_code=_RETRY_EXHAUSTED,
            )

        async with self._runtime.invocation_scope(IFC_REVISION_EXECUTION, deadline=deadline):
            return await self._supervisor.invoke(
                attempt, on_exhausted=pause, non_retryable_errors=(IfcRevisionRuntimeError,),
            )


__all__ = ["IfcGeometryBasisService"]
