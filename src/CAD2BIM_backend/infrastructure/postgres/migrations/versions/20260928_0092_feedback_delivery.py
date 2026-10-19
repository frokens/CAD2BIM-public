"""Require exact claimed input before a feedback batch can be delivered.

Revision ID: 20260928_0092
Revises: 20260927_0091
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "20260928_0092"
down_revision = "20260927_0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Old applied rows do not prove the full saved message, and old batches have
    # no exact input cursor. Refuse them before DDL; Alembic rolls back this step.
    op.execute(sa.text("""
        DO $preflight$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM feedback_commands
                WHERE status = 'applied'
                   OR applied_checkpoint_id IS NOT NULL OR applied_at IS NOT NULL
            ) OR EXISTS (SELECT 1 FROM feedback_delivery_batches) THEN
                RAISE EXCEPTION 'Existing feedback delivery lacks exact checkpoint evidence'
                    USING ERRCODE = '23514';
            END IF;
        END
        $preflight$
    """))
    op.drop_constraint(op.f("ck_feedback_commands_status_valid"), "feedback_commands", type_="check")
    op.drop_constraint(
        op.f("ck_feedback_commands_lifecycle_consistent"), "feedback_commands", type_="check"
    )
    op.alter_column(
        "feedback_commands", "applied_checkpoint_id", new_column_name="delivered_checkpoint_id"
    )
    op.alter_column("feedback_commands", "applied_at", new_column_name="delivered_at")
    op.create_check_constraint(
        op.f("ck_feedback_commands_status_valid"),
        "feedback_commands",
        "status IN ('accepted', 'claimed', 'delivered', 'superseded')",
    )
    op.create_check_constraint(
        op.f("ck_feedback_commands_lifecycle_consistent"),
        "feedback_commands",
        "((status = 'accepted' AND claimed_batch_id IS NULL "
        "AND delivered_checkpoint_id IS NULL AND delivered_at IS NULL) OR "
        "(status = 'claimed' AND claimed_batch_id IS NOT NULL "
        "AND delivered_checkpoint_id IS NULL AND delivered_at IS NULL) OR "
        "(status = 'delivered' AND claimed_batch_id IS NOT NULL "
        "AND delivered_checkpoint_id IS NOT NULL AND delivered_at IS NOT NULL) OR "
        "(status = 'superseded' AND claimed_batch_id IS NULL "
        "AND delivered_checkpoint_id IS NULL AND delivered_at IS NULL))",
    )
    op.drop_constraint(
        op.f("ck_feedback_delivery_batches_applied_fields_consistent"),
        "feedback_delivery_batches",
        type_="check",
    )
    op.alter_column(
        "feedback_delivery_batches",
        "applied_checkpoint_id",
        new_column_name="delivered_checkpoint_id",
    )
    op.alter_column(
        "feedback_delivery_batches", "applied_at", new_column_name="delivered_at"
    )
    op.alter_column(
        "feedback_delivery_batches", "claimed_checkpoint_id", nullable=False
    )
    op.add_column(
        "feedback_delivery_batches",
        sa.Column("claimed_checkpoint_cursor", JSONB(), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_feedback_delivery_batches_delivered_fields_consistent"),
        "feedback_delivery_batches",
        "(delivered_checkpoint_id IS NULL) = (delivered_at IS NULL)",
    )


def downgrade() -> None:
    op.execute(sa.text("""
        DO $preflight$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM feedback_commands WHERE status = 'delivered'
                   OR delivered_checkpoint_id IS NOT NULL OR delivered_at IS NOT NULL
            ) OR EXISTS (SELECT 1 FROM feedback_delivery_batches) THEN
                RAISE EXCEPTION 'Current feedback delivery cannot be downgraded'
                    USING ERRCODE = '23514';
            END IF;
        END
        $preflight$
    """))
    op.drop_constraint(
        op.f("ck_feedback_delivery_batches_delivered_fields_consistent"),
        "feedback_delivery_batches",
        type_="check",
    )
    op.drop_column("feedback_delivery_batches", "claimed_checkpoint_cursor")
    op.alter_column("feedback_delivery_batches", "claimed_checkpoint_id", nullable=True)
    op.alter_column(
        "feedback_delivery_batches", "delivered_at", new_column_name="applied_at"
    )
    op.alter_column(
        "feedback_delivery_batches",
        "delivered_checkpoint_id",
        new_column_name="applied_checkpoint_id",
    )
    op.create_check_constraint(
        op.f("ck_feedback_delivery_batches_applied_fields_consistent"),
        "feedback_delivery_batches",
        "(applied_checkpoint_id IS NULL) = (applied_at IS NULL)",
    )
    op.drop_constraint(
        op.f("ck_feedback_commands_lifecycle_consistent"), "feedback_commands", type_="check"
    )
    op.drop_constraint(op.f("ck_feedback_commands_status_valid"), "feedback_commands", type_="check")
    op.alter_column("feedback_commands", "delivered_at", new_column_name="applied_at")
    op.alter_column(
        "feedback_commands", "delivered_checkpoint_id", new_column_name="applied_checkpoint_id"
    )
    op.create_check_constraint(
        op.f("ck_feedback_commands_status_valid"),
        "feedback_commands",
        "status IN ('accepted', 'claimed', 'applied', 'superseded')",
    )
    op.create_check_constraint(
        op.f("ck_feedback_commands_lifecycle_consistent"),
        "feedback_commands",
        "((status = 'accepted' AND claimed_batch_id IS NULL "
        "AND applied_checkpoint_id IS NULL AND applied_at IS NULL) OR "
        "(status = 'claimed' AND claimed_batch_id IS NOT NULL "
        "AND applied_checkpoint_id IS NULL AND applied_at IS NULL) OR "
        "(status = 'applied' AND claimed_batch_id IS NOT NULL "
        "AND applied_checkpoint_id IS NOT NULL AND applied_at IS NOT NULL) OR "
        "(status = 'superseded' AND claimed_batch_id IS NULL "
        "AND applied_checkpoint_id IS NULL AND applied_at IS NULL))",
    )
