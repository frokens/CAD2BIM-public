"""Persist exact-checkpoint projection receipts.

Revision ID: 20260712_0013
Revises: 20260712_0012
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260712_0013"
down_revision: str | None = "20260712_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "checkpoint_projection_receipts",
        sa.Column("receipt_id", sa.String(length=80), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=255), nullable=False),
        sa.Column("checkpoint_ns", sa.String(length=1024), nullable=False),
        sa.Column("root_checkpoint_id", sa.String(length=255), nullable=False),
        sa.Column("checkpoint_map_digest", sa.String(length=64), nullable=False),
        sa.Column("projector_name", sa.String(length=100), nullable=False),
        sa.Column("projector_version", sa.String(length=64), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("output_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("first_sequence", sa.BigInteger()),
        sa.Column("last_sequence", sa.BigInteger()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "schema_version = '1'",
            name="schema_version_valid",
        ),
        sa.CheckConstraint(
            "(first_sequence IS NULL) = (last_sequence IS NULL)",
            name="event_sequence_pair",
        ),
        sa.CheckConstraint(
            "first_sequence IS NULL OR first_sequence <= last_sequence",
            name="event_sequence_order",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_projection_receipts_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("receipt_id", name="pk_checkpoint_projection_receipts"),
        sa.UniqueConstraint(
            "run_id",
            "thread_id",
            "checkpoint_id",
            "checkpoint_ns",
            "root_checkpoint_id",
            "checkpoint_map_digest",
            "projector_name",
            "projector_version",
            name="uq_projection_receipts_exact_projector",
        ),
    )
    op.create_index(
        "ix_projection_receipts_owner_run_created",
        "checkpoint_projection_receipts",
        ["owner_id", "run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_projection_receipts_owner_run_created",
        table_name="checkpoint_projection_receipts",
    )
    op.drop_table("checkpoint_projection_receipts")
