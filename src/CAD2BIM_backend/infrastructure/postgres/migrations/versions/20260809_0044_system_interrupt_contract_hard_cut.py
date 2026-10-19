"""Remove persisted development interrupts using the retired contract.

Revision ID: 20260809_0044
Revises: 20260809_0043
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260809_0044"
down_revision: str | None = "20260809_0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_QUARANTINE_REASON = "migration.system_interrupt_contract_removed"


def upgrade() -> None:
    # CAD2BIM has not been released. Persisted interrupt fingerprints include
    # the removed schema field, so relabeling the JSON would make an old open
    # operation appear replay-equivalent to a current one. Cancel affected
    # active development Runs and discard their interrupt history instead of
    # adding a runtime decoder, dual reader, or fabricated fingerprint.
    op.execute(
        sa.text(
            """
            CREATE TEMPORARY TABLE g4_removed_system_interrupt_runs
            ON COMMIT DROP AS
            SELECT DISTINCT owner_id, task_id, run_id
            FROM interrupts
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE runs AS run
            SET status = 'cancelled',
                claimed_by = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                heartbeat_at = NULL,
                reclaimed_lease = false,
                claim_not_before = NULL,
                pause_requested = false,
                cancel_requested = false,
                feedback_pending = false,
                error = '{_QUARANTINE_REASON}',
                ended_at = COALESCE(run.ended_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            FROM g4_removed_system_interrupt_runs AS affected
            WHERE run.owner_id = affected.owner_id
              AND run.task_id = affected.task_id
              AND run.run_id = affected.run_id
              AND run.status IN ('queued', 'running', 'paused', 'waiting_user')
            """
        )
    )
    op.execute(sa.text("DELETE FROM interrupts"))
    op.execute(
        sa.text(
            """
            UPDATE tasks AS task
            SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
            WHERE task.status IN ('draft', 'running', 'paused', 'waiting_user')
              AND NOT EXISTS (
                  SELECT 1
                  FROM runs AS active_run
                  WHERE active_run.owner_id = task.owner_id
                    AND active_run.task_id = task.task_id
                    AND active_run.status IN ('queued', 'running', 'paused', 'waiting_user')
              )
              AND EXISTS (
                  SELECT 1
                  FROM g4_removed_system_interrupt_runs AS affected
                  WHERE affected.owner_id = task.owner_id
                    AND affected.task_id = task.task_id
              )
            """
        )
    )


def downgrade() -> None:
    # Deleted development interrupt rows and their request fingerprints cannot
    # be reconstructed safely. Runtime compatibility is intentionally absent.
    pass
