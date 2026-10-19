"""Close feedback finalization and exact interrupt-resume races.

Revision ID: 20260728_0031
Revises: 20260728_0030
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260728_0031"
down_revision: str | None = "20260728_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "feedback_sealed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "feedback_delivery_batches",
        sa.Column("resume_interrupt_id", sa.String(length=80), nullable=True),
    )
    op.create_foreign_key(
        "fk_feedback_delivery_batches_resume_interrupt",
        "feedback_delivery_batches",
        "interrupts",
        ["resume_interrupt_id"],
        ["interrupt_id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_feedback_delivery_batches_resume_interrupt",
        "feedback_delivery_batches",
        ["resume_interrupt_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_feedback_delivery_batches_resume_interrupt",
        "feedback_delivery_batches",
        type_="unique",
    )
    op.drop_constraint(
        "fk_feedback_delivery_batches_resume_interrupt",
        "feedback_delivery_batches",
        type_="foreignkey",
    )
    op.drop_column("feedback_delivery_batches", "resume_interrupt_id")
    op.drop_column("runs", "feedback_sealed")
