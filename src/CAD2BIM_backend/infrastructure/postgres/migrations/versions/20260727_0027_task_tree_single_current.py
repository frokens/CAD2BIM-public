"""Hard-cut TaskTree current state to exact Stage business views.

Revision ID: 20260727_0027
Revises: 20260727_0026
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260727_0027"
down_revision: str | None = "20260727_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # This projection is derived from durable checkpoints and can be rebuilt.
    # Clearing it prevents old embedded trees from being mistaken for current
    # facts during the same hard-cut migration.
    op.execute(sa.text("DELETE FROM ifc_run_stage_projections"))

    op.drop_constraint(
        "fk_ifc_run_stage_projections_reviewed_artifact_scope",
        "ifc_run_stage_projections",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_ifc_run_stage_projections_reviewed_artifact_version",
        "ifc_run_stage_projections",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_ifc_run_stage_projections_passed_fields_present"),
        "ifc_run_stage_projections",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_ifc_run_stage_projections_reviewed_artifact_pair"),
        "ifc_run_stage_projections",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_ifc_run_stage_projections_stage_plan_fields_consistent"),
        "ifc_run_stage_projections",
        type_="check",
    )

    op.drop_column("ifc_run_stage_projections", "business_tree")
    op.drop_column("ifc_run_stage_projections", "reviewed_snapshot_ref")
    op.drop_column("ifc_run_stage_projections", "reviewed_artifact")
    op.drop_column("ifc_run_stage_projections", "reviewed_artifact_version_id")
    op.drop_column("ifc_run_stage_projections", "reviewed_artifact_id")
    op.drop_column("ifc_run_stage_projections", "reviewed_artifact_run_id")
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("accepted_artifact_run_id", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("accepted_artifact_id", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("accepted_artifact_version_id", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("accepted_artifact", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("accepted_tree_revision", sa.Integer(), nullable=True),
    )
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("accepted_tree_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("accepted_savepoint_id", sa.String(length=80), nullable=True),
    )

    op.create_foreign_key(
        "fk_ifc_stage_projection_accepted_artifact_scope",
        "ifc_run_stage_projections",
        "artifacts",
        ["owner_id", "task_id", "accepted_artifact_run_id", "accepted_artifact_id"],
        ["owner_id", "task_id", "run_id", "artifact_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_ifc_stage_projection_accepted_artifact_version",
        "ifc_run_stage_projections",
        "artifact_versions",
        [
            "owner_id",
            "task_id",
            "accepted_artifact_id",
            "accepted_artifact_version_id",
        ],
        ["owner_id", "task_id", "artifact_id", "version_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_ifc_stage_projection_accepted_savepoint",
        "ifc_run_stage_projections",
        "checkpoint_links",
        ["owner_id", "task_id", "accepted_savepoint_id"],
        ["owner_id", "task_id", "savepoint_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_ifc_run_stage_projections_accepted_tree_fields_consistent"),
        "ifc_run_stage_projections",
        "(stage_plan_id IS NULL) = (stage_plan_version IS NULL) "
        "AND (stage_plan_id IS NULL) = (accepted_tree_revision IS NULL) "
        "AND (stage_plan_id IS NULL) = (accepted_tree_fingerprint IS NULL) "
        "AND (stage_plan_id IS NULL) = (accepted_savepoint_id IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_ifc_run_stage_projections_accepted_artifact_pair"),
        "ifc_run_stage_projections",
        "(accepted_artifact_id IS NULL) = (accepted_artifact_run_id IS NULL) "
        "AND (accepted_artifact_id IS NULL) = (accepted_artifact_version_id IS NULL) "
        "AND (accepted_artifact_id IS NULL) = (accepted_artifact IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_ifc_run_stage_projections_passed_fields_present"),
        "ifc_run_stage_projections",
        "status != 'passed' OR (stage_plan_id IS NOT NULL "
        "AND accepted_tree_revision IS NOT NULL "
        "AND accepted_tree_fingerprint IS NOT NULL "
        "AND accepted_savepoint_id IS NOT NULL "
        "AND accepted_artifact IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_ifc_run_stage_projections_accepted_tree_revision_positive"),
        "ifc_run_stage_projections",
        "accepted_tree_revision IS NULL OR accepted_tree_revision > 0",
    )
    op.create_check_constraint(
        op.f("ck_ifc_run_stage_projections_accepted_tree_fingerprint_shape"),
        "ifc_run_stage_projections",
        "accepted_tree_fingerprint IS NULL OR "
        "(length(accepted_tree_fingerprint) = 64 "
        "AND accepted_tree_fingerprint ~ '^[0-9a-f]{64}$')",
    )

    op.drop_column("runs", "plan")
    op.drop_column("runs", "business_tree")


def downgrade() -> None:
    op.add_column("runs", sa.Column("plan", postgresql.JSONB(), nullable=True))
    op.add_column("runs", sa.Column("business_tree", postgresql.JSONB(), nullable=True))

    op.execute(sa.text("DELETE FROM ifc_run_stage_projections"))
    op.drop_constraint(
        op.f("ck_ifc_run_stage_projections_accepted_tree_fingerprint_shape"),
        "ifc_run_stage_projections",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_ifc_run_stage_projections_accepted_tree_revision_positive"),
        "ifc_run_stage_projections",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_ifc_run_stage_projections_passed_fields_present"),
        "ifc_run_stage_projections",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_ifc_run_stage_projections_accepted_artifact_pair"),
        "ifc_run_stage_projections",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_ifc_run_stage_projections_accepted_tree_fields_consistent"),
        "ifc_run_stage_projections",
        type_="check",
    )
    op.drop_constraint(
        "fk_ifc_stage_projection_accepted_savepoint",
        "ifc_run_stage_projections",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_ifc_stage_projection_accepted_artifact_version",
        "ifc_run_stage_projections",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_ifc_stage_projection_accepted_artifact_scope",
        "ifc_run_stage_projections",
        type_="foreignkey",
    )
    op.drop_column("ifc_run_stage_projections", "accepted_savepoint_id")
    op.drop_column("ifc_run_stage_projections", "accepted_tree_fingerprint")
    op.drop_column("ifc_run_stage_projections", "accepted_tree_revision")
    op.drop_column("ifc_run_stage_projections", "accepted_artifact")
    op.drop_column("ifc_run_stage_projections", "accepted_artifact_version_id")
    op.drop_column("ifc_run_stage_projections", "accepted_artifact_id")
    op.drop_column("ifc_run_stage_projections", "accepted_artifact_run_id")
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("reviewed_artifact_run_id", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("reviewed_artifact_id", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("reviewed_artifact_version_id", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("reviewed_artifact", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("reviewed_snapshot_ref", sa.Text(), nullable=True),
    )
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("business_tree", postgresql.JSONB(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ifc_run_stage_projections_reviewed_artifact_scope",
        "ifc_run_stage_projections",
        "artifacts",
        ["owner_id", "task_id", "reviewed_artifact_run_id", "reviewed_artifact_id"],
        ["owner_id", "task_id", "run_id", "artifact_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_ifc_run_stage_projections_reviewed_artifact_version",
        "ifc_run_stage_projections",
        "artifact_versions",
        [
            "owner_id",
            "task_id",
            "reviewed_artifact_id",
            "reviewed_artifact_version_id",
        ],
        ["owner_id", "task_id", "artifact_id", "version_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_ifc_run_stage_projections_stage_plan_fields_consistent"),
        "ifc_run_stage_projections",
        "(stage_plan_id IS NULL) = (stage_plan_version IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_ifc_run_stage_projections_reviewed_artifact_pair"),
        "ifc_run_stage_projections",
        "(reviewed_artifact_id IS NULL) = (reviewed_artifact_run_id IS NULL) "
        "AND (reviewed_artifact_id IS NULL) = (reviewed_artifact_version_id IS NULL) "
        "AND (reviewed_artifact_id IS NULL) = (reviewed_artifact IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_ifc_run_stage_projections_passed_fields_present"),
        "ifc_run_stage_projections",
        "status != 'passed' OR (stage_plan_id IS NOT NULL "
        "AND business_tree IS NOT NULL AND reviewed_snapshot_ref IS NOT NULL "
        "AND reviewed_artifact IS NOT NULL)",
    )
