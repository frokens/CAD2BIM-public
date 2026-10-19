"""Require one exact source cursor for every savepoint resume operation.

Revision ID: 20260802_0038
Revises: 20260802_0037
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260802_0038"
down_revision: str | None = "20260802_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A resume operation without the exact source cursor cannot be replayed or
    # reconciled safely.  Pre-release development rows are cancelled instead
    # of retaining a runtime adapter that fabricates checkpoint lineage.
    op.execute(
        sa.text(
            """
            CREATE TEMPORARY TABLE g2a_inexact_resume_runs
            ON COMMIT DROP AS
            SELECT DISTINCT owner_id, task_id, run_id
            FROM run_resume_operations
            WHERE cursor_schema_version IS DISTINCT FROM '1'
               OR source_checkpoint_cursor IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE runs AS run
            SET status = 'cancelled',
                claimed_by = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                heartbeat_at = NULL,
                claim_not_before = NULL,
                error = 'compatibility.resume_cursor_removed',
                ended_at = COALESCE(run.ended_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            FROM g2a_inexact_resume_runs AS invalid
            WHERE run.owner_id = invalid.owner_id
              AND run.task_id = invalid.task_id
              AND run.run_id = invalid.run_id
              AND run.status IN ('queued', 'running', 'paused', 'waiting_user')
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE tasks AS task
            SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
            FROM g2a_inexact_resume_runs AS invalid
            WHERE task.owner_id = invalid.owner_id
              AND task.task_id = invalid.task_id
              AND task.status IN ('draft', 'running', 'paused', 'waiting_user')
              AND NOT EXISTS (
                  SELECT 1
                  FROM runs AS active_run
                  WHERE active_run.owner_id = task.owner_id
                    AND active_run.task_id = task.task_id
                    AND active_run.status IN ('queued', 'running', 'paused', 'waiting_user')
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM run_resume_operations
            WHERE cursor_schema_version IS DISTINCT FROM '1'
               OR source_checkpoint_cursor IS NULL
            """
        )
    )

    op.drop_constraint(
        op.f("ck_run_resume_operations_exact_cursor_phase_consistent"),
        "run_resume_operations",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_run_resume_operations_cursor_schema_version_valid"),
        "run_resume_operations",
        type_="check",
    )
    op.alter_column(
        "run_resume_operations",
        "cursor_schema_version",
        existing_type=sa.String(length=16),
        nullable=False,
        server_default="1",
    )
    op.alter_column(
        "run_resume_operations",
        "source_checkpoint_cursor",
        existing_type=postgresql.JSONB(),
        nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_run_resume_operations_cursor_schema_version_valid"),
        "run_resume_operations",
        "cursor_schema_version = '1'",
    )
    op.create_check_constraint(
        op.f("ck_run_resume_operations_exact_cursor_phase_consistent"),
        "run_resume_operations",
        "cursor_schema_version = '1' AND source_checkpoint_cursor IS NOT NULL AND "
        "((phase IN ('intent', 'graph_inflight') "
        "AND observed_checkpoint_cursor IS NULL) OR "
        "(phase IN ('graph_observed', 'finalized') "
        "AND observed_checkpoint_cursor IS NOT NULL))",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_run_resume_operations_exact_cursor_phase_consistent"),
        "run_resume_operations",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_run_resume_operations_cursor_schema_version_valid"),
        "run_resume_operations",
        type_="check",
    )
    op.alter_column(
        "run_resume_operations",
        "source_checkpoint_cursor",
        existing_type=postgresql.JSONB(),
        nullable=True,
    )
    op.alter_column(
        "run_resume_operations",
        "cursor_schema_version",
        existing_type=sa.String(length=16),
        nullable=True,
        server_default=None,
    )
    op.create_check_constraint(
        op.f("ck_run_resume_operations_cursor_schema_version_valid"),
        "run_resume_operations",
        "cursor_schema_version IS NULL OR cursor_schema_version = '1'",
    )
    op.create_check_constraint(
        op.f("ck_run_resume_operations_exact_cursor_phase_consistent"),
        "run_resume_operations",
        "((cursor_schema_version IS NULL AND source_checkpoint_cursor IS NULL "
        "AND observed_checkpoint_cursor IS NULL) OR "
        "(cursor_schema_version = '1' AND source_checkpoint_cursor IS NOT NULL AND "
        "(((phase IN ('intent', 'graph_inflight')) "
        "AND observed_checkpoint_cursor IS NULL) OR "
        "((phase IN ('graph_observed', 'finalized')) "
        "AND observed_checkpoint_cursor IS NOT NULL))))",
    )
