"""Hard-cut IFC Stage projections to semantic DAG identities.

Revision ID: 20260801_0035
Revises: 20260730_0034
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260801_0035"
down_revision: str | None = "20260730_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The system is not released.  Existing rows encode the old linear Stage
    # order and cannot be converted into authoritative DAG generations or
    # ContextCard identities, so quarantine happens by rebuilding this
    # checkpoint-derived projection instead of adding a runtime fallback.
    op.execute(sa.text("DELETE FROM ifc_run_stage_projections"))
    op.execute(sa.text("DELETE FROM ifc_run_projections"))

    op.drop_constraint(
        "fk_ifc_run_stage_projections_stage_plan_scope",
        "ifc_run_stage_projections",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_ifc_run_stage_projections_stage_order_positive"),
        "ifc_run_stage_projections",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_ifc_run_stage_projections_accepted_tree_fields_consistent"),
        "ifc_run_stage_projections",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_ifc_run_stage_projections_passed_fields_present"),
        "ifc_run_stage_projections",
        type_="check",
    )
    op.drop_index(
        "ix_ifc_run_stage_projections_owner_run_order",
        table_name="ifc_run_stage_projections",
    )

    op.alter_column(
        "ifc_run_stage_projections",
        "stage_plan_version",
        new_column_name="stage_plan_revision",
        existing_type=sa.Integer(),
        existing_nullable=True,
    )
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("stage_generation", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column(
            "dependency_input_digest",
            sa.String(length=64),
            nullable=False,
            server_default="0" * 64,
        ),
    )
    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("context_card", postgresql.JSONB(), nullable=True),
    )
    op.drop_column("ifc_run_stage_projections", "stage_order")
    op.alter_column(
        "ifc_run_stage_projections", "stage_generation", server_default=None
    )
    op.alter_column(
        "ifc_run_stage_projections", "dependency_input_digest", server_default=None
    )

    op.create_check_constraint(
        op.f("ck_ifc_run_stage_projections_stage_generation_positive"),
        "ifc_run_stage_projections",
        "stage_generation > 0",
    )
    op.create_check_constraint(
        op.f("ck_ifc_run_stage_projections_dependency_input_digest_shape"),
        "ifc_run_stage_projections",
        "length(dependency_input_digest) = 64",
    )
    op.create_check_constraint(
        op.f("ck_ifc_run_stage_projections_accepted_tree_fields_consistent"),
        "ifc_run_stage_projections",
        "(stage_plan_id IS NULL) = (stage_plan_revision IS NULL) "
        "AND (stage_plan_id IS NULL) = (accepted_tree_revision IS NULL) "
        "AND (stage_plan_id IS NULL) = (accepted_tree_fingerprint IS NULL) "
        "AND (stage_plan_id IS NULL) = (accepted_savepoint_id IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_ifc_run_stage_projections_passed_fields_present"),
        "ifc_run_stage_projections",
        "status != 'passed' OR (stage_plan_id IS NOT NULL "
        "AND accepted_tree_revision IS NOT NULL "
        "AND accepted_tree_fingerprint IS NOT NULL "
        "AND accepted_savepoint_id IS NOT NULL "
        "AND accepted_artifact IS NOT NULL "
        "AND context_card IS NOT NULL)",
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
            "stage_plan_revision",
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
    op.create_index(
        "ix_ifc_run_stage_projections_owner_run",
        "ifc_run_stage_projections",
        ["owner_id", "run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM ifc_run_stage_projections"))
    op.execute(sa.text("DELETE FROM ifc_run_projections"))

    op.drop_constraint(
        "fk_ifc_run_stage_projections_stage_plan_scope",
        "ifc_run_stage_projections",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_ifc_run_stage_projections_stage_generation_positive"),
        "ifc_run_stage_projections",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_ifc_run_stage_projections_dependency_input_digest_shape"),
        "ifc_run_stage_projections",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_ifc_run_stage_projections_accepted_tree_fields_consistent"),
        "ifc_run_stage_projections",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_ifc_run_stage_projections_passed_fields_present"),
        "ifc_run_stage_projections",
        type_="check",
    )
    op.drop_index(
        "ix_ifc_run_stage_projections_owner_run",
        table_name="ifc_run_stage_projections",
    )

    op.add_column(
        "ifc_run_stage_projections",
        sa.Column("stage_order", sa.Integer(), nullable=False, server_default="1"),
    )
    op.alter_column("ifc_run_stage_projections", "stage_order", server_default=None)
    op.drop_column("ifc_run_stage_projections", "context_card")
    op.drop_column("ifc_run_stage_projections", "dependency_input_digest")
    op.drop_column("ifc_run_stage_projections", "stage_generation")
    op.alter_column(
        "ifc_run_stage_projections",
        "stage_plan_revision",
        new_column_name="stage_plan_version",
        existing_type=sa.Integer(),
        existing_nullable=True,
    )

    op.create_check_constraint(
        op.f("ck_ifc_run_stage_projections_stage_order_positive"),
        "ifc_run_stage_projections",
        "stage_order > 0",
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
        op.f("ck_ifc_run_stage_projections_passed_fields_present"),
        "ifc_run_stage_projections",
        "status != 'passed' OR (stage_plan_id IS NOT NULL "
        "AND accepted_tree_revision IS NOT NULL "
        "AND accepted_tree_fingerprint IS NOT NULL "
        "AND accepted_savepoint_id IS NOT NULL "
        "AND accepted_artifact IS NOT NULL)",
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
    op.create_index(
        "ix_ifc_run_stage_projections_owner_run_order",
        "ifc_run_stage_projections",
        ["owner_id", "run_id", "stage_order"],
        unique=False,
    )
