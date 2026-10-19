"""Persist exact source and observed cursors for savepoint resume operations.

Revision ID: 20260712_0010
Revises: 20260712_0009
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260712_0010"
down_revision: str | None = "20260712_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run_resume_operations",
        sa.Column("cursor_schema_version", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "run_resume_operations",
        sa.Column("source_checkpoint_cursor", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "run_resume_operations",
        sa.Column("observed_checkpoint_cursor", postgresql.JSONB(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_run_resume_operations_cursor_schema_version_valid"),
        "run_resume_operations",
        "cursor_schema_version IS NULL OR cursor_schema_version = '1'",
    )
    op.create_check_constraint(
        op.f("ck_run_resume_operations_exact_cursor_phase_consistent"),
        "run_resume_operations",
        "((cursor_schema_version IS NULL AND source_checkpoint_cursor IS NULL "
        "AND observed_checkpoint_cursor IS NULL) OR "
        "(cursor_schema_version = '1' AND source_checkpoint_cursor IS NOT NULL AND "
        "(((phase IN ('intent', 'graph_inflight')) "
        "AND observed_checkpoint_cursor IS NULL) OR "
        "((phase IN ('graph_observed', 'finalized')) "
        "AND observed_checkpoint_cursor IS NOT NULL))))",
    )
    op.create_check_constraint(
        op.f("ck_run_resume_operations_observed_cursor_identity"),
        "run_resume_operations",
        "observed_checkpoint_cursor IS NULL OR "
        "observed_checkpoint_cursor ->> 'checkpoint_id' = observed_checkpoint_id",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_run_resume_operations_observed_cursor_identity"),
        "run_resume_operations",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_run_resume_operations_exact_cursor_phase_consistent"),
        "run_resume_operations",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_run_resume_operations_cursor_schema_version_valid"),
        "run_resume_operations",
        type_="check",
    )
    op.drop_column("run_resume_operations", "observed_checkpoint_cursor")
    op.drop_column("run_resume_operations", "source_checkpoint_cursor")
    op.drop_column("run_resume_operations", "cursor_schema_version")
