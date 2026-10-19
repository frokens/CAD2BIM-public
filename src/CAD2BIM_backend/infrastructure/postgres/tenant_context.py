"""Transaction-local PostgreSQL identity context for tenant-scoped work."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope


_CURRENT_CONTEXT: ContextVar[TenantExecutionContext | None] = ContextVar(
    "cad2bim_tenant_execution_context",
    default=None,
)


class TenantContextMismatch(RuntimeError):
    """A write attempted to escape the invocation's validated tenant."""


class TenantAwareSyncSession(Session):
    """Sync delegate used by SQLAlchemy's async tenant-aware session."""


class TenantAwareAsyncSession(AsyncSession):
    sync_session_class = TenantAwareSyncSession


def current_tenant_execution_context() -> TenantExecutionContext | None:
    return _CURRENT_CONTEXT.get()


@contextmanager
def bind_tenant_execution_context(
    context: TenantExecutionContext,
) -> Iterator[None]:
    active = _CURRENT_CONTEXT.get()
    if active is not None:
        if active != context:
            raise TenantContextMismatch("another tenant context is already active")
        yield
        return
    token = _CURRENT_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_CONTEXT.reset(token)


@event.listens_for(TenantAwareSyncSession, "after_begin")
def _set_context_after_begin(_session, _transaction, connection) -> None:
    context = _CURRENT_CONTEXT.get()
    if context is None or connection.dialect.name != "postgresql":
        return
    connection.execute(
        text(
            """
            SELECT
                set_config('cad2bim.tenant_id', :tenant_id, true),
                set_config('cad2bim.principal_id', :principal_id, true),
                set_config('cad2bim.task_id', :task_id, true)
            """
        ),
        {
            "tenant_id": context.tenant_id,
            "principal_id": context.principal_id,
            "task_id": context.task_id or "",
        },
    )


@event.listens_for(TenantAwareSyncSession, "before_flush")
def _bind_new_rows_to_context(session, _flush_context, _instances) -> None:
    context = _CURRENT_CONTEXT.get()
    if context is None:
        return
    for value in session.new:
        if not hasattr(value, "tenant_id"):
            continue
        tenant_id = getattr(value, "tenant_id")
        if tenant_id is None:
            setattr(value, "tenant_id", context.tenant_id)
        elif tenant_id != context.tenant_id:
            raise TenantContextMismatch("new row belongs to another tenant")


async def set_tenant_execution_context(
    session: AsyncSession,
    context: TenantExecutionContext,
) -> None:
    """Bind RLS inputs to the current transaction without leaking through the pool."""

    await session.execute(
        text(
            """
            SELECT
                set_config('cad2bim.tenant_id', :tenant_id, true),
                set_config('cad2bim.principal_id', :principal_id, true),
                set_config('cad2bim.task_id', :task_id, true)
            """
        ),
        {
            "tenant_id": context.tenant_id,
            "principal_id": context.principal_id,
            "task_id": context.task_id or "",
        },
    )


async def set_run_execution_context(session: AsyncSession, run_id: str) -> None:
    """Bind one server-resolved Run to the current PostgreSQL transaction."""

    await session.execute(
        text("SELECT set_config('cad2bim.run_id', :run_id, true)"),
        {"run_id": run_id},
    )


async def set_invocation_scope(session: AsyncSession, scope: InvocationScope) -> None:
    """Project one authoritative InvocationScope into transaction-local RLS inputs."""

    await set_tenant_execution_context(session, scope.identity)
    await set_run_execution_context(session, scope.run_id)


__all__ = [
    "TenantAwareAsyncSession",
    "TenantContextMismatch",
    "bind_tenant_execution_context",
    "current_tenant_execution_context",
    "set_invocation_scope",
    "set_run_execution_context",
    "set_tenant_execution_context",
]
