"""Reusable durable writer for non-initial IFC model revisions."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from contextlib import asynccontextmanager
from time import monotonic
from CAD2BIM_backend.application.async_settlement import drain_shielded_task as _drain_shielded_task
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass

from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationErrorPhase,
    CriticalOperationStatus,
    ProductReportPublication,
)
from CAD2BIM_backend.domain.ifc_model import (
    ExactIfcArtifactVersion,
    ExactIfcRevision,
    IfcExecutionClaim,
    IFC_REVISION_CONTENT_TYPE,
    IFC_REVISION_FILENAME,
    IfcModelConflictError,
    IfcModelFrozenError,
    IfcModelInitializeResult,
    IfcModelIntegrityError,
    IfcCommittedVerificationError,
    IfcModelState,
    IfcSettledOperationError,
    IfcRevisionInspection,
    IfcRunIdentity,
    PreparedIfcInitialize,
    PreparedIfcRevision,
    VerifiedIfcPublication,
)
from CAD2BIM_backend.domain.runtime_execution import RuntimeExecutionPolicy
from CAD2BIM_backend.ports.ifc_model import (
    CommittedIfcRevision,
    IfcInitializeOperationState,
    IfcModelRepository,
    IfcRevisionOperationState,
    IfcSelectedCandidate,
)
from CAD2BIM_backend.ports.ifc_revision_reader import (
    IfcRevisionReaderPort,
    VerifiedExactIfcRevision,
)
from CAD2BIM_backend.ports.ifc_revision_runtime import (
    GeneratedIfcRevision,
    IfcRevisionChunkSource,
    IfcRevisionRuntimePort,
    InspectedIfcRevision,
    InspectExactIfcRevisionRequest,
)
from CAD2BIM_backend.ports.storage import (
    StorageConflictError, StorageKey, StoragePort, StoredObject,
)
from CAD2BIM_backend.domain.ifc_project import IfcProjectSummary
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    IfcSpatialAppliedItem,
    IfcSpatialBlockerSummary,
    IfcSpatialNodeSummary,
)


@dataclass(frozen=True, slots=True)
class VerifiedCommittedIfcRevision:
    """Committed exact identity and independently inspected IFC payload."""

    operation_alias: str
    exact: ExactIfcRevision
    inspection: IfcRevisionInspection


@dataclass(frozen=True, slots=True)
class VerifiedInitializedIfcRevision:
    """Committed initialization result and independently inspected revision 1."""

    result: IfcModelInitializeResult
    exact: ExactIfcRevision
    inspection: IfcRevisionInspection


@dataclass(frozen=True, slots=True)
class _StoredIfcRevisionSource:
    storage: StoragePort
    key: StorageKey
    expected: StoredObject
    size_bytes: int
    sha256: str

    def iter_chunks(self, *, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        return self.storage.iter_chunks(
            self.key,
            chunk_size=chunk_size,
            expected=self.expected,
        )


@dataclass(slots=True)
class _SourceIfcCandidate:
    """Borrow exact immutable source bytes through the existing candidate lifecycle.

    The caller owns the durable source. Closing this short-lived capability
    prevents further candidate reads; it does not close or mutate that source.
    """

    source: IfcRevisionChunkSource
    closed: bool = False

    @property
    def size_bytes(self) -> int:
        return self.source.size_bytes

    @property
    def sha256(self) -> str:
        return self.source.sha256

    async def iter_chunks(self, *, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        if self.closed:
            raise IfcModelIntegrityError("IFC source candidate is closed")
        async for chunk in self.source.iter_chunks(chunk_size=chunk_size):
            if self.closed:
                raise IfcModelIntegrityError("IFC source candidate is closed")
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class IfcRevisionWriter:
    """Publish and atomically advance one prepared non-initial IFC revision."""

    def __init__(
        self,
        repository: IfcModelRepository,
        storage: StoragePort,
        reader: IfcRevisionReaderPort,
        runtime: IfcRevisionRuntimePort,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._reader = reader
        self._runtime = runtime

    async def claim_execution(
        self, scope: IfcRunIdentity, prepared: PreparedIfcInitialize | PreparedIfcRevision,
    ) -> IfcExecutionClaim:
        return await self._repository.claim_execution(
            scope, operation_id=prepared.operation_id,
            request_digest=prepared.request_digest,
        )

    def guard_registration(
        self, scope: IfcRunIdentity, claim: IfcExecutionClaim,
    ) -> Callable[[int, int], Awaitable[None]]:
        async def register(guardian_pid: int, guardian_birth_us: int) -> None:
            await self._repository.associate_execution_guard(
                scope, claim, guardian_pid=guardian_pid,
                guardian_birth_us=guardian_birth_us,
            )
        return register

    async def retire_execution(
        self, scope: IfcRunIdentity, claim: IfcExecutionClaim,
    ) -> None:
        await self._repository.retire_execution(scope, claim)

    @asynccontextmanager
    async def execution_invocation(
        self, scope: IfcRunIdentity, claim: IfcExecutionClaim,
        policy: RuntimeExecutionPolicy,
    ):
        """Keep a claimed O until its bounded Runtime and children have stopped."""
        try:
            async with self._runtime.invocation_scope(
                policy, deadline=monotonic() + claim.remaining_seconds,
                on_guard_registered=self.guard_registration(scope, claim),
            ):
                yield
        finally:
            await self.retire_execution(scope, claim)

    async def require_recovery_allowed(self, scope: IfcRunIdentity) -> None:
        """Check original Run write authority without bypassing or blocking reconciliation."""
        await self._repository.require_recovery_allowed(scope)

    async def recover_revision(
        self,
        scope: IfcRunIdentity,
        *,
        tool_name: str,
        idempotency_key: str,
        request_digest: str,
        intent_summary: str,
        operation_detail: Mapping[str, object],
    ) -> IfcRevisionOperationState | None:
        """Return a settled original write before any prepare or mutation Worker."""

        state = await self._repository.get_revision_operation_state(
            scope, tool_name=tool_name, idempotency_key=idempotency_key,
            request_digest=request_digest, intent_summary=intent_summary,
            operation_detail=operation_detail,
        )
        if state is None or state.status in {
            CriticalOperationStatus.PREPARED, CriticalOperationStatus.INDETERMINATE,
        }:
            return None
        if state.status is CriticalOperationStatus.FAILED:
            if tool_name in {"ifc_products_apply", "ifc_products_edit"}:
                return state
            self.raise_settled_failure(state)
        if state.operation_alias is None:
            raise IfcModelIntegrityError("successful IFC Operation lost its catalog alias")
        exact = await self._repository.get_exact(
            scope, revision_id=state.prepared.after_revision_id,
        )
        if exact is None:
            raise IfcModelIntegrityError("successful IFC Operation lost its exact revision")
        self._verify_reconciled_exact(exact, state.prepared)
        if tool_name in {"ifc_products_apply", "ifc_products_edit", "ifc_trusted_baseline_load"}:
            try:
                await self.inspect_exact(exact)
            except Exception as exc:
                await self.freeze_exact(scope, exact, reason_code="IFC_COMMITTED_READBACK_FAILED")
                raise IfcCommittedVerificationError(
                    "The original Operation committed, but its exact IFC readback remains unavailable",
                    operation_alias=state.operation_alias, revision_id=exact.revision_id,
                ) from exc
        return state

    @staticmethod
    def raise_settled_failure(
        state: IfcInitializeOperationState | IfcRevisionOperationState,
    ) -> None:
        if (state.error_code is None or state.error_message is None
                or state.error_phase is None or state.operation_alias is None):
            raise IfcModelIntegrityError("failed IFC Operation lost its original error")
        raise IfcSettledOperationError(
            code=state.error_code, message=state.error_message,
            phase=state.error_phase.value, operation_alias=state.operation_alias,
            diagnostic=(state.failure_blockers.model_dump(mode="json")
                        if isinstance(state, IfcRevisionOperationState)
                        and state.failure_blockers is not None else None),
        )

    async def prepare(
        self,
        scope: IfcRunIdentity,
        *,
        tool_name: str,
        idempotency_key: str,
        request_digest: str,
        intent_summary: str,
        operation_detail: Mapping[str, object],
    ) -> PreparedIfcRevision:
        """Fix one logical Operation and its exact CURRENT/CAS frontier."""

        try:
            await self._repository.require_invocation_allowed(scope)
        except IfcModelFrozenError as frozen:
            await self._reconcile_frozen_prepare(
                scope,
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                intent_summary=intent_summary,
                operation_detail=operation_detail,
                frozen=frozen,
            )
            await self._repository.require_invocation_allowed(scope)
        return await self._repository.prepare_revision(
            scope,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            intent_summary=intent_summary,
            operation_detail=operation_detail,
        )

    async def _reconcile_frozen_prepare(
        self,
        scope: IfcRunIdentity,
        *,
        tool_name: str,
        idempotency_key: str,
        request_digest: str,
        intent_summary: str,
        operation_detail: Mapping[str, object],
        frozen: IfcModelFrozenError,
    ) -> None:
        """Recover only the exact logical write that caused a fail-closed pause."""

        state = await self._repository.get_revision_operation_state(
            scope,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            intent_summary=intent_summary,
            operation_detail=operation_detail,
        )
        if state is None:
            raise frozen
        prepared = state.prepared
        if state.status is CriticalOperationStatus.FAILED:
            raise IfcModelConflictError("IFC write Operation is already terminal")
        if state.status is CriticalOperationStatus.PREPARED:
            raise frozen
        if state.status is CriticalOperationStatus.INDETERMINATE:
            try:
                exact = await self._repository.get_exact(
                    scope,
                    revision_id=prepared.after_revision_id,
                )
            except Exception as cause:
                raise IfcModelFrozenError(
                    "IFC write remains paused because commit evidence is unreadable"
                ) from cause
            if exact is not None:
                raise IfcModelFrozenError(
                    "IFC write remains paused because commit evidence is contradictory"
                )
            await self._repository.reopen_indeterminate_revision(
                scope,
                operation_id=prepared.operation_id,
            )
            return
        if state.status is not CriticalOperationStatus.SUCCEEDED:
            raise frozen

        try:
            exact = await self._repository.get_exact(
                scope,
                revision_id=prepared.after_revision_id,
            )
            if exact is None:
                raise IfcModelIntegrityError("successful IFC write lost its exact revision")
            self._verify_reconciled_exact(exact, prepared)
            await self.inspect_exact(exact)
            await self._repository.unfreeze_succeeded_revision(
                scope,
                operation_id=prepared.operation_id,
                revision_id=prepared.after_revision_id,
            )
        except Exception as cause:
            if isinstance(cause, IfcModelFrozenError):
                raise
            raise IfcModelFrozenError(
                "IFC write remains paused because exact evidence is invalid"
            ) from cause


    async def open_prepared_base(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
    ) -> IfcRevisionChunkSource:
        """Open the prepared immutable base without loading it into Backend memory."""

        exact = await self._repository.get_exact(
            scope,
            revision_id=prepared.before.revision_id,
        )
        if exact != prepared.before:
            raise IfcModelConflictError("prepared IFC base revision changed")
        return await self._reader.open_exact(exact)

    async def preflight_prepared_product_body(self, prepared: PreparedIfcRevision):
        """Check actual Body facts tied to the already verified prepared revision."""
        from CAD2BIM_backend.domain.ifc_product_compilation import preflight_product_body_facts

        inspection = await self._repository.get_revision_inspection(prepared.before)
        if inspection is None:
            # Reusable facts are an optimization, not execution eligibility.
            # Read exact bytes with the current inspector when none were saved.
            inspection = await self.inspect_exact(prepared.before)
        if inspection.product_body_contexts is None:
            raise IfcModelIntegrityError("current IFC inspector omitted Body context facts")
        return preflight_product_body_facts(
            inspection.product_body_contexts, inspection.geometry_basis,
            inspection.project_length_unit_m,
        )

    async def read_current(
        self,
        scope: IfcRunIdentity,
    ) -> VerifiedExactIfcRevision:
        """Pin and verify CURRENT for a server-owned geometry/model reader."""

        exact = await self.pin_current(scope)
        return await self.read_exact(exact)

    async def pin_current(
        self,
        scope: IfcRunIdentity,
    ) -> ExactIfcRevision:
        """Pin CURRENT before any fallible Storage or IFC inspection work."""

        return await self._reader.pin_current(scope)

    async def pin_exact(
        self,
        scope: IfcRunIdentity,
        *,
        revision_id: str,
    ) -> ExactIfcRevision:
        """Resolve one server-owned immutable revision identity for exact continuation reads."""

        return await self._reader.pin_exact(scope, revision_id=revision_id)

    async def read_exact(
        self,
        exact: ExactIfcRevision,
    ) -> VerifiedExactIfcRevision:
        """Verify one previously pinned immutable revision."""

        return await self._reader.read_exact(exact)

    async def commit_trusted_baseline(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
        source: IfcRevisionChunkSource,
        *,
        claim: IfcExecutionClaim,
    ) -> VerifiedCommittedIfcRevision:
        """Inspect a registered trusted source, then use the normal revision commit.

        Admission and exact source identity belong to the calling Application
        and Repository. The caller resumes an already selected candidate before
        resolving this source, just as for other prepared revision writes.
        """
        candidate = _SourceIfcCandidate(source)
        try:
            inspection = await self.inspect_source(candidate)
            generated = GeneratedIfcRevision(candidate=candidate, inspection=inspection)
            return await self.commit(scope, prepared, generated, claim=claim)
        finally:
            await candidate.aclose()

    async def commit(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
        generated: GeneratedIfcRevision,
        *,
        claim: IfcExecutionClaim,
        project_result: IfcProjectSummary | None = None,
        spatial_applied: tuple[IfcSpatialAppliedItem, ...] | None = None,
        spatial_summary: tuple[IfcSpatialNodeSummary, ...] | None = None,
        product_report: ProductReportPublication | None = None,
        product_failure_report: ProductReportPublication | None = None,
    ) -> VerifiedCommittedIfcRevision:
        """Publish, CAS commit, response-loss reconcile, and exact reread."""

        self._validate_candidate(prepared.before.project_global_id, generated)
        selected, staged = await self._stage_and_select(
            scope, prepared, claim, generated,
            project_result=project_result,
            spatial_applied=spatial_applied, spatial_summary=spatial_summary,
            product_report=product_report, product_failure_report=product_failure_report,
        )
        return await self._finish_selected_revision(scope, prepared, claim, selected, staged=staged)

    async def _finish_selected_revision(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
        claim: IfcExecutionClaim,
        selected: IfcSelectedCandidate,
        *,
        staged: StoredObject | None = None,
    ) -> VerifiedCommittedIfcRevision:
        publication = await self._publish_selected(scope, prepared, selected, staged=staged)
        settlement = asyncio.create_task(
            self._settle_revision_commit(
                scope, prepared, publication,
                claim=claim,
                same_call_inspection=selected.inspection if staged is not None else None,
                project_result=selected.project_result,
                spatial_applied=selected.spatial_applied,
                spatial_summary=selected.spatial_summary,
            )
        )
        try:
            return await asyncio.shield(settlement)
        except asyncio.CancelledError as cancellation:
            try:
                await self._finish_cancelled_settlement(
                    settlement,
                    scope,
                    model_id=prepared.before.model_id,
                    revision_id=None,
                    operation_id=prepared.operation_id,
                    reason_code="IFC_COMMIT_CANCELLED_UNCERTAIN",
                    claim=claim,
                )
            except BaseException as settlement_failure:
                raise cancellation from settlement_failure
            raise

    async def commit_initialize(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcInitialize,
        generated: GeneratedIfcRevision,
        *,
        claim: IfcExecutionClaim,
    ) -> VerifiedInitializedIfcRevision:
        """Publish and atomically commit one create-only IFC revision 1."""

        self._validate_candidate(prepared.project_global_id, generated)
        selected, staged = await self._stage_and_select(scope, prepared, claim, generated)
        return await self._finish_selected_initialize(scope, prepared, claim, selected, staged=staged)

    async def _finish_selected_initialize(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcInitialize,
        claim: IfcExecutionClaim,
        selected: IfcSelectedCandidate,
        *,
        staged: StoredObject | None = None,
    ) -> VerifiedInitializedIfcRevision:
        publication = await self._publish_selected(scope, prepared, selected, staged=staged)
        settlement = asyncio.create_task(
            self._settle_initialize_commit(scope, prepared, publication, claim=claim)
        )
        try:
            return await asyncio.shield(settlement)
        except asyncio.CancelledError as cancellation:
            try:
                await self._finish_cancelled_settlement(
                    settlement,
                    scope,
                    model_id=None,
                    revision_id=prepared.revision_id,
                    operation_id=prepared.operation_id,
                    reason_code="IFC_INITIALIZE_COMMIT_CANCELLED_UNCERTAIN",
                    claim=claim,
                )
            except BaseException as settlement_failure:
                raise cancellation from settlement_failure
            raise

    async def resume_selected_revision(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
        claim: IfcExecutionClaim,
    ) -> tuple[VerifiedCommittedIfcRevision, IfcSelectedCandidate] | None:
        """Continue the original selected bytes before starting another mutation Worker."""
        selected = await self._repository.get_selected_candidate(scope, prepared, claim)
        if selected is None:
            return None
        committed = await self._finish_selected_revision(scope, prepared, claim, selected)
        return committed, selected

    async def resume_selected_initialize(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcInitialize,
        claim: IfcExecutionClaim,
    ) -> VerifiedInitializedIfcRevision | None:
        selected = await self._repository.get_selected_candidate(scope, prepared, claim)
        if selected is None:
            return None
        return await self._finish_selected_initialize(scope, prepared, claim, selected)

    async def _settle_revision_commit(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
        publication: VerifiedIfcPublication,
        *,
        claim: IfcExecutionClaim,
        same_call_inspection: IfcRevisionInspection | None = None,
        project_result: IfcProjectSummary | None = None,
        spatial_applied: tuple[IfcSpatialAppliedItem, ...] | None = None,
        spatial_summary: tuple[IfcSpatialNodeSummary, ...] | None = None,
    ) -> VerifiedCommittedIfcRevision:
        try:
            committed = await self._repository.commit_revision(
                scope,
                prepared=prepared,
                publication=publication,
                claim=claim,
                project_result=project_result,
                spatial_applied=spatial_applied,
                spatial_summary=spatial_summary,
            )
        except Exception as cause:
            committed = await self._reconcile_commit(
                scope,
                prepared,
                publication,
                cause=cause,
                claim=claim,
                project_result=project_result,
                spatial_applied=spatial_applied,
                spatial_summary=spatial_summary,
            )
        return await self._verify_committed(
            scope, prepared, publication, committed,
            same_call_inspection=same_call_inspection,
        )

    async def _settle_initialize_commit(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcInitialize,
        publication: VerifiedIfcPublication,
        *,
        claim: IfcExecutionClaim,
    ) -> VerifiedInitializedIfcRevision:
        try:
            result = await self._repository.commit_initialize(
                scope,
                prepared=prepared,
                publication=publication,
                claim=claim,
            )
        except Exception as cause:
            result = await self._reconcile_initialize_commit(
                scope,
                prepared,
                publication,
                cause=cause,
                claim=claim,
            )
        exact = await self._repository.get_exact(scope, revision_id=prepared.revision_id)
        if exact is None:
            raise IfcModelIntegrityError("committed IFC revision is missing")
        self._verify_exact_initialize(exact, prepared, publication)
        inspection = await self.inspect_exact(
            exact,
            expected_revision1_project_name=publication.inspection.project_name,
        )
        if not inspection.matches_recorded_facts(publication.inspection) or result.model != inspection.to_safe_info():
            raise IfcModelIntegrityError(
                "committed IFC initialization differs from the validated candidate"
            )
        return VerifiedInitializedIfcRevision(
            result=result,
            exact=exact,
            inspection=inspection,
        )

    async def _finish_cancelled_settlement(
        self,
        settlement: asyncio.Task[object],
        scope: IfcRunIdentity,
        *,
        model_id: str | None,
        revision_id: str | None,
        operation_id: str,
        reason_code: str,
        claim: IfcExecutionClaim,
    ) -> None:
        """Reach a durable terminal state after the caller loses its response."""

        settlement_failure = await _drain_shielded_task(settlement)
        if settlement_failure is None:
            return
        if model_id is None and revision_id is not None:
            model_lookup = asyncio.create_task(
                self._repository.get_exact(scope, revision_id=revision_id)
            )
            if await _drain_shielded_task(model_lookup) is None:
                exact = model_lookup.result()
                model_id = exact.model_id if exact is not None else None
        freeze = asyncio.create_task(
            self._repository.freeze_indeterminate(
                scope,
                model_id=model_id,
                operation_id=operation_id,
                reason_code=reason_code,
                claim=claim,
            )
        )
        freeze_failure = await _drain_shielded_task(freeze)
        if freeze_failure is not None:
            raise IfcModelIntegrityError(
                "cancelled IFC commit could not reach a durable terminal state"
            ) from freeze_failure

    async def fail_prepared(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
        *,
        claim: IfcExecutionClaim,
        error_code: str,
        error_message: str,
        spatial_blockers: IfcSpatialBlockerSummary | None = None,
        product_report: ProductReportPublication | None = None,
    ) -> str:
        """Close a deterministic candidate rejection without publishing a revision."""

        if product_report is not None:
            await self._verify_report_storage(product_report)
        return await self._repository.record_failed_ifc_operation(
            scope,
            operation_id=prepared.operation_id,
            error_code=error_code,
            error_message=error_message,
            error_phase=CriticalOperationErrorPhase.IFC_VALIDATION,
            claim=claim,
            spatial_blockers=spatial_blockers,
            **({"product_report": product_report} if product_report is not None else {}),
        )

    async def freeze_indeterminate(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
        *,
        reason_code: str,
        claim: IfcExecutionClaim,
    ) -> None:
        """Fail closed after technical retries leave commit state unresolved."""

        await self._repository.freeze_indeterminate(
            scope,
            model_id=prepared.before.model_id,
            operation_id=prepared.operation_id,
            reason_code=reason_code,
            claim=claim,
        )

    async def freeze_exact(
        self,
        scope: IfcRunIdentity,
        exact: ExactIfcRevision,
        *,
        reason_code: str,
    ) -> None:
        """Fail closed after a committed exact read exhausts technical retries."""

        await self._repository.freeze_indeterminate(
            scope,
            model_id=exact.model_id,
            operation_id=None,
            reason_code=reason_code,
        )

    async def _stage_and_select(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcInitialize | PreparedIfcRevision,
        claim: IfcExecutionClaim,
        generated: GeneratedIfcRevision,
        *,
        project_result: IfcProjectSummary | None = None,
        spatial_applied: tuple[IfcSpatialAppliedItem, ...] | None = None,
        spatial_summary: tuple[IfcSpatialNodeSummary, ...] | None = None,
        product_report: ProductReportPublication | None = None,
        product_failure_report: ProductReportPublication | None = None,
    ) -> tuple[IfcSelectedCandidate, StoredObject | None]:
        existing = await self._repository.get_selected_candidate(scope, prepared, claim)
        if existing is not None:
            if (existing.project_result != project_result
                    or existing.spatial_applied != spatial_applied
                    or existing.spatial_summary != spatial_summary
                    or existing.product_report != product_report
                    or existing.product_failure_report != product_failure_report):
                raise IfcModelConflictError("selected IFC business result cannot change")
            return existing, None
        origin_generation = claim.generation
        upload_id = secrets.token_hex(16)
        key = _candidate_storage_key(scope, prepared.operation_id, origin_generation, upload_id)
        written = await self._storage.write(
            key,
            filename=IFC_REVISION_FILENAME,
            content_type=IFC_REVISION_CONTENT_TYPE,
            chunks=generated.candidate.iter_chunks(),
        )
        stored = await self._storage.stat(key)
        if stored.key != key or stored != written:
            raise IfcModelIntegrityError("private IFC candidate metadata changed after staging")
        self._validate_stored_media(stored)
        if (
            stored.size_bytes != generated.candidate.size_bytes
            or stored.sha256 != generated.candidate.sha256
        ):
            raise IfcModelIntegrityError("private IFC candidate bytes changed after staging")
        source = _StoredIfcRevisionSource(
            storage=self._storage,
            key=key,
            expected=stored,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
        )
        # The compiler already reopened these candidate bytes and inspected
        # their IFC facts. Reuse that same-call result only after checking the
        # actual stored bytes; another IFC parse cannot establish a new fact
        # about identical bytes. Recovery without this live result still
        # independently parses its selected and committed exact IFC.
        digest = hashlib.sha256()
        size = 0
        async for chunk in source.iter_chunks():
            if not isinstance(chunk, bytes) or not chunk:
                raise IfcModelIntegrityError("private IFC candidate yielded invalid bytes")
            size += len(chunk)
            if size > stored.size_bytes:
                raise IfcModelIntegrityError("private IFC candidate exceeds its stored size")
            digest.update(chunk)
        inspection = generated.inspection
        if size != inspection.size_bytes or digest.hexdigest() != inspection.sha256:
            raise IfcModelIntegrityError("private IFC candidate bytes do not match metadata")
        proposed = IfcSelectedCandidate(
            origin_generation=origin_generation, upload_id=upload_id,
            size_bytes=stored.size_bytes, sha256=stored.sha256,
            inspection=inspection,
            project_result=project_result, spatial_applied=spatial_applied,
            spatial_summary=spatial_summary,
            product_report=product_report,
            product_failure_report=product_failure_report,
        )
        for report in (product_report, product_failure_report):
            if report is not None:
                await self._verify_report_storage(report)
        selected = await self._repository.select_candidate(scope, prepared, claim, proposed)
        if selected != proposed:
            raise IfcModelIntegrityError("IFC candidate selection changed the staged bytes")
        return selected, stored

    async def _publish_selected(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcInitialize | PreparedIfcRevision,
        selected: IfcSelectedCandidate,
        *,
        staged: StoredObject | None,
    ) -> VerifiedIfcPublication:
        if staged is None:
            for report in (selected.product_report, selected.product_failure_report):
                if report is not None:
                    await self._verify_report_storage(report)
        private_key = _candidate_storage_key(
            scope, prepared.operation_id, selected.origin_generation, selected.upload_id,
        )
        stored = await self._storage.stat(private_key)
        self._validate_stored_media(stored)
        if (stored.key != private_key or stored.size_bytes != selected.size_bytes
                or stored.sha256 != selected.sha256 or
                (staged is not None and stored != staged)):
            raise IfcModelIntegrityError("selected IFC private storage changed")
        source = _StoredIfcRevisionSource(
            storage=self._storage, key=private_key, expected=stored,
            size_bytes=stored.size_bytes, sha256=stored.sha256,
        )
        inspection = selected.inspection
        if staged is None:
            inspection = await self.inspect_source(source)
            if not inspection.matches_recorded_facts(selected.inspection):
                raise IfcModelIntegrityError("selected IFC private bytes failed recovery inspection")
        final_key = _storage_key(scope, prepared.artifact_id)
        try:
            written = await self._storage.write(
                final_key,
                filename=IFC_REVISION_FILENAME,
                content_type=IFC_REVISION_CONTENT_TYPE,
                chunks=source.iter_chunks(),
            )
        except StorageConflictError as exc:
            raise IfcModelIntegrityError("formal IFC key conflicts with selected bytes") from exc
        final = await self._storage.stat(final_key)
        self._validate_stored_media(final)
        if (final.key != final_key or final != written
                or final.size_bytes != selected.size_bytes
                or final.sha256 != selected.sha256):
            raise IfcModelIntegrityError("formal IFC bytes differ from selected candidate")
        return VerifiedIfcPublication(
            scope=scope,
            artifact=ExactIfcArtifactVersion(
                artifact_id=prepared.artifact_id,
                version_id=prepared.artifact_version_id,
                filename=final.filename,
                content_type=final.content_type,
                size_bytes=final.size_bytes,
                sha256=final.sha256,
            ),
            inspection=inspection,
            storage_created_at=final.created_at,
        )

    async def _verify_report_storage(self, report: ProductReportPublication) -> None:
        """Verify the private bytes consumed by the joint publication transaction."""
        for artifact in report.artifacts:
            key = StorageKey(artifact.owner_id, artifact.task_id, artifact.artifact_id, 1)
            stored = await self._storage.stat(key)
            if (stored.filename != artifact.filename or stored.content_type != artifact.content_type
                    or stored.size_bytes != artifact.size_bytes or stored.sha256 != artifact.sha256):
                raise IfcModelIntegrityError("private product report metadata changed")

    async def inspect_source(
        self,
        source: IfcRevisionChunkSource,
    ) -> IfcRevisionInspection:
        result = await self._runtime.execute(
            InspectExactIfcRevisionRequest(),
            source=source,
        )
        if not isinstance(result, InspectedIfcRevision):
            raise IfcModelIntegrityError("IFC revision runtime returned an invalid inspection")
        return result.inspection

    async def inspect_exact(
        self,
        exact: ExactIfcRevision,
        *,
        expected_revision1_project_name: str | None = None,
    ) -> IfcRevisionInspection:
        result = await self.read_projection(
            exact,
            request=InspectExactIfcRevisionRequest(
                expected_project_global_id=(
                    exact.project_global_id
                    if expected_revision1_project_name is not None
                    else None
                ),
                expected_project_name=expected_revision1_project_name,
            ),
        )
        return result.inspection

    async def read_projection(
        self,
        exact: ExactIfcRevision,
        *,
        request: InspectExactIfcRevisionRequest,
    ) -> InspectedIfcRevision:
        source = await self._reader.open_exact(exact)
        result = await self._runtime.execute(request, source=source)
        if not isinstance(result, InspectedIfcRevision):
            raise IfcModelIntegrityError("IFC revision runtime returned an invalid inspection")
        inspection = result.inspection
        if any(
            (
                inspection.project_global_id != exact.project_global_id,
                inspection.size_bytes != exact.artifact.size_bytes,
                inspection.sha256 != exact.artifact.sha256,
            )
        ):
            raise IfcModelIntegrityError("exact IFC bytes do not match committed metadata")
        return result

    async def _reconcile_commit(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
        publication: VerifiedIfcPublication,
        *,
        cause: Exception,
        claim: IfcExecutionClaim,
        project_result: IfcProjectSummary | None = None,
        spatial_applied: tuple[IfcSpatialAppliedItem, ...] | None = None,
        spatial_summary: tuple[IfcSpatialNodeSummary, ...] | None = None,
    ) -> CommittedIfcRevision:
        try:
            exact = await self._repository.get_exact(
                scope,
                revision_id=prepared.after_revision_id,
            )
        except Exception as reconcile_error:
            raise IfcModelIntegrityError(
                "IFC revision commit could not be reconciled"
            ) from reconcile_error
        if exact is None:
            raise cause
        self._verify_exact_identity(exact, prepared, publication)
        try:
            return await self._repository.commit_revision(
                scope,
                prepared=prepared,
                publication=publication,
                claim=claim,
                project_result=project_result,
                spatial_applied=spatial_applied,
                spatial_summary=spatial_summary,
            )
        except Exception as reconcile_error:
            raise IfcModelIntegrityError(
                "committed IFC revision result could not be replayed"
            ) from reconcile_error

    async def _verify_committed(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcRevision,
        publication: VerifiedIfcPublication,
        committed: CommittedIfcRevision,
        *,
        same_call_inspection: IfcRevisionInspection | None = None,
    ) -> VerifiedCommittedIfcRevision:
        exact = await self._repository.get_exact(
            scope,
            revision_id=prepared.after_revision_id,
        )
        if exact is None:
            raise IfcModelIntegrityError("committed IFC revision is missing")
        if exact != committed.exact:
            raise IfcModelIntegrityError("committed IFC revision replay changed identity")
        self._verify_exact_identity(exact, prepared, publication)
        try:
            if same_call_inspection is None:
                inspection = await self.inspect_exact(exact)
            else:
                # Storage.stat verifies actual final bytes after commit. The
                # same candidate was already independently reopened by its
                # compiler; identical immutable bytes need no second parse.
                await self._reader.open_exact(exact)
                inspection = same_call_inspection
            if not inspection.matches_recorded_facts(publication.inspection):
                raise IfcModelIntegrityError("committed IFC inspection changed after publication")
        except Exception as exc:
            await self.freeze_exact(scope, exact, reason_code="IFC_COMMITTED_READBACK_FAILED")
            raise IfcCommittedVerificationError(
                "The original Operation committed, but its exact IFC readback failed; inspect that Operation before recovery",
                operation_alias=committed.operation_alias, revision_id=exact.revision_id,
            ) from exc
        return VerifiedCommittedIfcRevision(
            operation_alias=committed.operation_alias,
            exact=exact,
            inspection=inspection,
        )

    @staticmethod
    def _validate_candidate(
        expected_project_global_id: str,
        generated: GeneratedIfcRevision,
    ) -> None:
        inspection = generated.inspection
        if (
            inspection.size_bytes != generated.candidate.size_bytes
            or inspection.sha256 != generated.candidate.sha256
        ):
            raise IfcModelIntegrityError(
                "candidate IFC inspection is not independently reproducible"
            )
        if inspection.project_global_id != expected_project_global_id:
            raise IfcModelIntegrityError("candidate IFC changed the IfcProject GlobalId")

    async def _reconcile_initialize_commit(
        self,
        scope: IfcRunIdentity,
        prepared: PreparedIfcInitialize,
        publication: VerifiedIfcPublication,
        *,
        cause: Exception,
        claim: IfcExecutionClaim,
    ) -> IfcModelInitializeResult:
        try:
            exact = await self._repository.get_exact(
                scope,
                revision_id=prepared.revision_id,
            )
        except Exception as reconcile_error:
            raise IfcModelIntegrityError(
                "IFC initialization commit could not be reconciled"
            ) from reconcile_error
        if exact is None:
            raise cause
        try:
            self._verify_exact_initialize(exact, prepared, publication)
            inspection = await self.inspect_exact(
                exact,
                expected_revision1_project_name=publication.inspection.project_name,
            )
            if not inspection.matches_recorded_facts(publication.inspection):
                raise IfcModelIntegrityError(
                    "committed IFC initialization inspection changed after publication"
                )
            return await self._repository.commit_initialize(
                scope,
                prepared=prepared,
                publication=publication,
                claim=claim,
            )
        except Exception as reconcile_error:
            raise IfcModelIntegrityError(
                "committed IFC initialization result could not be replayed"
            ) from reconcile_error

    @staticmethod
    def _verify_exact_initialize(
        exact: ExactIfcRevision,
        prepared: PreparedIfcInitialize,
        publication: VerifiedIfcPublication,
    ) -> None:
        if any(
            (
                exact.scope != prepared.scope,
                exact.model_id != prepared.model_id,
                exact.model_state not in {IfcModelState.ACTIVE, IfcModelState.FROZEN},
                exact.revision_id != prepared.revision_id,
                exact.revision_no != 1,
                exact.write_epoch != 1,
                exact.artifact != publication.artifact,
                exact.project_global_id != prepared.project_global_id,
            )
        ):
            raise IfcModelIntegrityError(
                "committed revision does not match prepared IFC initialization"
            )

    @staticmethod
    def _verify_reconciled_exact(
        exact: ExactIfcRevision,
        prepared: PreparedIfcRevision,
    ) -> None:
        if any(
            (
                exact.scope != prepared.before.scope,
                exact.model_id != prepared.before.model_id,
                exact.revision_id != prepared.after_revision_id,
                exact.revision_no != prepared.before.revision_no + 1,
                exact.write_epoch != prepared.before.write_epoch + 1,
                exact.artifact.artifact_id != prepared.artifact_id,
                exact.artifact.version_id != prepared.artifact_version_id,
                exact.project_global_id != prepared.before.project_global_id,
            )
        ):
            raise IfcModelIntegrityError(
                "reconciled revision does not match the prepared IFC write"
            )

    @staticmethod
    def _validate_stored_media(stored: StoredObject) -> None:
        if (
            stored.filename != IFC_REVISION_FILENAME
            or stored.content_type != IFC_REVISION_CONTENT_TYPE
        ):
            raise IfcModelIntegrityError("stored IFC media metadata is invalid")

    @staticmethod
    def _verify_exact_identity(
        exact: ExactIfcRevision,
        prepared: PreparedIfcRevision,
        publication: VerifiedIfcPublication,
    ) -> None:
        if any(
            (
                exact.scope != prepared.before.scope,
                exact.model_id != prepared.before.model_id,
                exact.revision_id != prepared.after_revision_id,
                exact.revision_no != prepared.before.revision_no + 1,
                exact.write_epoch != prepared.before.write_epoch + 1,
                exact.artifact != publication.artifact,
                exact.project_global_id != prepared.before.project_global_id,
            )
        ):
            raise IfcModelIntegrityError("committed revision does not match the prepared IFC write")


def _candidate_storage_key(
    scope: IfcRunIdentity,
    operation_id: str,
    generation: int,
    upload_id: str,
) -> StorageKey:
    return StorageKey(
        owner_id=scope.owner_id,
        task_id=scope.task_id,
        object_id=f"ifc-candidate-{operation_id}",
        version=f"{generation}-{upload_id}",
    )


def _storage_key(scope: IfcRunIdentity, artifact_id: str) -> StorageKey:
    return StorageKey(
        owner_id=scope.owner_id,
        task_id=scope.task_id,
        object_id=artifact_id,
        version=1,
    )


__all__ = [
    "IfcRevisionWriter",
    "VerifiedCommittedIfcRevision",
    "VerifiedInitializedIfcRevision",
    "VerifiedExactIfcRevision",
]
