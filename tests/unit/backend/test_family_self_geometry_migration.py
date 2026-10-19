from importlib import import_module
from io import StringIO

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint

from CAD2BIM_backend.infrastructure.postgres.models import Base


_MIGRATION = import_module(
    "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
    "20260909_0082_family_self_geometry_contract"
)


def _render(direction: str) -> str:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:dummy@example.invalid/cad2bim")
    output = StringIO()
    config.output_buffer = output
    if direction == "upgrade":
        command.upgrade(config, "20260909_0081:20260909_0082", sql=True)
    else:
        command.downgrade(config, "20260909_0082:20260909_0081", sql=True)
    return output.getvalue()


def test_0082_metadata_matches_exact_migration_constraints() -> None:
    for table, name, expected in (
        ("product_family_versions", "ck_product_family_versions_contract_shape", _MIGRATION._contract_shape(self_geometry=True)),
        ("critical_operations", "ck_critical_operations_operation_kind_detail_valid", _MIGRATION._operation_detail_check(self_geometry=True)),
        ("geometry_query_attempts", "ck_geometry_query_attempts_target_shape_valid", _MIGRATION._query_target_shape(self_geometry=True)),
    ):
        actual = next(str(c.sqltext) for c in Base.metadata.tables[table].constraints
                      if isinstance(c, CheckConstraint) and c.name == name)
        assert " ".join(actual.replace("json_type(", "jsonb_typeof(").split()) == " ".join(expected.split())


def test_0082_extends_0081_without_rewriting_historical_family_facts() -> None:
    sql = _render("upgrade")
    for retained in ("ifc_products_apply", "product_family_write", "geometry_interface_annotation", "cad_compute_measurements"):
        assert retained in sql
    assert "'product_family_contract_v1', 'product_family_contract_v2'" in sql
    assert "'product_realization_v1' OR" in sql
    assert "'product_family_contract_v2') IS TRUE" in sql
    for forbidden in ("CREATE TABLE", "CREATE POLICY", "DELETE FROM", "UPDATE product_family_versions", "UPDATE critical_operations", "GRANT ", "ck_critical_operations_ck_"):
        assert forbidden not in sql
    assert _MIGRATION.down_revision == "20260909_0081"


def test_0082_geometry_preview_guard_keeps_independent_artifact_identity() -> None:
    sql = _render("upgrade")
    assert "target_kind IN ('product_realization_preview', 'geometry_preview')" in sql
    for required in ("geometry_preview_sample_geometry", "geometry_resource_preview", "resource_content_digest", "derived_only", "target_version.tenant_id = NEW.tenant_id", "target_version.task_id = NEW.task_id", "target_artifact.run_id IS NOT DISTINCT FROM NEW.origin_run_id", "geometry query repair parent is outside the exact Task target chain"):
        assert required in sql


def test_0082_downgrade_rejects_new_immutable_facts_before_restoring_0081() -> None:
    sql = _render("downgrade")
    assert sql.index("LOCK TABLE critical_operations, product_family_versions, geometry_query_attempts") < sql.index("DROP CONSTRAINT")
    assert "ERRCODE = '23514'" in sql
    assert "WHERE target_kind = 'geometry_preview'" in sql
    assert "Cannot downgrade while immutable self-geometry Family versions, Operations or Geometry preview queries exist" in sql
    assert "DELETE FROM" not in sql
    previous = import_module("CAD2BIM_backend.infrastructure.postgres.migrations.versions.20260909_0081_ifc_products_apply")
    assert _MIGRATION._operation_detail_check(self_geometry=False) == previous._detail_check(products=True)
