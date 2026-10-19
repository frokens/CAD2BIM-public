"""Add exact-checkpoint-derived Artifact and Component read models.

Revision ID: 20260713_0015
Revises: 20260712_0014
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260713_0015"
down_revision: str | None = "20260712_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _source_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column("source_checkpoint_id", sa.String(length=255), nullable=False),
        sa.Column("source_checkpoint_ns", sa.String(length=1024), nullable=False),
        sa.Column("source_root_checkpoint_id", sa.String(length=255), nullable=False),
        sa.Column("source_checkpoint_map_digest", sa.String(length=64), nullable=False),
        sa.Column("projector_name", sa.String(length=100), nullable=False),
        sa.Column("projector_version", sa.String(length=64), nullable=False),
        sa.Column("output_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
    )


def _timestamps() -> tuple[sa.Column, ...]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def upgrade() -> None:
    op.create_table(
        "run_artifact_views",
        sa.Column("resource_id", sa.String(length=80), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("module", sa.String(length=64), nullable=False),
        sa.Column("stage_key", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("artifact_id", sa.String(length=80), nullable=False),
        sa.Column("artifact_version_id", sa.String(length=80), nullable=False),
        sa.Column("artifact_version_no", sa.Integer(), nullable=False),
        sa.Column("subject_refs", postgresql.JSONB(), nullable=False),
        sa.Column("created_by_agent", sa.String(length=200)),
        sa.Column("attempt", sa.Integer()),
        *_source_columns(),
        *_timestamps(),
        sa.CheckConstraint("schema_version = '1'", name="schema_version_valid"),
        sa.CheckConstraint("category IN ('document', 'artifact')", name="category_valid"),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_run_artifact_views_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "artifact_id", "artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_run_artifact_views_owner_task_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("resource_id", name="pk_run_artifact_views"),
        sa.UniqueConstraint(
            "run_id",
            "artifact_id",
            "artifact_version_id",
            name="uq_run_artifact_views_run_artifact_version",
        ),
    )
    op.create_index(
        "ix_run_artifact_views_owner_run_created",
        "run_artifact_views",
        ["owner_id", "run_id", "created_at", "resource_id"],
    )

    op.create_table(
        "run_component_views",
        sa.Column("component_id", sa.String(length=80), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("stage_key", sa.String(length=255), nullable=False),
        sa.Column("requirement_id", sa.String(length=80), nullable=False),
        sa.Column("component_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("identity_source", sa.String(length=32)),
        sa.Column("ifc_type_class", sa.String(length=100)),
        sa.Column("primitive_kind", sa.String(length=32)),
        sa.Column("ifc_type_guid", sa.String(length=22)),
        sa.Column("spec_sha256", sa.String(length=64)),
        sa.Column("source_artifact_id", sa.String(length=80)),
        sa.Column("source_artifact_version_id", sa.String(length=80)),
        sa.Column("source_artifact_version_no", sa.Integer()),
        sa.Column("subject_refs", postgresql.JSONB(), nullable=False),
        sa.Column("created_by_agent", sa.String(length=200), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        *_source_columns(),
        *_timestamps(),
        sa.CheckConstraint("schema_version = '1'", name="schema_version_valid"),
        sa.CheckConstraint("status IN ('resolved', 'unresolved')", name="status_valid"),
        sa.CheckConstraint(
            "(source_artifact_id IS NULL) = (source_artifact_version_id IS NULL)",
            name="source_artifact_pair",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_run_component_views_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "source_artifact_id", "source_artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_run_component_views_owner_task_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("component_id", name="pk_run_component_views"),
        sa.UniqueConstraint(
            "run_id",
            "stage_key",
            "requirement_id",
            name="uq_run_component_views_run_stage_requirement",
        ),
    )
    op.create_index(
        "ix_run_component_views_owner_run_updated",
        "run_component_views",
        ["owner_id", "run_id", "updated_at", "component_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_run_component_views_owner_run_updated",
        table_name="run_component_views",
    )
    op.drop_table("run_component_views")
    op.drop_index(
        "ix_run_artifact_views_owner_run_created",
        table_name="run_artifact_views",
    )
    op.drop_table("run_artifact_views")
