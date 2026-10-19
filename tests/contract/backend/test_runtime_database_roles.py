from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPOSITORY_ROOT / "deploy/compose.foundation.yaml"
PYPROJECT_PATH = REPOSITORY_ROOT / "pyproject.toml"


def test_compose_provisions_runtime_logins_without_starting_old_services() -> None:
    source = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "cad2bim-db-runtime-roles" in source
    assert source.count("postgresql+psycopg://cad2bim:") == 1
    assert "CAD2BIM_API_POSTGRES_PASSWORD:?" in source
    assert "CAD2BIM_RUNNER_POSTGRES_PASSWORD:?" in source
    assert "CAD2BIM_MCP_POSTGRES_PASSWORD:?" in source
    assert "cad2bim_api_login" not in source
    assert "cad2bim_runner_login" not in source
    assert "cad2bim_mcp_login" not in source
    assert "workspace-exec" not in source


def test_runtime_role_provisioner_is_a_packaged_migration_command() -> None:
    source = PYPROJECT_PATH.read_text(encoding="utf-8")

    assert (
        'cad2bim-db-runtime-roles = '
        '"CAD2BIM_backend.infrastructure.postgres.runtime_roles:cli"'
    ) in source
