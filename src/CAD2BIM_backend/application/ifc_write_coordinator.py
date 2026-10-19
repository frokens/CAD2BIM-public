"""Process-local ordering and single-flight for durable IFC writes."""

from __future__ import annotations

import asyncio
from contextlib import AbstractContextManager, nullcontext
from contextvars import Context
from collections.abc import Awaitable, Callable
from typing import TypeVar

from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity


_T = TypeVar("_T")


class IfcWriteCoordinator:
    """Serialize writes per Run and share one in-process logical invocation.

    This coordinator owns no durable state and performs no repository, Storage,
    or IFC work.  PostgreSQL remains the cross-process idempotency and CURRENT
    fence; this object only prevents avoidable duplicate worker work inside one
    backend process.
    """

    def __init__(
        self,
        *,
        get_execution_context: Callable[[], TenantExecutionContext | None] | None = None,
        bind_execution_context: Callable[
            [TenantExecutionContext], AbstractContextManager[None]
        ] | None = None,
    ) -> None:
        if (get_execution_context is None) != (bind_execution_context is None):
            raise ValueError("IFC execution context capture and binding must be supplied together")
        self._get_execution_context = get_execution_context or (lambda: None)
        self._bind_execution_context = bind_execution_context or (lambda _context: nullcontext())
        self._claims_lock = asyncio.Lock()
        self._claims: dict[tuple[str, str, str, str, str], asyncio.Task[object]] = {}
        self._run_locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}
        self._closed = False

    async def invoke(
        self,
        scope: IfcRunIdentity,
        *,
        logical_operation_identity: str,
        operation: Callable[[], Awaitable[_T]],
        deadline: float | None = None,
    ) -> _T:
        """Run or join one logical write while preserving Run order."""

        identity = logical_operation_identity.strip()
        if not identity:
            raise ValueError("logical_operation_identity must not be empty")
        run_key = (scope.owner_id, scope.tenant_id, scope.task_id, scope.run_id)
        claim_key = (*run_key, identity)
        async with self._claims_lock:
            if self._closed:
                raise RuntimeError("IFC write coordinator is closing")
            task = self._claims.get(claim_key)
            if task is None:
                run_lock = self._run_locks.setdefault(run_key, asyncio.Lock())
                tenant_context = self._get_execution_context()

                async def ordered() -> _T:
                    binding = (
                        self._bind_execution_context(tenant_context)
                        if tenant_context is not None else nullcontext()
                    )
                    with binding:
                        async with run_lock:
                            return await operation()

                # An empty Task context excludes Request, bearer, old Runner
                # grant, and incidental per-request ContextVars.
                task = asyncio.create_task(ordered(), context=Context())
                self._claims[claim_key] = task  # type: ignore[assignment]

                def on_done(done: asyncio.Task[_T]) -> None:
                    asyncio.create_task(
                        self._retire(claim_key, run_key, done), context=Context(),
                    )

                task.add_done_callback(on_done, context=Context())

        async with asyncio.timeout_at(deadline):
            return await asyncio.shield(task)  # type: ignore[return-value]

    async def _retire(self, claim_key, run_key, done) -> None:
        # Observe a detached failure so it does not become an unhandled Task.
        if not done.cancelled():
            done.exception()
        async with self._claims_lock:
            if self._claims.get(claim_key) is done:
                self._claims.pop(claim_key, None)
            if not any(key[:4] == run_key for key in self._claims):
                self._run_locks.pop(run_key, None)

    async def close(self, *, drain_seconds: float = 5.0, stop_seconds: float = 30.0) -> None:
        """Stop admission, drain accepted work, then stop and reconcile owners."""
        async with self._claims_lock:
            self._closed = True
            tasks = set(self._claims.values())
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=drain_seconds)
        for task in pending:
            task.cancel()
        if pending:
            _, pending = await asyncio.wait(pending, timeout=stop_seconds)
        if pending:
            raise RuntimeError("IFC write owners did not stop before resource shutdown")


__all__ = ["IfcWriteCoordinator"]
