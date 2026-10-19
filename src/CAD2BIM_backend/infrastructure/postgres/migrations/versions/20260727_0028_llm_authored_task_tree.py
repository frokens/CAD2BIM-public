"""Hard-cut stored TaskTree definitions to the LLM-authored domain.

Revision ID: 20260727_0028
Revises: 20260727_0027
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260727_0028"
down_revision: str | None = "20260727_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # These rows are read projections derived from durable checkpoints. Old
    # fixed-template definitions cannot be interpreted as LLM-authored trees,
    # so the pre-release hard cut clears them instead of adding a fallback.
    op.execute(sa.text("DELETE FROM ifc_run_stage_projections"))
    op.execute(sa.text("DELETE FROM run_stage_business_views"))
    op.execute(sa.text("DELETE FROM stage_plans"))

    op.drop_constraint(
        "fk_ifc_run_stage_projections_stage_plan_scope",
        "ifc_run_stage_projections",
        type_="foreignkey",
    )

    op.drop_constraint(
        op.f("ck_run_stage_business_views_plan_version_positive"),
        "run_stage_business_views",
        type_="check",
    )
    op.alter_column(
        "run_stage_business_views",
        "plan_version",
        new_column_name="definition_revision",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        "run_stage_business_views",
        "business_tree",
        new_column_name="task_tree",
        existing_type=sa.JSON(),
        existing_nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_run_stage_business_views_definition_revision_positive"),
        "run_stage_business_views",
        "definition_revision >= 1",
    )

    op.drop_constraint(
        "uq_stage_plans_projection_scope",
        "stage_plans",
        type_="unique",
    )
    op.drop_constraint(
        "uq_stage_plans_run_stage_version",
        "stage_plans",
        type_="unique",
    )
    op.drop_constraint(
        op.f("ck_stage_plans_version_positive"),
        "stage_plans",
        type_="check",
    )
    op.drop_column("stage_plans", "template_id")
    op.alter_column(
        "stage_plans",
        "version",
        new_column_name="definition_revision",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_stage_plans_definition_revision_positive"),
        "stage_plans",
        "definition_revision > 0",
    )
    op.create_unique_constraint(
        "uq_stage_plans_run_stage_definition_revision",
        "stage_plans",
        ["run_id", "stage_key", "definition_revision"],
    )
    op.create_unique_constraint(
        "uq_stage_plans_projection_scope",
        "stage_plans",
        [
            "owner_id",
            "task_id",
            "run_id",
            "stage_key",
            "definition_revision",
            "plan_id",
        ],
    )
    op.create_foreign_key(
        "fk_ifc_run_stage_projections_stage_plan_scope",
        "ifc_run_stage_projections",
        "stage_plans",
        [
            "owner_id",
            "task_id",
            "run_id",
            "stage_key",
            "stage_plan_version",
            "stage_plan_id",
        ],
        [
            "owner_id",
            "task_id",
            "run_id",
            "stage_key",
            "definition_revision",
            "plan_id",
        ],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM ifc_run_stage_projections"))
    op.execute(sa.text("DELETE FROM run_stage_business_views"))
    op.execute(sa.text("DELETE FROM stage_plans"))

    op.drop_constraint(
        "fk_ifc_run_stage_projections_stage_plan_scope",
        "ifc_run_stage_projections",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_stage_plans_projection_scope",
        "stage_plans",
        type_="unique",
    )
    op.drop_constraint(
        "uq_stage_plans_run_stage_definition_revision",
        "stage_plans",
        type_="unique",
    )
    op.drop_constraint(
        op.f("ck_stage_plans_definition_revision_positive"),
        "stage_plans",
        type_="check",
    )
    op.alter_column(
        "stage_plans",
        "definition_revision",
        new_column_name="version",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.add_column(
        "stage_plans",
        sa.Column("template_id", sa.String(length=255), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_stage_plans_version_positive"),
        "stage_plans",
        "version > 0",
    )
    op.create_unique_constraint(
        "uq_stage_plans_run_stage_version",
        "stage_plans",
        ["run_id", "stage_key", "version"],
    )
    op.create_unique_constraint(
        "uq_stage_plans_projection_scope",
        "stage_plans",
        ["owner_id", "task_id", "run_id", "stage_key", "version", "plan_id"],
    )

    op.drop_constraint(
        op.f("ck_run_stage_business_views_definition_revision_positive"),
        "run_stage_business_views",
        type_="check",
    )
    op.alter_column(
        "run_stage_business_views",
        "definition_revision",
        new_column_name="plan_version",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        "run_stage_business_views",
        "task_tree",
        new_column_name="business_tree",
        existing_type=sa.JSON(),
        existing_nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_run_stage_business_views_plan_version_positive"),
        "run_stage_business_views",
        "plan_version >= 1",
    )
    op.create_foreign_key(
        "fk_ifc_run_stage_projections_stage_plan_scope",
        "ifc_run_stage_projections",
        "stage_plans",
        [
            "owner_id",
            "task_id",
            "run_id",
            "stage_key",
            "stage_plan_version",
            "stage_plan_id",
        ],
        [
            "owner_id",
            "task_id",
            "run_id",
            "stage_key",
            "version",
            "plan_id",
        ],
        ondelete="RESTRICT",
    )
