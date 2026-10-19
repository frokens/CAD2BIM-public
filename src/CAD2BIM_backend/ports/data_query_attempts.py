"""Persistence boundary for Run-scoped DataQueryAttempt facts."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from CAD2BIM_backend.domain.data_query_attempt import (
    DataQueryAttemptPreparation,
    DataQueryAttemptRecord,
    DataQueryAttemptStatus,
    DataQueryAttemptTerminal,
    DataQueryEngine,
    DataQueryPrepared,
)
from CAD2BIM_backend.domain.invocation import InvocationScope


class DataQueryAttemptRepository(Protocol):
    async def prepare_data_query_attempt(
        self, scope: InvocationScope, preparation: DataQueryAttemptPreparation
    ) -> DataQueryPrepared: ...

    async def get_data_query_attempt(
        self, scope: InvocationScope, *, attempt_id: str
    ) -> DataQueryAttemptRecord | None: ...

    async def commit_data_query_attempt_terminal(
        self,
        scope: InvocationScope,
        *,
        attempt_id: str,
        terminal: DataQueryAttemptTerminal,
    ) -> DataQueryAttemptRecord: ...

    async def list_terminal_data_query_attempts(
        self,
        scope: InvocationScope,
        *,
        engine: DataQueryEngine,
        statuses: tuple[DataQueryAttemptStatus, ...],
        before_created_at: datetime | None,
        before_attempt_id: str | None,
        limit: int,
    ) -> tuple[DataQueryAttemptRecord, ...]: ...


__all__ = ["DataQueryAttemptRepository"]
