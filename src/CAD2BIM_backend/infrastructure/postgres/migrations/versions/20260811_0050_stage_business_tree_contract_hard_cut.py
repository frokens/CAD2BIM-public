"""Remove development projections using the retired Stage business contract.

Revision ID: 20260811_0050
Revises: 20260810_0049
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260811_0050"
down_revision: str | None = "20260810_0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_QUARANTINE_REASON = "migration.stage_business_tree_contract_removed"


def upgrade() -> None:
    # Stage business/source schema markers participated in view, resource and
    # checkpoint-projection request fingerprints. Existing resource rows do
    # not retain every optional input needed to recompute those hashes. This
    # pre-release hard cut therefore removes the derived projections and makes
    # their development Runs non-resumable instead of fabricating identity or
    # adding a runtime compatibility reader.
    op.execute(
        sa.text(
            """
            CREATE TEMPORARY TABLE g4_removed_stage_business_runs
            ON COMMIT DROP AS
            SELECT DISTINCT owner_id, task_id, run_id
            FROM run_stage_business_views
            UNION
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
            """
            UPDATE runs AS run
            SET resume_from_savepoint_id = NULL
            FROM checkpoint_links AS link,
                 g4_removed_stage_business_runs AS affected
            WHERE run.owner_id = link.owner_id
              AND run.task_id = link.task_id
              AND run.resume_from_savepoint_id = link.savepoint_id
              AND link.owner_id = affected.owner_id
              AND link.task_id = affected.task_id
              AND link.run_id = affected.run_id
            """
        )
    )
    for table in (
        "user_savepoint_commands",
        "run_resume_operations",
        "safe_point_publications",
        "ifc_run_stage_projections",
        "ifc_run_projections",
        "checkpoint_links",
        "checkpoint_projection_receipts",
        "run_component_views",
        "run_artifact_views",
        "run_stage_business_views",
    ):
        op.execute(
            sa.text(
                f"""
                DELETE FROM {table} AS record
                USING g4_removed_stage_business_runs AS affected
                WHERE record.owner_id = affected.owner_id
                  AND record.task_id = affected.task_id
                  AND record.run_id = affected.run_id
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
                feedback_sealed = false,
                feedback_transition_subject_id = NULL,
                active_feedback_subject = NULL,
                error = '{_QUARANTINE_REASON}',
                ended_at = COALESCE(run.ended_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            FROM g4_removed_stage_business_runs AS affected
            WHERE run.owner_id = affected.owner_id
              AND run.task_id = affected.task_id
              AND run.run_id = affected.run_id
              AND run.status IN ('queued', 'running', 'paused', 'waiting_user')
            """
        )
    )

    op.drop_constraint(
        op.f("ck_run_stage_business_views_schema_version_valid"),
        "run_stage_business_views",
        type_="check",
    )
    op.drop_column("run_stage_business_views", "schema_version")

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
                  FROM g4_removed_stage_business_runs AS affected
                  WHERE affected.owner_id = task.owner_id
                    AND affected.task_id = task.task_id
              )
            """
        )
    )


def downgrade() -> None:
    # Restore only the database shape. Removed derived projections and their
    # exact fingerprints cannot be reconstructed safely by a schema downgrade.
    op.add_column(
        "run_stage_business_views",
        sa.Column(
            "schema_version",
            sa.String(length=16),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_check_constraint(
        op.f("ck_run_stage_business_views_schema_version_valid"),
        "run_stage_business_views",
        "schema_version = '1'",
    )
