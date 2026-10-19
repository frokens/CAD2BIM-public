"""Shared configuration for the current Direct, storage and database foundations."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values


def _clean(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable settings shared by current infrastructure adapters.

    Direct MCP builds this object from its own stricter server-only configuration.
    Alembic and the checkpointer setup command use :meth:`load` so they can share
    the same database URL without importing a product entry point.
    """

    environment: str = "development"
    max_upload_bytes: int = 100 * 1024 * 1024
    storage_root: Path = Path("storage/v2")
    env_file: Path | None = field(default=None, repr=False)
    database_url: str | None = field(default=None, repr=False)
    openai_api_key: str | None = field(default=None, repr=False)
    system_publication_subject: str | None = None

    def __post_init__(self) -> None:
        if self.environment not in {"development", "test", "production"}:
            raise ValueError("CAD2BIM_ENV must be development, test, or production")
        if self.max_upload_bytes <= 0:
            raise ValueError("CAD2BIM_MAX_UPLOAD_BYTES must be positive")

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Load one optional dotenv file, then apply environment overrides."""

        source_env = dict(os.environ if environ is None else environ)
        env_file_value = _clean(source_env.get("CAD2BIM_ENV_FILE"))
        env_file = Path(env_file_value).expanduser() if env_file_value else None

        values: dict[str, object] = {}
        if env_file is not None and env_file.is_file():
            values.update(dotenv_values(env_file))
        values.update(source_env)

        def get(name: str, default: str | None = None) -> str | None:
            return _clean(values.get(name)) or default

        max_upload_default = str(100 * 1024 * 1024)
        return cls(
            environment=(get("CAD2BIM_ENV", "development") or "development").lower(),
            max_upload_bytes=int(
                get("CAD2BIM_MAX_UPLOAD_BYTES", max_upload_default)
                or max_upload_default
            ),
            storage_root=Path(
                get("CAD2BIM_STORAGE_ROOT", "storage/v2") or "storage/v2"
            ).expanduser(),
            env_file=env_file,
            database_url=get("CAD2BIM_DATABASE_URL"),
            openai_api_key=get("OPENAI_API_KEY"),
            system_publication_subject=get("CAD2BIM_SYSTEM_PUBLICATION_SUBJECT"),
        )


__all__ = ["Settings"]
