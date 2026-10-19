"""Application core for durable IFC spatial writes and exact snapshot reads."""

from __future__ import annotations

import hashlib
from time import monotonic
import time
from collections.abc import Callable
from dataclasses import dataclass
from CAD2BIM_backend.domain.runtime_execution import IFC_REVISION_INVOCATION_EXECUTION, IFC_REVISION_EXECUTION

from pydantic import ValidationError

from CAD2BIM_backend.application.ifc_model_service import IfcInvocationSupervisor
from CAD2BIM_backend.application.ifc_revision_writer import IfcRevisionWriter
from CAD2BIM_backend.application.ifc_write_coordinator import IfcWriteCoordinator
from CAD2BIM_backend.application.ifc_write_support import (
    bind_failed_operation,
    require_ifc_idempotency_key,
)
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.data_runtime import (
    IFC_SPATIAL_STRUCTURE_APPLY_INTENT_SUMMARY,
    IfcSpatialStructureApplyOperationDetail,
)
from CAD2BIM_backend.domain.ifc_model import ExactIfcRevision, IfcExecutionClaim, IfcRunIdentity, PreparedIfcRevision
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    CreateIfcSpatialOperation,
    IfcSpatialAppliedItem,
    IfcSpatialCursorError,
    IfcSpatialCursorExpiredError,
    IfcSpatialNotEmptyError,
    IfcSpatialStructureApplyResult,
    IfcSpatialStructureBusinessError,
    IfcSpatialStructureIntegrityError,
    IfcSpatialStructureListQuery,
    IfcSpatialStructureListResult,
)
from CAD2BIM_backend.ports.opaque_cursor import OpaqueCursorCodecError, OpaqueCursorCodecPort
from CAD2BIM_backend.domain.ifc_model import IfcModelIntegrityError
from CAD2BIM_backend.ports.ifc_revision_runtime import (
    ApplyIfcSpatialStructureRevisionRequest,
    GeneratedIfcRevision,
    IfcRevisionRuntimePort,
    IfcRevisionRuntimeError,
    InspectExactIfcRevisionRequest,
    IfcSpatialRevisionEffect,
)


_APPLY_TOOL_NAME = "ifc_spatial_structure_apply"
_LIST_TOOL_NAME = "ifc_spatial_structure_list"
_CURSOR_TTL_SECONDS = 15 * 60
_RETRY_EXHAUSTED = "IFC_INVOCATION_RETRIES_EXHAUSTED"


@dataclass(slots=True)
class _WriteAttemptState:
    prepared: PreparedIfcRevision | None = None
    claim: IfcExecutionClaim | None = None
    generated: GeneratedIfcRevision | None = None


@dataclass(slots=True)
class _ReadAttemptState:
    exact: ExactIfcRevision | None = None


@dataclass(frozen=True, slots=True)
class _Continuation:
    revision_id: str
    query: IfcSpatialStructureListQuery


