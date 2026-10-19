"""Persistence boundary for Task-scoped geometry query attempts."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from CAD2BIM_backend.domain.geometry_query import (
    GeometryQueryAttemptPreparation,
    GeometryQueryAttemptRecord,
    GeometryQueryAttemptStatus,
    GeometryQueryAttemptTerminal,
)
from CAD2BIM_backend.domain.identity import TenantExecutionContext


class GeometryQueryRepository(Protocol):
    async def create_geometry_query_attempt(
        self,
        context: TenantExecutionContext,
        preparation: GeometryQueryAttemptPreparation,
    ) -> GeometryQueryAttemptRecord: ...

    async def complete_geometry_query_attempt(
        self,
        context: TenantExecutionContext,
        *,
        attempt_id: str,
        terminal: GeometryQueryAttemptTerminal,
    ) -> GeometryQueryAttemptRecord: ...

    async def get_geometry_query_attempt(
        self,
        context: TenantExecutionContext,
        *,
        attempt_id: str,
    ) -> GeometryQueryAttemptRecord | None: ...

    async def list_terminal_geometry_query_attempts(
        self,
        context: TenantExecutionContext,
        *,
        statuses: tuple[GeometryQueryAttemptStatus, ...],
        before_created_at: datetime | None,
        before_attempt_id: str | None,
        limit: int,
    ) -> tuple[GeometryQueryAttemptRecord, ...]: ...


__all__ = ["GeometryQueryRepository"]
