"""Hard-cut Component publication to validated authoring candidates.

Revision ID: 20260811_0054
Revises: 20260811_0053
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260811_0054"
down_revision: str | None = "20260811_0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Pre-release hard cut: primitive-only rows cannot prove the required
    # candidate/inspection/validation frontier and therefore are not migrated.
    op.execute("DELETE FROM component_promotions")
    op.execute("DELETE FROM run_component_views")
    op.execute("DELETE FROM component_catalog_events")

    for column_name in ("primitive_kind", "spec_sha256"):
        op.drop_column("run_component_views", column_name)
    op.add_column(
        "run_component_views",
        sa.Column("authoring_kind", sa.String(length=32), nullable=False),
    )
    op.add_column(
        "run_component_views",
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
    )
    op.add_column(
        "run_component_views",
        sa.Column("validation_report_digest", sa.String(length=64), nullable=False),
    )
    op.add_column(
        "run_component_views",
        sa.Column("validation_artifact_id", sa.String(length=80), nullable=False),
    )
    op.add_column(
        "run_component_views",
        sa.Column(
            "validation_artifact_version_id",
            sa.String(length=80),
            nullable=False,
        ),
    )
    op.add_column(
        "run_component_views",
        sa.Column("validation_artifact_version_no", sa.Integer(), nullable=False),
    )
    op.create_foreign_key(
        "fk_run_component_views_owner_task_validation_artifact_version",
        "run_component_views",
        "artifact_versions",
        [
            "owner_id",
            "task_id",
            "validation_artifact_id",
            "validation_artifact_version_id",
        ],
        ["owner_id", "task_id", "artifact_id", "version_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_run_component_views_validation_artifact_version_positive"),
        "run_component_views",
        "validation_artifact_version_no > 0",
    )

    for column_name in ("primitive_kind", "spec_sha256"):
        op.drop_column("component_catalog_events", column_name)
    for column in (
        sa.Column("validation_artifact_id", sa.String(length=80), nullable=False),
        sa.Column(
            "validation_artifact_version_id",
            sa.String(length=80),
            nullable=False,
        ),
        sa.Column("validation_artifact_version_no", sa.Integer(), nullable=False),
        sa.Column("validation_artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("ifc_type_guid", sa.String(length=22), nullable=False),
        sa.Column("authoring_kind", sa.String(length=32), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("recipe_digest", sa.String(length=64), nullable=False),
        sa.Column("validation_report_digest", sa.String(length=64), nullable=False),
        sa.Column("parameter_schema_json", sa.JSON(), nullable=False),
        sa.Column("ports_json", sa.JSON(), nullable=False),
        sa.Column("compatibility_json", sa.JSON(), nullable=False),
    ):
        op.add_column("component_catalog_events", column)
    op.create_foreign_key(
        "fk_component_catalog_validation_artifact",
        "component_catalog_events",
        "artifact_versions",
        [
            "owner_id",
            "task_id",
            "validation_artifact_id",
            "validation_artifact_version_id",
        ],
        ["owner_id", "task_id", "artifact_id", "version_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_component_catalog_events_validation_version_positive"),
        "component_catalog_events",
        "validation_artifact_version_no > 0",
    )
    op.create_check_constraint(
        op.f("ck_component_catalog_events_authoring_kind_valid"),
        "component_catalog_events",
        "authoring_kind IN ('declarative', 'parametric', 'template', 'composite')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM component_promotions")
    op.execute("DELETE FROM run_component_views")
    op.execute("DELETE FROM component_catalog_events")
    op.drop_constraint(
        op.f("ck_component_catalog_events_authoring_kind_valid"),
        "component_catalog_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_component_catalog_events_validation_version_positive"),
        "component_catalog_events",
        type_="check",
    )
    op.drop_constraint(
        "fk_component_catalog_validation_artifact",
        "component_catalog_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_run_component_views_validation_artifact_version_positive"),
        "run_component_views",
        type_="check",
    )
    op.drop_constraint(
        "fk_run_component_views_owner_task_validation_artifact_version",
        "run_component_views",
        type_="foreignkey",
    )
    for column_name in (
        "compatibility_json",
        "ports_json",
        "parameter_schema_json",
        "validation_report_digest",
        "recipe_digest",
        "manifest_digest",
        "authoring_kind",
        "ifc_type_guid",
        "validation_artifact_sha256",
        "validation_artifact_version_no",
        "validation_artifact_version_id",
        "validation_artifact_id",
    ):
        op.drop_column("component_catalog_events", column_name)
    op.add_column(
        "component_catalog_events",
        sa.Column("primitive_kind", sa.String(length=32), nullable=False),
    )
    op.add_column(
        "component_catalog_events",
        sa.Column("spec_sha256", sa.String(length=64), nullable=False),
    )

    for column_name in (
        "validation_artifact_version_no",
        "validation_artifact_version_id",
        "validation_artifact_id",
        "validation_report_digest",
        "manifest_digest",
        "authoring_kind",
    ):
        op.drop_column("run_component_views", column_name)
    op.add_column(
        "run_component_views",
        sa.Column("primitive_kind", sa.String(length=32), nullable=False),
    )
    op.add_column(
        "run_component_views",
        sa.Column("spec_sha256", sa.String(length=64), nullable=False),
    )
