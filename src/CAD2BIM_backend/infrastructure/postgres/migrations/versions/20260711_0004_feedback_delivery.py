"""Persist two-phase feedback delivery and checkpoint acknowledgement.

Revision ID: 20260711_0004
Revises: 20260711_0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260711_0004"
down_revision: str | None = "20260711_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("feedback", sa.Column("delivery_id", sa.String(length=255), nullable=True))
    op.add_column("feedback", sa.Column("delivered_to", sa.String(length=255), nullable=True))
    op.add_column(
        "feedback",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "feedback",
        sa.Column(
            "acknowledged_by_checkpoint_id",
            sa.String(length=255),
            nullable=True,
        ),
    )
    op.add_column(
        "feedback",
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE feedback "
        "SET acknowledged_by_checkpoint_id = consumed_by_checkpoint_id, "
        "acknowledged_at = consumed_at "
        "WHERE status = 'consumed'"
    )
    op.drop_constraint(op.f("ck_feedback_status_valid"), "feedback", type_="check")
    op.create_check_constraint(
        op.f("ck_feedback_status_valid"),
        "feedback",
        "status IN ('pending', 'delivered', 'acknowledged', 'consumed', 'ignored', 'superseded')",
    )
    op.create_check_constraint(
        op.f("ck_feedback_delivery_fields_consistent"),
        "feedback",
        "(delivery_id IS NULL) = (delivered_to IS NULL) "
        "AND (delivery_id IS NULL) = (delivered_at IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_feedback_acknowledgement_fields_consistent"),
        "feedback",
        "(acknowledged_by_checkpoint_id IS NULL) = (acknowledged_at IS NULL)",
    )
    op.create_unique_constraint(
        op.f("uq_feedback_run_delivery"),
        "feedback",
        ["run_id", "delivery_id"],
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_feedback_run_delivery"), "feedback", type_="unique")
    op.drop_constraint(
        op.f("ck_feedback_acknowledgement_fields_consistent"),
        "feedback",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_feedback_delivery_fields_consistent"),
        "feedback",
        type_="check",
    )
    op.drop_constraint(op.f("ck_feedback_status_valid"), "feedback", type_="check")
    op.execute(
        "UPDATE feedback "
        "SET consumed_by_checkpoint_id = COALESCE("
        "consumed_by_checkpoint_id, acknowledged_by_checkpoint_id), "
        "consumed_at = COALESCE(consumed_at, acknowledged_at), "
        "status = CASE "
        "WHEN status = 'acknowledged' THEN 'consumed' "
        "WHEN status = 'delivered' THEN 'pending' "
        "ELSE status END"
    )
    op.create_check_constraint(
        op.f("ck_feedback_status_valid"),
        "feedback",
        "status IN ('pending', 'consumed', 'ignored', 'superseded')",
    )
    op.drop_column("feedback", "acknowledged_at")
    op.drop_column("feedback", "acknowledged_by_checkpoint_id")
    op.drop_column("feedback", "delivered_at")
    op.drop_column("feedback", "delivered_to")
    op.drop_column("feedback", "delivery_id")
