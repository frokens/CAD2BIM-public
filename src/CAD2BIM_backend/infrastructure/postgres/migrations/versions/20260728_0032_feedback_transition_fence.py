"""Distinguish temporary feedback handoff seals from finalization.

Revision ID: 20260728_0032
Revises: 20260728_0031
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260728_0032"
down_revision: str | None = "20260728_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "feedback_transition_subject_id",
            sa.String(length=100),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_runs_feedback_transition_requires_seal",
        "runs",
        "feedback_transition_subject_id IS NULL OR feedback_sealed",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_runs_feedback_transition_requires_seal",
        "runs",
        type_="check",
    )
    op.drop_column("runs", "feedback_transition_subject_id")
