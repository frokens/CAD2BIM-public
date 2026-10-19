from __future__ import annotations

from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from CAD2BIM_backend.infrastructure.postgres.models import Base


ATTEMPTS = "ifc_query_attempts"
INPUTS = "ifc_query_inputs"


def _render_upgrade() -> str:
    root = Path(__file__).resolve().parents[3]
    config = Config(root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "20260902_0068:20260903_0069", sql=True)
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
    command.downgrade(config, "20260903_0069:20260902_0068", sql=True)
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


def test_ifc_query_metadata_binds_exact_revision_artifacts_and_members() -> None:
    attempts = Base.metadata.tables[ATTEMPTS]
    inputs = Base.metadata.tables[INPUTS]
    assert set(attempts.c.keys()) == {
        "attempt_id",
        "owner_id",
        "tenant_id",
        "task_id",
        "run_id",
        "ifc_model_id",
        "ifc_revision_id",
        "source_artifact_id",
        "source_artifact_version_id",
        "result_artifact_id",
        "result_artifact_version_id",
        "repair_parent_attempt_id",
        "request_identity",
        "request_digest",
        "input_count",
        "budget_json",
        "status",
        "diagnostics_json",
        "created_at",
        "finished_at",
    }
    assert "cursor" not in " ".join(attempts.c.keys())
    assert set(inputs.c.keys()) == {
        "attempt_id",
        "owner_id",
        "tenant_id",
        "task_id",
        "run_id",
        "ordinal",
        "input_name",
        "package_id",
        "payload_member_id",
        "schema_member_id",
        "created_at",
    }
    assert (
        ("owner_id", "task_id", "run_id", "ifc_model_id", "ifc_revision_id"),
        (
            "ifc_model_revisions.owner_id",
            "ifc_model_revisions.task_id",
            "ifc_model_revisions.run_id",
            "ifc_model_revisions.ifc_model_id",
            "ifc_model_revisions.revision_id",
        ),
        "RESTRICT",
    ) in _foreign_keys(ATTEMPTS)
    for prefix in ("source", "result"):
        assert (
            ("owner_id", "task_id", "run_id", f"{prefix}_artifact_id"),
            (
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ),
            "RESTRICT",
        ) in _foreign_keys(ATTEMPTS)
        assert (
            ("owner_id", "task_id", f"{prefix}_artifact_id", f"{prefix}_artifact_version_id"),
            (
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ),
            "RESTRICT",
        ) in _foreign_keys(ATTEMPTS)
    for member_column in ("payload_member_id", "schema_member_id"):
        assert (
            ("owner_id", "task_id", "package_id", member_column),
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
    checks = " ".join(
        str(constraint.sqltext)
        for constraint in attempts.constraints
        if isinstance(constraint, CheckConstraint)
    )
    assert "status IN ('PREPARED', 'SUCCEEDED', 'FAILED')" in checks
    assert "result_artifact_version_id IS NOT NULL" in checks
    input_checks = " ".join(
        str(constraint.sqltext)
        for constraint in inputs.constraints
        if isinstance(constraint, CheckConstraint)
    )
    assert "length(input_name) BETWEEN 1 AND 63" in input_checks
    assert "substr(input_name, 1, 1) BETWEEN 'a' AND 'z'" in input_checks
    assert "replace(" in input_checks


def test_0069_offline_sql_enforces_deferred_exact_repair_rls_and_artifact_policy() -> None:
    sql = _render_upgrade()
    assert "CREATE TABLE ifc_query_attempts" in sql
    assert "CREATE TABLE ifc_query_inputs" in sql
    assert "DEFERRABLE INITIALLY DEFERRED" in sql
    assert "bound_count <> NEW.input_count" in sql
    assert "bound_count <> attempt.input_count" in sql
    assert "CREATE CONSTRAINT TRIGGER trg_ifc_query_input_parent" in sql
    assert "ifc query repair must preserve the exact input set" in sql
    assert "parent.ifc_revision_id = NEW.ifc_revision_id" in sql
    assert "payload.member_kind = 'payload'" in sql
    assert "schema_member.member_kind = 'schema'" in sql
    assert "schema_member.package_id = NEW.package_id" in sql
    assert "schemas/" in sql and ".schema.json" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "current_setting('cad2bim.task_id', true)" in sql
    assert "current_setting('cad2bim.run_id', true)" in sql
    assert (
        "GRANT UPDATE (status, result_artifact_id, result_artifact_version_id, diagnostics_json, finished_at)"
        in sql
    )
    assert "GRANT DELETE" not in sql
    assert "ifc_query_source" in sql and "ifc_query_result" in sql
    assert "CREATE POLICY ifc_query_artifact_run_isolation ON artifacts" in sql
    assert "CREATE POLICY ifc_query_artifact_version_run_isolation ON artifact_versions" in sql
    assert "CREATE TRIGGER trg_ifc_query_artifact_guard" in sql
    assert "CREATE TRIGGER trg_ifc_query_artifact_version_guard" in sql
    assert "API runtime cannot publish IFC query Artifacts" in sql
    assert "IFC query ArtifactVersions are immutable" in sql
    assert "artifact.run_id = NEW.run_id" in sql
    assert "entry.run_id = NEW.run_id" in sql
    assert "cursor" not in sql.lower()


def test_0069_downgrade_refuses_to_destroy_query_history() -> None:
    sql = _render_downgrade()
    assert "IN ACCESS EXCLUSIVE MODE" in sql
    assert "IFC query history is not empty" in sql
    assert "DELETE FROM" not in sql.upper()
    assert sql.index("DROP POLICY ifc_query_artifact_version_run_isolation") < sql.index(
        "DROP TABLE ifc_query_attempts"
    )
