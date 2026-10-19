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
        command.upgrade(config, "20260903_0069:20260903_0070", sql=True)
    else:
        command.downgrade(config, "20260903_0070:20260903_0069", sql=True)
    return " ".join(output.getvalue().split())


def test_0070_upgrade_accepts_unified_name_without_rewriting_history() -> None:
    sql = _render("upgrade")

    assert "ALTER TABLE critical_operations DROP CONSTRAINT" in sql
    assert "ALTER TABLE critical_operations ADD CONSTRAINT" in sql
    assert "ifc_geometry_resource_write" in sql
    assert "ifc_geometry_resource_create" in sql
    assert "ifc_geometry_resource_repair" in sql
    assert "tool_name = 'ifc_geometry_resource_write'" in sql
    assert "UPDATE CRITICAL_OPERATIONS" not in sql.upper()


def test_0070_downgrade_is_fail_closed_when_unified_rows_exist() -> None:
    sql = _render("downgrade")

    assert "LOCK TABLE critical_operations IN ACCESS EXCLUSIVE MODE" in sql
    assert "WHERE tool_name = 'ifc_geometry_resource_write'" in sql
    assert "unified geometry resource write Operations exist" in sql
    assert "ERRCODE = '23514'" in sql
    assert "ifc_geometry_resource_create" in sql
    assert "ifc_geometry_resource_repair" in sql
    assert "DELETE FROM" not in sql.upper()
    assert "UPDATE CRITICAL_OPERATIONS" not in sql.upper()
