"""Persist exact transform descriptions and immutable repair ancestry.

Revision ID: 20260828_0063
Revises: 20260824_0062
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260828_0063"
down_revision: str | None = "20260824_0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DETAIL_CONSTRAINT = "ck_critical_operations_operation_kind_detail_valid"
_ERROR_PHASE_CONSTRAINT = "ck_critical_operations_error_phase_valid"


def _create_current_checks() -> None:
    op.create_check_constraint(
        op.f(_DETAIL_CONSTRAINT),
        "critical_operations",
        "(operation_kind = 'source_ingestion' AND tool_name = 'cad_import_source' "
        "AND source_filename IS NOT NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NULL AND code_artifact_version_id IS NULL "
        "AND code_artifact_sha256 IS NULL AND description_artifact_id IS NULL "
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
        op.f(_ERROR_PHASE_CONSTRAINT),
        "critical_operations",
        "error_phase IS NULL OR "
        "(operation_kind = 'source_ingestion' AND status = 'failed' "
        "AND error_phase = 'source_extraction') OR "
        "(operation_kind = 'data_transform' AND status = 'failed' "
        "AND error_phase IN ('code_validation', 'execution', 'output_validation'))",
    )


def _create_legacy_checks() -> None:
    op.create_check_constraint(
        op.f(_DETAIL_CONSTRAINT),
        "critical_operations",
        "(operation_kind = 'source_ingestion' AND tool_name = 'cad_import_source' "
        "AND source_filename IS NOT NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NULL AND code_artifact_version_id IS NULL "
        "AND code_artifact_sha256 IS NULL) OR "
        "(operation_kind = 'data_transform' "
        "AND tool_name IN ('data_transform', 'cad_compute_measurements') "
        "AND source_filename IS NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NOT NULL AND code_artifact_version_id IS NOT NULL "
        "AND code_artifact_sha256 IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f(_ERROR_PHASE_CONSTRAINT),
        "critical_operations",
        "error_phase IS NULL OR "
        "(operation_kind = 'source_ingestion' AND status = 'failed' "
        "AND error_phase = 'source_extraction') OR "
        "(operation_kind = 'data_transform' AND status = 'failed' "
        "AND error_phase IN ('execution', 'output_validation'))",
    )


def upgrade() -> None:
    op.add_column(
        "critical_operations", sa.Column("description_artifact_id", sa.String(80))
    )
    op.add_column(
        "critical_operations",
        sa.Column("description_artifact_version_id", sa.String(80)),
    )
    op.add_column(
        "critical_operations", sa.Column("description_artifact_sha256", sa.String(64))
    )
    op.add_column(
        "critical_operations", sa.Column("repair_parent_operation_id", sa.String(80))
    )

    op.execute(
        sa.text(
            """
            UPDATE critical_operations AS operation
            SET description_artifact_id = member.artifact_id,
                description_artifact_version_id = member.artifact_version_id,
                description_artifact_sha256 = member.artifact_sha256
            FROM data_package_members AS member
            WHERE operation.operation_kind = 'data_transform'
              AND operation.status = 'succeeded'
              AND operation.result_package_id = member.package_id
              AND operation.owner_id = member.owner_id
              AND operation.task_id = member.task_id
              AND member.member_kind = 'description'
              AND member.member_name = 'description.md'
            """
        )
    )

    op.drop_constraint(op.f(_DETAIL_CONSTRAINT), "critical_operations", type_="check")
    op.drop_constraint(op.f(_ERROR_PHASE_CONSTRAINT), "critical_operations", type_="check")
    _create_current_checks()
    op.create_check_constraint(
        op.f("ck_critical_operations_description_artifact_triplet_valid"),
        "critical_operations",
        "(description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL) OR "
        "(description_artifact_id IS NOT NULL "
        "AND description_artifact_version_id IS NOT NULL "
        "AND description_artifact_sha256 IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_critical_operations_repair_parent_not_self"),
        "critical_operations",
        "repair_parent_operation_id IS NULL OR repair_parent_operation_id <> operation_id",
    )
    op.create_foreign_key(
        op.f("fk_critical_operations_exact_description_artifact_version"),
        "critical_operations",
        "artifact_versions",
        [
            "owner_id",
            "task_id",
            "description_artifact_id",
            "description_artifact_version_id",
        ],
        ["owner_id", "task_id", "artifact_id", "version_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_critical_operations_repair_parent"),
        "critical_operations",
        "critical_operations",
        ["owner_id", "task_id", "repair_parent_operation_id"],
        ["owner_id", "task_id", "operation_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM critical_operations
                    WHERE tool_name = 'data_repair_transform'
                       OR error_phase = 'code_validation'
                ) THEN
                    RAISE EXCEPTION
                        'cannot downgrade 0063 while repair or code-validation Operations exist';
                END IF;
            END
            $$
            """
        )
    )
    op.drop_constraint(
        op.f("fk_critical_operations_repair_parent"),
        "critical_operations",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_critical_operations_exact_description_artifact_version"),
        "critical_operations",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_critical_operations_repair_parent_not_self"),
        "critical_operations",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_critical_operations_description_artifact_triplet_valid"),
        "critical_operations",
        type_="check",
    )
    op.drop_constraint(op.f(_DETAIL_CONSTRAINT), "critical_operations", type_="check")
    op.drop_constraint(op.f(_ERROR_PHASE_CONSTRAINT), "critical_operations", type_="check")
    _create_legacy_checks()
    op.drop_column("critical_operations", "repair_parent_operation_id")
    op.drop_column("critical_operations", "description_artifact_sha256")
    op.drop_column("critical_operations", "description_artifact_version_id")
    op.drop_column("critical_operations", "description_artifact_id")
