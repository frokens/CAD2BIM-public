"""Claim and execute one durable Graph Run through the fenced runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager, suppress
from typing import Any, Protocol

from CAD2BIM_backend.domain.control_plane import (
    ConflictError,
    GateResumePlan,
    LeaseConflictError,
    OpenInterruptRequest,
    RunRecord,
    TerminalGateReentryPlan,
)
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.domain.run_events import RunEvent
from CAD2BIM_backend.domain.state_read_binding import make_completion_checkpoint_event
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.system_interrupts import InterruptResumeEnvelope
from CAD2BIM_backend.domain.execution_protocol import (
    ExecutionProtocolMismatchError,
    require_current_execution_protocol,
)
from CAD2BIM_backend.ports.graph_runtime import GraphExecutionResult, GraphRuntimePort


class _RunnerRuns(Protocol):
    async def claim_next_run(
        self, runner_id: str, *, lease_seconds: int = 30
    ) -> RunRecord | None: ...

    async def heartbeat(
        self, run_id: str, runner_id: str, lease_token: str, *, lease_seconds: int = 30
    ) -> RunRecord: ...

    async def complete(
        self, run_id: str, runner_id: str, lease_token: str, *, events: Sequence[RunEvent] = ()
    ) -> RunRecord: ...

    async def pause_claimed(
        self, run_id: str, runner_id: str, lease_token: str, *, error: str | None = None,
    ) -> RunRecord: ...


class _RunnerFeedback(Protocol):
    async def open_run_pause(
        self, grant: RunnerCheckpointGrant, request: OpenInterruptRequest
    ) -> RunRecord: ...

    async def load_run_continue(
        self, grant: RunnerCheckpointGrant
    ) -> tuple[str, str, InterruptResumeEnvelope] | None: ...

    async def open_gate(
        self, grant: RunnerCheckpointGrant, request: OpenInterruptRequest
    ) -> object: ...

    async def park_waiting_user(
        self, grant: RunnerCheckpointGrant, interrupt_id: str
    ) -> RunRecord: ...

    async def load_gate_resume(
        self, grant: RunnerCheckpointGrant
    ) -> GateResumePlan | None: ...

    async def load_terminal_reentry(
        self, grant: RunnerCheckpointGrant
    ) -> TerminalGateReentryPlan | None: ...

    async def select_terminal_reentry(
        self, grant: RunnerCheckpointGrant, *, source_checkpoint_id: str
    ) -> TerminalGateReentryPlan | None: ...


class RunnerExecutionService:
    """Single entry that derives Graph write authority from a repository claim.

    A process error leaves the Run claimed until its lease expires. The next
    claim then reconciles checkpoint receipts; it is never marked as a failed
    business Run merely because this process stopped.
    """

    def __init__(
        self,
        *,
        repository: _RunnerRuns,
        graph_runtime: GraphRuntimePort,
        feedback_service: _RunnerFeedback,
        build_gate_open_request: Callable[[RunRecord, GraphExecutionResult], OpenInterruptRequest],
        build_pause_open_request: Callable[[RunRecord, GraphExecutionResult], OpenInterruptRequest],
        build_gate_reentry_update: Callable[[GateResumePlan, list[Any] | None], dict[str, Any]],
        bind_execution_context: Callable[[TenantExecutionContext], AbstractContextManager[None]],
        runner_id: str,
        lease_seconds: int = 30,
        heartbeat_interval_seconds: float = 10,
        recoverable_execution_error: Callable[[Exception], str | None] | None = None,
    ) -> None:
        if not runner_id.strip():
            raise ValueError("runner_id must not be empty")
        if lease_seconds <= 0 or not 0 < heartbeat_interval_seconds < lease_seconds:
            raise ValueError("heartbeat interval must be within the positive lease")
        self._repository = repository
        self._graph_runtime = graph_runtime
        self._feedback_service = feedback_service
        self._build_gate_open_request = build_gate_open_request
        self._build_pause_open_request = build_pause_open_request
        self._build_gate_reentry_update = build_gate_reentry_update
        self._bind_execution_context = bind_execution_context
        self._runner_id = runner_id
        self._lease_seconds = lease_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._recoverable_execution_error = recoverable_execution_error
        self._dispatch_lock = asyncio.Lock()

    async def execute_next(self) -> RunRecord | None:
        """Claim at most one Run and drive its initial durable Graph operation."""

        async with self._dispatch_lock:
            run = await self._repository.claim_next_run(
                self._runner_id, lease_seconds=self._lease_seconds
            )
            if run is None:
                return None
            if run.execution_protocol is None:
                raise ExecutionProtocolMismatchError("claimed Run has no execution protocol")
            require_current_execution_protocol(
                run.execution_protocol,
                expected=self._graph_runtime.execution_protocol,
                module=run.module,
                stage_key=run.stage_key,
            )
            grant = RunnerCheckpointGrant.from_claim(run, runner_id=self._runner_id)
            with self._bind_execution_context(grant.scope.identity):
                terminal_reentry = await self._feedback_service.load_terminal_reentry(grant)
                run_continue = (
                    None if terminal_reentry is not None
                    else await self._feedback_service.load_run_continue(grant)
                )
                plan = (
                    None if terminal_reentry is not None or run_continue is not None
                    else await self._feedback_service.load_gate_resume(grant)
                )
                operation_id = (
                    terminal_reentry.operation_id if terminal_reentry is not None
                    else run_continue[0] if run_continue is not None
                    else plan.operation_id if plan is not None
                    else f"run:{run.run_id}:start"
                )
                if terminal_reentry is not None:
                    result = await self._run_with_heartbeat(
                        run, grant, lambda: self._continue_terminal_reentry(
                            run, grant, terminal_reentry
                        )
                    )
                elif run_continue is not None:
                    _, graph_interrupt_id, envelope = run_continue
                    cursor = envelope.checkpoint_cursor
                    result = await self._run_with_heartbeat(
                        run, grant, lambda: self._graph_runtime.resume_durable(
                            grant=grant, thread_id=run.thread_id,
                            source_checkpoint_id=cursor.checkpoint_id,
                            checkpoint_ns=cursor.checkpoint_ns,
                            operation_id=operation_id, target_run_id=run.run_id,
                            resume_value={graph_interrupt_id: envelope.model_dump(mode="json")},
                            lease_guard=lambda: self._require_current_claim(run, grant),
                        )
                    )
                elif plan is None:
                    result = await self._run_with_heartbeat(
                        run, grant, lambda: self._graph_runtime.start_durable(
                            grant=grant, thread_id=run.thread_id,
                            operation_id=operation_id, target_run_id=run.run_id,
                            input_value=run.input_payload,
                            lease_guard=lambda: self._require_current_claim(run, grant),
                        )
                    )
                else:
                    result = await self._run_with_heartbeat(
                        run, grant, lambda: self._graph_runtime.resume_durable(
                            grant=grant, thread_id=run.thread_id,
                            source_checkpoint_id=plan.source_checkpoint_cursor.checkpoint_id,
                            checkpoint_ns=plan.source_checkpoint_cursor.checkpoint_ns,
                            operation_id=operation_id, target_run_id=run.run_id,
                            resume_value=plan.model_dump(mode="json"),
                            lease_guard=lambda: self._require_current_claim(run, grant),
                        )
                    )
                for attempt in range(2):
                    if result.interrupts:
                        signals = tuple(item.get("value") for item in result.interrupts)
                        if len(signals) == 1 and isinstance(signals[0], Mapping) and (
                            signals[0].get("kind") == "run_pause"
                        ):
                            request = self._build_pause_open_request(run, result)
                            return await self._feedback_service.open_run_pause(grant, request)
                        request = self._build_gate_open_request(run, result)
                        await self._feedback_service.open_gate(grant, request)
                        return await self._feedback_service.park_waiting_user(
                            grant, request.requested_interrupt_id
                        )
                    if result.checkpoint.next_nodes:
                        return run
                    await self._require_terminal_root(run, result, operation_id)
                    event = make_completion_checkpoint_event(
                        run,
                        root_checkpoint_id=result.checkpoint.checkpoint_id,
                        graph_operation_id=operation_id,
                    )
                    try:
                        return await self._repository.complete(
                            run.run_id, self._runner_id, grant.lease_token, events=(event,)
                        )
                    except ConflictError:
                        if attempt:
                            raise
                        recovery = await self._feedback_service.select_terminal_reentry(
                            grant,
                            source_checkpoint_id=result.checkpoint.checkpoint_id,
                        )
                        if recovery is None:
                            raise
                        result = await self._run_with_heartbeat(
                            run, grant, lambda: self._continue_terminal_reentry(
                                run, grant, recovery
                            )
                        )
                        operation_id = recovery.operation_id
                raise RuntimeError("terminal Gate recovery did not settle")

    async def _continue_terminal_reentry(
        self,
        run: RunRecord,
        grant: RunnerCheckpointGrant,
        selection: TerminalGateReentryPlan,
    ) -> GraphExecutionResult:
        saved = await self._graph_runtime.get_checkpoint_state(
            thread_id=run.thread_id,
            checkpoint_id=selection.source_checkpoint_id,
            checkpoint_ns="",
        )
        if (
            saved is None
            or saved.checkpoint.checkpoint_ns
            or saved.checkpoint.root_checkpoint_id != selection.source_checkpoint_id
            or saved.target_run_id != run.run_id
        ):
            raise RuntimeError("terminal Gate reentry source is not an exact saved root")
        values = saved.values if isinstance(saved.values, Mapping) else {}
        update = self._build_gate_reentry_update(
            selection.gate_plan, values.get("clarifications")
        )
        await self._graph_runtime.checkpoint_state_durable(
            grant=grant,
            thread_id=run.thread_id,
            source_checkpoint_id=selection.source_checkpoint_id,
            operation_id=selection.operation_id,
            target_run_id=run.run_id,
            values=update,
            as_node="user_gate_route",
        )
        resumed = await self._graph_runtime.reconcile_durable_resume(
            grant=grant,
            thread_id=run.thread_id,
            source_checkpoint_id=selection.source_checkpoint_id,
            operation_id=selection.operation_id,
            target_run_id=run.run_id,
            continue_pending=True,
        )
        if resumed is None:
            raise RuntimeError("terminal Gate reentry has no durable receipt")
        return resumed

    async def _require_terminal_root(
        self, run: RunRecord, result: GraphExecutionResult, operation_id: str
    ) -> None:
        root = result.checkpoint
        if root.checkpoint_ns or root.root_checkpoint_id != root.checkpoint_id:
            raise RuntimeError("completed Graph operation has no terminal root checkpoint")
        saved = await self._graph_runtime.get_checkpoint_state(
            thread_id=run.thread_id, checkpoint_id=root.checkpoint_id, checkpoint_ns=""
        )
        if (
            saved is None
            or saved.checkpoint != root
            or saved.checkpoint.next_nodes
            or saved.interrupts
            or saved.graph_operation_id != operation_id
            or saved.target_run_id != run.run_id
        ):
            raise RuntimeError("completed Graph root checkpoint was not saved exactly")

    async def _require_current_claim(
        self, run: RunRecord, grant: RunnerCheckpointGrant
    ) -> None:
        current = await self._repository.heartbeat(
            run.run_id, self._runner_id, grant.lease_token,
            lease_seconds=self._lease_seconds,
        )
        if (
            current.thread_id != grant.thread_id
            or current.writer_generation != grant.writer_generation
            or current.claimed_by != grant.runner_id
        ):
            raise LeaseConflictError("Graph continuation has no current Run claim")

    async def _run_with_heartbeat(
        self,
        run: RunRecord,
        grant: RunnerCheckpointGrant,
        operation: Callable[[], Awaitable[GraphExecutionResult]],
    ) -> GraphExecutionResult:
        stop = asyncio.Event()
        graph_task = asyncio.create_task(operation())
        heartbeat_task = asyncio.create_task(self._heartbeat_until_stopped(run, grant, stop))
        try:
            done, _pending = await asyncio.wait(
                {graph_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat_task in done:
                await heartbeat_task
                raise RuntimeError("Runner heartbeat ended before Graph execution")
            result = await graph_task
            stop.set()
            await heartbeat_task
            return result
        except Exception as error:
            reason = (
                self._recoverable_execution_error(error)
                if self._recoverable_execution_error is not None else None
            )
            if reason is not None:
                # The failed Graph invocation has ended. Stop its heartbeat
                # before releasing the same claim, without inventing an interrupt.
                stop.set()
                await heartbeat_task
                await self._repository.pause_claimed(
                    run.run_id, self._runner_id, grant.lease_token, error=reason,
                )
            raise
        finally:
            stop.set()
            for task in (graph_task, heartbeat_task):
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    async def _heartbeat_until_stopped(
        self, run: RunRecord, grant: RunnerCheckpointGrant, stop: asyncio.Event
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), self._heartbeat_interval_seconds)
            except TimeoutError:
                await self._repository.heartbeat(
                    run.run_id,
                    self._runner_id,
                    grant.lease_token,
                    lease_seconds=self._lease_seconds,
                )


__all__ = ["RunnerExecutionService"]
