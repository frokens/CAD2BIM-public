"""Persistence boundary for Run-scoped durable IFC query attempts."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from CAD2BIM_backend.domain.ifc_query import (
    IfcQueryAttemptPreparation,
    IfcQueryAttemptRecord,
    IfcQueryAttemptStatus,
    IfcQueryAttemptTerminal,
    IfcQueryPrepared,
)
from CAD2BIM_backend.domain.invocation import InvocationScope


class IfcQueryRepository(Protocol):
    async def prepare_ifc_query_attempt(
        self,
        scope: InvocationScope,
        preparation: IfcQueryAttemptPreparation,
    ) -> IfcQueryPrepared: ...

    async def get_ifc_query_attempt(
        self,
        scope: InvocationScope,
        *,
        attempt_id: str,
    ) -> IfcQueryAttemptRecord | None: ...

    async def commit_ifc_query_attempt_terminal(
        self,
        scope: InvocationScope,
        *,
        attempt_id: str,
        terminal: IfcQueryAttemptTerminal,
    ) -> IfcQueryAttemptRecord: ...

    async def list_terminal_ifc_query_attempts(
        self,
        scope: InvocationScope,
        *,
        statuses: tuple[IfcQueryAttemptStatus, ...],
        before_created_at: datetime | None,
        before_attempt_id: str | None,
        limit: int,
    ) -> tuple[IfcQueryAttemptRecord, ...]: ...


__all__ = ["IfcQueryRepository"]
