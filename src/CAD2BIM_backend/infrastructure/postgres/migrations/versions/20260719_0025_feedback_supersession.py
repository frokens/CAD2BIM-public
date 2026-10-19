"""Persist terminal disposition for stale exact feedback targets.

Revision ID: 20260719_0025
Revises: 20260719_0024
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260719_0025"
down_revision: str | None = "20260719_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "feedback",
        sa.Column(
            "supersession",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        op.f("ck_feedback_supersession_schema"),
        "feedback",
        "supersession IS NULL OR "
        "(supersession ->> 'schema_version' = '1' "
        "AND status = 'superseded' "
        "AND target_v2 IS NOT NULL "
        "AND delivery_id IS NULL "
        "AND delivered_to IS NULL "
        "AND delivered_at IS NULL "
        "AND acknowledged_by_checkpoint_id IS NULL "
        "AND acknowledged_at IS NULL "
        "AND consumed_by_checkpoint_id IS NULL "
        "AND consumed_at IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_feedback_supersession_schema"),
        "feedback",
        type_="check",
    )
    op.drop_column("feedback", "supersession")
