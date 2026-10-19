"""Add shared Stage-scoped business read models.

Revision ID: 20260712_0014
Revises: 20260712_0013
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260712_0014"
down_revision: str | None = "20260712_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_stage_business_views",
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("module", sa.String(length=64), nullable=False),
        sa.Column("stage_key", sa.String(length=255), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("plan_id", sa.String(length=80), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("stage_plan", postgresql.JSONB(), nullable=False),
        sa.Column("business_tree", postgresql.JSONB(), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column("source_checkpoint_id", sa.String(length=255), nullable=False),
        sa.Column("source_checkpoint_ns", sa.String(length=1024), nullable=False),
        sa.Column("source_root_checkpoint_id", sa.String(length=255), nullable=False),
        sa.Column("source_checkpoint_map_digest", sa.String(length=64), nullable=False),
        sa.Column("projector_name", sa.String(length=100), nullable=False),
        sa.Column("projector_version", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("output_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("schema_version = '1'", name="schema_version_valid"),
        sa.CheckConstraint("plan_version >= 1", name="plan_version_positive"),
        sa.CheckConstraint("revision >= 1", name="revision_positive"),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_stage_business_views_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "run_id",
            "module",
            "stage_key",
            name="pk_run_stage_business_views",
        ),
    )
    op.create_index(
        "ix_stage_business_views_owner_run_updated",
        "run_stage_business_views",
        ["owner_id", "run_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_stage_business_views_owner_run_updated",
        table_name="run_stage_business_views",
    )
    op.drop_table("run_stage_business_views")
