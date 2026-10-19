from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from CAD2BIM_backend.infrastructure.langgraph import setup as checkpoint_setup


@pytest.mark.asyncio
async def test_checkpoint_setup_owns_and_closes_the_saver(monkeypatch):
    calls: list[str] = []

    class Saver:
        async def setup(self):
            calls.append("setup")

    @asynccontextmanager
    async def context():
        calls.append("open")
        try:
            yield Saver()
        finally:
            calls.append("close")

    monkeypatch.setattr(
        checkpoint_setup.AsyncPostgresSaver,
        "from_conn_string",
        lambda value: context() if value == "postgresql://db/cad2bim" else None,
    )
    async def grant(_url):
        calls.append("grant")
    monkeypatch.setattr(checkpoint_setup, "_grant_runner_access", grant)

    await checkpoint_setup.setup_checkpoint_schema("postgresql+psycopg://db/cad2bim")

    assert calls == ["open", "setup", "close", "grant"]


@pytest.mark.asyncio
async def test_failed_checkpoint_setup_closes_saver_and_does_not_grant(monkeypatch):
    closed = []
    @asynccontextmanager
    async def context():
        try:
            yield SimpleNamespace(setup=AsyncMock(side_effect=RuntimeError("schema failed")))
        finally:
            closed.append(True)
    monkeypatch.setattr(checkpoint_setup.AsyncPostgresSaver, "from_conn_string", lambda _url: context())
    grant = AsyncMock()
    monkeypatch.setattr(checkpoint_setup, "_grant_runner_access", grant)
    with pytest.raises(RuntimeError, match="schema failed"):
        await checkpoint_setup.setup_checkpoint_schema("postgresql+psycopg://db/cad2bim")
    assert closed == [True]
    grant.assert_not_awaited()


def test_checkpoint_setup_cli_fails_before_connect_without_database(monkeypatch):
    monkeypatch.setattr(
        checkpoint_setup,
        "Settings",
        SimpleNamespace(load=lambda: SimpleNamespace(database_url=None)),
    )

    with pytest.raises(RuntimeError, match="CAD2BIM_DATABASE_URL"):
        checkpoint_setup.cli()
