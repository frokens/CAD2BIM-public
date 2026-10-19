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
        command.upgrade(config, "20260905_0076:20260905_0077", sql=True)
    else:
        command.downgrade(config, "20260905_0077:20260905_0076", sql=True)
    return " ".join(output.getvalue().split())


def _render_usage(direction: str) -> str:
    root = Path(__file__).resolve().parents[3]
    config = Config(root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = StringIO()
    config.output_buffer = output
    if direction == "upgrade":
        command.upgrade(config, "20260910_0084:20260913_0085", sql=True)
    else:
        command.downgrade(config, "20260913_0085:20260910_0084", sql=True)
    return " ".join(output.getvalue().split())


def test_0077_upgrade_allows_source_less_annotation_and_keeps_exact_base_fences() -> None:
    sql = _render("upgrade")

    assert "geometry_interface_annotation" in sql
    assert "code_artifact_id IS NULL" in sql
    assert "detail_kind' IS DISTINCT FROM 'geometry_interface_annotation'" in sql
    assert "geometry interface annotation cannot contain source or repair bindings" in sql
    assert "resource.resource_kind = 'mapped_model'" in sql
    assert "geometry interface annotation exact mapped base is invalid" in sql
    assert "same-root annotation base is not writable in this Task" in sql
    assert "derived annotation target root already exists" in sql
    assert "derived annotation base is not visible in this Task" in sql
    assert "geometry interface annotation result metadata is invalid" in sql
    assert "geometry interface annotation changed immutable base assets" in sql
    assert "CREATE TABLE" not in sql.upper()
    assert "CREATE POLICY" not in sql.upper()


def test_0077_downgrade_fails_closed_when_annotation_operations_exist() -> None:
    sql = _render("downgrade")

    assert "LOCK TABLE critical_operations IN ACCESS EXCLUSIVE MODE" in sql
    assert "geometry_interface_annotation" in sql
    assert "geometry interface annotation Operations exist" in sql
    assert "ERRCODE = '23514'" in sql
    assert "DELETE FROM" not in sql.upper()
    assert "UPDATE CRITICAL_OPERATIONS" not in sql.upper()


def test_0085_upgrade_admits_bounded_usage_only_annotations_without_changing_assets() -> None:
    sql = _render_usage("upgrade")

    assert "ck_critical_operations_geometry_annotation_usage_current" in sql
    assert "NOT VALID" in sql
    assert "jsonb_typeof(operation_detail_json -> 'usage_instructions') = 'string'" in sql
    assert "result_version.manifest_json -> 'usage_instructions'" in sql
    assert "NEW.operation_detail_json -> 'usage_instructions'" in sql
    assert "((result_version.manifest_json - 'interfaces') - 'usage_instructions')" in sql
    assert "geometry interface annotation changed immutable base assets" not in sql
    assert "UPDATE GEOMETRY_RESOURCE_VERSIONS" not in sql.upper()
    assert "DELETE FROM" not in sql.upper()


def test_0085_downgrade_restores_the_pre_usage_annotation_guard() -> None:
    sql = _render_usage("downgrade")

    assert "Expected Geometry annotation result guard was not found" in sql
    assert "(result_version.manifest_json - 'interfaces')" in sql
    assert "DROP CONSTRAINT ck_critical_operations_geometry_annotation_usage_current" in sql
    assert "UPDATE GEOMETRY_RESOURCE_VERSIONS" not in sql.upper()
    assert "DELETE FROM" not in sql.upper()
