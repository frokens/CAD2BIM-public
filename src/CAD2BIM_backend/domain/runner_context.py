"""Process-private authority derived from a claimed Run."""

from __future__ import annotations

from dataclasses import dataclass, field

from CAD2BIM_backend.domain.control_plane import ExecutionMode, RunRecord, RunStatus
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope


@dataclass(frozen=True, slots=True)
class RunnerGraphContext:
    """Non-secret scope available to Graph nodes through runtime.context."""

    scope: InvocationScope


@dataclass(frozen=True, slots=True)
class RunnerCheckpointGrant:
    """Current claim held only while one Runner executes the Graph."""

    graph_context: RunnerGraphContext
    thread_id: str
    runner_id: str
    lease_token: str = field(repr=False)
    writer_generation: int

    @property
    def scope(self) -> InvocationScope:
        return self.graph_context.scope

    @classmethod
    def from_claim(cls, run: RunRecord, *, runner_id: str) -> "RunnerCheckpointGrant":
        tenant_id, principal_id = run.require_execution_identity()
        if (
            run.execution_mode is not ExecutionMode.GRAPH
            or run.status is not RunStatus.RUNNING
            or run.claimed_by != runner_id
            or not run.lease_token
            or run.writer_generation < 1
            or run.owner_id != tenant_id
        ):
            raise ValueError("Run has no current Graph claim")
        scope = InvocationScope(
            identity=TenantExecutionContext(
                tenant_id=tenant_id,
                principal_id=principal_id,
                task_id=run.task_id,
                actor_kind=TenantActorKind.RUNNER,
                command_id=f"run:{run.run_id}:generation:{run.writer_generation}",
            ),
            owner_id=run.owner_id,
            run_id=run.run_id,
            execution_mode=run.execution_mode,
            catalog_frontier=None,
        )
        return cls(
            graph_context=RunnerGraphContext(scope=scope),
            thread_id=run.thread_id,
            runner_id=runner_id,
            lease_token=run.lease_token,
            writer_generation=run.writer_generation,
        )


__all__ = ["RunnerCheckpointGrant", "RunnerGraphContext"]
