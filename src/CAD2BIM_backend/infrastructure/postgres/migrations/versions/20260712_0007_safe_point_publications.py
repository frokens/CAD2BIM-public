"""Persist monotonic safe-point publication receipts.

Revision ID: 20260712_0007
Revises: 20260711_0006
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260712_0007"
down_revision: str | None = "20260711_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "ifc_run_projections",
        "source_checkpoint_ns",
        existing_type=sa.String(length=255),
        type_=sa.String(length=1_024),
        existing_nullable=False,
    )
    op.drop_constraint(
        "uq_checkpoint_links_run_checkpoint",
        "checkpoint_links",
        type_="unique",
    )
    op.alter_column(
        "checkpoint_links",
        "checkpoint_ns",
        existing_type=sa.String(length=255),
        type_=sa.String(length=1_024),
        existing_nullable=False,
        existing_server_default=sa.text("''"),
    )
    op.create_unique_constraint(
        "uq_checkpoint_links_run_checkpoint_cursor",
        "checkpoint_links",
        ["run_id", "checkpoint_ns", "checkpoint_id"],
    )

    op.create_table(
        "safe_point_publications",
        sa.Column("operation_id", sa.String(length=255), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(length=16),
            server_default=sa.text("'1'"),
            nullable=False,
        ),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column("stage_key", sa.String(length=255), nullable=False),
        sa.Column("safe_point_kind", sa.String(length=64), nullable=False),
        sa.Column("mcp_session_id", sa.String(length=80), nullable=False),
        sa.Column("mcp_snapshot_id", sa.String(length=80), nullable=False),
        sa.Column("artifact_id", sa.String(length=80), nullable=False),
        sa.Column("artifact_version_id", sa.String(length=80), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("source_checkpoint_id", sa.String(length=255), nullable=True),
        sa.Column("source_checkpoint_ns", sa.String(length=1_024), nullable=True),
        sa.Column("checkpoint_as_node", sa.String(length=255), nullable=True),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=255), nullable=True),
        sa.Column("checkpoint_ns", sa.String(length=1_024), nullable=True),
        sa.Column("root_checkpoint_id", sa.String(length=255), nullable=True),
        sa.Column("checkpoint_map", postgresql.JSONB(), nullable=True),
        sa.Column("checkpoint_map_digest", sa.String(length=64), nullable=True),
        sa.Column("checkpoint_next", postgresql.JSONB(), nullable=True),
        sa.Column("savepoint_id", sa.String(length=80), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "schema_version = '1'",
            name=op.f("ck_safe_point_publications_schema_version_valid"),
        ),
        sa.CheckConstraint(
            "phase IN ('prepared', 'checkpointed', 'linked')",
            name=op.f("ck_safe_point_publications_phase_valid"),
        ),
        sa.CheckConstraint(
            "length(checksum) = 64",
            name=op.f("ck_safe_point_publications_checksum_length"),
        ),
        sa.CheckConstraint(
            "checkpoint_map_digest IS NULL OR length(checkpoint_map_digest) = 64",
            name=op.f("ck_safe_point_publications_checkpoint_digest_length"),
        ),
        sa.CheckConstraint(
            "((source_checkpoint_id IS NULL AND source_checkpoint_ns IS NULL "
            "AND checkpoint_as_node IS NULL) OR "
            "(source_checkpoint_id IS NOT NULL AND source_checkpoint_ns IS NOT NULL))",
            name=op.f("ck_safe_point_publications_source_cursor_consistent"),
        ),
        sa.CheckConstraint(
            "((checkpoint_id IS NULL AND checkpoint_ns IS NULL "
            "AND root_checkpoint_id IS NULL AND checkpoint_map IS NULL "
            "AND checkpoint_map_digest IS NULL AND checkpoint_next IS NULL) OR "
            "(checkpoint_id IS NOT NULL AND checkpoint_ns IS NOT NULL "
            "AND root_checkpoint_id IS NOT NULL AND checkpoint_map IS NOT NULL "
            "AND checkpoint_map_digest IS NOT NULL AND checkpoint_next IS NOT NULL))",
            name=op.f("ck_safe_point_publications_checkpoint_fields_consistent"),
        ),
        sa.CheckConstraint(
            "((phase = 'prepared' AND checkpoint_id IS NULL AND savepoint_id IS NULL) OR "
            "(phase = 'checkpointed' AND checkpoint_id IS NOT NULL "
            "AND savepoint_id IS NULL) OR "
            "(phase = 'linked' AND checkpoint_id IS NOT NULL "
            "AND savepoint_id IS NOT NULL))",
            name=op.f("ck_safe_point_publications_phase_fields_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_safe_point_publications_owner_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_safe_point_publications_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "mcp_session_id"],
            ["mcp_sessions.owner_id", "mcp_sessions.task_id", "mcp_sessions.session_id"],
            name="fk_safe_point_publications_owner_task_mcp_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "mcp_session_id", "mcp_snapshot_id"],
            [
                "mcp_snapshots.owner_id",
                "mcp_snapshots.task_id",
                "mcp_snapshots.session_id",
                "mcp_snapshots.snapshot_id",
            ],
            name="fk_safe_point_publications_owner_task_mcp_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "artifact_id", "artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_safe_point_publications_owner_task_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "savepoint_id"],
            [
                "checkpoint_links.owner_id",
                "checkpoint_links.task_id",
                "checkpoint_links.savepoint_id",
            ],
            name="fk_safe_point_publications_owner_task_savepoint",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "operation_id",
            name=op.f("pk_safe_point_publications"),
        ),
        sa.UniqueConstraint(
            "run_id",
            "idempotency_key",
            name="uq_safe_point_publications_run_idempotency",
        ),
        sa.UniqueConstraint(
            "run_id",
            "checkpoint_ns",
            "checkpoint_id",
            name="uq_safe_point_publications_run_checkpoint_cursor",
        ),
        sa.UniqueConstraint(
            "savepoint_id",
            name="uq_safe_point_publications_savepoint",
        ),
    )
    op.create_index(
        "ix_safe_point_publications_owner_run_phase",
        "safe_point_publications",
        ["owner_id", "run_id", "phase"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_safe_point_publications_owner_run_phase",
        table_name="safe_point_publications",
    )
    op.drop_table("safe_point_publications")

    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM checkpoint_links
                    GROUP BY run_id, checkpoint_id
                    HAVING count(*) > 1
                ) THEN
                    RAISE EXCEPTION
                        'cannot downgrade: checkpoint IDs repeat across namespaces';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM checkpoint_links
                    WHERE char_length(checkpoint_ns) > 255
                ) THEN
                    RAISE EXCEPTION
                        'cannot downgrade: checkpoint namespace exceeds 255 characters';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM ifc_run_projections
                    WHERE char_length(source_checkpoint_ns) > 255
                ) THEN
                    RAISE EXCEPTION
                        'cannot downgrade: projection checkpoint namespace exceeds 255 characters';
                END IF;
            END
            $$;
            """
        )
    )
    op.drop_constraint(
        "uq_checkpoint_links_run_checkpoint_cursor",
        "checkpoint_links",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_checkpoint_links_run_checkpoint",
        "checkpoint_links",
        ["run_id", "checkpoint_id"],
    )
    op.alter_column(
        "checkpoint_links",
        "checkpoint_ns",
        existing_type=sa.String(length=1_024),
        type_=sa.String(length=255),
        existing_nullable=False,
        existing_server_default=sa.text("''"),
    )
    op.alter_column(
        "ifc_run_projections",
        "source_checkpoint_ns",
        existing_type=sa.String(length=1_024),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
