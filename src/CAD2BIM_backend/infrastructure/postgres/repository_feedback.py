"""Feedback and Gate transactions on the existing control-plane database."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, or_, select, update

from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.control_plane import (
    ConflictError,
    FeedbackClaimCursor,
    FeedbackCommand,
    FeedbackCommandReceipt,
    FeedbackCommandStatus,
    FeedbackDeliveryBatch,
    FeedbackDeliveryMode,
    FeedbackScope,
    FeedbackWakeResumePlan,
    FrozenFeedbackBatch,
    GateResumePlan,
    LeaseConflictError,
    OpenInterruptRequest,
    RespondGateCommand,
    ResourceNotFoundError,
    RunRecord,
    RunStatus,
    SubmitFeedbackCommand,
    TaskStatus,
    TerminalGateReentryPlan,
    UserGateResumePlan,
    feedback_message_id,
    require_run_transition,
)
from CAD2BIM_backend.domain.feedback_target import (
    ActiveFeedbackSubject,
    FeedbackSubjectActivation,
    compile_active_feedback_subject,
    feedback_claim_cursor,
    feedback_cursor_fingerprint,
    feedback_scope_matches_cursor,
    resolve_feedback_scope,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.identity import stable_internal_identity_id
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.domain.run_events import (
    RunEvent, RunEventName, RunEventScope, RunEventType, make_run_status_event,
)
from CAD2BIM_backend.domain.system_interrupts import (
    InterruptResumeEnvelope,
    SystemInterrupt,
    SystemInterruptDecision,
    SystemInterruptEnvelope,
    SystemInterruptResponseReceipt,
)
from CAD2BIM_backend.infrastructure.postgres.models import (
    FeedbackCommandModel,
    FeedbackDeliveryBatchMemberModel,
    FeedbackDeliveryBatchModel,
    InterruptModel,
    ArtifactVersionModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository_base import (
    PostgresRepositoryBase,
    database_now,
    fingerprint,
    json_value,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    set_tenant_execution_context,
)


class PostgresFeedbackRepositoryMixin(PostgresRepositoryBase):
    async def _require_feedback_claim(self, session, grant: RunnerCheckpointGrant):
        identity = grant.scope.identity
        run = await self._require_lease(
            session, grant.scope.run_id, grant.runner_id, grant.lease_token
        )
        if (
            run.owner_id != grant.scope.owner_id
            or run.tenant_id != identity.tenant_id
            or run.task_id != identity.task_id
            or run.thread_id != grant.thread_id
            or run.writer_generation != grant.writer_generation
            or run.execution_mode != grant.scope.execution_mode.value
        ):
            raise LeaseConflictError("feedback write does not belong to the current Run claim")
        return run

    async def list_feedback(self, owner_id: str, run_id: str, *, after_sequence: int = 0,
                            limit: int = 100) -> tuple[FeedbackCommand, ...]:
        if after_sequence < 0 or not 1 <= limit <= 500:
            raise ValueError("invalid feedback page")
        async with self._sessions() as session:
            await self._require_run(session, owner_id, run_id)
            rows = (await session.scalars(select(FeedbackCommandModel).where(
                FeedbackCommandModel.run_id == run_id,
                FeedbackCommandModel.accepted_sequence > after_sequence
            ).order_by(FeedbackCommandModel.accepted_sequence).limit(limit))).all()
            return tuple(self._feedback_command(row) for row in rows)

    async def get_feedback(self, owner_id: str, run_id: str, feedback_id: str) -> FeedbackCommand | None:
        async with self._sessions() as session:
            await self._require_run(session, owner_id, run_id)
            row = await session.scalar(select(FeedbackCommandModel).where(
                FeedbackCommandModel.run_id == run_id, FeedbackCommandModel.feedback_id == feedback_id))
            return self._feedback_command(row) if row is not None else None

    async def pause_requested(self, grant: RunnerCheckpointGrant) -> bool:
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, grant.scope.identity)
            run = await self._require_feedback_claim(session, grant)
            return bool(run.pause_requested)

    async def open_run_pause(self, grant: RunnerCheckpointGrant, request: OpenInterruptRequest) -> RunRecord:
        public, cursor = request.system_interrupt, request.runtime_envelope.checkpoint_cursor
        if public.kind.value != "run_paused" or cursor.thread_id != grant.thread_id:
            raise ConflictError("pause must bind the current saved Graph checkpoint")
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, grant.scope.identity)
            run = await self._require_feedback_claim(session, grant)
            if public.source.module != run.module or public.source.stage_key != run.stage_key:
                raise ConflictError("pause does not belong to the Run")
            existing = await session.scalar(select(InterruptModel).where(
                InterruptModel.run_id == run.run_id, InterruptModel.idempotency_key == request.idempotency_key))
            if existing is not None:
                if existing.request_fingerprint != fingerprint(request):
                    raise ConflictError("pause request identity was reused")
            else:
                other = await session.scalar(select(InterruptModel.interrupt_id).where(
                    InterruptModel.run_id == run.run_id, InterruptModel.status == "pending"))
                if other is not None:
                    raise ConflictError("Run already has a pending interrupt")
                session.add(InterruptModel(interrupt_id=request.requested_interrupt_id,
                    owner_id=run.owner_id, tenant_id=run.tenant_id, task_id=run.task_id, run_id=run.run_id,
                    payload=request.persisted_payload, status="pending", idempotency_key=request.idempotency_key,
                    request_fingerprint=fingerprint(request), created_at=request.requested_created_at))
            now = await database_now(session)
            require_run_transition(RunStatus(run.status), RunStatus.PAUSED)
            run.status = RunStatus.PAUSED.value
            run.pause_requested = False
            run.claimed_by = run.lease_token = run.lease_expires_at = run.heartbeat_at = None
            run.updated_at = now
            await self._set_task_status(session, run.task_id, TaskStatus.PAUSED, now)
            await self._append_events(session, run, (make_run_status_event(
                source_event_key=f"paused:{request.requested_interrupt_id}",
                task_id=run.task_id, run_id=run.run_id, module=run.module, stage_key=run.stage_key,
                run_status=RunStatus.PAUSED, pause_requested=False, reason="paused", occurred_at=now,
            ),))
            await session.flush()
            return self._run_record(run)

    async def load_run_continue(self, grant: RunnerCheckpointGrant):
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, grant.scope.identity)
            run = await self._require_feedback_claim(session, grant)
            gate = await session.scalar(select(InterruptModel).where(InterruptModel.run_id == run.run_id)
                                       .order_by(InterruptModel.created_at.desc()).limit(1))
            if gate is None:
                return None
            envelope = SystemInterruptEnvelope.model_validate(gate.payload)
            if envelope.system_interrupt.kind.value != "run_paused":
                return None
            response = gate.response if isinstance(gate.response, dict) else {}
            if response.get("origin") != "run_continue":
                raise ConflictError("saved pause has no explicit continue selection")
            receipt = SystemInterruptResponseReceipt.model_validate(response["receipt"])
            source = envelope.runtime.checkpoint_cursor
            if source.thread_id != grant.thread_id:
                raise ConflictError("saved pause belongs to another Graph")
            resume = InterruptResumeEnvelope(decision=SystemInterruptDecision(action="continue"),
                checkpoint_cursor=source, response_high_watermark=receipt.response_high_watermark,
                expected_writer_generation=grant.writer_generation)
            return response["resume_selection"]["operation_id"], envelope.runtime.graph_interrupt_id, resume

    async def get_pending_gate(
        self, identity: TenantExecutionContext, run_id: str
    ) -> SystemInterrupt | None:
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            run = await self._require_run(session, identity.tenant_id, run_id)
            if run.task_id != identity.task_id:
                raise ResourceNotFoundError("run not found")
            pending = (await session.scalars(
                select(InterruptModel)
                .where(
                    InterruptModel.run_id == run_id,
                    InterruptModel.status == "pending",
                )
                .order_by(InterruptModel.created_at.desc())
            )).all()
            for gate in pending:
                public = SystemInterruptEnvelope.model_validate(gate.payload).system_interrupt
                if (
                    public.kind.value in {"approval_required", "user_input_required"}
                    and public.reason_code == "user_gate"
                    and public.source.node == "user_gate"
                ):
                    return public
            return None

    async def load_gate_resume(self, grant: RunnerCheckpointGrant) -> GateResumePlan | None:
        identity = grant.scope.identity
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            run = await self._require_feedback_claim(session, grant)
            gate = await session.scalar(
                select(InterruptModel)
                .where(InterruptModel.run_id == run.run_id)
                .order_by(InterruptModel.created_at.desc(), InterruptModel.interrupt_id.desc())
                .limit(1)
            )
            if gate is None:
                return None
            envelope = SystemInterruptEnvelope.model_validate(gate.payload)
            if envelope.system_interrupt.kind.value == "run_paused":
                return None
            business_entry_id, accepted_root = envelope.runtime.require_gate_binding()
            source = envelope.runtime.checkpoint_cursor
            if source.thread_id != grant.thread_id or source.checkpoint_ns != "":
                raise ConflictError("saved Gate does not belong to the current Graph claim")
            response = gate.response if isinstance(gate.response, dict) else {}
            selection = response.get("resume_selection")
            if selection is not None and not isinstance(selection, dict):
                raise ConflictError("saved Gate resume selection is malformed")
            frozen: FrozenFeedbackBatch | None = None
            selected_batch_id = (
                selection.get("batch_id") if selection is not None else response.get("batch_id")
            )
            if selected_batch_id:
                batch = await session.scalar(
                    select(FeedbackDeliveryBatchModel).where(
                        FeedbackDeliveryBatchModel.run_id == run.run_id,
                        FeedbackDeliveryBatchModel.batch_id == selected_batch_id,
                    )
                )
                if batch is None:
                    raise ConflictError("saved Gate feedback batch is missing")
                frozen = await self._frozen_batch_locked(session, batch)
            if selection is None and (
                gate.status == "pending" or response.get("origin") == "user_response"
            ):
                # A saved user response does not close the inbox. Any later
                # accepted feedback must join this deterministic Gate route.
                if response.get("origin") != "user_response" or response.get("receipt", {}).get("action") != "cancel":
                    newly_frozen = await self._freeze_gate_feedback_locked(
                        session, run, grant, gate.interrupt_id, source
                    )
                    frozen = newly_frozen or frozen
            if selection is None and gate.status == "pending":
                if frozen is None:
                    return None
                now = await database_now(session)
                response = {"origin": "feedback_wake", "batch_id": frozen.batch.batch_id}
                gate.status = "responded"
                gate.response = response
                gate.responded_at = now
                await session.flush()
            if response.get("origin") == "feedback_wake":
                if frozen is None:
                    raise ConflictError("feedback wake has no original batch")
                trigger = frozen.batch.batch_id
                operation_id = (
                    selection.get("operation_id") if selection is not None else
                    stable_internal_identity_id(
                        "gate_operation_", "gate-resume", run.run_id, gate.interrupt_id,
                        trigger, business_entry_id, source.checkpoint_id,
                    )
                )
                if selection is None:
                    gate.response = {
                        **response,
                        "resume_selection": {
                            "operation_id": operation_id,
                            "batch_id": frozen.batch.batch_id,
                        },
                    }
                    await session.flush()
                return FeedbackWakeResumePlan(
                    interrupt_id=gate.interrupt_id,
                    source_checkpoint_cursor=source,
                    operation_id=operation_id,
                    business_entry_id=business_entry_id,
                    accepted_root_checkpoint_id=accepted_root,
                    frozen_batch=frozen,
                )
            if response.get("origin") != "user_response":
                raise ConflictError("saved Gate response has an unknown origin")
            receipt = SystemInterruptResponseReceipt.model_validate(response["receipt"])
            trigger = frozen.batch.batch_id if frozen is not None else gate.response_idempotency_key
            if not trigger:
                raise ConflictError("saved Gate response has no stable identity")
            operation_id = (
                selection.get("operation_id") if selection is not None else
                stable_internal_identity_id(
                    "gate_operation_", "gate-resume", run.run_id, gate.interrupt_id,
                    trigger, business_entry_id, source.checkpoint_id,
                )
            )
            if selection is None:
                gate.response = {
                    **response,
                    "resume_selection": {
                        "operation_id": operation_id,
                        "batch_id": frozen.batch.batch_id if frozen is not None else None,
                    },
                }
                await session.flush()
            return UserGateResumePlan(
                interrupt_id=gate.interrupt_id,
                source_checkpoint_cursor=source,
                operation_id=operation_id,
                business_entry_id=business_entry_id,
                accepted_root_checkpoint_id=accepted_root,
                response=InterruptResumeEnvelope(
                    decision=SystemInterruptDecision(action=receipt.action),
                    checkpoint_cursor=source,
                    response_high_watermark=receipt.response_high_watermark,
                    expected_writer_generation=grant.writer_generation,
                    feedback_batch_id=frozen.batch.batch_id if frozen else None,
                    selected_feedback_ids=(frozen.batch.member_feedback_ids if frozen else ()),
                ),
                frozen_batch=frozen,
            )

    async def _active_terminal_reentry_locked(
        self, session, run
    ) -> tuple[InterruptModel, TerminalGateReentryPlan] | None:
        gates = (await session.scalars(
            select(InterruptModel)
            .where(InterruptModel.run_id == run.run_id)
            .order_by(InterruptModel.created_at.desc(), InterruptModel.interrupt_id.desc())
        )).all()
        active = [
            (gate, gate.response["terminal_reentry"])
            for gate in gates
            if isinstance(gate.response, dict)
            and isinstance(gate.response.get("terminal_reentry"), dict)
            and not gate.response["terminal_reentry"].get("settled", False)
        ]
        if not active:
            return None
        if len(active) != 1:
            raise ConflictError("Run has ambiguous terminal Gate reentry selections")
        gate, selected = active[0]
        envelope = SystemInterruptEnvelope.model_validate(gate.payload)
        business_entry_id, accepted_root = envelope.runtime.require_gate_binding()
        batch = await session.scalar(
            select(FeedbackDeliveryBatchModel).where(
                FeedbackDeliveryBatchModel.run_id == run.run_id,
                FeedbackDeliveryBatchModel.batch_id == selected.get("batch_id"),
            )
        )
        if batch is None:
            raise ConflictError("terminal Gate reentry batch is missing")
        frozen = await self._frozen_batch_locked(session, batch)
        operation_id = selected.get("operation_id")
        source_checkpoint_id = selected.get("source_checkpoint_id")
        plan = TerminalGateReentryPlan(
            source_checkpoint_id=source_checkpoint_id,
            operation_id=operation_id,
            gate_plan=FeedbackWakeResumePlan(
                interrupt_id=gate.interrupt_id,
                source_checkpoint_cursor=envelope.runtime.checkpoint_cursor,
                operation_id=operation_id,
                business_entry_id=business_entry_id,
                accepted_root_checkpoint_id=accepted_root,
                frozen_batch=frozen,
            ),
        )
        return gate, plan

    async def load_terminal_reentry(
        self, grant: RunnerCheckpointGrant
    ) -> TerminalGateReentryPlan | None:
        identity = grant.scope.identity
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            run = await self._require_feedback_claim(session, grant)
            selected = await self._active_terminal_reentry_locked(session, run)
            return selected[1] if selected is not None else None

    async def select_terminal_reentry(
        self,
        grant: RunnerCheckpointGrant,
        *,
        source_checkpoint_id: str,
    ) -> TerminalGateReentryPlan | None:
        if not source_checkpoint_id.strip():
            raise ValueError("terminal Gate reentry source must be saved")
        identity = grant.scope.identity
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            run = await self._require_feedback_claim(session, grant)
            current = await self._active_terminal_reentry_locked(session, run)
            if current is not None and current[1].source_checkpoint_id == source_checkpoint_id:
                return current[1]
            gate = await session.scalar(
                select(InterruptModel)
                .where(InterruptModel.run_id == run.run_id)
                .order_by(InterruptModel.created_at.desc(), InterruptModel.interrupt_id.desc())
                .limit(1)
            )
            if gate is None or gate.status != "responded":
                raise ConflictError("terminal Gate reentry has no responded Gate")
            envelope = SystemInterruptEnvelope.model_validate(gate.payload)
            business_entry_id, _accepted_root = envelope.runtime.require_gate_binding()
            frozen = await self._freeze_gate_feedback_locked(
                session, run, grant, gate.interrupt_id,
                envelope.runtime.checkpoint_cursor,
            )
            if frozen is None:
                return None
            operation_id = stable_internal_identity_id(
                "gate_reentry_", "terminal-gate-reentry", run.run_id,
                gate.interrupt_id, frozen.batch.batch_id,
                business_entry_id, source_checkpoint_id,
            )
            if current is not None:
                previous_gate, _previous_plan = current
                previous_response = dict(previous_gate.response)
                previous_response["terminal_reentry"] = {
                    **previous_response["terminal_reentry"], "settled": True,
                }
                previous_gate.response = previous_response
            response = dict(gate.response)
            response["terminal_reentry"] = {
                "source_checkpoint_id": source_checkpoint_id,
                "operation_id": operation_id,
                "batch_id": frozen.batch.batch_id,
                "settled": False,
            }
            gate.response = response
            await session.flush()
            selected = await self._active_terminal_reentry_locked(session, run)
            if selected is None:
                raise RuntimeError("saved terminal Gate reentry cannot be read")
            return selected[1]

    async def _freeze_gate_feedback_locked(
        self,
        session,
        run,
        grant: RunnerCheckpointGrant,
        gate_id: str,
        source: CheckpointCursor,
    ) -> FrozenFeedbackBatch | None:
        active = (
            ActiveFeedbackSubject.model_validate(run.active_feedback_subject)
            if run.active_feedback_subject is not None else None
        )
        cursor = feedback_claim_cursor(
            run_id=run.run_id,
            stage_key=run.stage_key,
            role_execution_id=(
                active.subject_id if active is not None else stable_internal_identity_id(
                    "gate_target_", "gate-feedback", run.run_id, gate_id
                )
            ),
            writer_generation=grant.writer_generation,
            checkpoint_id=source.checkpoint_id,
            task_ids=active.task_ids if active is not None else (),
            check_ids=active.check_ids if active is not None else (),
        )
        return await self._freeze_feedback_locked(
            session, run, cursor, source, stage_only=True
        )

    async def freeze_feedback(
        self,
        grant: RunnerCheckpointGrant,
        cursor: FeedbackClaimCursor,
        *,
        input_checkpoint_cursor: CheckpointCursor,
        delivered_batch_ids: tuple[str, ...] = (),
    ) -> tuple[FrozenFeedbackBatch, ...]:
        identity = grant.scope.identity
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            run = await self._require_feedback_claim(session, grant)
            if (
                cursor.run_id != run.run_id
                or cursor.stage_key != run.stage_key
                or cursor.writer_generation != grant.writer_generation
                or input_checkpoint_cursor.thread_id != grant.thread_id
                or cursor.checkpoint_id != input_checkpoint_cursor.checkpoint_id
                or run.cancel_requested
            ):
                raise ConflictError("feedback freeze differs from the current claim")
            active = (
                ActiveFeedbackSubject.model_validate(run.active_feedback_subject)
                if run.active_feedback_subject is not None and not run.feedback_sealed
                else None
            )
            if active is None or active.subject_id != cursor.role_execution_id:
                raise ConflictError("feedback freeze target is no longer active")
            for batch_id in delivered_batch_ids:
                await self._settle_feedback_locked(
                    session, run, batch_id, input_checkpoint_cursor.checkpoint_id)
            # Acceptance also holds this Run lock. This is the atomic boundary
            # for the imminent Main request, including Gate-preselected batches.
            cutoff = await session.scalar(select(func.max(FeedbackCommandModel.accepted_sequence))
                                          .where(FeedbackCommandModel.run_id == run.run_id))
            previous = (await session.scalars(select(FeedbackDeliveryBatchModel).where(
                FeedbackDeliveryBatchModel.run_id == run.run_id,
                FeedbackDeliveryBatchModel.delivered_at.is_(None)
            ).order_by(FeedbackDeliveryBatchModel.created_at, FeedbackDeliveryBatchModel.batch_id))).all()
            batches = []
            for batch in previous:
                frozen = await self._frozen_batch_locked(session, batch)
                if all(feedback_scope_matches_cursor(item.resolved_scope, cursor) for item in frozen.members):
                    batches.append(frozen)
            new = await self._freeze_new_feedback_locked(
                session, run, cursor, input_checkpoint_cursor, cutoff=cutoff or 0)
            if new is not None:
                batches.append(new)
            return tuple(sorted(batches, key=lambda batch: batch.members[0].accepted_sequence))

    async def _freeze_feedback_locked(
        self,
        session,
        run,
        cursor: FeedbackClaimCursor,
        input_checkpoint_cursor: CheckpointCursor,
        *,
        stage_only: bool = False,
    ) -> FrozenFeedbackBatch | None:
        target_fingerprint = feedback_cursor_fingerprint(cursor)
        previous_batches = (await session.scalars(
            select(FeedbackDeliveryBatchModel)
            .where(
                FeedbackDeliveryBatchModel.run_id == run.run_id,
                FeedbackDeliveryBatchModel.delivered_at.is_(None),
            )
            .order_by(FeedbackDeliveryBatchModel.created_at, FeedbackDeliveryBatchModel.batch_id)
        )).all()
        for previous in previous_batches:
            frozen = await self._frozen_batch_locked(session, previous)
            if all(
                self._feedback_scope_matches_delivery(item.resolved_scope, cursor, stage_only)
                for item in frozen.members
            ):
                return frozen
        return await self._freeze_new_feedback_locked(
            session, run, cursor, input_checkpoint_cursor, stage_only=stage_only)

    async def _freeze_new_feedback_locked(
        self, session, run, cursor: FeedbackClaimCursor,
        input_checkpoint_cursor: CheckpointCursor, *, stage_only: bool = False,
        cutoff: int | None = None,
    ) -> FrozenFeedbackBatch | None:
        target_fingerprint = feedback_cursor_fingerprint(cursor)
        candidates = await session.scalars(
            select(FeedbackCommandModel)
            .where(
                FeedbackCommandModel.run_id == run.run_id,
                FeedbackCommandModel.status == FeedbackCommandStatus.ACCEPTED.value,
                FeedbackCommandModel.accepted_sequence <= cutoff if cutoff is not None else True,
            )
            .order_by(FeedbackCommandModel.accepted_sequence)
        )
        selected = [
            item for item in candidates
            if self._feedback_scope_matches_delivery(
                FeedbackScope.model_validate(item.resolved_scope), cursor, stage_only
            )
        ]
        if not selected:
            return None
        now = await database_now(session)
        batch = FeedbackDeliveryBatchModel(
            batch_id=f"feedback_batch_{uuid4().hex}",
            owner_id=run.owner_id,
            tenant_id=run.tenant_id,
            task_id=run.task_id,
            run_id=run.run_id,
            target_fingerprint=target_fingerprint,
            claim_high_watermark=max(item.accepted_sequence for item in selected),
            contains_interrupt=any(item.delivery_mode == FeedbackDeliveryMode.INTERRUPT.value for item in selected),
            claimed_writer_generation=cursor.writer_generation,
            claimed_checkpoint_id=input_checkpoint_cursor.checkpoint_id,
            claimed_checkpoint_cursor=json_value(input_checkpoint_cursor),
            resume_interrupt_id=None,
            created_at=now,
        )
        session.add(batch)
        await session.flush()
        for item in selected:
            item.status = FeedbackCommandStatus.CLAIMED.value
            item.claimed_batch_id = batch.batch_id
            session.add(FeedbackDeliveryBatchMemberModel(
                batch_id=batch.batch_id,
                feedback_id=item.feedback_id,
                tenant_id=run.tenant_id,
                run_id=run.run_id,
                accepted_sequence=item.accepted_sequence,
            ))
        run.feedback_pending = True
        run.updated_at = now
        await session.flush()
        return await self._frozen_batch_locked(session, batch)

    @staticmethod
    def _feedback_scope_matches_delivery(
        scope: FeedbackScope, cursor: FeedbackClaimCursor, stage_only: bool
    ) -> bool:
        if stage_only and any(
            value is not None for value in (
                scope.role_execution_id, scope.task_id, scope.check_id,
                scope.expected_checkpoint_id,
            )
        ):
            return False
        return feedback_scope_matches_cursor(scope, cursor)

    async def get_frozen_feedback_batch(
        self, grant: RunnerCheckpointGrant, batch_id: str
    ) -> FrozenFeedbackBatch:
        identity = grant.scope.identity
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            run = await self._require_feedback_claim(session, grant)
            batch = await session.scalar(
                select(FeedbackDeliveryBatchModel).where(
                    FeedbackDeliveryBatchModel.run_id == run.run_id,
                    FeedbackDeliveryBatchModel.batch_id == batch_id,
                )
            )
            if batch is None:
                raise ResourceNotFoundError("feedback batch not found")
            return await self._frozen_batch_locked(session, batch)

    async def _frozen_batch_locked(self, session, batch) -> FrozenFeedbackBatch:
        membership = (await session.scalars(
            select(FeedbackDeliveryBatchMemberModel)
            .where(FeedbackDeliveryBatchMemberModel.batch_id == batch.batch_id)
            .order_by(FeedbackDeliveryBatchMemberModel.accepted_sequence)
        )).all()
        if not membership:
            raise ConflictError("feedback batch has no persisted members")
        members = (await session.scalars(
            select(FeedbackCommandModel).where(
                FeedbackCommandModel.feedback_id.in_(tuple(item.feedback_id for item in membership))
            )
        )).all()
        by_id = {item.feedback_id: item for item in members}
        if len(by_id) != len(membership):
            raise ConflictError("feedback batch member is missing")
        ordered = tuple(by_id[item.feedback_id] for item in membership)
        domain_batch = FeedbackDeliveryBatch(
            batch_id=batch.batch_id,
            owner_id=batch.owner_id,
            run_id=batch.run_id,
            target_fingerprint=batch.target_fingerprint,
            claim_high_watermark=batch.claim_high_watermark,
            member_feedback_ids=tuple(item.feedback_id for item in ordered),
            contains_interrupt=batch.contains_interrupt,
            claimed_writer_generation=batch.claimed_writer_generation,
            claimed_checkpoint_id=batch.claimed_checkpoint_id,
            claimed_checkpoint_cursor=CheckpointCursor.model_validate(batch.claimed_checkpoint_cursor),
            resume_interrupt_id=batch.resume_interrupt_id,
            delivered_checkpoint_id=batch.delivered_checkpoint_id,
            created_at=batch.created_at,
            delivered_at=batch.delivered_at,
        )
        return FrozenFeedbackBatch(
            batch=domain_batch,
            members=tuple(self._feedback_command(item) for item in ordered),
            message_ids=tuple(feedback_message_id(batch.batch_id, item.feedback_id) for item in ordered),
        )

    @staticmethod
    def _feedback_command(model: FeedbackCommandModel) -> FeedbackCommand:
        return FeedbackCommand(
            feedback_id=model.feedback_id,
            accepted_sequence=model.accepted_sequence,
            owner_id=model.owner_id,
            author_principal_id=model.author_principal_id,
            task_id=model.task_id,
            run_id=model.run_id,
            client_request_id=model.client_request_id,
            request_fingerprint=model.request_fingerprint,
            delivery_mode=FeedbackDeliveryMode(model.delivery_mode),
            content=model.content,
            resolved_scope=FeedbackScope.model_validate(model.resolved_scope),
            status=FeedbackCommandStatus(model.status),
            claimed_batch_id=model.claimed_batch_id,
            delivered_checkpoint_id=model.delivered_checkpoint_id,
            created_at=model.created_at,
            delivered_at=model.delivered_at,
        )

    async def _settle_feedback_locked(
        self, session, run, batch_id: str | None, checkpoint_id: str
    ) -> None:
        """Project a batch from saved node input; caller holds the current Run lock."""

        if batch_id is None:
            return
        batch = await session.scalar(
            select(FeedbackDeliveryBatchModel).where(
                FeedbackDeliveryBatchModel.run_id == run.run_id,
                FeedbackDeliveryBatchModel.batch_id == batch_id,
            )
        )
        if batch is None:
            raise ResourceNotFoundError("feedback batch not found")
        if run.cancel_requested:
            raise ConflictError("cancelled Run cannot settle feedback delivery")
        if batch.delivered_at is not None:
            return
        now = await database_now(session)
        batch.delivered_checkpoint_id = checkpoint_id
        batch.delivered_at = now
        await session.execute(
            update(FeedbackCommandModel).where(
                FeedbackCommandModel.run_id == run.run_id,
                FeedbackCommandModel.claimed_batch_id == batch_id,
                FeedbackCommandModel.status == FeedbackCommandStatus.CLAIMED.value,
            ).values(
                status=FeedbackCommandStatus.DELIVERED.value,
                delivered_checkpoint_id=checkpoint_id,
                delivered_at=now,
            )
        )
        pending = await session.scalar(
            select(func.count()).select_from(FeedbackCommandModel).where(
                FeedbackCommandModel.run_id == run.run_id,
                FeedbackCommandModel.status.in_((
                    FeedbackCommandStatus.ACCEPTED.value,
                    FeedbackCommandStatus.CLAIMED.value,
                )),
            )
        )
        run.feedback_pending = bool(pending)
        run.updated_at = now
        await session.flush()

    async def respond_gate(
        self,
        identity: TenantExecutionContext,
        run_id: str,
        interrupt_id: str,
        command: RespondGateCommand,
    ) -> SystemInterruptResponseReceipt:
        if identity.actor_kind is not TenantActorKind.USER:
            raise ConflictError("Gate response must come from an authenticated user")
        if command.interrupt_id != interrupt_id:
            raise ConflictError("Gate response addresses another interrupt")
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            run = await self._require_run(
                session, identity.tenant_id, run_id, for_update=True
            )
            if run.task_id != identity.task_id:
                raise ResourceNotFoundError("run not found")
            gate = await session.scalar(
                select(InterruptModel).where(
                    InterruptModel.run_id == run_id,
                    InterruptModel.interrupt_id == interrupt_id,
                )
            )
            if gate is None:
                raise ConflictError("Gate is unavailable")
            prior_by_key = await session.scalar(
                select(InterruptModel).where(
                    InterruptModel.run_id == run_id,
                    InterruptModel.response_idempotency_key == command.idempotency_key,
                )
            )
            if prior_by_key is not None and prior_by_key.interrupt_id != interrupt_id:
                raise ConflictError("Gate response key belongs to another interrupt")
            request_fingerprint = fingerprint(command)
            if gate.status == "responded":
                stored = gate.response if isinstance(gate.response, dict) else {}
                if (
                    gate.response_idempotency_key != command.idempotency_key
                    or stored.get("request_fingerprint") != request_fingerprint
                ):
                    raise ConflictError("Gate was already consumed by another response")
                return SystemInterruptResponseReceipt.model_validate(stored["receipt"])
            if gate.status != "pending" or run.status not in (
                RunStatus.RUNNING.value,
                RunStatus.WAITING_USER.value,
                RunStatus.QUEUED.value,
            ):
                raise ConflictError("Gate is no longer awaiting a response")
            envelope = SystemInterruptEnvelope.model_validate(gate.payload)
            envelope.runtime.require_gate_binding()
            if command.action not in envelope.system_interrupt.allowed_actions:
                raise ConflictError("Gate action is not allowed")
            feedback_id = None
            if command.content:
                feedback_request_id = stable_internal_identity_id(
                    "gate_feedback_",
                    "gate-response-feedback",
                    run_id,
                    interrupt_id,
                    command.idempotency_key,
                )
                accepted = await self._accept_feedback_locked(
                    session,
                    run,
                    identity,
                    SubmitFeedbackCommand(
                        client_request_id=feedback_request_id,
                        delivery_mode=FeedbackDeliveryMode.STEER,
                        content=command.content,
                        scope=FeedbackScope(run_id=run_id, stage_key=run.stage_key),
                    ),
                )
                feedback_id = accepted.feedback_id
            high_watermark = await session.scalar(
                select(func.max(FeedbackCommandModel.accepted_sequence)).where(
                    FeedbackCommandModel.run_id == run_id
                )
            )
            now = await database_now(session)
            receipt = SystemInterruptResponseReceipt(
                interrupt_id=interrupt_id,
                action=command.action,
                response_high_watermark=high_watermark or 0,
                responded_at=now,
            )
            gate.status = "responded"
            gate.response_idempotency_key = command.idempotency_key
            gate.responded_at = now
            gate.response = {
                "origin": "user_response",
                "request_fingerprint": request_fingerprint,
                "feedback_id": feedback_id,
                "receipt": receipt.model_dump(mode="json"),
            }
            if command.action == "cancel":
                run.cancel_requested = True
            if run.status == RunStatus.WAITING_USER.value:
                require_run_transition(RunStatus.WAITING_USER, RunStatus.QUEUED)
                run.status = RunStatus.QUEUED.value
                await self._set_task_status(session, run.task_id, TaskStatus.RUNNING, now)
                await self._append_events(session, run, (make_run_status_event(
                    source_event_key=f"gate-response:{interrupt_id}:{command.idempotency_key}",
                    task_id=run.task_id, run_id=run.run_id, module=run.module, stage_key=run.stage_key,
                    run_status=RunStatus.QUEUED, pause_requested=run.pause_requested,
                    reason="gate_response", occurred_at=now,
                ),))
            run.updated_at = now
            await session.flush()
            return receipt

    async def accept_feedback(
        self,
        identity: TenantExecutionContext,
        run_id: str,
        command: SubmitFeedbackCommand,
    ) -> FeedbackCommandReceipt:
        if identity.actor_kind is not TenantActorKind.USER:
            raise ConflictError("feedback must come from an authenticated user")
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            run = await self._require_run(
                session, identity.tenant_id, run_id, for_update=True
            )
            return await self._accept_feedback_locked(session, run, identity, command)

    async def _accept_feedback_locked(
        self,
        session,
        run,
        identity: TenantExecutionContext,
        command: SubmitFeedbackCommand,
    ) -> FeedbackCommandReceipt:
        if run.task_id != identity.task_id:
            raise ResourceNotFoundError("run not found")
        request_fingerprint = fingerprint(
            {"run_id": run.run_id, "command": command.model_dump(mode="json")}
        )
        existing = await session.scalar(
            select(FeedbackCommandModel).where(
                FeedbackCommandModel.tenant_id == identity.tenant_id,
                FeedbackCommandModel.author_principal_id == identity.principal_id,
                FeedbackCommandModel.client_request_id == command.client_request_id,
            )
        )
        if existing is not None:
            if (
                existing.run_id != run.run_id
                or existing.request_fingerprint != request_fingerprint
            ):
                raise ConflictError("feedback idempotency key was reused with different input")
            return self._feedback_receipt(existing)
        if (
            run.status not in (
                RunStatus.QUEUED.value,
                RunStatus.RUNNING.value,
                RunStatus.PAUSED.value,
                RunStatus.WAITING_USER.value,
            )
            or run.cancel_requested
        ):
            raise ConflictError("Run cannot accept new feedback")
        active = (
            ActiveFeedbackSubject.model_validate(run.active_feedback_subject)
            if run.active_feedback_subject is not None and not run.feedback_sealed
            else None
        )
        resolved_scope = resolve_feedback_scope(
            task_id=run.task_id,
            run_id=run.run_id,
            run_stage_key=run.stage_key,
            requested_scope=command.scope,
            active_subject=active,
        )
        for block in command.content:
            if block.type == "text":
                continue
            artifact = await session.scalar(select(ArtifactVersionModel).where(
                ArtifactVersionModel.owner_id == identity.tenant_id,
                ArtifactVersionModel.task_id == run.task_id,
                ArtifactVersionModel.artifact_id == block.artifact_id,
                ArtifactVersionModel.version_id == block.version_id))
            if artifact is None:
                raise ResourceNotFoundError("input ArtifactVersion not found")
            if block.type == "image" and not artifact.content_type.startswith("image/"):
                raise ConflictError("image input must reference an image ArtifactVersion")
        highest = await session.scalar(
            select(func.max(FeedbackCommandModel.accepted_sequence)).where(
                FeedbackCommandModel.run_id == run.run_id
            )
        )
        now = await database_now(session)
        feedback = FeedbackCommandModel(
            feedback_id=f"feedback_{uuid4().hex}",
            accepted_sequence=(highest or 0) + 1,
            tenant_id=identity.tenant_id,
            owner_id=run.owner_id,
            task_id=run.task_id,
            run_id=run.run_id,
            author_principal_id=identity.principal_id,
            client_request_id=command.client_request_id,
            request_fingerprint=request_fingerprint,
            delivery_mode=command.delivery_mode.value,
            content=[block.model_dump(mode="json") for block in command.content],
            resolved_scope=json_value(resolved_scope),
            status=FeedbackCommandStatus.ACCEPTED.value,
            created_at=now,
        )
        session.add(feedback)
        run.feedback_pending = True
        woke_run = run.status == RunStatus.WAITING_USER.value
        if run.status == RunStatus.WAITING_USER.value:
            require_run_transition(RunStatus.WAITING_USER, RunStatus.QUEUED)
            run.status = RunStatus.QUEUED.value
            await self._set_task_status(session, run.task_id, TaskStatus.RUNNING, now)
        run.updated_at = now
        await session.flush()
        await self._append_events(session, run, (RunEvent(
            event_id=stable_internal_identity_id("event_", "feedback-accepted", feedback.feedback_id),
            sequence=1, task_id=run.task_id, run_id=run.run_id,
            event_type=RunEventName.FEEDBACK_ACCEPTED.value, occurred_at=now,
            scope=RunEventScope(module=run.module, stage_key=run.stage_key),
            item_id=feedback.feedback_id, status=RunEventType.SUCCEEDED,
            payload_ref=f"feedback://{feedback.feedback_id}",
            attributes={"feedback_id": feedback.feedback_id, "client_request_id": feedback.client_request_id},
        ),))
        if woke_run:
            await self._append_events(session, run, (make_run_status_event(
                source_event_key=f"feedback-wake:{feedback.feedback_id}",
                task_id=run.task_id, run_id=run.run_id, module=run.module, stage_key=run.stage_key,
                run_status=RunStatus.QUEUED, pause_requested=run.pause_requested,
                reason="feedback_wake", occurred_at=now,
            ),))
        return self._feedback_receipt(feedback)

    @staticmethod
    def _feedback_receipt(model: FeedbackCommandModel) -> FeedbackCommandReceipt:
        return FeedbackCommandReceipt(
            feedback_id=model.feedback_id,
            accepted_sequence=model.accepted_sequence,
            delivery_mode=FeedbackDeliveryMode(model.delivery_mode),
            resolved_scope=FeedbackScope.model_validate(model.resolved_scope),
            status=FeedbackCommandStatus(model.status),
        )

    async def open_gate(
        self,
        grant: RunnerCheckpointGrant,
        request: OpenInterruptRequest,
    ) -> SystemInterruptEnvelope:
        identity = grant.scope.identity
        request.runtime_envelope.require_gate_binding()
        cursor = request.runtime_envelope.checkpoint_cursor
        if cursor.thread_id != grant.thread_id or cursor.checkpoint_ns != "":
            raise ConflictError("Gate must bind the claimed root checkpoint")
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            run = await self._require_lease(
                session, grant.scope.run_id, grant.runner_id, grant.lease_token
            )
            if (
                run.task_id != identity.task_id
                or run.writer_generation != grant.writer_generation
                or request.system_interrupt.source.module != run.module
                or request.system_interrupt.source.stage_key != run.stage_key
            ):
                raise ConflictError("Gate does not belong to the current Run claim")
            existing = await session.scalar(
                select(InterruptModel).where(
                    InterruptModel.run_id == run.run_id,
                    InterruptModel.idempotency_key == request.idempotency_key,
                )
            )
            if existing is not None:
                if existing.request_fingerprint != fingerprint(request):
                    raise ConflictError("Gate idempotency key was reused with different input")
                return SystemInterruptEnvelope.model_validate(existing.payload)
            other_pending = await session.scalar(
                select(InterruptModel.interrupt_id).where(
                    InterruptModel.run_id == run.run_id,
                    InterruptModel.status == "pending",
                )
            )
            if other_pending is not None:
                raise ConflictError("Run already has a pending Gate")
            envelope = SystemInterruptEnvelope(
                system_interrupt=request.system_interrupt,
                runtime=request.runtime_envelope,
            )
            session.add(
                InterruptModel(
                    interrupt_id=request.requested_interrupt_id,
                    owner_id=run.owner_id,
                    tenant_id=run.tenant_id,
                    task_id=run.task_id,
                    run_id=run.run_id,
                    payload=json_value(envelope),
                    status="pending",
                    idempotency_key=request.idempotency_key,
                    request_fingerprint=fingerprint(request),
                    created_at=request.requested_created_at,
                )
            )
            await session.flush()
            return envelope

    async def park_waiting_user(
        self,
        grant: RunnerCheckpointGrant,
        interrupt_id: str,
    ) -> RunRecord:
        identity = grant.scope.identity
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            run = await self._require_lease(
                session, grant.scope.run_id, grant.runner_id, grant.lease_token
            )
            if run.task_id != identity.task_id or run.writer_generation != grant.writer_generation:
                raise ConflictError("Gate park does not belong to the current claim")
            gate = await session.scalar(
                select(InterruptModel).where(
                    InterruptModel.run_id == run.run_id,
                    InterruptModel.interrupt_id == interrupt_id,
                )
            )
            if gate is None:
                raise ConflictError("saved Gate is unavailable")
            envelope = SystemInterruptEnvelope.model_validate(gate.payload)
            envelope.runtime.require_gate_binding()
            if envelope.runtime.checkpoint_cursor.thread_id != grant.thread_id:
                raise ConflictError("saved Gate belongs to another Graph thread")
            if run.cancel_requested:
                now = await database_now(session)
                if gate.status == "pending":
                    gate.status = "responded"
                    gate.response = {"origin": "run_cancelled"}
                    gate.responded_at = now
                await self._supersede_undelivered_feedback(session, run, now)
                require_run_transition(RunStatus(run.status), RunStatus.CANCELLED)
                run.status = RunStatus.CANCELLED.value
                run.claimed_by = None
                run.lease_token = None
                run.lease_expires_at = None
                run.heartbeat_at = None
                run.updated_at = now
                run.ended_at = now
                await self._set_task_status(session, run.task_id, TaskStatus.CANCELLED, now)
                await session.flush()
                return self._run_record(run)
            pending_count = await session.scalar(
                select(func.count())
                .select_from(FeedbackCommandModel)
                .where(
                    FeedbackCommandModel.run_id == run.run_id,
                    FeedbackCommandModel.status.in_(
                        (
                            FeedbackCommandStatus.ACCEPTED.value,
                            FeedbackCommandStatus.CLAIMED.value,
                        )
                    ),
                )
            )
            wake_now = bool(pending_count) or gate.status == "responded"
            target = RunStatus.PAUSED if run.pause_requested else (
                RunStatus.QUEUED if wake_now else RunStatus.WAITING_USER)
            require_run_transition(RunStatus(run.status), target)
            now = await database_now(session)
            run.status = target.value
            run.pause_requested = False
            run.claimed_by = None
            run.lease_token = None
            run.lease_expires_at = None
            run.heartbeat_at = None
            run.feedback_pending = bool(pending_count)
            run.updated_at = now
            prior_gates = (await session.scalars(
                select(InterruptModel).where(
                    InterruptModel.run_id == run.run_id,
                    InterruptModel.interrupt_id != interrupt_id,
                )
            )).all()
            for previous in prior_gates:
                payload = previous.response if isinstance(previous.response, dict) else {}
                selected = payload.get("terminal_reentry")
                if isinstance(selected, dict) and not selected.get("settled", False):
                    previous.response = {
                        **payload,
                        "terminal_reentry": {**selected, "settled": True},
                    }
            await self._set_task_status(
                session,
                run.task_id,
                TaskStatus.PAUSED if target is RunStatus.PAUSED else (
                    TaskStatus.RUNNING if wake_now else TaskStatus.WAITING_USER),
                now,
            )
            await self._append_events(session, run, (make_run_status_event(
                source_event_key=f"gate-park:{interrupt_id}:{target.value}",
                task_id=run.task_id, run_id=run.run_id, module=run.module, stage_key=run.stage_key,
                run_status=target, pause_requested=False,
                reason="paused" if target is RunStatus.PAUSED else (
                    "gate_wake" if target is RunStatus.QUEUED else "gate_waiting"),
                occurred_at=now,
            ),))
            await session.flush()
            return self._run_record(run)

    async def activate_feedback_subject(
        self,
        grant: RunnerCheckpointGrant,
        activation: FeedbackSubjectActivation,
    ) -> ActiveFeedbackSubject:
        identity = grant.scope.identity
        if identity.actor_kind is not TenantActorKind.RUNNER:
            raise ConflictError("only Runner may activate a feedback subject")
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            run = await self._require_feedback_claim(session, grant)
            if (
                activation.run_id != run.run_id
                or activation.task_id != run.task_id
                or activation.stage_key != run.stage_key
                or activation.writer_generation != grant.writer_generation
            ):
                raise ConflictError("feedback subject does not match the claimed Run")
            previous = (
                ActiveFeedbackSubject.model_validate(run.active_feedback_subject)
                if run.active_feedback_subject is not None
                else None
            )
            now = await database_now(session)
            subject = compile_active_feedback_subject(
                activation,
                revision=(previous.revision + 1 if previous is not None else 1),
                activated_at=now,
            )
            if previous is not None and previous.model_dump(exclude={"revision", "activated_at"}) == subject.model_dump(exclude={"revision", "activated_at"}):
                return previous
            run.active_feedback_subject = json_value(subject)
            run.feedback_sealed = False
            run.feedback_transition_subject_id = None
            run.updated_at = now
            await session.flush()
            return subject

    async def seal_role_exit(
        self,
        grant: RunnerCheckpointGrant,
        role_execution_id: str,
        input_high_watermark: int,
        *,
        input_checkpoint_cursor: CheckpointCursor,
        delivered_batch_ids: tuple[str, ...] = (),
    ) -> bool:
        identity = grant.scope.identity
        if identity.actor_kind is not TenantActorKind.RUNNER:
            raise ConflictError("only Runner may seal a role exit")
        if input_high_watermark < 0:
            raise ValueError("input_high_watermark must be nonnegative")
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, identity)
            run = await self._require_feedback_claim(session, grant)
            active = (
                ActiveFeedbackSubject.model_validate(run.active_feedback_subject)
                if run.active_feedback_subject is not None
                else None
            )
            if active is None or active.subject_id != role_execution_id:
                raise ConflictError("role exit subject is no longer active")
            if input_checkpoint_cursor.thread_id != grant.thread_id:
                raise ConflictError("role exit input belongs to another thread")
            for batch_id in delivered_batch_ids:
                await self._settle_feedback_locked(
                    session, run, batch_id, input_checkpoint_cursor.checkpoint_id)
            if run.feedback_sealed:
                if run.feedback_transition_subject_id == role_execution_id:
                    return True
                raise ConflictError("another role exit is sealed")
            scope = FeedbackCommandModel.resolved_scope
            pending = await session.scalar(
                select(func.count())
                .select_from(FeedbackCommandModel)
                .where(
                    FeedbackCommandModel.run_id == run.run_id,
                    FeedbackCommandModel.owner_id == identity.tenant_id,
                    FeedbackCommandModel.status.in_(
                        (
                            FeedbackCommandStatus.ACCEPTED.value,
                            FeedbackCommandStatus.CLAIMED.value,
                        )
                    ),
                    scope["stage_key"].as_string() == active.stage_key,
                    or_(
                        scope["role_execution_id"].as_string() == role_execution_id,
                        scope["role_execution_id"].as_string().is_(None),
                    ),
                    or_(
                        scope["task_id"].as_string().is_(None),
                        scope["task_id"].as_string().in_(active.task_ids),
                    ),
                    or_(
                        scope["check_id"].as_string().is_(None),
                        scope["check_id"].as_string().in_(active.check_ids),
                    ),
                )
            )
            if pending:
                return False
            now = await database_now(session)
            run.feedback_sealed = True
            run.feedback_transition_subject_id = role_execution_id
            run.updated_at = now
            await session.flush()
            return True


__all__ = ["PostgresFeedbackRepositoryMixin"]
