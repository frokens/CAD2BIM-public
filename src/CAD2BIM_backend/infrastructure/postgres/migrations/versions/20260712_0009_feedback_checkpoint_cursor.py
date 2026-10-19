"""Persist the exact Graph cursor used to acknowledge feedback.

Revision ID: 20260712_0009
Revises: 20260712_0008
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260712_0009"
down_revision: str | None = "20260712_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "feedback",
        sa.Column(
            "acknowledged_by_checkpoint_thread_id",
            sa.String(length=255),
            nullable=True,
        ),
    )
    op.add_column(
        "feedback",
        sa.Column(
            "acknowledged_by_checkpoint_ns",
            sa.String(length=1_024),
            nullable=True,
        ),
    )
    op.add_column(
        "feedback",
        sa.Column(
            "acknowledged_by_root_checkpoint_id",
            sa.String(length=255),
            nullable=True,
        ),
    )
    op.add_column(
        "feedback",
        sa.Column(
            "acknowledged_by_checkpoint_map",
            postgresql.JSONB(),
            nullable=True,
        ),
    )
    op.add_column(
        "feedback",
        sa.Column(
            "acknowledged_by_checkpoint_digest",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        op.f("ck_feedback_exact_acknowledgement_cursor_consistent"),
        "feedback",
        "((acknowledged_by_checkpoint_thread_id IS NULL "
        "AND acknowledged_by_checkpoint_ns IS NULL "
        "AND acknowledged_by_root_checkpoint_id IS NULL "
        "AND acknowledged_by_checkpoint_map IS NULL "
        "AND acknowledged_by_checkpoint_digest IS NULL) OR "
        "(acknowledged_by_checkpoint_id IS NOT NULL "
        "AND acknowledged_by_checkpoint_thread_id IS NOT NULL "
        "AND acknowledged_by_checkpoint_ns IS NOT NULL "
        "AND acknowledged_by_root_checkpoint_id IS NOT NULL "
        "AND acknowledged_by_checkpoint_map IS NOT NULL "
        "AND acknowledged_by_checkpoint_digest IS NOT NULL))",
    )
    op.create_check_constraint(
        op.f("ck_feedback_acknowledgement_checkpoint_digest_length"),
        "feedback",
        "acknowledged_by_checkpoint_digest IS NULL "
        "OR length(acknowledged_by_checkpoint_digest) = 64",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_feedback_acknowledgement_checkpoint_digest_length"),
        "feedback",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_feedback_exact_acknowledgement_cursor_consistent"),
        "feedback",
        type_="check",
    )
    op.drop_column("feedback", "acknowledged_by_checkpoint_digest")
    op.drop_column("feedback", "acknowledged_by_checkpoint_map")
    op.drop_column("feedback", "acknowledged_by_root_checkpoint_id")
    op.drop_column("feedback", "acknowledged_by_checkpoint_ns")
    op.drop_column("feedback", "acknowledged_by_checkpoint_thread_id")
