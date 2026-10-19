"""Remove persisted development Resource Views using the retired contract.

Revision ID: 20260809_0045
Revises: 20260809_0044
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260809_0045"
down_revision: str | None = "20260809_0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_QUARANTINE_REASON = "migration.resource_view_contract_removed"


def upgrade() -> None:
    # The retired schema marker participated in both delta and write-request
    # fingerprints. Resource rows do not retain enough information to recover
    # which optional expectations produced those hashes. Relabeling or
    # fabricating hashes would make a replay look idempotent when it is not.
    # This pre-release hard cut therefore discards the derived read models and
    # cancels only active development Runs that had published them.
    op.execute(
        sa.text(
            """
            CREATE TEMPORARY TABLE g4_removed_resource_view_runs
            ON COMMIT DROP AS
            SELECT DISTINCT owner_id, task_id, run_id
            FROM run_artifact_views
            UNION
            SELECT DISTINCT owner_id, task_id, run_id
            FROM run_component_views
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
            FROM g4_removed_resource_view_runs AS affected
            WHERE run.owner_id = affected.owner_id
              AND run.task_id = affected.task_id
              AND run.run_id = affected.run_id
              AND run.status IN ('queued', 'running', 'paused', 'waiting_user')
            """
        )
    )
    op.execute(sa.text("DELETE FROM run_component_views"))
    op.execute(sa.text("DELETE FROM run_artifact_views"))

    op.drop_constraint(
        op.f("ck_run_artifact_views_schema_version_valid"),
        "run_artifact_views",
        type_="check",
    )
    op.drop_column("run_artifact_views", "schema_version")
    op.drop_constraint(
        op.f("ck_run_component_views_schema_version_valid"),
        "run_component_views",
        type_="check",
    )
    op.drop_column("run_component_views", "schema_version")

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
                  FROM g4_removed_resource_view_runs AS affected
                  WHERE affected.owner_id = task.owner_id
                    AND affected.task_id = task.task_id
              )
            """
        )
    )


def downgrade() -> None:
    # Restore only the database shape. Deleted derived rows and their exact
    # fingerprints cannot be reconstructed safely by a schema downgrade.
    for table_name in ("run_artifact_views", "run_component_views"):
        op.add_column(
            table_name,
            sa.Column(
                "schema_version",
                sa.String(length=16),
                nullable=False,
                server_default="1",
            ),
        )
        op.create_check_constraint(
            op.f(f"ck_{table_name}_schema_version_valid"),
            table_name,
            "schema_version = '1'",
        )
