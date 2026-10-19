from io import StringIO
from importlib import import_module
from unittest.mock import Mock

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint

from CAD2BIM_backend.infrastructure.postgres.models import Base


def test_product_operation_migration_only_extends_the_existing_write_contract():
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:dummy@example.invalid/cad2bim")
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "20260909_0080:20260909_0081", sql=True)
    upgrade = output.getvalue()
    assert "'ifc_spatial_structure_apply', 'ifc_products_apply'" in upgrade
    for retained in ("geometry_interface_annotation", "product_family_write", "cad_compute_measurements"):
        assert retained in upgrade
    assert "CREATE TABLE" not in upgrade and "CREATE POLICY" not in upgrade
    assert "ck_critical_operations_ck_" not in upgrade
    output.seek(0)
    output.truncate()
    command.downgrade(config, "20260909_0081:20260909_0080", sql=True)
    assert "product apply Operations exist" in output.getvalue()
    assert "DELETE FROM" not in output.getvalue()


def test_retirement_check_preserves_predecessor_and_matches_current_metadata(monkeypatch):
    prefix = "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
    previous = import_module(prefix + "20260920_0086_family_numeric_realization")
    retired = import_module(prefix + "20260920_0088_retire_ifc_products_apply")
    captured = {}
    operations = Mock()
    operations.f.side_effect = lambda name: name
    operations.create_check_constraint.side_effect = (
        lambda name, table, check: captured.__setitem__(name, check)
    )
    monkeypatch.setattr(previous, "op", operations)
    previous._historical_checks(numeric=True)
    name = "ck_critical_operations_operation_kind_detail_valid"
    assert retired._operation_detail_check(products=True) == captured[name]
    retirement_check = retired._operation_detail_check(products=False)
    assert retirement_check == captured[name].replace(", 'ifc_products_apply'", "", 1)
    # 0088's retirement expression is historical. The later Apply/report
    # migration owns the complete constraint installed at current head.
    current = import_module(prefix + "20261013_0108_atomic_product_apply_reports")
    actual = next(
        str(check.sqltext)
        for check in Base.metadata.tables["critical_operations"].constraints
        if isinstance(check, CheckConstraint) and check.name == name
    )
    normalize = lambda value: " ".join(value.replace("json_type(", "jsonb_typeof(").split())
    assert normalize(current._CHECKS[name]) == normalize(actual)


def test_retirement_migration_blocks_old_rows_before_replacing_validated_check():
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:dummy@example.invalid/cad2bim")
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "20260920_0087:20260920_0088", sql=True)
    sql = output.getvalue()
    assert sql.index("LOCK TABLE critical_operations IN ACCESS EXCLUSIVE MODE") < sql.index("IF EXISTS")
    assert sql.index("USING ERRCODE = '23514'") < sql.index("DROP CONSTRAINT") < sql.index("ADD CONSTRAINT")
    assert "WHERE tool_name = 'ifc_products_apply')" in sql
    for forbidden in ("DELETE FROM", "UPDATE critical_operations", "CASCADE", "NOT VALID", "CREATE TABLE", "CREATE POLICY"):
        assert forbidden not in sql
    output.seek(0)
    output.truncate()
    command.downgrade(config, "20260920_0088:20260920_0087", sql=True)
    downgrade = output.getvalue()
    assert "'ifc_spatial_structure_apply', 'ifc_products_apply'" in downgrade
    assert "DELETE FROM" not in downgrade
