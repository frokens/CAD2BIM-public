"""Remove runtime MD5 snapshots and enforce one SHA-256 snapshot contract.

Revision ID: 20260803_0041
Revises: 20260803_0040
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260803_0041"
down_revision: str | None = "20260803_0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CAD2BIM has not been released. Historical MD5 development snapshots are
    # not converted into authoritative SHA-256 facts because their Artifact
    # bytes and writer generation cannot be proven. Any dependent resumable
    # state is cancelled before the rows are deleted.
    op.execute(
        sa.text(
            """
            CREATE TEMPORARY TABLE g2c_md5_snapshots
            ON COMMIT DROP AS
            SELECT owner_id, task_id, session_id, snapshot_id
            FROM mcp_snapshots
            WHERE checksum_algorithm = 'md5'
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TEMPORARY TABLE g2c_md5_savepoints
            ON COMMIT DROP AS
            SELECT link.owner_id, link.task_id, link.run_id, link.savepoint_id
            FROM checkpoint_links AS link
            JOIN g2c_md5_snapshots AS snapshot
              ON snapshot.owner_id = link.owner_id
             AND snapshot.task_id = link.task_id
             AND snapshot.session_id = link.mcp_session_id
             AND snapshot.snapshot_id = link.mcp_snapshot_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TEMPORARY TABLE g2c_affected_runs
            ON COMMIT DROP AS
            SELECT DISTINCT owner_id, task_id, run_id
            FROM g2c_md5_savepoints
            UNION
            SELECT DISTINCT publication.owner_id, publication.task_id, publication.run_id
            FROM safe_point_publications AS publication
            JOIN g2c_md5_snapshots AS snapshot
              ON snapshot.owner_id = publication.owner_id
             AND snapshot.task_id = publication.task_id
             AND snapshot.session_id = publication.mcp_session_id
             AND snapshot.snapshot_id = publication.mcp_snapshot_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM run_resume_operations AS operation
            USING g2c_md5_savepoints AS invalid
            WHERE operation.owner_id = invalid.owner_id
              AND operation.task_id = invalid.task_id
              AND operation.savepoint_id = invalid.savepoint_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE ifc_run_stage_projections AS projection
            SET status = 'cancelled',
                stage_plan_id = NULL,
                stage_plan_revision = NULL,
                accepted_tree_revision = NULL,
                accepted_tree_fingerprint = NULL,
                accepted_savepoint_id = NULL,
                accepted_artifact_run_id = NULL,
                accepted_artifact_id = NULL,
                accepted_artifact_version_id = NULL,
                accepted_artifact = NULL,
                context_card = NULL,
                evidence_refs = '[]'::jsonb,
                failure_reason = 'migration.snapshot_md5_removed'
            FROM g2c_md5_savepoints AS invalid
            WHERE projection.owner_id = invalid.owner_id
              AND projection.task_id = invalid.task_id
              AND projection.run_id = invalid.run_id
              AND projection.accepted_savepoint_id = invalid.savepoint_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM safe_point_publications AS publication
            USING g2c_md5_snapshots AS snapshot
            WHERE publication.owner_id = snapshot.owner_id
              AND publication.task_id = snapshot.task_id
              AND publication.mcp_session_id = snapshot.session_id
              AND publication.mcp_snapshot_id = snapshot.snapshot_id
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
                resume_from_savepoint_id = NULL,
                final_artifact_id = NULL,
                final_artifact_version_id = NULL,
                error = 'migration.snapshot_md5_removed',
                ended_at = COALESCE(run.ended_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            FROM g2c_affected_runs AS invalid
            WHERE run.owner_id = invalid.owner_id
              AND run.task_id = invalid.task_id
              AND run.run_id = invalid.run_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM checkpoint_links AS link
            USING g2c_md5_savepoints AS invalid
            WHERE link.owner_id = invalid.owner_id
              AND link.task_id = invalid.task_id
              AND link.savepoint_id = invalid.savepoint_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM mcp_snapshots AS snapshot
            USING g2c_md5_snapshots AS invalid
            WHERE snapshot.owner_id = invalid.owner_id
              AND snapshot.task_id = invalid.task_id
              AND snapshot.session_id = invalid.session_id
              AND snapshot.snapshot_id = invalid.snapshot_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE tasks AS task
            SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
            WHERE task.status <> 'archived'
              AND EXISTS (
                  SELECT 1
                  FROM g2c_affected_runs AS invalid
                  WHERE invalid.owner_id = task.owner_id
                    AND invalid.task_id = task.task_id
              )
            """
        )
    )
    op.drop_constraint(
        op.f("ck_mcp_snapshots_checksum_algorithm_supported"),
        "mcp_snapshots",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_mcp_snapshots_checksum_algorithm_supported"),
        "mcp_snapshots",
        "checksum_algorithm = 'sha256'",
    )


def downgrade() -> None:
    # Deleted development snapshots cannot be reconstructed and the removed
    # runtime reader must not be restored by a schema downgrade.
    pass

