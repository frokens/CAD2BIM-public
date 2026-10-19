"""Quarantine development Runs pinned to the removed failure contract.

Revision ID: 20260810_0048
Revises: 20260810_0047
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260810_0048"
down_revision: str | None = "20260810_0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CURRENT_PROTOCOL_DIGEST = (
    "3c10fc223d3a6fb30960ebed253bdecf6f187494c6971aec74637027c3d4d619"
)
_QUARANTINE_REASON = "migration.failure_signal_contract_removed"


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
    # Failure operation contexts and signals are embedded in checkpoints.
    # Their numeric discriminator was removed from the one current contract.
    # Existing checkpoint bytes cannot be relabeled safely, so affected
    # development Runs are quarantined instead of adding a runtime decoder.
    op.execute(
        sa.text(
            f"""
            CREATE TEMPORARY TABLE g4_removed_failure_contract_runs
            ON COMMIT DROP AS
            SELECT owner_id, task_id, run_id
            FROM runs
            WHERE execution_protocol_status = 'pinned'
              AND execution_protocol_digest IS DISTINCT FROM
                  '{_CURRENT_PROTOCOL_DIGEST}'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE runs AS run
            SET resume_from_savepoint_id = NULL
            FROM checkpoint_links AS link,
                 g4_removed_failure_contract_runs AS invalid
            WHERE run.owner_id = link.owner_id
              AND run.task_id = link.task_id
              AND run.resume_from_savepoint_id = link.savepoint_id
              AND link.owner_id = invalid.owner_id
              AND link.task_id = invalid.task_id
              AND link.run_id = invalid.run_id
            """
        )
    )
    for table in (
        "user_savepoint_commands",
        "run_resume_operations",
        "safe_point_publications",
        "ifc_run_stage_projections",
        "checkpoint_links",
    ):
        op.execute(
            sa.text(
                f"""
                DELETE FROM {table} AS record
                USING g4_removed_failure_contract_runs AS invalid
                WHERE record.owner_id = invalid.owner_id
                  AND record.task_id = invalid.task_id
                  AND record.run_id = invalid.run_id
                """
            )
        )

    op.execute(
        sa.text("DROP TRIGGER trg_runs_pinned_execution_protocol_immutable ON runs")
    )
    op.execute(sa.text("DROP FUNCTION prevent_pinned_execution_protocol_mutation()"))
    op.execute(
        sa.text(
            f"""
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
                reclaimed_lease = false,
                claim_not_before = NULL,
                pause_requested = false,
                cancel_requested = false,
                feedback_pending = false,
                feedback_sealed = false,
                feedback_transition_subject_id = NULL,
                active_feedback_subject = NULL,
                error = CASE
                    WHEN run.status IN ('queued', 'running', 'paused', 'waiting_user')
                    THEN '{_QUARANTINE_REASON}'
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
                execution_protocol_quarantine_reason = '{_QUARANTINE_REASON}',
                updated_at = CURRENT_TIMESTAMP
            FROM g4_removed_failure_contract_runs AS invalid
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
                  FROM g4_removed_failure_contract_runs AS invalid
                  WHERE invalid.owner_id = task.owner_id
                    AND invalid.task_id = task.task_id
              )
            """
        )
    )


def downgrade() -> None:
    # Quarantined checkpoints cannot be converted back safely. The database
    # shape itself is unchanged.
    pass