class IfcSpatialStructureService:
    """Share one spatial Application Core between Direct and future Runner entrypoints."""

    def __init__(
        self,
        writer: IfcRevisionWriter,
        cursor_codec: OpaqueCursorCodecPort,
        runtime: IfcRevisionRuntimePort,
        coordinator: IfcWriteCoordinator,
        *,
        supervisor: IfcInvocationSupervisor | None = None,
        clock: Callable[[], float] = time.time,
        cursor_ttl_seconds: int = _CURSOR_TTL_SECONDS,
    ) -> None:
        if cursor_ttl_seconds < 1:
            raise ValueError("cursor_ttl_seconds must be positive")
        self._writer = writer
        self._cursor_codec = cursor_codec
        self._runtime = runtime
        self._coordinator = coordinator
        self._supervisor = supervisor or IfcInvocationSupervisor()
        self._clock = clock
        self._cursor_ttl_seconds = cursor_ttl_seconds

    async def apply(
        self,
        scope: IfcRunIdentity,
        command: ApplyIfcSpatialStructureCommand,
        *,
        idempotency_key: str,
    ) -> IfcSpatialStructureApplyResult:
        normalized_key = require_ifc_idempotency_key(idempotency_key)
        request_digest = hashlib.sha256(command.model_dump_json().encode("utf-8")).hexdigest()
        detail = IfcSpatialStructureApplyOperationDetail(request=command)
        detail_payload = detail.persisted_payload()
        state = _WriteAttemptState()
        deadline = monotonic() + IFC_REVISION_INVOCATION_EXECUTION.total_seconds

        async def attempt() -> IfcSpatialStructureApplyResult:
            prepared = state.prepared
            claim = state.claim
            if prepared is None or claim is None:
                raise IfcSpatialStructureIntegrityError("spatial execution was not claimed")
            try:
                generated = state.generated
                if generated is None:
                    resumed = await self._writer.resume_selected_revision(scope, prepared, claim)
                    if resumed is not None:
                        committed, selected = resumed
                        if selected.spatial_applied is None or selected.spatial_summary is None:
                            raise IfcSpatialStructureIntegrityError("selected spatial result is missing")
                        return IfcSpatialStructureApplyResult(
                            operation_alias=committed.operation_alias,
                            applied_count=len(selected.spatial_applied),
                            applied=selected.spatial_applied,
                            spatial_summary=selected.spatial_summary,
                        )
                    base = await self._writer.open_prepared_base(scope, prepared)
                    runtime_result = await self._runtime.execute(
                        ApplyIfcSpatialStructureRevisionRequest(command=command),
                        source=base,
                    )
                    if not isinstance(runtime_result, GeneratedIfcRevision):
                        raise IfcSpatialStructureIntegrityError(
                            "IFC revision runtime did not generate a spatial revision"
                        )
                    generated = runtime_result
                    state.generated = generated
            except IfcSpatialStructureBusinessError as exc:
                blockers = exc.blockers if isinstance(exc, IfcSpatialNotEmptyError) else None
                operation_alias = await self._writer.fail_prepared(
                    scope,
                    prepared,
                    claim=claim,
                    error_code=exc.code,
                    error_message=str(exc),
                    spatial_blockers=blockers,
                )
                diagnostic = getattr(exc, "diagnostic", None)
                if isinstance(exc, IfcSpatialNotEmptyError):
                    diagnostic = exc.blockers.model_dump(mode="json")
                raise bind_failed_operation(
                    exc,
                    operation_alias=operation_alias,
                    diagnostic=diagnostic,
                )

            effect = generated.effect
            if not isinstance(effect, IfcSpatialRevisionEffect):
                raise IfcSpatialStructureIntegrityError(
                    "IFC revision runtime omitted the spatial result"
                )

            created = {item.client_ref: item.global_id for item in effect.created}
            expected_refs = {
                operation.client_ref
                for operation in command.operations
                if isinstance(operation, CreateIfcSpatialOperation)
            }
            if set(created) != expected_refs:
                raise IfcSpatialStructureIntegrityError(
                    "compiler create bindings do not match the committed command"
                )
            saved_by_id = {item.global_id: item for item in effect.spatial_summary}
            if any(global_id not in saved_by_id for global_id in created.values()):
                raise IfcSpatialStructureIntegrityError(
                    "a created spatial object is absent from the committed revision"
                )
            applied = tuple(
                IfcSpatialAppliedItem(
                    action=operation.action,
                    global_id=(
                        created[operation.client_ref]
                        if isinstance(operation, CreateIfcSpatialOperation)
                        else operation.target_global_id
                    ),
                    client_ref=(
                        operation.client_ref
                        if isinstance(operation, CreateIfcSpatialOperation)
                        else None
                    ),
                    target_global_id=(
                        None
                        if isinstance(operation, CreateIfcSpatialOperation)
                        else operation.target_global_id
                    ),
                )
                for operation in command.operations
            )
            committed = await self._writer.commit(
                scope, prepared, generated, claim=claim,
                spatial_applied=applied,
                spatial_summary=effect.spatial_summary,
            )
            return IfcSpatialStructureApplyResult(
                operation_alias=committed.operation_alias,
                applied_count=len(applied),
                applied=applied,
                spatial_summary=effect.spatial_summary,
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

        async def supervised() -> IfcSpatialStructureApplyResult:
            recovered = await self._writer.recover_revision(
                scope, tool_name=_APPLY_TOOL_NAME, idempotency_key=normalized_key,
                request_digest=request_digest,
                intent_summary=IFC_SPATIAL_STRUCTURE_APPLY_INTENT_SUMMARY,
                operation_detail=detail_payload,
            )
            if recovered is not None:
                if recovered.spatial_result is None or recovered.operation_alias is None:
                    raise IfcSpatialStructureIntegrityError(
                        "successful spatial Operation lost its result"
                    )
                if recovered.spatial_result.operation_alias != recovered.operation_alias:
                    raise IfcSpatialStructureIntegrityError(
                        "spatial result alias differs from its Operation"
                    )
                return recovered.spatial_result
            prepared = await self._writer.prepare(
                scope, tool_name=_APPLY_TOOL_NAME, idempotency_key=normalized_key,
                request_digest=request_digest,
                intent_summary=IFC_SPATIAL_STRUCTURE_APPLY_INTENT_SUMMARY,
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
                        non_retryable_errors=(IfcSpatialStructureBusinessError,),
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
                f"{_APPLY_TOOL_NAME}:{normalized_key}:{request_digest}"
            ),
            operation=supervised,
            deadline=deadline,
        )

    async def list(
        self,
        scope: IfcRunIdentity,
        query: IfcSpatialStructureListQuery | None,
        cursor: str | None,
    ) -> IfcSpatialStructureListResult:
        deadline = monotonic() + IFC_REVISION_EXECUTION.total_seconds
        if (query is None) == (cursor is None):
            raise ValueError("exactly one of query or cursor is required")
        if query is not None and query.after is not None:
            raise ValueError("query.after is server-only")

        continuation = None if cursor is None else self._decode_cursor(scope, cursor)
        effective_query = query if continuation is None else continuation.query
        assert effective_query is not None
        state = _ReadAttemptState()

        async def attempt() -> IfcSpatialStructureListResult:
            if state.exact is None:
                state.exact = (
                    await self._writer.pin_current(scope)
                    if continuation is None
                    else await self._writer.pin_exact(
                        scope,
                        revision_id=continuation.revision_id,
                    )
                )
            verified = await self._writer.read_projection(
                state.exact,
                request=InspectExactIfcRevisionRequest(projection="spatial", spatial_query=effective_query),
            )
            page = verified.spatial_page
            if page is None:
                raise IfcModelIntegrityError("IFC runtime omitted the requested Spatial page")
            next_cursor = None
            if page.next_after is not None:
                next_cursor = self._encode_cursor(
                    scope,
                    revision_id=state.exact.revision_id,
                    query=effective_query.model_copy(update={"after": page.next_after}),
                )
            return IfcSpatialStructureListResult(
                items=page.items,
                returned_count=page.returned_count,
                next_cursor=next_cursor,
            )

        async def pause(last_error: Exception) -> None:
            del last_error
            if state.exact is not None:
                await self._writer.freeze_exact(
                    scope,
                    state.exact,
                    reason_code=_RETRY_EXHAUSTED,
                )

        async with self._runtime.invocation_scope(IFC_REVISION_EXECUTION, deadline=deadline):
            return await self._supervisor.invoke(
                attempt, on_exhausted=pause, non_retryable_errors=(IfcRevisionRuntimeError,),
            )

    def _encode_cursor(
        self,
        scope: IfcRunIdentity,
        *,
        revision_id: str,
        query: IfcSpatialStructureListQuery,
    ) -> str:
        claims = {
            "tool": _LIST_TOOL_NAME,
            "scope": _scope_digest(scope),
            "revision_id": revision_id,
            "sort": ["ifc_class", "global_id"],
            "query": query.model_dump(mode="json"),
            "expires_at": int(self._clock()) + self._cursor_ttl_seconds,
        }
        try:
            return self._cursor_codec.encode(claims)
        except OpaqueCursorCodecError as exc:
            raise IfcSpatialCursorError("the spatial continuation cannot be encoded") from exc

    def _decode_cursor(self, scope: IfcRunIdentity, token: str) -> _Continuation:
        try:
            claims = self._cursor_codec.decode(token)
            if set(claims) != {
                "tool", "scope", "revision_id", "sort", "query", "expires_at"
            }:
                raise ValueError
            expires_at = claims["expires_at"]
            if (
                claims["tool"] != _LIST_TOOL_NAME
                or claims["scope"] != _scope_digest(scope)
                or not isinstance(claims["revision_id"], str)
                or not claims["revision_id"]
                or claims["sort"] != ["ifc_class", "global_id"]
                or not isinstance(expires_at, int)
                or isinstance(expires_at, bool)
            ):
                raise ValueError
            query = IfcSpatialStructureListQuery.model_validate(claims["query"])
            if query.after is None:
                raise ValueError
        except (OpaqueCursorCodecError, KeyError, TypeError, ValueError, ValidationError) as exc:
            raise IfcSpatialCursorError("the spatial cursor is invalid") from exc
        if self._clock() >= expires_at:
            raise IfcSpatialCursorExpiredError(
                "the spatial cursor expired; restart from the first page"
            )
        return _Continuation(revision_id=claims["revision_id"], query=query)


def _scope_digest(scope: IfcRunIdentity) -> str:
    return hashlib.sha256(canonical_json(scope.model_dump(mode="json")).encode()).hexdigest()


__all__ = ["IfcSpatialStructureService"]
