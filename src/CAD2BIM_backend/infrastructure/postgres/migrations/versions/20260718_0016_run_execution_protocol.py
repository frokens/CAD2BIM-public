"""Pin immutable execution protocols and quarantine unprovable legacy Runs.

Revision ID: 20260718_0016
Revises: 20260713_0015
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260718_0016"
down_revision: str | None = "20260713_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "execution_protocol_status",
            sa.String(length=32),
            nullable=False,
            server_default="quarantined",
        ),
    )
    op.add_column(
        "runs",
        sa.Column("execution_protocol", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("execution_protocol_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("execution_protocol_digest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("execution_protocol_quarantine_reason", sa.Text(), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE runs
            SET execution_protocol_quarantine_reason = CASE
                WHEN module = 'workflow' AND stage_key = 'cad2bim_workflow'
                    THEN 'legacy.workflow_protocol_unprovable'
                WHEN module = 'ifc'
                    THEN 'legacy.standalone_ifc_not_production_bundle'
                ELSE 'legacy.execution_protocol_scope_unknown'
            END
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE runs
            SET status = 'failed',
                claimed_by = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                heartbeat_at = NULL,
                reclaimed_lease = false,
                pause_requested = false,
                cancel_requested = false,
                feedback_pending = false,
                error = concat_ws(
                    E'\n',
                    NULLIF(error, ''),
                    'compatibility.execution_protocol_quarantined:'
                        || execution_protocol_quarantine_reason
                ),
                ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            WHERE execution_protocol_status = 'quarantined'
              AND status IN ('queued', 'running', 'paused', 'waiting_user')
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE tasks AS task
            SET status = 'failed',
                updated_at = CURRENT_TIMESTAMP
            WHERE task.status IN ('running', 'paused', 'waiting_user')
              AND EXISTS (
                  SELECT 1
                  FROM runs AS quarantined_run
                  WHERE quarantined_run.task_id = task.task_id
                    AND quarantined_run.execution_protocol_status = 'quarantined'
                    AND quarantined_run.error LIKE
                        '%compatibility.execution_protocol_quarantined:%'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM runs AS active_run
                  WHERE active_run.task_id = task.task_id
                    AND active_run.status IN (
                        'queued', 'running', 'paused', 'waiting_user'
                    )
              )
            """
        )
    )
    op.alter_column(
        "runs",
        "execution_protocol_status",
        server_default=None,
    )
    op.create_check_constraint(
        "ck_runs_execution_protocol_mapping_valid",
        "runs",
        "(execution_protocol_status = 'pinned' "
        "AND execution_protocol IS NOT NULL "
        "AND execution_protocol_id IS NOT NULL "
        "AND execution_protocol_digest IS NOT NULL "
        "AND execution_protocol_quarantine_reason IS NULL) "
        "OR (execution_protocol_status = 'quarantined' "
        "AND execution_protocol IS NULL "
        "AND execution_protocol_id IS NULL "
        "AND execution_protocol_digest IS NULL "
        "AND execution_protocol_quarantine_reason IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_runs_execution_protocol_identity_consistent",
        "runs",
        "execution_protocol_status <> 'pinned' OR ("
        "execution_protocol_id IS NOT DISTINCT FROM "
        "(execution_protocol #>> '{protocol,protocol_id}') "
        "AND execution_protocol_digest IS NOT DISTINCT FROM "
        "(execution_protocol ->> 'protocol_digest'))",
    )
    op.create_check_constraint(
        "ck_runs_execution_protocol_digest_shape",
        "runs",
        "execution_protocol_digest IS NULL OR execution_protocol_digest ~ '^[0-9a-f]{64}$'",
    )
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


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER trg_runs_pinned_execution_protocol_immutable ON runs"))
    op.execute(sa.text("DROP FUNCTION prevent_pinned_execution_protocol_mutation()"))
    op.drop_constraint(
        "ck_runs_execution_protocol_digest_shape",
        "runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_runs_execution_protocol_identity_consistent",
        "runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_runs_execution_protocol_mapping_valid",
        "runs",
        type_="check",
    )
    op.drop_column("runs", "execution_protocol_quarantine_reason")
    op.drop_column("runs", "execution_protocol_digest")
    op.drop_column("runs", "execution_protocol_id")
    op.drop_column("runs", "execution_protocol")
    op.drop_column("runs", "execution_protocol_status")
