"""Checkpoint writes fenced by the current Run claim on the saver connection."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from CAD2BIM_backend.domain.control_plane import LeaseConflictError
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant


class FencedAsyncPostgresSaver(AsyncPostgresSaver):
    """Keep each upstream save and its Run check in one short transaction.

    The locked checkpoint-postgres 3.1.0 implementation sends aput and
    aput_writes through ``_cursor``.  Calling its cursor with pipeline disabled
    retains its connection lock and serialization while this adapter opens an
    explicit transaction on that same psycopg connection.
    """

    def __init__(self, conn: Any, pipe: Any = None, serde: Any = None) -> None:
        super().__init__(conn=conn, pipe=pipe, serde=serde)
        self._active_grant: ContextVar[RunnerCheckpointGrant | None] = ContextVar(
            "cad2bim_active_checkpoint_grant", default=None
        )
        self._write_grant: ContextVar[RunnerCheckpointGrant | None] = ContextVar(
            "cad2bim_checkpoint_write_grant", default=None
        )

    @contextmanager
    def bind_execution(self, grant: RunnerCheckpointGrant) -> Iterator[None]:
        previous = self._active_grant.get()
        if previous is not None and previous != grant:
            raise LeaseConflictError("another Run claim is already bound")
        token = self._active_grant.set(grant)
        try:
            yield
        finally:
            self._active_grant.reset(token)

    def require_current_execution_grant(self) -> RunnerCheckpointGrant:
        """Expose this invocation's private claim only to trusted in-process callbacks."""

        grant = self._active_grant.get()
        if grant is None:
            raise LeaseConflictError("no Run claim is bound to this Graph invocation")
        return grant

    def _require_write_grant(self, config: dict[str, Any]) -> RunnerCheckpointGrant:
        grant = self._active_grant.get()
        configurable = config.get("configurable", {})
        if grant is None or configurable.get("thread_id") != grant.thread_id:
            raise LeaseConflictError("checkpoint write has no matching Run claim")
        return grant

    async def aput(self, config, checkpoint, metadata, new_versions):
        grant = self._require_write_grant(config)
        token = self._write_grant.set(grant)
        try:
            return await super().aput(config, checkpoint, metadata, new_versions)
        finally:
            self._write_grant.reset(token)

    async def aput_writes(
        self,
        config,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        grant = self._require_write_grant(config)
        token = self._write_grant.set(grant)
        try:
            await super().aput_writes(config, writes, task_id, task_path)
        finally:
            self._write_grant.reset(token)

    @asynccontextmanager
    async def _cursor(self, *, pipeline: bool = False) -> AsyncIterator[Any]:
        grant = self._write_grant.get()
        if grant is None:
            async with super()._cursor(pipeline=pipeline) as cursor:
                yield cursor
            return
        if self.pipe is not None:
            raise RuntimeError("fenced checkpoint writes require a non-pipeline saver")
        async with super()._cursor(pipeline=False) as cursor:
            async with cursor.connection.transaction():
                identity = grant.scope.identity
                await cursor.execute(
                    "SELECT set_config('cad2bim.tenant_id', %s, true), "
                    "set_config('cad2bim.principal_id', %s, true), "
                    "set_config('cad2bim.task_id', %s, true), "
                    "set_config('cad2bim.run_id', %s, true)",
                    (
                        identity.tenant_id,
                        identity.principal_id,
                        identity.task_id,
                        grant.scope.run_id,
                    ),
                )
                await cursor.fetchone()
                await cursor.execute(
                    "SELECT run_id, task_id, owner_id, tenant_id, thread_id, "
                    "execution_mode, execution_protocol_status, status, claimed_by, "
                    "lease_token, writer_generation, lease_expires_at "
                    "FROM runs WHERE run_id = %s FOR UPDATE",
                    (grant.scope.run_id,),
                )
                run = await cursor.fetchone()
                await cursor.execute("SELECT clock_timestamp() AS checked_at")
                clock = await cursor.fetchone()
                if (
                    run is None
                    or clock is None
                    or not self._matches_claim(run, clock["checked_at"], grant)
                ):
                    raise LeaseConflictError("checkpoint write has no current Run claim")
                yield cursor

    @staticmethod
    def _matches_claim(run: dict[str, Any], checked_at: Any, grant: RunnerCheckpointGrant) -> bool:
        identity = grant.scope.identity
        return (
            run["run_id"] == grant.scope.run_id
            and run["task_id"] == identity.task_id
            and run["owner_id"] == grant.scope.owner_id
            and run["tenant_id"] == identity.tenant_id
            and run["thread_id"] == grant.thread_id
            and run["execution_mode"] == "graph"
            and run["execution_protocol_status"] == "pinned"
            and run["status"] == "running"
            and run["claimed_by"] == grant.runner_id
            and run["lease_token"] == grant.lease_token
            and run["writer_generation"] == grant.writer_generation
            and run["lease_expires_at"] is not None
            and run["lease_expires_at"] > checked_at
        )

    async def adelete_thread(self, thread_id: str) -> None:
        raise RuntimeError("Runner checkpoint deletion is unavailable")

    def put(self, config, checkpoint, metadata, new_versions):
        raise RuntimeError("Runner uses async checkpoint writes only")

    def put_writes(self, config, writes, task_id: str, task_path: str = "") -> None:
        raise RuntimeError("Runner uses async checkpoint writes only")

    def delete_thread(self, thread_id: str) -> None:
        raise RuntimeError("Runner checkpoint deletion is unavailable")

    async def setup(self) -> None:
        raise RuntimeError("Runner cannot change the checkpoint schema")


__all__ = ["FencedAsyncPostgresSaver"]
