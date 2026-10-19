from __future__ import annotations

from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from CAD2BIM_backend.infrastructure.postgres.models import Base


TABLE = "geometry_query_attempts"


def _render_upgrade() -> str:
    root = Path(__file__).resolve().parents[3]
    config = Config(root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "20260902_0066:20260902_0067", sql=True)
    return " ".join(output.getvalue().split())


def _render_downgrade() -> str:
    root = Path(__file__).resolve().parents[3]
    config = Config(root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = StringIO()
    config.output_buffer = output
    command.downgrade(config, "20260902_0067:20260902_0066", sql=True)
    return " ".join(output.getvalue().split())


def _render_target_upgrade() -> str:
    root = Path(__file__).resolve().parents[3]
    config = Config(root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "20260904_0074:20260905_0075", sql=True)
    return " ".join(output.getvalue().split())


def _render_target_downgrade() -> str:
    root = Path(__file__).resolve().parents[3]
    config = Config(root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = StringIO()
    config.output_buffer = output
    command.downgrade(config, "20260905_0075:20260904_0074", sql=True)
    return " ".join(output.getvalue().split())


def _checks() -> str:
    return " ".join(
        str(constraint.sqltext)
        for constraint in Base.metadata.tables[TABLE].constraints
        if isinstance(constraint, CheckConstraint)
    )


def _foreign_keys() -> set[tuple[tuple[str, ...], tuple[str, ...], str | None]]:
    return {
        (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
            constraint.ondelete,
        )
        for constraint in Base.metadata.tables[TABLE].constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }


def test_geometry_query_attempt_metadata_has_exact_append_only_shape() -> None:
    table = Base.metadata.tables[TABLE]
    assert set(table.c.keys()) == {
        "attempt_id",
        "tenant_id",
        "task_id",
        "origin_run_id",
        "target_kind",
        "resource_version_id",
        "family_version_id",
        "target_artifact_version_id",
        "target_geometry_format",
        "inspection_selection_json",
        "inspection_coordinate_space_json",
        "source_artifact_version_id",
        "result_artifact_version_id",
        "repair_parent_attempt_id",
        "request_identity",
        "request_digest",
        "status",
        "diagnostics_json",
        "created_at",
        "finished_at",
    }
    assert (
        ("target_artifact_version_id",),
        ("artifact_versions.version_id",),
        "RESTRICT",
    ) in _foreign_keys()
    assert (
        ("resource_version_id",),
        ("geometry_resource_versions.resource_version_id",),
        "RESTRICT",
    ) in _foreign_keys()
    assert (
        ("family_version_id",),
        ("product_family_versions.family_version_id",),
        "RESTRICT",
    ) in _foreign_keys()
    assert (
        ("source_artifact_version_id",),
        ("artifact_versions.version_id",),
        "RESTRICT",
    ) in _foreign_keys()
    assert (
        ("result_artifact_version_id",),
        ("artifact_versions.version_id",),
        "RESTRICT",
    ) in _foreign_keys()
    assert (
        ("tenant_id", "task_id", "repair_parent_attempt_id"),
        (
            "geometry_query_attempts.tenant_id",
            "geometry_query_attempts.task_id",
            "geometry_query_attempts.attempt_id",
        ),
        "RESTRICT",
    ) in _foreign_keys()
    uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("tenant_id", "task_id", "request_identity") in uniques
    checks = _checks()
    assert "status IN ('PREPARED', 'SUCCEEDED', 'FAILED')" in checks
    assert "result_artifact_version_id IS NOT NULL" in checks
    assert "json_array_length(diagnostics_json) <= 32" in checks
    assert "request_digest = lower(request_digest)" in checks
    assert "target_kind IN ('product_realization_preview', 'geometry_preview')" in checks
    assert "target_geometry_format = 'occt_brep_v1'" in checks


def test_0067_uses_task_rls_column_limited_terminal_update_and_guard_trigger() -> None:
    sql = _render_upgrade()
    assert "CREATE TABLE geometry_query_attempts" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FOR SELECT TO cad2bim_api_runtime, cad2bim_runner_runtime, cad2bim_mcp_runtime" in sql
    assert "FOR INSERT TO cad2bim_runner_runtime, cad2bim_mcp_runtime" in sql
    assert "FOR UPDATE TO cad2bim_runner_runtime, cad2bim_mcp_runtime" in sql
    assert "current_setting('cad2bim.tenant_id', true)" in sql
    assert "current_setting('cad2bim.task_id', true)" in sql
    assert "GRANT UPDATE (status, result_artifact_version_id, diagnostics_json, finished_at)" in sql
    assert "GRANT DELETE" not in sql
    assert "GRANT UPDATE ON TABLE geometry_query_attempts" not in sql
    assert "geometry query attempt identity is append-only" in sql
    assert "OLD.status <> 'PREPARED'" in sql
    assert "octet_length(NEW.diagnostics_json::text) > 16384" in sql
    assert "source_version.tenant_id = NEW.tenant_id" in sql
    assert "parent.resource_version_id = NEW.resource_version_id" in sql


def test_0067_downgrade_refuses_to_destroy_attempt_history() -> None:
    sql = _render_downgrade()
    assert "IN ACCESS EXCLUSIVE MODE" in sql
    assert "geometry query attempt log is not empty" in sql
    assert "DELETE FROM" not in sql.upper()
    assert sql.index("DROP TRIGGER trg_geometry_query_attempt_guard") < sql.index(
        "DROP TABLE geometry_query_attempts"
    )


def test_0075_generalizes_the_existing_attempt_table_without_parallel_persistence() -> None:
    sql = _render_target_upgrade()
    assert "ADD COLUMN target_kind" in sql
    assert "ADD COLUMN target_artifact_version_id" in sql
    assert "ADD COLUMN target_geometry_format" in sql
    assert "ALTER COLUMN resource_version_id DROP NOT NULL" in sql
    assert "product_realization_preview_geometry" in sql
    assert "application/vnd.opencascade.brep" in sql
    assert "family_version_content_digest" in sql
    assert "family_version_content_digest' ~ '^[0-9a-f]{64}$'" in sql
    assert "solve_digest' ~ '^[0-9a-f]{64}$'" in sql
    assert "parent.target_artifact_version_id IS NOT DISTINCT FROM" in sql
    assert "CREATE TABLE" not in sql
    assert "CREATE OR REPLACE FUNCTION cad2bim_guard_geometry_query_attempt" in sql


def test_0075_downgrade_fails_closed_when_preview_history_exists() -> None:
    sql = _render_target_downgrade()
    assert "IN ACCESS EXCLUSIVE MODE" in sql
    assert "product realization preview query history exists" in sql
    assert "DELETE FROM" not in sql.upper()
    assert sql.index("CREATE OR REPLACE FUNCTION cad2bim_guard_geometry_query_attempt") < sql.index(
        "DROP COLUMN target_kind"
    )


def test_0098_retains_history_and_binds_immutable_inspection_choices() -> None:
    root = Path(__file__).resolve().parents[3]
    config = Config(root / "alembic.ini")
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:secret@example.invalid/cad2bim")
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "20260929_0097:20260929_0098", sql=True)
    sql = " ".join(output.getvalue().split())
    assert "ADD COLUMN inspection_selection_json JSONB" in sql
    assert "ADD COLUMN inspection_coordinate_space_json JSONB" in sql
    assert "NEW.inspection_selection_json, NEW.inspection_coordinate_space_json" in sql
    assert "parent.inspection_selection_json IS NOT DISTINCT FROM NEW.inspection_selection_json" in sql
    assert "parent.inspection_coordinate_space_json IS NOT DISTINCT FROM NEW.inspection_coordinate_space_json" in sql
    assert "application/vnd.cad2bim.geometry-inspection+zip" in sql
    assert "new geometry queries cannot use retired preview targets" in sql
    assert "CREATE TABLE" not in sql and "DELETE FROM" not in sql.upper()
    output = StringIO()
    config.output_buffer = output
    command.downgrade(config, "20260929_0098:20260929_0097", sql=True)
    sql = " ".join(output.getvalue().split())
    assert "Cannot remove retained geometry inspection query history" in sql
    assert sql.index("Cannot remove retained geometry inspection query history") < sql.index("DROP COLUMN inspection_selection_json")
