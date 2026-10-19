"""Quarantine development Runs pinned to the removed Checkpoint/Graph contract.

Revision ID: 20260809_0043
Revises: 20260809_0042
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260809_0043"
down_revision: str | None = "20260809_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CURRENT_PROTOCOL_DIGEST = (
    "60b030070dbba09ca6b2283f2458da5d929a83a13ea4e2fa6982e83a66358ea7"
)
_QUARANTINE_REASON = "migration.checkpoint_graph_contract_removed"


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
    op.drop_constraint(
        op.f("ck_checkpoint_projection_receipts_schema_version_valid"),
        "checkpoint_projection_receipts",
        type_="check",
    )
    op.drop_column("checkpoint_projection_receipts", "schema_version")

    # The state schema digest and topology fingerprint include the current
    # Checkpoint/Graph contract. Existing development checkpoints cannot be
    # relabeled safely, so remove their resumable control links and quarantine
    # their Runs instead of adding a compatibility decoder.
    op.execute(
        sa.text(
            f"""
            CREATE TEMPORARY TABLE g4_removed_checkpoint_graph_runs
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
            DELETE FROM user_savepoint_commands AS command
            USING g4_removed_checkpoint_graph_runs AS invalid
            WHERE command.owner_id = invalid.owner_id
              AND command.task_id = invalid.task_id
              AND command.run_id = invalid.run_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM run_resume_operations AS operation
            USING g4_removed_checkpoint_graph_runs AS invalid
            WHERE operation.owner_id = invalid.owner_id
              AND operation.task_id = invalid.task_id
              AND operation.run_id = invalid.run_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM safe_point_publications AS publication
            USING g4_removed_checkpoint_graph_runs AS invalid
            WHERE publication.owner_id = invalid.owner_id
              AND publication.task_id = invalid.task_id
              AND publication.run_id = invalid.run_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM ifc_run_stage_projections AS projection
            USING g4_removed_checkpoint_graph_runs AS invalid
            WHERE projection.owner_id = invalid.owner_id
              AND projection.task_id = invalid.task_id
              AND projection.run_id = invalid.run_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE runs AS run
            SET resume_from_savepoint_id = NULL
            FROM checkpoint_links AS link,
                 g4_removed_checkpoint_graph_runs AS invalid
            WHERE run.owner_id = link.owner_id
              AND run.task_id = link.task_id
              AND run.resume_from_savepoint_id = link.savepoint_id
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
            USING g4_removed_checkpoint_graph_runs AS invalid
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
            FROM g4_removed_checkpoint_graph_runs AS invalid
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
                  FROM g4_removed_checkpoint_graph_runs AS invalid
                  WHERE invalid.owner_id = task.owner_id
                    AND invalid.task_id = task.task_id
              )
            """
        )
    )


def downgrade() -> None:
    # Restore only the removed database shape. Quarantined development Runs
    # cannot be relabeled or reconstructed by a schema downgrade.
    op.add_column(
        "checkpoint_projection_receipts",
        sa.Column(
            "schema_version",
            sa.String(length=16),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_check_constraint(
        op.f("ck_checkpoint_projection_receipts_schema_version_valid"),
        "checkpoint_projection_receipts",
        "schema_version = '1'",
    )
