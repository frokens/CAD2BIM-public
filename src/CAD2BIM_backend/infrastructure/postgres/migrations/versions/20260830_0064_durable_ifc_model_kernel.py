"""Add the durable IFC model and revision kernel.

Revision ID: 20260830_0064
Revises: 20260828_0063
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260830_0064"
down_revision: str | None = "20260828_0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STATUS_CHECK = "ck_critical_operations_status_valid"
_KIND_CHECK = "ck_critical_operations_operation_kind_valid"
_INITIATOR_CHECK = "ck_critical_operations_initiator_valid"
_DETAIL_CHECK = "ck_critical_operations_operation_kind_detail_valid"
_TERMINAL_CHECK = "ck_critical_operations_terminal_result_valid"
_ERROR_PHASE_CHECK = "ck_critical_operations_error_phase_valid"
_TIME_CHECK = "ck_critical_operations_time_order_valid"
_DATA_IDENTITY_INDEX = "uq_critical_operations_run_tool_fingerprint"
_IFC_TABLES = ("ifc_models", "ifc_model_revisions")
_RUNTIME_ROLES = (
    "cad2bim_api_runtime",
    "cad2bim_runner_runtime",
    "cad2bim_mcp_runtime",
)


def _create_i0a_operation_checks() -> None:
    op.create_check_constraint(
        op.f(_STATUS_CHECK),
        "critical_operations",
        "status IN ('prepared', 'succeeded', 'failed', 'indeterminate')",
    )
    op.create_check_constraint(
        op.f(_KIND_CHECK),
        "critical_operations",
        "operation_kind IN ('source_ingestion', 'data_transform', 'ifc_model_write')",
    )
    op.create_check_constraint(
        op.f(_INITIATOR_CHECK),
        "critical_operations",
        "initiator IN ('direct_client', 'runner')",
    )
    op.create_check_constraint(
        op.f(_DETAIL_CHECK),
        "critical_operations",
        "(operation_kind = 'source_ingestion' AND tool_name = 'cad_import_source' "
        "AND source_filename IS NOT NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NULL AND code_artifact_version_id IS NULL "
        "AND code_artifact_sha256 IS NULL AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL "
        "AND repair_parent_operation_id IS NULL "
        "AND base_ifc_revision_id IS NULL "
        "AND expected_ifc_write_epoch IS NULL) OR "
        "(operation_kind = 'data_transform' "
        "AND tool_name IN ('data_transform', 'data_repair_transform', "
        "'cad_compute_measurements') "
        "AND source_filename IS NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NOT NULL AND code_artifact_version_id IS NOT NULL "
        "AND code_artifact_sha256 IS NOT NULL "
        "AND ((tool_name = 'data_repair_transform' "
        "AND description_artifact_id IS NOT NULL "
        "AND description_artifact_version_id IS NOT NULL "
        "AND description_artifact_sha256 IS NOT NULL "
        "AND repair_parent_operation_id IS NOT NULL) "
        "OR (tool_name <> 'data_repair_transform' "
        "AND repair_parent_operation_id IS NULL)) "
        "AND base_ifc_revision_id IS NULL "
        "AND expected_ifc_write_epoch IS NULL) OR "
        "(operation_kind = 'ifc_model_write' "
        "AND source_filename IS NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NULL AND code_artifact_version_id IS NULL "
        "AND code_artifact_sha256 IS NULL AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL "
        "AND repair_parent_operation_id IS NULL AND result_package_id IS NULL "
        "AND ((tool_name = 'ifc_model_initialize' "
        "AND base_ifc_revision_id IS NULL "
        "AND expected_ifc_write_epoch IS NULL) "
        "OR (tool_name <> 'ifc_model_initialize' "
        "AND base_ifc_revision_id IS NOT NULL "
        "AND expected_ifc_write_epoch IS NOT NULL)))",
    )
    op.create_check_constraint(
        op.f(_TERMINAL_CHECK),
        "critical_operations",
        "(status = 'prepared' AND finished_at IS NULL "
        "AND result_package_id IS NULL AND error_code IS NULL "
        "AND error_message IS NULL AND error_phase IS NULL) OR "
        "(status = 'succeeded' AND finished_at IS NOT NULL "
        "AND error_code IS NULL AND error_message IS NULL AND error_phase IS NULL "
        "AND ((operation_kind IN ('source_ingestion', 'data_transform') "
        "AND result_package_id IS NOT NULL) OR "
        "(operation_kind = 'ifc_model_write' AND result_package_id IS NULL))) OR "
        "(status = 'failed' AND finished_at IS NOT NULL "
        "AND error_code IS NOT NULL AND error_message IS NOT NULL "
        "AND result_package_id IS NULL) OR "
        "(status = 'indeterminate' AND finished_at IS NOT NULL "
        "AND error_code IS NOT NULL AND error_message IS NOT NULL "
        "AND error_phase = 'reconcile' AND result_package_id IS NULL)",
    )
    op.create_check_constraint(
        op.f(_ERROR_PHASE_CHECK),
        "critical_operations",
        "(status IN ('prepared', 'succeeded') AND error_phase IS NULL) OR "
        "(status = 'indeterminate' AND error_phase = 'reconcile') OR "
        "(status = 'failed' AND operation_kind = 'source_ingestion' "
        "AND (error_phase IS NULL OR error_phase = 'source_extraction')) OR "
        "(status = 'failed' AND operation_kind = 'data_transform' "
        "AND error_phase IN ('code_validation', 'execution', 'output_validation')) OR "
        "(status = 'failed' AND operation_kind = 'ifc_model_write' "
        "AND error_phase IN "
        "('ifc_validation', 'storage_verification', 'commit_fence'))",
    )
    op.create_check_constraint(
        op.f("ck_critical_operations_ifc_write_expectation_pair_valid"),
        "critical_operations",
        "(base_ifc_revision_id IS NULL) = (expected_ifc_write_epoch IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_critical_operations_expected_ifc_write_epoch_positive"),
        "critical_operations",
        "expected_ifc_write_epoch IS NULL OR expected_ifc_write_epoch > 0",
    )
    op.create_check_constraint(
        op.f(_TIME_CHECK),
        "critical_operations",
        "finished_at IS NULL OR finished_at >= started_at",
    )


def _create_0063_operation_checks() -> None:
    op.create_check_constraint(
        op.f(_STATUS_CHECK),
        "critical_operations",
        "status IN ('succeeded', 'failed')",
    )
    op.create_check_constraint(
        op.f(_KIND_CHECK),
        "critical_operations",
        "operation_kind IN ('source_ingestion', 'data_transform')",
    )
    op.create_check_constraint(
        op.f(_INITIATOR_CHECK),
        "critical_operations",
        "initiator = 'direct_client'",
    )
    op.create_check_constraint(
        op.f(_DETAIL_CHECK),
        "critical_operations",
        "(operation_kind = 'source_ingestion' AND tool_name = 'cad_import_source' "
        "AND source_filename IS NOT NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NULL "
        "AND code_artifact_version_id IS NULL AND code_artifact_sha256 IS NULL "
        "AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL "
        "AND repair_parent_operation_id IS NULL) OR "
        "(operation_kind = 'data_transform' "
        "AND tool_name IN ('data_transform', 'data_repair_transform', "
        "'cad_compute_measurements') "
        "AND source_filename IS NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NOT NULL AND code_artifact_version_id IS NOT NULL "
        "AND code_artifact_sha256 IS NOT NULL "
        "AND ((tool_name = 'data_repair_transform' "
        "AND description_artifact_id IS NOT NULL "
        "AND description_artifact_version_id IS NOT NULL "
        "AND description_artifact_sha256 IS NOT NULL "
        "AND repair_parent_operation_id IS NOT NULL) "
        "OR (tool_name <> 'data_repair_transform' "
        "AND repair_parent_operation_id IS NULL)))",
    )
    op.create_check_constraint(
        op.f(_TERMINAL_CHECK),
        "critical_operations",
        "(status = 'succeeded' AND result_package_id IS NOT NULL "
        "AND error_code IS NULL AND error_message IS NULL AND error_phase IS NULL) "
        "OR (status = 'failed' AND error_code IS NOT NULL "
        "AND error_message IS NOT NULL AND result_package_id IS NULL)",
    )
    op.create_check_constraint(
        op.f(_ERROR_PHASE_CHECK),
        "critical_operations",
        "error_phase IS NULL OR "
        "(operation_kind = 'source_ingestion' AND status = 'failed' "
        "AND error_phase = 'source_extraction') OR "
        "(operation_kind = 'data_transform' AND status = 'failed' "
        "AND error_phase IN ('code_validation', 'execution', 'output_validation'))",
    )
    op.create_check_constraint(
        op.f(_TIME_CHECK),
        "critical_operations",
        "finished_at >= started_at",
    )


def _enable_task_rls(table: str) -> None:
    expression = (
        "tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '') "
        "AND task_id = NULLIF(current_setting('cad2bim.task_id', true), '')"
    )
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({expression}) WITH CHECK ({expression})"
        )
    )


def upgrade() -> None:
    op.drop_constraint(
        _DATA_IDENTITY_INDEX,
        "critical_operations",
        type_="unique",
    )
    op.add_column(
        "critical_operations",
        sa.Column("base_ifc_revision_id", sa.String(length=80)),
    )
    op.add_column(
        "critical_operations",
        sa.Column("expected_ifc_write_epoch", sa.BigInteger()),
    )
    op.alter_column(
        "critical_operations",
        "finished_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
    op.create_unique_constraint(
        "uq_critical_operations_owner_task_run_operation",
        "critical_operations",
        ("owner_id", "task_id", "run_id", "operation_id"),
    )
    for constraint in (
        _STATUS_CHECK,
        _KIND_CHECK,
        _INITIATOR_CHECK,
        _DETAIL_CHECK,
        _TERMINAL_CHECK,
        _ERROR_PHASE_CHECK,
        _TIME_CHECK,
    ):
        op.drop_constraint(op.f(constraint), "critical_operations", type_="check")
    _create_i0a_operation_checks()
    op.create_index(
        _DATA_IDENTITY_INDEX,
        "critical_operations",
        ("run_id", "tool_name", "request_fingerprint"),
        unique=True,
        postgresql_where=sa.text(
            "operation_kind IN ('source_ingestion', 'data_transform')"
        ),
    )

    op.create_table(
        "ifc_models",
        sa.Column("ifc_model_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("ifc_schema", sa.String(length=32), nullable=False),
        sa.Column("project_global_id", sa.String(length=22), nullable=False),
        sa.Column("current_revision_id", sa.String(length=80), nullable=False),
        sa.Column("write_epoch", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("freeze_reason_code", sa.String(length=120)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_ifc_models_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_ifc_models_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_ifc_models_owner_task_run",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "ifc_schema = 'IFC4X3_ADD2'",
            name=op.f("ck_ifc_models_ifc_schema_valid"),
        ),
        sa.CheckConstraint(
            "length(project_global_id) = 22",
            name=op.f("ck_ifc_models_project_global_id_length_valid"),
        ),
        sa.CheckConstraint(
            "write_epoch > 0",
            name=op.f("ck_ifc_models_write_epoch_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'frozen')",
            name=op.f("ck_ifc_models_status_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'active' AND freeze_reason_code IS NULL) OR "
            "(status = 'frozen' AND freeze_reason_code IS NOT NULL)",
            name=op.f("ck_ifc_models_freeze_reason_valid"),
        ),
        sa.PrimaryKeyConstraint("ifc_model_id", name=op.f("pk_ifc_models")),
        sa.UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "ifc_model_id",
            name="uq_ifc_models_owner_task_run_model",
        ),
        sa.UniqueConstraint("run_id", name="uq_ifc_models_run"),
    )
    op.create_index(op.f("ix_ifc_models_tenant_id"), "ifc_models", ("tenant_id",))
    op.create_index(
        "ix_ifc_models_run_current",
        "ifc_models",
        ("run_id", "current_revision_id"),
    )

    op.create_table(
        "ifc_model_revisions",
        sa.Column("revision_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("ifc_model_id", sa.String(length=80), nullable=False),
        sa.Column("revision_no", sa.BigInteger(), nullable=False),
        sa.Column("artifact_id", sa.String(length=80), nullable=False),
        sa.Column("artifact_version_id", sa.String(length=80), nullable=False),
        sa.Column("operation_id", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_ifc_model_revisions_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_ifc_model_revisions_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_ifc_model_revisions_owner_task_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "ifc_model_id"],
            [
                "ifc_models.owner_id",
                "ifc_models.task_id",
                "ifc_models.run_id",
                "ifc_models.ifc_model_id",
            ],
            name="fk_ifc_model_revisions_model",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "artifact_id"],
            [
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ],
            name="fk_ifc_model_revisions_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "artifact_id", "artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_ifc_model_revisions_exact_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "operation_id"],
            [
                "critical_operations.owner_id",
                "critical_operations.task_id",
                "critical_operations.run_id",
                "critical_operations.operation_id",
            ],
            name="fk_ifc_model_revisions_operation",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "revision_no > 0",
            name=op.f("ck_ifc_model_revisions_revision_no_positive"),
        ),
        sa.PrimaryKeyConstraint(
            "revision_id",
            name=op.f("pk_ifc_model_revisions"),
        ),
        sa.UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "ifc_model_id",
            "revision_id",
            name="uq_ifc_model_revisions_owner_task_run_model_revision",
        ),
        sa.UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "revision_id",
            name="uq_ifc_model_revisions_owner_task_run_revision",
        ),
        sa.UniqueConstraint(
            "ifc_model_id",
            "revision_no",
            name="uq_ifc_model_revisions_model_revision_no",
        ),
        sa.UniqueConstraint(
            "operation_id",
            name="uq_ifc_model_revisions_operation",
        ),
    )
    op.create_index(
        op.f("ix_ifc_model_revisions_tenant_id"),
        "ifc_model_revisions",
        ("tenant_id",),
    )
    op.create_index(
        "ix_ifc_model_revisions_model_created",
        "ifc_model_revisions",
        ("ifc_model_id", "created_at"),
    )

    op.create_foreign_key(
        "fk_ifc_models_current_revision",
        "ifc_models",
        "ifc_model_revisions",
        ("owner_id", "task_id", "run_id", "ifc_model_id", "current_revision_id"),
        ("owner_id", "task_id", "run_id", "ifc_model_id", "revision_id"),
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_critical_operations_base_ifc_revision",
        "critical_operations",
        "ifc_model_revisions",
        ("owner_id", "task_id", "run_id", "base_ifc_revision_id"),
        ("owner_id", "task_id", "run_id", "revision_id"),
        ondelete="RESTRICT",
    )

    for table in _IFC_TABLES:
        _enable_task_rls(table)
    tables = ", ".join(_IFC_TABLES)
    for role in _RUNTIME_ROLES:
        op.execute(sa.text(f"GRANT SELECT ON TABLE {tables} TO {role}"))
    for role in ("cad2bim_runner_runtime", "cad2bim_mcp_runtime"):
        op.execute(sa.text(f"GRANT INSERT ON TABLE {tables} TO {role}"))
    for role in _RUNTIME_ROLES:
        op.execute(
            sa.text(
                "GRANT UPDATE (current_revision_id, write_epoch, status, "
                f"freeze_reason_code, updated_at) ON TABLE ifc_models TO {role}"
            )
        )
        op.execute(
            sa.text(
                "GRANT UPDATE (status, result_package_id, error_code, error_message, "
                "error_phase, operation_detail_json, finished_at) "
                f"ON TABLE critical_operations TO {role}"
            )
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM ifc_models)
                   OR EXISTS (SELECT 1 FROM ifc_model_revisions)
                   OR EXISTS (
                       SELECT 1 FROM critical_operations
                       WHERE operation_kind = 'ifc_model_write'
                          OR status IN ('prepared', 'indeterminate')
                          OR initiator <> 'direct_client'
                          OR base_ifc_revision_id IS NOT NULL
                          OR expected_ifc_write_epoch IS NOT NULL
                   ) THEN
                    RAISE EXCEPTION
                        'cannot downgrade 0064 while durable IFC state exists';
                END IF;
            END
            $$
            """
        )
    )

    for role in _RUNTIME_ROLES:
        op.execute(
            sa.text(
                "REVOKE UPDATE (status, result_package_id, error_code, error_message, "
                "error_phase, operation_detail_json, finished_at) "
                f"ON TABLE critical_operations FROM {role}"
            )
        )
        op.execute(
            sa.text(
                "REVOKE UPDATE (current_revision_id, write_epoch, status, "
                f"freeze_reason_code, updated_at) ON TABLE ifc_models FROM {role}"
            )
        )
    tables = ", ".join(_IFC_TABLES)
    for role in ("cad2bim_runner_runtime", "cad2bim_mcp_runtime"):
        op.execute(sa.text(f"REVOKE INSERT ON TABLE {tables} FROM {role}"))
    for role in _RUNTIME_ROLES:
        op.execute(sa.text(f"REVOKE SELECT ON TABLE {tables} FROM {role}"))

    op.drop_constraint(
        "fk_critical_operations_base_ifc_revision",
        "critical_operations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_ifc_models_current_revision",
        "ifc_models",
        type_="foreignkey",
    )
    for table in reversed(_IFC_TABLES):
        op.execute(sa.text(f"DROP POLICY tenant_isolation ON {table}"))
        op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
        op.drop_table(table)

    op.drop_index(_DATA_IDENTITY_INDEX, table_name="critical_operations")
    op.drop_constraint(
        "uq_critical_operations_owner_task_run_operation",
        "critical_operations",
        type_="unique",
    )
    for constraint in (
        _STATUS_CHECK,
        _KIND_CHECK,
        _INITIATOR_CHECK,
        _DETAIL_CHECK,
        _TERMINAL_CHECK,
        _ERROR_PHASE_CHECK,
        "ck_critical_operations_ifc_write_expectation_pair_valid",
        "ck_critical_operations_expected_ifc_write_epoch_positive",
        _TIME_CHECK,
    ):
        op.drop_constraint(op.f(constraint), "critical_operations", type_="check")
    _create_0063_operation_checks()
    op.alter_column(
        "critical_operations",
        "finished_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.drop_column("critical_operations", "expected_ifc_write_epoch")
    op.drop_column("critical_operations", "base_ifc_revision_id")
    op.create_unique_constraint(
        _DATA_IDENTITY_INDEX,
        "critical_operations",
        ("run_id", "tool_name", "request_fingerprint"),
    )
