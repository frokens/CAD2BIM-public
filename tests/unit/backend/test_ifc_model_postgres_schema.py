from __future__ import annotations

from io import StringIO
from importlib import import_module
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from CAD2BIM_backend.infrastructure.postgres.models import Base


def test_trusted_baseline_0112_preserves_existing_operation_mechanism() -> None:
    migration = import_module("CAD2BIM_backend.infrastructure.postgres.migrations.versions."
                              "20261017_0112_ifc_trusted_baseline")
    checks = {constraint.name: str(constraint.sqltext)
              for constraint in Base.metadata.tables["critical_operations"].constraints
              if isinstance(constraint, CheckConstraint)}
    assert migration.down_revision == "20261017_0111"
    assert migration._DETAIL_CHECK != migration._PREVIOUS_DETAIL
    assert checks[migration._BASELINE_NAME] == migration._BASELINE_CHECK
    assert "ifc_trusted_baseline_load" in checks[migration._DETAIL_NAME]
    assert "baseline_loaded" in migration._BASELINE_CHECK
    config = Config(Path(__file__).resolve().parents[3] / "alembic.ini")
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:secret@example.invalid/db")
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "20261017_0111:20261017_0112", sql=True)
    assert "ADD CONSTRAINT ck_critical_operations_ifc_trusted_baseline_detail_valid" in output.getvalue()


def _foreign_keys(
    table_name: str,
) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    return {
        (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }


def _unique_columns(table_name: str) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _check_sql(table_name: str) -> str:
    return " ".join(
        str(constraint.sqltext)
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, CheckConstraint)
    )


def _render_0064_sql() -> str:
    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "20260828_0063:20260830_0064", sql=True)
    return " ".join(output.getvalue().split())


def test_i0a_metadata_has_one_current_pointer_and_thin_revisions() -> None:

    models = Base.metadata.tables["ifc_models"]
    revisions = Base.metadata.tables["ifc_model_revisions"]
    assert {name for name in models.c.keys() if name.startswith("current_revision")} == {
        "current_revision_id"
    }
    assert set(revisions.c.keys()) == {
        "revision_id",
        "owner_id",
        "tenant_id",
        "task_id",
        "run_id",
        "ifc_model_id",
        "revision_no",
        "artifact_id",
        "artifact_version_id",
        "operation_id",
        "created_at",
    }
    assert {
        "artifact_sha256",
        "sha256",
        "size_bytes",
        "media_type",
    }.isdisjoint(revisions.c.keys())


def test_i0a_revision_owns_exact_artifact_and_operation_lineage() -> None:
    model_foreign_keys = _foreign_keys("ifc_models")
    revision_foreign_keys = _foreign_keys("ifc_model_revisions")
    assert (
        (
            "owner_id",
            "task_id",
            "run_id",
            "ifc_model_id",
            "current_revision_id",
        ),
        (
            "ifc_model_revisions.owner_id",
            "ifc_model_revisions.task_id",
            "ifc_model_revisions.run_id",
            "ifc_model_revisions.ifc_model_id",
            "ifc_model_revisions.revision_id",
        ),
    ) in model_foreign_keys
    assert (
        ("owner_id", "task_id", "run_id", "artifact_id"),
        (
            "artifacts.owner_id",
            "artifacts.task_id",
            "artifacts.run_id",
            "artifacts.artifact_id",
        ),
    ) in revision_foreign_keys
    assert (
        ("owner_id", "task_id", "artifact_id", "artifact_version_id"),
        (
            "artifact_versions.owner_id",
            "artifact_versions.task_id",
            "artifact_versions.artifact_id",
            "artifact_versions.version_id",
        ),
    ) in revision_foreign_keys
    assert (
        ("owner_id", "task_id", "run_id", "operation_id"),
        (
            "critical_operations.owner_id",
            "critical_operations.task_id",
            "critical_operations.run_id",
            "critical_operations.operation_id",
        ),
    ) in revision_foreign_keys
    assert ("operation_id",) in _unique_columns("ifc_model_revisions")

    operation_columns = Base.metadata.tables["critical_operations"].c.keys()
    assert not any("result_ifc_revision" in name for name in operation_columns)


def test_i0a_operation_checks_and_data_identity_index_are_exact() -> None:
    checks = _check_sql("critical_operations")
    assert "status IN ('prepared', 'succeeded', 'failed', 'indeterminate')" in checks
    assert (
        "operation_kind IN ('source_ingestion', 'data_transform', 'ifc_model_write', "
        "'geometry_resource_write', 'product_family_write', 'geometry_inspection', 'authoring_publish')"
    ) in checks
    assert "error_phase = 'reconcile'" in checks
    assert all(
        phase in checks
        for phase in (
            "ifc_validation",
            "storage_verification",
            "commit_fence",
        )
    )

    indexes = Base.metadata.tables["critical_operations"].indexes
    fingerprint_index = next(
        index for index in indexes if index.name == "uq_critical_operations_run_tool_fingerprint"
    )
    assert fingerprint_index.unique is True
    assert tuple(column.name for column in fingerprint_index.columns) == (
        "run_id",
        "tool_name",
        "request_fingerprint",
    )
    assert str(fingerprint_index.dialect_options["postgresql"]["where"]) == (
        "operation_kind IN ('source_ingestion', 'data_transform', 'geometry_resource_write', "
        "'product_family_write')"
    )


def test_0064_migration_renders_i0a_constraints_rls_and_runtime_grants() -> None:
    sql = _render_0064_sql()

    assert "CREATE TABLE ifc_models" in sql
    assert "CREATE TABLE ifc_model_revisions" in sql
    assert "CONSTRAINT uq_ifc_models_run UNIQUE (run_id)" in sql
    assert "CONSTRAINT uq_ifc_model_revisions_operation UNIQUE (operation_id)" in sql
    assert "CONSTRAINT fk_ifc_model_revisions_artifact" in sql
    assert "CONSTRAINT fk_ifc_model_revisions_exact_artifact_version" in sql
    assert "CONSTRAINT fk_ifc_model_revisions_operation" in sql
    assert "CONSTRAINT fk_ifc_models_current_revision" in sql
    assert "DEFERRABLE INITIALLY DEFERRED" in sql
    assert (
        "CREATE UNIQUE INDEX uq_critical_operations_run_tool_fingerprint "
        "ON critical_operations (run_id, tool_name, request_fingerprint) "
        "WHERE operation_kind IN ('source_ingestion', 'data_transform')"
    ) in sql

    for table in ("ifc_models", "ifc_model_revisions"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY tenant_isolation ON {table}" in sql
    for role in (
        "cad2bim_api_runtime",
        "cad2bim_runner_runtime",
        "cad2bim_mcp_runtime",
    ):
        assert (f"GRANT SELECT ON TABLE ifc_models, ifc_model_revisions TO {role}") in sql
        assert (
            "GRANT UPDATE (current_revision_id, write_epoch, status, "
            "freeze_reason_code, updated_at) ON TABLE ifc_models "
            f"TO {role}"
        ) in sql
    for role in ("cad2bim_runner_runtime", "cad2bim_mcp_runtime"):
        assert (f"GRANT INSERT ON TABLE ifc_models, ifc_model_revisions TO {role}") in sql
