"""Quarantine development Runs pinned to the removed protocol registry.

Revision ID: 20260803_0040
Revises: 20260802_0039
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260803_0040"
down_revision: str | None = "20260802_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CURRENT_PROTOCOL_ID = "cad2bim-workflow"


def _create_immutability_trigger() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION prevent_pinned_execution_protocol_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF OLD.execution_protocol_status = 'pinned'
                   AND (
                       NEW.execution_protocol_status IS DISTINCT FROM
                           OLD.execution_protocol_status
                       OR NEW.execution_protocol IS DISTINCT FROM
                           OLD.execution_protocol
                       OR NEW.execution_protocol_id IS DISTINCT FROM
                           OLD.execution_protocol_id
                       OR NEW.execution_protocol_digest IS DISTINCT FROM
                           OLD.execution_protocol_digest
                       OR NEW.execution_protocol_quarantine_reason IS DISTINCT FROM
                           OLD.execution_protocol_quarantine_reason
                   )
                THEN
                    RAISE EXCEPTION
                        'pinned execution protocol fields are immutable'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_runs_pinned_execution_protocol_immutable
            BEFORE UPDATE OF
                execution_protocol_status,
                execution_protocol,
                execution_protocol_id,
                execution_protocol_digest,
                execution_protocol_quarantine_reason
            ON runs
            FOR EACH ROW
            EXECUTE FUNCTION prevent_pinned_execution_protocol_mutation()
            """
        )
    )


def upgrade() -> None:
    # No released Run has a compatibility promise. A binding from the removed
    # registry cannot be converted without inventing a new Graph identity, so
    # its resumable control data is discarded and the Run is quarantined.
    op.execute(
        sa.text(
            f"""
            CREATE TEMPORARY TABLE g2b_noncurrent_protocol_runs
            ON COMMIT DROP AS
            SELECT owner_id, task_id, run_id
            FROM runs
            WHERE execution_protocol_status = 'pinned'
              AND execution_protocol_id IS DISTINCT FROM '{_CURRENT_PROTOCOL_ID}'
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM run_resume_operations AS operation
            USING checkpoint_links AS link, g2b_noncurrent_protocol_runs AS invalid
            WHERE operation.owner_id = link.owner_id
              AND operation.task_id = link.task_id
              AND operation.savepoint_id = link.savepoint_id
              AND link.owner_id = invalid.owner_id
              AND link.task_id = invalid.task_id
              AND link.run_id = invalid.run_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM checkpoint_links AS link
            USING g2b_noncurrent_protocol_runs AS invalid
            WHERE link.owner_id = invalid.owner_id
              AND link.task_id = invalid.task_id
              AND link.run_id = invalid.run_id
            """
        )
    )
    op.execute(
        sa.text("DROP TRIGGER trg_runs_pinned_execution_protocol_immutable ON runs")
    )
    op.execute(sa.text("DROP FUNCTION prevent_pinned_execution_protocol_mutation()"))
    op.execute(
        sa.text(
            """
            UPDATE runs AS run
            SET status = CASE
                    WHEN run.status IN ('queued', 'running', 'paused', 'waiting_user')
                    THEN 'cancelled'
                    ELSE run.status
                END,
                claimed_by = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                heartbeat_at = NULL,
                claim_not_before = NULL,
                error = CASE
                    WHEN run.status IN ('queued', 'running', 'paused', 'waiting_user')
                    THEN 'migration.execution_protocol_removed'
                    ELSE run.error
                END,
                ended_at = CASE
                    WHEN run.status IN ('queued', 'running', 'paused', 'waiting_user')
                    THEN COALESCE(run.ended_at, CURRENT_TIMESTAMP)
                    ELSE run.ended_at
                END,
                execution_protocol_status = 'quarantined',
                execution_protocol = NULL,
                execution_protocol_id = NULL,
                execution_protocol_digest = NULL,
                execution_protocol_quarantine_reason =
                    'migration.execution_protocol_removed',
                updated_at = CURRENT_TIMESTAMP
            FROM g2b_noncurrent_protocol_runs AS invalid
            WHERE run.owner_id = invalid.owner_id
              AND run.task_id = invalid.task_id
              AND run.run_id = invalid.run_id
            """
        )
    )
    _create_immutability_trigger()
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
                  FROM g2b_noncurrent_protocol_runs AS invalid
                  WHERE invalid.owner_id = task.owner_id
                    AND invalid.task_id = task.task_id
              )
            """
        )
    )


def downgrade() -> None:
    # Quarantined development Runs cannot safely recover the removed registry
    # identity or deleted checkpoint links. Runtime compatibility is absent.
    pass
