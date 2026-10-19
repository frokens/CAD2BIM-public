from __future__ import annotations

import pytest
from contextlib import contextmanager
from CAD2BIM_backend.infrastructure.postgres import runtime_roles

from CAD2BIM_backend.infrastructure.postgres.runtime_roles import (
    _psycopg_database_url,
    _runtime_passwords,
)


def test_runtime_database_url_is_converted_without_losing_credentials() -> None:
    assert _psycopg_database_url(
        "postgresql+psycopg://migration:secret@postgres:5432/cad2bim"
    ) == "postgresql://migration:secret@postgres:5432/cad2bim"


def test_runtime_role_passwords_are_required_and_distinct() -> None:
    with pytest.raises(RuntimeError, match="CAD2BIM_API_POSTGRES_PASSWORD"):
        _runtime_passwords({})
    with pytest.raises(RuntimeError, match="must be distinct"):
        _runtime_passwords(
            {
                "CAD2BIM_API_POSTGRES_PASSWORD": "same-password-value",
                "CAD2BIM_RUNNER_POSTGRES_PASSWORD": "same-password-value",
                "CAD2BIM_MCP_POSTGRES_PASSWORD": "mcp-password-value",
            }
        )
    with pytest.raises(RuntimeError, match="URL-safe ASCII"):
        _runtime_passwords(
            {
                "CAD2BIM_API_POSTGRES_PASSWORD": "api/password/value",
                "CAD2BIM_RUNNER_POSTGRES_PASSWORD": "runner-password-value",
                "CAD2BIM_MCP_POSTGRES_PASSWORD": "mcp-password-value",
            }
        )
    assert _runtime_passwords(
        {
            "CAD2BIM_API_POSTGRES_PASSWORD": "api-password-value",
            "CAD2BIM_RUNNER_POSTGRES_PASSWORD": "runner-password-value",
            "CAD2BIM_MCP_POSTGRES_PASSWORD": "mcp-password-value",
        }
    ) == {
        "cad2bim_api_login": "api-password-value",
        "cad2bim_runner_login": "runner-password-value",
        "cad2bim_mcp_login": "mcp-password-value",
    }


def test_selected_login_is_independent_of_other_passwords_and_checkpoint_schema(monkeypatch):
    statements = []

    class Cursor:
        def execute(self, query, params=None):
            statements.append(query if isinstance(query, str) else query.as_string())

        def fetchone(self):
            return (False,)

    @contextmanager
    def cursor():
        yield Cursor()

    @contextmanager
    def connect(_url):
        yield type("Connection", (), {"cursor": staticmethod(cursor)})()

    monkeypatch.setattr(runtime_roles.psycopg, "connect", connect)
    runtime_roles.provision_runtime_login_roles(
        "postgresql+psycopg://admin/db",
        environ={"CAD2BIM_SYSTEM_CURATOR_POSTGRES_PASSWORD": "curator-only-password"},
        role_names=("system-curator",),
    )
    queries = "\n".join(statements)
    assert 'CREATE ROLE "cad2bim_system_curator_login"' in queries
    assert 'GRANT "cad2bim_system_curator" TO "cad2bim_system_curator_login"' in queries
    for unrelated in ("checkpoint", "cad2bim_runner", "cad2bim_api", "cad2bim_mcp"):
        assert unrelated not in queries


@pytest.mark.parametrize("roles", [(), ("mcp", "mcp"), ("arbitrary-superuser",)])
def test_invalid_role_selection_fails_before_connect(roles, monkeypatch):
    def forbidden(_url):
        pytest.fail("invalid role selection reached the database")
    monkeypatch.setattr(runtime_roles.psycopg, "connect", forbidden)
    with pytest.raises(ValueError):
        runtime_roles.provision_runtime_login_roles("postgresql+psycopg://admin/db", environ={}, role_names=roles)
