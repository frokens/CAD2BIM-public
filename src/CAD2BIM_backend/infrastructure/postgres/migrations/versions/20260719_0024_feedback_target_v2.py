"""Persist exact active feedback subjects and server-resolved targets.

Revision ID: 20260719_0024
Revises: 20260719_0023
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260719_0024"
down_revision: str | None = "20260719_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "active_feedback_subject",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "feedback",
        sa.Column(
            "target_v2",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        op.f("ck_runs_active_feedback_subject_schema"),
        "runs",
        "active_feedback_subject IS NULL "
        "OR active_feedback_subject ->> 'schema_version' = '1'",
    )
    op.create_check_constraint(
        op.f("ck_feedback_target_v2_schema"),
        "feedback",
        "target_v2 IS NULL OR target_v2 ->> 'schema_version' = '2'",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_feedback_target_v2_schema"),
        "feedback",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_runs_active_feedback_subject_schema"),
        "runs",
        type_="check",
    )
    op.drop_column("feedback", "target_v2")
    op.drop_column("runs", "active_feedback_subject")
