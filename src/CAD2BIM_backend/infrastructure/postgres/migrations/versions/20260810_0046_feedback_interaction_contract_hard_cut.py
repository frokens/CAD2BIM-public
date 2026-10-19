"""Quarantine development Runs pinned to the removed feedback interaction contract.

Revision ID: 20260810_0046
Revises: 20260809_0045
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260810_0046"
down_revision: str | None = "20260809_0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CURRENT_PROTOCOL_DIGEST = (
    "b4c1c8e3e1dd8289b24ebc6f853861c312aa42c383cc1f34ba9bfd0069a9ddb3"
)
_QUARANTINE_REASON = "migration.feedback_interaction_contract_removed"


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
    # Prompt messages live in durable LangGraph state, while the active target
    # lives in runs.active_feedback_subject. Removing their schema discriminator
    # and changing their stable identities is one state-protocol hard cut. Old
    # checkpoints cannot be relabeled safely, so quarantine affected development
    # Runs and remove resumable links instead of adding a runtime decoder.
    op.execute(
        sa.text(
            f"""
            CREATE TEMPORARY TABLE g4_removed_feedback_interaction_runs
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
                 g4_removed_feedback_interaction_runs AS invalid
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
                USING g4_removed_feedback_interaction_runs AS invalid
                WHERE record.owner_id = invalid.owner_id
                  AND record.task_id = invalid.task_id
                  AND record.run_id = invalid.run_id
                """
            )
        )

    op.drop_constraint(
        op.f("ck_runs_active_feedback_subject_schema"),
        "runs",
        type_="check",
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
            FROM g4_removed_feedback_interaction_runs AS invalid
            WHERE run.owner_id = invalid.owner_id
              AND run.task_id = invalid.task_id
              AND run.run_id = invalid.run_id
            """
        )
    )
    # Quarantined development rows can also retain the retired JSON shape even
    # without a pinned protocol. Clear only this ephemeral target state so all
    # Run reads use the one current model.
    op.execute(
        sa.text(
            """
            UPDATE runs
            SET active_feedback_subject = NULL,
                feedback_transition_subject_id = NULL,
                feedback_sealed = false,
                updated_at = CURRENT_TIMESTAMP
            WHERE active_feedback_subject IS NOT NULL
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
                  FROM g4_removed_feedback_interaction_runs AS invalid
                  WHERE invalid.owner_id = task.owner_id
                    AND invalid.task_id = task.task_id
              )
            """
        )
    )


def downgrade() -> None:
    # Restore only the retired database shape. Quarantined checkpoints and
    # cleared active targets cannot be reconstructed safely.
    op.create_check_constraint(
        op.f("ck_runs_active_feedback_subject_schema"),
        "runs",
        "active_feedback_subject IS NULL "
        "OR active_feedback_subject ->> 'schema_version' = '1'",
    )
