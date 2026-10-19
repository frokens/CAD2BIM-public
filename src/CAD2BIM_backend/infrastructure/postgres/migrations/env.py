"""Alembic environment backed by the process Settings contract."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.infrastructure.postgres.models import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
_LANGGRAPH_CHECKPOINT_TABLES = {
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
}


def _include_object(object_, name, type_, reflected, compare_to) -> bool:
    """Leave AsyncPostgresSaver-owned tables to its own migration runner."""

    return not (
        type_ == "table"
        and reflected
        and compare_to is None
        and name in _LANGGRAPH_CHECKPOINT_TABLES
    )


def _database_url() -> str:
    """Prefer an explicit Alembic override, otherwise use Settings."""

    configured = config.get_main_option("sqlalchemy.url").strip()
    if configured:
        return configured
    settings = Settings.load()
    if not settings.database_url:
        raise RuntimeError(
            "CAD2BIM_DATABASE_URL is required (or set sqlalchemy.url in Alembic Config)"
        )
    return settings.database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
