"""Persist one exact graph-observation cursor for resume operations.

Revision ID: 20260809_0042
Revises: 20260803_0041
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260809_0042"
down_revision: str | None = "20260803_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The exact JSON cursor already contains checkpoint_id and is mandatory for
    # observed/finalized rows. Drop the duplicated scalar identity and the
    # internal schema discriminator in the same forward-only owner cut.
    for constraint in (
        "ck_run_resume_operations_exact_cursor_phase_consistent",
        "ck_run_resume_operations_cursor_schema_version_valid",
        "ck_run_resume_operations_observed_cursor_identity",
        "ck_run_resume_operations_observation_pair",
        "ck_run_resume_operations_phase_observation_consistent",
    ):
        op.drop_constraint(op.f(constraint), "run_resume_operations", type_="check")

    op.drop_column("run_resume_operations", "observed_checkpoint_id")
    op.drop_column("run_resume_operations", "cursor_schema_version")

    op.create_check_constraint(
        op.f("ck_run_resume_operations_observation_pair"),
        "run_resume_operations",
        "(observed_checkpoint_cursor IS NULL) = (observed_outcome IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_run_resume_operations_phase_observation_consistent"),
        "run_resume_operations",
        "((phase IN ('intent', 'graph_inflight')) "
        "AND observed_checkpoint_cursor IS NULL) OR "
        "((phase IN ('graph_observed', 'finalized')) "
        "AND observed_checkpoint_cursor IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_run_resume_operations_exact_cursor_phase_consistent"),
        "run_resume_operations",
        "source_checkpoint_cursor IS NOT NULL AND "
        "((phase IN ('intent', 'graph_inflight') "
        "AND observed_checkpoint_cursor IS NULL) OR "
        "(phase IN ('graph_observed', 'finalized') "
        "AND observed_checkpoint_cursor IS NOT NULL))",
    )


def downgrade() -> None:
    for constraint in (
        "ck_run_resume_operations_exact_cursor_phase_consistent",
        "ck_run_resume_operations_observation_pair",
        "ck_run_resume_operations_phase_observation_consistent",
    ):
        op.drop_constraint(op.f(constraint), "run_resume_operations", type_="check")

    op.add_column(
        "run_resume_operations",
        sa.Column(
            "cursor_schema_version",
            sa.String(length=16),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "run_resume_operations",
        sa.Column("observed_checkpoint_id", sa.String(length=255), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE run_resume_operations
            SET observed_checkpoint_id = observed_checkpoint_cursor ->> 'checkpoint_id'
            WHERE observed_checkpoint_cursor IS NOT NULL
            """
        )
    )

    op.create_check_constraint(
        op.f("ck_run_resume_operations_cursor_schema_version_valid"),
        "run_resume_operations",
        "cursor_schema_version = '1'",
    )
    op.create_check_constraint(
        op.f("ck_run_resume_operations_observation_pair"),
        "run_resume_operations",
        "(observed_checkpoint_id IS NULL) = (observed_outcome IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_run_resume_operations_phase_observation_consistent"),
        "run_resume_operations",
        "((phase IN ('intent', 'graph_inflight')) AND observed_checkpoint_id IS NULL) "
        "OR ((phase IN ('graph_observed', 'finalized')) "
        "AND observed_checkpoint_id IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_run_resume_operations_exact_cursor_phase_consistent"),
        "run_resume_operations",
        "cursor_schema_version = '1' AND source_checkpoint_cursor IS NOT NULL AND "
        "((phase IN ('intent', 'graph_inflight') "
        "AND observed_checkpoint_cursor IS NULL) OR "
        "(phase IN ('graph_observed', 'finalized') "
        "AND observed_checkpoint_cursor IS NOT NULL))",
    )
    op.create_check_constraint(
        op.f("ck_run_resume_operations_observed_cursor_identity"),
        "run_resume_operations",
        "observed_checkpoint_cursor IS NULL OR "
        "observed_checkpoint_cursor ->> 'checkpoint_id' = observed_checkpoint_id",
    )
