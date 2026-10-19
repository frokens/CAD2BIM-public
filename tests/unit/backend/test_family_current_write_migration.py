from io import StringIO

from alembic import command
from alembic.config import Config


def _render(direction: str) -> str:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:dummy@example.invalid/cad2bim")
    output = StringIO()
    config.output_buffer = output
    if direction == "upgrade":
        command.upgrade(config, "20260909_0082:20260909_0083", sql=True)
    else:
        command.downgrade(config, "20260909_0083:20260909_0082", sql=True)
    return output.getvalue()


def test_current_write_guard_preserves_historical_storage_and_other_owners() -> None:
    sql = _render("upgrade")
    assert "BEFORE INSERT ON product_family_versions" in sql
    assert "BEFORE INSERT ON critical_operations" in sql
    assert "NEW.operation_kind = 'product_family_write'" in sql
    assert sql.count("IS DISTINCT FROM 'product_family_contract_v2'") == 2
    assert sql.count("IS DISTINCT FROM 'product_realization_v2'") == 2
    assert sql.count("ERRCODE = '23514'") == 2
    for forbidden in ("CREATE TABLE", "CREATE POLICY", "DROP CONSTRAINT", "UPDATE product_family_versions", "UPDATE critical_operations", "DELETE FROM", "GRANT "):
        assert forbidden not in sql


def test_current_write_guard_downgrade_only_removes_its_own_admission_guards() -> None:
    sql = _render("downgrade")
    assert sql.count("DROP TRIGGER") == 2
    assert sql.count("DROP FUNCTION") == 2
    assert "product_family_current_version_guard" in sql
    assert "product_family_current_operation_guard" in sql
    assert "DELETE FROM" not in sql
    assert "DROP CONSTRAINT" not in sql
