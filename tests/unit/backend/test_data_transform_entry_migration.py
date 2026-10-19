from __future__ import annotations

from io import StringIO
import logging
from pathlib import Path
from importlib import import_module

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint

from CAD2BIM_backend.infrastructure.postgres.models import Base



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
        command.upgrade(config, "20260904_0071:20260904_0072", sql=True)
    else:
        command.downgrade(config, "20260904_0072:20260904_0071", sql=True)
    return " ".join(output.getvalue().split())


def test_0072_upgrade_accepts_new_and_legacy_repair_shapes_without_rewrite() -> None:
    sql = _render("upgrade")

    assert "tool_name = 'data_transform'" in sql
    assert "tool_name = 'data_repair_transform'" in sql
    assert "tool_name = 'cad_compute_measurements'" in sql
    assert "repair_parent_operation_id IS NOT NULL" in sql
    assert "UPDATE CRITICAL_OPERATIONS" not in sql.upper()


def test_0072_downgrade_locks_and_fails_closed_for_new_repair_rows() -> None:
    sql = _render("downgrade")

    assert "LOCK TABLE critical_operations IN ACCESS EXCLUSIVE MODE" in sql
    assert "tool_name = 'data_transform'" in sql
    assert "repair_parent_operation_id IS NOT NULL" in sql
    assert "ERRCODE = '23514'" in sql
    assert "DELETE FROM" not in sql.upper()
    assert "UPDATE CRITICAL_OPERATIONS" not in sql.upper()


def test_migration_keeps_existing_execution_observation_logger(caplog) -> None:
    logger = logging.getLogger("CAD2BIM.execution")
    _render("upgrade")
    # Alembic intentionally configures root handlers; install capture afterwards.
    logging.getLogger().addHandler(caplog.handler)
    with caplog.at_level(logging.INFO, logger=logger.name):
        logger.info("execution observation after migration")
    assert not logger.disabled
    assert "execution observation after migration" in caplog.messages


def test_data_derive_migration_extends_exact_source_without_rewriting_historical_producers():
    migration = import_module("CAD2BIM_backend.infrastructure.postgres.migrations.versions.20261017_0111_data_derive_ifc_source")
    constraints = {item.name: str(item.sqltext)
        for item in Base.metadata.tables["critical_operations"].constraints if isinstance(item, CheckConstraint)}
    normalize = lambda text: " ".join(text.replace("json_type(", "jsonb_typeof(").split())
    for name, expression in ((migration._DETAIL_NAME, migration._DETAIL_CHECK),
            (migration._CONTROL_NAME, migration._CONTROL_CHECK),
            ("ck_critical_operations_ifc_read_source_consistent", migration._SOURCE_CHECK)):
        assert normalize(constraints[name]) == normalize(expression)
    source_fk = next(item for item in Base.metadata.tables["critical_operations"].foreign_key_constraints
        if item.name == "fk_critical_operations_ifc_read_source")
    assert [item.parent.name for item in source_fk.elements] == [
        "owner_id", "task_id", "run_id", "source_ifc_model_id", "source_ifc_revision_id"]
    assert [item.target_fullname for item in source_fk.elements] == [
        f"ifc_model_revisions.{name}" for name in ["owner_id", "task_id", "run_id", "ifc_model_id", "revision_id"]]
    root = Path(__file__).resolve().parents[3]
    config = Config(root / "alembic.ini")
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:secret@example.invalid/cad2bim")
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "20261014_0110:20261017_0111", sql=True)
    sql = output.getvalue()
    assert "source_ifc_model_id" in sql and "source_ifc_revision_id" in sql
    assert "data_derive" in sql and "data_transform" in sql
    assert "UPDATE critical_operations" not in sql
    output = StringIO()
    config.output_buffer = output
    command.downgrade(config, "20261017_0111:20261014_0110", sql=True)
    assert "Cannot discard Data derivation Operations" in output.getvalue()
    assert output.getvalue().index("RAISE EXCEPTION") < output.getvalue().index("DROP COLUMN")
