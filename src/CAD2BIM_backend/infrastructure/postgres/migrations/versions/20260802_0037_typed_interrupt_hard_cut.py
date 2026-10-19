"""Remove development interrupts that do not use the typed envelope.

Revision ID: 20260802_0037
Revises: 20260801_0036
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260802_0037"
down_revision: str | None = "20260801_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INVALID_TYPED_ENVELOPE = """
NOT (
    COALESCE(jsonb_typeof(payload) = 'object', FALSE)
    AND COALESCE(payload ->> 'schema_version' = '1', FALSE)
    AND COALESCE(jsonb_typeof(payload -> 'system_interrupt') = 'object', FALSE)
    AND COALESCE(jsonb_typeof(payload -> 'runtime') = 'object', FALSE)
)
"""


def upgrade() -> None:
    # CAD2BIM has not been released.  An untyped interrupt cannot be resumed
    # from an exact cursor without inventing checkpoint lineage, so affected
    # development Runs are cancelled and the incompatible rows are discarded.
    op.execute(
        sa.text(
            f"""
            CREATE TEMPORARY TABLE g2a_invalid_interrupt_runs
            ON COMMIT DROP AS
            SELECT DISTINCT owner_id, task_id, run_id
            FROM interrupts
            WHERE {_INVALID_TYPED_ENVELOPE}
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
                error = 'compatibility.interrupt_contract_removed',
                ended_at = COALESCE(run.ended_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            FROM g2a_invalid_interrupt_runs AS invalid
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
            FROM g2a_invalid_interrupt_runs AS invalid
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
            DELETE FROM interrupts AS interrupt
            USING g2a_invalid_interrupt_runs AS invalid
            WHERE interrupt.owner_id = invalid.owner_id
              AND interrupt.task_id = invalid.task_id
              AND interrupt.run_id = invalid.run_id
              AND NOT (
                  COALESCE(jsonb_typeof(interrupt.payload) = 'object', FALSE)
                  AND COALESCE(interrupt.payload ->> 'schema_version' = '1', FALSE)
                  AND COALESCE(
                      jsonb_typeof(interrupt.payload -> 'system_interrupt') = 'object',
                      FALSE
                  )
                  AND COALESCE(
                      jsonb_typeof(interrupt.payload -> 'runtime') = 'object',
                      FALSE
                  )
              )
            """
        )
    )


def downgrade() -> None:
    # Removed development rows cannot be reconstructed without fabricating an
    # exact checkpoint cursor.  Runtime compatibility is intentionally absent.
    pass
