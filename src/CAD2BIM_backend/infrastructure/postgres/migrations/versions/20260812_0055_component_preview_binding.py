"""Bind every Component publication to one immutable PNG preview.

Revision ID: 20260812_0055
Revises: 20260811_0054
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260812_0055"
down_revision: str | None = "20260811_0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_empty_pre_cut_component_state() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                populated text[] := ARRAY[]::text[];
            BEGIN
                IF EXISTS (SELECT 1 FROM run_component_views LIMIT 1) THEN
                    populated := array_append(populated, 'run_component_views');
                END IF;
                IF EXISTS (SELECT 1 FROM component_catalog_events LIMIT 1) THEN
                    populated := array_append(populated, 'component_catalog_events');
                END IF;
                IF cardinality(populated) > 0 THEN
                    RAISE EXCEPTION
                        'Component preview hard cut requires an empty development '
                        'Component projection and catalog; back up and rebuild before '
                        'retrying (populated: %)',
                        array_to_string(populated, ', ');
                END IF;
            END
            $$
            """
        )
    )


def upgrade() -> None:
    # Deliberately no compatibility default or destructive data rewrite. A
    # development database containing pre-cut Component rows fails closed and
    # must be explicitly backed up and rebuilt before this migration is retried.
    _require_empty_pre_cut_component_state()
    for column in (
        sa.Column("preview_digest", sa.String(length=64), nullable=False),
        sa.Column("preview_artifact_id", sa.String(length=80), nullable=False),
        sa.Column(
            "preview_artifact_version_id",
            sa.String(length=80),
            nullable=False,
        ),
        sa.Column("preview_artifact_version_no", sa.Integer(), nullable=False),
    ):
        op.add_column("run_component_views", column)
    op.create_foreign_key(
        "fk_run_component_views_owner_task_preview_artifact_version",
        "run_component_views",
        "artifact_versions",
        [
            "owner_id",
            "task_id",
            "preview_artifact_id",
            "preview_artifact_version_id",
        ],
        ["owner_id", "task_id", "artifact_id", "version_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_run_component_views_preview_artifact_version_positive"),
        "run_component_views",
        "preview_artifact_version_no > 0",
    )

    for column in (
        sa.Column("preview_artifact_id", sa.String(length=80), nullable=False),
        sa.Column(
            "preview_artifact_version_id",
            sa.String(length=80),
            nullable=False,
        ),
        sa.Column("preview_artifact_version_no", sa.Integer(), nullable=False),
        sa.Column("preview_artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("preview_digest", sa.String(length=64), nullable=False),
    ):
        op.add_column("component_catalog_events", column)
    op.create_foreign_key(
        "fk_component_catalog_preview_artifact",
        "component_catalog_events",
        "artifact_versions",
        [
            "owner_id",
            "task_id",
            "preview_artifact_id",
            "preview_artifact_version_id",
        ],
        ["owner_id", "task_id", "artifact_id", "version_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_component_catalog_events_preview_version_positive"),
        "component_catalog_events",
        "preview_artifact_version_no > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_component_catalog_events_preview_version_positive"),
        "component_catalog_events",
        type_="check",
    )
    op.drop_constraint(
        "fk_component_catalog_preview_artifact",
        "component_catalog_events",
        type_="foreignkey",
    )
    for column_name in (
        "preview_digest",
        "preview_artifact_sha256",
        "preview_artifact_version_no",
        "preview_artifact_version_id",
        "preview_artifact_id",
    ):
        op.drop_column("component_catalog_events", column_name)

    op.drop_constraint(
        op.f("ck_run_component_views_preview_artifact_version_positive"),
        "run_component_views",
        type_="check",
    )
    op.drop_constraint(
        "fk_run_component_views_owner_task_preview_artifact_version",
        "run_component_views",
        type_="foreignkey",
    )
    for column_name in (
        "preview_artifact_version_no",
        "preview_artifact_version_id",
        "preview_artifact_id",
        "preview_digest",
    ):
        op.drop_column("run_component_views", column_name)
