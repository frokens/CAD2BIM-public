"""The existing Task/Run repository operations needed by the user entry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from CAD2BIM_backend.domain.control_plane import (
    CreateTaskRequest,
    RunRecord,
    StartRunRequest,
    TaskRecord,
)
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.run_events import RunEvent, RunEventAudience
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant


class TaskRunRepository(Protocol):
    async def set_generated_task_title(self, grant: RunnerCheckpointGrant, title: str) -> TaskRecord: ...

    async def create_task(self, owner_id: str, request: CreateTaskRequest) -> TaskRecord: ...

    async def get_task(self, owner_id: str, task_id: str) -> TaskRecord | None: ...

    async def delete_task(self, owner_id: str, task_id: str) -> None: ...

    async def create_run(
        self,
        owner_id: str,
        task_id: str,
        request: StartRunRequest,
        *,
        execution_protocol_factory: Callable[[], ExecutionProtocolBinding],
    ) -> RunRecord: ...

    async def get_run(self, owner_id: str, run_id: str) -> RunRecord | None: ...

    async def get_run_with_execution_identity(
        self, owner_id: str, run_id: str
    ) -> RunRecord | None: ...

    async def get_completion_checkpoint_events(
        self, owner_id: str, run_id: str
    ) -> tuple[RunEvent, ...]: ...

    async def list_tasks(self, owner_id: str, *, after_task_id: str | None = None, limit: int = 50) -> tuple[tuple[TaskRecord, RunRecord | None], ...]: ...
    async def read_chat_snapshot(self, owner_id: str, run_id: str, *, before_sequence: int | None = None, limit: int = 100) -> tuple[RunRecord, tuple[RunEvent, ...], int, int | None, tuple[RunEvent, ...]]:
        """Read a public event page, its item histories and activity at one boundary."""
        ...
    async def list_events(self, owner_id: str, run_id: str, *, after_sequence: int = 0,
                          limit: int = 100, through_sequence: int | None = None,
                          audience: RunEventAudience | None = None) -> tuple[RunEvent, ...]: ...
    async def get_observed_run(self, run_id: str, thread_id: str) -> tuple[TenantExecutionContext, RunRecord]: ...
    async def append_chat_events(self, owner_id: str, run_id: str, events: tuple[RunEvent, ...]) -> tuple[RunEvent, ...]: ...
    async def get_item_events(self, owner_id: str, run_id: str, item_id: str) -> tuple[RunEvent, ...]: ...
    async def pause_run(self, owner_id: str, run_id: str, *, client_request_id: str) -> RunRecord: ...
    async def continue_run(self, owner_id: str, run_id: str, *, client_request_id: str) -> RunRecord: ...


__all__ = ["TaskRunRepository"]
