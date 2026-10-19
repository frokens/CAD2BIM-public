"""Async SQLAlchemy engine and session factory construction.

Importing this module never opens a connection or creates database objects.
Schema changes are exclusively owned by Alembic migrations.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantAwareAsyncSession,
)


EXPECTED_SCHEMA_REVISION = "20261017_0113"


@dataclass(frozen=True, slots=True)
class Database:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    async def dispose(self) -> None:
        """Release the engine pool during process shutdown."""

        await self.engine.dispose()

    async def verify_ready(self) -> None:
        """Fail startup when PostgreSQL or the frozen Alembic schema is unavailable."""

        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            revisions = tuple(
                (await connection.scalars(text("SELECT version_num FROM alembic_version"))).all()
            )
        if revisions != (EXPECTED_SCHEMA_REVISION,):
            raise RuntimeError("database schema revision is not the application revision")


def create_database(settings: Settings, *, echo: bool = False) -> Database:
    """Build a lazy async engine from ``Settings.database_url``.

    ``create_async_engine`` allocates the pool but performs no network I/O until
    a session actually checks out a connection.
    """

    database_url = settings.database_url
    if not database_url:
        raise ValueError("CAD2BIM_DATABASE_URL is required for PostgreSQL persistence")

    driver = make_url(database_url).drivername
    if driver != "postgresql+psycopg":
        raise ValueError("CAD2BIM_DATABASE_URL must use postgresql+psycopg://")

    engine = create_async_engine(database_url, echo=echo, pool_pre_ping=True)
    sessions = async_sessionmaker(
        engine,
        class_=TenantAwareAsyncSession,
        expire_on_commit=False,
    )
    return Database(engine=engine, session_factory=sessions)


__all__ = ["Database", "EXPECTED_SCHEMA_REVISION", "create_database"]
