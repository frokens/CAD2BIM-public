"""Persist tenant-scoped IFC generation read projections.

Revision ID: 20260711_0006
Revises: 20260711_0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260711_0006"
down_revision: str | None = "20260711_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_stage_plans_projection_scope",
        "stage_plans",
        ["owner_id", "task_id", "run_id", "stage_key", "version", "plan_id"],
    )
    op.create_unique_constraint(
        "uq_artifacts_owner_task_run_artifact",
        "artifacts",
        ["owner_id", "task_id", "run_id", "artifact_id"],
    )
    op.add_column(
        "runs",
        sa.Column("final_artifact_id", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("final_artifact_version_id", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("final_artifact", postgresql.JSONB(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_runs_final_artifact_fields_consistent"),
        "runs",
        "(final_artifact_id IS NULL) = (final_artifact_version_id IS NULL) "
        "AND (final_artifact_id IS NULL) = (final_artifact IS NULL)",
    )
    op.create_foreign_key(
        "fk_runs_final_artifact_scope",
        "runs",
        "artifacts",
        ["owner_id", "task_id", "run_id", "final_artifact_id"],
        ["owner_id", "task_id", "run_id", "artifact_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_runs_final_artifact_version",
        "runs",
        "artifact_versions",
        ["owner_id", "task_id", "final_artifact_id", "final_artifact_version_id"],
        ["owner_id", "task_id", "artifact_id", "version_id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "ifc_run_projections",
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_checkpoint_id", sa.String(length=255), nullable=False),
        sa.Column("source_checkpoint_ns", sa.String(length=255), nullable=False),
        sa.Column("global_plan", postgresql.JSONB(), nullable=False),
        sa.Column("current_stage_key", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("global_review", postgresql.JSONB(), nullable=True),
        sa.Column("final_artifact_id", sa.String(length=80), nullable=True),
        sa.Column("final_artifact_version_id", sa.String(length=80), nullable=True),
        sa.Column("final_artifact", postgresql.JSONB(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=False),
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
        sa.CheckConstraint(
            "(final_artifact_id IS NULL) = (final_artifact_version_id IS NULL) "
            "AND (final_artifact_id IS NULL) = (final_artifact IS NULL)",
            name=op.f("ck_ifc_run_projections_final_artifact_fields_consistent"),
        ),
        sa.CheckConstraint(
            "status = 'passed' OR final_artifact IS NULL",
            name=op.f("ck_ifc_run_projections_final_artifact_only_when_passed"),
        ),
        sa.CheckConstraint(
            "status != 'passed' OR (global_review IS NOT NULL AND final_artifact IS NOT NULL)",
            name=op.f("ck_ifc_run_projections_passed_fields_present"),
        ),
        sa.CheckConstraint(
            "revision > 0",
            name=op.f("ck_ifc_run_projections_revision_positive"),
        ),
        sa.CheckConstraint(
            "source_checkpoint_id <> ''",
            name=op.f("ck_ifc_run_projections_source_checkpoint_id_nonempty"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'passed', 'blocked', 'cancelled')",
            name=op.f("ck_ifc_run_projections_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "final_artifact_id"],
            [
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ],
            name="fk_ifc_run_projections_final_artifact_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "final_artifact_id", "final_artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_ifc_run_projections_final_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_ifc_run_projections_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_ifc_run_projections"),
        sa.UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            name="uq_ifc_run_projections_owner_task_run",
        ),
    )
    op.create_index(
        "ix_ifc_run_projections_owner_task",
        "ifc_run_projections",
        ["owner_id", "task_id"],
        unique=False,
    )

    op.create_table(
        "ifc_run_stage_projections",
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("stage_key", sa.String(length=255), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("stage_id", sa.String(length=80), nullable=False),
        sa.Column("stage_order", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage_plan_id", sa.String(length=80), nullable=True),
        sa.Column("stage_plan_version", sa.Integer(), nullable=True),
        sa.Column("business_tree", postgresql.JSONB(), nullable=True),
        sa.Column("reviewed_snapshot_ref", sa.Text(), nullable=True),
        sa.Column("reviewed_artifact_run_id", sa.String(length=80), nullable=True),
        sa.Column("reviewed_artifact_id", sa.String(length=80), nullable=True),
        sa.Column("reviewed_artifact_version_id", sa.String(length=80), nullable=True),
        sa.Column("reviewed_artifact", postgresql.JSONB(), nullable=True),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=False),
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
        sa.CheckConstraint(
            "status != 'passed' OR (stage_plan_id IS NOT NULL "
            "AND business_tree IS NOT NULL AND reviewed_snapshot_ref IS NOT NULL "
            "AND reviewed_artifact IS NOT NULL)",
            name=op.f("ck_ifc_run_stage_projections_passed_fields_present"),
        ),
        sa.CheckConstraint(
            "(reviewed_artifact_id IS NULL) = (reviewed_artifact_run_id IS NULL) "
            "AND (reviewed_artifact_id IS NULL) = (reviewed_artifact_version_id IS NULL) "
            "AND (reviewed_artifact_id IS NULL) = (reviewed_artifact IS NULL)",
            name=op.f("ck_ifc_run_stage_projections_reviewed_artifact_pair"),
        ),
        sa.CheckConstraint(
            "revision > 0",
            name=op.f("ck_ifc_run_stage_projections_revision_positive"),
        ),
        sa.CheckConstraint(
            "stage_order > 0",
            name=op.f("ck_ifc_run_stage_projections_stage_order_positive"),
        ),
        sa.CheckConstraint(
            "(stage_plan_id IS NULL) = (stage_plan_version IS NULL)",
            name=op.f("ck_ifc_run_stage_projections_stage_plan_fields_consistent"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'passed', 'blocked', 'cancelled')",
            name=op.f("ck_ifc_run_stage_projections_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            [
                "owner_id",
                "task_id",
                "reviewed_artifact_run_id",
                "reviewed_artifact_id",
            ],
            [
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ],
            name="fk_ifc_run_stage_projections_reviewed_artifact_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "owner_id",
                "task_id",
                "reviewed_artifact_id",
                "reviewed_artifact_version_id",
            ],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_ifc_run_stage_projections_reviewed_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            [
                "ifc_run_projections.owner_id",
                "ifc_run_projections.task_id",
                "ifc_run_projections.run_id",
            ],
            name="fk_ifc_run_stage_projections_run_projection",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            [
                "owner_id",
                "task_id",
                "run_id",
                "stage_key",
                "stage_plan_version",
                "stage_plan_id",
            ],
            [
                "stage_plans.owner_id",
                "stage_plans.task_id",
                "stage_plans.run_id",
                "stage_plans.stage_key",
                "stage_plans.version",
                "stage_plans.plan_id",
            ],
            name="fk_ifc_run_stage_projections_stage_plan_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "run_id",
            "stage_key",
            name="pk_ifc_run_stage_projections",
        ),
        sa.UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "stage_key",
            name="uq_ifc_run_stage_projections_owner_task_run_stage",
        ),
    )
    op.create_index(
        "ix_ifc_run_stage_projections_owner_run_order",
        "ifc_run_stage_projections",
        ["owner_id", "run_id", "stage_order"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("ifc_run_stage_projections")
    op.drop_table("ifc_run_projections")
    op.drop_constraint("fk_runs_final_artifact_version", "runs", type_="foreignkey")
    op.drop_constraint("fk_runs_final_artifact_scope", "runs", type_="foreignkey")
    op.drop_constraint(
        op.f("ck_runs_final_artifact_fields_consistent"),
        "runs",
        type_="check",
    )
    op.drop_column("runs", "final_artifact")
    op.drop_column("runs", "final_artifact_version_id")
    op.drop_column("runs", "final_artifact_id")
    op.drop_constraint(
        "uq_artifacts_owner_task_run_artifact",
        "artifacts",
        type_="unique",
    )
    op.drop_constraint(
        "uq_stage_plans_projection_scope",
        "stage_plans",
        type_="unique",
    )
