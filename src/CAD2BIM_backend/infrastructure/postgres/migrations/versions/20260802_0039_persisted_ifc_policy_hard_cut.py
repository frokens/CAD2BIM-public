"""Cancel active development Runs without a persisted IFC policy.

Revision ID: 20260802_0039
Revises: 20260802_0038
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260802_0039"
down_revision: str | None = "20260802_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The execution policy is part of the immutable Run input frontier.  A
    # missing policy cannot be reconstructed during restart without silently
    # changing the historical command, so active development Runs are stopped.
    op.execute(
        sa.text(
            """
            CREATE TEMPORARY TABLE g2a_missing_ifc_policy_runs
            ON COMMIT DROP AS
            SELECT owner_id, task_id, run_id
            FROM runs
            WHERE module IN ('ifc', 'workflow')
              AND NOT COALESCE(
                  jsonb_typeof(input_payload -> 'execution_policy') = 'object',
                  FALSE
              )
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
                error = 'migration.ifc_execution_policy_missing',
                ended_at = COALESCE(run.ended_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            FROM g2a_missing_ifc_policy_runs AS invalid
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
            FROM g2a_missing_ifc_policy_runs AS invalid
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


def downgrade() -> None:
    # Cancelled development Runs cannot be made executable by fabricating a
    # policy that was absent from their immutable input frontier.
    pass
