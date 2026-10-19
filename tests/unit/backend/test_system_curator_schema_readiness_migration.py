from __future__ import annotations

from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config



def _render(direction: str) -> str:
    root = Path(__file__).resolve().parents[3]
    config = Config(root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = StringIO()
    config.output_buffer = output
    if direction == "upgrade":
        command.upgrade(config, "20260905_0077:20260906_0078", sql=True)
    else:
        command.downgrade(config, "20260906_0078:20260905_0077", sql=True)
    return " ".join(output.getvalue().split())


def test_0078_grants_only_schema_revision_readiness_to_system_curator() -> None:
    sql = _render("upgrade")

    assert sql.upper().count("GRANT ") == 1
    assert "GRANT SELECT ON TABLE alembic_version TO cad2bim_system_curator" in sql
    assert "GRANT INSERT" not in sql.upper()
    assert "GRANT UPDATE" not in sql.upper()
    assert "GRANT DELETE" not in sql.upper()
    assert "GRANT ALL" not in sql.upper()
    assert "CREATE ROLE" not in sql.upper()
    assert "ALTER ROLE" not in sql.upper()


def test_0078_downgrade_revokes_only_schema_revision_readiness() -> None:
    sql = _render("downgrade")

    assert sql.upper().count("REVOKE ") == 1
    assert "REVOKE SELECT ON TABLE alembic_version FROM cad2bim_system_curator" in sql
    assert "REVOKE INSERT" not in sql.upper()
    assert "REVOKE UPDATE" not in sql.upper()
    assert "REVOKE DELETE" not in sql.upper()
    assert "REVOKE ALL" not in sql.upper()
    assert "DROP ROLE" not in sql.upper()
    assert "ALTER ROLE" not in sql.upper()
