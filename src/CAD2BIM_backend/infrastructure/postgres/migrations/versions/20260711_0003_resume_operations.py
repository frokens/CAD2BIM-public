"""Persist atomic savepoint-resume intents and graph observations.

Revision ID: 20260711_0003
Revises: 20260711_0002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260711_0003"
down_revision: str | None = "20260711_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_resume_operations",
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("operation_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("savepoint_id", sa.String(length=80), nullable=False),
        sa.Column("target_mcp_session_id", sa.String(length=80), nullable=True),
        sa.Column("command_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("observed_checkpoint_id", sa.String(length=255), nullable=True),
        sa.Column("observed_outcome", sa.String(length=32), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_run_resume_operations_attempt_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "observed_outcome IS NULL OR observed_outcome IN ('completed', 'interrupted')",
            name=op.f("ck_run_resume_operations_observed_outcome_valid"),
        ),
        sa.CheckConstraint(
            "(observed_checkpoint_id IS NULL) = (observed_outcome IS NULL)",
            name=op.f("ck_run_resume_operations_observation_pair"),
        ),
        sa.CheckConstraint(
            "phase IN ('intent', 'graph_inflight', 'graph_observed', 'finalized')",
            name=op.f("ck_run_resume_operations_phase_valid"),
        ),
        sa.CheckConstraint(
            "((phase IN ('intent', 'graph_inflight')) AND observed_checkpoint_id IS NULL) "
            "OR ((phase IN ('graph_observed', 'finalized')) "
            "AND observed_checkpoint_id IS NOT NULL)",
            name=op.f("ck_run_resume_operations_phase_observation_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "target_mcp_session_id"],
            ["mcp_sessions.owner_id", "mcp_sessions.task_id", "mcp_sessions.session_id"],
            name="fk_run_resume_operations_owner_task_mcp_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_run_resume_operations_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "savepoint_id"],
            [
                "checkpoint_links.owner_id",
                "checkpoint_links.task_id",
                "checkpoint_links.savepoint_id",
            ],
            name="fk_run_resume_operations_owner_task_savepoint",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_run_resume_operations")),
        sa.UniqueConstraint(
            "operation_id",
            name="uq_run_resume_operations_operation_id",
        ),
    )
    op.create_index(
        "ix_run_resume_operations_owner_task_phase",
        "run_resume_operations",
        ["owner_id", "task_id", "phase"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_run_resume_operations_owner_task_phase",
        table_name="run_resume_operations",
    )
    op.drop_table("run_resume_operations")
