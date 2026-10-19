"""Persist a stable run-scoped sequence for feedback.

Revision ID: 20260712_0008
Revises: 20260712_0007
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260712_0008"
down_revision: str | None = "20260712_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "feedback",
        sa.Column("sequence", sa.BigInteger(), nullable=True),
    )
    op.execute(
        "WITH ranked AS ("
        "SELECT feedback_id, "
        "row_number() OVER (PARTITION BY run_id ORDER BY created_at, feedback_id) AS sequence "
        "FROM feedback"
        ") "
        "UPDATE feedback AS target "
        "SET sequence = ranked.sequence "
        "FROM ranked "
        "WHERE target.feedback_id = ranked.feedback_id"
    )
    op.alter_column(
        "feedback",
        "sequence",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_feedback_sequence_positive"),
        "feedback",
        "sequence > 0",
    )
    op.create_unique_constraint(
        op.f("uq_feedback_run_sequence"),
        "feedback",
        ["run_id", "sequence"],
    )
    op.create_index(
        op.f("ix_feedback_owner_run_sequence"),
        "feedback",
        ["owner_id", "run_id", "sequence"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_feedback_owner_run_sequence"), table_name="feedback")
    op.drop_constraint(op.f("uq_feedback_run_sequence"), "feedback", type_="unique")
    op.drop_constraint(op.f("ck_feedback_sequence_positive"), "feedback", type_="check")
    op.drop_column("feedback", "sequence")
