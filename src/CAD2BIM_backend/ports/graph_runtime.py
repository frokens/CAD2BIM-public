"""Provider-neutral durable Graph runtime contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest


class GraphCheckpointDescriptor(BaseModel):
    """Logical checkpoint identity safe to persist in the control plane."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: str
    checkpoint_id: str
    checkpoint_ns: str = ""
    root_checkpoint_id: str | None = None
    checkpoint_map: tuple[tuple[str, str], ...] = ()
    checkpoint_map_digest: str | None = Field(default=None, validate_default=True)
    next_nodes: tuple[str, ...] = ()
    created_at: str | None = None

    @field_validator("checkpoint_map_digest")
    @classmethod
    def validate_checkpoint_lineage(
        cls, digest: str | None, info: ValidationInfo
    ) -> str | None:
        root_checkpoint_id = info.data.get("root_checkpoint_id")
        checkpoint_map = info.data.get("checkpoint_map", ())
        lineage_present = (
            root_checkpoint_id is not None,
            bool(checkpoint_map),
            digest is not None,
        )
        if any(lineage_present) and not all(lineage_present):
            raise ValueError("checkpoint descriptor lineage fields must be provided together")
        if all(lineage_present):
            mapping = dict(checkpoint_map)
            if (
                len(mapping) != len(checkpoint_map)
                or mapping.get("") != root_checkpoint_id
                or mapping.get(info.data.get("checkpoint_ns")) != info.data.get("checkpoint_id")
            ):
                raise ValueError("checkpoint descriptor lineage is inconsistent")
            checkpoint_id = info.data.get("checkpoint_id")
            checkpoint_ns = info.data.get("checkpoint_ns")
            if checkpoint_id is None or checkpoint_ns is None:
                return digest
            expected = checkpoint_cursor_digest(
                checkpoint_id=checkpoint_id,
                checkpoint_ns=checkpoint_ns,
                checkpoint_map=checkpoint_map,
            )
            if digest != expected:
                raise ValueError("checkpoint descriptor map digest differs")
        return digest


class GraphExecutionResult(BaseModel):
    """Bounded result returned by start/resume operations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    values: Any
    checkpoint: GraphCheckpointDescriptor
    interrupts: tuple[dict[str, Any], ...] = ()


class GraphCheckpointState(BaseModel):
    """Values and lineage read from one explicitly named checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    values: Any
    checkpoint: GraphCheckpointDescriptor
    interrupts: tuple[dict[str, Any], ...] = ()
    graph_operation_id: str | None = None
    target_run_id: str | None = None


class GraphRuntimePort(Protocol):
    @property
    def execution_protocol(self) -> ExecutionProtocolBinding: ...

    async def start_durable(
        self,
        *,
        grant: RunnerCheckpointGrant,
        thread_id: str,
        operation_id: str,
        target_run_id: str,
        input_value: Any,
        checkpoint_ns: str = "",
        lease_guard: Callable[[], Awaitable[None]] | None = None,
    ) -> GraphExecutionResult:
        """Start once per durable Run and reconcile from its operation checkpoint.

        Every checkpoint write uses the current server-owned Run claim.
        """

        ...

    async def resume_durable(
        self,
        *,
        grant: RunnerCheckpointGrant,
        thread_id: str,
        source_checkpoint_id: str,
        operation_id: str,
        target_run_id: str,
        resume_value: Any,
        state_update: Any | None = None,
        checkpoint_ns: str = "",
        lease_guard: Callable[[], Awaitable[None]] | None = None,
    ) -> GraphExecutionResult:
        """Resume once per durable operation and reconcile after process failure.

        Implementations probe checkpoints tagged with ``operation_id`` before
        issuing ``Command(resume=...)`` and bind the current claim to writes.
        """

        ...

    async def reconcile_durable_resume(
        self,
        *,
        grant: RunnerCheckpointGrant,
        thread_id: str,
        source_checkpoint_id: str,
        operation_id: str,
        target_run_id: str,
        checkpoint_ns: str = "",
        continue_pending: bool = False,
        lease_guard: Callable[[], Awaitable[None]] | None = None,
    ) -> GraphExecutionResult | None:
        """Return/continue an existing resume receipt without issuing a new resume."""

        ...

    async def checkpoint_state_durable(
        self,
        *,
        grant: RunnerCheckpointGrant,
        thread_id: str,
        source_checkpoint_id: str,
        operation_id: str,
        target_run_id: str,
        values: Any,
        checkpoint_ns: str = "",
        as_node: str | None = None,
        lease_guard: Callable[[], Awaitable[None]] | None = None,
    ) -> GraphCheckpointDescriptor:
        """Create or reconcile one exact state-update checkpoint."""

        ...

    async def get_checkpoint(
        self,
        *,
        thread_id: str,
        checkpoint_id: str,
        checkpoint_ns: str = "",
    ) -> GraphCheckpointDescriptor | None: ...

    async def get_checkpoint_state(
        self,
        *,
        thread_id: str,
        checkpoint_id: str,
        checkpoint_ns: str = "",
        observe: bool = True,
    ) -> GraphCheckpointState | None:
        """Read one exact cursor and its values; thread-latest reads are forbidden."""

        ...

    async def close(self) -> None: ...


__all__ = [
    "GraphCheckpointDescriptor",
    "GraphCheckpointState",
    "GraphExecutionResult",
    "GraphRuntimePort",
]
