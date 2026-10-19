from __future__ import annotations

from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from CAD2BIM_backend.infrastructure.postgres.models import Base


ATTEMPTS = "data_query_attempts"
INPUTS = "data_query_inputs"


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
        command.upgrade(config, "20260903_0070:20260904_0071", sql=True)
    else:
        command.downgrade(config, "20260904_0071:20260903_0070", sql=True)
    return " ".join(output.getvalue().split())


def _foreign_keys(table_name: str) -> set[tuple[tuple[str, ...], tuple[str, ...], str | None]]:
    return {
        (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
            constraint.ondelete,
        )
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }


def _checks(table_name: str) -> str:
    return " ".join(
        str(constraint.sqltext)
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, CheckConstraint)
    )


def test_0071_metadata_owns_only_attempt_and_exact_input_tables() -> None:
    attempts = Base.metadata.tables[ATTEMPTS]
    inputs = Base.metadata.tables[INPUTS]
    assert set(attempts.c.keys()) == {
        "attempt_id",
        "sequence",
        "owner_id",
        "tenant_id",
        "task_id",
        "run_id",
        "engine",
        "source_artifact_id",
        "source_artifact_version_id",
        "repair_parent_attempt_id",
        "request_identity",
        "request_digest",
        "input_count",
        "parameters_json",
        "inline_tables_json",
        "page_size",
        "status",
        "diagnostic_json",
        "result_summary_json",
        "created_at",
        "finished_at",
    }
    assert set(inputs.c.keys()) == {
        "attempt_id",
        "ordinal",
        "owner_id",
        "tenant_id",
        "task_id",
        "run_id",
        "input_name",
        "package_id",
        "payload_member_id",
        "schema_member_id",
        "created_at",
    }
    assert (
        ("owner_id", "task_id", "source_artifact_id", "source_artifact_version_id"),
        (
            "artifact_versions.owner_id",
            "artifact_versions.task_id",
            "artifact_versions.artifact_id",
            "artifact_versions.version_id",
        ),
        "RESTRICT",
    ) in _foreign_keys(ATTEMPTS)
    assert (
        ("owner_id", "task_id", "package_id", "payload_member_id"),
        (
            "data_package_members.owner_id",
            "data_package_members.task_id",
            "data_package_members.package_id",
            "data_package_members.member_id",
        ),
        "RESTRICT",
    ) in _foreign_keys(INPUTS)
    uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in attempts.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("owner_id", "task_id", "run_id", "request_identity") in uniques
    assert ("owner_id", "task_id", "run_id", "sequence") in uniques
    checks = _checks(ATTEMPTS)
    assert "engine IN ('duckdb_sql', 'python_arrow')" in checks
    assert "status IN ('PREPARED', 'SUCCEEDED', 'FAILED')" in checks
    assert "result_summary_json IS NOT NULL" in checks
    assert "diagnostic_json IS NOT NULL" in checks


def test_0071_upgrade_is_run_scoped_rls_append_only_and_exact() -> None:
    sql = _render("upgrade")
    for table in (ATTEMPTS, INPUTS):
        assert f"CREATE TABLE {table}" in sql
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
    for setting in ("cad2bim.tenant_id", "cad2bim.task_id", "cad2bim.run_id"):
        assert f"current_setting('{setting}', true)" in sql
    assert "TO cad2bim_api_runtime, cad2bim_runner_runtime, cad2bim_mcp_runtime" in sql
    assert "FOR INSERT TO cad2bim_runner_runtime, cad2bim_mcp_runtime" in sql
    assert (
        "GRANT UPDATE (status, diagnostic_json, result_summary_json, finished_at) "
        "ON TABLE data_query_attempts"
    ) in sql
    assert "GRANT DELETE" not in sql
    assert "Data Query attempt identity is append-only" in sql
    assert "Data Query attempt terminal state is immutable" in sql
    assert "Data Query exact inputs are immutable" in sql
    assert "Data Query repair must preserve the exact input set" in sql
    assert "artifact.kind = 'data_query_source'" in sql
    assert "version.version_no = 1" in sql


def test_0071_source_guard_blocks_only_data_query_source_artifact_deletes() -> None:
    sql = _render("upgrade")
    artifact_guard = sql[
        sql.index("CREATE FUNCTION cad2bim_guard_data_query_source_artifact()") : sql.index(
            "CREATE TRIGGER trg_data_query_source_artifact_guard"
        )
    ]
    assert "IF TG_OP = 'DELETE'" in artifact_guard
    assert "IF OLD.kind = 'data_query_source'" in artifact_guard
    assert "Data Query source Artifacts are immutable" in artifact_guard
    assert artifact_guard.index("END IF; RETURN OLD") > artifact_guard.index(
        "IF OLD.kind = 'data_query_source'"
    )
    assert (
        "IF OLD.kind <> 'data_query_source' AND NEW.kind <> 'data_query_source'" in artifact_guard
    )
    assert "RETURN NEW" in artifact_guard


def test_0071_downgrade_is_empty_only_and_never_rewrites_history() -> None:
    sql = _render("downgrade")
    assert (
        "LOCK TABLE data_query_inputs, data_query_attempts, artifacts, artifact_versions "
        "IN ACCESS EXCLUSIVE MODE"
    ) in sql
    assert "WHERE kind = 'data_query_source'" in sql
    assert "Data Query history is not empty" in sql
    assert "ERRCODE = '23514'" in sql
    assert "DELETE FROM" not in sql.upper()
    assert "UPDATE DATA_QUERY" not in sql.upper()
    assert sql.index("DROP TRIGGER trg_data_query_input_guard") < sql.index(
        "DROP TABLE data_query_inputs"
    )
