"""Explicit deployment command for the LangGraph checkpointer schema."""

from __future__ import annotations

import asyncio
import psycopg
from psycopg import sql

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from CAD2BIM_backend.config import Settings

from .runtime import _psycopg_connection_string

CHECKPOINT_TABLES = (
    "checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes",
)


async def _grant_runner_access(database_url: str) -> None:
    """The checkpointer owner grants its own tables after successful setup."""
    async with await psycopg.AsyncConnection.connect(_psycopg_connection_string(database_url)) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT current_schema()")
            row = await cursor.fetchone()
            if row is None or not row[0]:
                raise RuntimeError("PostgreSQL current schema is unavailable")
            await cursor.execute(sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {} TO {}"
            ).format(
                sql.SQL(", ").join(sql.Identifier(str(row[0]), table) for table in CHECKPOINT_TABLES),
                sql.Identifier("cad2bim_runner_runtime"),
            ))


async def setup_checkpoint_schema(database_url: str) -> None:
    """Apply LangGraph's own idempotent checkpoint migrations.

    This command is run during deployment, separately from API/Runner startup;
    the production Runner remains read-only with respect to database schemas.
    """

    context = AsyncPostgresSaver.from_conn_string(
        _psycopg_connection_string(database_url)
    )
    saver = await context.__aenter__()
    try:
        await saver.setup()
    finally:
        await context.__aexit__(None, None, None)
    await _grant_runner_access(database_url)


def cli() -> None:
    settings = Settings.load()
    database_url = str(settings.database_url or "").strip()
    if not database_url:
        raise RuntimeError("CAD2BIM_DATABASE_URL is required for checkpoint schema setup")
    asyncio.run(setup_checkpoint_schema(database_url))


if __name__ == "__main__":
    cli()


__all__ = ["setup_checkpoint_schema"]
